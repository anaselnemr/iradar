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
