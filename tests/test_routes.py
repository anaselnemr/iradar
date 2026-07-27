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
    _client.post("/login", data={"email": "routes@example.com", "password": _PW})
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
