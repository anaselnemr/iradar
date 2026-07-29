"""Pack this installation for a Linux server, losing nothing.

Produces one archive holding the database, every page's baseline, every open
update with its diff artwork, the accounts, the notes and the ownership
metadata. Extracted into /app/data on the server, the app carries on exactly
where the laptop left off - the first run there detects real updates instead of
burning a capture cycle re-baselining.

Three things make that work, and all three are silent if you get them wrong:

  PATHS. Every snapshot and change directory is stored in the database as a
  Windows absolute path (C:\\Users\\...\\data\\snapshots\\85\\...). On Linux
  none of those resolve, so the app would come up looking empty while the files
  sat right there. Every stored path is rewritten to the server's layout.

  SIZE. The archive is 3.13 GB as it stands and the volume is 4999 MB, which
  leaves no room to grow. Every screenshot is recompressed on the way through
  to the format the app now writes - half size, 256 colours, 3.3x smaller -
  bringing the whole thing to about 1.3 GB.

  COMPARABILITY. A recompressed baseline has to be comparable with a freshly
  captured page or the next run flags all 4,188. So each snapshot's meta is
  stamped shot_rev/shot_scale to match what capture.py writes today, and the
  recorded mask regions are scaled with the image - they are coordinates INTO
  the picture, and left at full-page scale every mask lands in the wrong place.

Reads the live data and writes elsewhere. It never modifies this installation.

    python migrate_to_server.py            # build the archive
    python migrate_to_server.py --verify   # check an already-built one
"""
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image                                       # noqa: E402
import capture as C                                          # noqa: E402
import db                                                    # noqa: E402

SERVER_DATA = "/app/data"          # where the volume is mounted on the server
OUT = Path(__file__).resolve().parent / "iradar-migration.tar.gz"
STAGE = Path(tempfile.gettempdir()) / "iradar-migrate"


def human(n):
    return "%.2f GB" % (n / 1024 ** 3) if n > 1024 ** 3 else "%.0f MB" % (n / 1024 ** 2)


def tree_size(p):
    p = Path(p)
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def to_server(win_path):
    """C:\\...\\data\\snapshots\\85\\x  ->  /app/data/snapshots/85/x"""
    if not win_path:
        return ""
    s = str(win_path).replace("\\", "/")
    i = s.lower().rfind("/data/")
    if i == -1:
        return win_path                      # not under data/, leave alone
    return SERVER_DATA + s[i + len("/data"):]


def shrink_png(src, dst):
    """Recompress to exactly what capture.py writes now. Returns True if it
    became a smaller, valid image; False means the original was copied."""
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            if C.SHOT_SCALE != 1.0:
                im = im.resize((max(1, int(im.width * C.SHOT_SCALE)),
                                max(1, int(im.height * C.SHOT_SCALE))),
                               Image.Resampling.LANCZOS)
            if C.SHOT_COLORS:
                im = im.convert("P", palette=Image.Palette.ADAPTIVE,
                                colors=C.SHOT_COLORS)
            im.save(dst, "PNG", optimize=True)
        return True
    except Exception:
        shutil.copy2(src, dst)               # unreadable? ship it untouched
        return False


