"""Route smoke tests: every read-only dashboard page must RENDER, not 500.

/assets 500'd in production because its SQL selected u.page_title - a column
that lives on snapshots, not urls. No unit test touched the route, so a
schema/query mismatch shipped. These drive the real Flask app against a temp
database seeded with one of each row the pages join over, so a bad column,
template error or None-handling bug fails here instead of in the browser.
"""
import os
import sys
import tempfile
from pathlib import Path

# no-auth BEFORE importing app (it reads DASH_USER at import time)
os.environ.pop("DASH_USER", None)
os.environ.pop("DASH_PASS", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "iradar"))
import db          # noqa: E402

try:
    _D = Path(tempfile.mkdtemp(prefix="ifxroutes_"))
    db.DATA_DIR = _D
    db.SNAP_DIR = _D / "snapshots"
    db.CHANGE_DIR = _D / "changes"
    db.DB_PATH = _D / "watch.db"
    db.init_db()
    _con = db.connect()
    _con.execute("INSERT INTO urls(id, url, active, ptype, last_status)"
                 " VALUES(1, 'https://www.infineon.com/part/X', 1, 'Part', 'unchanged')")
    _sid = _con.execute(
        "INSERT INTO snapshots(url_id, ts, dir, status, page_title)"
        " VALUES(1, 't', '', 'ok', 'X part page')").lastrowid
    _con.execute("INSERT INTO items(url_id, item_url, category, title, item_key)"
                 " VALUES(1, 'https://a/doc.pdf', 'documents', 'Datasheet', 'k1')")
    _con.execute("INSERT INTO asset_changes(url, kind, ts, old_etag, new_etag,"
                 " old_length, new_length) VALUES('https://a/doc.pdf',"
                 " 'document', 't', 'aaa', 'bbb', 100, 200)")
    _con.commit()
    _con.close()
    import app                                     # noqa: E402
    import auth                                    # noqa: E402
    # every page needs a signed-in person now, so these render as an admin
    _PW = "route-test-password-2026"
    _uid, _ = auth.create_user("routes@example.com", "Routes", "admin")
    auth.set_password(_uid, _PW, must_change=False)
    _client = app.app.test_client()
    # forms carry a CSRF token; load the login page to get one, as a browser does
    import re as _re
    _tok = _re.search(r'name="_csrf" value="([^"]+)"',
                      _client.get("/login").data.decode("utf-8", "replace"))
    _client.post("/login", data={"email": "routes@example.com", "password": _PW,
                                 "_csrf": _tok.group(1) if _tok else ""})
    _READY = True
except Exception:                                  # missing dep etc. -> skip
    from run_tests import Skip
    raise Skip("could not stand up the Flask app for route tests")


def _ok(path):
    r = _client.get(path)
    assert r.status_code == 200, f"{path} -> HTTP {r.status_code}"
    return r


def test_signed_in_session_actually_took():
    # if the sign-in above silently failed, every other test here would be
    # asserting on a redirect to /login instead of the real page
    assert _client.get("/").status_code == 200


def test_assets_page_renders():
    # the exact page that 500'd: joins asset_changes -> items -> urls and
    # resolves a page title from the snapshot
    r = _ok("/assets")
    assert b"File updates" in r.data


def test_dashboard_renders():
    _ok("/")


def test_sentiment_page_renders():
    # off by default (SENTIMENT unset) - must still render its 'switched off' state
    _ok("/sentiment")


def test_status_json_ok():
    import json
    r = _ok("/status")
    json.loads(r.data)     # must be valid JSON the poller can read


# --- accept / reject --------------------------------------------------------

def _csrf():
    import re
    m = re.search(r'name="_csrf" value="([^"]+)"',
                  _client.get("/").data.decode("utf-8", "replace"))
    return m.group(1) if m else ""


