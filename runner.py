"""Check runner: captures all active URLs, compares against the previous
snapshot, records flagged changes, and prunes storage.

Storage policy: for every URL we always keep the newest snapshot. Older
snapshots are deleted UNLESS they are referenced by a flagged change (so you
can always open old diffs). This keeps disk usage flat even at 3-4k URLs.
"""
import asyncio
import hashlib
import json
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import capture
import compare as cmp
import db

RUN_LOCK = threading.Lock()

RUN_STATE = {
    "running": False,
    "paused": False,
    "stopping": False,
    "kind": "",
    "total": 0,
    "done": 0,
    "saved": 0,
    "changed": 0,
    "errors": 0,
    "started": "",
    "last_finished": "",
    "message": "",
}


def _snapshot_dir(url_id, ts_label):
    return db.SNAP_DIR / str(url_id) / ts_label


# breadcrumb is the page's own hierarchy, not library content: it lives on the
# urls row and must never reach search results.
NON_LIBRARY_CATS = {"breadcrumb"}


def item_key(category, title, item_url):
    """Stable id for one real-world item across every page that carries it.

    Identity is (category, normalized title, url): the same datasheet listed on
    forty product pages is ONE document with forty pages, while two distinct
    products sharing a vendor URL stay two items (title is part of the key).
    """
    norm = re.sub(r"\s+", " ", (title or "")).strip().lower()
    raw = f"{category}|{norm}|{(item_url or '').strip().rstrip('/').lower()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _same_page(a, b):
    strip = lambda u: (u or "").split("#")[0].split("?")[0].rstrip("/").lower()
    return strip(a) == strip(b)


# Infineon tags its own redirects (…/digital-health?redirId=135683). That param
# describes the hop, not the page, and must never end up on the watch list.
_REDIR_PARAMS = {"redirid", "redir_id"}


def _clean_redirect_url(u):
    """The destination address, without the redirect's own bookkeeping."""
    base, _, query = (u or "").split("#")[0].partition("?")
    keep = [p for p in query.split("&")
            if p and p.split("=")[0].strip().lower() not in _REDIR_PARAMS]
    return base + ("?" + "&".join(keep) if keep else "")


def _follow_redirect(con, url_id, old_url, final_url):
    """Keep the watch list pointed at the page that actually answers.

    A listed URL that 301s elsewhere is a stale address: the library under it
    describes a different page. So the list is repointed at the target and the
    old address dropped. If the target is already tracked, the stale row is
    removed outright rather than duplicating a page under two names.

    Returns (row_deleted, message). When row_deleted is True the caller MUST
    stop working on this url_id: the row is gone and anything still referencing
    it (snapshots, items) has cascaded away with it.
    """
    final_url = _clean_redirect_url(final_url)
    if not final_url or not final_url.startswith("http") \
            or _same_page(old_url, final_url):
        return False, ""
    existing = con.execute(
        "SELECT id FROM urls WHERE id<>? AND rtrim(url,'/')=?",
        (url_id, final_url.rstrip("/"))).fetchone()
    if existing:
        # the destination is already on the list: drop the stale twin rather
        # than track one page under two names. Its snapshots are captures of
        # the destination, which the destination's own row will make anyway.
        for s in con.execute("SELECT dir FROM snapshots WHERE url_id=?",
                             (url_id,)).fetchall():
            if s["dir"]:
                shutil.rmtree(s["dir"], ignore_errors=True)
        con.execute("DELETE FROM urls WHERE id=?", (url_id,))
        return True, f"{old_url} redirects to already-tracked {final_url} - removed"
    con.execute("UPDATE urls SET url=?, redirect_from=? WHERE id=?",
                (final_url, old_url, url_id))
    # the destination can be a DIFFERENT KIND of page (a retired part often
    # 301s to the evaluation-board finder - a design-resources page). Keeping
    # the old type would dress a finder tool up as a PTP, so re-type the row
    # from the new URL's shape and drop a level that no longer applies.
    new_pt = db.ptype_for_url(final_url)
    cur = con.execute("SELECT ptype FROM urls WHERE id=?",
                      (url_id,)).fetchone()
    if new_pt and cur and cur["ptype"] != new_pt:
        fl = final_url.lower()
        lvl = ("EB" if "/evaluation-board/" in fl
               else "PTP" if "/part/" in fl else "") if new_pt == "Part" else ""
        con.execute("UPDATE urls SET ptype=?, level=?, level_src=? WHERE id=?",
                    (new_pt, lvl, "inferred" if lvl else "", url_id))
    return False, f"{old_url} -> {final_url} (list repointed)"


