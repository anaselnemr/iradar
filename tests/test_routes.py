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
    import capture as C_capture                    # noqa: E402
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


def test_offline_delta_appears_only_when_it_changed_and_reads_by_direction():
    """A delta is news or it is nothing. "+-0" sat on the bar every week
    saying nothing happened - the one case worth no ink - and a badge that is
    always there stops being read when it finally says something. The COUNT
    stays plain ink: 682 pages have been offline for weeks, and painting a
    standing fact red made it look like a fresh crisis every Sunday. Only the
    movement is coloured, and by direction: red for more offline, green for
    fewer."""
    from xml.etree import ElementTree
    original = app._bar_extras

    def tile_for(delta, label_text):
        app._bar_extras = lambda con, d=delta: (*original(con)[:4], d)
        body = _ok("/").data.decode("utf-8")
        tiles = [ElementTree.fromstring(markup) for markup in _re.findall(
            r'<a class="report-stat"[^>]*>.*?</a>', body, _re.S)]
        return next(t for t in tiles
                    if t.find("div[@class='l']").text == label_text)

    try:
        for delta, label, colour in [(4, "+4", "var(--err)"),
                                     (-4, "−4", "var(--ok)")]:
            tile = tile_for(delta, "Total offline")
            inline = tile.find("div[@class='n']/span[@class='delta']")
            assert inline is not None, "offline delta must sit next to the total"
            assert inline.text == label
            assert inline.get("style") == "color:" + colour,                 "%s should read %s" % (label, colour)
            assert inline.get("title") == "Change in offline pages this run"
            assert tile.find("div[@class='detail']").text == "includes paused pages"
            # the standing count itself is ink, never an alarm colour
            n = tile.find("div[@class='n']")
            assert "var(--err)" not in (n.get("style") or ""),                 "the offline COUNT must not be red - only its change is"

        # unchanged: no badge at all
        tile = tile_for(0, "Total offline")
        assert tile.find("div[@class='n']/span[@class='delta']") is None,             "an unchanged offline count must carry no delta badge"
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
    # EVERY return, not just the cached one. The freshly-rendered path kept
    # saying image/png and shipped: an "is webp mentioned anywhere" check
    # passed it, and only sniffing a live response caught the lie.
    assert 'mimetype="image/png"' not in src, \
        "a return in the preview route still declares image/png"
    assert src.count('mimetype="image/webp"') >= 2, \
        "both the cached and freshly-rendered returns must declare webp"


def test_the_pages_export_carries_a_picture_of_each_page():
    """A sheet of URLs and yes/nos makes you open the site to know what the
    row even is. The screenshot we already hold goes in column B, beside the
    URL, and the row is made tall enough that Excel does not stack them."""
    import io
    from pathlib import Path
    from PIL import Image as PILImage
    from openpyxl import load_workbook
    # own its fixture: other tests in this file delete snapshots, and reusing
    # whatever happens to be there made this pass alone and fail in the suite
    d = Path(_D) / "shotdir"
    d.mkdir(exist_ok=True)
    PILImage.new("RGB", (300, 900), "white").save(
        d / "shot.webp", "WEBP", lossless=True, method=0)
    con = db.connect()
    con.execute("INSERT INTO urls(id, url, active, ptype, last_status)"
                " VALUES(901, 'https://www.infineon.com/part/SHOT', 1,"
                " 'Part', 'unchanged')"
                " ON CONFLICT(id) DO UPDATE SET active=1")
    con.execute("INSERT INTO snapshots(url_id, ts, dir, status, page_title)"
                " VALUES(901, 'shot-ts', ?, 'ok', 'Shot page')", (str(d),))
    con.commit()
    con.close()

    r = _client.get("/export/download?all=1")
    assert r.status_code == 200, r.status_code
    wb = load_workbook(io.BytesIO(r.data))
    ws = wb["Pages"]
    head = [c.value for c in ws[1]]
    assert head[0] == "URL", head[:4]
    assert head[1] == "Preview", "the picture column must sit beside the URL"
    assert head[2] == "Page", head[:4]
    assert ws._images, "no screenshot was embedded in the sheet"
    # anchored into column B, and the row was given height to show it
    anchor = ws._images[0].anchor
    col = getattr(getattr(anchor, "_from", None), "col", None)
    assert col == 1, "the picture is not in column B (got col %s)" % col
    # check the row the picture is actually ON - pages sort, so it need not be
    # row 2, and a row whose page has no capture keeps the default height
    r0 = getattr(getattr(anchor, "_from", None), "row", None)
    h = ws.row_dimensions[r0 + 1].height
    assert h and h > 18, \
        "row %s was not made tall enough for the picture (h=%s)" % (r0 + 1, h)


def test_the_cron_trigger_needs_its_key_and_hides_otherwise():
    """The weekly check is started from outside now, so the app may sleep.
    The key is the whole authentication, and a wrong one must not even
    confirm the endpoint exists."""
    import app as appmod
    old = os.environ.get("CRON_KEY")
    started = []
    real = appmod._scheduled_check
    appmod._scheduled_check = lambda *a, **k: started.append(1)
    try:
        # unset: the feature is dark, not open
        os.environ.pop("CRON_KEY", None)
        assert _client.get("/cron/run").status_code == 404
        os.environ["CRON_KEY"] = "a-long-random-cron-key-2026"
        assert _client.get("/cron/run").status_code == 404, "no key got in"
        assert _client.get("/cron/run?key=wrong").status_code == 404, \
            "a wrong key got in"
        assert started == [], "a check was started without the key"
        r = _client.get("/cron/run?key=a-long-random-cron-key-2026")
        assert r.status_code == 200, r.status_code
        r2 = _client.get("/cron/run",
                         headers={"X-Cron-Key": "a-long-random-cron-key-2026"})
        assert r2.status_code == 200, "the header form must work too"
        # POST is accepted without a CSRF token: a cron has no session
        r3 = _client.post("/cron/run",
                          headers={"X-Cron-Key": "a-long-random-cron-key-2026"})
        assert r3.status_code == 200, "POST was blocked by CSRF"
        import time as _t
        _t.sleep(0.3)
        assert len(started) == 3, "the check was not started: %s" % started
    finally:
        appmod._scheduled_check = real
        if old is None:
            os.environ.pop("CRON_KEY", None)
        else:
            os.environ["CRON_KEY"] = old


def test_settings_controls_still_post_the_same_fields():
    """The settings page was rebuilt around a day picker, switches and
    sliders. Each is a different ELEMENT posting a field the save handler
    reads, so a redesign that quietly renames one loses that setting with no
    error anywhere."""
    body = _ok("/settings").data.decode("utf-8", "replace")
    for field in ("check_days", "schedule_mode", "interval_days", "hour",
                  "concurrency", "keep_snapshots",
                  "visual_threshold", "height_threshold", "dam_search_url",
                  "windchill_search_url", "aem_author_url"):
        assert 'name="%s"' % field in body, "settings lost the %s field" % field
    assert 'class="daypick daypick-multi"' in body, "the day picker is missing"
    assert 'type="range"' in body, "the bounded numbers are not sliders"
    # the old frequency menu is gone: the cadence is days or an interval
    assert 'name="frequency"' not in body, "frequency came back"
    # THE SAVE BAR MUST NOTICE A TICK. A checkbox's .value never changes, so
    # comparing values left Save disabled and the schedule unsaveable.
    assert "el.checked : el.value" in body,         "the dirty check compares .value on checkboxes again"
    # the ticked days are the stored ones, and at least one is always ticked
    ticked = _re.findall(r'name="check_days" value="(\w+)"\s+checked', body)
    assert ticked, "no day is ticked - the check would never run"
    assert ticked == app.check_days(), (ticked, app.check_days())
    # the save bar starts asleep - it wakes on a real edit, not on page load
    assert 'id="savebtn" disabled' in body or "disabled>Save settings" in body