def _make_change(url_id=1):
    """A page with an old and a new capture, and an open update between them."""
    con = db.connect()
    old = con.execute("INSERT INTO snapshots(url_id, ts, dir, status)"
                      " VALUES(?, 'old', '', 'ok')", (url_id,)).lastrowid
    new = con.execute("INSERT INTO snapshots(url_id, ts, dir, status)"
                      " VALUES(?, 'new', '', 'ok')", (url_id,)).lastrowid
    cid = con.execute(
        "INSERT INTO changes(url_id, ts, old_snapshot_id, new_snapshot_id,"
        " text_changed, reviewed) VALUES(?, 't', ?, ?, 1, 0)",
        (url_id, old, new)).lastrowid
    con.execute("UPDATE urls SET last_status='changed' WHERE id=?", (url_id,))
    con.commit()
    con.close()
    return cid, old, new


def _row(table, rid, col):
    con = db.connect()
    v = con.execute(f"SELECT {col} FROM {table} WHERE id=?", (rid,)).fetchone()[0]
    con.close()
    return v


def test_rejecting_drops_the_capture_but_not_the_page():
    # Reject means ONE thing: do not use this capture as the baseline. It used
    # to also park the page in a 'rejected' state shown as "Awaiting fix",
    # where it sat until the website changed back - a queue that only grew,
    # for a decision already made.
    cid, old, new = _make_change()
    r = _client.post(f"/change/{cid}/review",
                     data={"resolution": "reject", "_csrf": _csrf()})
    assert r.status_code in (302, 200), f"reject failed ({r.status_code})"
    assert _row("snapshots", new, "rejected") == 1, \
        "the rejected capture can still become a baseline"
    assert _row("snapshots", old, "rejected") == 0, "the OLD capture was rejected"
    assert _row("urls", 1, "last_status") == "unchanged", \
        "the page is still parked in an 'awaiting fix' state after rejecting"
    assert _row("changes", cid, "reviewed") == 1


def test_approving_makes_the_new_capture_current():
    cid, old, new = _make_change()
    r = _client.post(f"/change/{cid}/review",
                     data={"resolution": "approve", "_csrf": _csrf()})
    assert r.status_code in (302, 200)
    assert _row("snapshots", new, "rejected") == 0, \
        "approving marked the capture rejected"
    assert _row("urls", 1, "last_status") == "unchanged"


def test_a_page_with_another_open_update_stays_flagged():
    # clearing one update must not declare the whole page dealt with
    first, _, _ = _make_change()
    second, _, _ = _make_change()
    _client.post(f"/change/{first}/review",
                 data={"resolution": "reject", "_csrf": _csrf()})
    assert _row("urls", 1, "last_status") == "changed", \
        "the page went quiet while another update was still open"
    _client.post(f"/change/{second}/review",
                 data={"resolution": "approve", "_csrf": _csrf()})
    assert _row("urls", 1, "last_status") == "unchanged"


def test_awaiting_fix_is_gone_from_the_filters():
    assert "rejected" not in app.STATUS_FILTERS, \
        "the retired 'Awaiting fix' filter is still selectable"
    html = _client.get("/").data.decode("utf-8", "replace")
    assert "Awaiting fix" not in html, "the Awaiting fix chip is still rendered"


# --- avatar colours ---------------------------------------------------------
# A fixed palette of N colours fails twice: the N+1th person repeats somebody,
# and white initials disappear on the bright half of the wheel. Both are the
# kind of thing you only notice after the tenth person joins.

def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _white_contrast(hexcode):
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(hexcode)
    lum = 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)
    return 1.05 / (lum + 0.05)


def _colours(css):
    import re
    return re.findall(r"#[0-9a-f]{6}", css)


def test_every_avatar_carries_white_text():
    for uid in range(1, 61):
        for hexcode in _colours(app._avatar_css(uid)):
            c = _white_contrast(hexcode)
            assert c >= 4.5, \
                f"account {uid} got {hexcode}, {c:.2f}:1 against white initials"


