"""Accounts, roles and sign-in.

This is the security boundary of a dashboard that is going on a public domain,
so each test pins one rule that would be quietly expensive to get wrong:
signed-out people see nothing, an editor cannot reach an admin control, the
last admin cannot be removed, and the shared starting password cannot survive
first sign-in.
"""
import os
import re
import sys
import tempfile
import time
from pathlib import Path

os.environ.pop("DASH_USER", None)
os.environ.pop("DASH_PASS", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "iradar"))
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


def test_any_password_the_person_picks_is_accepted():
    # No length minimum, no banned words, no character-variety rule. What
    # protects the account is hashing, the throttle and HTTPS - none of which
    # care what the password looks like - and arguing with someone about their
    # choice while they are trying to get in only ever cost time.
    _reset()
    c = _client()
    _signin(c, EDITOR)
    for pw in ("abc", "1234", auth.SEED_PASSWORD, "aaaaaaaa", "p"):
        cur = auth.by_email(EDITOR)
        r = c.post("/account/password",
                   data={"current": NEWPW if pw == "abc" else prev,
                         "new": pw, "again": pw, "_csrf": _token(c, "/")})
        assert r.status_code in (302, 200), f"refused the password {pw!r}"
        assert auth.check_password_hash(auth.by_email(EDITOR)["pw_hash"], pw), \
            f"{pw!r} was not actually saved"
        prev = pw
    # and a blank one is still not a password
    assert auth.password_problem("") is not None


def test_password_change_requires_the_current_one():
    _reset()
    c = _client()
    _signin(c, EDITOR)
    r = c.post("/account/password", data={"current": "wrong",
                                          "new": "brand-new-password-9",
                                          "again": "brand-new-password-9"})
    assert r.status_code == 400, "password changed without proving the old one"


def test_only_a_blank_password_is_refused():
    assert auth.password_problem("") is not None
    assert auth.password_problem(None) is not None
    for pw in ("x", "short", "password", "Infineon@2026", "aaaa"):
        assert auth.password_problem(pw) is None, f"still arguing about {pw!r}"


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


def test_sign_in_is_never_blocked_by_a_cookie_problem():
    # Sign-in is CSRF-exempt on purpose: it is the one request that must work
    # before a usable session exists, so a cookie problem there is a locked
    # door with nothing behind it. It locked this account out four times.
    _reset()
    for data in ({},                                   # no token at all
                 {"_csrf": "a-token-from-an-older-tab"},
                 {"_csrf": ""}):
        c = _client()
        c.get("/login")
        payload = {"email": ADMIN, "password": NEWPW}
        payload.update(data)
        r = c.post("/login", data=payload)
        assert r.status_code == 302, \
            f"sign-in refused over a token problem: {data}"
        assert c.get("/").status_code == 200


def test_everything_that_touches_data_still_needs_a_token():
    # the exemption is sign-in ONLY
    _reset()
    c = _client()
    _signin(c, ADMIN)
    for path in ("/users/add", "/settings", "/urls/add", "/run-now"):
        assert c.post(path, data={}).status_code == 400, \
            f"{path} accepted a POST with no CSRF token"


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


def test_the_session_cookie_has_our_own_name():
    # The default name "session" collides with the cookie this app set under
    # its old signing key. Both match the path, the browser sends both, and
    # whichever the parser reads first wins - when the stale one won, the
    # browser was locked out and nothing the app did could dislodge it.
    _reset()
    name = appmod.app.config["SESSION_COOKIE_NAME"]
    assert name != "session", \
        "still using the default cookie name an old cookie can shadow"
    assert "iradar" in name
    # and it is really what goes on the wire
    c = _client()
    r = c.get("/login")
    setc = "".join(v for k, v in r.headers if k.lower() == "set-cookie")
    assert setc.startswith(name + "="), \
        f"Set-Cookie does not use {name}: {setc[:60]}"


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