def canonical_url(page_url, crumbs, final_url):
    """The page's LONG address - its true place in the hierarchy - even when
    the tracked link is shortened or language-prefixed. The page's own
    breadcrumb ends with itself carrying that full URL; it counts only when
    its final path segment matches the page's (a part's crumb ends at its
    family - not this page, so not its canonical). Falls back to the redirect
    target when the server sent us elsewhere."""
    def seg(u):
        return (u or "").split("#")[0].rstrip("/").rsplit("/", 1)[-1].lower()
    own = seg(page_url)
    for c in reversed(crumbs if isinstance(crumbs, list) else []):
        cu = (c.get("url") or "").strip() if isinstance(c, dict) else ""
        if cu and "infineon.com" in cu and seg(cu) == own:
            cu = cu.split("#")[0].rstrip("/")
            return "" if cu == page_url.rstrip("/") else cu
    fu = (final_url or "").split("#")[0].rstrip("/")
    if fu and fu != page_url.rstrip("/"):
        return fu
    return ""


def _snapshot_item_count(snap_dir):
    """How many library items a snapshot's sections.json holds - used to catch
    a suddenly-empty extraction before it overwrites a populated baseline."""
    p = Path(snap_dir) / "sections.json"
    if not p.exists():
        return 0
    try:
        sections = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return -1              # unreadable = don't trigger the empty guard
    n = 0
    for cat, items in sections.items():
        if isinstance(items, list) and cat not in NON_LIBRARY_CATS:
            n += sum(1 for it in items
                     if isinstance(it, dict) and (it.get("title") or "").strip())
    return n


def index_items(con, url_id, snap_dir, dead=False):
    """Refresh the searchable item index for a URL from its current snapshot.

    `dead` (HTTP >= 400) purges instead: an error page's own furniture is not
    library content, and indexing it would pollute search with the nav links
    of every retired part page.
    """
    con.execute("DELETE FROM items WHERE url_id=?", (url_id,))
    # the page's full text rides along: same lifecycle, same dead-page purge
    con.execute("DELETE FROM page_fts WHERE rowid=?", (url_id,))
    if not dead:
        tp = Path(snap_dir) / "text.txt"
        if tp.exists():
            try:
                con.execute("INSERT INTO page_fts(rowid, txt) VALUES(?, ?)",
                            (url_id, tp.read_text(encoding="utf-8",
                                                  errors="replace")[:200_000]))
            except Exception:
                pass
    p = Path(snap_dir) / "sections.json"
    if dead or not p.exists():
        return
    try:
        sections = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return
    rows = []
    for cat, items in sections.items():
        if not isinstance(items, list) or cat in NON_LIBRARY_CATS:
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            title = (it.get("title") or "").strip()
            if not title:
                continue
            rows.append((url_id, cat, (it.get("group") or "")[:80],
                         title[:300], (it.get("url") or "")[:600],
                         (it.get("meta") or "")[:260],
                         item_key(cat, title, it.get("url") or ""),
                         (it.get("dtype") or "")[:80],
                         (it.get("img") or "")[:400]))
    if rows:
        # drop byte-identical repeats (a page can list the same card twice).
        # Only fully-identical rows collapse - a language variant differs in
        # its URL or meta, so it is never touched.
        rows = list(dict.fromkeys(rows))
        con.executemany(
            "INSERT INTO items(url_id, category, grp, title, item_url, meta,"
            " item_key, doc_type, img) VALUES(?,?,?,?,?,?,?,?,?)", rows)