def test_sixty_accounts_get_sixty_different_colours():
    seen = [app._avatar_css(uid) for uid in range(1, 61)]
    assert len(set(seen)) == 60, "two accounts were handed the same colour"


def test_neighbouring_accounts_are_not_near_neighbouring_colours():
    # ids handed out in a row must not come out as shades of one another
    for uid in range(1, 40):
        a = _rgb(_colours(app._avatar_css(uid))[0])
        b = _rgb(_colours(app._avatar_css(uid + 1))[0])
        apart = sum(abs(x - y) for x, y in zip(a, b))
        assert apart > 90, f"accounts {uid} and {uid + 1} look alike"


def test_a_persons_colour_never_moves():
    # renaming somebody, or anyone else joining or leaving, must not reshuffle
    row = {"id": 7, "name": "Ada Lovelace", "email": "ada@example.com"}
    before = app._avatar_filter(row)
    row["name"] = "Ada L"
    assert app._avatar_filter(row) == before, "a rename changed the colour"
    assert app._avatar_filter({"id": 7}) == before


def test_a_note_is_the_same_colour_as_its_author():
    # the notes feed stores the author's name, not just their id - the avatar
    # must still key off the account or one person shows up two colours
    account = {"id": 12, "name": "Grace", "email": "grace@example.com"}
    note = {"id": 5000, "user_id": 12, "author": "Grace"}
    assert app._avatar_filter(note) == app._avatar_filter(account), \
        "the same person is one colour in notes and another in the corner"


def test_a_deleted_authors_notes_still_get_a_colour():
    orphan = {"id": 5001, "user_id": None, "author": "Someone Who Left"}
    css = app._avatar_filter(orphan)
    assert css.startswith("linear-gradient")
    assert app._avatar_filter("Someone Who Left") == css


# --- how long a check will take -------------------------------------------
# The confirmation box used to multiply by a typed-in 90 seconds a page against
# a real 8, and offer to spend 86 hours on a job that takes 9. An estimate that
# wrong is worse than none: it reads as "do not press this".

def _runs(*specs):
    """Replace run history with (pages, elapsed_seconds) pairs, newest last."""
    con = db.connect()
    con.execute("DELETE FROM runs")
    for i, spec in enumerate(specs):
        pages, secs = spec[0], spec[1]
        start = 1_700_000_000 + i * 1_000_000
        fmt = lambda t: __import__("datetime").datetime.utcfromtimestamp(t) \
            .strftime("%Y-%m-%d %H:%M:%S")
        # ok defaults to the full page count so the run reads as one that
        # finished its own work; a test wanting an interrupted run passes a
        # 3-tuple (pages, secs, ok)
        ok = spec[2] if len(spec) > 2 else pages
        con.execute("INSERT INTO runs(started, finished, kind, total, ok,"
                    " errors) VALUES(?,?,?,?,?,0)",
                    (fmt(start), fmt(start + secs), "full", pages, ok))
    con.commit()
    con.close()


def test_the_estimate_is_measured_not_typed_in():
    _runs((4000, 4000 * 8), (4000, 4000 * 8), (4000, 4000 * 8))
    assert abs(app.seconds_per_page() - 8.0) < 0.01


def test_an_interrupted_run_does_not_poison_the_estimate():
    # elapsed is wall clock, so a run that stopped overnight and resumed counts
    # the downtime as work. It must not drag the estimate with it.
    _runs((4000, 4000 * 8), (4000, 4000 * 40), (4000, 4000 * 9))
    got = app.seconds_per_page()
    assert got < 12, f"one interrupted run moved the estimate to {got:.1f}s/page"


def test_a_resumed_run_that_captured_little_does_not_set_the_rate():
    """Run 10 read 56 hours because a deploy left it open two days while it
    captured 9 of work. A run whose ok is far below its total is downtime, not
    a rate, and must be skipped entirely."""
    # newest run: 4000 pages, but only 500 ok (interrupted), 56h wall clock
    _runs((4000, 4000 * 8, 4000), (4000, 202608, 500))
    got = app.seconds_per_page()
    assert abs(got - 8.0) < 0.5,         "a barely-captured run set the rate to %.1fs/page" % got


