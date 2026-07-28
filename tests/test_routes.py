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
sys.path.insert(0, str(ROOT))
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
    for i, (pages, secs) in enumerate(specs):
        start = 1_700_000_000 + i * 1_000_000
        fmt = lambda t: __import__("datetime").datetime.utcfromtimestamp(t) \
            .strftime("%Y-%m-%d %H:%M:%S")
        con.execute("INSERT INTO runs(started, finished, kind, total)"
                    " VALUES(?,?,?,?)", (fmt(start), fmt(start + secs),
                                         "full", pages))
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