# --- "last active", not "last sign-in" --------------------------------------
# last_login only moves when the sign-in FORM is submitted. A session lasts
# days, so somebody can open the dashboard every morning for a week and still
# show a week-old time — which is the opposite of what the column is for.

def test_using_it_without_signing_in_again_still_counts():
    import time as _t
    u = auth.by_email(ADMIN)
    con = db.connect()
    con.execute("UPDATE users SET last_seen='' WHERE id=?", (u["id"],))
    con.commit()
    con.close()

    auth.touch_seen(auth.by_email(ADMIN))
    first = auth.by_email(ADMIN)["last_seen"]
    assert first, "browsing did not register at all"

    login_before = auth.by_email(ADMIN)["last_login"]
    auth.touch_seen(auth.by_email(ADMIN))
    assert auth.by_email(ADMIN)["last_login"] == login_before, \
        "browsing moved last_login, which should only move on a real sign-in"


def test_it_does_not_write_on_every_single_request():
    u = auth.by_email(ADMIN)
    con = db.connect()
    con.execute("UPDATE users SET last_seen=? WHERE id=?", (auth._now(), u["id"]))
    con.commit()
    con.close()
    before = auth.by_email(ADMIN)["last_seen"]
    for _ in range(20):
        auth.touch_seen(auth.by_email(ADMIN))
    assert auth.by_email(ADMIN)["last_seen"] == before, \
        "twenty page views wrote twenty times"


def test_a_stale_stamp_is_refreshed():
    u = auth.by_email(ADMIN)
    con = db.connect()
    con.execute("UPDATE users SET last_seen='2020-01-01 00:00:00' WHERE id=?",
                (u["id"],))
    con.commit()
    con.close()
    auth.touch_seen(auth.by_email(ADMIN))
    assert auth.by_email(ADMIN)["last_seen"] != "2020-01-01 00:00:00", \
        "an old stamp was never refreshed"


def test_signing_in_sets_both():
    auth.set_password(auth.by_email(ADMIN)["id"], NEWPW, must_change=False)
    con = db.connect()
    con.execute("UPDATE users SET last_login='', last_seen='' WHERE email=?", (ADMIN,))
    con.commit()
    con.close()
    u, err = auth.check_login(ADMIN, NEWPW)
    assert not err, err
    row = auth.by_email(ADMIN)
    assert row["last_login"] and row["last_seen"], dict(row)


# --- the shared read-only link ---------------------------------------------
# GUEST_KEY is read from the environment at import, so these set the module
# attribute directly and always put it back: a leaked key would switch the
# sign-in wall off for every test that runs after.

def _with_guest_key(key, fn):
    old = appmod.GUEST_KEY
    appmod.GUEST_KEY = key
    try:
        return fn()
    finally:
        appmod.GUEST_KEY = old


def test_no_guest_key_means_no_way_in():
    """The default. An unset key is not a key that matches the empty string."""
    _reset()
    c = _client()
    def go():
        r = c.get("/?key=", follow_redirects=False)
        assert r.status_code == 302 and "/login" in r.headers["Location"], \
            "an empty key let someone in"
        r = c.get("/?key=anything", follow_redirects=False)
        assert r.status_code == 302 and "/login" in r.headers["Location"]
    _with_guest_key("", go)


def test_the_right_key_opens_it_and_the_wrong_one_does_not():
    _reset()
    def go():
        c = _client()
        assert c.get("/?key=s3cret-and-long").status_code == 200
        c2 = _client()
        r = c2.get("/?key=s3cret-and-wrong", follow_redirects=False)
        assert r.status_code == 302 and "/login" in r.headers["Location"]
    _with_guest_key("s3cret-and-long", go)


def test_the_key_is_needed_once_not_on_every_link():
    """It is answered with the session cookie, so the next click is a plain URL."""
    _reset()
    def go():
        c = _client()
        assert c.get("/?key=s3cret-and-long").status_code == 200
        assert c.get("/").status_code == 200, "the guest was dropped"
        assert c.get("/search").status_code == 200
    _with_guest_key("s3cret-and-long", go)


