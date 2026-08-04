"""Check runner: captures all active URLs, compares against the previous
snapshot, records flagged changes, and prunes storage.

Storage policy: for every URL we always keep the newest snapshot. Older
snapshots are deleted UNLESS they are referenced by a flagged change (so you
can always open old diffs). This keeps disk usage flat even at 3-4k URLs.
"""
import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import threading
import time
import traceback
from collections import Counter
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
    """The destination address without the redirect's own bookkeeping OR any
    tracking junk the site glued on (intc campaign tags and friends) - one
    such tag once turned a repointed short link into a duplicate of the
    clean page it landed on."""
    return db.strip_junk_params((u or "").split("#")[0])


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
        "SELECT id, bl, pl, cm, owner, redirect_from FROM urls"
        " WHERE id<>? AND rtrim(url,'/')=?",
        (url_id, final_url.rstrip("/"))).fetchone()
    if existing:
        # the destination is already on the list: drop the stale twin rather
        # than track one page under two names. Its snapshots are captures of
        # the destination, which the destination's own row will make anyway.
        # Its OWNERSHIP is not a duplicate though - the CSS report lists these
        # pages under their legacy address, so that is the only row carrying
        # BL/PL/CM/Owner. Move it to the keeper before the row goes, or the
        # report's ownership dies here and re-importing just recreates the row.
        stale = con.execute("SELECT bl, pl, cm, owner FROM urls WHERE id=?",
                            (url_id,)).fetchone()
        for f in ("bl", "pl", "cm", "owner"):
            if (stale[f] or "").strip() and not (existing[f] or "").strip():
                con.execute(f"UPDATE urls SET {f}=? WHERE id=?",
                            (stale[f], existing["id"]))
        # Record on the KEEPER that this address leads here. Without it the
        # source report re-adds the legacy URL on every import, the next run
        # deletes it again, and 60-odd rows churn back and forth forever. The
        # importer matches redirect_from, so one round of this ends the loop.
        if not (existing["redirect_from"] or "").strip():
            con.execute("UPDATE urls SET redirect_from=? WHERE id=?",
                        (old_url, existing["id"]))
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


# Template chrome served on EVERY page - the loading spinner, the header and
# footer logos. Not page content: listing them says nothing about the page and
# drowns the one real image (the banner) in noise identical across the site.
_FURNITURE_IMG = re.compile(
    r"/etc\.clientlibs/|/content/dam/ifx/infineon_logo"
    # developer-community thread avatars and the community section icon: forum
    # users' pictures, not the page's content. These were only ever removed by
    # a boot sweep that DELETED the rows, so the indexer kept writing them and
    # the page kept RENDERING them - every one linking to an item page that
    # had just been swept away. One rule, so index and display cannot drift.
    r"|community\.infineon\.com|infineon-developer-community\.png", re.I)


def is_furniture_img(url):
    return bool(_FURNITURE_IMG.search(url or ""))


def img_title_fallback(url):
    """A display name for an image whose markup carried no alt text: the
    asset's own filename, with the Scene7 ':asset-…' rendition suffix, query
    and extension stripped - so '…/ai-visual-keyvisual-new.jpg:asset-680-2x1'
    reads as 'ai-visual-keyvisual-new' and earns an item page like any
    titled image instead of rendering as a bare URL."""
    u = (url or "").split("?")[0].rsplit("/", 1)[-1]
    u = u.split(":")[0]
    u = re.sub(r"\.[a-z0-9]{2,5}$", "", u, flags=re.I)
    return u.strip()


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
            if cat == "images":
                if is_furniture_img(it.get("url") or ""):
                    continue
                if not title:
                    title = img_title_fallback(it.get("url") or "")
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


# human labels for the diff, mirroring the app's section/component names
_DIFF_SECTIONS = [
    ("overview", "Overview"), ("subheadline", "Subheadline"),
    ("features", "Key features"), ("benefits", "Benefits"),
    ("applications", "Applications"), ("categories", "Browse by category"),
    ("products", "Related products"), ("documents", "Documents"),
    ("design_resources", "Design resources & boards"),
    ("trainings", "Trainings & webinars"), ("partners", "Partner solutions"),
    ("news", "News & highlights"), ("videos", "Videos"),
    ("images", "Images"), ("about", "About"),
]
_DIFF_FLAGS = [("hero_image", "Hero image"), ("product_table", "Products table"),
               ("developer_community", "Developer community")]


