"""Team accounts, roles and sign-in for InfineonWatch.

Replaces the single shared HTTP Basic password (DASH_USER/DASH_PASS) with real
per-person accounts, because the dashboard is going on a public domain for a
whole team: a shared password cannot be revoked for one person, tells you
nothing about who accepted an update, and lands in everyone's browser store.

Two roles:
  admin  - everything: accounts, settings, urls, capture runs
  editor - the review work: accept/reject updates, edit page metadata, read
           everything. Cannot manage accounts, settings, urls or runs.

Passwords are never stored. werkzeug hashes them with a per-password salt
(scrypt where available, pbkdf2 otherwise), so the database never reveals a
password even if it leaks.
"""
import os
import re
import secrets
import time

from werkzeug.security import check_password_hash, generate_password_hash

import db

ROLES = ("admin", "editor")
SEED_PASSWORD = "Infineon@2026"          # starting password for seeded accounts

# A brand-new account (and any admin password reset) must pick a new password at
# first sign-in. The team all starting on one known string is convenient exactly
# once; leaving it that way means any one of them can sign in as any other.
FORCE_CHANGE_ON_FIRST_LOGIN = True

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# --- sign-in throttle -------------------------------------------------------
# A public URL gets password-guessed. Five wrong tries for one account parks it
# for a minute - slow enough to make guessing pointless, short enough that a
# colleague who fat-fingered their password is not locked out of their evening.
_FAILS = {}
MAX_FAILS = 5
LOCK_SECONDS = 60


def _now():
    return db.now_iso()


# --- password / email rules -------------------------------------------------

def valid_email(email):
    return bool(_EMAIL_RE.match((email or "").strip()))


def norm_email(email):
    return (email or "").strip().lower()


def password_problem(pw):
    """None if the password is acceptable, else the reason to show the user."""
    if not pw or len(pw) < 10:
        return "Use at least 10 characters."
    if pw.lower() in ("password", "infineon", "infineonwatch"):
        return "Too easy to guess - pick something else."
    return None


# --- lookups ----------------------------------------------------------------

def get_user(user_id, con=None):
    own = con is None
    con = con or db.connect()
    try:
        return con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    finally:
        if own:
            con.close()


def by_email(email, con=None):
    own = con is None
    con = con or db.connect()
    try:
        return con.execute("SELECT * FROM users WHERE email=?",
                           (norm_email(email),)).fetchone()
    finally:
        if own:
            con.close()


def list_users(con=None):
    own = con is None
    con = con or db.connect()
    try:
        return con.execute(
            "SELECT * FROM users ORDER BY (role<>'admin'), email").fetchall()
    finally:
        if own:
            con.close()


def count_admins(con=None):
    own = con is None
    con = con or db.connect()
    try:
        return con.execute("SELECT COUNT(*) FROM users"
                           " WHERE role='admin' AND active=1").fetchone()[0]
    finally:
        if own:
            con.close()


# --- create / modify --------------------------------------------------------

def create_user(email, name="", role="editor", password=None, con=None):
    """Add an account. Returns (user_id, None) or (None, 'why not')."""
    email = norm_email(email)
    if not valid_email(email):
        return None, "That does not look like an email address."
    if role not in ROLES:
        return None, "Unknown role."
    own = con is None
    con = con or db.connect()
    try:
        if con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            return None, "An account with that email already exists."
        pw = password or SEED_PASSWORD
        cur = con.execute(
            "INSERT INTO users(email, name, pw_hash, role, active,"
            " must_change_pw, created) VALUES(?,?,?,?,1,?,?)",
            (email, (name or "").strip(), generate_password_hash(pw), role,
             1 if FORCE_CHANGE_ON_FIRST_LOGIN else 0, _now()))
        con.commit()
        return cur.lastrowid, None
    finally:
        if own:
            con.close()


def set_password(user_id, password, must_change=False, con=None):
    own = con is None
    con = con or db.connect()
    try:
        con.execute("UPDATE users SET pw_hash=?, must_change_pw=? WHERE id=?",
                    (generate_password_hash(password), 1 if must_change else 0,
                     user_id))
        con.commit()
    finally:
        if own:
            con.close()