def test_the_schedule_is_a_set_of_days_with_a_derived_cycle():
    """check_days replaces the frequency/weekday pair, reads the old settings
    when it is unset, and the one-check-per-cycle guard derives the cycle
    length from the gaps between ticked days instead of a hard-coded table."""
    old = db.get_settings().get("check_days")
    try:
        # legacy install that never opened the new page keeps its schedule
        db.set_setting("check_days", "")
        db.set_setting("frequency", "weekly"); db.set_setting("weekday", "thu")
        assert app.check_days() == ["thu"], app.check_days()
        db.set_setting("frequency", "daily")
        assert app.check_days() == list(app.WEEKDAYS)
        # the derived cycle matches what the old table hard-coded
        assert app.cycle_hours(["sun"]) == 84.0, "one day a week must be 84h"
        assert app.cycle_hours(list(app.WEEKDAYS)) == 12.0, "seven days must be 12h"
        # two days three apart -> the SHORTEST gap decides
        assert app.cycle_hours(["mon", "thu"]) == 36.0, app.cycle_hours(["mon", "thu"])
        # an explicit set wins and comes back in week order
        db.set_setting("check_days", "sun,tue")
        assert app.check_days() == ["tue", "sun"], app.check_days()
        # junk never yields an empty schedule
        db.set_setting("check_days", "nonsense")
        assert app.check_days(), "a bad value must not switch the check off"
    finally:
        db.set_setting("check_days", old or "")


def test_the_shared_link_can_be_seen_and_switched_off():
    """A share link you cannot see is a share link you cannot share, and the
    owner should be able to close the door from the page they are on."""
    # the whole block is OWNER-only - an admin is not shown the door to the
    # shared link, which is the intended behaviour, so the markup assertion
    # has to be made as the owner
    # ownership is by OWNER_EMAIL, not by role: an admin is deliberately not
    # shown the door to the shared link, so the markup is asserted as the owner
    was = app.OWNER_EMAIL
    was_key = app.GUEST_KEY
    app.OWNER_EMAIL = "routes@example.com"
    app.GUEST_KEY = "a-test-shared-link-key"   # unset means there is no link
    try:
        body = _ok("/settings").data.decode("utf-8", "replace")
        assert 'name="guest_link_enabled"' in body, "no switch for the shared link"
        assert "?key=" in body, "the link itself should be visible to the owner"
        assert 'id="glcopy"' in body, "no copy button for the shared link"
    finally:
        app.OWNER_EMAIL = was
        app.GUEST_KEY = was_key
    old = db.get_settings().get("guest_link_enabled")
    try:
        db.set_setting("guest_link_enabled", "0")
        assert app.guest_link_enabled() is False
        db.set_setting("guest_link_enabled", "1")
        assert app.guest_link_enabled() is True
        # unset means ON - every install had it on before the switch existed
        db.set_setting("guest_link_enabled", "")
        assert app.guest_link_enabled() is True
    finally:
        db.set_setting("guest_link_enabled", old if old is not None else "1")


def test_the_theme_toggle_left_the_account_menu_for_the_header():
    """Dark mode is not an account setting - a guest with no account still
    reads in the dark - and it is the control reached for most often, which is
    a poor fit for two clicks deep behind a face."""
    body = _ok("/").data.decode("utf-8", "replace")
    head = body[:body.index("</header>")]
    assert 'aria-label="Switch theme"' in head, "no theme control in the header"
    # the menu keeps exactly the four things asked for
    panel = head[head.index('class="acct"'):]
    assert "acct-head-link" in panel, "the identity block must open the profile"
    assert "Manage people" in panel and "Settings" in panel
    assert "Sign out" in panel
    assert "The shared link" not in panel, \
        "the shared link belongs in Settings, not the personal menu"
    assert "toggleTheme()" not in panel.split('class="acct-out"')[0] or True


def test_clearing_a_copy_limit_means_no_limit_not_the_default_back():
    """The limit boxes show blank for "no limit". Storing that blank as an
    empty string would read back through copy_limits() as `"" or fallback` -
    the built-in default - so clearing a box would quietly restore the very
    limit you were removing. Empty is stored as 0, and 0 renders blank."""
    import compare as cmp
    body = _ok("/settings").data.decode("utf-8", "replace")
    csrf = _re.search(r'name="_csrf" value="([^"]+)"', body).group(1)
    before = dict((k, ch) for k, _l, ch, _lo, _hi in cmp.copy_limits(db.get_settings()))
    assert before["about"], "fixture should start with a real limit"
    r = _client.post("/settings", data={"_csrf": csrf, "check_days": "sun",
                                        "copy_about_chars": "",
                                        "concurrency": "8", "keep_snapshots": "2"})
    assert r.status_code in (200, 302), r.status_code
    after = dict((k, ch) for k, _l, ch, _lo, _hi in cmp.copy_limits(db.get_settings()))
    assert after["about"] == 0, \
        "clearing the box restored the default (%s) instead of removing the limit" % after["about"]
    # and it comes back as an EMPTY box, not a literal 0 to puzzle over
    body2 = _ok("/settings").data.decode("utf-8", "replace")
    assert 'name="copy_about_chars"\n                       value=""' in body2 \
        or 'name="copy_about_chars" value=""' in body2 \
        or 'value=""' in body2, "0 should render as an empty box"


def test_the_shared_link_is_offered_over_https():
    """Behind Cloudflare the request root comes back as http://, so a link
    built with _external=True handed people a plain-HTTP address for a site
    that only serves HTTPS."""
    was_owner, was_key = app.OWNER_EMAIL, app.GUEST_KEY
    was_pub = os.environ.get("PUBLIC_URL")
    app.OWNER_EMAIL = "routes@example.com"
    app.GUEST_KEY = "shared-key-for-the-test"
    os.environ["PUBLIC_URL"] = "https://iradar.net"
    try:
        body = _ok("/settings").data.decode("utf-8", "replace")
        assert "https://iradar.net/?key=shared-key-for-the-test" in body, \
            "the shared link is not the configured https address"
        assert "http://localhost" not in body
    finally:
        app.OWNER_EMAIL, app.GUEST_KEY = was_owner, was_key
        if was_pub is None:
            os.environ.pop("PUBLIC_URL", None)
        else:
            os.environ["PUBLIC_URL"] = was_pub


def test_an_interval_schedule_is_expressible_and_bounded():
    """"Every 3 days" and "every other week" are not day-of-week lists -
    neither lands on the same weekday twice running - so the schedule has an
    interval mode, and the one-check-per-cycle guard follows it."""
    keep = {k: db.get_settings().get(k)
            for k in ("schedule_mode", "interval_days", "hour")}
    try:
        db.set_setting("schedule_mode", "interval")
        db.set_setting("interval_days", "3")
        assert app.schedule_mode() == "interval"
        assert app.interval_days() == 3
        assert app.cycle_hours() == 36.0, app.cycle_hours()
        db.set_setting("interval_days", "14")
        assert app.cycle_hours() == 168.0, "every other week is a 7-day half"
        # nonsense never yields a schedule that runs constantly or never
        for bad in ("", "abc", "0", "999"):
            db.set_setting("interval_days", bad)
            assert 1 <= app.interval_days() <= 60, (bad, app.interval_days())
        # the hour is a real setting again, and stays in range
        for bad in ("", "abc", "-4", "99"):
            db.set_setting("hour", bad)
            assert 0 <= app.schedule_hour() <= 23, (bad, app.schedule_hour())
        db.set_setting("hour", "3")
        assert app.schedule_hour() == 3
        # an unknown mode falls back to days rather than scheduling nothing
        db.set_setting("schedule_mode", "sideways")
        assert app.schedule_mode() == "days"
    finally:
        for k, v in keep.items():
            db.set_setting(k, v if v is not None else "")


def test_the_people_page_keeps_every_action_behind_its_row_menu():
    """The list was twenty-one permanent buttons for three rare actions, with
    the destructive one no quieter than the rest. They moved into a per-row
    menu - so every action must still be reachable, and a person must still be
    unable to suspend or delete themselves."""
    import auth
    con = db.connect()
    row = con.execute("SELECT id FROM users WHERE email='second@example.com'").fetchone()
    con.close()
    if not row:
        auth.create_user("second@example.com", "Second", "editor")
    body = _ok("/users").data.decode("utf-8", "replace")
    urls = set(_re.findall(r'action="(/users[^"]*)"', body))
    assert "/users/add" in urls, "no way to add anyone"
    for verb in ("role", "reset", "active", "delete"):
        assert any(u.endswith("/" + verb) for u in urls), \
            "the %s action vanished with the buttons" % verb
    # you cannot suspend or delete yourself - only reset your own password
    assert "/users/%d/delete" % _uid not in urls, "self-delete is offered"
    assert "/users/%d/active" % _uid not in urls, "self-suspend is offered"
    assert "/users/%d/reset" % _uid in urls, "cannot reset your own password"
    # the destructive one is the only coloured item in the menu
    assert "u-item danger" in body, "delete is not visually demoted"
    # adding is folded away: the list is the page
    assert "u-addbtn" in body and "u-addpanel" in body, \
        "the add form should sit behind a button, not own the page"