def test_a_guest_may_look_and_may_not_touch():
    _reset()
    def go():
        c = _client()
        c.get("/?key=s3cret-and-long")
        for path in ("/run-now", "/pause", "/urls/bulk", "/comment/add"):
            r = c.post(path, data={})
            assert r.status_code in (302, 400, 403), \
                "%s accepted a write from a guest (%s)" % (path, r.status_code)
            assert r.status_code != 200
    _with_guest_key("s3cret-and-long", go)


def test_a_guest_cannot_reach_settings_people_or_the_backup():
    """The backup is the whole database, password hashes included."""
    _reset()
    def go():
        c = _client()
        c.get("/?key=s3cret-and-long")
        for path in ("/settings", "/users", "/settings/components",
                     "/settings/backup.db"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code in (403, 404), \
                "%s was reachable by a guest (%s)" % (path, r.status_code)
    _with_guest_key("s3cret-and-long", go)


def test_turning_the_key_off_ends_the_guest_sessions_it_opened():
    _reset()
    c = _client()
    _with_guest_key("s3cret-and-long", lambda: c.get("/?key=s3cret-and-long"))
    r = c.get("/", follow_redirects=False)        # key now unset again
    assert r.status_code == 302 and "/login" in r.headers["Location"], \
        "a guest session outlived the key that opened it"


def test_accounts_still_work_while_the_link_is_open():
    """The link is an addition, not a replacement - nobody loses their sign-in."""
    _reset()
    def go():
        c = _client()
        assert _signin(c, ADMIN, NEWPW).status_code in (302, 200)
        r = c.get("/settings")
        assert r.status_code == 200, "an admin lost their own access"
    _with_guest_key("s3cret-and-long", go)


def test_a_guest_gets_a_refusal_not_a_crash_on_the_account_pages():
    """"My profile" reads the signed-in person off the session. A guest is not
    one, so the view subscripted None and answered 500 - on a link the avatar
    menu drew for them. Refused now, and not drawn."""
    _reset()
    def go():
        c = _client()
        body = c.get("/?key=s3cret-and-long").data.decode("utf-8", "replace")
        assert "My profile" not in body, "a guest was offered a profile link"
        r = c.get("/account/password", follow_redirects=False)
        msg = "/account/password answered %s for a guest" % r.status_code
        assert r.status_code == 403, msg
        # POST-only, so a GET is 405 before the gate is reached; what
        # matters is that a guest cannot rename an account not theirs.
        assert c.post("/account/name", data={}).status_code != 200
    _with_guest_key("s3cret-and-long", go)


def test_a_signed_in_person_still_has_their_profile():
    _reset()
    c = _client()
    _signin(c, ADMIN, NEWPW)
    assert c.get("/account/password").status_code == 200
    assert "My profile" in c.get("/").data.decode("utf-8", "replace")


def test_every_visit_lands_in_the_register_and_pages_are_counted():
    """One row per VISIT, not per page view - that is what makes it readable."""
    _reset()
    con = db.connect()
    con.execute("DELETE FROM guest_visits")
    con.commit()
    con.close()

    def go():
        c = _client()
        assert c.get("/?key=s3cret-and-long").status_code == 200
        for _ in range(4):
            c.get("/")
        con = db.connect()
        rows = con.execute("SELECT * FROM guest_visits").fetchall()
        con.close()
        assert len(rows) == 1, "five page views made %d rows" % len(rows)
        assert rows[0]["hits"] == 5, "pages read counted %s" % rows[0]["hits"]
        assert rows[0]["last_path"] == "/"

        other = _client()                      # a second browser is a second row
        other.get("/?key=s3cret-and-long")
        con = db.connect()
        n = con.execute("SELECT COUNT(*) FROM guest_visits").fetchone()[0]
        con.close()
        assert n == 2, "a second visitor did not get their own line"
    _with_guest_key("s3cret-and-long", go)


def test_the_register_never_stores_an_address():
    _reset()
    con = db.connect()
    con.execute("DELETE FROM guest_visits")
    con.commit()
    cols = [r[1] for r in con.execute("PRAGMA table_info(guest_visits)")]
    con.close()
    for banned in ("ip", "addr", "address", "remote", "forwarded"):
        assert not any(banned in c.lower() for c in cols),             "guest_visits grew a %s column" % banned


def test_the_register_is_the_owners_and_not_every_admins():
    """Every account here is an admin, so admin-only would be team-wide."""
    _reset()
    old_owner = appmod.OWNER_EMAIL
    try:
        appmod.OWNER_EMAIL = ADMIN
        c = _client()
        _signin(c, ADMIN, NEWPW)
        assert c.get("/guests").status_code == 200, "the owner is refused"
        appmod.OWNER_EMAIL = "someone.else@example.com"
        d = _client()
        _signin(d, ADMIN, NEWPW)
        assert d.get("/guests").status_code == 403,             "an admin who is not the owner reached the register"
    finally:
        appmod.OWNER_EMAIL = old_owner


def test_a_guest_cannot_read_the_register_of_guests():
    _reset()
    def go():
        c = _client()
        c.get("/?key=s3cret-and-long")
        assert c.get("/guests").status_code == 403
    _with_guest_key("s3cret-and-long", go)


def test_the_agent_label_is_two_words_not_a_fingerprint():
    f = appmod._agent_label
    assert f("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
             " (KHTML, like Gecko) Chrome/120.0 Safari/537.36") == "Chrome on Windows"
    assert f("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/605.1"
             " (KHTML, like Gecko) Version/17 Safari/605.1") == "Safari on Mac"
    assert f("Mozilla/5.0 (Windows NT 10.0) ... Chrome/120 Safari/537 Edg/120")         == "Edge on Windows", "Chromium browsers all claim to be Chrome"
    assert f("") == "" and f(None) == ""


def test_a_guest_can_leave_and_is_not_trapped_as_one():
    """Signing out is a POST, and a blanket GET-only refused it: the button
    answered 403, the session survived, and the next page made you a guest
    again. There was no way out of the shared view from inside the app."""
    _reset()
    def go():
        c = _client()
        body = c.get("/?key=s3cret-and-long").data.decode("utf-8", "replace")
        assert "Sign in with an account" in body, "a guest is offered no way in"
        m = re.search(r'name="_csrf" value="([^"]+)"', body)
        r = c.post("/logout", data={"_csrf": m.group(1) if m else ""})
        assert r.status_code in (302, 200), "leaving answered %s" % r.status_code
        after = c.get("/", follow_redirects=False)
        assert after.status_code == 302 and "/login" in after.headers["Location"], \
            "still a guest after leaving"
    _with_guest_key("s3cret-and-long", go)


def test_a_guest_can_always_reach_the_sign_in_page():
    _reset()
    def go():
        c = _client()
        c.get("/?key=s3cret-and-long")
        r = c.get("/login")
        assert r.status_code == 200
        assert b'type="password"' in r.data, "no sign-in form for a guest"
    _with_guest_key("s3cret-and-long", go)


def _seed_visits(n=3):
    con = db.connect()
    con.execute("DELETE FROM guest_visits")
    for i in range(n):
        con.execute("INSERT INTO guest_visits(visit,first_seen,last_seen,hits,"
                    "agent,last_path) VALUES(?,?,?,?,?,?)",
                    ("v%d" % i, "2026-08-20 10:0%d:00" % i,
                     "2026-08-20 10:1%d:00" % i, 5, "Chrome on Windows", "/"))
    con.commit()
    ids = [r[0] for r in con.execute("SELECT id FROM guest_visits ORDER BY id")]
    con.close()
    return ids


def _owner_client():
    c = _client()
    _signin(c, ADMIN, NEWPW)
    return c


def test_the_owner_can_forget_one_visit_and_the_rest_stay():
    _reset()
    ids = _seed_visits(3)
    old_owner = appmod.OWNER_EMAIL
    try:
        appmod.OWNER_EMAIL = ADMIN
        c = _owner_client()
        r = c.post("/guests/%d/delete" % ids[1], data={"_csrf": _token(c, "/")})
        assert r.status_code in (302, 200), "delete answered %s" % r.status_code
        con = db.connect()
        left = [x[0] for x in con.execute("SELECT id FROM guest_visits")]
        con.close()
        assert ids[1] not in left, "the visit is still on the register"
        assert len(left) == 2, "it took the others with it"
    finally:
        appmod.OWNER_EMAIL = old_owner


def test_the_owner_can_clear_the_whole_register():
    _reset()
    _seed_visits(4)
    old_owner = appmod.OWNER_EMAIL
    try:
        appmod.OWNER_EMAIL = ADMIN
        c = _owner_client()
        c.post("/guests/clear", data={"_csrf": _token(c, "/")})
        con = db.connect()
        n = con.execute("SELECT COUNT(*) FROM guest_visits").fetchone()[0]
        con.close()
        assert n == 0, "%d visits survived the clear" % n
    finally:
        appmod.OWNER_EMAIL = old_owner


def test_nobody_but_the_owner_can_erase_the_register():
    """Every account is an admin, so this must not be an admin-level power."""
    _reset()
    ids = _seed_visits(2)
    old_owner = appmod.OWNER_EMAIL
    try:
        appmod.OWNER_EMAIL = "someone.else@example.com"
        c = _owner_client()                       # an admin, not the owner
        assert c.post("/guests/%d/delete" % ids[0],
                      data={"_csrf": _token(c, "/")}).status_code == 403
        assert c.post("/guests/clear",
                      data={"_csrf": _token(c, "/")}).status_code == 403
        con = db.connect()
        n = con.execute("SELECT COUNT(*) FROM guest_visits").fetchone()[0]
        con.close()
        assert n == 2, "a non-owner erased something"
    finally:
        appmod.OWNER_EMAIL = old_owner


def test_a_guest_cannot_erase_the_register_of_guests():
    _reset()
    ids = _seed_visits(2)
    def go():
        c = _client()
        c.get("/?key=s3cret-and-long")
        assert c.post("/guests/%d/delete" % ids[0], data={}).status_code != 200
        assert c.post("/guests/clear", data={}).status_code != 200
        con = db.connect()
        n = con.execute("SELECT COUNT(*) FROM guest_visits").fetchone()[0]
        con.close()
        assert n >= 2, "a guest cleared the register"
    _with_guest_key("s3cret-and-long", go)


def test_a_browser_that_keeps_no_cookie_is_told_so():
    """Twenty-four attempts went by showing nothing but the form again."""
    _reset()
    # a POST carrying no cookie at all: the browser was handed the page and
    # came back without what it was given
    c = _client()
    r = c.post("/login", data={"email": ADMIN, "password": "wrong"})
    body = r.data.decode("utf-8", "replace")
    assert "not keeping" in body, "the cookie problem was not explained"


def test_the_note_is_never_a_refusal():
    """Being wrong about a cookie must not become a locked door - the code
    comment on the CSRF exemption says this cost four lockouts already."""
    _reset()
    c = _client()
    tok = _token(c)
    r = c.post("/login", data={"email": ADMIN, "password": NEWPW, "_csrf": tok},
               follow_redirects=False)
    assert r.status_code == 302, "a correct password was refused (%s)" % r.status_code
    assert c.get("/").status_code == 200, "signing in did not take"


def test_a_first_visit_is_not_accused_of_blocking_cookies():
    _reset()
    c = _client()
    body = c.get("/login").data.decode("utf-8", "replace")
    assert "not keeping" not in body, "a first-time visitor was shown the warning"
