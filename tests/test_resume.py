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
