"""Capture retention: keep the newest N per page, delete the rest.

This is the job that stops the archive growing forever, and it deletes real
screenshots off disk - so every test here pins something that would be
expensive to get wrong. The dangerous mistake is not "kept too many"; it is
deleting the capture the next run needs to compare against, or the one an
open update draws its diff from.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import db                                          # noqa: E402

_D = Path(tempfile.mkdtemp(prefix="ifxret_"))
db.DATA_DIR = _D
db.SNAP_DIR = _D / "snapshots"
db.CHANGE_DIR = _D / "changes"
db.DB_PATH = _D / "watch.db"
db.init_db()


def _mkshot(con, url_id, n, status="ok"):
    """A capture with a real folder and a byte of 'screenshot' on disk."""
    d = db.SNAP_DIR / str(url_id) / f"cap{n}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "shot.png").write_bytes(b"x" * 1000)
    cur = con.execute("INSERT INTO snapshots(url_id, ts, dir, status)"
                      " VALUES(?,?,?,?)", (url_id, f"t{n}", str(d), status))
    return cur.lastrowid


def _fresh(n_pages=2, n_each=6):
    con = db.connect()
    con.execute("DELETE FROM snapshots")
    con.execute("DELETE FROM changes")
    con.execute("DELETE FROM urls")
    ids = {}
    for p in range(1, n_pages + 1):
        con.execute("INSERT INTO urls(id, url, active) VALUES(?,?,1)",
                    (p, f"https://example.com/{p}"))
        ids[p] = [_mkshot(con, p, i) for i in range(n_each)]
    con.commit()
    return con, ids


def _alive(con, sid):
    row = con.execute("SELECT dir FROM snapshots WHERE id=?", (sid,)).fetchone()
    return row is not None


def test_keeps_exactly_the_newest_n_per_page():
    con, ids = _fresh(n_pages=3, n_each=7)
    con.close()
    db.prune_history(keep=3)
    con = db.connect()
    for page, snaps in ids.items():
        left = [s for s in snaps if _alive(con, s)]
        assert len(left) == 3, f"page {page} kept {len(left)}, expected 3"
        assert set(left) == set(snaps[-3:]), \
            f"page {page} kept the wrong three - must be the NEWEST"
    con.close()


def test_deletes_the_folder_not_just_the_row():
    con, ids = _fresh(n_pages=1, n_each=5)
    doomed = ids[1][0]
    path = Path(con.execute("SELECT dir FROM snapshots WHERE id=?",
                            (doomed,)).fetchone()["dir"])
    con.close()
    assert path.exists()
    db.prune_history(keep=2)
    assert not path.exists(), "row deleted but the screenshot stayed on disk"


def test_reports_what_it_freed():
    _fresh(n_pages=2, n_each=5)[0].close()
    gone, freed = db.prune_history(keep=2)
    assert gone == 6, f"expected 6 deleted, got {gone}"
    assert freed >= 6000, f"freed {freed} bytes, expected the shot.png bytes"


def test_dry_run_deletes_nothing():
    con, ids = _fresh(n_pages=1, n_each=5)
    con.close()
    gone, freed = db.prune_history(keep=1, dry_run=True)
    assert gone == 4 and freed > 0, "dry run should still report the plan"
    con = db.connect()
    assert all(_alive(con, s) for s in ids[1]), "dry run deleted something"
    con.close()


# --- the things that must survive regardless -------------------------------

def test_never_deletes_a_capture_an_open_update_needs():
    con, ids = _fresh(n_pages=1, n_each=8)
    old, new = ids[1][0], ids[1][1]        # both far outside the keep window
    con.execute("INSERT INTO changes(url_id, ts, old_snapshot_id,"
                " new_snapshot_id, reviewed) VALUES(1,'t',?,?,0)", (old, new))
    con.commit()
    con.close()
    db.prune_history(keep=2)
    con = db.connect()
    assert _alive(con, old) and _alive(con, new), \
        "deleted a capture an OPEN update needs - its diff can no longer render"
    con.close()


def test_a_reviewed_update_does_not_protect_its_captures():
    # the whole point of reviewing is that the artwork becomes history
    con, ids = _fresh(n_pages=1, n_each=8)
    old, new = ids[1][0], ids[1][1]
    con.execute("INSERT INTO changes(url_id, ts, old_snapshot_id,"
                " new_snapshot_id, reviewed) VALUES(1,'t',?,?,1)", (old, new))
    con.commit()
    con.close()
    db.prune_history(keep=2)
    con = db.connect()
    assert not _alive(con, old), "a reviewed update kept its old capture alive"
    con.close()


def test_never_deletes_a_capture_still_being_checked():
    con, ids = _fresh(n_pages=1, n_each=6)
    pending = _mkshot(con, 1, 99, status="pending")
    # make it the OLDEST by id order? it is newest here, so bury it instead
    for i in range(100, 104):
        _mkshot(con, 1, i)
    con.commit()
    con.close()
    db.prune_history(keep=2)
    con = db.connect()
    assert _alive(con, pending), "deleted a capture that was mid-check"
    con.close()


def test_a_page_always_keeps_its_newest_capture():
    # keep=0 would erase the baseline the next run compares against, so it is
    # clamped rather than obeyed
    con, ids = _fresh(n_pages=2, n_each=4)
    con.close()
    db.prune_history(keep=0)
    con = db.connect()
    for page, snaps in ids.items():
        left = [s for s in snaps if _alive(con, s)]
        assert len(left) >= 1, f"page {page} lost every capture"
        assert snaps[-1] in left, f"page {page} lost its NEWEST capture"
    con.close()


def test_pages_are_pruned_independently():
    # one page having many captures must not eat into another page's history
    con, ids = _fresh(n_pages=1, n_each=9)
    con.execute("INSERT INTO urls(id, url, active) VALUES(2,'https://e/2',1)")
    only = [_mkshot(con, 2, i) for i in range(2)]
    con.commit()
    con.close()
    db.prune_history(keep=3)
    con = db.connect()
    assert len([s for s in only if _alive(con, s)]) == 2, \
        "a page with fewer captures than the limit lost some"
    assert len([s for s in ids[1] if _alive(con, s)]) == 3
    con.close()


def test_setting_drives_the_default():
    _fresh(n_pages=1, n_each=6)[0].close()
    db.set_setting("keep_snapshots", "2")
    gone, _ = db.prune_history()          # no argument -> read the setting
    assert gone == 4, f"setting ignored: deleted {gone}, expected 4"
    db.set_setting("keep_snapshots", "3")


def test_running_twice_changes_nothing_the_second_time():
    _fresh(n_pages=2, n_each=6)[0].close()
    db.prune_history(keep=3)
    gone, _ = db.prune_history(keep=3)
    assert gone == 0, f"second pass deleted {gone} more - not settled"
