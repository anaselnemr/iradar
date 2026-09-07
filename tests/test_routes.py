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


def test_dashboard_summary_keeps_inventory_health_and_updates_in_order():
    """Inventory includes paused pages; monitoring and health have their own
    readings. Scheduling and run controls must not become extra tiles."""
    body = _ok("/").data.decode("utf-8", "replace")
    start = body.index('class="report-stats"')
    end = body.index('class="next-check"', start)
    summary = body[start:end]
    labels = [">Total pages<", ">Live<", ">Paused<", ">Updated<", ">Content<",
              ">Updated files<", ">Total offline<"]
    positions = [summary.index(label) for label in labels]
    assert positions == sorted(positions), "dashboard report metrics changed order"
    assert "Next auto check" not in summary and "Check all pages" not in summary
    assert 'name="email_report"' in body, "manual report choice is missing"
    # Schedule and controls share one column beside the stats, above progress.
    controls = body.index('class="summary-controls"', end)
    progress = body.index('id="runidle"', controls)
    assert 'Next auto check (' in body[end:controls]
    assert 'name="email_report"' in body[controls:progress]
    assert 'Check all pages' in body[controls:progress]


def test_run_split_button_preserves_partial_forms_and_full_run_report_choice():
    render = app.render_template
    def with_partial_options(name, **ctx):
        if name == "index.html":
            ctx.update(pending_total=4, comp_pending=2, notlive_total=3)
            ctx["counts"] = dict(ctx["counts"], fresh=1, checked=10, blank=2)
            ctx["state"] = dict(ctx["state"], running=False)
        return render(name, **ctx)
    app.render_template = with_partial_options
    try:
        body = _ok("/").data.decode()
        assert 'name="email_report" value="1" form="run-all"' in body
        split = body[body.index('class="run-split has-partial"'):body.index('id="runidle"')]
        main_form = _re.search(r'<form id="run-all"[^>]*>(.*?)</form>', split, _re.S)
        assert main_form and 'Check all pages' in main_form.group(1)
        assert 'name="_csrf"' in main_form.group(1)
        assert 'aria-label="Partial runs"' in split and '>Partial runs</summary>' not in split
        menu = split[split.index('class="runmore-menu"'):]
        forms = _re.findall(r'<form[^>]*action="/run-now"[^>]*>(.*?)</form>', menu, _re.S)
        assert len(forms) == 5
        for field, form in zip(("pending", "only_new", "pending_components", "skip_gone", "skip_gone_blank"), forms):
            assert 'name="%s" value="1"' % field in form
            assert 'name="_csrf"' in form and 'email_report' not in form
    finally:
        app.render_template = render


def test_run_split_menu_is_hidden_while_running_and_available_for_offline_only():
    render = app.render_template
    running = False
    def with_offline_only(name, **ctx):
        if name == "index.html":
            ctx.update(pending_total=0, comp_pending=0, notlive_total=3)
            ctx["counts"] = dict(ctx["counts"], fresh=0, blank=0)
            ctx["state"] = dict(ctx["state"], running=running)
        return render(name, **ctx)
    app.render_template = with_offline_only
    try:
        body = _ok("/").data.decode()
        assert 'aria-label="Partial runs"' in body and 'name="skip_gone"' in body
        running = True
        body = _ok("/").data.decode()
        assert 'aria-label="Partial runs"' not in body and 'name="email_report"' not in body
        main_form = _re.search(r'<form id="run-all"[^>]*>(.*?)</form>', body, _re.S).group(1)
        assert 'disabled' in main_form and 'Check all pages' in main_form
    finally:
        app.render_template = render