def test_a_page_that_takes_minutes_is_rejected_as_downtime():
    _runs((4000, 4000 * 8, 4000), (4000, 4000 * 400, 4000))  # 400s/page
    assert app.seconds_per_page() < 20


def test_a_fresh_install_still_gets_a_number():
    _runs()
    assert app.seconds_per_page() == app._FALLBACK_SECS_PER_PAGE


def test_a_tiny_run_is_not_treated_as_a_rate():
    # checking one page has a fixed browser-startup cost that says nothing
    # about what four thousand pages cost each
    _runs((1, 120))
    assert app.seconds_per_page() == app._FALLBACK_SECS_PER_PAGE


def test_the_dashboard_hands_the_page_a_real_rate():
    _runs((4000, 4000 * 8), (4000, 4000 * 8))
    html = _client.get("/").data.decode("utf-8", "replace")
    m = __import__("re").search(r"SECS_PER_PAGE = ([\d.]+)", html)
    assert m, "the dashboard no longer passes a measured rate to the page"
    assert 7.5 < float(m.group(1)) < 8.5, m.group(1)
    assert "* 90 / 3600" not in html, "the hardcoded 90s guess is back"


def test_almost_nobody_is_handed_an_olive_avatar():
    # Solving for brightness makes every hue readable, which is not the same
    # as every hue being wanted: at that brightness 42-96 degrees is olive,
    # and olive on a person's face reads as a mistake rather than a colour.
    #
    # Not zero, because a wheel with a hole has a seam, and the two people
    # either side of a seam get near-identical colours. A handful in a few
    # hundred is the trade.
    n = 400
    band = [uid for uid in range(1, n + 1)
            if 42 <= app._skip_olive((uid * app._GOLDEN_ANGLE) % 360) < 96]
    assert len(band) / n < 0.01,         f"{len(band)} of {n} landed in the olive arc: {band[:8]}"


def test_bending_the_wheel_keeps_it_a_wheel():
    # monotonic and onto, or two people either side of the bend collide
    xs = [i * 0.5 for i in range(720)]
    ys = [app._skip_olive(x) for x in xs]
    assert all(a <= b + 1e-9 for a, b in zip(ys, ys[1:])), "the remap folds back"
    assert ys[0] < 1 and ys[-1] > 358, f"it no longer covers the wheel: {ys[0]}..{ys[-1]}"


# --- the backup ------------------------------------------------------------
# The volume the server runs on is not backed up by anything, and the database
# is the part that cannot be rebuilt. A backup that is never checked is not a
# backup, so these open what comes out.

def test_the_backup_downloads_and_is_a_real_database():
    import gzip
    import io as _io
    import sqlite3 as _sq
    import tempfile as _tf

    r = _client.get("/settings/backup.db")
    assert r.status_code == 200, r.status_code
    assert "gzip" in r.headers.get("Content-Type", "")
    assert ".db.gz" in r.headers.get("Content-Disposition", "")

    raw = gzip.GzipFile(fileobj=_io.BytesIO(r.data)).read()
    assert raw[:16] == b"SQLite format 3\x00", "that is not a SQLite file"

    # and it opens, and the rows are in it
    path = Path(_tf.mkdtemp(prefix="bk_")) / "restored.db"
    path.write_bytes(raw)
    con = _sq.connect(str(path))
    try:
        got = con.execute("SELECT COUNT(*) FROM urls").fetchone()[0]
        assert got >= 1, "the copy came back empty"
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def test_the_backup_is_admin_only():
    assert "settings_backup" in app.ADMIN_ONLY_VIEWS, \
        "anyone signed in could download the whole database"