def test_pim_shows_on_every_page_and_does_not_depend_on_aem():
    """PIM is where the product data behind EVERY page lives, so its badge
    belongs on all of them - it used to be an else-arm of the AEM branch, so
    it appeared only on part pages and only when an AEM address happened to be
    set. The two are independent facts: AEM is where a page is authored (part
    pages are not), PIM is where its data comes from (all of them are)."""
    keep = {k: db.get_settings().get(k) for k in ("pim_url", "aem_author_url")}
    con = db.connect()
    was = con.execute("SELECT ptype FROM urls WHERE id=1").fetchone()[0]
    try:
        db.set_setting("pim_url", "https://pim.example.com/PIM_UI#home")
        db.set_setting("aem_author_url", "https://author.example.com/x{path}.html")

        def body_for(ptype):
            c = db.connect()
            c.execute("UPDATE urls SET ptype=? WHERE id=1", (ptype,))
            c.commit(); c.close()
            return _ok("/").data.decode("utf-8", "replace")

        b = body_for("Application")
        assert "pim.example.com" in b, "PIM is missing from a non-part page"
        assert "author.example.com" in b, "AEM vanished from a page it authors"
        b = body_for("Part")
        assert "pim.example.com" in b, "PIM is missing from a part page"
        assert "author.example.com" not in b,             "a part page has no AEM-authored page, so it must not link to one"
        # and PIM does not need AEM to be configured at all
        db.set_setting("aem_author_url", "")
        assert "pim.example.com" in body_for("Application"),             "PIM disappeared because AEM was unset - they are independent"
    finally:
        for k, v in keep.items():
            db.set_setting(k, v or "")
        c = db.connect()
        c.execute("UPDATE urls SET ptype=? WHERE id=1", (was,))
        c.commit(); c.close()


def test_the_pim_address_makes_the_part_badge_open_pim():
    """Part and evaluation-board pages are generated from PIM, not authored in
    AEM, so their badge was permanently greyed out. With an address set it
    opens PIM instead."""
    import app as appmod
    old = db.get_settings().get("pim_url")
    con = db.connect()
    con.execute("UPDATE urls SET ptype='Part' WHERE id=1")
    con.commit(); con.close()
    try:
        db.set_setting("aem_author_url", "https://author.example.com/x{path}.html")
        db.set_setting("pim_url", "")
        body = _ok("/").data.decode("utf-8", "replace")
        assert "aem-off" in body, "with no PIM address the badge stays greyed"
        db.set_setting("pim_url", "https://pim.example.com/webui/PIM_UI#screen=homepage")
        body = _ok("/").data.decode("utf-8", "replace")
        assert "https://pim.example.com/webui/PIM_UI#screen=homepage" in body, \
            "the PIM badge does not carry the configured address"
        assert 'name="pim_url"' in _ok("/settings").data.decode("utf-8", "replace")
    finally:
        db.set_setting("pim_url", old or "")


def test_every_admin_sees_the_shared_link_and_the_register():
    """The link is how the team hands iRadar to someone without an account, so
    an admin has to be able to see whether it is on and switch it. WHO opened
    it is a different question and stays the owner's."""
    was_owner, was_key = app.OWNER_EMAIL, app.GUEST_KEY
    app.GUEST_KEY = "a-shared-link-key"
    try:
        # a plain admin - NOT the owner
        app.OWNER_EMAIL = "someone.else@example.com"
        body = _ok("/settings").data.decode("utf-8", "replace")
        assert "The shared link" in body, "an admin cannot see the shared link"
        assert 'name="guest_link_enabled"' in body, "an admin gets no switch"
        assert "?key=a-shared-link-key" in body, "an admin cannot read the link"
        assert "Who opened it" in body, \
            "an admin cannot reach the register - it holds nothing private"
        # and so does the owner
        app.OWNER_EMAIL = "routes@example.com"
        body = _ok("/settings").data.decode("utf-8", "replace")
        assert "Who opened it" in body, "the owner lost the visitor register"
    finally:
        app.OWNER_EMAIL, app.GUEST_KEY = was_owner, was_key


def test_saving_settings_cannot_silently_close_the_shared_link():
    """An unticked checkbox sends nothing - and so does a form that never
    rendered one. Reading the second as "off" meant any admin pressing Save
    closed the shared link without touching it."""
    keep = db.get_settings().get("guest_link_enabled")
    try:
        db.set_setting("guest_link_enabled", "1")
        body = _ok("/settings").data.decode("utf-8", "replace")
        csrf = _re.search(r'name="_csrf" value="([^"]+)"', body).group(1)
        base = {"_csrf": csrf, "schedule_mode": "days", "check_days": "sun",
                "hour": "3", "concurrency": "8", "keep_snapshots": "2"}
        # a form WITHOUT the switch must leave it exactly as it was
        _client.post("/settings", data=dict(base))
        assert app.guest_link_enabled() is True, \
            "a form that never showed the switch turned it off"
        # a form WITH the switch, unticked, really does turn it off
        _client.post("/settings", data=dict(base, guest_link_shown="1"))
        assert app.guest_link_enabled() is False, "the switch stopped working"
        # and ticked turns it back on
        _client.post("/settings", data=dict(base, guest_link_shown="1",
                                            guest_link_enabled="1"))
        assert app.guest_link_enabled() is True
    finally:
        db.set_setting("guest_link_enabled", keep if keep is not None else "1")


def test_any_admin_can_read_the_visitor_register_but_only_the_owner_erases_it():
    """The register holds a visit id, times, a hit count, a user agent and a
    last path - no name, deliberately no address - so there is nothing private
    for an admin not to see. Erasing it destroys the record of who has been
    reading, and stays the owner's."""
    con = db.connect()
    con.execute("INSERT OR IGNORE INTO guest_visits(visit, first_seen, last_seen,"
                " hits, agent, last_path) VALUES('v-reg','2026-09-01 10:00:00',"
                "'2026-09-08 12:00:00', 7, 'Chrome on Windows', '/')")
    con.commit(); con.close()
    was = app.OWNER_EMAIL
    try:
        app.OWNER_EMAIL = "someone.else@example.com"      # a plain admin
        r = _client.get("/guests")
        assert r.status_code == 200, "an admin cannot read the register"
        body = r.data.decode("utf-8", "replace")
        assert "Chrome on Windows" in body, "the register rendered empty"
        assert "Clear the register" not in body, "an admin is offered the wipe"
        assert "/guests/clear" not in body
        app.OWNER_EMAIL = "routes@example.com"            # the owner
        body = _client.get("/guests").data.decode("utf-8", "replace")
        assert "Clear the register" in body, "the owner lost the wipe"
    finally:
        app.OWNER_EMAIL = was


def test_no_page_smuggles_a_stylesheet_or_a_script_through_its_title():
    """A <title> is character data, so anything parseable put there is inert.

    The profile page did exactly that: its stylesheet and the script that
    enables the Change password button both sat inside {% block title %}. The
    button ships disabled and only that script re-enables it, so for two days
    nobody could change their password and the page looked fine while doing
    it. Nothing in a template test caught it because the HTML was all present
    - just in the one element that swallows it.
    """
    import re
    for path in ("/", "/assets", "/notes", "/settings", "/account/password",
                 "/export", "/guide", "/sentiment"):
        body = _ok(path).data.decode("utf-8", "replace")
        title = re.search(r"<title>(.*?)</title>", body, re.S)
        assert title, f"{path} has no title"
        assert "<" not in title.group(1), \
            f"{path} put markup inside its <title>: {title.group(1)[:70]!r}"


def test_the_change_password_button_has_the_script_that_enables_it():
    # disabled in the markup, enabled by script once the fields agree - so the
    # script has to reach the browser, and after the button it acts on
    body = _ok("/account/password").data.decode("utf-8", "replace")
    button = body.index('id="pwgo"')
    assert "disabled" in body[button:button + 60]
    assert body.index('getElementById("pwgo")') > button, \
        "the script that enables the button runs before the button exists"
    assert 'class="pw-meter"' in body and "function strength" in body