def set_role(user_id, role, con=None):
    """Change a role. Refuses to remove the last admin - an account list nobody
    can administer is only recoverable from a shell."""
    if role not in ROLES:
        return "Unknown role."
    own = con is None
    con = con or db.connect()
    try:
        u = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not u:
            return "No such account."
        if u["role"] == "admin" and role != "admin" and count_admins(con) <= 1:
            return "That is the only admin - promote someone else first."
        con.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        con.commit()
        return None
    finally:
        if own:
            con.close()


def set_active(user_id, active, con=None):
    own = con is None
    con = con or db.connect()
    try:
        u = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not u:
            return "No such account."
        if u["role"] == "admin" and not active and count_admins(con) <= 1:
            return "That is the only admin - promote someone else first."
        con.execute("UPDATE users SET active=? WHERE id=?",
                    (1 if active else 0, user_id))
        con.commit()
        return None
    finally:
        if own:
            con.close()


def delete_user(user_id, con=None):
    own = con is None
    con = con or db.connect()
    try:
        u = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not u:
            return "No such account."
        if u["role"] == "admin" and count_admins(con) <= 1:
            return "That is the only admin - promote someone else first."
        con.execute("DELETE FROM users WHERE id=?", (user_id,))
        con.commit()
        return None
    finally:
        if own:
            con.close()


# --- sign-in ----------------------------------------------------------------

def locked_for(email):
    """Seconds remaining on the guess-throttle for this account, 0 if free."""
    rec = _FAILS.get(norm_email(email))
    if not rec:
        return 0
    fails, until = rec
    if fails >= MAX_FAILS and until > time.time():
        return int(until - time.time()) + 1
    return 0


def _note_fail(email):
    email = norm_email(email)
    fails = _FAILS.get(email, (0, 0))[0] + 1
    _FAILS[email] = (fails, time.time() + LOCK_SECONDS)


def _clear_fails(email):
    _FAILS.pop(norm_email(email), None)


def check_login(email, password):
    """(user_row, None) on success, (None, 'why not') otherwise.

    The same wording is returned for an unknown email and a wrong password: a
    login form that distinguishes them tells an outsider which addresses are
    real accounts.
    """
    wait = locked_for(email)
    if wait:
        return None, f"Too many attempts - try again in {wait} seconds."
    u = by_email(email)
    if not u or not check_password_hash(u["pw_hash"], password or ""):
        _note_fail(email)
        return None, "Wrong email or password."
    if not u["active"]:
        return None, "That account has been switched off."
    _clear_fails(email)
    con = db.connect()
    con.execute("UPDATE users SET last_login=? WHERE id=?", (_now(), u["id"]))
    con.commit()
    con.close()
    return u, None


# --- first-run seeding ------------------------------------------------------

def seed_users(spec, con=None):
    """Create any accounts in `spec` that do not exist yet.

    spec: iterable of (email, name, role). Existing accounts are left alone, so
    this is safe to run on every start - it never resets a password someone has
    already changed. Returns the list of emails actually created.
    """
    made = []
    own = con is None
    con = con or db.connect()
    try:
        for email, name, role in spec:
            if by_email(email, con):
                continue
            uid, err = create_user(email, name, role, con=con)
            if uid:
                made.append(norm_email(email))
        return made
    finally:
        if own:
            con.close()


def secret_key(path):
    """A stable random session key, generated once and kept out of git.

    The key signs the session cookie. Shipping a constant in the source (the old
    'infineonwatch-local-only') means anyone with the repo can forge a signed
    cookie for any account once the site is public. Regenerating it per start
    would sign everyone out on every restart, so it is written next to the
    database instead.
    """
    try:
        if os.path.exists(path):
            data = open(path, "r", encoding="utf-8").read().strip()
            if len(data) >= 32:
                return data
    except Exception:
        pass
    key = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(key)
        try:                      # best effort: readable only by this account
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception:
        pass                      # fall back to an in-memory key for this run
    return key