def test_the_backup_needs_no_csrf_token():
    # it has to be fetchable by a scheduled download, which cannot hold a token
    assert "settings_backup" not in app.EDITOR_MAY_POST
    r = _client.get("/settings/backup.db")
    assert r.status_code == 200


# --- times on screen -------------------------------------------------------
# Everything is stored UTC and shown local. The conversion used to be a bare
# .astimezone(), which means "whatever zone this machine is set to" - right on
# a laptop by accident, and wrong the moment it moved into a container.

def test_a_stored_time_is_shown_in_the_display_zone():
    from datetime import datetime, timezone as _tz
    stamped = "2026-07-28 08:02:20"                    # UTC
    shown = app.localtime_filter(stamped)
    utc = datetime.strptime(stamped, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_tz.utc)
    want = utc.astimezone(app._DISPLAY_ZONE) if app._DISPLAY_ZONE else utc.astimezone()
    assert shown == want.strftime("%d %b %Y, %H:%M"), shown
    if app._DISPLAY_ZONE:                              # Berlin is ahead of UTC
        assert shown != stamped[:16], "it is still showing the raw UTC string"


def test_the_display_zone_is_named_not_inherited():
    assert app.DISPLAY_TZ, "no display zone configured"
    assert app._DISPLAY_ZONE is not None, \
        f"tzdata is missing, so times fall back to the machine's own zone"