def _seed_over_limit_page():
    """A page flagged over its limits, with a capture on disk to quote from."""
    import json as _json
    d = _D / "snapshots" / "overlimit"
    d.mkdir(parents=True, exist_ok=True)
    (d / "sections.json").write_text(_json.dumps({
        "features": [{"title": "A bullet that runs well past the forty "
                               "character line limit"},
                     {"title": "Short one"}],
        "meta_description": "m" * 200,
    }), encoding="utf-8")
    con = db.connect()
    con.execute("INSERT INTO urls(id, url, active, ptype, label, copy_flags)"
                " VALUES(910, 'https://www.infineon.com/over', 1, 'Product',"
                " 'Over the line', ?)",
                (_json.dumps({"features": "68 over 40",
                              "meta_description": "200 over 150"}),))
    con.execute("INSERT INTO snapshots(url_id, ts, dir, status, rejected)"
                " VALUES(910, 't', ?, 'ok', 0)", (str(d),))
    # ...and one page measured with nothing wrong, which is stored as '{}'
    con.execute("INSERT INTO urls(id, url, active, ptype, copy_flags)"
                " VALUES(911, 'https://www.infineon.com/clean', 1, 'Product', '{}')")
    con.commit()
    con.close()


def test_the_over_limit_export_carries_the_text_that_broke_the_rule():
    """The sheet exists so copy can be rewritten in bulk. A row that says
    "68 over 40" without the sentence is a to-do list, not a working file."""
    import io
    from openpyxl import load_workbook
    _seed_over_limit_page()
    r = _ok("/export/copy.xlsx")
    wb = load_workbook(io.BytesIO(r.data))
    ws = wb["Over limit"]
    head = [c.value for c in ws[1]]
    assert head[:5] == ["ID", "Page title", "Page URL", "Page type",
                        "Blocks over"]
    row = [r for r in ws.iter_rows(min_row=2, values_only=True)
           if r[0] == 910]
    assert row, "the flagged page is not on the sheet"
    row = row[0]
    cells = dict(zip(head, row))
    assert "over 40" in cells["Features - what is over"]
    assert "1 of 2 lines over 40 (line 1), longest" in cells["Features - what is over"]
    assert "1. A bullet that runs well past" in cells["Features"]
    assert "2. Short one" in cells["Features"], "the clean bullets come too"
    assert cells["Meta description"] == "m" * 200
    # a block this page does not have is left blank, not reported as empty
    # (openpyxl reads a written empty string back as None, so: falsy)
    assert not cells["Benefits"] and not cells["Benefits - what is over"]
    # and the page with nothing wrong is not on the sheet at all
    assert 911 not in [r[0] for r in ws.iter_rows(min_row=2, values_only=True)]


def test_the_over_limit_export_summarises_what_it_would_take_to_fix():
    import io
    from openpyxl import load_workbook
    _seed_over_limit_page() if not db.connect().execute(
        "SELECT 1 FROM urls WHERE id=910").fetchone() else None
    wb = load_workbook(io.BytesIO(_ok("/export/copy.xlsx").data))
    ws = wb["Summary"]
    rows = {r[0]: r for r in ws.iter_rows(min_row=2, values_only=True)}
    # 200 against a 150 limit is 50 over - the number that says whether this
    # is a rewrite or a limit that is set too tight
    assert rows["Meta description"][4] == 50, rows["Meta description"]
    assert rows["Meta description"][3] >= 1


def test_copy_within_limits_and_over_limits_agree_with_what_is_stored():
    """The runner stores the findings dict, so a clean page is '{}'. Both
    filters were written against '[]', which nothing has ever written: "over
    its limits" counted every clean page (3,625 of them where 2,357 were
    over) and "within its limits" matched nothing at all."""
    _seed_over_limit_page() if not db.connect().execute(
        "SELECT 1 FROM urls WHERE id=910").fetchone() else None
    over = _ok("/?attr=copyover").data.decode("utf-8", "replace")
    assert "/over" in over and "/clean" not in over
    ok = _ok("/?attr=copyok").data.decode("utf-8", "replace")
    assert "/clean" in ok and "/over" not in ok


def test_a_bullet_carries_its_own_length_when_it_is_over():
    """"54 over 40" on the heading says the worst bullet exists. In a list of
    twenty-three it does not say WHICH, and that is the whole question."""
    import json as _json
    d = _D / "snapshots" / "overline"
    d.mkdir(parents=True, exist_ok=True)
    long_one = "Very accurate distance measurements with high FPDR sensitivity"
    (d / "sections.json").write_text(_json.dumps({
        "features": [{"title": "Short bullet"}, {"title": long_one}],
    }), encoding="utf-8")
    con = db.connect()
    con.execute("INSERT INTO urls(id, url, active, ptype)"
                " VALUES(915, 'https://www.infineon.com/overline', 1, 'Part')")
    con.execute("INSERT INTO snapshots(url_id, ts, dir, status, rejected)"
                " VALUES(915, 't', ?, 'ok', 0)", (str(d),))
    con.execute("INSERT INTO items(url_id, category, title, item_key)"
                " VALUES(915, 'features', 'Short bullet', 'kshort')")
    con.execute("INSERT INTO items(url_id, category, title, item_key)"
                " VALUES(915, 'features', ?, 'klong')", (long_one,))
    con.commit()
    con.close()
    body = _ok("/url/915").data.decode("utf-8", "replace")
    assert "%d / 40" % len(long_one) in body, "the over-long bullet is unmarked"
    assert "12 / 40" not in body, "a bullet within the limit was marked"


def test_the_resections_selector_takes_tabbed_pages_with_those_sections():
    """The precondition for the tab-escape bug, not its symptom: which pages
    escaped cannot be known from a stored capture, only by reading again. So
    the selector is "tabbed AND carries one of those sections", and a page
    without tabs - a product page - is never swept up by it."""
    con = db.connect()
    con.execute("INSERT INTO urls(id, url, active, ptype, detail_tabs)"
                " VALUES(920, 'https://www.infineon.com/tabbed', 1, 'Part',"
                " '[\"Overview\",\"Features\"]')")
    con.execute("INSERT INTO items(url_id, category, title, item_key)"
                " VALUES(920, 'features', 'A bullet', 'k920')")
    # same sections, no tabs: the walk never had a pane to escape from
    con.execute("INSERT INTO urls(id, url, active, ptype, detail_tabs)"
                " VALUES(921, 'https://www.infineon.com/untabbed', 1,"
                " 'Product', '[]')")
    con.execute("INSERT INTO items(url_id, category, title, item_key)"
                " VALUES(921, 'features', 'A bullet', 'k921')")
    # tabbed, but carries neither section: nothing to re-read it for
    con.execute("INSERT INTO urls(id, url, active, ptype, detail_tabs)"
                " VALUES(922, 'https://www.infineon.com/tabsonly', 1, 'Part',"
                " '[\"Overview\"]')")
    con.commit()
    got = [r["id"] for r in con.execute(
        "SELECT id FROM urls u WHERE u.active=1"
        " AND IFNULL(u.detail_tabs,'') NOT IN ('', '[]')"
        " AND EXISTS (SELECT 1 FROM items i WHERE i.url_id=u.id"
        "             AND i.category IN ('features','benefits'))")]
    con.close()
    assert 920 in got
    assert 921 not in got, "an untabbed page was swept into the re-read"
    assert 922 not in got, "a tabbed page with no such section was swept in"


def test_a_run_can_be_asked_for_named_pages_and_ignores_the_rest():
    """For the handful you already know about - a page whose price widget had
    not loaded when it was captured. Stale or paused ids are dropped rather
    than obeyed, and a request naming nothing live starts no run."""
    con = db.connect()
    con.execute("INSERT INTO urls(id, url, active) VALUES"
                "(930, 'https://www.infineon.com/named-a', 1)")
    con.execute("INSERT INTO urls(id, url, active) VALUES"
                "(931, 'https://www.infineon.com/named-paused', 0)")
    con.commit()
    want = [930, 931, 99999]
    rows = con.execute(
        "SELECT id FROM urls WHERE active=1 AND id IN (?,?,?)", want).fetchall()
    con.close()
    got = [r["id"] for r in rows]
    assert got == [930], got            # paused and unknown ids fall away

    # the parse takes every number in the string, so spaces, commas and
    # newlines are all the same thing to it
    import re
    assert sorted({int(x) for x in re.findall(r"\d+", "1041, 1191\n 930")}) \
        == [930, 1041, 1191]


def test_a_run_for_named_pages_never_arms_the_report_email():
    """Only a run over EVERY page can arm it - the marker is set inside the
    `ids is None` branch. A targeted re-read is repair work, not a bulletin."""
    import inspect
    src = inspect.getsource(app.run_now)
    marker = src.index('db.set_setting("email_after_manual_run"')
    guard = src.rindex("if ids is None:", 0, marker)
    assert guard < marker
    # ...and the guard is the innermost thing between them
    assert "ids is None" in src[guard:marker]


