"""Tests for durable run state + resume (runner.py + the run_items table).

These exercise the pieces that let an interrupted run continue instead of
redoing captured work, against a throwaway database - no live run, no browser.
The resume path itself only runs on a restart, so these are how it is checked.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "iradar"))
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


def _counters_on_resume(con, run_id=1, requeue=(), owed=()):
    """What a resume seeds its counters with - the real function the run uses,
    not a copy of it, so a change to the carry-forward is covered here."""
    row = con.execute("SELECT ok, changed, errors FROM runs WHERE id=?",
                      (run_id,)).fetchone()
    done_count = con.execute(
        "SELECT COUNT(*) FROM run_items WHERE run_id=? AND state='done'",
        (run_id,)).fetchone()[0] or 0
    return runner._resume_counters(row, done_count, requeue, owed)


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


def test_a_requeued_suspect_is_not_counted_ok_twice():
    """Anas's rule: a run cannot report more pages than it ran. A suspect is
    counted ok at capture and held afterwards, so a resume that re-captures it
    (its pending snapshot is gone) would count the same page a second time.
    Live run 6 finished "3,574 ok of 3,560" that way."""
    con = _run_with_history(done=3500, suspect=14, queued=46,
                            ok=3514, changed=0, errors=0)
    c = _counters_on_resume(con, requeue=[1, 2, 3, 4, 5, 6, 7,
                                          8, 9, 10, 11, 12, 13, 14])
    con.close()
    assert c["ok"] == 3500, \
        f"carried {c['ok']} ok forward; the 14 re-captured suspects are owed" \
        " their count when they land, not before"


def test_a_requeued_owed_page_is_not_counted_error_twice():
    """A page written as an error before it earned its retries goes back in the
    queue on resume. Its error came with it, and the recapture writes another."""
    con = _run_with_history(done=600, suspect=0, queued=100,
                            ok=560, changed=0, errors=40)
    c = _counters_on_resume(con, owed=list(range(40)))
    con.close()
    assert c["errors"] == 0, \
        f"carried {c['errors']} errors for pages about to be re-judged"


def test_withdrawing_a_carried_tally_never_goes_negative():
    con = _run_with_history(done=5, suspect=0, queued=0,
                            ok=1, changed=0, errors=1)
    c = _counters_on_resume(con, requeue=[1, 2, 3], owed=[4, 5, 6])
    con.close()
    assert c["ok"] == 0 and c["errors"] == 0, \
        f"a bad ledger must floor at zero, not report {c}"


def test_a_fresh_resume_with_nothing_requeued_carries_everything():
    con = _run_with_history(done=618, suspect=54, queued=2763,
                            ok=578, changed=3, errors=40)
    c = _counters_on_resume(con)
    con.close()
    assert (c["ok"], c["changed"], c["errors"], c["saved"]) == (578, 3, 40, 618), \
        f"nothing was re-queued, so nothing should be withdrawn: {c}"


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


def test_an_error_stub_does_not_erase_the_new_capture():
    """Change #6977 said 'Hero image removed - Products table removed -
    section X removed' six times over, while the saved capture's own
    sections.json listed every one of them. The verify path handed
    component_changes a bare error stub (a resumed suspect whose re-capture
    failed), and the stub's empty flags read as 'the page now has nothing'.
    The new side must fall back to the capture ON DISK, as the old side
    always has."""
    import json as _json
    import runner as R

    root = _fresh_db()
    both = {"flags": {"hero_image": {"exists": True, "has_content": True},
                      "product_table": {"exists": True, "has_content": True}},
            "components": ["Overview", "Products", "Documents"]}
    old_dir = root / "old"; old_dir.mkdir()
    new_dir = root / "new"; new_dir.mkdir()
    for d in (old_dir, new_dir):
        (d / "sections.json").write_text(_json.dumps(both), encoding="utf-8")

    stub = {"status": "error", "http_status": None}
    out = R.component_changes(str(old_dir), str(new_dir), None, stub,
                              {"text_changed": False, "images_changed": False,
                               "visual_changed": False})
    assert not any("removed" in o for o in out), out
    assert not any("emptied" in o for o in out), out


def test_pure_loss_smells_like_a_half_render_until_it_repeats():
    """#7118: two stressed captures both silently skipped the price box,
    documents and design resources - 74 lines gone, zero added, no apology
    text for the apology guard - and the double-check 'confirmed' a page
    that was fine. A pure-loss diff is an error and a retry, unless the
    page ALREADY wears that error, in which case the loss is real gutting
    and flags one run late."""
    import runner as R

    fresh = {"last_status": "unchanged"}
    worn = {"last_status": "error"}
    loss = {"text_changed": True, "lines_added": 0, "lines_removed": 74}
    edit = {"text_changed": True, "lines_added": 3, "lines_removed": 74}
    small = {"text_changed": True, "lines_added": 0, "lines_removed": 4}
    assert R._smells_like_half_render(loss, fresh)
    assert not R._smells_like_half_render(edit, fresh), "real edits add lines"
    assert not R._smells_like_half_render(small, fresh), "small cuts are edits"
    assert not R._smells_like_half_render(loss, worn), \
        "the second sighting is the truth"


def test_the_double_check_demands_the_same_difference():
    """Anas's rule: a suspected change is only confirmed when both captures
    agree on WHAT changed, not merely that something did. The fingerprint is
    the multiset of added/removed lines plus the image sets - order-blind,
    count-aware - so a real edit matches itself and a flap does not."""
    import json as _json
    import runner as R

    root = _fresh_db()

    def snap(name, lines, imgs=()):
        d = root / name
        d.mkdir()
        (d / "text.txt").write_text("\n".join(lines), encoding="utf-8")
        (d / "images.json").write_text(_json.dumps(list(imgs)),
                                       encoding="utf-8")
        return str(d)

    base = snap("base", ["alpha", "beta", "gamma"], ["a.png"])
    edit1 = snap("edit1", ["alpha", "beta", "delta"], ["a.png"])
    edit2 = snap("edit2", ["alpha", "beta", "delta"], ["a.png"])
    flap = snap("flap", ["alpha", "gamma"], ["a.png"])

    st = {"ignore_patterns": "", "ignore_image_patterns": ""}
    f_edit1 = R._diff_fingerprint(base, edit1, st)
    f_edit2 = R._diff_fingerprint(base, edit2, st)
    f_flap = R._diff_fingerprint(base, flap, st)
    assert R._fingerprints_agree(f_edit1, f_edit2), \
        "the same edit must match itself across captures"
    assert not R._fingerprints_agree(f_edit1, f_flap), \
        "a different difference must not pass for the same one"


def test_sections_coming_back_empty_is_a_broken_capture_not_an_edit():
    """#7481 / #7459: both pages were published as 'sections removed' while a
    capture of the live page taken minutes later held every item the baseline
    had. The breadcrumb, documents, design resources, partners and components
    all read ZERO at once and nothing grew anywhere - the widgets never
    mounted. The text signal could not catch it (those baselines predate the
    current text method, so it was silently re-baselined) and the items-table
    guard could not either (these pages keep no items rows)."""
    import json as _json
    import runner as R

    root = _fresh_db()

    def sections(name, **counts):
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "sections.json").write_text(_json.dumps(
            {k: [{"title": f"{k}-{i}", "url": f"/{k}/{i}"} for i in range(n)]
             for k, n in counts.items()}), encoding="utf-8")
        return str(d)

    fresh = {"last_status": "unchanged"}
    worn = {"last_status": "error"}
    # the real numbers from #7481
    base = sections("b", breadcrumb=6, documents=6, design_resources=16,
                    partners=2, components=5, features=0)
    gutted = sections("g", breadcrumb=0, documents=0, design_resources=0,
                      partners=0, components=0, features=0)
    one_gone = sections("o", breadcrumb=6, documents=0, design_resources=16,
                        partners=2, components=5, features=0)
    real_edit = sections("e", breadcrumb=6, documents=0, design_resources=17,
                         partners=2, components=5, features=0)

    assert R._smells_like_lost_widgets(base, gutted, fresh), \
        "five sections emptied at once with nothing gained is a failed render"
    assert not R._smells_like_lost_widgets(base, one_gone, fresh), \
        "a single section emptying can be a real removal"
    assert not R._smells_like_lost_widgets(base, real_edit, fresh), \
        "something GROWING means the page really re-rendered - that is an edit"
    assert not R._smells_like_lost_widgets(base, gutted, worn), \
        "the second sighting is the truth, same rule as the half-render guard"
    assert not R._smells_like_lost_widgets(base, str(root / "nope"), fresh), \
        "an unreadable capture is not evidence of anything"


def test_a_page_that_just_went_blank_is_retried_but_a_blank_page_is_not():
    """Anas's rule: blank is judged against what the page was doing before. A
    page that rendered yesterday and answers empty today is a suspected bad
    render and takes the retries; a page that was ALREADY blank is a steady
    state and stays that way instead of erroring every run."""
    import capture as C
    import runner as R

    blank = {"status": "error", "error": f"{C.BLANK_RENDER}: empty body"}
    timeout = {"status": "error", "error": "Timeout 90000ms exceeded"}
    good = {"status": "ok", "error": ""}

    assert R._transient_failure(blank, {"last_status": "unchanged"}), \
        "a page that just went blank gets the same retries as a timeout"
    assert not R._transient_failure(blank, {"last_status": "blank"}), \
        "a page that was already blank is a state, not a fresh failure"
    assert R._transient_failure(timeout, {"last_status": "blank"}), \
        "a timeout is transient whatever the page was doing before"
    assert not R._transient_failure(good, {"last_status": "error"}), \
        "a capture that worked is never retried"
    assert R._transient_failure(blank, None), \
        "with no row to compare against, retry rather than write a verdict"


def test_the_retry_pause_is_a_pause_and_never_the_same_twice():
    """Every page in a retry round was refused inside the same few minutes, so
    coming back together just rebuilds the wave that got blocked. Each round
    waits, and waits a different amount."""
    import capture as C
    import runner as R

    lo1, hi1 = R.RETRY_PAUSE_S[0]
    lo2, hi2 = R.RETRY_PAUSE_S[1]
    assert lo1 > 0 and hi2 > hi1, "round 2 waits longer than round 1"
    waits = {C._jittered(10_000) for _ in range(40)}
    assert len(waits) > 5, "the backoff must not be one fixed number"
    assert all(5_500 <= w <= 14_500 for w in waits), sorted(waits)[:3]

    # a pause must not outlive the request to stop
    ticks = {"n": 0}

    def stop_now():
        ticks["n"] += 1
        return True

    R._nap(120, stop_now)
    assert ticks["n"] == 1, "End check must not wait out a two-minute pause"


def test_a_pure_loss_is_published_only_when_three_captures_agree():
    """Anas's rule: a big loss with nothing added no longer waits a whole run
    to be believed. It goes to a third capture, and the bar depends on what
    the first two said. Agreeing already? The third must agree too - two of
    three would be satisfied by that pair alone, and a pair of broken renders
    agreeing is what published #7118. Disagreeing? Then the third is a real
    vote and any two matching exactly carries it."""
    import json as _json
    import runner as R

    root = _fresh_db()

    def snap(name, lines):
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "text.txt").write_text("\n".join(lines), encoding="utf-8")
        (d / "images.json").write_text("[]", encoding="utf-8")
        return str(d)

    full = [f"line {i}" for i in range(40)]
    base = snap("base", full)
    # the same 25 lines gone from all three captures, nothing added
    thin1 = snap("thin1", full[:15])
    thin2 = snap("thin2", full[:15])
    thin3 = snap("thin3", full[:15])
    # a different loss
    other = snap("other", full[:20])

    st = {"ignore_patterns": "", "ignore_image_patterns": ""}
    f1 = R._diff_fingerprint(base, thin1, st)
    f2 = R._diff_fingerprint(base, thin2, st)
    f3 = R._diff_fingerprint(base, thin3, st)
    f_other = R._diff_fingerprint(base, other, st)

    assert R._loss_survived_three_looks(f1, f2, f3), \
        "three captures printing the same loss is real - flag it now"
    assert not R._loss_survived_three_looks(f1, f2, None), \
        "no third capture is not agreement"
    assert not R._loss_survived_three_looks(f1, f2, f_other), \
        "with the first two agreed, a dissenting third keeps the baseline"
    assert R._loss_survived_three_looks(f1, f_other, f1), \
        "when the two losses differ, a third matching either one carries it"
    assert R._loss_survived_three_looks(f_other, f1, f1), \
        "and it does not matter WHICH of the two the third agrees with"
    assert not R._loss_survived_three_looks(
        f1, f_other, R._diff_fingerprint(base, snap("thin4", full[:5]), st)), \
        "three different losses is nothing steady enough to publish"


def test_there_is_one_third_capture_path():
    """All three reasons to look again - a tie to break, a text loss to
    confirm, sections to confirm - take the same extra capture the same way,
    so they cannot drift apart."""
    import inspect
    import runner as R

    src = inspect.getsource(R._verify_suspects)
    assert "capture.capture_all([(row[\"url\"], dir3)]" not in src, \
        "the extra capture belongs in _third_look, not inline"
    assert src.count("_third_look(") == 3, \
        "the tie-break and both loss checks must use the helper"


def test_empty_sections_are_published_only_when_three_captures_agree():
    """The widget-collapse guard gets the same third look as a pure loss: a
    page CAN really drop a section, so three captures emptying the SAME
    sections publishes now instead of a run later. Widgets that fail to mount
    fail a different set each time, and that still keeps the baseline."""
    import json as _json
    import runner as R

    root = _fresh_db()

    def sections(name, **counts):
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "sections.json").write_text(_json.dumps(
            {k: [{"title": f"{k}-{i}", "url": f"/{k}/{i}"} for i in range(n)]
             for k, n in counts.items()}), encoding="utf-8")
        return str(d)

    base = sections("wb", documents=6, design_resources=16, partners=2,
                    features=4)
    # the same two sections empty in all three captures
    gone1 = sections("w1", documents=0, design_resources=0, partners=2,
                     features=4)
    gone2 = sections("w2", documents=0, design_resources=0, partners=2,
                     features=4)
    gone3 = sections("w3", documents=0, design_resources=0, partners=2,
                     features=4)
    # a capture that stalled on a DIFFERENT set
    other = sections("wo", documents=0, design_resources=16, partners=0,
                     features=4)

    c1 = R._collapse_fingerprint(base, gone1)
    c2 = R._collapse_fingerprint(base, gone2)
    c3 = R._collapse_fingerprint(base, gone3)
    c_other = R._collapse_fingerprint(base, other)

    assert c1 == frozenset({"documents", "design_resources"}), c1
    assert R._loss_survived_three_looks(c1, c2, c3), \
        "the same sections empty three times means the page dropped them"
    assert not R._loss_survived_three_looks(c1, c2, None), \
        "no third capture is not agreement"
    assert not R._loss_survived_three_looks(c1, c2, c_other), \
        "a third capture emptying a different set keeps the baseline"
    assert R._loss_survived_three_looks(c_other, c1, c1), \
        "two captures emptying different sets get the ordinary tie-break"
    assert R._collapse_fingerprint(base, str(root / "missing")) is None, \
        "an unreadable capture is not evidence"
    assert not R._loss_survived_three_looks(None, None, c3), \
        "unreadable sections can never pass for agreement"


def test_a_capture_error_is_a_two_run_verdict():
    """Anas's rule: the red badge is for extreme cases. A page gets three
    attempts every run; failing ALL of them counts one strike, and only a
    second straight run of strikes - six failed attempts - is shown as a
    Capture error. Until then the page keeps its previous status, and one
    good capture wipes the slate."""
    import runner as R

    _fresh_db()
    con = db.connect()
    con.execute("INSERT INTO urls(id, url, active, last_status)"
                " VALUES(9, 'https://www.infineon.com/part/X', 1, 'unchanged')")
    con.commit()

    def row():
        return con.execute("SELECT * FROM urls WHERE id=9").fetchone()

    # strike one: the status is withheld, the streak remembers
    R._fail_page(con, row(), "t1")
    r = row()
    assert r["last_status"] == "unchanged", \
        "one bad run must not paint the page red"
    assert r["error_streak"] == 1

    # strike two: now it is an error, honestly
    R._fail_page(con, row(), "t2")
    r = row()
    assert r["last_status"] == "error" and r["error_streak"] == 2

    # one good capture forgives everything
    R._set_checked(con, 9, "t3", "unchanged")
    r = row()
    assert r["error_streak"] == 0
    # (status stays 'error'->'unchanged' via _set_checked's normal rules)

    # and the streak survives rows that never heard of it (old fixtures)
    assert R._streak_of({"last_status": "unchanged"}) == 0
    con.close()


def test_the_loss_guards_read_the_streak_not_just_the_badge():
    """The second-sighting rule ('once is a bad render, twice is the truth')
    keys on the page already wearing a failure. With the badge withheld for
    a run, the STREAK is the wearing - otherwise a real loss would need four
    runs to publish instead of two."""
    import runner as R

    fresh = {"last_status": "unchanged"}
    struck = {"last_status": "unchanged", "error_streak": 1}
    badged = {"last_status": "error"}
    loss = {"text_changed": True, "lines_added": 0, "lines_removed": 74}
    assert R._smells_like_half_render(loss, fresh), "first sighting defers"
    assert not R._smells_like_half_render(loss, struck), \
        "a struck page's second sighting is the truth"
    assert not R._smells_like_half_render(loss, badged)
    assert R._wearing_error(struck) and R._wearing_error(badged)
    assert not R._wearing_error(fresh)


def test_a_half_walked_library_is_dropped_not_stored():
    """#7481's surviving sibling: a pagination walk that dies at page 3 of 14
    used to store the fraction it had as groundtruth, and the next healthy
    run read the missing documents as 'removed'. A capture carrying
    walk_errors is a failed render: baseline kept, snapshot dropped, page
    queued for the retry rounds - and when none are left, the streak (not a
    first stumble) decides what is shown."""
    import runner as R

    root = _fresh_db()
    con = db.connect()
    con.execute("INSERT INTO urls(id, url, active, last_status)"
                " VALUES(5, 'https://www.infineon.com/part/W', 1, 'unchanged')")
    snap = root / "w1"
    snap.mkdir()
    con.commit()
    row = con.execute("SELECT * FROM urls WHERE id=5").fetchone()
    res = {"status": "ok", "error": "", "title": "W", "text_lines": 200,
           "image_count": 4, "http_status": 200,
           "walk_errors": [("documents", "Exception('detached')")]}
    counters = {"ok": 0, "changed": 0, "errors": 0, "saved": 0}
    retry_q = []
    R._snapshot_dir = lambda uid, ts: snap          # the dir the gate drops
    try:
        R._handle_result(con, res, row, "t1", counters, [], retry_queue=retry_q)
    finally:
        del R._snapshot_dir  # restore the module's real helper
        import importlib
        importlib.reload(R)
    r2 = con.execute("SELECT * FROM urls WHERE id=5").fetchone()
    assert r2["last_status"] == "unchanged", "a dead walk must not paint status"
    assert counters["ok"] == 0 and counters["errors"] == 1
    assert [x["id"] for x in retry_q] == [5], "the page goes back in the queue"
    assert not snap.exists(), "the partial capture's files are dropped"
    sdir = con.execute("SELECT dir FROM snapshots WHERE url_id=5").fetchone()
    assert sdir["dir"] == "", "the snapshot row must not point at evidence"
    con.close()


def test_a_deliberate_stop_parks_suspects_and_survives_reboots():
    """End check used to publish every held suspect unverified - the polite
    button was strictly worse than pulling the plug. A stop now leaves the
    suspects parked exactly as a crash would (ledger rows + pending
    snapshots), stamps the run `stopped` with finished still NULL, and the
    split is: a BOOT never resumes a deliberate stop, a PERSON's next check
    does - where the parked suspects finally get their real double-check."""
    import runner as R

    _fresh_db()
    con = db.connect()
    con.execute("INSERT INTO runs(id, started, stopped) VALUES(4, 't1', 't2')")
    con.execute("INSERT INTO run_items(run_id, url_id, state, prev_snap_id,"
                " snap_id) VALUES(4, 1, 'suspect', 10, 11)")
    con.commit()

    boot = R._resumable_run(con, include_stopped=False)
    assert boot is None, "a reboot must never resume what a person stopped"
    person = R._resumable_run(con)
    assert person is not None and person["id"] == 4, \
        "the next check a person starts must pick the parked suspects up"

    # a run that simply crashed (no stopped stamp) is resumed by both
    con.execute("UPDATE runs SET stopped='' WHERE id=4")
    con.commit()
    assert R._resumable_run(con, include_stopped=False) is not None
    con.close()


def test_library_and_community_suspects_demand_the_same_difference():
    """The last unfingerprinted signals get theirs. A library-only suspect
    used to confirm because the verify capture differed AT ALL - two walks
    that died at different pages confirmed each other. Now the two captures
    must name the SAME items, and a community transition must agree on the
    same from/to pair; and a signal that did not fire contributes nothing,
    so noise on one channel cannot hold another hostage."""
    import json as _json
    import runner as R

    root = _fresh_db()

    def snap(name, docs, community=None, lines=("alpha", "beta")):
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "text.txt").write_text("\n".join(lines), encoding="utf-8")
        (d / "images.json").write_text("[]", encoding="utf-8")
        sec = {"documents": [{"title": t, "url": "/d/%s.pdf" % t, "meta": "PDF"}
                             for t in docs]}
        if community is not None:
            sec["flags"] = {"developer_community": community}
        (d / "sections.json").write_text(_json.dumps(sec), encoding="utf-8")
        (d / "meta.json").write_text(_json.dumps({"sections_rev": 3}),
                                     encoding="utf-8")
        return str(d)

    st = {"ignore_patterns": "", "ignore_image_patterns": ""}
    lib_fired = {"library_changed": True}

    base = snap("fb", ["ds", "an", "errata"])
    walk_died_p2 = snap("f1", ["ds"])              # lost an + errata
    walk_died_p3 = snap("f2", ["ds", "an"])        # lost only errata
    same_loss = snap("f3", ["ds"])                 # the SAME loss as f1

    f1 = R._diff_fingerprint(base, walk_died_p2, st, signals=lib_fired)
    f2 = R._diff_fingerprint(base, walk_died_p3, st, signals=lib_fired)
    f3 = R._diff_fingerprint(base, same_loss, st, signals=lib_fired)
    assert not R._fingerprints_agree(f1, f2), \
        "walks that died at different pages must not confirm each other"
    assert R._fingerprints_agree(f1, f3), \
        "the same real removal must still match itself"

    # a community transition carries its from/to pair
    com_fired = {"community_changed": True}
    cb = snap("cb", ["ds"], community={"exists": True, "threads": ["t1"]})
    c_absent = snap("c1", ["ds"], community={"exists": False})
    c_empty = snap("c2", ["ds"], community={"exists": True, "threads": []})
    g1 = R._diff_fingerprint(cb, c_absent, st, signals=com_fired)
    g2 = R._diff_fingerprint(cb, c_empty, st, signals=com_fired)
    g3 = R._diff_fingerprint(cb, c_absent, st, signals=com_fired)
    assert not R._fingerprints_agree(g1, g2), \
        "absent and empty are different transitions"
    assert R._fingerprints_agree(g1, g3)

    # an unfired signal contributes nothing: same text, noisy library ignored
    quiet = R._diff_fingerprint(base, walk_died_p2, st, signals={})
    quiet2 = R._diff_fingerprint(base, walk_died_p3, st, signals={})
    assert R._fingerprints_agree(quiet, quiet2), \
        "a signal that did not fire must not block agreement"