def test_no_page_prints_a_raw_timestamp():
    # a raw slice of the stored string shows UTC and looks like a real time
    import re
    from pathlib import Path as _P
    bad = []
    for tpl in _P("templates").glob("*.html"):
        for n, line in enumerate(tpl.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\[:16\]|\[:19\]", line) and "localtime" not in line:
                bad.append(f"{tpl.name}:{n}")
    assert not bad, f"raw timestamps still on screen: {bad}"


def test_mark_filter_bolds_safely():
    """The tracked word lights up inside a mention - after escaping, so a
    hostile title stays inert."""
    if not _READY:
        return
    out = str(app.mark_filter("PSoC 6 vs psoc4 <script>alert(1)</script>",
                              "PSOC"))
    assert "<b>PSoC</b>" in out
    assert "<b>psoc4</b>" in out or "<b>psoc</b>4" in out
    assert "<script>" not in out and "&lt;script&gt;" in out
    out2 = str(app.mark_filter("Infineon CY8C4247 kit", "PSOC"))
    assert "<b>Infineon</b>" in out2 and "<b>CY8C4247</b>" in out2 or "<b>CY8C</b>4247" in out2


# --- BL / PL filter and the eval-board filter --------------------------------

def _pages_with_owners():
    """Four pages: two business lines, three product lines, one unassigned."""
    con = db.connect()
    con.execute("DELETE FROM urls")
    rows = [("https://www.infineon.com/part/a", "ICW", "81"),
            ("https://www.infineon.com/part/b", "ICW", "92"),
            ("https://www.infineon.com/part/c", "DSI", "81"),
            ("https://www.infineon.com/part/d", "",    "")]
    for u, bl, pl in rows:
        con.execute("INSERT INTO urls(url, active, ptype, last_checked,"
                    " last_status, bl, pl) VALUES(?,1,'Part','x','unchanged',?,?)",
                    (u, bl, pl))
    con.commit()
    con.close()


def _ids(where, params):
    con = db.connect()
    rows = con.execute("SELECT u.url FROM urls u " + where, params).fetchall()
    con.close()
    return sorted(r["url"].rsplit("/", 1)[-1] for r in rows)


def test_one_business_line_narrows_and_two_mean_either():
    _pages_with_owners()
    w, p = app._url_filter("", [], [], [], bls=["ICW"])
    assert _ids(w, p) == ["a", "b"]
    w, p = app._url_filter("", [], [], [], bls=["ICW", "DSI"])
    assert _ids(w, p) == ["a", "b", "c"], "two business lines must mean either"


def test_business_line_and_product_line_are_and_ed_together():
    _pages_with_owners()
    w, p = app._url_filter("", [], [], [], bls=["ICW"], pls=["81"])
    assert _ids(w, p) == ["a"], "the two dimensions must narrow each other"


def test_unassigned_pages_can_be_asked_for_rather_than_hidden():
    """A third of real pages carry no BL. They are not 'some other BL'."""
    _pages_with_owners()
    w, p = app._url_filter("", [], [], [], bls=[app._UNASSIGNED])
    assert _ids(w, p) == ["d"]


def test_an_unfiltered_list_still_holds_everything():
    _pages_with_owners()
    w, p = app._url_filter("", [], [], [])
    assert _ids(w, p) == ["a", "b", "c", "d"]


def test_the_owner_values_are_read_from_the_data():
    _pages_with_owners()
    con = db.connect()
    vals = app._owner_values(con, "bl")
    con.close()
    labels = {v["label"]: v["n"] for v in vals}
    assert labels.get("ICW") == 2 and labels.get("DSI") == 1
    assert labels.get("(not assigned)") == 1, "unassigned must be offered"


def test_eval_board_filter_reads_the_links_already_captured():
    _pages_with_owners()
    con = db.connect()
    uid = con.execute("SELECT id FROM urls WHERE url LIKE '%/a'").fetchone()["id"]
    con.execute("INSERT INTO items(url_id, category, title, item_url, item_key)"
                " VALUES(?, 'design_resources', 'KIT_XMC', ?, 'k1')",
                (uid, "https://www.infineon.com/evaluation-board/KIT_XMC"))
    con.commit()
    con.close()
    w, p = app._url_filter("", [], [], ["haseval"])
    assert _ids(w, p) == ["a"]
    w, p = app._url_filter("", [], [], ["noeval"])
    assert "a" not in _ids(w, p), "a page with a board must not be listed as having none"
    assert "b" in _ids(w, p), "the report people want is the pages with no board"


def test_the_reaper_is_safe_where_there_is_nothing_to_reap():
    """It only ever runs from a SIGCHLD handler, which Windows has not got -
    but a function that throws when called is a trap for the next person."""
    app._reap_children()


def test_the_reaper_collects_what_it_can_and_stops():
    """Drains ready children without blocking, because signals coalesce: one
    delivery can stand for several exits."""
    import os
    calls = []
    real_wait = getattr(os, "waitpid", None)
    had_flag = hasattr(os, "WNOHANG")
    if not had_flag:
        os.WNOHANG = 1           # the container has it; this box need not
    def fake(pid, opts):
        calls.append((pid, opts))
        if len(calls) < 3:
            return (1000 + len(calls), 0)
        return (0, 0)            # nothing left ready
    os.waitpid = fake
    try:
        app._reap_children()
    finally:
        if real_wait is not None:
            os.waitpid = real_wait
        if not had_flag:
            del os.WNOHANG
    assert len(calls) == 3, "should drain until nothing is ready, got %d" % len(calls)
    assert all(pid == -1 for pid, _ in calls), "must collect ANY child"
    assert all(o != 0 for _, o in calls), "must never block waiting"


def test_the_old_export_bookmark_downloads_the_workbook():
    """/export.xlsx decorated a helper by mistake - a July bookmark fetched a
    JSON array of column names instead of a spreadsheet."""
    r = _ok("/export.xlsx")
    assert r.data[:2] == b"PK", "not a workbook: %r" % r.data[:40]


def test_every_shipped_filter_is_reachable_from_a_dropdown():
    """A filter with no UI entry point is a feature only for whoever built it.
    The eval-board and per-block copy filters lived a week like that."""
    for key in ("haseval", "noeval", "over_overview", "over_subheadline",
                "over_meta_description", "over_features", "over_benefits",
                "over_about"):
        assert key in app.CONTENT_KEYS, "%s is not offered anywhere" % key
        assert key in app.ATTR_FILTERS, "%s does not resolve" % key
    body = _ok("/").data.decode("utf-8", "replace")
    assert 'value="haseval"' in body, "the Content dropdown does not offer it"
    assert 'value="over_overview"' in body


def test_the_guide_teaches_the_six_signals():
    body = _ok("/guide").data.decode("utf-8", "replace")
    assert "Six signals" in body, "the guide still counts five"
    assert "Products table" in body, "the new signal is not taught"
    assert "outlined in amber" in body, "the new close-ups are not described"


def test_a_change_with_no_artwork_says_why_nothing_is_boxed():
    """The first question a reviewer asks on a text-only change is "why no
    highlights?" - the page answers it where the boxes would have been."""
    cid, _old, _new = _make_change()
    body = _ok("/change/%d" % cid).data.decode("utf-8", "replace")
    assert "Nothing is boxed because" in body, \
        "a change without artwork does not explain itself"
    # leave the shared page the way it was found - an open change here
    # leaks into every later test that counts url 1 open updates
    _client.post("/change/%d/review" % cid,
                 data={"resolution": "approve", "_csrf": _csrf()})


def test_the_pairs_export_is_the_join_of_the_other_two():
    """One row per document ON each page, with the metadata as columns - the
    join reviewers were doing by hand across two exports."""
    con = db.connect()
    # self-sufficient: earlier filter tests rebuild the urls table wholesale
    uid = con.execute(
        "INSERT INTO urls(url, active, ptype, level, bl, pl, last_status)"
        " VALUES('https://www.infineon.com/part/PAIRX', 1, 'Part', 'PTP',"
        " 'ICW', '81', 'unchanged')").lastrowid
    con.execute("DELETE FROM items WHERE url_id=?", (uid,))
    con.execute(
        "INSERT INTO items(url_id, category, title, item_url, meta, doc_type,"
        " grp, item_key) VALUES(?, 'documents',"
        " 'AN12345 - Getting started', 'https://x/an12345.pdf',"
        " 'Application note | PDF | Public | EN | 02_00 | 1.10MB |"
        "  Aug 12, 2026', 'Application note', 'Documents', 'kpair1')", (uid,))
    con.commit()
    con.close()
    r = _ok("/export/download?mode=pairs&kind=docs")
    assert r.data[:2] == b"PK", "not a workbook"
    import io
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(r.data))
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    head = rows[0]
    assert head[:8] == ("Level", "BL", "PL", "Page", "Page URL",
                        "Section on page", "Kind", "Title"), head
    body = [dict(zip(head, r)) for r in rows[1:]]
    mine = [b for b in body if b["Title"] == "AN12345 - Getting started"]
    assert mine, "the document row is missing"
    b = mine[0]
    assert b["BL"] == "ICW" and b["PL"] == "81" and b["Level"] == "PTP"
    assert b["Page URL"].endswith("/part/PAIRX")
    assert b["File type"] == "PDF", b
    assert b["Access"] == "Public", b
    assert b["Version"] == "02_00", b
    assert b["Date"] == "Aug 12, 2026", b
    assert b["Link"] == "https://x/an12345.pdf", b


