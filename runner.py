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
    "kind": "",
    "total": 0,
    "done": 0,
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
    con.execute("UPDATE urls SET url=? WHERE id=?", (final_url, url_id))
    return False, f"{old_url} -> {final_url} (list repointed)"


def index_items(con, url_id, snap_dir, dead=False):
    """Refresh the searchable item index for a URL from its current snapshot.

    `dead` (HTTP >= 400) purges instead: an error page's own furniture is not
    library content, and indexing it would pollute search with the nav links
    of every retired part page.
    """
    con.execute("DELETE FROM items WHERE url_id=?", (url_id,))
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
                         (it.get("dtype") or "")[:80]))
    if rows:
        con.executemany(
            "INSERT INTO items(url_id, category, grp, title, item_url, meta,"
            " item_key, doc_type) VALUES(?,?,?,?,?,?,?,?)", rows)


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
        RUN_LOCK.release()


def _run_check_inner(kind, url_ids):
    db.init_db()
    settings = db.get_settings()
    concurrency = max(1, int(settings.get("concurrency", 4)))

    con = db.connect()
    if url_ids:
        marks = ",".join("?" * len(url_ids))
        urls = con.execute(
            f"SELECT * FROM urls WHERE active=1 AND id IN ({marks})", url_ids).fetchall()
    else:
        urls = con.execute("SELECT * FROM urls WHERE active=1 ORDER BY id").fetchall()
    con.close()

    RUN_STATE.update(running=True, kind=kind, total=len(urls), done=0,
                     changed=0, errors=0, started=db.now_iso(),
                     started_ts=time.time(), message="")
    if not urls:
        RUN_STATE["message"] = "No active URLs to check."
        return {"total": 0, "ok": 0, "changed": 0, "errors": 0}

    ts_label = datetime.now().strftime("%Y%m%d-%H%M%S")

    def progress(res):
        RUN_STATE["done"] += 1
        if res["status"] != "ok":
            RUN_STATE["errors"] += 1
        RUN_STATE["message"] = res["url"]

    con = db.connect()
    run_cur = con.execute(
        "INSERT INTO runs(started, kind, total) VALUES(?, ?, ?)",
        (RUN_STATE["started"], kind, len(urls)))
    run_id = run_cur.lastrowid
    con.commit()
    ok = changed = errors = 0

    # Process in small batches: every batch is captured, compared, verified
    # and COMMITTED before the next starts - so the table fills continuously
    # and an interruption loses at most one batch. Keep this near the
    # concurrency (a batch of ~2 waves) - a big batch means long stretches
    # where nothing is saved.
    BATCH = max(4, concurrency * 2)
    for batch_start in range(0, len(urls), BATCH):
        batch = urls[batch_start:batch_start + BATCH]
        ok_b, changed_b, errors_b = _process_batch(
            con, batch, ts_label, concurrency, progress)
        ok += ok_b
        changed += changed_b
        errors += errors_b
        RUN_STATE["changed"] = changed
        con.execute("UPDATE runs SET ok=?, changed=?, errors=? WHERE id=?",
                    (ok, changed, errors, run_id))
        con.commit()

    con.execute("UPDATE runs SET finished=?, ok=?, changed=?, errors=? WHERE id=?",
                (db.now_iso(), ok, changed, errors, run_id))
    con.commit()
    con.close()

    RUN_STATE.update(changed=changed, errors=errors, last_finished=db.now_iso(),
                     message="finished")
    return {"total": len(urls), "ok": ok, "changed": changed, "errors": errors}