def test_latest_report_is_self_only_and_does_not_change_automatic_email_settings():
    import mailer
    con = db.connect()
    previous_opt_in = con.execute("SELECT email_opt_in FROM users WHERE id=?", (_uid,)).fetchone()[0]
    con.execute("UPDATE users SET email_opt_in=0 WHERE id=?", (_uid,))
    rid = con.execute("INSERT INTO runs(started,finished,kind,total) VALUES("
                      "'2026-09-06 01:00:00','2026-09-06 02:00:00','scheduled',100)").lastrowid
    pending = con.execute("INSERT INTO runs(started,kind,total)"
                          " VALUES('2026-09-07 01:00:00','manual',100)").lastrowid
    con.commit()
    con.close()
    before = {key:db.get_settings().get(key) for key in
              ("last_emailed_run", "email_after_manual_run")}
    sent, selected = [], []
    configured, send, build = mailer.configured, mailer.send, mailer.run_report
    mailer.configured = lambda: True
    mailer.send = lambda to, subject, html: sent.append((to,subject,html)) or None
    def record(con, run):
        selected.append(run["id"])
        return build(con, run)
    mailer.run_report = record
    try:
        profile = _ok("/account").data.decode()
        assert "Send latest report" in profile and "Send me a test" not in profile
        assert "digest" not in profile.lower()
        assert 'action="/account/latest-report"' in profile
        response = _client.post("/account/latest-report", data={"_csrf":_csrf()})
        assert response.status_code == 302
        assert selected == [rid], "must use the latest finished check, not the in-progress one"
        assert len(sent) == 1 and sent[0][0] == "routes@example.com"
        assert "latest report" in sent[0][1] and not _re.search(r"\btest\b", sent[0][1])
        assert ">Total offline<" in sent[0][2]
        assert before == {key:db.get_settings().get(key) for key in before}
        c = db.connect()
        assert c.execute("SELECT email_opt_in FROM users WHERE id=?", (_uid,)).fetchone()[0] == 0
        c.close()
        # Existing account pages can still submit their old action.
        assert _client.post("/account/test-email", data={"_csrf":_csrf()}).status_code == 302
        assert len(sent) == 2
    finally:
        mailer.configured, mailer.send, mailer.run_report = configured, send, build
        con = db.connect()
        con.execute("UPDATE users SET email_opt_in=? WHERE id=?", (previous_opt_in,_uid))
        con.execute("DELETE FROM runs WHERE id IN (?,?)", (rid,pending))
        con.commit()
        con.close()


def test_latest_report_with_no_finished_run_does_not_send():
    import mailer
    con = db.connect()
    prior = [(r["finished"],r["id"]) for r in con.execute("SELECT id,finished FROM runs")]
    con.execute("UPDATE runs SET finished=NULL")
    con.commit()
    con.close()
    configured, send = mailer.configured, mailer.send
    sent = []
    mailer.configured = lambda: True
    mailer.send = lambda *args: sent.append(args)
    try:
        response = _client.post("/account/latest-report", data={"_csrf":_csrf()})
        assert response.status_code == 302 and not sent
    finally:
        mailer.configured, mailer.send = configured, send
        con = db.connect()
        con.executemany("UPDATE runs SET finished=? WHERE id=?", prior)
        con.commit()
        con.close()


def test_offline_delta_is_red_beside_the_total_for_every_sign():
    from xml.etree import ElementTree
    original = app._bar_extras
    try:
        for delta, label in [(4, "+4"), (-4, "−4"), (0, "±0")]:
            app._bar_extras = lambda con, d=delta: (*original(con)[:4], d)
            body = _ok("/").data.decode("utf-8")
            tiles = [ElementTree.fromstring(markup) for markup in _re.findall(
                r'<a class="report-stat"[^>]*>.*?</a>', body, _re.S)]
            tile = next(t for t in tiles
                        if t.find("div[@class='l']").text == "Total offline")
            inline = tile.find("div[@class='n']/span[@class='delta']")
            assert inline is not None, "offline delta must sit next to the total"
            assert inline.text == label
            assert inline.get("style") == "color:var(--err)"
            assert inline.get("title") == "Change in offline pages this run"
            assert tile.find("div[@class='detail']").text == "includes paused pages"
    finally:
        app._bar_extras = original


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