def test_everything_per_page_is_one_click_and_carries_the_lot():
    """The quick download: no columns ticked, all=1 means give them
    everything - URL first, every owner and component's content."""
    con = db.connect()
    uid = con.execute(
        "INSERT INTO urls(url, active, ptype, level, bl, pl, last_status,"
        " components) VALUES('https://www.infineon.com/part/ALLX', 1, 'Part',"
        " 'PTP', 'ICW', '92', 'unchanged', '[]')").lastrowid
    con.execute(
        "INSERT INTO items(url_id, category, title, item_url, meta, item_key)"
        " VALUES(?, 'documents', 'AN99 datasheet', 'https://x/an99.pdf',"
        " 'Datasheet | PDF | Public | EN', 'kall1')", (uid,))
    con.commit()
    con.close()
    r = _ok("/export/download?mode=pages&go=1&all=1")
    assert r.data[:2] == b"PK", "not a workbook"
    import io
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(r.data)).active
    head = [c.value for c in ws[1]]
    assert head[0] == "URL", "URL must lead the row: %s" % head[:4]
    assert "Documents" in head, "the documents column is missing"
    assert "BL" in head and "Owner" in head, "owners missing from everything"
    body = [dict(zip(head, [c.value for c in row])) for row in ws.iter_rows(min_row=2)]
    mine = [b for b in body if b["URL"].endswith("/part/ALLX")]
    assert mine, "the page row is missing"
    assert "AN99 datasheet" in (mine[0]["Documents"] or ""), \
        "the document content was not spelled out: %r" % mine[0]["Documents"]
    assert mine[0]["BL"] == "ICW"