def _prune_snapshot(con, snap_row):
    """Delete a snapshot's files+row if no flagged change references it."""
    ref = con.execute(
        "SELECT 1 FROM changes WHERE old_snapshot_id=? OR new_snapshot_id=? LIMIT 1",
        (snap_row["id"], snap_row["id"])).fetchone()
    if ref:
        return
    d = Path(snap_row["dir"])
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    con.execute("DELETE FROM snapshots WHERE id=?", (snap_row["id"],))


def run_check(kind="manual", url_ids=None):
    """Run a full check. Returns run summary dict, or None if already running."""
    if not RUN_LOCK.acquire(blocking=False):
        return None
    try:
        return _run_check_inner(kind, url_ids)
    finally:
        RUN_STATE["running"] = False
        RUN_STATE["paused"] = False
        RUN_STATE["stopping"] = False
        RUN_LOCK.release()


def _run_check_inner(kind, url_ids):
    db.init_db()
    settings = db.get_settings()
    concurrency = max(1, int(settings.get("concurrency", 4)))

    con = db.connect()
    # Work the queue in NEED order, not id order: a page never captured has
    # no library at all (highest priority), a baseline page has only its first
    # capture, a failed page (blank/error) has no good current state - all of
    # those beat re-checking a page we already know is unchanged. Dead pages
    # come last: a 404 re-check is the least urgent thing in the run.
    priority = ("CASE WHEN last_checked IS NULL THEN 0"
                " WHEN IFNULL(last_status,'')='baseline' THEN 1"
                " WHEN IFNULL(last_status,'') IN ('blank','error') THEN 2"
                " WHEN IFNULL(last_status,'')='notlive' THEN 4"
                " ELSE 3 END")
    if url_ids:
        marks = ",".join("?" * len(url_ids))
        urls = con.execute(
            f"SELECT * FROM urls WHERE active=1 AND id IN ({marks})"
            f" ORDER BY {priority}, id", url_ids).fetchall()
    else:
        urls = con.execute(
            f"SELECT * FROM urls WHERE active=1"
            f" ORDER BY {priority}, id").fetchall()
    con.close()

    RUN_STATE.update(running=True, paused=False, stopping=False, kind=kind,
                     total=len(urls), done=0, saved=0, changed=0, errors=0,
                     started=db.now_iso(), started_ts=time.time(), message="")
    if not urls:
        RUN_STATE["message"] = "No active URLs to check."
        return {"total": 0, "ok": 0, "changed": 0, "errors": 0}

    ts_label = datetime.now().strftime("%Y%m%d-%H%M%S")

    con = db.connect()
    run_cur = con.execute(
        "INSERT INTO runs(started, kind, total) VALUES(?, ?, ?)",
        (RUN_STATE["started"], kind, len(urls)))
    run_id = run_cur.lastrowid
    con.commit()

    # Save one page at a time: each capture is recorded, compared and COMMITTED
    # the instant it finishes, inside the progress callback. That callback runs
    # on this same thread (asyncio.run blocks it), so the one connection is
    # never touched concurrently, and an interruption loses only pages still
    # in flight - everything already captured is on disk and in the table.
    counters = {"ok": 0, "changed": 0, "errors": 0, "saved": 0}
    suspects = []
    url_by_addr = {row["url"]: row for row in urls}
    jobs = [(row["url"], str(_snapshot_dir(row["id"], ts_label))) for row in urls]

    def progress(res):
        RUN_STATE["done"] += 1
        RUN_STATE["message"] = res["url"]
        row = url_by_addr.get(res["url"])
        if row is not None:
            try:
                _handle_result(con, res, row, ts_label, counters, suspects)
                con.commit()
            except Exception as exc:            # one bad row must not sink the run
                try:
                    con.rollback()
                except Exception:
                    pass
                print("row error:", res.get("url"), exc, flush=True)
        RUN_STATE["changed"] = counters["changed"]
        RUN_STATE["errors"] = counters["errors"]
        RUN_STATE["saved"] = counters["saved"]
        try:
            con.execute("UPDATE runs SET ok=?, changed=?, errors=? WHERE id=?",
                        (counters["ok"], counters["changed"],
                         counters["errors"], run_id))
            con.commit()
        except Exception:
            pass

    def paused():
        return bool(RUN_STATE.get("paused"))

    def stopping():
        return bool(RUN_STATE.get("stopping"))

    asyncio.run(capture.capture_all(jobs, concurrency=concurrency,
                                    progress=progress, pause_check=paused,
                                    stop_check=stopping))

    # Suspected changes are re-captured once, all together, and flagged only if
    # the difference survives (real edits persist; first-load flakes vanish).
    # A user-requested stop skips this pass - everything captured is already saved.
    if suspects and not stopping():
        _verify_suspects(con, suspects, ts_label, concurrency, counters, run_id)

    ok, changed, errors = counters["ok"], counters["changed"], counters["errors"]
    stopped = stopping()
    con.execute("UPDATE runs SET finished=?, ok=?, changed=?, errors=? WHERE id=?",
                (db.now_iso(), ok, changed, errors, run_id))
    con.commit()
    con.close()

    RUN_STATE.update(changed=changed, errors=errors, last_finished=db.now_iso(),
                     message=f"stopped early - {ok + errors} of {len(urls)} captured"
                             if stopped else "finished")
    return {"total": len(urls), "ok": ok, "changed": changed, "errors": errors,
            "stopped": stopped}