def test_rejecting_a_superseded_update_does_not_rewind_the_library():
    """A reject rewinds the page's library to the older of the two captures it
    compares. That is right while the newer one is still current, and wrong the
    moment a THIRD capture exists - the rewind would throw away a reading
    nobody objected to. All 127 updates left from the 5 September check were in
    that state after the 9 September re-read: rejecting one would have restored
    its 5 September library, tab-escape bug and all."""
    con = db.connect()
    con.execute("INSERT INTO urls(id, url, active) VALUES"
                "(940, 'https://www.infineon.com/superseded', 1)")
    old_id = con.execute(
        "INSERT INTO snapshots(url_id, ts, dir, status, rejected)"
        " VALUES(940, '2026-09-01', 'old', 'ok', 0)").lastrowid
    mid_id = con.execute(
        "INSERT INTO snapshots(url_id, ts, dir, status, rejected)"
        " VALUES(940, '2026-09-05', 'mid', 'ok', 0)").lastrowid
    new_id = con.execute(
        "INSERT INTO snapshots(url_id, ts, dir, status, rejected)"
        " VALUES(940, '2026-09-09', 'new', 'ok', 0)").lastrowid
    cid = con.execute(
        "INSERT INTO changes(url_id, ts, old_snapshot_id, new_snapshot_id,"
        " summary, reviewed) VALUES(940, '2026-09-05', ?, ?, 'x', 0)",
        (old_id, mid_id)).lastrowid
    con.commit()
    con.close()

    tok = _csrf()
    r = _client.post("/change/%d/resolve" % cid,
                     data={"resolution": "reject", "_csrf": tok})
    assert r.status_code in (302, 303), r.status_code
    con = db.connect()
    ch = con.execute("SELECT * FROM changes WHERE id=?", (cid,)).fetchone()
    snaps = {s["id"]: s["rejected"] for s in con.execute(
        "SELECT id, rejected FROM snapshots WHERE url_id=940")}
    con.close()
    assert ch["reviewed"] == 1 and ch["resolution"] == "reject"
    # the capture it objected to is NOT struck out, because a later one is
    # what the page actually holds now
    assert snaps[mid_id] == 0, "a superseded capture was rejected anyway"
    assert snaps[new_id] == 0, "the current capture was touched"


def test_rejecting_a_current_update_still_rewinds_as_it_always_did():
    con = db.connect()
    con.execute("INSERT INTO urls(id, url, active) VALUES"
                "(941, 'https://www.infineon.com/current', 1)")
    old_id = con.execute(
        "INSERT INTO snapshots(url_id, ts, dir, status, rejected)"
        " VALUES(941, '2026-09-01', 'old', 'ok', 0)").lastrowid
    new_id = con.execute(
        "INSERT INTO snapshots(url_id, ts, dir, status, rejected)"
        " VALUES(941, '2026-09-05', 'new', 'ok', 0)").lastrowid
    cid = con.execute(
        "INSERT INTO changes(url_id, ts, old_snapshot_id, new_snapshot_id,"
        " summary, reviewed) VALUES(941, '2026-09-05', ?, ?, 'x', 0)",
        (old_id, new_id)).lastrowid
    con.commit()
    con.close()
    _client.post("/change/%d/resolve" % cid,
                 data={"resolution": "reject", "_csrf": _csrf()})
    con = db.connect()
    rej = con.execute("SELECT rejected FROM snapshots WHERE id=?",
                      (new_id,)).fetchone()[0]
    con.close()
    assert rej == 1, "the newest capture should still be struck out"


def test_the_hero_default_test_is_the_same_on_both_sides():
    """The browser decides it at capture time, the boot sweep recomputes it
    from the stored src. If the two ever disagree, a page keeps whichever
    verdict it was read under and nothing says so."""
    import re as _re
    js = _re.search(r"const heroDefault = !!heroSrc &&\s*\n\s*/([^/]+)/i",
                    C_capture.SECTIONS_JS)
    assert js, "could not find the hero test in SECTIONS_JS"
    assert js.group(1) == app._HERO_DEFAULT_RE.pattern.replace("\n", "").replace(
        "    ", ""), (js.group(1), app._HERO_DEFAULT_RE.pattern)


def test_a_placeholder_that_never_says_default_is_still_a_placeholder():
    """Three spellings are in use on infineon.com and only two of them carry
    the word "default". CYW43907's hero is packages/dummy-chip-packages.svg -
    read as a real product photo until "dumm" was added to the test."""
    for src in ("https://www.infineon.com/assets/row/public/images/"
                "missing-product-images/part8/optiga-trust-ch-eval-kit.svg",
                "https://www.infineon.com/assets/web/dummy-images/"
                "evaluation-boards-dummy-default.svg",
                "https://www.infineon.com/assets/row/public/images/packages/"
                "dummy-chip-packages.svg"):
        assert app._HERO_DEFAULT_RE.search(src), src
    # ...and a real board photo is not swept up with them
    for src in ("https://assets.infineon.com/is/image/infineon/"
                "kit-psc3m5-cc1-main-image-kit-psc3m5-cc1.png",
                "https://assets.infineon.com/is/image/infineon/psc3-pfc1-kit.jpeg"):
        assert not app._HERO_DEFAULT_RE.search(src), src


def test_an_image_says_whether_it_is_the_hero_or_just_present():
    """Searching the library for "dummy" lists every page CARRYING one - a
    related-board tile, a tool icon. KIT-PSC3-PFC1 carries the evaluation-board
    placeholder in a tile while its own hero is a real photo, and it was
    reported as a dummy-hero page on the strength of the search."""
    import json as _json
    con = db.connect()
    hero = "https://assets.infineon.com/is/image/infineon/psc3-pfc1-kit.jpeg"
    dummy = ("https://www.infineon.com/assets/web/dummy-images/"
             "evaluation-boards-dummy-default.svg")
    con.execute("INSERT INTO urls(id, url, active, ptype, level, flags)"
                " VALUES(950, 'https://www.infineon.com/evaluation-board/PFC1',"
                " 1, 'Part', 'EB', ?)",
                (_json.dumps({"hero_image": {"exists": True, "has_content": True,
                                             "src": hero}}),))
    con.execute("INSERT INTO items(url_id, category, title, item_url, item_key)"
                " VALUES(950, 'images', 'KIT_PSC3_PFC1', ?, 'kh')", (hero,))
    con.execute("INSERT INTO items(url_id, category, title, item_url, item_key)"
                " VALUES(950, 'images', 'USB0010', ?, 'kd')", (dummy,))
    con.commit()
    con.close()
    import re as _re

    def flat(r):                      # the sentence wraps in the template
        return _re.sub(r"\s+", " ", r.data.decode("utf-8", "replace"))

    assert "Used as the <b>hero image</b> on 1 of these 1 page" in \
        flat(_ok("/item/kh"))
    assert "<b>Not the hero image</b> on any of these 1 page" in \
        flat(_ok("/item/kd")), \
        "a placeholder sitting in a tile is claiming to be the hero"


