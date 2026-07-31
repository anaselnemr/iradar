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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "iradar"))

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

    # Two REAL captures on disk, so the update opens with a real text diff and
    # real screenshots - which is what the reviewing screen is: highlighting,
    # the rail, the queue. A change row pointing at empty dirs renders none of
    # it, and a screen nobody can look at is a screen nobody can check.
    before = ["XMC1000 series microcontroller", "Arm Cortex-M0 core at 32 MHz",
              "Operating temperature -40 to 105 C", "RoHS compliant",
              "The line that was taken away.", "Ordering code XMC1007-T016",
              "Package PG-TSSOP-16", "Documents", "Data sheet v1.4",
              "Errata sheet v1.1"]
    after = ["XMC1000 series microcontroller", "Arm Cortex-M0 core at 48 MHz",
             "Operating temperature -40 to 105 C", "RoHS compliant",
             "A new paragraph about the gate driver family.",
             "A second line that appeared.", "Ordering code XMC1007-T016",
             "Package PG-TSSOP-16", "Documents", "Data sheet v1.5",
             "Errata sheet v1.1"]
    try:
        from PIL import Image, ImageDraw
    except Exception:
        Image = None
    con.execute(
        "INSERT INTO snapshots(id, url_id, ts, dir, status, page_title)"
        " VALUES(101, 7, '2026-07-27T11:00:00', '', 'ok',"
        " 'XMC1007 32-bit industrial microcontroller Arm Cortex-M0')")
    for snap_id, lines, mark in ((7, before, None),
                                 (101, after, (60, 300, 300, 360))):
        d = db.SNAP_DIR / "0007" / ("cap%d" % snap_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "text.txt").write_text("\n".join(lines), encoding="utf-8")
        (d / "images.json").write_text(json.dumps(
            ["https://www.infineon.com/img/hero.png",
             "https://www.infineon.com/img/pinout.png"]), encoding="utf-8")
        (d / "sections.json").write_text(json.dumps({
            "documents": [{"title": "Data sheet", "url": "/d/ds.pdf",
                           "meta": "PDF | EN | v1.%d" % (4 if snap_id == 7 else 5)}],
            "design_resources": [], "videos": [], "news": [],
        }), encoding="utf-8")
        (d / "meta.json").write_text(json.dumps(
            {"text_rev": 6, "img_rev": 5, "shot_rev": 7, "sections_rev": 2,
             "shot_scale": 1.0, "shot_ignore": []}), encoding="utf-8")
        if Image is not None:
            im = Image.new("RGB", (600, 800), (252, 252, 254))
            dr = ImageDraw.Draw(im)
            for y in range(40, 760, 90):
                dr.rectangle([40, y, 560, y + 54], fill=(226, 232, 240))
            if mark:
                dr.rectangle(list(mark), fill=(191, 219, 254))
            im.save(d / "shot.png")
        con.execute("UPDATE snapshots SET dir=? WHERE id=?", (str(d), snap_id))
    con.execute(
        "INSERT INTO changes(id, url_id, ts, old_snapshot_id, new_snapshot_id,"
        " text_changed, lines_added, lines_removed, summary, dir, reviewed,"
        " components)"
        " VALUES(1, 7, '2026-07-27T11:00:00', 7, 101, 1, 2, 1,"
        " 'page text (+2/-1 lines)', ?, 0, ?)",
        (str(cd), json.dumps(["page text (+2/-1 lines)",
                              "Documents (~1)"])))
    # a few more waiting, so the queue strip has a real position to show
    for cid, uid in ((2, 14), (3, 21), (4, 28)):
        con.execute(
            "INSERT INTO changes(id, url_id, ts, old_snapshot_id,"
            " new_snapshot_id, text_changed, lines_added, lines_removed,"
            " summary, dir, reviewed) VALUES(?, ?, '2026-07-27T11:%02d:00',"
            " 7, 101, 1, 1, 0, 'page text (+1/-0 lines)', '', 0)"
            % (cid * 7), (cid, uid))
    for cid, uid in ((5, 35), (6, 42)):
        con.execute(
            "INSERT INTO changes(id, url_id, ts, old_snapshot_id,"
            " new_snapshot_id, text_changed, summary, dir, reviewed,"
            " resolution) VALUES(?, ?, '2026-07-26T09:00:00', 7, 101, 1,"
            " 'page text (+3/-2 lines)', '', 1, 'approve')", (cid, uid))
    con.execute(
        "INSERT INTO asset_changes(url, kind, ts, old_etag, new_etag,"
        " old_length, new_length) VALUES("
        "'https://www.infineon.com/dgdl/Infineon-XMC1007-DataSheet.pdf',"
        " 'document', '2026-07-27T11:00:00', 'aaa', 'bbb', 100200, 245800)")
    con.execute(
        "INSERT INTO runs(id, started, finished, kind, total, ok, changed,"
        " errors) VALUES(1, '2026-07-27T10:00:00', '2026-07-27T14:20:00',"
        " 'manual', 60, 59, 3, 1)")
    mentions = [
        ("XMC", "stackexchange", "https://electronics.stackexchange.com/q/1",
         "XMC4700 CCU8 dead-time looks wrong after errata 2.1",
         "Configured complementary PWM but the dead time doubles when...",
         "j.doe", "2026-07-25T10:11:00", 0.1, "pwm,errata",
         "electronics | score 12 | 3 answers"),
        ("XMC", "github", "https://github.com/x/y/issues/12",
         "XMC DAVE generated startup breaks with -O2",
         "The generated SystemInit clobbers the clock tree when optimised.",
         "embedddev", "2026-07-26T09:00:00", -0.6, "toolchain",
         "x/y | open | 9 comment(s)"),
        ("XMC", "forum", "https://forum.arduino.cc/t/xmc/1",
         "XMC1100 Boot Kit works great with the Arduino core",
         "Took ten minutes from unboxing to blink, impressed.",
         "", "2026-07-24T18:40:00", 0.8, "onboarding",
         "Arduino forum | 4 repl(ies)"),
        ("PSOC", "hn", "https://news.ycombinator.com/item?id=2",
         "PSoC 6 as a lower-power alternative for BLE sensors",
         "", "hnuser", "2026-07-22T20:02:00", 0.5, "ble,power", "hn"),
        ("PSOC", "blog", "https://hackaday.com/psoc-thing/",
         "A capacitive harp built on a PSoC 4 dev board",
         "The CapSense block does all the heavy lifting here...",
         "", "2026-07-20T12:00:00", 0.7, "capsense", "Hackaday"),
        ("PSOC", "community", "https://community.infineon.com/t/1",
         "PSoC Creator vs ModusToolbox for new designs?",
         "", "", "2026-07-19T08:30:00", None, "",
         "developer community"),
        ("AURIX", "github", "https://github.com/a/b/issues/3",
         "AURIX TC375 lockstep trap after flash write",
         "We hit CPU1 lockstep traps when writing DFLASH while...",
         "safetyeng", "2026-07-23T15:20:00", -0.4, "safety,flash",
         "a/b | open | 5 comment(s)"),
    ]
    for ent, src, url, title, body, author, posted, score, topics, extra in mentions:
        con.execute(
            "INSERT INTO mentions(entity, source, url, title, body, author,"
            " posted, fetched, score, topics, reason)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ent, src, url, title, body, author, posted,
             "2026-07-27T00:00:00", score, topics, extra))


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