def _handle_result(con, res, row, ts_label, counters, suspects):
    """Record ONE finished capture and decide its fate: error/blank, redirect,
    baseline, unchanged, or queue as a suspected change. Mutates counters and
    suspects; the caller owns the commit (one commit per page)."""
    url_id = row["id"]
    snap_dir = str(_snapshot_dir(url_id, ts_label))
    now = db.now_iso()

    cur = con.execute(
        "INSERT INTO snapshots(url_id, ts, dir, status, error, page_title,"
        " text_lines, image_count, http_status) VALUES(?,?,?,?,?,?,?,?,?)",
        (url_id, now, snap_dir, res["status"], res["error"],
         res.get("title", ""), res.get("text_lines", 0),
         res.get("image_count", 0), res.get("http_status")))
    new_snap_id = cur.lastrowid

    if res["status"] != "ok":
        counters["errors"] += 1
        counters["saved"] += 1
        # "Infineon served an empty page" and "our capture broke" are different
        # facts and only one is the site owner's to fix. A blank render answers
        # with a 2xx and an empty <body>, so the HTTP code is real.
        blank = capture.BLANK_RENDER in (res.get("error") or "")
        con.execute(
            "UPDATE urls SET last_checked=?, last_status=?, last_http=?"
            " WHERE id=?",
            (now, "blank" if blank else "error", res.get("http_status"), url_id))
        # keep the previous good snapshot; drop the failed one's files
        shutil.rmtree(snap_dir, ignore_errors=True)
        con.execute("UPDATE snapshots SET dir='' WHERE id=?", (new_snap_id,))
        return

    counters["ok"] += 1
    # A page the site itself serves as gone (404/410) is not "captured fine":
    # it still gets a snapshot (so a revival shows up as a change) but reads as
    # not live and stays out of the library index. ONLY those two codes count -
    # a 403/429/5xx is the CDN blocking US or hiccuping, not the page's state
    # (capture already refuses those; this guard keeps the rule if one slips).
    dead = (res.get("http_status") or 0) in (404, 410)
    # A 404 page publishes a breadcrumb too ("Home > Error Pages > Page Not
    # Found") and it would sit in the product tree as if it were a product.
    # An error page's own furniture is not this page's hierarchy, the same
    # reason its images stay out of the library index.
    con.execute(
        "UPDATE urls SET last_http=?, breadcrumb=?, flags=?, components=?,"
        " detail_tabs=?, price=?, canonical_url=? WHERE id=?",
        (res.get("http_status"),
         json.dumps([] if dead else (res.get("breadcrumb") or []),
                    ensure_ascii=False),
         # a dead page's own furniture (its default hero, error-page table) is
         # not this page's content - drop the flags too, or they leak into the
         # type's component superset
         json.dumps({} if dead else (res.get("flags") or {}), ensure_ascii=False),
         json.dumps([] if dead else (res.get("components") or []),
                    ensure_ascii=False),
         json.dumps([] if dead else (res.get("detail_tabs") or []),
                    ensure_ascii=False),
         "" if dead else (res.get("price") or ""),
         "" if dead else canonical_url(row["url"],
                                       res.get("breadcrumb") or [],
                                       res.get("final_url") or ""),
         url_id))
    # SELF-HEAL at capture: a page that entered without a type gets typed from
    # its URL, and any URL-derivable level (PTP/EB, DRC/DRF/DRSF) is filled if
    # missing - so type/level can never silently stay blank for a captured page
    if not dead:
        pt, lvl, lsrc = db.type_level_for(row["url"])
        if pt and not (row["ptype"] or ""):
            con.execute("UPDATE urls SET ptype=? WHERE id=?", (pt, url_id))
        if lvl:
            con.execute("UPDATE urls SET level=?, level_src=?"
                        " WHERE id=? AND IFNULL(level,'')=''",
                        (lvl, lsrc, url_id))
    gone, rmsg = _follow_redirect(con, url_id, row["url"],
                                  res.get("final_url") or "")
    if rmsg:
        RUN_STATE["redirected"] = RUN_STATE.get("redirected", 0) + 1
        print("redirect:", rmsg, flush=True)
    if gone:
        counters["saved"] += 1        # the row (and its snapshot) no longer exist
        return
    prev = con.execute(
        "SELECT * FROM snapshots WHERE url_id=? AND status='ok' AND id<>?"
        " AND dir<>'' AND rejected=0 ORDER BY id DESC LIMIT 1",
        (url_id, new_snap_id)).fetchone()

    if prev is None:
        con.execute("UPDATE urls SET last_checked=?, last_status=? WHERE id=?",
                    (now, "notlive" if dead else "baseline", url_id))
        index_items(con, url_id, snap_dir, dead)
        counters["saved"] += 1
        return

    result = cmp.compare_snapshots(prev["dir"], snap_dir, change_dir=None,
                                   settings=db.get_settings())

    # GUARD: text UNCHANGED but a populated library suddenly empty = Infineon
    # renamed the markup our selectors depend on (a real content loss would
    # change the text too; a partial render under memory pressure changes it as
    # well). Refuse to overwrite the good baseline with emptiness - keep the old
    # snapshot + items, report an error so it is loud and retried. Without this,
    # one markup change could silently hollow out the library for a whole shape.
    if not dead and not result.get("text_changed"):
        prev_n = con.execute("SELECT COUNT(*) FROM items WHERE url_id=?",
                             (url_id,)).fetchone()[0]
        if prev_n >= 5 and _snapshot_item_count(snap_dir) == 0:
            con.execute("UPDATE urls SET last_checked=?, last_status='error'"
                        " WHERE id=?", (now, url_id))
            shutil.rmtree(snap_dir, ignore_errors=True)
            con.execute("UPDATE snapshots SET dir='' WHERE id=?", (new_snap_id,))
            counters["errors"] += 1
            counters["saved"] += 1
            print(f"[guard] {row['url']}: {prev_n} items -> 0 with text unchanged;"
                  " extractor likely broke, baseline kept", flush=True)
            return

    if result["any_changed"]:
        # Don't flag yet: late-loading widgets can make a page LOOK different
        # once. Re-capture at the end and flag only if the change persists
        # (real edits stay; loading flakes don't).
        suspects.append({"row": row, "new1_id": new_snap_id,
                         "new1_dir": snap_dir, "prev": prev})
    else:
        con.execute("UPDATE urls SET last_checked=?, last_status=? WHERE id=?",
                    (now, "notlive" if dead else "unchanged", url_id))
        index_items(con, url_id, snap_dir, dead)
        _prune_snapshot(con, prev)
        counters["saved"] += 1