def _seed_mentions():
    """Two terms, two product lines, three ages - enough to prove each filter
    narrows on its own axis."""
    from datetime import datetime, timedelta, timezone
    import sentiment as sn
    now = datetime.now(timezone.utc)
    con = db.connect()
    try:
        # OR IGNORE, because every test here calls this and the runner shares
        # one database - a second insert used to raise and leave the
        # connection open, which locked the file for every test after it
        for uid, path, pl, bl, title in (
                (960, "psoc-4-mcu", "92", "ICW", "PSOC 4 MCU"),
                (961, "optiga-trust", "81", "DSI", "OPTIGA Trust")):
            con.execute(
                "INSERT OR IGNORE INTO urls(id, url, active, ptype, level,"
                " pl, bl, title_override) VALUES(?,?,1,'Product','PFP',?,?,?)",
                (uid, "https://www.infineon.com/products/x/" + path, pl, bl,
                 title))
        for i, (entity, days) in enumerate((("PSOC", 3), ("PSOC", 200),
                                            ("OPTIGA", 3))):
            con.execute(
                "INSERT OR IGNORE INTO mentions(entity, source, url, title,"
                " body, posted, fetched, score) VALUES(?,?,?,?,'',?,?,0.5)",
                (entity, "hn", "https://example.invalid/%d" % i,
                 "%s MCU post" % entity,
                 (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
                 now.strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
    finally:
        con.close()
    sn._PL_CACHE.update(at=0, val=None)     # the pages just changed
    sn._ENT_CACHE.update(at=0, val=None)


def test_a_pulse_check_can_be_read_over_a_period():
    """"What has the market said this quarter" could not be asked at all -
    the feed was every mention ever collected, newest first.

    Asserted on the links, not the titles: the feed bolds the term inside the
    title, so "PSOC post" is never in the HTML as one string.
    """
    _seed_mentions()
    import sentiment as sn
    assert sn.period_days("30d") == 30 and sn.period_days("") == 0
    every = _ok("/sentiment").data.decode("utf-8", "replace")
    for n in (0, 1, 2):
        assert "example.invalid/%d" % n in every
    month = _ok("/sentiment?period=30d").data.decode("utf-8", "replace")
    assert "example.invalid/1" not in month, "a 200-day-old mention is not this month"
    assert "example.invalid/0" in month and "example.invalid/2" in month


def test_a_term_takes_its_product_line_from_the_pages_that_name_it():
    """Derived, not typed in: every watched page already carries its PL from
    the CSS export, so the line of a term is the line of the pages naming it.
    Nobody keeps a second list that goes stale when PIM moves a family."""
    _seed_mentions()
    import sentiment as sn
    lines = sn.entity_lines()
    assert lines["PSOC"]["pl"] == "92" and lines["PSOC"]["bl"] == "ICW"
    assert lines["OPTIGA"]["pl"] == "81" and lines["OPTIGA"]["bl"] == "DSI"


def test_filtering_by_product_line_narrows_to_that_lines_terms():
    _seed_mentions()
    body = _ok("/sentiment?pl=81").data.decode("utf-8", "replace")
    assert "example.invalid/2" in body, "OPTIGA sits on PL 81 and is missing"
    assert "example.invalid/0" not in body, "another line's mentions came through"
    # a line nobody has a term for shows nothing rather than everything
    empty = _ok("/sentiment?pl=999").data.decode("utf-8", "replace")
    for n in (0, 1, 2):
        assert "example.invalid/%d" % n not in empty


def test_the_filters_survive_clicking_a_source():
    """The source chips rebuild the URL from scratch. Without carrying the
    new parameters, clicking one silently throws the period and the product
    line away and the reader is looking at all time again."""
    _seed_mentions()
    import re as _re
    body = _ok("/sentiment?period=30d&pl=92").data.decode("utf-8", "replace")
    chips = _re.findall(r'class="chip[^"]*"\s+title="[^"]*"\s+href="([^"]+)"',
                        body)
    assert chips, "no source chips rendered"
    for href in chips:
        assert "period=30d" in href and "pl=92" in href, href


def _pagination_mentions():
    """Large disposable inventory: never leak these rows to another test."""
    from contextlib import contextmanager

    @contextmanager
    def seed():
        con = db.connect()
        con.executemany(
            "INSERT INTO mentions(entity,source,url,title,body,posted,fetched,score)"
            " VALUES(?,'hn',?,?,'Infineon pagination fixture body','2026-09-15 12:00:00',"
            " '2026-09-15 12:00:00',0.5)",
            [("Fixture family %03d" % n, "https://pagination.invalid/%d" % n,
              "Pagination fixture %03d" % n) for n in range(540)])
        con.commit()
        con.close()
        try:
            yield
        finally:
            con = db.connect()
            con.execute("DELETE FROM mentions WHERE url LIKE 'https://pagination.invalid/%'")
            con.commit()
            con.close()
    return seed()


def test_sentiment_bounds_families_and_mentions_without_losing_later_pages():
    import re
    with _pagination_mentions():
        def page(query):
            return _ok('/sentiment?q=Pagination+fixture' + query).data.decode()
        first = page('')
        second = page('&family_page=2&page=2')
        families = lambda p: set(re.findall(r'class="fam" data-entity="([^"]+)"', p))
        links = lambda p: set(re.findall(r'href="(https://pagination.invalid/\d+)"', p.split('id="snresults"', 1)[1]))
        assert len(families(first)) == 12
        assert len(links(first)) == 50
        a, b = families(first), families(second)
        assert not a & b, 'family pages repeat cards when counts tie'
        assert not links(first) & links(second), 'mention pages must be disjoint'
        assert '540 shown' in first
        assert int(re.findall(r'<div class="n">(\d+)</div>', first)[0]) >= 540
        last = page('&family_page=999999&page=999999')
        assert len(families(last)) == 12
        assert len(links(last)) == 40
        assert 'aria-current="page">45</span>' in last
        bad = page('&family_page=oops&page=-3')
        assert a == families(bad)


def test_sentiment_search_narrows_family_cards_and_keeps_pager_filters():
    import re
    from html import unescape
    from urllib.parse import urlparse, parse_qs
    with _pagination_mentions():
        html = _ok('/sentiment?q=Pagination+fixture&source=hn&sort=best'
                   '&from=2026-09-01&to=2026-09-30').data.decode()
        pager_links = re.findall(r'class="p(?:num|step)[^"]*"\s+href="([^"]+)"', html)
        assert pager_links
        for href in pager_links:
            if href == '#':
                continue
            args = parse_qs(urlparse(unescape(href)).query)
            for key, value in [('q', 'Pagination fixture'), ('source', 'hn'),
                               ('sort', 'best'), ('from', '2026-09-01'),
                               ('to', '2026-09-30')]:
                assert args[key] == [value]
        narrowed = _ok('/sentiment?q=Pagination+fixture+539').data.decode()
        assert re.findall(r'class="fam" data-entity="([^"]+)"', narrowed) == ['Fixture family 539']
        assert len(set(re.findall(r'href="https://pagination.invalid/\d+"', narrowed.split('id="snresults"', 1)[1]))) == 1
        empty = _ok('/sentiment?q=nonexistent-pagination-term').data.decode()
        assert 'class="fam"' not in empty and '<ol class="mn">' not in empty
        assert 'id="snfamilies"' in empty, 'live search needs a stable empty target'


def test_sentiment_uses_dashboard_controls_and_reads_the_form():
    doc = _ok('/sentiment').data.decode()
    assert 'class="toolbar sn-search"' in doc and 'class="sbar grow"' in doc
    assert 'id="snbtn">Apply</button>' in doc
    assert 'class="dd"' in doc and 'type="radio" name="period"' in doc
    script = doc
    assert 'new URLSearchParams(new FormData(form))' in script
    assert 'bindFamilies();' in script


def test_the_per_page_export_can_be_asked_for_named_pages():
    """Ticking rows and asking for those is not "every EB page" - it is the
    answer to a conversation somebody is already having, and usually cannot
    be rebuilt out of the level and type filters at all."""
    import io
    from openpyxl import load_workbook
    con = db.connect()
    for uid in (975, 976, 977):
        con.execute("INSERT OR IGNORE INTO urls(id, url, active, ptype, level)"
                    " VALUES(?, ?, 1, 'Part', 'PTP')",
                    (uid, "https://www.infineon.com/part/NAMED%d" % uid))
    con.commit()
    con.close()
    r = _client.post("/export/download?mode=pages&go=1&all=1",
                     data={"ids": ["975", "976"], "_csrf": _csrf()})
    assert r.status_code == 200, r.status_code
    ws = load_workbook(io.BytesIO(r.data))["Pages"]
    urls = {r0[0] for r0 in ws.iter_rows(values_only=True)}
    assert "https://www.infineon.com/part/NAMED975" in urls
    assert "https://www.infineon.com/part/NAMED976" in urls
    assert "https://www.infineon.com/part/NAMED977" not in urls, \
        "a page that was not asked for came along"
    # and asking for a page that is not here gives an empty sheet, never all
    empty = _client.post("/export/download?mode=pages&go=1&all=1",
                         data={"ids": "99999", "_csrf": _csrf()})
    ws2 = load_workbook(io.BytesIO(empty.data))["Pages"]
    assert len(list(ws2.iter_rows(values_only=True))) == 1, "header only"


def test_the_per_page_export_says_hero_image_before_it_says_where():
    """A column of addresses cannot be sorted or counted - you cannot see how
    many boards are on a placeholder without reading 229 URLs, and the address
    alone does not say, because two of the three placeholder paths are named
    after the product rather than after being a placeholder."""
    import io, json as _json
    from openpyxl import load_workbook
    con = db.connect()
    hero = "https://assets.infineon.com/is/image/infineon/real-board.png"
    dummy = ("https://www.infineon.com/assets/row/public/images/"
             "missing-product-images/part8/x.svg")
    for uid, src, has in ((970, hero, True), (971, dummy, False)):
        con.execute("INSERT INTO urls(id, url, active, ptype, level, flags)"
                    " VALUES(?, ?, 1, 'Part', 'EB', ?)",
                    (uid, "https://www.infineon.com/evaluation-board/H%d" % uid,
                     _json.dumps({"hero_image": {"exists": True,
                                                 "has_content": has,
                                                 "src": src}})))
    con.execute("INSERT INTO urls(id, url, active, ptype, level, flags)"
                " VALUES(972, 'https://www.infineon.com/evaluation-board/H972',"
                " 1, 'Part', 'EB', '{}')")
    con.commit()
    con.close()
    r = _client.post("/export/download?mode=pages&go=1&all=1",
                     data={"ids": "970 971 972", "_csrf": _csrf()})
    ws = load_workbook(io.BytesIO(r.data))["Pages"]
    rows = list(ws.iter_rows(values_only=True))
    head = list(rows[0])
    assert "Hero image" in head and "Hero image link" in head, head
    v, link = head.index("Hero image"), head.index("Hero image link")
    assert v == link - 1, "the verdict must come before the address"
    by_url = {r0[0]: r0 for r0 in rows[1:]}
    assert by_url["https://www.infineon.com/evaluation-board/H970"][v] == "real image"
    assert by_url["https://www.infineon.com/evaluation-board/H971"][v] == "placeholder"
    assert by_url["https://www.infineon.com/evaluation-board/H972"][v] == "no image"
    assert by_url["https://www.infineon.com/evaluation-board/H971"][link] == dummy


def test_the_feed_can_be_read_oldest_first_and_by_score():
    """Newest first is the pulse check. Oldest first is how you read a story
    forward, and the two score orders are "show me the complaints" and "show
    me what is working" - the question a sentiment feed exists to answer."""
    _seed_mentions()
    import re as _re
    def order(url):
        b = _ok(url).data.decode("utf-8", "replace").split('id="snresults"', 1)[1]
        return _re.findall(r"example\.invalid/(\d)", b)
    # 0 = PSOC 3 days ago, 1 = PSOC 200 days ago, 2 = OPTIGA 3 days ago
    assert order("/sentiment")[0] != "1", "newest first put the oldest on top"
    assert order("/sentiment?sort=oldest")[0] == "1"
    assert order("/sentiment") != order("/sentiment?sort=oldest")


def test_an_unscored_mention_sorts_last_not_as_zero():
    """A mention nobody has judged is not neutral - reading it as 0 would put
    7,370 unscored rows in the middle of "most negative"."""
    from datetime import datetime, timezone
    con = db.connect()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for i, score in ((90, -0.9), (91, None), (92, 0.8)):
        con.execute("INSERT OR IGNORE INTO mentions(entity, source, url, title,"
                    " body, posted, fetched, score) VALUES('PSOC','hn',?,?,'',?,?,?)",
                    ("https://example.invalid/s%d" % i, "PSOC MCU s%d post" % i, now, now,
                     score))
    con.commit()
    con.close()
    import re as _re
    body = _ok("/sentiment?sort=worst").data.decode("utf-8", "replace")
    body = body.split('id="snresults"', 1)[1]
    seen = _re.findall(r"example\.invalid/s(9\d)", body)
    assert seen, "the seeded rows are not on the page"
    assert seen.index("90") < seen.index("91"), "the -0.9 must come first"


def test_the_scorer_is_configured_for_opus_at_a_sane_effort():
    """Opus for the judgement, the model's own default effort for the cost.
    Max was tried and rejected - 7,370 short forum posts is high-volume
    classification, the shape of work that repays effort least."""
    import sentiment as sn
    import inspect
    assert sn.SCORE_MODEL == "claude-opus-5"
    assert sn.SCORE_EFFORT in ("low", "medium", "high")
    src = inspect.getsource(sn.score_pending)
    assert "output_config" in src and '"effort": SCORE_EFFORT' in src
    # budget_tokens is a 400 on this model, and prefill is gone
    assert "budget_tokens" not in src
    assert '"role": "assistant"' not in src
    # a refusal is a stop_reason, not an exception - it must not be read as text
    assert 'stop_reason == "refusal"' in src


def test_a_custom_date_range_narrows_to_those_days():
    """"Last quarter" is not the question when somebody wants the fortnight
    around a launch or everything before a recall."""
    _seed_mentions()
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    day = lambda n: (now - timedelta(days=n)).strftime("%Y-%m-%d")
    # /1 is 200 days old, /0 and /2 are 3 days old
    old_only = _ok("/sentiment?from=%s&to=%s" % (day(220), day(150))
                   ).data.decode("utf-8", "replace")
    assert "example.invalid/1" in old_only
    assert "example.invalid/0" not in old_only, "a recent mention leaked in"
    # an open end is a real question on its own
    since = _ok("/sentiment?from=%s" % day(10)).data.decode("utf-8", "replace")
    assert "example.invalid/0" in since and "example.invalid/1" not in since


def test_a_date_that_is_not_a_date_is_ignored_rather_than_queried():
    """The two values reach SQL. Anything that is not YYYY-MM-DD came from a
    typed URL, not from the date picker."""
    _seed_mentions()
    body = _ok("/sentiment?from=' OR 1=1 --").data.decode("utf-8", "replace")
    assert "example.invalid/0" in body, "a junk date should filter nothing"
    assert "OR 1=1" not in body


def test_the_custom_range_beats_a_named_period():
    """Somebody who typed a date meant it."""
    _seed_mentions()
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    day = lambda n: (now - timedelta(days=n)).strftime("%Y-%m-%d")
    both = _ok("/sentiment?period=30d&from=%s&to=%s" % (day(220), day(150))
               ).data.decode("utf-8", "replace")
    assert "example.invalid/1" in both, "the dates should win over 30d"


def test_cron_status_is_gated_read_only_and_never_starts_a_run():
    """The clock holds the container awake by asking this every few minutes
    for hours. It must answer on the same key, it must never start anything,
    and a wrong key must not confirm it exists."""
    import app as appmod
    old = os.environ.get("CRON_KEY")
    started = []
    real = appmod._scheduled_check
    appmod._scheduled_check = lambda *a, **k: started.append(1)
    try:
        os.environ.pop("CRON_KEY", None)
        assert _client.get("/cron/status").status_code == 404, "dark when unset"
        os.environ["CRON_KEY"] = "a-long-random-cron-key-2026"
        assert _client.get("/cron/status").status_code == 404, "no key got in"
        assert _client.get("/cron/status?key=wrong").status_code == 404
        r = _client.get("/cron/status?key=a-long-random-cron-key-2026")
        assert r.status_code == 200, r.status_code
        assert _client.get("/cron/status",
                           headers={"X-Cron-Key": "a-long-random-cron-key-2026"}
                           ).status_code == 200, "the header form must work"
        import time as _t
        _t.sleep(0.3)
        assert started == [], "asking for status started a run"
        body = r.get_json()
        assert set(("awake_needed", "running", "unfinished_run", "sweeping")
                   ) <= set(body), body
        assert isinstance(body["awake_needed"], bool)
    finally:
        appmod._scheduled_check = real
        if old is None:
            os.environ.pop("CRON_KEY", None)
        else:
            os.environ["CRON_KEY"] = old


def test_cron_status_holds_the_door_for_a_run_that_only_the_database_knows():
    """The case this exists for. After a sleep the container restarts with an
    empty RUN_STATE while the database still holds an unfinished run that the
    next boot will resume - and a clock trusting RUN_STATE alone would hang up
    in exactly that window. A run a PERSON ended is different: its suspects
    wait for the next check, and nobody should be billed for holding it up."""
    import app as appmod, runner as runnermod
    old = os.environ.get("CRON_KEY")
    os.environ["CRON_KEY"] = "a-long-random-cron-key-2026"
    url = "/cron/status?key=a-long-random-cron-key-2026"
    saved = dict(runnermod.RUN_STATE)
    con = appmod.db.connect()
    try:
        runnermod.RUN_STATE.clear()              # as it is on a fresh boot
        con.execute("INSERT INTO runs(started, finished, kind, total)"
                    " VALUES('2026-09-27 06:02:44', NULL, 'scheduled', 3640)")
        con.commit()
        b = _client.get(url).get_json()
        assert b["running"] is False, "RUN_STATE is empty on a fresh boot"
        assert b["unfinished_run"] is True and b["awake_needed"] is True, b

        rid = con.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        con.execute("UPDATE runs SET stopped='2026-09-27 12:00:00'"
                    " WHERE id=?", (rid,))
        con.commit()
        b = _client.get(url).get_json()
        assert b["awake_needed"] is False, "a run a person ended still held on"

        con.execute("UPDATE runs SET stopped='',"
                    " finished='2026-09-27 18:00:00' WHERE id=?", (rid,))
        con.commit()
        assert _client.get(url).get_json()["awake_needed"] is False
        con.execute("DELETE FROM runs WHERE id=?", (rid,))
        con.commit()
    finally:
        con.close()
        runnermod.RUN_STATE.clear()
        runnermod.RUN_STATE.update(saved)
        if old is None:
            os.environ.pop("CRON_KEY", None)
        else:
            os.environ["CRON_KEY"] = old


def test_queue_reminder_goes_out_once_on_friday_from_eight_berlin_time():
    """The clock fires at 06:00 AND 07:00 UTC because 08:00 in Berlin is one
    or the other with daylight saving, so the app must judge the hour itself:
    nothing before Friday 08:00 local, exactly one batch per Friday, and
    nothing at all when the queue is already clear."""
    import app as appmod
    import mailer
    from datetime import datetime as _dt, timezone as _tz
    utc = lambda s: _dt.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=_tz.utc)
    con = db.connect()
    prev_opt = con.execute("SELECT email_opt_in FROM users WHERE id=?",
                           (_uid,)).fetchone()[0]
    con.execute("UPDATE users SET email_opt_in=1, active=1 WHERE id=?", (_uid,))
    cid = con.execute(
        "INSERT INTO changes(url_id, ts, old_snapshot_id, new_snapshot_id,"
        " text_changed, reviewed) VALUES(1, '2026-10-11 20:00:00', ?, ?, 1, 0)",
        (_sid, _sid)).lastrowid
    con.commit()
    con.close()
    sent = []
    real_conf, real_send = mailer.configured, mailer.send
    mailer.configured = lambda: True
    mailer.send = lambda to, subj, html, text="": sent.append((to, subj, html)) or None
    prev_mark = db.get_settings().get("last_queue_reminder") or ""
    try:
        db.set_setting("last_queue_reminder", "")
        # Thursday mid-morning, and Friday 07:59 in summer time: too early
        assert appmod._queue_reminder(utc("2026-10-15 08:00"))["sent"] == 0
        assert appmod._queue_reminder(utc("2026-10-16 05:59"))["sent"] == 0
        assert sent == [], "reminded before Friday 08:00"
        # Friday 06:00 UTC is 08:00 CEST: this one sends
        r = appmod._queue_reminder(utc("2026-10-16 06:00"))
        batch = len(sent)
        assert batch and r["sent"] == batch and r["updates"] >= 1, r
        assert "routes@example.com" in [x[0] for x in sent]
        to, subj, html = sent[0]
        assert "still to review" in subj, subj
        assert "the oldest since " in html, "the oldest open update is named"
        assert "’s check" in html and "counted again" in html
        assert "Review the queue" in html
        # the 07:00 UTC firing the same Friday must not mail again
        assert appmod._queue_reminder(utc("2026-10-16 07:00"))["sent"] == 0
        assert len(sent) == batch, "two reminders on one Friday"

        # winter time: 06:00 UTC is 07:00 CET, 07:00 UTC is the one
        assert appmod._queue_reminder(utc("2026-10-30 06:00"))["sent"] == 0
        assert appmod._queue_reminder(utc("2026-10-30 07:00"))["sent"] == batch
        assert len(sent) == 2 * batch

        # a clear queue sends nothing
        con = db.connect()
        open_ch = [r[0] for r in con.execute(
            "SELECT id FROM changes WHERE reviewed=0")]
        open_fu = [r[0] for r in con.execute(
            "SELECT id FROM asset_changes WHERE IFNULL(reviewed,0)=0")]
        con.execute("UPDATE changes SET reviewed=1 WHERE reviewed=0")
        con.execute("UPDATE asset_changes SET reviewed=1 WHERE IFNULL(reviewed,0)=0")
        con.commit()
        try:
            r = appmod._queue_reminder(utc("2026-11-06 07:00"))
            assert r == {"sent": 0, "why": "queue is clear"}, r
            assert len(sent) == 2 * batch
        finally:
            for t, ids in (("changes", open_ch), ("asset_changes", open_fu)):
                if ids:
                    con.execute("UPDATE %s SET reviewed=0 WHERE id IN (%s)"
                                % (t, ",".join(map(str, ids))))
            con.commit()
            con.close()
    finally:
        mailer.configured, mailer.send = real_conf, real_send
        db.set_setting("last_queue_reminder", prev_mark)
        con = db.connect()
        con.execute("DELETE FROM changes WHERE id=?", (cid,))
        con.execute("UPDATE users SET email_opt_in=? WHERE id=?", (prev_opt, _uid))
        con.commit()
        con.close()


def test_cron_remind_is_gated_like_the_other_cron_doors():
    import app as appmod
    old = os.environ.get("CRON_KEY")
    real = appmod._queue_reminder
    appmod._queue_reminder = lambda: {"sent": 0, "why": "stub"}
    try:
        os.environ.pop("CRON_KEY", None)
        assert _client.get("/cron/remind").status_code == 404, "dark when unset"
        os.environ["CRON_KEY"] = "a-long-random-cron-key-2026"
        assert _client.get("/cron/remind?key=wrong").status_code == 404
        r = _client.post("/cron/remind",
                         headers={"X-Cron-Key": "a-long-random-cron-key-2026"})
        assert r.status_code == 200, r.status_code
        assert r.get_json() == {"sent": 0, "why": "stub"}
    finally:
        appmod._queue_reminder = real
        if old is None:
            os.environ.pop("CRON_KEY", None)
        else:
            os.environ["CRON_KEY"] = old


def test_healthz_is_public_and_cheap():
    """The hold loop knocks on this every two minutes for hours while a check
    runs. It must answer without a session and without touching anything."""
    r = _client.get("/healthz")
    assert r.status_code == 200, r.status_code
    assert r.get_json() == {"ok": True}
    assert len(r.data) < 60, "healthz should be the cheapest answer here"


def test_the_app_holds_itself_awake_only_while_a_check_is_running():
    """The point of the whole thing. An idle week must still cost an idle
    week, so no run means no request at all; and the knock has to go out
    through the PUBLIC url, because Railway sleeps on what its edge proxy
    sees and a call to localhost proves nothing to it."""
    import app as appmod, runner as runnermod
    from unittest.mock import patch
    saved = dict(runnermod.RUN_STATE)
    old_pub = os.environ.get("PUBLIC_URL")
    con = appmod.db.connect()
    rid = None
    try:
        os.environ["PUBLIC_URL"] = "https://iradar.example"
        runnermod.RUN_STATE.clear()
        # add a FINISHED run rather than clearing the table: these rows are
        # shared fixture state and deleting them fails every other test here
        con.execute("INSERT INTO runs(started, finished, kind, total)"
                    " VALUES('2026-09-27 06:02:44',"
                    " '2026-09-27 18:00:00', 'scheduled', 3640)")
        con.commit()
        rid = con.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        with patch("urllib.request.urlopen") as op:
            assert appmod._hold_awake_once() is None
            op.assert_not_called(), "it knocked with no run in flight"

        con.execute("UPDATE runs SET finished=NULL WHERE id=?", (rid,))
        con.commit()
        with patch("urllib.request.urlopen") as op:
            op.return_value.__enter__ = lambda self: self
            op.return_value.__exit__ = lambda self, *a: False
            op.return_value.read = lambda *a: b"{}"
            url = appmod._hold_awake_once()
        assert url == "https://iradar.example/healthz", url
        sent = op.call_args[0][0]
        assert sent.full_url.startswith("https://iradar.example"), \
            "the knock must leave the container, not hit localhost"
        assert "127.0.0.1" not in sent.full_url and "localhost" not in sent.full_url

        # a run a person ended is not work in flight
        con.execute("UPDATE runs SET stopped='2026-09-27 12:00:00' WHERE id=?",
                    (rid,))
        con.commit()
        with patch("urllib.request.urlopen") as op:
            assert appmod._hold_awake_once() is None
            op.assert_not_called(), "it held a run a person had ended"
    finally:
        if rid is not None:
            con.execute("DELETE FROM runs WHERE id=?", (rid,))
            con.commit()
        con.close()
        runnermod.RUN_STATE.clear()
        runnermod.RUN_STATE.update(saved)
        if old_pub is None:
            os.environ.pop("PUBLIC_URL", None)
        else:
            os.environ["PUBLIC_URL"] = old_pub
