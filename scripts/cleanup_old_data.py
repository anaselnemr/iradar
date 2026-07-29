"""Reclaim disk from finished work, keeping everything still in play.

The snapshot and change archives grow forever: every run stores a full copy of
each page (text, images list, screenshot) and every flagged update stores its
own diff artwork. Once an update has been accepted or rejected, its pictures
are just history - but the CURRENT snapshot of every page must survive, because
that is what the next run compares against, and so must anything an open update
still needs to render.

Kept:
  - the latest good snapshot of every page      (the next run's baseline)
  - every snapshot an OPEN update points at     (or its diff cannot be drawn)
  - every open, unreviewed update and its files
Deleted:
  - older snapshots nothing points at
  - updates already accepted or rejected, and their diff artwork

Run with no arguments for a dry run that only reports. Pass --apply to delete.
"""
import shutil
import sys
from pathlib import Path

import sys
from pathlib import Path

# the app's modules live one level up from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db


def _dir_size(path):
    p = Path(path) if path else None
    if not p or not p.exists():
        return 0
    total = 0
    for f in p.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def gb(n):
    return "%.2f GB" % (n / 1024 ** 3)


def plan(con):
    """Return (snapshots_to_delete, changes_to_delete) as row lists."""
    # snapshots that must survive -------------------------------------------
    keep = set()
    # 1. the newest good snapshot of every page: the next run's baseline
    for r in con.execute(
            "SELECT url_id, MAX(id) FROM snapshots WHERE status='ok'"
            " GROUP BY url_id"):
        keep.add(r[1])
    # 2. anything an OPEN update still needs in order to render
    for r in con.execute(
            "SELECT old_snapshot_id, new_snapshot_id FROM changes"
            " WHERE IFNULL(reviewed,0)=0"):
        keep.update(x for x in (r[0], r[1]) if x)
    # 3. a snapshot still being verified this run is not ours to touch
    for r in con.execute("SELECT id FROM snapshots WHERE status='pending'"):
        keep.add(r[0])

    snaps = [s for s in con.execute("SELECT id, url_id, ts, dir, status"
                                    " FROM snapshots").fetchall()
             if s["id"] not in keep]
    changes = con.execute("SELECT id, url_id, dir FROM changes"
                          " WHERE IFNULL(reviewed,0)<>0").fetchall()
    return snaps, changes


def main(apply_it):
    con = db.connect()
    snaps, changes = plan(con)

    snap_bytes = sum(_dir_size(s["dir"]) for s in snaps)
    chg_bytes = sum(_dir_size(c["dir"]) for c in changes)

    total_snaps = con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    total_chg = con.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    open_chg = con.execute("SELECT COUNT(*) FROM changes"
                           " WHERE IFNULL(reviewed,0)=0").fetchone()[0]

    print("snapshots : %d of %d would go, keeping %d   %s"
          % (len(snaps), total_snaps, total_snaps - len(snaps), gb(snap_bytes)))
    print("updates   : %d of %d would go, keeping %d open   %s"
          % (len(changes), total_chg, open_chg, gb(chg_bytes)))
    print("total free: %s" % gb(snap_bytes + chg_bytes))

    # a page losing its last good snapshot would re-baseline on the next run
    # and report no updates for a cycle - refuse rather than silently do it
    pages = con.execute("SELECT COUNT(DISTINCT url_id) FROM snapshots"
                        " WHERE status='ok'").fetchone()[0]
    left = set(r[0] for r in con.execute(
        "SELECT url_id FROM snapshots WHERE status='ok'"))
    doomed = set(s["url_id"] for s in snaps if s["status"] == "ok")
    kept_ok = set()
    for r in con.execute("SELECT url_id, MAX(id) FROM snapshots"
                         " WHERE status='ok' GROUP BY url_id"):
        kept_ok.add(r[0])
    orphan = (doomed - kept_ok) & left
    if orphan:
        print("REFUSING: %d page(s) would lose their last good snapshot"
              % len(orphan))
        con.close()
        return 1
    print("checked   : all %d pages keep a good snapshot" % pages)

    if not apply_it:
        print()
        print("dry run - nothing deleted. Pass --apply to do it.")
        con.close()
        return 0

    gone_s = gone_c = 0
    for c in changes:
        if c["dir"]:
            shutil.rmtree(c["dir"], ignore_errors=True)
        con.execute("DELETE FROM changes WHERE id=?", (c["id"],))
        gone_c += 1
    for s in snaps:
        if s["dir"]:
            shutil.rmtree(s["dir"], ignore_errors=True)
        con.execute("DELETE FROM snapshots WHERE id=?", (s["id"],))
        gone_s += 1
    con.commit()
    con.execute("VACUUM")          # give the freed database pages back too
    con.close()
    print()
    print("deleted %d snapshot(s) and %d finished update(s), freed %s"
          % (gone_s, gone_c, gb(snap_bytes + chg_bytes)))
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