def build():
    if STAGE.exists():
        shutil.rmtree(STAGE, ignore_errors=True)
    (STAGE / "snapshots").mkdir(parents=True)
    (STAGE / "changes").mkdir(parents=True)

    print("source: %s (%s)" % (db.DATA_DIR, human(tree_size(db.DATA_DIR))))
    print()

    # --- 1. the files ------------------------------------------------------
    shrunk = copied = 0
    for kind, src_root in (("snapshots", db.SNAP_DIR), ("changes", db.CHANGE_DIR)):
        if not Path(src_root).exists():
            continue
        for f in Path(src_root).rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(src_root)
            out = STAGE / kind / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            if f.suffix.lower() == ".png":
                if shrink_png(f, out):
                    shrunk += 1
                else:
                    copied += 1
            elif f.name == "meta.json":
                # stamp it as the format we just wrote, and scale the mask
                # regions with the image, or every mask lands wrong
                try:
                    m = json.loads(f.read_text(encoding="utf-8"))
                    if float(m.get("shot_scale", 1.0)) == 1.0 and C.SHOT_SCALE != 1.0:
                        m["shot_ignore"] = [
                            {k: (int(v * C.SHOT_SCALE) if k in ("x", "y", "w", "h") else v)
                             for k, v in r.items()}
                            for r in (m.get("shot_ignore") or [])]
                        m["shot_scale"] = C.SHOT_SCALE
                    m["shot_rev"] = C.SHOT_REV
                    out.write_text(json.dumps(m, indent=1), encoding="utf-8")
                except Exception:
                    shutil.copy2(f, out)
                copied += 1
            else:
                shutil.copy2(f, out)
                copied += 1
        print("  %-10s done" % kind)
    print("  recompressed %d image(s), copied %d other file(s)" % (shrunk, copied))

    # --- 2. the database, with every path rewritten ------------------------
    dbf = STAGE / "watch.db"
    shutil.copy2(db.DB_PATH, dbf)
    con = sqlite3.connect(dbf)
    con.row_factory = sqlite3.Row
    n_s = n_c = 0
    for sid, d in con.execute("SELECT id, dir FROM snapshots WHERE dir<>''").fetchall():
        con.execute("UPDATE snapshots SET dir=? WHERE id=?", (to_server(d), sid))
        n_s += 1
    for cid, d in con.execute("SELECT id, dir FROM changes WHERE dir<>''").fetchall():
        con.execute("UPDATE changes SET dir=? WHERE id=?", (to_server(d), cid))
        n_c += 1
    con.commit()
    con.execute("VACUUM")
    con.close()
    print("  rewrote %d snapshot and %d change paths to %s" % (n_s, n_c, SERVER_DATA))

    # --- 3. check it before shipping it ------------------------------------
    print()
    ok = verify(STAGE)

    # --- 4. pack -----------------------------------------------------------
    print()
    print("packing ...")
    if OUT.exists():
        OUT.unlink()
    with tarfile.open(OUT, "w:gz", compresslevel=6) as tar:
        for item in sorted(STAGE.iterdir()):
            tar.add(item, arcname=item.name)
    print("  %s  (%s)" % (OUT.name, human(OUT.stat().st_size)))
    print("  staged size %s -> fits the 4999 MB volume: %s"
          % (human(tree_size(STAGE)), tree_size(STAGE) < 4999 * 1024 ** 2))
    return ok


def verify(stage):
    """Would the server actually work from this? Checks the things that fail
    silently: a page with no baseline, an open update with a missing picture."""
    stage = Path(stage)
    dbf = stage / "watch.db"
    con = sqlite3.connect(dbf)
    con.row_factory = sqlite3.Row

    def local(server_path):
        """/app/data/snapshots/85/x -> the staged copy of it"""
        s = str(server_path or "")
        if not s.startswith(SERVER_DATA):
            return None
        return stage / s[len(SERVER_DATA) + 1:]

    problems = []
    pages = con.execute("SELECT COUNT(*) FROM urls WHERE active=1").fetchone()[0]
    with_base = 0
    for r in con.execute("SELECT url_id, MAX(id) mid FROM snapshots"
                         " WHERE status='ok' GROUP BY url_id"):
        d = local(con.execute("SELECT dir FROM snapshots WHERE id=?",
                              (r["mid"],)).fetchone()["dir"])
        if d and d.exists():
            with_base += 1

    open_ch = con.execute("SELECT id, old_snapshot_id, new_snapshot_id FROM changes"
                          " WHERE IFNULL(reviewed,0)=0").fetchall()
    broken = 0
    for c in open_ch:
        for sid in (c["old_snapshot_id"], c["new_snapshot_id"]):
            if not sid:
                continue
            row = con.execute("SELECT dir FROM snapshots WHERE id=?", (sid,)).fetchone()
            d = local(row["dir"]) if row else None
            if not d or not (d / "shot.png").exists():
                broken += 1
    stray = con.execute("SELECT COUNT(*) FROM snapshots WHERE dir<>''"
                        " AND dir NOT LIKE ?", (SERVER_DATA + "%",)).fetchone()[0]

    print("verification")
    print("  pages with a baseline      : %d of %d" % (with_base, pages))
    print("  open updates               : %d, with %d missing picture(s)"
          % (len(open_ch), broken))
    print("  paths still pointing local : %d" % stray)
    print("  accounts / notes / metadata: %d / %d / %d"
          % (con.execute("SELECT COUNT(*) FROM users").fetchone()[0],
             con.execute("SELECT COUNT(*) FROM comments").fetchone()[0],
             con.execute("SELECT COUNT(*) FROM urls WHERE IFNULL(pl,'')<>''"
                         " OR IFNULL(bl,'')<>'' OR IFNULL(cm,'')<>''"
                         " OR IFNULL(owner,'')<>''").fetchone()[0]))
    con.close()
    if broken or stray:
        problems.append("broken references")
    print("  -> %s" % ("READY" if not problems else "PROBLEMS: " + ", ".join(problems)))
    return not problems


if __name__ == "__main__":
    if "--verify" in sys.argv:
        sys.exit(0 if verify(STAGE) else 1)
    sys.exit(0 if build() else 1)