def _verify_suspects(con, suspects, ts_label, concurrency, counters, run_id):
    """Re-capture every suspected change once and flag only those that persist.
    Each verified page is committed on its own."""
    RUN_STATE["message"] = f"double-checking {len(suspects)} suspected change(s)…"
    RUN_STATE["total"] += len(suspects)
    ts2 = ts_label + "-verify"
    vjobs = [(s["row"]["url"], str(_snapshot_dir(s["row"]["id"], ts2)))
             for s in suspects]

    def vprogress(res):
        RUN_STATE["done"] += 1
        RUN_STATE["message"] = res["url"]

    def paused():
        return bool(RUN_STATE.get("paused"))

    vresults = asyncio.run(capture.capture_all(
        vjobs, concurrency=concurrency, progress=vprogress, pause_check=paused))
    vby = {r["url"]: r for r in vresults}
    settings_now = db.get_settings()

    for s in suspects:
        row, prev = s["row"], s["prev"]
        url_id = row["id"]
        vres = vby[row["url"]]
        vdir = str(_snapshot_dir(url_id, ts2))
        now = db.now_iso()

        if vres["status"] != "ok":
            # Verification capture failed - keep the original detection rather
            # than risk missing a real change.
            shutil.rmtree(vdir, ignore_errors=True)
            confirm_id, confirm_dir = s["new1_id"], s["new1_dir"]
            confirmed = True
        else:
            cur = con.execute(
                "INSERT INTO snapshots(url_id, ts, dir, status, error,"
                " page_title, text_lines, image_count, http_status)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (url_id, now, vdir, "ok", "", vres.get("title", ""),
                 vres.get("text_lines", 0), vres.get("image_count", 0),
                 vres.get("http_status")))
            vsnap_id = cur.lastrowid
            recheck = cmp.compare_snapshots(prev["dir"], vdir, change_dir=None,
                                            settings=settings_now)
            confirmed = recheck["any_changed"]
            # the first suspect capture is superseded either way
            shutil.rmtree(s["new1_dir"], ignore_errors=True)
            con.execute("DELETE FROM snapshots WHERE id=?", (s["new1_id"],))
            confirm_id, confirm_dir = vsnap_id, vdir
            con.execute("UPDATE urls SET last_http=? WHERE id=?",
                        (vres.get("http_status"), url_id))

        # A page that JUST died is the most important change there is, so it
        # stays 'changed' (the Live column reports the 404); only a steady dead
        # page reads as 'notlive'. Gone means 404/410 - never a block code.
        dead_now = (vres.get("http_status") or 0) in (404, 410)
        if confirmed:
            counters["changed"] += 1
            change_dir = db.CHANGE_DIR / str(url_id) / ts_label
            result = cmp.compare_snapshots(prev["dir"], confirm_dir,
                                           change_dir=str(change_dir),
                                           settings=settings_now)
            con.execute(
                "INSERT INTO changes(url_id, ts, old_snapshot_id,"
                " new_snapshot_id, text_changed, images_changed,"
                " visual_changed, pixel_ratio, height_delta, lines_added,"
                " lines_removed, images_added, images_removed, summary, dir)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (url_id, now, prev["id"], confirm_id,
                 int(result["text_changed"]), int(result["images_changed"]),
                 int(result["visual_changed"]), result["pixel_ratio"],
                 result["height_delta"], result["lines_added"],
                 result["lines_removed"], result["images_added"],
                 result["images_removed"], result["summary"],
                 str(change_dir)))
            con.execute("UPDATE urls SET last_checked=?, last_status=?"
                        " WHERE id=?", (now, "changed", url_id))
            index_items(con, url_id, confirm_dir, dead_now)
        else:
            con.execute("UPDATE urls SET last_checked=?, last_status=?"
                        " WHERE id=?",
                        (now, "notlive" if dead_now else "unchanged", url_id))
            index_items(con, url_id, confirm_dir, dead_now)
            _prune_snapshot(con, prev)
        counters["saved"] += 1
        RUN_STATE["saved"] = counters["saved"]
        RUN_STATE["changed"] = counters["changed"]
        con.execute("UPDATE runs SET changed=? WHERE id=?",
                    (counters["changed"], run_id))
        con.commit()


def run_check_background(kind="manual", url_ids=None):
    """Fire-and-forget check in a daemon thread. Returns False if already running."""
    if RUN_STATE["running"]:
        return False
    # start clean: never inherit a pause/stop flag from a prior run
    RUN_STATE["paused"] = False
    RUN_STATE["stopping"] = False
    t = threading.Thread(target=run_check, kwargs={"kind": kind, "url_ids": url_ids},
                         daemon=True)
    t.start()
    # tiny wait so the UI immediately shows "running"
    time.sleep(0.3)
    return True