def _process_batch(con, batch, ts_label, concurrency, progress):
    """Capture + compare + verify one batch of URL rows. Returns (ok, changed, errors)."""
    jobs = [(row["url"], str(_snapshot_dir(row["id"], ts_label))) for row in batch]
    url_by_addr = {row["url"]: row for row in batch}
    results = asyncio.run(capture.capture_all(jobs, concurrency=concurrency,
                                              progress=progress))
    ok = changed = errors = 0
    suspects = []   # pages that look changed - re-captured before flagging

    for res in results:
        row = url_by_addr[res["url"]]
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
            errors += 1
            # "Infineon served an empty page" and "our capture broke" are
            # different facts and only one of them is the site owner's to fix.
            # A blank render answers with a 2xx and an empty <body>, so the
            # HTTP code is real and worth keeping.
            blank = capture.BLANK_RENDER in (res.get("error") or "")
            con.execute(
                "UPDATE urls SET last_checked=?, last_status=?, last_http=?"
                " WHERE id=?",
                (now, "blank" if blank else "error",
                 res.get("http_status"), url_id))
            # keep the previous good snapshot; drop the failed one's files
            shutil.rmtree(snap_dir, ignore_errors=True)
            con.execute("UPDATE snapshots SET dir='' WHERE id=?", (new_snap_id,))
            continue

        ok += 1
        # A page the site itself serves as 404/500 is not "captured fine": it
        # still gets a snapshot (so a revival shows up as a change) but it is
        # reported as not live and kept out of the library index.
        dead = (res.get("http_status") or 0) >= 400
        con.execute(
            "UPDATE urls SET last_http=?, breadcrumb=?, flags=? WHERE id=?",
            (res.get("http_status"),
             json.dumps(res.get("breadcrumb") or [], ensure_ascii=False),
             json.dumps(res.get("flags") or {}, ensure_ascii=False), url_id))
        gone, rmsg = _follow_redirect(con, url_id, row["url"],
                                      res.get("final_url") or "")
        if rmsg:
            RUN_STATE["redirected"] = RUN_STATE.get("redirected", 0) + 1
            print("redirect:", rmsg, flush=True)
        if gone:
            con.commit()          # the row (and its snapshot) no longer exist
            continue
        prev = con.execute(
            "SELECT * FROM snapshots WHERE url_id=? AND status='ok' AND id<>?"
            " AND dir<>'' AND rejected=0 ORDER BY id DESC LIMIT 1",
            (url_id, new_snap_id)).fetchone()

        if prev is None:
            con.execute("UPDATE urls SET last_checked=?, last_status=? WHERE id=?",
                        (now, "notlive" if dead else "baseline", url_id))
            index_items(con, url_id, snap_dir, dead)
            con.commit()
            continue

        result = cmp.compare_snapshots(prev["dir"], snap_dir, change_dir=None,
                                       settings=db.get_settings())
        if result["any_changed"]:
            # Don't flag yet: late-loading widgets can make a page LOOK
            # different once. Capture again and only flag if the change
            # is still there (real edits persist; loading flakes don't).
            suspects.append({"row": row, "new1_id": new_snap_id,
                             "new1_dir": snap_dir, "prev": prev})
        else:
            con.execute("UPDATE urls SET last_checked=?, last_status=? WHERE id=?",
                        (now, "notlive" if dead else "unchanged", url_id))
            index_items(con, url_id, snap_dir, dead)
            _prune_snapshot(con, prev)
        con.commit()

    if suspects:
        RUN_STATE["message"] = f"double-checking {len(suspects)} suspected change(s)…"
        RUN_STATE["total"] += len(suspects)
        ts2 = ts_label + "-verify"
        vjobs = [(s["row"]["url"], str(_snapshot_dir(s["row"]["id"], ts2)))
                 for s in suspects]
        vresults = asyncio.run(capture.capture_all(
            vjobs, concurrency=concurrency, progress=progress))
        vby = {r["url"]: r for r in vresults}
        settings_now = db.get_settings()

        for s in suspects:
            row, prev = s["row"], s["prev"]
            url_id = row["id"]
            vres = vby[row["url"]]
            vdir = str(_snapshot_dir(url_id, ts2))
            now = db.now_iso()

            if vres["status"] != "ok":
                # Verification capture failed - keep the original detection
                # rather than risk missing a real change.
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
                recheck = cmp.compare_snapshots(prev["dir"], vdir,
                                                change_dir=None,
                                                settings=settings_now)
                confirmed = recheck["any_changed"]
                # the first suspect capture is superseded either way
                shutil.rmtree(s["new1_dir"], ignore_errors=True)
                con.execute("DELETE FROM snapshots WHERE id=?", (s["new1_id"],))
                confirm_id, confirm_dir = vsnap_id, vdir
                con.execute("UPDATE urls SET last_http=? WHERE id=?",
                            (vres.get("http_status"), url_id))

            # A page that JUST died is the most important change there is, so
            # it stays 'changed' (the Live column reports the 404); only a
            # steady dead page reads as 'notlive'.
            dead_now = (vres.get("http_status") or 0) >= 400
            if confirmed:
                changed += 1
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
            con.commit()

    return ok, changed, errors


def run_check_background(kind="manual", url_ids=None):
    """Fire-and-forget check in a daemon thread. Returns False if already running."""
    if RUN_STATE["running"]:
        return False
    t = threading.Thread(target=run_check, kwargs={"kind": kind, "url_ids": url_ids},
                         daemon=True)
    t.start()
    # tiny wait so the UI immediately shows "running"
    time.sleep(0.3)
    return True