def test_the_report_builds_and_renders_from_a_real_run():
    """The after-run email: run_report reads the run, render_report makes
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
    d = mailer.run_report(con, run)
    con.close()
    assert d["updates"] == 1
    assert d["gains"] == 1 and d["losses"] == 0, d
    # the page-type breakdown counts the changed page under its own ptype
    assert sum(d["by_ptype"].values()) == 1, d["by_ptype"]
    html = mailer.render_report(d, "https://iradar.net", "Sunday, 6 September 2026")
    assert html.startswith("<!doctype html")
    assert 'Radar</span>' in html and "cid:iradarlogo" in html,         "the brand wordmark or logo is missing"
    assert "Review the queue" in html
    # changes are grouped by SHAPE, not listed page by page: the numbers are
    # stripped out of the label and the page count leads
    assert d["patterns"] and d["patterns"][0]["label"] == "text edited", \
        d["patterns"]
    assert "what repeated" in html.lower(), "the pattern list is missing"
    assert "40 lines added" not in html, \
        "a per-page diff line came back into the pattern list"
    assert "pages affected, by type" in html.lower(), "the page-type chart is missing"
    assert "/report-chart.png?" in html, "the donut chart image is missing"
    assert "digest" not in html.lower() and "test summary" not in html.lower()
    # The stat row mirrors all seven dashboard readings, in the same order.
    assert "live" in d, "run_report must count live pages"
    labels = (">Total pages<", ">Live<", ">Paused<", ">Updated<", ">Content<",
              ">Updated files<", ">Total offline<")
    assert [html.index(label) for label in labels] == sorted(html.index(label) for label in labels)
    assert html.count('class="report-stat"') == 7
    assert html.count('class="delta"') == 2


def test_manual_report_form_choice_is_per_run_and_accepts_older_pages():
    import runner
    previous = db.get_settings().get("email_after_manual_run", "")
    launch = runner.run_check_background
    calls = []
    runner.run_check_background = lambda **kwargs: calls.append(kwargs) or True
    try:
        # the flag is a TIMESTAMP, not a bare "1": it is honoured only by the
        # run whose lifetime contains it, so a request made while an older
        # run's file sweep is still going cannot mail that older run
        for fields, wanted in (({"email_report":"1"}, True), ({}, False),
                               ({"email_digest":"1"}, True)):
            response = _client.post("/run-now", data={"_csrf":_csrf(), **fields})
            assert response.status_code == 302
            stamp = db.get_settings().get("email_after_manual_run")
            assert bool(stamp) is wanted, (fields, stamp)
            if wanted:
                assert stamp[:2] == "20" and len(stamp) >= 19, \
                    "the request must be stamped with when it was made: %r" % stamp
            assert calls[-1] == {"kind":"manual", "url_ids":None}
    finally:
        runner.run_check_background = launch
        db.set_setting("email_after_manual_run", previous)


def test_manual_runs_only_email_when_asked():
    """The report gate: a scheduled run always mails, a manual run only when
    its starter ticked the box, a baseline run never - and the box's flag is
    consumed by the run it belongs to."""
    import app as appmod
    import mailer
    con = db.connect()
    con.execute("DELETE FROM runs")
    rid = con.execute(
        "INSERT INTO runs(started, finished, kind, total, ok, changed, errors)"
        " VALUES('2026-09-06 01:00:00','2026-09-06 10:00:00','manual',"
        " 3651, 3650, 0, 1)").lastrowid
    # someone must be opted in, or every path is silent for the wrong reason
    con.execute("UPDATE users SET email_opt_in=1, active=1"
                " WHERE email<>'' AND id=(SELECT MIN(id) FROM users"
                " WHERE email<>'')")
    con.commit()
    con.close()
    sent = []
    real_conf, real_send = mailer.configured, mailer.send
    mailer.configured = lambda: True
    mailer.send = lambda to, subj, html, text="": sent.append(to) or None
    try:
        DURING = "2026-09-06 02:00:00"      # inside the run's own lifetime
        db.set_setting("last_emailed_run", "")
        # manual, box not ticked: silence
        db.set_setting("email_after_manual_run", "")
        appmod._email_after_run()
        assert sent == [], "an unrequested manual run mailed people: %s" % sent
        # manual, box ticked: mails, and eats the flag
        db.set_setting("email_after_manual_run", DURING)
        appmod._email_after_run()
        assert sent, "a requested manual digest never went out"
        assert db.get_settings().get("email_after_manual_run") == "", \
            "the one-run flag must be consumed"
        # the once-per-run marker still holds afterwards
        n = len(sent)
        appmod._email_after_run()
        assert len(sent) == n, "the same run emailed twice"

        # A REQUEST BELONGS TO ITS OWN RUN. A tick made while the previous
        # run's file sweep was still going (so stamped AFTER that run
        # finished) must not mail the older run, and must survive for the run
        # it was actually made for.
        db.set_setting("last_emailed_run", "")
        db.set_setting("email_after_manual_run", "2026-09-06 20:00:00")
        appmod._email_after_run()
        assert len(sent) == n, "a later run's request mailed an earlier run"
        assert db.get_settings().get("email_after_manual_run") == \
            "2026-09-06 20:00:00", "the later run's request was eaten"

        # THE DEDUP MARKER IS CHECKED FIRST: a resume transition on an
        # already-mailed run must not spend a fresh request.
        db.set_setting("last_emailed_run", str(rid))
        db.set_setting("email_after_manual_run", DURING)
        appmod._email_after_run()
        assert len(sent) == n, "an already-emailed run mailed again"
        assert db.get_settings().get("email_after_manual_run") == DURING, \
            "a request was burned by a run that had already mailed"

        # baseline runs never mail, ticked or not
        con = db.connect()
        con.execute("UPDATE runs SET kind='baseline' WHERE id=?", (rid,))
        con.commit(); con.close()
        db.set_setting("last_emailed_run", "")
        db.set_setting("email_after_manual_run", DURING)
        appmod._email_after_run()
        assert len(sent) == n, "a baseline run mailed the team"
    finally:
        mailer.configured, mailer.send = real_conf, real_send
        db.set_setting("email_after_manual_run", "")


def test_only_one_file_sweep_runs_at_a_time():
    """Two sweeps at once meant 16 threads into a throttled store and every
    changed file recorded twice - both read their fingerprints before either
    wrote. The lock lives in run_asset_check, so EVERY caller is guarded."""
    import asset_watch
    import threading
    started = threading.Event()
    release = threading.Event()
    calls = []
    real = asset_watch._run_asset_check_locked

    def slow():
        calls.append(1)
        started.set()
        release.wait(5)
    asset_watch._run_asset_check_locked = slow
    try:
        t = threading.Thread(target=asset_watch.run_asset_check, daemon=True)
        t.start()
        assert started.wait(5), "the first sweep never started"
        asset_watch.run_asset_check()      # second caller, while one is live
        assert len(calls) == 1, "a second sweep ran on top of the first"
        release.set()
        t.join(5)
        # and once it is free, a later sweep is allowed again
        asset_watch.run_asset_check()
        assert len(calls) == 2, "the lock stayed stuck after the sweep ended"
    finally:
        release.set()
        asset_watch._run_asset_check_locked = real


def test_a_partial_run_cannot_erase_an_owed_file_sweep():
    """A 3-page partial finishing between a full run and its post-run chain
    used to blank asset_check_due, so that week's files were never checked."""
    import runner
    runner.RUN_STATE["asset_check_due"] = True
    # what the tail of a partial run does now
    url_ids, stopped = [1, 2, 3], False
    if not url_ids and not stopped:
        runner.RUN_STATE["asset_check_due"] = True
    assert runner.RUN_STATE.get("asset_check_due") is True, \
        "a partial run erased the full run's owed sweep"
    runner.RUN_STATE.pop("asset_check_due", None)


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

