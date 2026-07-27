"""Accounts, roles and sign-in.

This is the security boundary of a dashboard that is going on a public domain,
so each test pins one rule that would be quietly expensive to get wrong:
signed-out people see nothing, an editor cannot reach an admin control, the
last admin cannot be removed, and the shared starting password cannot survive
first sign-in.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.pop("DASH_USER", None)
os.environ.pop("DASH_PASS", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import db                                          # noqa: E402
from run_tests import Skip                         # noqa: E402

_D = Path(tempfile.mkdtemp(prefix="ifxauth_"))
db.DATA_DIR = _D
db.SNAP_DIR = _D / "snapshots"
db.CHANGE_DIR = _D / "changes"
db.DB_PATH = _D / "watch.db"
db.init_db()

try:
    import auth                                    # noqa: E402
    import app as appmod                           # noqa: E402
except Exception as e:                             # missing dep -> skip
    raise Skip(f"could not import the app: {e}")

ADMIN = "admin@example.com"
EDITOR = "editor@example.com"
NEWPW = "a-much-longer-secret-2026"


def _reset():
    """One known admin + one known editor, both past the forced change."""
    auth._FAILS.clear()
    con = db.connect()
    con.execute("DELETE FROM users")
    con.commit()
    con.close()
    a, _ = auth.create_user(ADMIN, "Admin", "admin")
    e, _ = auth.create_user(EDITOR, "Editor", "editor")
    auth.set_password(a, NEWPW, must_change=False)
    auth.set_password(e, NEWPW, must_change=False)
    return a, e


def _client():
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def _token(c, path="/login"):
    """The CSRF token the server injected into the page's forms."""
    import re
    html = c.get(path).data.decode("utf-8", "replace")
    m = re.search(r'name="_csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def _signin(c, email, password=NEWPW):
    # loading the form first is what a browser does, and it is where the
    # token comes from
    return c.post("/login", data={"email": email, "password": password,
                                  "_csrf": _token(c)})


# --- signed out -------------------------------------------------------------

def test_signed_out_is_redirected_not_served():
    _reset()
    c = _client()
    r = c.get("/")
    assert r.status_code == 302, f"dashboard served to a stranger ({r.status_code})"
    assert "/login" in r.headers.get("Location", "")


def test_signed_out_cannot_post():
    _reset()
    c = _client()
    r = c.post("/urls/add", data={"urls": "https://example.com"})
    assert r.status_code in (302, 401, 403), "a stranger could POST"


def test_login_page_is_public():
    _reset()
    assert _client().get("/login").status_code == 200


# --- signing in -------------------------------------------------------------

def test_correct_password_signs_in():
    _reset()
    c = _client()
    r = _signin(c, ADMIN)
    assert r.status_code == 302, "correct password did not sign in"
    assert c.get("/").status_code == 200


def test_wrong_password_refused():
    _reset()
    c = _client()
    r = _signin(c, ADMIN, "not-the-password")
    assert r.status_code == 401
    assert c.get("/").status_code == 302, "refused login still granted access"


def test_unknown_email_says_the_same_as_wrong_password():
    # different wording would confirm which addresses are real accounts
    _reset()
    _, err_unknown = auth.check_login("nobody@example.com", "x")
    auth._FAILS.clear()
    _, err_wrong = auth.check_login(ADMIN, "wrong")
    assert err_unknown == err_wrong, "login reveals whether an account exists"


def test_password_is_not_stored_in_the_clear():
    _reset()
    con = db.connect()
    row = con.execute("SELECT pw_hash FROM users WHERE email=?", (ADMIN,)).fetchone()
    con.close()
    assert NEWPW not in row["pw_hash"], "password stored in readable form"
    assert len(row["pw_hash"]) > 40


def test_guessing_is_throttled():
    _reset()
    for _ in range(auth.MAX_FAILS):
        auth.check_login(ADMIN, "wrong")
    u, err = auth.check_login(ADMIN, NEWPW)   # right password, still locked
    assert u is None and "try again" in (err or ""), "no throttle on guessing"
    auth._FAILS.clear()


def test_a_served_lock_gives_a_fresh_set_of_tries():
    # Once the wait has been served the count must reset. Leaving it at the
    # limit meant the minute bought exactly ONE attempt - a second typo put the
    # account back into another full minute, forever.
    _reset()
    for _ in range(auth.MAX_FAILS):
        auth.check_login(ADMIN, "wrong")
    assert auth.locked_for(ADMIN) > 0
    # pretend the lock expired
    fails, _ = auth._FAILS[ADMIN]
    auth._FAILS[ADMIN] = (fails, time.time() - 1)
    assert auth.locked_for(ADMIN) == 0, "lock did not expire"
    auth.check_login(ADMIN, "wrong-again")          # one more typo
    assert auth.locked_for(ADMIN) == 0, \
        "a single typo after the wait re-locked the account for another minute"
    u, err = auth.check_login(ADMIN, NEWPW)
    assert u is not None, f"correct password refused after a served lock: {err}"
    auth._FAILS.clear()


# --- roles ------------------------------------------------------------------

def test_editor_cannot_reach_admin_pages():
    _reset()
    c = _client()
    _signin(c, EDITOR)
    for path in ("/settings", "/users", "/settings/components"):
        assert c.get(path).status_code == 403, f"editor reached {path}"


def test_editor_cannot_run_admin_actions():
    # with a VALID token, so this proves the ROLE refuses them - not merely
    # that the CSRF check fired first
    _reset()
    c = _client()
    _signin(c, EDITOR)
    tok = _token(c, "/")
    assert tok, "no token on the dashboard"
    for path in ("/urls/add", "/run-now", "/users/add", "/stop"):
        r = c.post(path, data={"_csrf": tok})
        assert r.status_code == 403, \
            f"editor could POST {path} (got {r.status_code})"


def test_editor_can_do_review_work():
    # the whole point of the role: an editor must be able to review updates
    _reset()
    c = _client()
    _signin(c, EDITOR)
    assert c.get("/").status_code == 200
    r = c.post("/change/999999/review", data={"action": "accept"})
    assert r.status_code != 403, "editor blocked from reviewing updates"


def test_admin_can_reach_admin_pages():
    _reset()
    c = _client()
    _signin(c, ADMIN)
    assert c.get("/users").status_code == 200
    assert c.get("/settings").status_code == 200


def test_unknown_post_route_defaults_to_admin_only():
    # default-deny: a new POST endpoint must NOT be editor-writable by accident
    for ep in appmod.EDITOR_MAY_POST:
        assert ep in {r.endpoint for r in appmod.app.url_map.iter_rules()}, \
            f"EDITOR_MAY_POST names {ep}, which is not a route any more"


# --- account safety rails ---------------------------------------------------

def test_last_admin_cannot_be_demoted_or_removed():
    _reset()
    con = db.connect()
    con.execute("DELETE FROM users WHERE email=?", (EDITOR,))
    con.commit()
    con.close()
    only = auth.by_email(ADMIN)
    assert auth.set_role(only["id"], "editor") is not None, "demoted the last admin"
    assert auth.set_active(only["id"], False) is not None, "switched off the last admin"
    assert auth.delete_user(only["id"]) is not None, "deleted the last admin"


def test_switched_off_account_loses_access_immediately():
    _reset()
    c = _client()
    _signin(c, EDITOR)
    assert c.get("/").status_code == 200
    auth.set_active(auth.by_email(EDITOR)["id"], False)
    assert c.get("/").status_code == 302, "a switched-off account kept its session"


# --- the shared starting password ------------------------------------------

def test_seeded_account_must_change_password_first():
    _reset()
    uid, _ = auth.create_user("fresh@example.com", "Fresh", "editor")
    c = _client()
    r = _signin(c, "fresh@example.com", auth.SEED_PASSWORD)
    assert r.status_code == 302
    assert "/account/password" in r.headers.get("Location", "")
    # and every other page bounces back until it is changed
    r2 = c.get("/")
    assert "/account/password" in r2.headers.get("Location", ""), \
        "seeded account could browse without setting a password"


def test_cannot_keep_the_shared_starting_password():
    _reset()
    auth.create_user("fresh2@example.com", "Fresh", "editor")
    c = _client()
    _signin(c, "fresh2@example.com", auth.SEED_PASSWORD)
    r = c.post("/account/password", data={"current": auth.SEED_PASSWORD,
                                          "new": auth.SEED_PASSWORD,
                                          "again": auth.SEED_PASSWORD})
    assert r.status_code == 400, "the starting password was accepted as the new one"


def test_password_change_requires_the_current_one():
    _reset()
    c = _client()
    _signin(c, EDITOR)
    r = c.post("/account/password", data={"current": "wrong",
                                          "new": "brand-new-password-9",
                                          "again": "brand-new-password-9"})
    assert r.status_code == 400, "password changed without proving the old one"


def test_short_password_refused():
    assert auth.password_problem("short") is not None
    assert auth.password_problem("a-long-enough-one") is None


# --- open redirect ----------------------------------------------------------

def test_login_will_not_bounce_to_another_site():
    _reset()
    c = _client()
    r = c.post("/login", data={"email": ADMIN, "password": NEWPW,
                               "next": "https://evil.example/steal"})
    dest = r.headers.get("Location", "")
    assert "evil.example" not in dest, f"login redirected off-site to {dest}"


# --- CSRF -------------------------------------------------------------------

def _csrf_of(c):
    """Read the token the server just issued to this client."""
    import re
    html = c.get("/login").data.decode("utf-8", "replace")
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    if not m:
        m = re.search(r'name="_csrf" value="([^"]+)"', html)
    return m.group(1) if m else None


def test_forms_carry_a_csrf_token():
    _reset()
    c = _client()
    _signin(c, ADMIN)
    html = c.get("/users").data.decode("utf-8", "replace")
    assert 'name="_csrf"' in html, "no CSRF field was injected into the forms"
    assert 'name="csrf-token"' in html, "no CSRF meta tag for fetch() calls"


def test_post_without_a_token_is_refused():
    _reset()
    c = _client()
    _signin(c, ADMIN)
    # a signed-in admin, right endpoint, but no token -> must not go through
    r = c.post("/users/add", data={"email": "sneak@example.com",
                                   "role": "admin"})
    assert r.status_code == 400, f"POST without a CSRF token returned {r.status_code}"
    assert auth.by_email("sneak@example.com") is None, \
        "the account was created anyway"


def test_post_with_a_wrong_token_is_refused():
    _reset()
    c = _client()
    _signin(c, ADMIN)
    r = c.post("/users/add", data={"email": "sneak2@example.com",
                                   "role": "admin", "_csrf": "not-the-token"})
    assert r.status_code == 400
    assert auth.by_email("sneak2@example.com") is None


def test_post_with_the_right_token_goes_through():
    _reset()
    c = _client()
    _signin(c, ADMIN)
    import re
    html = c.get("/users").data.decode("utf-8", "replace")
    tok = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    r = c.post("/users/add", data={"email": "proper@example.com",
                                   "role": "editor", "_csrf": tok})
    assert r.status_code in (302, 200), f"legit POST refused ({r.status_code})"
    assert auth.by_email("proper@example.com") is not None, \
        "a correctly-tokened POST did not take effect"


def test_the_header_form_of_the_token_works_too():
    # the JS fetch() wrapper sends it as a header, not a form field
    _reset()
    c = _client()
    _signin(c, ADMIN)
    import re
    html = c.get("/").data.decode("utf-8", "replace")
    tok = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    r = c.post("/pause", headers={"X-CSRF-Token": tok})
    assert r.status_code != 400, "the header form of the token was refused"


# --- session hardening ------------------------------------------------------

def test_changing_a_password_ends_other_sessions():
    _reset()
    other = _client()          # a second browser, signed in as the same person
    _signin(other, EDITOR)
    assert other.get("/").status_code == 200

    auth.set_password(auth.by_email(EDITOR)["id"], "a-brand-new-secret-2026")
    assert other.get("/").status_code == 302, \
        "an old session survived the password change - a stolen cookie would too"


def test_security_headers_are_set():
    _reset()
    c = _client()
    r = c.get("/login")
    assert r.headers.get("X-Frame-Options") == "DENY", "page can be framed"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("Referrer-Policy")


def test_one_source_guessing_many_accounts_is_stopped():
    # per-account limits never trip when the attacker sprays one password
    # across many addresses - no single account reaches five
    _reset()
    auth._IP_FAILS.clear()
    for i in range(auth.MAX_IP_FAILS + 1):
        auth.check_login(f"nobody{i}@example.com", "Summer2026!", ip="9.9.9.9")
    assert auth.source_blocked("9.9.9.9"), "password spraying was not throttled"
    u, err = auth.check_login(ADMIN, NEWPW, ip="9.9.9.9")
    assert u is None and "from here" in (err or "")
    # a different source is unaffected
    assert not auth.source_blocked("1.1.1.1")
    auth._IP_FAILS.clear()
    auth._FAILS.clear()


def test_common_passwords_are_refused():
    for bad in ("Infineon@2026", "password123", "iradar2026", "aaaaaaaaaaaa"):
        assert auth.password_problem(bad) is not None, f"accepted {bad!r}"
    assert auth.password_problem("purple-canyon-47-jetty") is None


def test_a_browser_that_kept_no_cookie_can_still_sign_in():
    # The real lockout: signing in needs a cookie the browser accepted BEFORE
    # anyone signed in. A browser holding a cookie this app can no longer
    # verify - one signed with an older key - kept failing with no way back.
    # Six attempts in a row in the field.
    _reset()
    c = _client()
    r = c.post("/login", data={"email": ADMIN, "password": NEWPW})  # no token
    assert r.status_code == 302, \
        "a browser with no usable session cookie cannot sign in at all"
    assert c.get("/").status_code == 200


def test_a_bad_token_is_still_refused_when_one_exists():
    # relaxing the no-cookie case must not relax the real check
    _reset()
    c = _client()
    c.get("/login")                       # now the session HAS a token
    r = c.post("/login", data={"email": ADMIN, "password": NEWPW,
                               "_csrf": "wrong"})
    assert r.status_code == 200 and "sign in again" in r.data.decode(
        "utf-8", "replace").lower(), "a wrong token was accepted"


def test_a_valid_session_is_not_thrown_away_by_visiting_login():
    # clearing on arrival must not sign out someone who followed a bookmark
    _reset()
    c = _client()
    _signin(c, ADMIN)
    r = c.get("/login")
    assert r.status_code == 302, "signed-in user was shown the sign-in form"
    assert c.get("/").status_code == 200, "visiting /login signed them out"


def test_an_unusable_session_is_cleared_at_the_login_page():
    # a cookie this app can no longer verify must not shadow the fresh one
    _reset()
    c = _client()
    with c.session_transaction() as s:
        s["uid"] = 999999                 # points at nobody
        s["_csrf"] = "left-over-token"
    c.get("/login")                       # arriving replaces it
    with c.session_transaction() as s:
        assert s.get("uid") is None, "a dead session survived the login page"
        assert s.get("_csrf") != "left-over-token", "stale token was kept"
    r = _signin(c, ADMIN)                 # and signing in works straight after
    assert r.status_code == 302


def test_a_stale_login_form_says_so_instead_of_failing_silently():
    # A login page left open across a restart carries a token the session no
    # longer knows. Bouncing it back to /login was indistinguishable from a
    # wrong password: the form just reappeared, blank, with nothing to act on.
    _reset()
    c = _client()
    c.get("/login")                      # open the form, get a session
    r = c.post("/login", data={"email": ADMIN, "password": NEWPW,
                               "_csrf": "a-token-from-an-older-tab"})
    body = r.data.decode("utf-8", "replace")
    assert r.status_code == 200
    assert "sign in again" in body.lower(), \
        "a stale login form gave the user nothing to act on"
    assert 'name="_csrf"' in body, "the retry form has no fresh token"
    # and the credentials must NOT have been accepted on that request
    assert c.get("/").status_code in (302,), "stale-token login signed them in"


def test_the_retry_after_a_stale_form_works():
    _reset()
    c = _client()
    c.get("/login")
    c.post("/login", data={"email": ADMIN, "password": NEWPW, "_csrf": "old"})
    r = _signin(c, ADMIN)                # retry, as a person would
    assert r.status_code == 302, "could not sign in after a stale form"
    assert c.get("/").status_code == 200


def test_seeding_does_not_resurrect_a_deleted_account():
    # seeding used to re-create a fixed address on every start, so removing
    # that person quietly undid itself at the next restart
    _reset()
    con = db.connect()
    con.execute("DELETE FROM users WHERE email=?", (EDITOR,))
    con.commit()
    con.close()
    before = {u["email"] for u in auth.list_users()}
    appmod._seed_accounts()
    after = {u["email"] for u in auth.list_users()}
    assert after == before, f"seeding added accounts back: {after - before}"


def test_session_key_is_not_the_old_constant():
    assert appmod.app.secret_key != "infineonwatch-local-only", \
        "session cookies still signed with the constant from the source"
    assert len(appmod.app.secret_key) >= 32