def component_changes(old_dir, new_dir, old_row, new_res, cmp_result):
    """Name WHAT changed, component by component.

    A review should read 'page text · News & highlights (+2/-1) · Hero image
    added' - three concrete things - instead of one opaque 'something
    changed'. Compares the two snapshots' captured sections item-by-item, the
    presence/emptiness flags, and the page's own section-nav list. Ownership
    metadata (BL/PL/CM/Owner) is deliberately NOT here: it is our bookkeeping,
    not the page's content, so re-importing the report never raises a review.
    """
    out = []
    if cmp_result.get("text_changed"):
        out.append(f"page text (+{cmp_result.get('lines_added', 0)}"
                   f"/-{cmp_result.get('lines_removed', 0)} lines)")

    def _sections(d):
        try:
            return json.loads((Path(d) / "sections.json")
                              .read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _keys(sec, cat):
        return {(str(i.get("title", "")).strip().lower(),
                 str(i.get("url", "")).strip())
                for i in (sec.get(cat) or []) if isinstance(i, dict)}
    # per-section adds/removes/in-place edits, from the same library comparison
    # that decided this page changed at all - so what raised the review and what
    # the review NAMES can never disagree
    lib = (cmp_result.get("library") or {}).get("per_section") or {}
    _lbl = dict(_DIFF_SECTIONS)
    for cat in sorted(lib, key=lambda c: [k for k, _ in _DIFF_SECTIONS].index(c)
                      if c in _lbl else 99):
        d = lib[cat]
        label = _lbl.get(cat, cat.replace("_", " ").capitalize())
        bits = []
        if d.get("added"):
            bits.append(f"+{d['added']}")
        if d.get("removed"):
            bits.append(f"-{d['removed']}")
        if d.get("changed"):
            bits.append(f"~{d['changed']}")
        if bits:
            out.append(f"{label} ({'/'.join(bits)})")

    def _json(v, default):
        try:
            return json.loads(v or default)
        except Exception:
            return json.loads(default)
    # THE OLD CAPTURE's own flags, not the urls row's. The row is a live
    # field that every capture overwrites, including a failed one - an errored
    # or dead capture writes {} over it - so comparing against the row asks
    # "does this page differ from whatever touched the row last", which is not
    # the question. It produced "Products table filled · section 'design
    # resources' added" on a page whose two captures were identical, because a
    # failure in between had emptied the row.
    #
    # sections.json has carried flags and components since it existed, so the
    # right answer is already on disk beside the capture that raised this. The
    # row is kept only as a fallback for captures older than that.
    _osec = _sections(old_dir)
    of = _osec.get("flags") or {}
    if not of and old_row is not None and "flags" in old_row.keys():
        of = _json(old_row["flags"], "{}")
    # THE NEW SIDE GETS THE SAME COURTESY. new_res is whatever result object
    # the caller had in hand - and in the verify path a suspect rebuilt from
    # a resumed run whose re-capture failed arrives as a bare error stub with
    # no flags and no components. Reading that stub as "the page now carries
    # nothing" wrote 'Hero image removed - Products table removed - section
    # X removed' six times over on change #6977 while the saved capture's own
    # sections.json listed every one of them, present and correct. The disk
    # is the record; the stub is a messenger that lost the envelope.
    _nsec = _sections(new_dir)
    nf = new_res.get("flags") or _nsec.get("flags") or {}
    for key, label in _DIFF_FLAGS:
        oe = bool((of.get(key) or {}).get("exists"))
        ne = bool((nf.get(key) or {}).get("exists"))
        if oe != ne:
            out.append(f"{label} {'added' if ne else 'removed'}")
        elif oe and ne:
            oc = bool((of.get(key) or {}).get("has_content"))
            nc = bool((nf.get(key) or {}).get("has_content"))
            if oc != nc:
                out.append(f"{label} {'filled' if nc else 'emptied'}")

    def _set(v):
        try:
            return {str(x).strip().lower() for x in (v or [])}
        except Exception:
            return set()
    _ocomp = _osec.get("components")
    if not _ocomp and old_row is not None and "components" in old_row.keys():
        _ocomp = _json(old_row["components"], "[]")
    onav = _set(_ocomp)
    nnav = _set(new_res.get("components") or _nsec.get("components") or [])
    # a section whose ITEMS already reported a change is not also reported as
    # a nav add/remove - one real change, one line
    said = {o.split(" (")[0].strip().lower() for o in out}
    for c in sorted(nnav - onav):
        if c not in said:
            out.append(f"section '{c}' added")
    for c in sorted(onav - nnav):
        if c not in said:
            out.append(f"section '{c}' removed")

    # images/visual only matter when nothing more specific already says it
    if cmp_result.get("images_changed") and not any("Images" in o for o in out):
        out.append(f"images (+{cmp_result.get('images_added', 0)}"
                   f"/-{cmp_result.get('images_removed', 0)})")
    if cmp_result.get("visual_changed") and not out:
        out.append(f"visual layout ({cmp_result.get('pixel_ratio', 0):.2f}% of pixels)")
    return out


def _restore_page_meta(con, url_id, prev):
    """Put the page's derived columns back to the last GOOD capture.

    _write_page_meta runs before the guards below get to judge the capture, so
    a partial render writes its half-empty flags and section-nav onto the urls
    row and the guard then throws the snapshot away without taking the row
    with it. The row is left describing a capture the system itself rejected -
    and that row is what the Dashboard filters on, and what the NEXT run
    compares against. One page came back reporting "Products table filled -
    section 'design resources' added" when both of its kept captures had both
    all along; the only thing that had ever lacked them was the discarded one.
    """
    try:
        sec = json.loads((Path(prev["dir"]) / "sections.json")
                         .read_text(encoding="utf-8"))
    except Exception:
        return
    con.execute(
        "UPDATE urls SET flags=?, components=?, detail_tabs=? WHERE id=?",
        (json.dumps(sec.get("flags") or {}, ensure_ascii=False),
         json.dumps(sec.get("components") or [], ensure_ascii=False),
         json.dumps(sec.get("detail_tabs") or [], ensure_ascii=False),
         url_id))


def _write_page_meta(con, url_id, page_url, res, dead):
    """Write every page-derived urls column from ONE capture result.

    Used by the first capture AND the verify re-capture, so the metadata
    (breadcrumb/flags/components/tabs/price/canonical) always describes the
    same capture the library items came from. Before this was shared, a
    dead<->alive flap between the two captures left the columns describing
    one state and the items another.

    A dead page's own furniture (error-page hero, its breadcrumb "Home >
    Error Pages") is not this page's content - everything empties, or the
    404 furniture leaks into the hierarchy and the type's component superset.
    """
    con.execute(
        "UPDATE urls SET last_http=?, breadcrumb=?, flags=?, components=?,"
        " detail_tabs=?, price=?, canonical_url=? WHERE id=?",
        (res.get("http_status"),
         json.dumps([] if dead else (res.get("breadcrumb") or []),
                    ensure_ascii=False),
         json.dumps({} if dead else (res.get("flags") or {}), ensure_ascii=False),
         json.dumps([] if dead else (res.get("components") or []),
                    ensure_ascii=False),
         json.dumps([] if dead else (res.get("detail_tabs") or []),
                    ensure_ascii=False),
         "" if dead else (res.get("price") or ""),
         "" if dead else canonical_url(page_url,
                                       res.get("breadcrumb") or [],
                                       res.get("final_url") or ""),
         url_id))


# How many runs in a row a page must fail EVERYTHING before the red badge.
# Every run already gives a failing page three attempts (first try + two
# retry rounds), so 2 here means SIX failed attempts across two separate
# checks before anyone is shown "Capture error" - Anas: the error state is
# for extreme cases, not for a page the CDN refused during one bad hour.
# Until the streak fills, the page keeps its previous status and simply
# gets tried again next run; one success resets the count to zero.
ERROR_RUNS = 2


def _streak_of(row):
    """The page's failed-run count so far, tolerant of rows without the
    column (old fixtures, plain dicts in tests)."""
    try:
        return row["error_streak"] or 0
    except Exception:
        return 0


def _wearing_error(row):
    """Is this page already carrying a capture failure from before? The loss
    guards key their second-sighting rule on this - and the streak counts
    even while the visible status is still being withheld, or a failure
    would need FOUR runs to publish instead of two."""
    try:
        if (row["last_status"] or "") == "error":
            return True
    except Exception:
        pass
    return _streak_of(row) > 0


def _fail_page(con, row, now):
    """One more failed run for this page. The streak decides whether the
    failure is finally SHOWN as an error, or the page quietly keeps its old
    status and the next run gets another go."""
    streak = _streak_of(row) + 1
    if streak >= ERROR_RUNS:
        con.execute("UPDATE urls SET last_checked=?, last_status='error',"
                    " error_streak=? WHERE id=?", (now, streak, row["id"]))
    else:
        con.execute("UPDATE urls SET last_checked=?, error_streak=?"
                    " WHERE id=?", (now, streak, row["id"]))


def _set_checked(con, url_id, now, status):
    """Record a check result WITHOUT hiding pending review work.

    'unchanged' must not overwrite 'changed' while an unreviewed change is
    open: the page being stable at its NEW content does not mean the human
    has looked at the edit. Without the guard, the next run after a flagged
    change flipped the row back to unchanged and it vanished from the
    Changed filter while still counting in the review queue. A death
    (notlive) always shows - a page going 404 outranks review bookkeeping.
    """
    con.execute("UPDATE urls SET last_checked=?, error_streak=0 WHERE id=?",
                (now, url_id))
    if status == "unchanged":
        con.execute(
            "UPDATE urls SET last_status='unchanged' WHERE id=? AND NOT EXISTS"
            " (SELECT 1 FROM changes c WHERE c.url_id=urls.id AND c.reviewed=0)",
            (url_id,))
    else:
        con.execute("UPDATE urls SET last_status=? WHERE id=?",
                    (status, url_id))


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


def _mark_item(con, run_id, url_id, state, prev_snap_id=None, snap_id=None):
    """Record a page's state within a run (queued -> suspect/done), so an
    interrupted run can resume. Best-effort: a failure here must never sink the
    run - it only costs the ability to resume THIS one page."""
    try:
        con.execute(
            "INSERT INTO run_items(run_id, url_id, state, prev_snap_id, snap_id)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(run_id, url_id) DO UPDATE SET state=excluded.state,"
            " prev_snap_id=COALESCE(excluded.prev_snap_id, run_items.prev_snap_id),"
            " snap_id=COALESCE(excluded.snap_id, run_items.snap_id)",
            (run_id, url_id, state, prev_snap_id, snap_id))
    except Exception:
        pass


def _resumable_run(con):
    """The most recent run that died mid-flight - unfinished, with items still
    to do - or None. Only a FULL run is auto-resumed; a partial (specific-ids)
    run is cheap to just re-issue, and telling them apart keeps resume simple."""
    r = con.execute(
        "SELECT * FROM runs WHERE finished IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not r:
        return None
    left = con.execute(
        "SELECT COUNT(*) FROM run_items WHERE run_id=? AND state<>'done'",
        (r["id"],)).fetchone()[0]
    return r if left else None


def _rebuild_suspects(con, run_id):
    """Reconstruct the held-suspect queue of an interrupted run from run_items,
    so its double-check resumes instead of being thrown away. A suspect whose
    pending snapshot or baseline is gone cannot be rebuilt - its url_id is
    returned to be re-captured cleanly instead."""
    suspects, requeue = [], []
    rows = con.execute(
        "SELECT url_id, prev_snap_id, snap_id FROM run_items"
        " WHERE run_id=? AND state='suspect'", (run_id,)).fetchall()
    for it in rows:
        row = con.execute("SELECT * FROM urls WHERE id=? AND active=1",
                          (it["url_id"],)).fetchone()
        prev = con.execute("SELECT * FROM snapshots WHERE id=?",
                          (it["prev_snap_id"],)).fetchone() if it["prev_snap_id"] else None
        new1 = con.execute("SELECT * FROM snapshots WHERE id=?",
                          (it["snap_id"],)).fetchone() if it["snap_id"] else None
        ok = (row and prev and new1 and new1["dir"]
              and os.path.isdir(new1["dir"]) and prev["dir"]
              and os.path.isdir(prev["dir"]))
        if ok:
            suspects.append({"row": row, "new1_id": new1["id"],
                             "new1_dir": new1["dir"], "prev": prev, "res": None})
        elif row is not None:
            requeue.append(row["id"])
    return suspects, requeue


def run_check(kind="manual", url_ids=None, baseline=False):
    """Run a full check. Returns run summary dict, or None if already running.

    baseline=True is the fresh-start mode for after a detection-method change:
    every ok capture BECOMES the page's baseline with no comparison, no
    suspects and no double-check - comparing against captures made under the
    old method would only measure the method, not the site. Errors, blanks,
    404s, redirects and the transient-retry rounds all behave exactly as in a
    normal run; the next normal run then detects real edits against the fresh
    baselines."""
    if not RUN_LOCK.acquire(blocking=False):
        return None
    try:
        return _run_check_inner(kind, url_ids, baseline=baseline)
    finally:
        RUN_STATE["running"] = False
        RUN_STATE["paused"] = False
        RUN_STATE["stopping"] = False
        RUN_LOCK.release()


def _init_db_patiently(delays=(20, 20, 20)):
    """init_db, but a locked database is a wait, not a verdict. A capture
    run resuming at boot died here twice while a mention collection held
    the write lock - the one serial write at run start now waits it out."""
    import sqlite3 as _sq
    for i, delay in enumerate([0] + list(delays)):
        if delay:
            time.sleep(delay)
        try:
            db.init_db()
            return
        except _sq.OperationalError as exc:
            if "locked" not in str(exc).lower() or i == len(delays):
                raise
            print(f"[run] database busy ({exc}) - retrying in"
                  f" {delays[min(i, len(delays) - 1)]}s", flush=True)


def _run_check_inner(kind, url_ids, baseline=False):
    _init_db_patiently()
    settings = db.get_settings()
    # 8, and do not raise it without re-running the three-pass check.
    #
    # 16 tabs IS 1.67x faster and reports no errors - and captures less. Two
    # passes at 8 are near-perfectly deterministic (item libraries identical on
    # 14/14 pages, one line of drift); the same pages at 16 came back with
    # different libraries on 6/14 and 54 text lines missing. Nothing errors,
    # because the waits are capped: under contention a page misses
    # networkidle inside its 8 s budget and gets read half-loaded. A quietly
    # incomplete capture is worse than a slow one - it becomes the baseline
    # that the next run diffs against.
    concurrency = max(1, int(settings.get("concurrency", 8)))

    # Work the queue in NEED order, not id order: a page never captured has
    # no library at all (highest priority), a baseline page has only its first
    # capture, a failed page (blank/error) has no good current state - all of
    # those beat re-checking a page we already know is unchanged. Dead pages
    # come last: a 404 re-check is the least urgent thing in the run.
    priority = ("CASE WHEN last_checked IS NULL THEN 0"
                " WHEN IFNULL(last_status,'') IN ('blank','error') THEN 1"
                " WHEN IFNULL(last_status,'')='notlive' THEN 3"
                " ELSE 2 END")
    ts_label = datetime.now().strftime("%Y%m%d-%H%M%S")

    con = db.connect()
    suspects = []
    is_resume = False
    keep_pending = set()

    # RESUME: if the last run died mid-flight, continue it instead of redoing
    # its captured work. Only a full run (no explicit id list) is auto-resumed;
    # a specific-ids run is cheap to just re-issue. Anything unexpected here
    # leaves is_resume False, so the worst case is a normal fresh run.
    resumable = _resumable_run(con) if not url_ids else None
    if resumable is not None:
        run_id = resumable["id"]
        # a crashed BASELINE run must resume as a baseline run - resuming it
        # in normal mode would compare against old-method captures
        if (resumable["kind"] or "") == "baseline":
            baseline = True
        suspects, requeue = _rebuild_suspects(con, run_id)
        keep_pending = {s["new1_id"] for s in suspects}
        # THE RETRY DEBT SURVIVES THE RESTART. A page that fails its first
        # capture is written as an error immediately and owed two retries at
        # the end of the run - but the retry queue is a list in memory, so a
        # restart paid nobody. Those pages sat 'done' in the ledger wearing an
        # error the run never confirmed. On resume they go back in the queue:
        # a clean capture heals them, and a page that keeps failing lands in
        # the fresh transient rounds and earns its error properly. 404s are
        # 'notlive', not 'error', so nothing gone is re-fetched by this.
        owed = [r["url_id"] for r in con.execute(
            "SELECT ri.url_id FROM run_items ri JOIN urls u ON u.id=ri.url_id"
            " WHERE ri.run_id=? AND ri.state='done'"
            " AND u.last_status='error' AND u.active=1", (run_id,)).fetchall()]
        if owed:
            marks = ",".join("?" * len(owed))
            con.execute(f"UPDATE run_items SET state='queued'"
                        f" WHERE run_id=? AND url_id IN ({marks})",
                        [run_id] + owed)
            con.commit()
            print(f"[resume] re-queueing {len(owed)} page(s) whose error was"
                  " never given its retries", flush=True)
        want = set(requeue) | set(owed) | {r["url_id"] for r in con.execute(
            "SELECT url_id FROM run_items WHERE run_id=? AND state='queued'",
            (run_id,)).fetchall()}
        urls = []
        if want:
            marks = ",".join("?" * len(want))
            urls = con.execute(
                f"SELECT * FROM urls WHERE active=1 AND id IN ({marks})"
                f" ORDER BY {priority}, id", list(want)).fetchall()
        is_resume = True
        print(f"[resume] continuing run {run_id}: {len(urls)} page(s) left to"
              f" capture, {len(suspects)} suspect(s) to double-check", flush=True)

    # Drop unverified 'pending' snapshots from an interrupted run - EXCEPT the
    # ones being resumed. A pending snapshot is a suspected change whose verify
    # never ran; a fresh run re-detects it, a resume keeps and finishes it.
    stale = con.execute("SELECT id, dir FROM snapshots"
                        " WHERE status='pending'").fetchall()
    dropped = 0
    for s in stale:
        if s["id"] in keep_pending:
            continue
        if s["dir"]:
            shutil.rmtree(s["dir"], ignore_errors=True)
        con.execute("DELETE FROM snapshots WHERE id=?", (s["id"],))
        dropped += 1
    if dropped:
        con.commit()
        print(f"[recovery] dropped {dropped} unverified snapshot(s) from"
              " an interrupted run", flush=True)

    if not is_resume:
        # Fresh run: build the full queue, create the run row, and record every
        # page as 'queued' so an interruption can resume from exactly here.
        if url_ids:
            marks = ",".join("?" * len(url_ids))
            urls = con.execute(
                f"SELECT * FROM urls WHERE active=1 AND id IN ({marks})"
                f" ORDER BY {priority}, id", url_ids).fetchall()
        else:
            urls = con.execute(
                f"SELECT * FROM urls WHERE active=1"
                f" ORDER BY {priority}, id").fetchall()
        run_cur = con.execute(
            "INSERT INTO runs(started, kind, total) VALUES(?, ?, ?)",
            (db.now_iso(), kind, len(urls)))
        run_id = run_cur.lastrowid
        try:
            con.executemany(
                "INSERT OR IGNORE INTO run_items(run_id, url_id, state)"
                " VALUES(?, ?, 'queued')", [(run_id, r["id"]) for r in urls])
        except Exception:
            pass
        con.commit()

    # pages_total = every page IN this run, a constant denominator for the
    # dashboard's outcome bar (each page sits in exactly one bucket: waiting /
    # no update / updated / held for the double-check / error). The event
    # totals (total/done) keep growing with the double-check and retry debts;
    # pages_total never moves.
    try:
        pages_total = con.execute(
            "SELECT COUNT(*) FROM run_items WHERE run_id=?",
            (run_id,)).fetchone()[0] or len(urls)
    except Exception:
        pages_total = len(urls)
    # A RESUMED RUN IS NOT A NEW RUN. These all started at zero and counted
    # only what was LEFT, so the bar came back reading "0 of 3,273" on a run
    # that had already done 163 of 3,436, with every bucket empty until the
    # first page landed and they all jumped at once. Seeded from what the run
    # has actually done, a resume picks the bar up where it put it down.
    #
    # done0 is the same number kept aside: the ETA divides pages by elapsed
    # time, so it has to rate only what THIS session captured, or a resume
    # would look like it had done 163 pages in two seconds.
    # counters BEFORE the state seed, which reads them: the seed used to
    # come first and referenced a name that did not exist yet, so every
    # run died on its first line with UnboundLocalError.
    counters = {"ok": 0, "changed": 0, "errors": 0, "saved": 0}
    if is_resume:
        # CARRY the finished run's tallies forward: a resume that started from
        # zero overwrote run 127's real ok=4,038 with the ok=1 of its own short
        # tail at the final UPDATE, and the run read as if it captured nothing.
        prev_row = con.execute("SELECT ok, changed, errors FROM runs"
                               " WHERE id=?", (run_id,)).fetchone()
        if prev_row:
            counters["ok"] = prev_row["ok"] or 0
            counters["changed"] = prev_row["changed"] or 0
            counters["errors"] = prev_row["errors"] or 0
        # ...and "saved" with them. It has no column on runs, but it means
        # "pages whose outcome is final", which is exactly what a done item is.
        #
        # Leaving it at zero did not just under-report: the dashboard draws
        # "no update" as saved - updated - errors, so a resumed run subtracted
        # CUMULATIVE updates and errors from a counter that had just restarted.
        # A run 40 errors deep showed 39 pages checked out of hundreds, and the
        # bar read as though almost nothing had been done.
        try:
            counters["saved"] = con.execute(
                "SELECT COUNT(*) FROM run_items WHERE run_id=? AND state='done'",
                (run_id,)).fetchone()[0] or 0
        except Exception:
            pass

    already = max(0, pages_total - len(urls))
    RUN_STATE.update(running=True, paused=False, stopping=False, kind=kind,
                     total=pages_total, done=already, done0=already,
                     saved=counters["saved"], saved0=counters["saved"],
                     changed=counters["changed"],
                     errors=counters["errors"],
                     suspects=len(suspects), started=db.now_iso(),
                     started_ts=time.time(), message="",
                     # the runner is the authority on WHERE the run is - the
                     # page renders these instead of guessing from messages
                     phase="capture", round=0, phase_total=pages_total,
                     phase_done=already, held=len(suspects),
                     pages_total=max(pages_total, len(urls) + len(suspects)))
    if not urls and not suspects:
        RUN_STATE["message"] = "Nothing left to check."
        try:
            con.execute("UPDATE runs SET finished=? WHERE id=?",
                        (db.now_iso(), run_id))
            con.commit()
        except Exception:
            pass
        con.close()
        return {"total": 0, "ok": 0, "changed": 0, "errors": 0}

    # Save one page at a time: each capture is recorded, compared and COMMITTED
    # the instant it finishes, inside the progress callback. That callback runs
    # on this same thread (asyncio.run blocks it), so the one connection is
    # never touched concurrently, and an interruption loses only pages still
    # in flight - everything already captured is on disk and in the table.
    # suspects is set up above (holds any rebuilt from a resumed run); the
    # capture loop appends newly-found ones to it.
    url_by_addr = {row["url"]: row for row in urls}
    # A capture is matched back to its row by URL text. If that lookup ever
    # misses, the page is captured, written to disk, and then dropped on the
    # floor - which is exactly what happened: `done` climbed to 2,462 while 118
    # rows moved, and nothing was logged because the miss had no else branch.
    # This second key is forgiving about the things that do not change which
    # page it is.
    def _key(u):
        u = (u or "").strip().lower()
        if u.endswith("/"):
            u = u[:-1]
        for pre in ("https://", "http://"):
            if u.startswith(pre):
                u = u[len(pre):]
                break
        return u[4:] if u.startswith("www.") else u

    url_by_key = {_key(row["url"]): row for row in urls}
    jobs = [(row["url"], str(_snapshot_dir(row["id"], ts_label))) for row in urls]

    # Pages whose capture failed for a liftable reason (CDN block, loading
    # hiccup) - not a verdict, a recapture queue. Two more tries at the end of
    # the run; only what still fails then is reported as this run's errors.
    transient = []

    def progress(res):
        RUN_STATE["done"] += 1
        RUN_STATE["phase_done"] = RUN_STATE.get("phase_done", 0) + 1
        RUN_STATE["message"] = res["url"]
        row = url_by_addr.get(res["url"]) or url_by_key.get(_key(res.get("url")))
        if row is not None and _transient_failure(res, row):
            transient.append(row)
        if row is None:
            # Never silent again. A page we cannot file is a page we did not
            # save, and it must read as an error rather than as progress.
            counters["errors"] += 1
            RUN_STATE["errors"] = counters["errors"]
            print("UNMATCHED RESULT - not saved:", repr(res.get("url")),
                  flush=True)
        if row is not None:
            before = dict(counters)
            n_susp = len(suspects)
            try:
                _handle_result(con, res, row, ts_label, counters, suspects,
                               retry_queue=transient,
                               baseline=baseline)
                # Record this page's outcome for resume: held for the double-
                # check (with its baseline + capture, so the verify can be
                # rebuilt), or settled and done. Committed with the page.
                if len(suspects) > n_susp:
                    sp = suspects[-1]
                    _mark_item(con, run_id, row["id"], "suspect",
                               sp["prev"]["id"], sp["new1_id"])
                else:
                    _mark_item(con, run_id, row["id"], "done")
                con.commit()
            except Exception as exc:            # one bad row must not sink the run
                try:
                    con.rollback()
                except Exception:
                    pass
                # The rollback undoes the DATABASE work; these counters are a
                # Python dict and it cannot touch them. So a row that captured
                # fine and then threw on the way to disk was still being
                # reported "ok" - a run said "61 ok, 0 saved" while not one of
                # the 61 had moved. Put the counters back where the row found
                # them and count it for what it was.
                counters.clear()
                counters.update(before)
                counters["errors"] += 1
                _mark_item(con, run_id, row["id"], "done")   # errored; not retried
                try:
                    con.commit()
                except Exception:
                    pass
                print("row error:", res.get("url"), repr(exc), flush=True)
                traceback.print_exc()
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
    # A suspect is NOT saved yet: its snapshot sits in limbo, last_checked is
    # untouched and no change row exists. Skipping this pass on stop therefore
    # threw the whole queue away - a run could capture 2,438 pages, be stopped,
    # and leave 152 saved with 0 changes. Stopping now still resolves the
    # queue; it just does not get to re-capture first.
    if suspects:
        _verify_suspects(con, suspects, ts_label, concurrency, counters, run_id,
                         recapture=not stopping())

    # RETRY ROUNDS for the transient failures: run the whole recapture queue
    # once, then once more for whatever still fails, then accept the state.
    # A page that answers on retry flows through the exact same paths as the
    # first pass - unchanged into the table, changed into its own double-check,
    # blank/404 to their final classifications - and its earlier error tally is
    # taken back. A page that fails all three tries stays an honest error and
    # is retried by the next run as before.
    for rnd in (1, 2):
        if not transient or stopping():
            break
        retry_label = f"{ts_label}-retry{rnd}"
        retry_urls, transient = transient, []
        RUN_STATE["total"] += len(retry_urls)
        RUN_STATE.update(phase="retry", round=rnd,
                         phase_total=len(retry_urls), phase_done=0)
        RUN_STATE["message"] = (f"retrying {len(retry_urls)} blocked page(s), "
                                f"round {rnd} of 2…")
        print(f"[retry] round {rnd}: {len(retry_urls)} transient failure(s)",
              flush=True)
        # A blocked page needs TIME more than it needs another attempt, and
        # every page in this round was refused within the same few minutes -
        # so returning together just rebuilds the wave that got blocked. Wait
        # before each round, and wait a different amount every time, so the
        # queue comes back spread out instead of in formation.
        lo, hi = RETRY_PAUSE_S[min(rnd, len(RETRY_PAUSE_S)) - 1]
        nap = random.randint(lo, hi)
        RUN_STATE["message"] = (f"waiting {nap}s for the block to lift, then"
                                f" retrying {len(retry_urls)} page(s)…")
        print(f"[retry] round {rnd}: pausing {nap}s before trying again",
              flush=True)
        _nap(nap, stopping)
        retry_by_addr = {row["url"]: row for row in retry_urls}
        retry_by_key = {_key(row["url"]): row for row in retry_urls}
        retry_suspects = []

        def rprogress(res):
            RUN_STATE["done"] += 1
            RUN_STATE["phase_done"] = RUN_STATE.get("phase_done", 0) + 1
            RUN_STATE["message"] = res["url"]
            row = (retry_by_addr.get(res["url"])
                   or retry_by_key.get(_key(res.get("url"))))
            if row is None:
                return
            if _transient_failure(res, row):
                transient.append(row)      # next round, or the final state
                return
            before = dict(counters)
            n_susp = len(retry_suspects)
            try:
                # the earlier failed attempt was tallied as an error - the page
                # answered now, so take that back before recording the capture
                counters["errors"] = max(0, counters["errors"] - 1)
                _handle_result(con, res, row, retry_label, counters,
                               retry_suspects, baseline=baseline)
                if len(retry_suspects) > n_susp:
                    sp = retry_suspects[-1]
                    _mark_item(con, run_id, row["id"], "suspect",
                               sp["prev"]["id"], sp["new1_id"])
                else:
                    _mark_item(con, run_id, row["id"], "done")
                con.commit()
            except Exception as exc:
                try:
                    con.rollback()
                except Exception:
                    pass
                counters.clear()
                counters.update(before)
                counters["errors"] += 1
                try:
                    con.commit()
                except Exception:
                    pass
                print("retry row error:", res.get("url"), repr(exc), flush=True)
            RUN_STATE["changed"] = counters["changed"]
            RUN_STATE["errors"] = counters["errors"]
            RUN_STATE["saved"] = counters["saved"]

        rjobs = [(row["url"], str(_snapshot_dir(row["id"], retry_label)))
                 for row in retry_urls]
        asyncio.run(capture.capture_all(rjobs, concurrency=concurrency,
                                        progress=rprogress, pause_check=paused,
                                        stop_check=stopping))
        if retry_suspects:
            _verify_suspects(con, retry_suspects, retry_label, concurrency,
                             counters, run_id, recapture=not stopping(),
                             phase="retry")
    if transient:
        # THE FINAL VERDICT: these pages failed every try this run (or the run
        # was stopped before their retries). Only now does 'error' land on the
        # page row - a page that recovered in a retry round never wore it.
        now_fin = db.now_iso()
        for row in transient:
            try:
                _fail_page(con, row, now_fin)
            except Exception:
                pass
        con.commit()
        print(f"[retry] {len(transient)} page(s) still failing after the"
              " extra rounds - streak advanced, next run tries again",
              flush=True)

    ok, changed = counters["ok"], counters["changed"]
    # The record gets FINAL page states, not attempt tallies. The event
    # counter rises every time anything fails - including first tries the
    # retry rounds later heal, and captures a mid-run restart killed - so a
    # run that ended with zero broken pages once recorded "345 errors" while
    # 3186+7+345 exceeded its own total. What a person means by the number
    # is "how many pages ended broken", so that is what is stored.
    errors = con.execute(
        "SELECT COUNT(*) FROM run_items ri JOIN urls u ON u.id=ri.url_id"
        " WHERE ri.run_id=? AND u.last_status='error'", (run_id,)).fetchone()[0]
    stopped = stopping()
    con.execute("UPDATE runs SET finished=?, ok=?, changed=?, errors=? WHERE id=?",
                (db.now_iso(), ok, changed, errors, run_id))
    con.commit()
    con.close()

    # After a FULL run, fingerprint every referenced asset file in the
    # background (HEAD only, no downloads): a datasheet or image republished
    # at the SAME URL is invisible to the page compare - the validators are
    # the only signal it changed. Partial runs skip it (a 3-page manual check
    # should not fire 14k HEAD requests), as does an early stop.
    if not url_ids and not stopped:
        try:
            import asset_watch
            asset_watch.run_in_background()
        except Exception as exc:      # never let the watcher break a run
            print(f"[assets] skipped: {exc}", flush=True)

    RUN_STATE.update(changed=changed, errors=errors, last_finished=db.now_iso(),
                     message=f"stopped early - {ok + errors} of {len(urls)} captured"
                             if stopped else "finished")
    return {"total": len(urls), "ok": ok, "changed": changed, "errors": errors,
            "stopped": stopped}


# A section that fails to render prints an apology pane instead of its items
# ("Content could not be loaded" / "Unfortunately, we were unable to load the
# content for this section") - and run 124 recorded five such renders as the
# page's state, each reading as a huge "items removed" update, with the
# mirror-image "items returned" update on the next run. The apology's mere
# PRESENCE means nothing: the pricing box on most part pages shows it
# permanently (~75% of snapshots carry it). The failure signature is the
# sentence being ADDED relative to the baseline - a section that rendered last
# time answered with an apology this time. That capture is a broken render,
# not the page's state, and must never become a baseline or a suspect.
_LOAD_FAIL_RE = re.compile(
    r"content could not be loaded|unable to load the content", re.I)


# How long to wait before each retry round, as (low, high) seconds - the
# actual pause is drawn from the range so no two rounds (and no two runs) come
# back on the same beat. Round 2 waits longer: whatever refused us is still
# refusing, and the last try is the one that has to count.
RETRY_PAUSE_S = ((20, 45), (60, 120))


def _nap(seconds, stop_check=None):
    """Sleep, but stay reachable: 'End check' must not have to wait out a
    two-minute pause before the run notices it was asked to stop."""
    for _ in range(int(seconds)):
        if stop_check is not None and stop_check():
            return
        time.sleep(1)


def _transient_failure(res, row=None):
    """A failure worth retrying inside the SAME run: the capture errored for a
    reason that usually lifts within minutes (CDN block, timeout, loading
    hiccup). A successful capture is not retried (404s included - 'gone' is a
    state, recorded as not live).

    A BLANK render is judged against what the page was doing before. A page
    that rendered fine yesterday and answers empty today is far likelier to be
    a bad render than a site that deleted its own body, so it gets the same
    retries as a timeout, and only lands as an error if it answers empty every
    time. A page that was ALREADY blank is a steady state: retrying it every
    run buys nothing, so that one is final and stays 'serves blank page'."""
    if (res or {}).get("status") == "ok":
        return False
    if capture.BLANK_RENDER in ((res or {}).get("error") or ""):
        return not _was_blank(row)
    return True


def _was_blank(row):
    """Was this page already serving a blank body before this capture?"""
    if row is None:
        return False
    try:
        return (row["last_status"] or "") == "blank"
    except Exception:
        return False


def _load_failure_added(prev_dir, new_dir, settings=None):
    """True when the new capture ADDED a section load-failure apology that the
    baseline does not have (count-aware: pricing-box apology on both sides
    stays invisible; a second apology appearing still trips)."""
    ig = ((settings or {}).get("ignore_patterns") or "").splitlines()
    old = Counter(cmp._load_text_lines(prev_dir, ig))
    new = Counter(cmp._load_text_lines(new_dir, ig))
    return any(_LOAD_FAIL_RE.search(l) for l in (new - old))


def _handle_result(con, res, row, ts_label, counters, suspects,
                   baseline=False, retry_queue=None):
    """Record ONE finished capture and decide its fate: error/blank, redirect,
    baseline, unchanged, or queue as a suspected change. Mutates counters and
    suspects; the caller owns the commit (one commit per page).

    baseline=True: the capture IS the page's new baseline - no comparison, no
    suspects, no guards that need a comparison. Everything else (error/blank
    handling, dead pages, redirects, indexing, self-heal) is identical."""
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
        counters["saved"] += 1
        # "Infineon served an empty page" and "our capture broke" are different
        # facts and only one is the site owner's to fix. A blank render answers
        # with a 2xx and an empty <body>, so the HTTP code is real.
        blank = capture.BLANK_RENDER in (res.get("error") or "")
        # A page that was blank before and is blank again has not failed - it
        # is a page in a known state, like a dead one, and counting it as an
        # error every run makes a steady site look broken. A page that went
        # blank for the FIRST time is a suspected bad render and takes the
        # normal error path (retries first, error only if it sticks).
        still_blank = blank and _was_blank(row)
        if not still_blank:
            counters["errors"] += 1
        # A TRANSIENT failure mid-run is not a verdict - the retry rounds at
        # the end of the run get the last word. Until then the page KEEPS its
        # previous status ("unchanged" stays "unchanged"); only a failure that
        # survives every retry is written as an error (the run's tail does
        # that).
        if still_blank or not _transient_failure(res, row):
            con.execute(
                "UPDATE urls SET last_checked=?, last_status=?, last_http=?,"
                " error_streak=0 WHERE id=?",
                (now, "blank" if blank else "error",
                 res.get("http_status"), url_id))
        # keep the previous good snapshot; drop the failed one's files
        shutil.rmtree(snap_dir, ignore_errors=True)
        con.execute("UPDATE snapshots SET dir='' WHERE id=?", (new_snap_id,))
        return

    # A capture whose pagination walks died mid-way answered "ok" while
    # carrying a FRACTION of the library (or none of the products table).
    # Storing it makes the next healthy run read the missing items as
    # "removed" - #7481's surviving sibling. Failed render: baseline kept,
    # this capture dropped, the page tries again (same run when the retry
    # rounds are still ahead, next run otherwise - where the streak, not a
    # first stumble, decides what the team is shown).
    if res.get("walk_errors"):
        names = ", ".join(sorted({w[0] for w in res["walk_errors"]}))
        counters["errors"] += 1
        counters["saved"] += 1
        if retry_queue is not None:
            retry_queue.append(row)
            con.execute("UPDATE urls SET last_checked=? WHERE id=?",
                        (now, url_id))
        else:
            _fail_page(con, row, now)
        shutil.rmtree(snap_dir, ignore_errors=True)
        con.execute("UPDATE snapshots SET dir='' WHERE id=?", (new_snap_id,))
        print(f"[guard] {row['url']}: walk(s) died mid-pagination ({names});"
              " partial library dropped, baseline kept", flush=True)
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
    _write_page_meta(con, url_id, row["url"], res, dead)
    # CANONICAL DEDUP at capture: the long address only becomes known NOW
    # (from the page's own breadcrumb), so a short link and its long twin can
    # both have been added. The moment the canonical is known, drop a twin
    # that was never captured - it would only capture this same page again.
    # A captured twin is left alone: it holds history, and if the site 301s
    # one name to the other the redirect logic collapses them safely.
    if not dead:
        cu = con.execute("SELECT canonical_url FROM urls WHERE id=?",
                         (url_id,)).fetchone()
        cu = (cu["canonical_url"] or "").rstrip("/") if cu else ""
        if cu:
            twin = con.execute(
                "SELECT id FROM urls WHERE id<>? AND last_checked IS NULL"
                " AND rtrim(url,'/')=?", (url_id, cu)).fetchone()
            if twin:
                con.execute("DELETE FROM urls WHERE id=?", (twin["id"],))
                con.execute("DELETE FROM page_fts WHERE rowid=?", (twin["id"],))
                print(f"dedup: dropped never-captured twin #{twin['id']}"
                      f" ({cu}) - same page as {row['url']}", flush=True)
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

    # BASELINE MODE: this capture IS the truth from here on. No comparison -
    # against old-method captures a diff would measure the method, not the
    # site - no suspects, no compare-dependent guards. The previous snapshot
    # is pruned like any settled page's.
    if baseline:
        con.execute("UPDATE urls SET last_checked=?, last_status=? WHERE id=?",
                    (now, "notlive" if dead else "unchanged", url_id))
        index_items(con, url_id, snap_dir, dead)
        if prev is not None:
            _prune_snapshot(con, prev)
        counters["saved"] += 1
        return

    if prev is None:
        con.execute("UPDATE urls SET last_checked=?, last_status=? WHERE id=?",
                    (now, "notlive" if dead else "unchanged", url_id))
        index_items(con, url_id, snap_dir, dead)
        counters["saved"] += 1
        return

    # STILL not live: a page that is 404/410 AND was already not-live never
    # becomes "updated". Infineon's error page varies run to run (rotating
    # promos, timestamps), but the tracked page did not change - it is simply
    # still gone. Only a TRANSITION is worth flagging: back to live (handled
    # by the normal compare below, dead=False), or a different failure (a
    # blank/500 lands on the status!=ok path above). Dead->dead stays notlive.
    if dead and (row["last_status"] or "") == "notlive":
        _set_checked(con, url_id, now, "notlive")
        index_items(con, url_id, snap_dir, dead)   # stays purged
        _prune_snapshot(con, prev)                 # the old 404 snap is moot
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
        new_n = _snapshot_item_count(snap_dir)
        # a PARTIAL collapse is the same break in a smaller size: markup that
        # renamed for one component drops its items while the page text stays
        # identical. Real removals change the text (the titles leave the
        # page); text-unchanged with less than half the items left means the
        # extractor stopped seeing them. -1 = unreadable, never triggers.
        if prev_n >= 5 and 0 <= new_n < prev_n * 0.5:
            con.execute("UPDATE urls SET last_checked=? WHERE id=?",
                        (now, url_id))  # status untouched: the run tail decides
            # ...and try again before the run ends. A library that collapsed
            # to under half is a broken RENDER, which is exactly what a retry
            # is for - this used to be written as a final error on the first
            # attempt while a plain timeout got two more goes.
            if retry_queue is not None:
                retry_queue.append(row)
            # the row still describes the capture we are about to discard
            _restore_page_meta(con, url_id, prev)
            shutil.rmtree(snap_dir, ignore_errors=True)
            con.execute("UPDATE snapshots SET dir='' WHERE id=?", (new_snap_id,))
            counters["errors"] += 1
            counters["saved"] += 1
            print(f"[guard] {row['url']}: {prev_n} items -> {new_n} with text"
                  " unchanged; extractor likely broke, baseline kept",
                  flush=True)
            return

    # A capture where a section's items got replaced by the "could not be
    # loaded" apology is a FAILED render, not a suspected update: keep the
    # good baseline, report an error so it is loud, retry next run.
    if result["any_changed"] and not dead \
            and _load_failure_added(prev["dir"], snap_dir, db.get_settings()):
        con.execute("UPDATE urls SET last_checked=? WHERE id=?",
                    (now, url_id))   # status untouched: the run tail decides
        # a section that said "could not be loaded" is a transient render
        # failure by definition - give it the same two tries as a timeout
        if retry_queue is not None:
            retry_queue.append(row)
        _restore_page_meta(con, url_id, prev)
        shutil.rmtree(snap_dir, ignore_errors=True)
        con.execute("UPDATE snapshots SET dir='' WHERE id=?", (new_snap_id,))
        counters["errors"] += 1
        counters["saved"] += 1
        print(f"[guard] {row['url']}: a section answered 'could not be"
              " loaded' (was fine in the baseline); broken render dropped,"
              " baseline kept", flush=True)
        return

    # Sections that were full and came back EMPTY: the widgets never mounted.
    # Same treatment as a CDN block - the capture is thrown away, the baseline
    # stands, and the page goes back in the queue for another try this run.
    if result["any_changed"] and not dead \
            and _smells_like_lost_widgets(prev["dir"], snap_dir, row):
        con.execute("UPDATE urls SET last_checked=? WHERE id=?",
                    (now, url_id))   # status untouched: the run tail decides
        if retry_queue is not None:
            retry_queue.append(row)
        _restore_page_meta(con, url_id, prev)
        shutil.rmtree(snap_dir, ignore_errors=True)
        con.execute("UPDATE snapshots SET dir='' WHERE id=?", (new_snap_id,))
        counters["errors"] += 1
        counters["saved"] += 1
        print(f"[guard] {row['url']}: sections came back empty with nothing"
              " added anywhere; widgets never mounted, baseline kept",
              flush=True)
        return

    if result["any_changed"]:
        # Don't flag yet: late-loading widgets can make a page LOOK different
        # once. Re-capture at the end and flag only if the change persists
        # (real edits stay; loading flakes don't).
        # The snapshot goes into limbo ('pending') until the verify pass rules
        # on it: were it left 'ok' and the server died before verification,
        # the next run would silently adopt the UNVERIFIED capture as its
        # baseline and the real change would never be flagged.
        con.execute("UPDATE snapshots SET status='pending' WHERE id=?",
                    (new_snap_id,))
        suspects.append({"row": row, "new1_id": new_snap_id,
                         "new1_dir": snap_dir, "prev": prev, "res": res})
        # each suspect owes ONE more capture at the end - publish the debt so
        # the ETA can tell the truth instead of ignoring the verify tail
        RUN_STATE["suspects"] = len(suspects)
        # 'held' is the page-bucket view of the same fact: how many pages are
        # currently in limbo awaiting their double-check (down-ticked as each
        # one is resolved in the verify pass)
        RUN_STATE["held"] = RUN_STATE.get("held", 0) + 1
    else:
        _set_checked(con, url_id, now, "notlive" if dead else "unchanged")
        index_items(con, url_id, snap_dir, dead)
        _prune_snapshot(con, prev)
        counters["saved"] += 1


def _smells_like_half_render(result, row):
    """A diff that ONLY removes - twenty-plus lines gone, nothing arriving -
    is the shape of a render that silently skipped sections, not the shape
    of an edit (#7118: price box, documents and design resources absent from
    two stressed captures while the live page had all of it; no apology text
    for the apology guard to catch). Once, it is an error and a retry next
    run. The same loss showing again on a page ALREADY wearing that error is
    a real gutting and flags normally - one run late, never never."""
    try:
        pure_loss = (result.get("text_changed")
                     and (result.get("lines_added") or 0) == 0
                     and (result.get("lines_removed") or 0) >= 20)
        return bool(pure_loss) and not _wearing_error(row)
    except Exception:
        return False


# How many populated sections have to come back EMPTY before a capture is
# read as a failed render rather than an edit. Two is enough: a page does not
# lose its breadcrumb AND its documents in one edit, and #7481 lost six.
_COLLAPSE_MIN_SECTIONS = 2


def _section_counts(snap_dir):
    """Items per captured section, or None if the file is unreadable."""
    try:
        j = json.loads((Path(snap_dir) / "sections.json")
                       .read_text(encoding="utf-8"))
    except Exception:
        return None
    out = {}
    for k, v in (j or {}).items():
        out[k] = len(v) if isinstance(v, list) else (1 if v else 0)
    return out


def _collapse_fingerprint(prev_dir, new_dir):
    """WHICH sections came back empty, as a comparable value.

    The counterpart of _diff_fingerprint for a collapse: a page that really
    dropped its documents empties the same section every time it is
    photographed, while widgets that fail to mount fail a different set each
    time. None when either capture's sections cannot be read - not evidence of
    anything, and never equal to a real answer."""
    a, b = _section_counts(prev_dir), _section_counts(new_dir)
    if not a or not b:
        return None
    return frozenset(k for k, n in a.items() if n > 0 and b.get(k, 0) == 0)


def _smells_like_lost_widgets(prev_dir, new_dir, row):
    """Several populated sections empty at once with nothing gained anywhere:
    the page's late-mounting widgets never arrived, which is a broken capture
    and not an edit.

    #7481 and #7459 are the case this exists for. Both came back with the
    breadcrumb, documents, design resources, partners and components at ZERO
    and were recorded as 'sections removed'; a capture of each live page taken
    straight afterwards had every item the baseline had. Neither page had
    changed at all.

    The other guards were blind to it. The text signal was silently
    re-baselined (those baselines predate the current text method), so the
    122-line loss the half-render guard looks for was never presented to it,
    and the library-collapse guard counts rows in the items table, which these
    pages do not use. This one reads sections.json, which its own revision
    governs - so it means the same thing on both sides of a text/image method
    change, exactly when the others go quiet.

    Once is a failed render: keep the baseline, report an error, capture again.
    The same collapse on a page ALREADY wearing that error is real and flags
    normally - one run late, never never (the rule _smells_like_half_render
    follows)."""
    a, b = _section_counts(prev_dir), _section_counts(new_dir)
    if not a or not b:
        return False
    emptied = sum(1 for k, n in a.items() if n > 0 and b.get(k, 0) == 0)
    if emptied < _COLLAPSE_MIN_SECTIONS:
        return False
    # anything GROWING says the page really was re-rendered with new content,
    # which a stalled widget cannot do - that is an edit, not a broken capture
    grew = any(b.get(k, 0) > n for k, n in a.items()) \
        or any(n > 0 for k, n in b.items() if k not in a)
    if grew:
        return False
    try:
        return not _wearing_error(row)
    except Exception:
        return True


def _diff_fingerprint(prev_dir, new_dir, settings):
    """WHAT changed, as a comparable value: the multiset of added and removed
    text lines plus the image url sets. Two captures of the same real edit
    produce the same fingerprint; a hydration flap or a half-render produces
    a different one, which is the whole point."""
    ig = ((settings or {}).get("ignore_patterns") or "").splitlines()
    _c, _a, _r, hunks = cmp.text_diff(prev_dir, new_dir, ig)
    added, removed = [], []
    for h in hunks:
        for tag, line in h["lines"]:
            if tag == "+":
                added.append(line)
            elif tag == "-":
                removed.append(line)
    igi = ((settings or {}).get("ignore_image_patterns") or "").splitlines()
    _c2, img_a, img_r = cmp.images_diff(prev_dir, new_dir, igi)
    return (Counter(added), Counter(removed),
            frozenset(img_a), frozenset(img_r))


def _fingerprints_agree(f1, f2):
    return f1 == f2


def _loss_survived_three_looks(f1, f2, f3):
    """Whether a LOSS has earned publication on the spot - lines gone from the
    text, or whole sections come back empty. Both kinds pass through here.

    The bar depends on what the first two captures said, because the third
    capture is doing a different job in each case.

    WHEN THE FIRST TWO AGREE the third must agree as well. Two of three would
    already be satisfied by those two on their own, which would make the extra
    capture decorative - and "two captures agreeing" is exactly the pair that
    published #7118 and #7481, both of them broken renders telling the same
    lie. Unanimity is the only arrangement where looking again can change the
    answer.

    WHEN THEY DISAGREE the third breaks the tie the ordinary way: any two
    matching exactly carries it. Here the extra capture really is casting a
    vote, so a majority means something.

    f3 is None when the third capture never happened or failed, which is not
    agreement under either rule."""
    if f3 is None:
        return False
    if _fingerprints_agree(f1, f2):
        return _fingerprints_agree(f2, f3)
    return _fingerprints_agree(f3, f1) or _fingerprints_agree(f3, f2)


def _third_look(row, url_id, ts_label, why):
    """Capture the page once more, for a tie to break or a loss to confirm.

    Returns (res3, dir3). Comparing is the caller's job: a tie is settled on
    what the diff says, a collapse on which sections emptied, and one capture
    path serves both."""
    dir3 = str(_snapshot_dir(url_id, ts_label + "-verify3"))
    print(f"[verify] {row['url']}: {why} - taking a third look", flush=True)
    try:
        r3 = asyncio.run(capture.capture_all([(row["url"], dir3)],
                                             concurrency=1))
        res3 = r3[0] if r3 else None
    except Exception:
        res3 = None
    return res3, dir3


def _took(res3):
    """Did the extra capture actually come back with a page?"""
    return bool(res3) and res3.get("status") == "ok"


def _verify_suspects(con, suspects, ts_label, concurrency, counters, run_id,
                     recapture=True, phase="verify"):
    """Re-capture every suspected change once and flag only those that persist.
    Each verified page is committed on its own. phase names the pipeline stage
    for the dashboard ('verify' for the main double-check; the retry rounds
    pass 'retry' so the stepper stays on their step)."""
    RUN_STATE["message"] = (
        f"reconfirming {len(suspects)} suspected update(s)…" if recapture
        else f"saving {len(suspects)} suspected update(s) before stopping…")
    if recapture:
        RUN_STATE["total"] += len(suspects)
    RUN_STATE["suspects"] = 0     # the debt is folded into total now
    RUN_STATE.update(phase=phase, phase_total=len(suspects), phase_done=0)
    ts2 = ts_label + "-verify"
    vjobs = [(s["row"]["url"], str(_snapshot_dir(s["row"]["id"], ts2)))
             for s in suspects]

    def vprogress(res):
        RUN_STATE["done"] += 1
        RUN_STATE["phase_done"] = RUN_STATE.get("phase_done", 0) + 1
        RUN_STATE["message"] = res["url"]

    def paused():
        return bool(RUN_STATE.get("paused"))

    if recapture:
        vresults = asyncio.run(capture.capture_all(
            vjobs, concurrency=concurrency, progress=vprogress,
            pause_check=paused))
        vby = {r["url"]: r for r in vresults}
    else:
        vby = {}      # stopping: every suspect falls to the keep-detection path
    settings_now = db.get_settings()

    for s in suspects:
        row, prev = s["row"], s["prev"]
        url_id = row["id"]
        # A suspect rebuilt from an interrupted run has no stored capture result
        # (res is None); if its re-capture is also missing, treat it as a failed
        # verification and keep the already-detected snapshot rather than crash.
        vres = vby.get(row["url"]) or s.get("res") \
            or {"status": "error", "http_status": None}
        vdir = str(_snapshot_dir(url_id, ts2))
        now = db.now_iso()

        if not recapture or vres["status"] != "ok" \
                or vres.get("walk_errors") \
                or _load_failure_added(prev["dir"], vdir, settings_now):
            # Verification capture failed - or rendered a section as the
            # "could not be loaded" apology, which is the same failure wearing
            # a 200. Keep the original detection rather than risk missing a
            # real change (the first capture cannot carry the apology: the
            # first-pass guard never lets one become a suspect). The kept
            # snapshot leaves limbo: it is now this page's baseline-to-be.
            shutil.rmtree(vdir, ignore_errors=True)
            con.execute("UPDATE snapshots SET status='ok' WHERE id=?",
                        (s["new1_id"],))
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
            # Set when three captures have already agreed on a loss. That is a
            # higher bar than either check below applies, so they stand down
            # rather than re-litigate it (or spend a fourth capture on it).
            unanimous_loss = False
            # The second capture lost its widgets too. Two broken renders in a
            # row is a bad hour at their end, not an edit - #7481 got this far
            # and was published as 'sections removed' while the live page was
            # whole. But a page CAN really drop a section, so the same bargain
            # as a pure loss: ask a third capture instead of costing a run.
            # Three captures emptying the SAME sections is a page that was
            # stripped; widgets that fail to mount fail a different set each
            # time.
            if confirmed and _smells_like_lost_widgets(prev["dir"], vdir, row):
                c1 = _collapse_fingerprint(prev["dir"], s["new1_dir"])
                c2 = _collapse_fingerprint(prev["dir"], vdir)
                res3, dir3 = _third_look(
                    row, url_id, ts_label,
                    "the same sections came back empty twice"
                    if _fingerprints_agree(c1, c2)
                    else "the two captures empty different sections")
                c3 = (_collapse_fingerprint(prev["dir"], dir3)
                      if _took(res3) else None)
                if _loss_survived_three_looks(c1, c2, c3):
                    con.execute("DELETE FROM snapshots WHERE id=?",
                                (vsnap_id,))
                    shutil.rmtree(vdir, ignore_errors=True)
                    cur = con.execute(
                        "INSERT INTO snapshots(url_id, ts, dir, status,"
                        " error, page_title, text_lines, image_count,"
                        " http_status) VALUES(?,?,?,?,?,?,?,?,?)",
                        (url_id, now, dir3, "ok", "", res3.get("title", ""),
                         res3.get("text_lines", 0),
                         res3.get("image_count", 0),
                         res3.get("http_status")))
                    vsnap_id, vdir, vres = cur.lastrowid, dir3, res3
                    unanimous_loss = True
                    print(f"[verify] {row['url']}: three captures empty the"
                          " same sections - the page really dropped them,"
                          " flagging now", flush=True)
                else:
                    shutil.rmtree(dir3, ignore_errors=True)
                    _fail_page(con, row, now)
                    _restore_page_meta(con, url_id, prev)
                    shutil.rmtree(vdir, ignore_errors=True)
                    con.execute("DELETE FROM snapshots WHERE id=?",
                                (vsnap_id,))
                    counters["errors"] += 1
                    counters["saved"] += 1
                    _mark_item(con, run_id, url_id, "done")
                    con.commit()
                    print(f"[verify] {row['url']}: empty sections did not"
                          " survive a third capture - kept the baseline,"
                          " retrying next run", flush=True)
                    continue
            if confirmed and not unanimous_loss \
                    and _smells_like_half_render(recheck, row):
                # A loss this shape is either a render that skipped whole
                # sections or a page that really was stripped, and the two
                # cases deserve different answers. So ASK A THIRD CAPTURE:
                # three of them printing the same loss line for line is as
                # certain as this gets - a stalled widget does not reproduce
                # itself exactly three times - and that publishes now instead
                # of a run later (Anas: "if all three show the identical loss,
                # publish it right then"). Unanimous or nothing: two captures
                # that already disagree cannot become unanimous, so they skip
                # the extra capture and keep the baseline as before.
                f1 = _diff_fingerprint(prev["dir"], s["new1_dir"], settings_now)
                f2 = _diff_fingerprint(prev["dir"], vdir, settings_now)
                res3, dir3 = _third_look(
                    row, url_id, ts_label,
                    "both captures lost the same lines and nothing arrived"
                    if _fingerprints_agree(f1, f2)
                    else "the two captures lost different lines")
                f3 = (_diff_fingerprint(prev["dir"], dir3, settings_now)
                      if _took(res3) else None)
                if _loss_survived_three_looks(f1, f2, f3):
                    # unanimous: the page really did lose it. The third
                    # capture is the newest and carries the agreed state, so
                    # it becomes the capture of record and the rest of this
                    # loop publishes it like any confirmed update.
                    con.execute("DELETE FROM snapshots WHERE id=?",
                                (vsnap_id,))
                    shutil.rmtree(vdir, ignore_errors=True)
                    cur = con.execute(
                        "INSERT INTO snapshots(url_id, ts, dir, status,"
                        " error, page_title, text_lines, image_count,"
                        " http_status) VALUES(?,?,?,?,?,?,?,?,?)",
                        (url_id, now, dir3, "ok", "", res3.get("title", ""),
                         res3.get("text_lines", 0),
                         res3.get("image_count", 0),
                         res3.get("http_status")))
                    vsnap_id, vdir, vres = cur.lastrowid, dir3, res3
                    print(f"[verify] {row['url']}: three captures agree on the"
                          " loss - the page really was stripped, flagging now",
                          flush=True)
                else:
                    shutil.rmtree(dir3, ignore_errors=True)
                    _fail_page(con, row, now)
                    _restore_page_meta(con, url_id, prev)
                    shutil.rmtree(vdir, ignore_errors=True)
                    con.execute("DELETE FROM snapshots WHERE id=?",
                                (vsnap_id,))
                    counters["errors"] += 1
                    counters["saved"] += 1
                    _mark_item(con, run_id, url_id, "done")
                    con.commit()
                    print(f"[verify] {row['url']}: pure loss did not survive a"
                          " third capture - kept the baseline, retrying next"
                          " run", flush=True)
                    continue
            # THE SAME DIFFERENCE, NOT JUST A DIFFERENCE. The old test was
            # "does the verify capture differ from the baseline at all?" -
            # which a flap passes as easily as an edit, as long as it flaps
            # twice. Now the two captures must agree on WHAT changed; when
            # they disagree, a third capture breaks the tie, best two of
            # three. A real edit prints the same diff every time it is
            # photographed; noise does not.
            if confirmed and not unanimous_loss \
                    and (recheck.get("text_changed")
                         or recheck.get("images_changed")):
                f1 = _diff_fingerprint(prev["dir"], s["new1_dir"],
                                       settings_now)
                f2 = _diff_fingerprint(prev["dir"], vdir, settings_now)
                if not _fingerprints_agree(f1, f2):
                    res3, dir3 = _third_look(
                        row, url_id, ts_label,
                        "the two captures disagree on WHAT changed")
                    f3 = (_diff_fingerprint(prev["dir"], dir3, settings_now)
                          if _took(res3) else None)
                    if f3 is not None and (_fingerprints_agree(f3, f2)
                                           or _fingerprints_agree(f3, f1)):
                        # two of three agree: the third capture carries the
                        # agreed state and is the newest - it becomes the
                        # capture of record
                        con.execute("DELETE FROM snapshots WHERE id=?",
                                    (vsnap_id,))
                        shutil.rmtree(vdir, ignore_errors=True)
                        cur = con.execute(
                            "INSERT INTO snapshots(url_id, ts, dir, status,"
                            " error, page_title, text_lines, image_count,"
                            " http_status) VALUES(?,?,?,?,?,?,?,?,?)",
                            (url_id, now, dir3, "ok", "",
                             res3.get("title", ""),
                             res3.get("text_lines", 0),
                             res3.get("image_count", 0),
                             res3.get("http_status")))
                        vsnap_id, vdir, vres = cur.lastrowid, dir3, res3
                    else:
                        # three captures, no two alike - nothing here is
                        # steady enough to put in front of a person
                        shutil.rmtree(dir3, ignore_errors=True)
                        _fail_page(con, row, now)
                        _restore_page_meta(con, url_id, prev)
                        shutil.rmtree(vdir, ignore_errors=True)
                        con.execute("DELETE FROM snapshots WHERE id=?",
                                    (vsnap_id,))
                        counters["errors"] += 1
                        counters["saved"] += 1
                        _mark_item(con, run_id, url_id, "done")
                        con.commit()
                        print(f"[verify] {row['url']}: three captures, no two"
                              " alike - kept the baseline, retrying next run",
                              flush=True)
                        continue
            # the first suspect capture is superseded either way
            shutil.rmtree(s["new1_dir"], ignore_errors=True)
            con.execute("DELETE FROM snapshots WHERE id=?", (s["new1_id"],))
            confirm_id, confirm_dir = vsnap_id, vdir
            # the verify capture is the one the library will describe - point
            # every derived column at IT, not at the superseded first capture
            _write_page_meta(con, url_id, row["url"], vres,
                             (vres.get("http_status") or 0) in (404, 410))

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
            # name WHAT changed, component by component, so the review reads
            # "page text · News & highlights (+2/-1) · Hero image added"
            parts = component_changes(prev["dir"], confirm_dir, row, vres,
                                      result)
            summary = " · ".join(parts) if parts else result["summary"]
            con.execute(
                "INSERT INTO changes(url_id, ts, old_snapshot_id,"
                " new_snapshot_id, text_changed, images_changed,"
                " visual_changed, pixel_ratio, height_delta, lines_added,"
                " lines_removed, images_added, images_removed, summary, dir,"
                " components)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (url_id, now, prev["id"], confirm_id,
                 int(result["text_changed"]), int(result["images_changed"]),
                 int(result["visual_changed"]), result["pixel_ratio"],
                 result["height_delta"], result["lines_added"],
                 result["lines_removed"], result["images_added"],
                 result["images_removed"], summary,
                 str(change_dir), json.dumps(parts, ensure_ascii=False)))
            con.execute("UPDATE urls SET last_checked=?, last_status=?,"
                        " error_streak=0 WHERE id=?", (now, "changed", url_id))
            index_items(con, url_id, confirm_dir, dead_now)
        else:
            _set_checked(con, url_id, now,
                         "notlive" if dead_now else "unchanged")
            index_items(con, url_id, confirm_dir, dead_now)
            _prune_snapshot(con, prev)
        counters["saved"] += 1
        RUN_STATE["saved"] = counters["saved"]
        RUN_STATE["changed"] = counters["changed"]
        RUN_STATE["held"] = max(0, RUN_STATE.get("held", 0) - 1)  # out of limbo
        con.execute("UPDATE runs SET changed=? WHERE id=?",
                    (counters["changed"], run_id))
        _mark_item(con, run_id, url_id, "done")   # this suspect is now resolved
        con.commit()


def run_check_background(kind="manual", url_ids=None, baseline=False):
    """Fire-and-forget check in a daemon thread. Returns False if already running."""
    if RUN_STATE["running"]:
        return False
    # start clean: never inherit a pause/stop flag from a prior run
    RUN_STATE["paused"] = False
    RUN_STATE["stopping"] = False
    t = threading.Thread(target=run_check,
                         kwargs={"kind": kind, "url_ids": url_ids,
                                 "baseline": baseline},
                         daemon=True)
    t.start()
    # tiny wait so the UI immediately shows "running"
    time.sleep(0.3)
    return True