def test_the_digest_builds_and_renders_from_a_real_run():
    """The after-run email: run_digest reads the run, render_digest makes
    valid HTML with the brand logo reference and both good and bad counts."""
    import mailer
    con = db.connect()
    # a finished run plus one gain and one loss change tied to it
    con.execute("DELETE FROM runs")
    rid = con.execute(
        "INSERT INTO runs(started, finished, kind, total, ok, changed, errors)"
        " VALUES('2026-09-06 01:00:00','2026-09-06 10:00:00','scheduled',"
        " 3651, 3600, 41, 2)").lastrowid
    u = con.execute("SELECT id FROM urls LIMIT 1").fetchone()["id"]
    s1 = con.execute("INSERT INTO snapshots(url_id, ts, dir, status)"
                     " VALUES(?, 'a', '', 'ok')", (u,)).lastrowid
    s2 = con.execute("INSERT INTO snapshots(url_id, ts, dir, status)"
                     " VALUES(?, 'b', '', 'ok')", (u,)).lastrowid
    con.execute("INSERT INTO changes(url_id, ts, old_snapshot_id,"
                " new_snapshot_id, text_changed, lines_added, lines_removed,"
                " summary, reviewed) VALUES(?,'2026-09-06 05:00:00',?,?,1,40,3,"
                " 'page text (+40/-3 lines)', 0)", (u, s1, s2))
    con.commit()
    run = con.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    d = mailer.run_digest(con, run)
    con.close()
    assert d["updates"] == 1
    assert d["gains"] == 1 and d["losses"] == 0, d
    # the page-type breakdown counts the changed page under its own ptype
    assert sum(d["by_ptype"].values()) == 1, d["by_ptype"]
    html = mailer.render_digest(d, "https://iradar.net", "Sunday, 6 September 2026")
    assert html.startswith("<!doctype html")
    assert 'Radar</span>' in html and "cid:iradarlogo" in html,         "the brand wordmark or logo is missing"
    assert "Review the queue" in html
    assert "40 lines added, 3 removed" in html,         "the summary should read in words, not diff notation"
    assert "pages affected, by type" in html.lower(), "the page-type chart is missing"
    assert "/digest-chart.png?" in html, "the donut chart image is missing"


def test_email_alerts_toggle_flips_the_flag():
    _client.post("/account/alerts", data={"on": "1", "_csrf": _csrf()})
    con = db.connect()
    v = con.execute("SELECT email_opt_in FROM users WHERE id=?", (_uid,)).fetchone()[0]
    con.close()
    assert v == 1, "opt-in did not turn on"
    _client.post("/account/alerts", data={"on": "0", "_csrf": _csrf()})
    con = db.connect()
    v = con.execute("SELECT email_opt_in FROM users WHERE id=?", (_uid,)).fetchone()[0]
    con.close()
    assert v == 0, "opt-in did not turn off"

def test_the_digest_chart_route_draws_a_png():
    r = _ok("/digest-chart.png?d=Part+pages:334&d=Product+pages:13")
    assert r.data[1:4] == b"PNG", "not a PNG"
    assert r.headers["Content-Type"] == "image/png"
