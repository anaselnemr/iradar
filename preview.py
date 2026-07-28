"""Run iRadar on THIS branch, on a throwaway database, without going live.

    python preview.py                     demo data, made up on the spot
    python preview.py watch.db            a copy of a real database
    python preview.py iradar-backup.gz    a backup straight from Settings

Then open http://127.0.0.1:5055.

The point of it: `master` is what iradar.net serves, and work happens on
`dev`, where nothing is live. This is how you LOOK at the work on dev before
merging it - the whole app, your changes, real-shaped data, on your own
machine.

Three things it will not do, by construction rather than by care:

  * It never opens the file you point it at. It copies it first and serves
    the copy, so nothing you click here can reach the real database.
  * It cannot start a capture. Both entry points are replaced before the
    server starts, so "Check all pages" says no instead of fetching four
    thousand pages of infineon.com from your laptop.
  * It has no scheduler and no resume-on-boot: those live in app.py's
    __main__ block, and importing it the way this does never reaches them.
"""
import gzip
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PORT = 5055
DEMO_EMAIL = "you@preview.local"
DEMO_PW = "preview"


def _stage(src: Path) -> Path:
    """Copy the database somewhere disposable and hand back the copy."""
    tmp = Path(tempfile.mkdtemp(prefix="iradar_preview_"))
    dest = tmp / "watch.db"
    if src.suffix == ".gz" or src.name.endswith(".db.gz"):
        with gzip.open(src, "rb") as f_in, open(dest, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    else:
        shutil.copy2(src, dest)
    return tmp


def _seed(con):
    """Enough of everything that every screen has something on it."""
    ptypes = ["Part", "Product", "Family", "Application", "Document"]
    cats = ["documents", "videos", "boards", "design_resources", "images"]
    for i in range(1, 61):
        con.execute(
            "INSERT INTO urls(id, url, active, ptype, last_status)"
            " VALUES(?, ?, 1, ?, ?)",
            (i, "https://www.infineon.com/part/XMC%d-T016F0032-AB" % (1000 + i),
             ptypes[i % len(ptypes)],
             "changed" if i % 7 == 0 else "unchanged"))
        con.execute(
            "INSERT INTO snapshots(id, url_id, ts, dir, status, page_title)"
            " VALUES(?, ?, ?, '', 'ok', ?)",
            (i, i, "2026-07-27T10:%02d:00" % (i % 60),
             "XMC%d 32-bit industrial microcontroller Arm Cortex-M0"
             % (1000 + i)))
        for c in range(3):
            con.execute(
                "INSERT INTO items(url_id, item_url, category, title, item_key)"
                " VALUES(?, ?, ?, ?, ?)",
                (i, "https://www.infineon.com/dgdl/Infineon-XMC%d-v%02d-EN.pdf"
                 % (1000 + i, c), cats[(i + c) % len(cats)],
                 "XMC%d datasheet revision %d" % (1000 + i, c),
                 "k%d-%d" % (i, c)))

    import db
    cd = db.CHANGE_DIR / "0001"
    cd.mkdir(parents=True, exist_ok=True)
    (cd / "diff.json").write_text(json.dumps({
        "added": ["A new paragraph about the gate driver family.",
                  "A second line that appeared."],
        "removed": ["The line that was taken away."],
        "images_added": [], "images_removed": [],
        "lib": {"added": [], "removed": [], "changed": []},
    }), encoding="utf-8")
    con.execute(
        "INSERT INTO changes(id, url_id, ts, old_snapshot_id, new_snapshot_id,"
        " text_changed, lines_added, lines_removed, summary, dir, reviewed)"
        " VALUES(1, 7, '2026-07-27T11:00:00', 7, 7, 1, 2, 1,"
        " 'text: +2 -1', ?, 0)", (str(cd),))
    con.execute(
        "INSERT INTO asset_changes(url, kind, ts, old_etag, new_etag,"
        " old_length, new_length) VALUES("
        "'https://www.infineon.com/dgdl/Infineon-XMC1007-DataSheet.pdf',"
        " 'document', '2026-07-27T11:00:00', 'aaa', 'bbb', 100200, 245800)")
    con.execute(
        "INSERT INTO runs(id, started, finished, kind, total, ok, changed,"
        " errors) VALUES(1, '2026-07-27T10:00:00', '2026-07-27T14:20:00',"
        " 'manual', 60, 59, 3, 1)")


def main():
    src = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else None
    if src is not None and not src.exists():
        sys.exit("No such file: %s" % src)

    import db
    if src is None:
        work = Path(tempfile.mkdtemp(prefix="iradar_preview_"))
        note = "made-up demo data"
    else:
        work = _stage(src)
        note = "a COPY of %s (the original is not opened)" % src.name

    db.DATA_DIR = work
    db.SNAP_DIR = work / "snapshots"
    db.CHANGE_DIR = work / "changes"
    db.DB_PATH = work / "watch.db"
    db.init_db()                      # brings an older copy up to date too

    con = db.connect()
    try:
        empty = con.execute("SELECT COUNT(*) FROM urls").fetchone()[0] == 0
        if empty:
            _seed(con)
            con.commit()
    finally:
        con.close()

    import app
    import auth
    import runner
    import asset_watch

    # NOTHING here may touch the network. Both entry points are replaced
    # before the first request, so every button that would start a capture
    # reports "already running" and does nothing at all.
    runner.run_check_background = lambda *a, **k: False
    asset_watch.run_in_background = lambda *a, **k: False

    if empty:
        uid, _ = auth.create_user(DEMO_EMAIL, "Preview User", "admin")
        auth.set_password(uid, DEMO_PW, must_change=False)
        who = "  Sign in:  %s  /  %s" % (DEMO_EMAIL, DEMO_PW)
    else:
        who = "  Sign in with any account from that database."

    branch = "?"
    try:
        head = (ROOT / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        branch = head.rsplit("/", 1)[-1] if head.startswith("ref:") else head[:7]
    except Exception:
        pass

    print()
    print("  PREVIEW - nothing here is live, nothing here can be captured.")
    print("  Branch:   %s" % branch)
    print("  Data:     %s" % note)
    print("  Open:     http://127.0.0.1:%d" % PORT)
    print(who)
    print("  Ctrl+C to stop. The copy is thrown away with the temp folder.")
    print()
    app.app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
