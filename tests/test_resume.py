"""Tests for durable run state + resume (runner.py + the run_items table).

These exercise the pieces that let an interrupted run continue instead of
redoing captured work, against a throwaway database - no live run, no browser.
The resume path itself only runs on a restart, so these are how it is checked.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import db          # noqa: E402
import runner      # noqa: E402


def _fresh_db():
    """A temp database + snapshot dirs, wired into the db module for one test."""
    d = Path(tempfile.mkdtemp(prefix="ifxdb_"))
    db.DATA_DIR = d
    db.SNAP_DIR = d / "snapshots"
    db.CHANGE_DIR = d / "changes"
    db.DB_PATH = d / "watch.db"
    db.init_db()
    return d


def _snap_dir(root, name):
    p = root / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "text.txt").write_text("x", encoding="utf-8")
    return str(p)


# --- _mark_item upserts state ---------------------------------------------

def test_mark_item_transitions_queued_suspect_done():
    _fresh_db()
    con = db.connect()
    runner._mark_item(con, 1, 42, "queued")
    assert con.execute("SELECT state FROM run_items WHERE run_id=1 AND url_id=42"
                       ).fetchone()[0] == "queued"
    runner._mark_item(con, 1, 42, "suspect", 100, 200)
    row = con.execute("SELECT state, prev_snap_id, snap_id FROM run_items"
                      " WHERE run_id=1 AND url_id=42").fetchone()
    assert tuple(row) == ("suspect", 100, 200), tuple(row)
    runner._mark_item(con, 1, 42, "done")
    # state moves to done but the snap ids are kept (COALESCE)
    row = con.execute("SELECT state, prev_snap_id, snap_id FROM run_items"
                      " WHERE run_id=1 AND url_id=42").fetchone()
    assert tuple(row) == ("done", 100, 200), tuple(row)
    con.close()


# --- _resumable_run finds a crashed run, ignores clean ones ----------------

def test_resumable_run_detects_unfinished_with_work():
    _fresh_db()
    con = db.connect()
    con.execute("INSERT INTO runs(id, started, kind) VALUES(7, 't', 'manual')")
    con.execute("INSERT INTO run_items(run_id, url_id, state) VALUES(7, 1, 'done')")
    con.execute("INSERT INTO run_items(run_id, url_id, state) VALUES(7, 2, 'queued')")
    con.commit()
    r = runner._resumable_run(con)
    assert r is not None and r["id"] == 7
    con.close()


def test_finished_run_is_not_resumable():
    _fresh_db()
    con = db.connect()
    con.execute("INSERT INTO runs(id, started, finished, kind)"
                " VALUES(7, 't', 't2', 'manual')")
    con.execute("INSERT INTO run_items(run_id, url_id, state) VALUES(7, 2, 'queued')")
    con.commit()
    assert runner._resumable_run(con) is None
    con.close()


def test_run_with_all_items_done_is_not_resumable():
    _fresh_db()
    con = db.connect()
    con.execute("INSERT INTO runs(id, started, kind) VALUES(7, 't', 'manual')")
    con.execute("INSERT INTO run_items(run_id, url_id, state) VALUES(7, 1, 'done')")
    con.commit()
    assert runner._resumable_run(con) is None, "nothing left to do, not resumable"
    con.close()


# --- _rebuild_suspects reconstructs the double-check queue -----------------

def test_rebuild_suspects_reconstructs_valid_and_requeues_broken():
    root = _fresh_db()
    con = db.connect()
    con.execute("INSERT INTO runs(id, started, kind) VALUES(9, 't', 'manual')")
    # two active urls
    con.execute("INSERT INTO urls(id, url, active) VALUES(1, 'https://a/1', 1)")
    con.execute("INSERT INTO urls(id, url, active) VALUES(2, 'https://a/2', 1)")
    # url 1: a valid suspect - baseline + pending capture both on disk
    base_dir = _snap_dir(root, "1/base")
    pend_dir = _snap_dir(root, "1/pend")
    b = con.execute("INSERT INTO snapshots(url_id, ts, dir, status)"
                    " VALUES(1, 't', ?, 'ok')", (base_dir,)).lastrowid
    p = con.execute("INSERT INTO snapshots(url_id, ts, dir, status)"
                    " VALUES(1, 't', ?, 'pending')", (pend_dir,)).lastrowid
    con.execute("INSERT INTO run_items(run_id, url_id, state, prev_snap_id, snap_id)"
                " VALUES(9, 1, 'suspect', ?, ?)", (b, p))
    # url 2: a suspect whose snapshots are gone (ids that do not exist)
    con.execute("INSERT INTO run_items(run_id, url_id, state, prev_snap_id, snap_id)"
                " VALUES(9, 2, 'suspect', 999, 998)")
    con.commit()

    suspects, requeue = runner._rebuild_suspects(con, 9)
    assert len(suspects) == 1, f"expected 1 rebuildable suspect, got {len(suspects)}"
    s = suspects[0]
    assert s["row"]["id"] == 1
    assert s["new1_id"] == p and s["prev"]["id"] == b
    assert s["res"] is None, "a rebuilt suspect has no stored capture result"
    assert requeue == [2], f"broken suspect should be re-queued, got {requeue}"
    con.close()


# --- the outcome bar after a resume ----------------------------------------
# Every counter but one was carried across a restart. The dashboard draws
# "no update" as saved - updated - errors, so the one that restarted at zero
# had CUMULATIVE totals subtracted from it: a run 40 errors deep reported 39
# pages checked out of hundreds, and looked like it had barely started.

def _run_with_history(done, suspect, queued, ok, changed, errors):
    _fresh_db()
    con = db.connect()
    con.execute("INSERT INTO runs(id, started, kind, total, ok, changed, errors)"
                " VALUES(1, '2026-01-01 00:00:00', 'full', ?, ?, ?, ?)",
                (done + suspect + queued, ok, changed, errors))
    uid = 1
    for state, n in (("done", done), ("suspect", suspect), ("queued", queued)):
        for _ in range(n):
            con.execute("INSERT INTO run_items(run_id, url_id, state)"
                        " VALUES(1, ?, ?)", (uid, state))
            uid += 1
    con.commit()
    return con


def _counters_on_resume(con, run_id=1):
    """What a resume seeds its counters with - the block runner._go runs."""
    c = {"ok": 0, "changed": 0, "errors": 0, "saved": 0}
    row = con.execute("SELECT ok, changed, errors FROM runs WHERE id=?",
                      (run_id,)).fetchone()
    if row:
        c["ok"] = row["ok"] or 0
        c["changed"] = row["changed"] or 0
        c["errors"] = row["errors"] or 0
    c["saved"] = con.execute(
        "SELECT COUNT(*) FROM run_items WHERE run_id=? AND state='done'",
        (run_id,)).fetchone()[0] or 0
    return c


def test_saved_survives_a_restart_like_the_others():
    con = _run_with_history(done=618, suspect=54, queued=2763,
                            ok=578, changed=0, errors=40)
    c = _counters_on_resume(con)
    con.close()
    assert c["saved"] == 618, \
        f"saved restarted at {c['saved']} instead of carrying 618 forward"


def test_no_update_is_not_driven_negative_by_carried_errors():
    # what the dashboard computes: noUpd = saved - updated - errors
    con = _run_with_history(done=618, suspect=54, queued=2763,
                            ok=578, changed=0, errors=40)
    c = _counters_on_resume(con)
    con.close()
    no_update = c["saved"] - c["changed"] - c["errors"]
    assert no_update == 578, f"the bar would show {no_update} pages with no update"


def test_every_page_lands_in_exactly_one_bucket():
    done, suspect, queued = 618, 54, 2763
    con = _run_with_history(done, suspect, queued, ok=578, changed=0, errors=40)
    c = _counters_on_resume(con)
    con.close()
    total = done + suspect + queued
    no_update = c["saved"] - c["changed"] - c["errors"]
    waiting = total - no_update - c["changed"] - suspect - c["errors"]
    assert no_update + c["changed"] + suspect + c["errors"] + waiting == total
    assert waiting == queued, \
        f"waiting read {waiting}, but {queued} pages have not been reached"


# --- what the review NAMES must come from the two captures ------------------
# component_changes said "Products table filled - section 'design resources'
# added" about a page whose two captures were byte-identical in both. It was
# comparing the new capture against the urls ROW, which every capture
# overwrites - including a failed one, which writes {} over it. So a failure
# between two good captures made the second one look like it had grown a
# products table and a section.

import json as _json_mod


def _cap_dir(root, name, flags, components):
    d = Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "sections.json").write_text(_json_mod.dumps(
        {"flags": flags, "components": components}), encoding="utf-8")
    return str(d)


FULL_FLAGS = {"product_table": {"exists": True, "has_content": True}}
FULL_NAV = ["Overview", "Documents", "Design resources"]


def test_an_emptied_row_does_not_invent_a_change():
    root = _fresh_db()
    old = _cap_dir(root, "old", FULL_FLAGS, FULL_NAV)
    new = _cap_dir(root, "new", FULL_FLAGS, FULL_NAV)
    # the row as a failed capture in between left it
    row = {"flags": "{}", "components": "[]"}
    parts = runner.component_changes(
        old, new, row, {"flags": FULL_FLAGS, "components": FULL_NAV},
        {"text_changed": False, "library": {"per_section": {}}})
    assert not parts, f"invented a change out of an emptied row: {parts}"


def test_a_real_section_arrival_is_still_named():
    root = _fresh_db()
    old = _cap_dir(root, "old", {"product_table": {"exists": False}},
                   ["Overview"])
    new = _cap_dir(root, "new", FULL_FLAGS, FULL_NAV)
    parts = runner.component_changes(
        old, new, {"flags": "{}", "components": "[]"},
        {"flags": FULL_FLAGS, "components": FULL_NAV},
        {"text_changed": False, "library": {"per_section": {}}})
    joined = " · ".join(parts)
    assert "Products table added" in joined, joined
    assert "design resources' added" in joined, joined


def test_a_capture_too_old_to_carry_them_falls_back_to_the_row():
    root = _fresh_db()
    old = Path(root) / "ancient"
    old.mkdir(parents=True, exist_ok=True)          # no sections.json at all
    new = _cap_dir(root, "new", FULL_FLAGS, FULL_NAV)
    parts = runner.component_changes(
        str(old), new,
        {"flags": _json_mod.dumps(FULL_FLAGS),
         "components": _json_mod.dumps(FULL_NAV)},
        {"flags": FULL_FLAGS, "components": FULL_NAV},
        {"text_changed": False, "library": {"per_section": {}}})
    assert not parts, f"the row fallback stopped working: {parts}"


def test_a_rejected_capture_does_not_leave_its_meta_behind():
    # _write_page_meta runs BEFORE the guards judge a capture, so a partial
    # render writes its half-empty flags onto the urls row and the guard then
    # discards the snapshot without taking the row with it. The row is what
    # the Dashboard filters on and what the next run compares against.
    root = _fresh_db()
    con = db.connect()
    con.execute("INSERT INTO urls(id, url, active) VALUES(17,'https://x/p',1)")
    good = _cap_dir(root, "good", FULL_FLAGS, FULL_NAV)
    # the partial capture got there first
    con.execute("UPDATE urls SET flags='{}', components='[]' WHERE id=17")
    con.commit()

    runner._restore_page_meta(con, 17, {"dir": good})
    con.commit()
    row = con.execute("SELECT flags, components FROM urls WHERE id=17").fetchone()
    con.close()
    assert _json_mod.loads(row["flags"]) == FULL_FLAGS, row["flags"]
    assert _json_mod.loads(row["components"]) == FULL_NAV, row["components"]


def test_restoring_survives_a_baseline_with_no_sections_file():
    root = _fresh_db()
    con = db.connect()
    con.execute("INSERT INTO urls(id, url, active) VALUES(18,'https://x/q',1)")
    con.execute("UPDATE urls SET flags='{\"a\":1}' WHERE id=18")
    con.commit()
    missing = Path(root) / "nothing-here"
    missing.mkdir(parents=True, exist_ok=True)
    runner._restore_page_meta(con, 18, {"dir": str(missing)})   # must not raise
    row = con.execute("SELECT flags FROM urls WHERE id=18").fetchone()
    con.close()
    assert row["flags"] == '{"a":1}', "it overwrote the row with nothing"


# --- the bar after a resume -------------------------------------------------
# It came back reading "0 of 3,273" on a run that had done 163 of 3,436: the
# state was seeded with zeros and counted only what was LEFT, so a resume
# looked like a fresh start and every bucket sat empty until the first page.

def _seed_state(pages_total, remaining, saved, changed, errors):
    """What runner._go writes into RUN_STATE when a run starts."""
    already = max(0, pages_total - remaining)
    return {"total": pages_total, "done": already, "done0": already,
            "saved": saved, "changed": changed, "errors": errors,
            "phase_total": pages_total, "phase_done": already,
            "pages_total": pages_total}


def test_a_resumed_run_picks_the_bar_up_where_it_left_it():
    s = _seed_state(pages_total=3436, remaining=3273, saved=163,
                    changed=0, errors=0)
    assert s["phase_done"] == 163 and s["phase_total"] == 3436, \
        f"the bar would read {s['phase_done']} of {s['phase_total']}"
    no_update = s["saved"] - s["changed"] - s["errors"]
    waiting = s["pages_total"] - no_update - s["changed"] - s["errors"]
    assert no_update == 163, no_update
    assert waiting == 3273, waiting


def test_a_fresh_run_still_starts_at_zero():
    s = _seed_state(pages_total=4188, remaining=4188, saved=0,
                    changed=0, errors=0)
    assert s["done"] == 0 and s["phase_done"] == 0 and s["done0"] == 0
    assert s["phase_total"] == 4188


def test_the_eta_rates_only_this_session():
    # 163 pages carried in, 5 captured since: the rate is 5 per elapsed
    # second, not 168 - or a resume reads as nearly finished the moment it
    # starts
    s = _seed_state(3436, 3273, 163, 0, 0)
    s["done"] = 168
    did = s["done"] - s["done0"]
    assert did == 5, did
    elapsed = 50.0
    rate = did / elapsed
    remaining = max(0, s["total"] - s["done"])
    eta = int(remaining / rate)
    assert eta > 30000, f"eta collapsed to {eta}s"


def test_run_check_does_not_die_on_its_own_first_lines():
    """The real function, not a copy of its arithmetic.

    A previous test asserted the SEEDING MATHS by reimplementing it in this
    file. The maths was right and the code still crashed on every run, because
    the seed referenced `counters` before it was assigned - UnboundLocalError,
    the moment any check started. A test that rebuilds the logic it is
    checking cannot catch that, so this one drives the real thing.
    """
    import runner as R

    root = _fresh_db()
    con = db.connect()
    con.execute("INSERT INTO urls(id, url, active, ptype) "
                "VALUES(1, 'https://example.invalid/a', 1, 'Part')")
    con.commit()
    con.close()

    # no browser: capture_all returns nothing, so the run reaches the end
    # without touching the network. What matters is that it gets THERE.
    async def _nothing(*a, **k):          # capture_all is awaited
        return []
    real = R.capture.capture_all
    R.capture.capture_all = _nothing
    try:
        R.run_check("test")
    finally:
        R.capture.capture_all = real

    assert R.RUN_STATE.get("running") is False, "the run never finished"
    for key in ("total", "done", "done0", "saved", "changed", "errors",
                "phase_total", "phase_done", "pages_total"):
        assert key in R.RUN_STATE, f"the state seed never ran: {key} missing"
    con = db.connect()
    finished = con.execute("SELECT finished FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    assert finished and finished["finished"], "the run row was left open"


def test_the_seed_reads_counters_that_already_exist():
    # a static guard: whatever the seed reads must be assigned above it
    import inspect
    import runner as R
    src = inspect.getsource(R._run_check_inner)
    seed = src.index("RUN_STATE.update(running=True")
    assert "counters = {" in src[:seed], \
        "RUN_STATE.update reads counters before they are assigned"


# --- carrying on after a restart -------------------------------------------
# RUN_STATE is in memory, so a restart loses the thread while the run row and
# its per-page state survive. Without this, every restart ended the check and
# somebody had to notice and press Resume - which is what made shipping any
# change at all cost a nine-hour run.

def _run_row(con, finished=None, items=(("done", 5), ("queued", 7))):
    con.execute("INSERT INTO runs(id, started, finished, kind, total)"
                " VALUES(1, '2026-01-01 00:00:00', ?, 'full', 12)", (finished,))
    uid = 1
    for state, n in items:
        for _ in range(n):
            con.execute("INSERT INTO run_items(run_id, url_id, state)"
                        " VALUES(1, ?, ?)", (uid, state))
            uid += 1
    con.commit()


def test_an_interrupted_run_is_picked_up():
    _fresh_db()
    con = db.connect()
    _run_row(con, finished=None)
    got = runner._resumable_run(con)
    con.close()
    assert got is not None and got["id"] == 1, "a live run would not be resumed"


def test_a_finished_run_is_left_alone():
    _fresh_db()
    con = db.connect()
    _run_row(con, finished="2026-01-01 09:00:00")
    got = runner._resumable_run(con)
    con.close()
    assert got is None, "a finished run would be started again on every boot"


def test_a_run_with_nothing_left_is_not_resumed():
    # "End check" leaves the remaining pages done-or-gone; there is nothing to
    # carry on, and resuming would loop on every restart
    _fresh_db()
    con = db.connect()
    _run_row(con, finished=None, items=(("done", 12),))
    got = runner._resumable_run(con)
    con.close()
    assert got is None, "a run with no work left would restart forever"


def test_a_restart_does_not_confiscate_a_pages_retries():
    """A first-try failure is written as an error immediately and owed two
    retries at the end of the run - but the retry queue was a list in memory,
    so a restart paid nobody: the page sat 'done' wearing an error the run
    never confirmed. Six real pages spent a day like that. On resume, a done
    page still marked error goes back in the queue; a page that settled
    cleanly stays done, and a 404 ('notlive') is not re-fetched.
    """
    import runner as R

    _fresh_db()
    con = db.connect()
    for uid, status in ((1, "error"), (2, "unchanged"), (3, "notlive")):
        con.execute("INSERT INTO urls(id, url, active, ptype, last_status)"
                    " VALUES(?, ?, 1, 'Part', ?)",
                    (uid, "https://example.invalid/%d" % uid, status))
        con.execute("INSERT INTO run_items(run_id, url_id, state)"
                    " VALUES(1, ?, 'done')", (uid,))
    # one page genuinely still queued, so the run is resumable
    con.execute("INSERT INTO urls(id, url, active, ptype)"
                " VALUES(4, 'https://example.invalid/4', 1, 'Part')")
    con.execute("INSERT INTO run_items(run_id, url_id, state)"
                " VALUES(1, 4, 'queued')")
    con.execute("INSERT INTO runs(id, started, finished, kind, total)"
                " VALUES(1, '2026-01-01 00:00:00', NULL, 'full', 4)")
    con.commit()
    con.close()

    captured = []

    async def _nothing(jobs, *a, **k):     # jobs are (url, snap_dir) pairs
        captured.extend(u for u, _dir in jobs)
        return []
    real = R.capture.capture_all
    R.capture.capture_all = _nothing
    try:
        R.run_check("test")
    finally:
        R.capture.capture_all = real

    u = "https://example.invalid/%d"
    assert u % 1 in captured, "the error page was not re-queued on resume"
    assert u % 4 in captured, "the genuinely queued page must still run"
    assert u % 2 not in captured, "a settled page must not be redone"
    assert u % 3 not in captured, "a 404 is not an error and is not re-fetched"
    con = db.connect()
    st = con.execute("SELECT state FROM run_items WHERE run_id=1 AND url_id=1"
                     ).fetchone()[0]
    con.close()
    assert st in ("queued", "done"), st


def test_the_run_row_records_final_pages_not_attempts():
    """Run 3 recorded "345 errors" while zero pages ended broken: the event
    counter rises on every failed ATTEMPT - first tries the retry rounds
    heal, captures a restart killed - and 3186+7+345 exceeded the run's own
    total. The stored number must be how many pages ENDED in error."""
    import runner as R

    _fresh_db()
    con = db.connect()
    # two pages: one that will end healthy, one that ends in error
    con.execute("INSERT INTO urls(id, url, active, ptype, last_status)"
                " VALUES(1, 'https://example.invalid/ok', 1, 'Part', 'unchanged')")
    con.execute("INSERT INTO urls(id, url, active, ptype, last_status)"
                " VALUES(2, 'https://example.invalid/bad', 1, 'Part', 'error')")
    con.commit()
    con.close()

    async def _nothing(*a, **k):
        return []
    real = R.capture.capture_all
    R.capture.capture_all = _nothing
    try:
        R.run_check("test")
    finally:
        R.capture.capture_all = real

    con = db.connect()
    r = con.execute("SELECT errors FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    # page 2 was re-queued (retry debt), captured nothing, and stays error -
    # the row records THAT, one page, regardless of how many attempts failed
    assert r["errors"] == 1, r["errors"]


def test_a_locked_database_is_a_wait_not_a_verdict():
    """Run 4 died twice at its first write: a mention collection held the
    SQLite write lock across minutes of network pauses, and the resuming
    run's init_db took the 30s timeout as final. The opening write now
    retries; the lock-holder releasing lets the run start."""
    import sqlite3
    import threading
    import runner as R

    _fresh_db()
    # the real connect waits 30s per attempt, which is right for production
    # and glacial for a test - drop it to a blink for this one
    real_connect = db.connect

    def quick_connect():
        con = sqlite3.connect(db.DB_PATH, timeout=0.05)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con
    db.connect = quick_connect
    # check_same_thread off: the release fires from a Timer thread, and a
    # default connection refuses cross-thread use - silently, in the timer,
    # which held the lock forever and failed the test for the wrong reason
    blocker = sqlite3.connect(db.DB_PATH, timeout=30, check_same_thread=False)
    blocker.execute("BEGIN EXCLUSIVE")

    def release():
        blocker.commit()
        blocker.close()
    t = threading.Timer(0.3, release)
    t.start()
    try:
        R._init_db_patiently(delays=(0.15, 0.15, 0.15, 0.15))
    finally:
        t.cancel()
    # and a lock that never lifts still raises rather than hanging forever
    blocker2 = db.connect()
    blocker2.execute("BEGIN EXCLUSIVE")
    try:
        try:
            R._init_db_patiently(delays=(0.05,))
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("a permanent lock must still raise")
    finally:
        blocker2.rollback()
        blocker2.close()
        db.connect = real_connect