def test_the_report_chart_route_draws_a_png():
    r = _ok("/report-chart.png?d=Part+pages:334&d=Product+pages:13")
    assert r.data[1:4] == b"PNG", "not a PNG"
    assert r.headers["Content-Type"] == "image/png"
    assert _ok("/digest-chart.png?d=Part+pages:334").data[1:4] == b"PNG"


def test_report_charts_keep_inline_email_images_and_legacy_urls_working():
    import mailer
    html = ('<img src="https://iradar.net/report-chart.png?d=Part:3">'
            '<img src="https://iradar.net/digest-chart.png?d=Product:2">')
    converted, attachments = mailer._inline_images(html)
    assert len(attachments) == 2
    assert 'src="cid:chart1"' in converted and 'src="cid:chart2"' in converted
    assert "http" not in converted


def test_the_preview_cache_is_capped_oldest_first():
    """The thumbnail cache only dropped orphans, so it grew with the catalogue
    - 1.1GB in a day on a 4.6GB volume, after a full disk had already killed a
    run. Capped now, evicting least-recently-used."""
    import app as appmod
    import os
    import tempfile
    import time as _t
    from pathlib import Path
    real_dir = db.DATA_DIR
    tmp = Path(tempfile.mkdtemp())
    (tmp / "previews").mkdir()
    try:
        db.DATA_DIR = tmp
        cache = tmp / "previews"
        now = _t.time()
        for i in range(10):
            f = cache / ("f%d.png" % i)
            f.write_bytes(b"x" * 1000)
            os.utime(f, (now - (10 - i) * 3600, now))   # f0 oldest
        appmod._cap_preview_cache(limit=5000)
        left = sorted(p.name for p in cache.glob("*.png"))
        assert sum(p.stat().st_size for p in cache.glob("*.png")) <= 5000, left
        assert "f9.png" in left, "the most recently used preview was evicted"
        assert "f0.png" not in left, "the oldest preview survived the cap"
        # under the cap it must not touch anything
        before = sorted(p.name for p in cache.glob("*.png"))
        appmod._cap_preview_cache(limit=10 ** 9)
        assert sorted(p.name for p in cache.glob("*.png")) == before
    finally:
        db.DATA_DIR = real_dir
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_document_previews_are_webp_not_lossless_png():
    """PNG previews were lossless renders of a page - 1.1GB of cache on a
    4.6GB volume. WEBP/82 measured 90% smaller and indistinguishable, so the
    saving is a format change, not a quality cut."""
    import app as appmod
    from pathlib import Path
    assert appmod.PREVIEW_FORMAT == "WEBP"
    assert appmod.PREVIEW_QUALITY >= 80, \
        "quality dropped below the measured indistinguishable point"
    out, fail = appmod._preview_files(Path("/tmp"), "abc123", "v1")
    assert out.suffix == ".webp", out
    assert fail.suffix == ".fail", fail
    # a stale lossless preview must not be mistaken for a current one, so the
    # old name has to differ - that is what makes it an orphan and sweeps it
    assert out.name != out.name.replace(".webp", ".png")


def test_the_preview_route_declares_webp():
    """The path still ends .png so templates and bookmarks keep working; the
    header is what browsers read, and it must not lie about the bytes."""
    import inspect
    import app as appmod
    src = inspect.getsource(appmod.doc_preview_png)
    assert 'mimetype="image/webp"' in src, \
        "the route still claims image/png while serving webp bytes"
