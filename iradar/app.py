"""iRadar - website change monitor.

Run:  python app.py     then open http://127.0.0.1:5000
"""
import colorsys
import gzip
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote as urlquote, urlsplit

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from flask import (Flask, Response, abort, flash, jsonify, redirect,
                   render_template, request, send_file, session, url_for)
from rapidfuzz import fuzz, process

import auth
import comments
import compare as cmp
import mailer
import db
import runner

app = Flask(__name__)
# Signs the session cookie. Random, generated once, stored next to the database
# and never committed - a constant in the source would let anyone holding the
# repo forge a signed cookie for any account. See auth.secret_key.
app.secret_key = auth.secret_key(str(db.DATA_DIR / "secret_key"))
app.config.update(
    # NOT the default "session". A browser that still holds a cookie called
    # "session" from an older signing key keeps sending it alongside the new
    # one; both match the path, and whichever the parser reads first wins. When
    # the stale one wins, every request looks like it has no valid session and
    # nothing the app does can dislodge it - the browser is simply locked out.
    # A name this app has never used before cannot be shadowed by that history.
    SESSION_COOKIE_NAME="iradar_session",
    SESSION_COOKIE_HTTPONLY=True,      # javascript can never read the cookie
    SESSION_COOKIE_SAMESITE="Lax",     # not sent on cross-site POSTs
    # Only send the cookie over HTTPS. Off by default so plain-http localhost
    # still works; set SECURE_COOKIES=1 for the public deployment.
    SESSION_COOKIE_SECURE=os.environ.get("SECURE_COOKIES") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
)
# Re-read templates from disk on every render, so tweaks to the HTML/CSS in
# templates/ take effect on a browser refresh WITHOUT restarting the server
# (a restart would interrupt an in-process capture run). Negligible overhead.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# --- who may do what -------------------------------------------------------
# Reading is open to any signed-in person; WRITING is admin-only unless the
# endpoint is named below. Default-deny on writes is deliberate: a POST route
# added later is admin-only until someone consciously opens it to editors,
# instead of silently inheriting edit rights the day it is written.
EDITOR_MAY_POST = {
    "change_resolve",        # accept / reject a detected update - the core job
    "assets_review",         # same, for file updates
    "url_set_level",         # page metadata an editor is expected to curate
    "url_set_meta",
    "url_set_title",
    "prewarm",               # warms document previews; no data changes
    "account_password",      # everyone may change their OWN password
    "account_alerts",        # everyone toggles their OWN email alerts
    "account_latest_report",    # ...and may send themselves the latest report
    "logout",
    # Notes are the review conversation itself - an editor who cannot write one
    # cannot do the job. Who may edit or delete WHICH note is decided per note
    # inside the comments module, not by role alone: your own words are yours,
    # and an admin can remove anyone's.
    "comment_add", "comment_edit", "comment_delete", "comment_resolve",
    "account_name",          # your own display name is yours
}
# Pages an editor cannot even look at (they configure the system).
ADMIN_ONLY_VIEWS = {"settings_view", "settings_components", "users_page",
                    "settings_backup",
                    # readable by any admin, but never by a guest: taking it
                    # out of OWNER_ONLY_VIEWS left it in no gate at all, and a
                    # guest holding the shared link could read the register of
                    # everyone else holding it
                    "guests_page"}
# Endpoints that are ABOUT an account, so there is nothing for a guest to see:
# they read the signed-in person off the session and a guest is not one. Left
# reachable, "My profile" answered a guest with a 500 rather than a refusal -
# current_user() is None and the view subscripts it. A guest gets 403 here and
# the link is not drawn for them either.
ACCOUNT_ONLY_VIEWS = {"account_password", "account_name"}
# The one write a guest must be allowed. Signing out is a POST, so a blanket
# "GET only" refused it: the button answered 403, the guest session survived
# the click, and the next page load re-read the cookie and made them a guest
# again - a trap with no way out of it from inside the app. It only ever
# clears the caller's own session.
GUEST_MAY_POST = {"logout"}
# Pages only the owner may see. Every account here is an admin, so an
# admin-only page is a team-wide page; these are refused to anyone else.
# Reading the register is open to any admin: it holds a visit id, times,
# a hit count, a user agent and a last path - no name and deliberately no
# address, so there is nothing private in it to protect. ERASING it stays
# the owner's, because that destroys the record of who has been reading.
OWNER_ONLY_VIEWS = {"guests_delete", "guests_clear"}
# Reachable without signing in.
PUBLIC_ENDPOINTS = {"login", "static", "report_chart", "cron_run"}

# --- the shared read-only link ---------------------------------------------
# UNSET, WHICH IS THE DEFAULT AND WHAT EVERY EXISTING INSTALL HAS, means none
# of this exists and the app is sign-in only exactly as before. Set GUEST_KEY
# to any long random string and `?key=<that string>` opens the app without an
# account. The key is checked once and answered with the ordinary session
# cookie, so only the first link carries it and nothing has to be pasted onto
# every URL afterwards.
#
# READ-ONLY, and not as a courtesy. A key in a URL is a weak secret: it goes
# into browser history, into the Referer header of every outbound link, into
# proxy logs, and into whatever a person pastes it into. That is fine for
# letting somebody look, and not fine for letting them accept updates, change
# the schedule, manage accounts or download the database - so a guest is held
# to GET, and the admin views (settings, people, and the backup, which carries
# every password hash) stay shut to them regardless.
#
# It is switched off by removing the variable. Accounts are untouched either
# way: signing in still works, and everyone who has an account keeps theirs.
GUEST_KEY = os.environ.get("GUEST_KEY", "").strip()


def guest_link_enabled():
    """Is the shared read-only link switched ON?

    The KEY still comes from the environment - a secret does not belong in a
    database anyone with the file can read - but whether it currently works is
    a setting, so the owner can close the door from the page they are already
    on instead of editing an environment variable and waiting for a redeploy.
    Unset means on, which is what every install had before the switch."""
    try:
        return db.get_settings().get("guest_link_enabled", "1") != "0"
    except Exception:
        return True



# What templates render in place of a signed-in account. Item access only,
# because that is all the templates use of `me`.
GUEST_ME = {"id": 0, "name": "Guest", "email": "", "role": "viewer",
            "active": 1, "must_change_pw": 0}


_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Chrome/", "Chrome"),
             ("Firefox/", "Firefox"), ("Safari/", "Safari"))
_SYSTEMS = (("Windows", "Windows"), ("Android", "Android"),
            ("iPhone", "iPhone"), ("iPad", "iPad"), ("Mac OS", "Mac"),
            ("Linux", "Linux"))


def _agent_label(ua):
    """"Chrome on Windows" from a user-agent header, or "" if it is not one.

    Two words, derived and the header thrown away. It is here to tell one
    visitor apart from another at a glance, not to identify anybody - which is
    also why the raw string, which is long enough to fingerprint a machine, is
    not what gets stored. Order matters: every Chromium browser also claims to
    be Chrome and Safari, so the specific names are tested first.
    """
    ua = ua or ""
    browser = next((n for t, n in _BROWSERS if t in ua), "")
    system = next((n for t, n in _SYSTEMS if t in ua), "")
    if browser and system:
        return "%s on %s" % (browser, system)
    return browser or system or ""


def _record_guest_visit():
    """Add this page view to the register of who has opened the shared link.

    ONE ROW PER VISIT, not per page view: a random id is minted into the
    browser's session the first time it presents the key, so somebody reading
    forty pages is one line with hits=40 rather than forty lines. That is what
    makes this readable, and it is also why this is not throttled the way
    auth.touch_seen is - the row count is bounded by people rather than by
    clicks, and the hit count is the part worth having accurate.

    NO ADDRESS IS STORED. Everyone arrives through the same link, so an IP
    would be the only field here able to name a person, and a register of
    which colleague read which page is a different thing from "has anyone
    opened it".
    """
    vid = session.get("gvid")
    if not vid:
        vid = secrets.token_hex(8)
        session["gvid"] = vid
    now = db.now_iso()
    try:
        con = db.connect()
        try:
            con.execute(
                "INSERT INTO guest_visits(visit, first_seen, last_seen, hits,"
                " agent, last_path) VALUES(?,?,?,1,?,?)"
                " ON CONFLICT(visit) DO UPDATE SET last_seen=excluded.last_seen,"
                " hits=hits+1, last_path=excluded.last_path",
                (vid, now, now, _agent_label(request.headers.get("User-Agent")),
                 request.path))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass                    # a register entry is never worth a failed page


def guest_active():
    """Is this request riding the shared link rather than an account?

    Cached per request like current_user(). The key is accepted from the query
    string once and remembered in the session, so a guest who follows a link
    inside the app is not bounced the moment the `?key` falls off the URL.
    """
    if not hasattr(request, "_guest"):
        ok = False
        if GUEST_KEY and guest_link_enabled():
            sent = request.args.get("key", "")
            if sent and secrets.compare_digest(str(sent), GUEST_KEY):
                session["guest"] = True
                ok = True
            else:
                ok = bool(session.get("guest"))
        request._guest = ok
    return request._guest


def csrf_token():
    """This session's form token, minted on first use.

    SameSite=Lax already stops another site's form from POSTing here with the
    cookie attached, which is the classic attack. This is the second lock: it
    also covers a same-site injection, an old browser, and any future change
    that loosens the cookie policy - none of which should be one mistake away
    from letting a page somewhere else accept updates as you.
    """
    t = session.get("_csrf")
    if not t:
        t = secrets.token_urlsafe(32)
        session["_csrf"] = t
    return t


def current_user():
    """The signed-in user row, or None. Cached per request."""
    if not hasattr(request, "_user"):
        uid = session.get("uid")
        u = auth.get_user(uid) if uid else None
        # A user deactivated or deleted mid-session loses access immediately
        # rather than at cookie expiry.
        if u is not None and not u["active"]:
            u = None
        # The session names the password it was opened with. Change the
        # password and every other session for that account stops working -
        # the machine left signed in somewhere, or a lifted cookie.
        if u is not None and session.get("pwfp") != auth.pw_fingerprint(u):
            u = None
        request._user = u
    return request._user


def _client_ip():
    """Caller's address, trusting Cloudflare's header only when we are behind
    it - anyone can send X-Forwarded-For, so believing it on a direct
    connection would let one attacker look like thousands."""
    if os.environ.get("BEHIND_PROXY") == "1":
        fwd = request.headers.get("CF-Connecting-IP") or \
              request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if fwd:
            return fwd
    return request.remote_addr or ""


def is_admin():
    u = current_user()
    return bool(u and u["role"] == "admin")


# The person this installation belongs to, by the address that seeded it.
# ADMIN IS NOT A SMALL GROUP HERE: all seven accounts are admins, so
# "admin only" is the same as "everyone", which is the wrong audience for
# something that is nobody's business but the owner's - like whether the
# shared link has been opened. This is the one distinction the app already
# had, it costs no schema, and handing the tool over is changing one variable
# rather than editing code.
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "").strip().lower()


def is_owner():
    """Is the signed-in person the one OWNER_EMAIL names?

    False when OWNER_EMAIL is unset, which is deliberate: unset means nobody
    has claimed this installation, and guessing that the lowest user id is the
    owner would hand a stranger's install to whoever registered first.
    """
    u = current_user()
    if not u or not OWNER_EMAIL:
        return False
    return (u["email"] or "").strip().lower() == OWNER_EMAIL


@app.before_request
def _require_login():
    ep = request.endpoint
    # CSRF first, and it covers sign-in too: the token is issued when the login
    # page renders, so a POST that never loaded the page is refused.
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        # The external cron carries its own secret and has no session to hold
        # a CSRF token in - a machine calling a URL on a schedule is not a
        # browser being tricked into anything, which is the only thing CSRF
        # defends against. Its own key is checked in the view.
        if ep == "cron_run":
            return None
        sent = (request.form.get("_csrf")
                or request.headers.get("X-CSRF-Token") or "")
        have = session.get("_csrf") or ""
        if ep == "login":
            # Sign-in is EXEMPT, deliberately. It is the one request that has
            # to work before a usable session exists, so it is the one place a
            # cookie problem becomes a locked door with nothing behind it - it
            # locked this account out four times running. Cross-site sign-in is
            # already stopped by SameSite=Lax, the threat it protects against
            # (being signed into someone else's account) is mild, and every
            # endpoint that touches data is still covered below, where a
            # session exists by definition. A log line records what the browser
            # actually sent, so a repeat is diagnosed instead of guessed at.
            if not have or not secrets.compare_digest(str(sent), str(have)):
                # Sec-Fetch-* is what separates the two ways a browser sends
                # no cookie: "dest=iframe" or "site=cross-site" means the app is
                # embedded somewhere and SameSite=Lax is holding the cookie
                # back; "dest=document, site=same-origin" with no cookie means
                # the browser is simply not keeping them. Twenty-four failures
                # went by without anyone being able to tell those apart.
                print("[signin] token mismatch: cookie=%s session_token=%s"
                      " form_token=%s fetch-dest=%s fetch-site=%s ua=%.40s"
                      % (bool(request.cookies), (have or "-")[:8],
                         (sent or "-")[:8],
                         request.headers.get("Sec-Fetch-Dest", "?"),
                         request.headers.get("Sec-Fetch-Site", "?"),
                         request.headers.get("User-Agent", "?")), flush=True)
            return None
        if not have or not secrets.compare_digest(str(sent), str(have)):
            # A login form left open across a restart carries a token that no
            # longer matches. Bouncing it back to /login looked like the
            # password was wrong: the page simply reappeared, blank, with
            # nothing to act on. Let the sign-in view render it as a plain
            # "that form went stale, here is a fresh one" instead.
            if ep == "login":
                request._csrf_stale = True
                return None
            # A session that quietly expired looks the same as a bad token.
            # Sending those to sign in beats a bare 400 nobody can act on.
            if current_user() is None:
                return redirect(url_for("login"))
            abort(400)
    if ep in PUBLIC_ENDPOINTS:
        return
    u = current_user()
    if u is None and guest_active():
        # Look, do not touch. Anything that writes, configures, or hands over
        # the database is refused outright rather than hidden in the template -
        # a guest who guesses a URL gets a 403, not a form that half works.
        if (ep in ADMIN_ONLY_VIEWS or ep in ACCOUNT_ONLY_VIEWS
                or ep in OWNER_ONLY_VIEWS):
            abort(403)
        if request.method not in ("GET", "HEAD") and ep not in GUEST_MAY_POST:
            abort(403)
        _record_guest_visit()
        return
    if u is None:
        session.clear()
        if request.accept_mimetypes.best == "application/json" or \
                request.path.startswith(("/status", "/api/")):
            return jsonify({"error": "sign-in required"}), 401
        return redirect(url_for("login", next=request.full_path
                                if request.query_string else request.path))
    # A seeded or admin-reset account carries the shared starting password.
    # Nothing but changing it (or signing out) works until it is changed.
    if u["must_change_pw"] and ep not in ("account_password", "logout"):
        return redirect(url_for("account_password"))
    if ep in ADMIN_ONLY_VIEWS and not is_admin():
        abort(403)
    if ep in OWNER_ONLY_VIEWS and not is_owner():
        abort(403)
    if request.method in ("POST", "PUT", "PATCH", "DELETE") \
            and ep not in EDITOR_MAY_POST and not is_admin():
        abort(403)
    # Somebody who signed in on Monday and has opened the dashboard every
    # morning since is still using iRadar - but last_login cannot see that,
    # it only moves when the sign-in FORM is submitted. Throttled to one
    # write per five minutes of real use, so a page view costs nothing.
    try:
        auth.touch_seen(u)
    except Exception:
        pass


@app.context_processor
def _inject_user():
    """Every template can ask who is signed in and whether they are an admin."""
    me = current_user()
    guest = me is None and guest_active()
    if guest:
        me = GUEST_ME
    alerts_on = False
    if me is not None and not guest:
        try:
            alerts_on = bool(me["email_opt_in"])
        except (KeyError, IndexError, TypeError):
            alerts_on = False
    return {"me": me, "is_admin": is_admin(), "is_owner": is_owner(),
            "is_guest": guest, "email_alerts": alerts_on,
            "mail_ready": mailer.configured(),
            "guest_link_on": bool(GUEST_KEY) and guest_link_enabled(),
            "csrf_token": csrf_token}


# <form ... method="post" ...> - the tag only, so nothing inside a string or a
# comment further down the page can be mistaken for one
_FORM_TAG = re.compile(rb"<form\b[^>]*\bmethod\s*=\s*[\"']?post[\"']?[^>]*>",
                       re.I)


@app.after_request
def _harden(resp):
    """Stamp the CSRF token into every form, and set the headers a public site
    needs.

    The token is injected here rather than written into each of the 28 forms by
    hand for one reason: a form added next month would otherwise silently ship
    without one and be refused at submit. Injection cannot forget. It only ever
    touches HTML this app generated itself.
    """
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")          # no clickjacking
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if os.environ.get("SECURE_COOKIES") == "1":
        # a year, and only meaningful once the site really is HTTPS-only
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    if resp.mimetype == "text/html" and resp.direct_passthrough is False:
        try:
            token = csrf_token().encode()
            field = (b'<input type="hidden" name="_csrf" value="' + token + b'">')
            body, n = _FORM_TAG.subn(lambda m: m.group(0) + field, resp.get_data())
            if n:
                # the meta tag lets the page's fetch() calls send the header
                body = body.replace(
                    b"</head>",
                    b'<meta name="csrf-token" content="' + token + b'"></head>', 1)
                resp.set_data(body)
        except Exception:
            pass          # a failed injection must never blank a page
    return resp


@app.after_request
def _no_cache(resp):
    # The pages are dynamic and the JS/CSS is inlined in them, so a browser that
    # cached an old page keeps running old code (that read as "select-all still
    # broken" after a fix). Tell it to always revalidate the HTML.
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


URL_RE = re.compile(r"https?://[^\s\"',;<>]+")


@app.template_filter("clean_title")
def clean_title_filter(s):
    """Display titles: drop the site-name tail and Excel page-type codes."""
    if not s:
        return s
    s = re.sub(r"\s*\|\s*Infineon\s*Technologies.*$", "", s, flags=re.I)
    s = re.sub(r"\s*[—–-]+\s*[A-Z]{2,6}\s*$", "", s)
    return s.strip()


@app.template_filter("has_amount")
def has_amount_filter(s):
    """Is this captured text an actual price?

    The buying-options block also renders things that are not prices at all -
    "Multiple OPNs available", a button caption, a link to a distributor. Those
    read as a price on our page while telling you nothing, so the page says
    "empty" instead. A real price has a currency amount in it.
    """
    return bool(re.search(r"[$€£]\s?\d", s or ""))


@app.template_filter("fmt_price")
def fmt_price_filter(s):
    """The captured buying-options text, split into clean display lines.

    Raw capture mashes everything together ('$4.28ea. 986 in stock Volume
    pricing: $2.34- $4.28 ea. Request Free Samples'). Parse the known pieces
    and rebuild with proper spacing:
        $4.28 ea.
        986 in stock
        Volume pricing: $2.34 - $4.28 ea.
    'Request Free Samples' (a button caption, not a price) is dropped. A
    string with none of the known shapes ('Multiple OPNs available') passes
    through as a single line.
    """
    s = " ".join((s or "").split())
    if not s:
        return []
    rest = re.sub(r"request\s+free\s+samples?\.?", "", s, flags=re.I).strip()
    lines = []
    m = re.search(r"([$€£]\s?[\d.,]+\d)\s*ea\.?(?!\s*[-–])", rest)
    if m:
        lines.append(m.group(1).replace(" ", "") + " ea.")
        rest = rest.replace(m.group(0), " ", 1)
    m = re.search(r"([\d.,]*\d)\s*in\s+stock", rest, flags=re.I)
    if m:
        lines.append(m.group(1) + " in stock")
        rest = rest.replace(m.group(0), " ", 1)
    m = re.search(r"volume\s+pricing:?\s*([$€£]\s?[\d.,]+\d)\s*[-–]\s*"
                  r"([$€£]\s?[\d.,]+\d)\s*ea\.?", rest, flags=re.I)
    if m:
        lines.append(f"Volume pricing: {m.group(1).replace(' ', '')} - "
                     f"{m.group(2).replace(' ', '')} ea.")
        rest = rest.replace(m.group(0), " ", 1)
    rest = rest.strip(" .-–")
    if rest and not lines:
        lines.append(rest)          # unknown shape - show as captured
    elif rest:
        lines.append(rest)          # leftovers ride along, never lost
    return lines


ACRONYMS = {"psoc", "xmc", "optiga", "airoc", "traveo", "aurix", "tpm", "mcu",
            "usb", "iot", "ai", "hmi", "rf", "sdk", "pcb", "ev", "led", "nfc",
            "ble", "wifi", "gan", "sic", "id", "tpu", "cpu", "kit", "eol"}


@app.template_filter("fromjson")
def _fromjson_filter(s):
    """Decode a JSON column for the template (the per-change component list)."""
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


@app.template_filter("initials")
def _initials_filter(person):
    """One or two letters for an avatar: first name and last name.

    'Bogdan Constantin Palaghia' -> BP, not BC. The middle name is the least
    useful of the three for telling people apart at 32 pixels.
    """
    s = ""
    if isinstance(person, str):
        s = person
    else:
        try:
            s = (person["name"] or "").strip() or (person["email"] or "").split("@")[0]
        except Exception:
            s = str(person or "")
    parts = [p for p in re.split(r"[\s._-]+", s.strip()) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[-1][:1]).upper()


_GOLDEN_ANGLE = 137.508      # successive multiples are maximally far apart


@app.template_filter("avatar")
def _avatar_filter(person):
    """This person's avatar colour, as a ready CSS gradient.

    Two problems a fixed palette of N colours cannot solve: the N+1th person
    repeats somebody, and with ten colours and eight people a clash is already
    more likely than not. So the colour is placed on the wheel rather than
    chosen from a list.

    Successive accounts are spun by the golden angle, which is the arrangement
    that keeps every neighbour as far from the last as possible no matter how
    many there are - the first twenty are all obviously different, and it keeps
    working past that instead of wrapping onto itself.

    Lightness is not constant, because a constant one does not survive the
    wheel: yellow at the lightness that makes blue readable is a highlighter
    pen with invisible white text on it. Yellows and greens are darkened
    exactly enough to carry the initials.

    Anchored on the account id where there is one, so a person's colour never
    changes - not when they are renamed, and not when somebody else is added or
    removed.
    """
    def field(key):
        """(present, value) - a NULL user_id is not the same as no user_id."""
        try:
            return True, person[key]
        except (KeyError, IndexError, TypeError):
            return False, None

    seed = None
    if not isinstance(person, str):
        # a note carries the account that wrote it; an account carries its own
        # id. user_id first, because a note row has both and its own id is the
        # note's, not the person's. A note whose author has since been deleted
        # has the column but no value, and must fall through to the name -
        # taking the note's id would give that person a new colour per note.
        seen, val = field("user_id")
        if not seen:
            seen, val = field("id")
        if val is not None:
            try:
                seed = int(val)
            except (TypeError, ValueError):
                seed = None
    if seed is None:
        # a name with no account behind it any more - the person was deleted,
        # their notes stay. Falls back to the name, which is all that is left.
        s = person if isinstance(person, str) else ""
        if not s:
            try:
                s = person["author"] or person["email"] or person["name"] or ""
            except Exception:
                s = str(person or "")
        seed = int.from_bytes(
            hashlib.sha1(s.strip().lower().encode("utf-8")).digest()[:4], "big")

    return _avatar_css(seed)


def _luminance(r, g, b):
    def ch(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _at_brightness(hue, sat, target):
    """The colour at this hue and saturation that is exactly `target` bright.

    One lightness across the whole wheel does not give one brightness: at the
    value that makes indigo comfortable, teal is a highlighter pen and white
    text vanishes into it. So brightness is the thing held fixed and lightness
    is solved for, which is why the greens come out deep and the blues do not.
    """
    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2
        r, g, b = colorsys.hls_to_rgb(hue / 360.0, mid, sat)
        if _luminance(r, g, b) < target:
            lo = mid
        else:
            hi = mid
    r, g, b = colorsys.hls_to_rgb(hue / 360.0, (lo + hi) / 2, sat)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _skip_olive(hue):
    """Bend the wheel around the yellow-olive arc.

    Solving for brightness makes every hue READABLE, which is not the same as
    every hue being wanted: at the brightness white initials need, 45-95
    degrees is olive, and olive on a person's face looks like a mistake rather
    than a colour. The arc is not cut out - that would put a seam on the wheel
    where two people either side of it come out nearly identical - it is
    squeezed, so hues still land in order and still spread, just faster
    through the part nobody wants to be.

    Not to zero, though: a wheel with a hole in it has a SEAM, and the two
    people either side of a seam get near-identical colours - which is the
    thing this whole scheme exists to prevent. Crossing it in 1.5 degrees of
    input leaves about one account in 240 landing there, and keeps everyone
    else distinct.
    """
    lo, hi = 42.0, 96.0     # the arc to hurry through, in the OUTPUT
    cross = 1.5             # how much of the input is spent crossing it
    span = hi - lo
    scale = (360.0 - span) / (360.0 - cross)
    start = lo / scale      # the input hue that arrives at the arc
    if hue < start:
        return hue * scale
    if hue < start + cross:
        return lo + (hue - start) * span / cross
    return hi + (hue - start - cross) * scale


@lru_cache(maxsize=512)
def _avatar_css(seed):
    hue = _skip_olive((seed * _GOLDEN_ANGLE) % 360)
    # 0.16 puts white text at 5:1 - past the 4.5 the guidelines ask for, on
    # every hue, with the darker end of the gradient better still
    return ("linear-gradient(135deg, %s, %s)"
            % (_at_brightness(hue, 0.66, 0.16),
               _at_brightness(hue, 0.62, 0.085)))


@app.template_filter("notemd")
def _notemd_filter(body):
    """A note's text as safe HTML: escaped, then line breaks and links.

    Marked safe only AFTER comments.render has escaped it - the escaping is
    what makes it safe, and doing this in the template instead would be one
    |safe away from letting anyone on the team run script in everyone's
    browser.
    """
    from markupsafe import Markup
    return Markup(comments.render(body))


@app.template_filter("pretty_slug")
def pretty_slug_filter(s):
    """Readable provisional name for a page we have not captured yet."""
    if not s:
        return ""
    s = re.sub(r"\s*[—–-]+\s*[A-Z]{2,6}\s*$", "", s.strip())   # drop PFP/PEFP
    if " " in s:                       # already a real name
        return s
    words = [w for w in re.split(r"[-_/]+", s) if w]
    out = []
    for w in words:
        out.append(w.upper() if w.lower() in ACRONYMS else w.capitalize())
    return " ".join(out)


# The zone every time on screen is shown in. Named, not inherited: this used
# to be a bare .astimezone(), which means "whatever zone the machine is set
# to". On the laptop that was right by accident. In a container it is whatever
# the platform felt like, and a platform that quietly switches to UTC moves
# every timestamp in the app two hours without one line of code changing.
DISPLAY_TZ = os.environ.get("IRADAR_TZ", "Europe/Berlin")


def _report_when(run):
    """Human date for the run in the display zone, portable across OSes.

    A check is named for its SCHEDULED day, not the minute the machine
    happened to finish: started early on the Saturday, or overrunning into
    the Monday, it is still "the Sunday check". So the label snaps to the
    nearest ticked day - with several days ticked, the one this run actually
    belongs to, which is whichever is closest."""
    ts = run["finished"] or run["started"]
    if not ts:
        return ""
    dt = to_local(ts)
    try:
        if schedule_mode() == "interval":
            raise ValueError("no weekday to snap to")
        days = check_days()
        # forward only for a check run EARLY (a day or two before its slot);
        # one that overran into the following days belongs to the day behind
        # it, not the one ahead. Of the ticked days, the nearest wins.
        shifts = []
        for d in days:
            sh = WEEKDAYS.index(d) - dt.weekday()
            if sh > 2:
                sh -= 7
            shifts.append(sh)
        if shifts:
            dt = dt + timedelta(days=min(shifts, key=abs))
    except Exception:
        pass
    return "%s, %d %s %d" % (dt.strftime("%A"), dt.day,
                             dt.strftime("%B"), dt.year)


def _base_url():
    """The public address for links in email - the run's digest is built off
    the request thread when tested, and off nothing when the scheduler fires,
    so a configured PUBLIC_URL wins and the request root is only a fallback."""
    env = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
    if env:
        return env
    try:
        from flask import has_request_context, request as _rq
        if has_request_context():
            return _rq.url_root.rstrip("/")
    except Exception:
        pass
    return "https://iradar.net"
try:
    from zoneinfo import ZoneInfo
    _DISPLAY_ZONE = ZoneInfo(DISPLAY_TZ)
except Exception:                       # no tzdata installed - fall back
    _DISPLAY_ZONE = None


def to_local(s):
    """A stored UTC timestamp as a datetime in the display zone. Accepts the
    T-separated ISO shape too - mention timestamps from the source APIs carry
    it, and "2026-07-27T00:00:00" shown raw reads like a malfunction."""
    dt = datetime.strptime(s.replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")
    dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_DISPLAY_ZONE) if _DISPLAY_ZONE else dt.astimezone()


@app.template_filter("localtime")
def localtime_filter(s):
    """DB timestamps are UTC; show them where the people reading them are."""
    if not s:
        return "—"
    try:
        return to_local(s).strftime("%d %b %Y, %H:%M")
    except (ValueError, TypeError):
        return s

scheduler = BackgroundScheduler()

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------
# ONE CONSTANT HOUR. The hour was a setting nobody had a reason to move: any
# time works as long as it is outside the day, and a check that starts at 03:00
# has the night to finish in before anyone opens the dashboard. Making it a
# field meant a number to choose, a number to get wrong, and a number every
# other piece of code had to read.
SCHEDULE_MODES = ("days", "interval")


def schedule_hour(s=None):
    """The hour the automatic check starts, 0-23."""
    s = s or db.get_settings()
    try:
        return max(0, min(23, int(str(s.get("hour", 3)).strip() or 3)))
    except (TypeError, ValueError):
        return 3


def schedule_mode(s=None):
    """"days" - specific weekdays - or "interval" - every N days."""
    s = s or db.get_settings()
    m = (s.get("schedule_mode") or "").strip().lower()
    return m if m in SCHEDULE_MODES else "days"


def interval_days(s=None):
    """How many days between checks when the cadence is an interval.

    An interval is the honest way to say "every three days" or "every other
    week": a day-of-week list cannot express either, because neither lands on
    the same weekday twice running."""
    s = s or db.get_settings()
    try:
        return max(1, min(60, int(str(s.get("interval_days", 7)).strip() or 7)))
    except (TypeError, ValueError):
        return 7


def check_days(s=None):
    """Which weekdays the automatic check runs, in week order.

    Replaces the old frequency/weekday pair - "weekly on Sunday" and "daily"
    were two shapes of answer to one question, and the reader had to hold both
    to know what would happen. Reads the old settings when the new one is
    unset, so an install that has never opened this page keeps its schedule."""
    s = s or db.get_settings()
    raw = (s.get("check_days") or "").strip()
    if raw:
        days = [d for d in (x.strip().lower()[:3] for x in raw.split(","))
                if d in WEEKDAYS]
        if days:
            return [d for d in WEEKDAYS if d in days]
    freq = (s.get("frequency") or "weekly").lower()
    if freq == "daily":
        return list(WEEKDAYS)
    if freq == "bidaily":
        return [d for d in WEEKDAYS if d in ("mon", "wed", "fri", "sun")]
    wd = (s.get("weekday") or "mon").lower()[:3]
    return [wd if wd in WEEKDAYS else "mon"]


def cycle_hours(days=None, s=None):
    """Half the gap between one scheduled check and the next, in hours.

    The one-check-per-cycle guard needs to know how long a cycle is. On an
    interval that is simply the interval; on weekdays it is the SHORTEST gap
    between two ticked days, wrapping around the end of the week. One day a
    week gives 84h and all seven give 12h, which is what the old frequency
    table hard-coded."""
    s = s or db.get_settings()
    if days is None and schedule_mode(s) == "interval":
        return interval_days(s) * 24 / 2.0
    days = days or check_days(s)
    idx = sorted(WEEKDAYS.index(d) for d in days)
    if len(idx) < 2:
        return 84.0
    gaps = [b - a for a, b in zip(idx, idx[1:])] + [idx[0] + 7 - idx[-1]]
    return min(gaps) * 24 / 2.0


def reschedule():
    s = db.get_settings()
    hour = schedule_hour(s)

    # tz=, not the machine's: 03:00 has to mean 03:00 to the people it runs
    # for, on whatever box this happens to be running on. Without it the
    # schedule silently follows the container and a move to UTC would run the
    # check at 01:00 on the wrong day.
    if schedule_mode(s) == "interval":
        # IntervalTrigger, not cron: "every 3 days" and "every other week" are
        # not expressible as a day-of-week list - neither lands on the same
        # weekday twice running. Anchored at the next occurrence of the chosen
        # hour so the first one is not "N days from whenever you pressed Save".
        n = interval_days(s)
        now = datetime.now(_DISPLAY_ZONE)
        start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if start <= now:
            start += timedelta(days=1)
        trigger = IntervalTrigger(days=n, start_date=start,
                                  timezone=_DISPLAY_ZONE)
    else:
        trigger = CronTrigger(day_of_week=",".join(check_days(s)), hour=hour,
                              minute=0, timezone=_DISPLAY_ZONE)

    scheduler.add_job(_scheduled_check, trigger, id="check",
                      replace_existing=True, misfire_grace_time=3600 * 6)


def _scheduled_check(_retry=0):
    """The cron job, with a retry: run_check returns None when another run
    already holds the lock, and without a retry that whole scheduled check
    would silently vanish (on weekly cadence, a lost week). Re-try every
    15 minutes for up to 12 hours, then give up loudly."""
    # ONE CHECK PER CYCLE. A check run early - by hand, on the Saturday - is
    # this week's check; firing the cron on Sunday would redo nine hours of
    # work over an unchanged site and mail everyone a second, empty report.
    # Skip when a scheduled check already landed within half a cycle, which
    # on a weekly cadence means the last three and a half days.
    try:
        st = db.get_settings()
        half = cycle_hours()
        con = db.connect()
        last = con.execute(
            "SELECT finished FROM runs WHERE kind='scheduled'"
            " AND finished IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        con.close()
        if last and last["finished"]:
            done = datetime.strptime(last["finished"], "%Y-%m-%d %H:%M:%S")
            age = (datetime.utcnow() - done).total_seconds() / 3600.0
            if 0 <= age < half:
                print("[scheduler] this cycle's check already ran %.1fh ago"
                      " - skipping" % age, flush=True)
                return
    except Exception as e:
        print("[scheduler] cycle check failed, running anyway: %s" % e,
              flush=True)
    if runner.run_check(kind="scheduled") is not None:
        return
    if _retry >= 48:
        print("[scheduler] gave up: another run blocked the scheduled check"
              " for 12 hours", flush=True)
        return
    print(f"[scheduler] run in progress - scheduled check retries in 15 min"
          f" ({_retry + 1}/48)", flush=True)
    t = threading.Timer(900, _scheduled_check, kwargs={"_retry": _retry + 1})
    t.daemon = True
    t.start()


@app.route("/cron/run", methods=["GET", "POST"])
def cron_run():
    """Start the weekly check from OUTSIDE the container.

    The scheduler that fires this normally lives inside the app, which is fine
    for a process that runs 24/7 and useless for one that is allowed to sleep:
    a sleeping app fires nothing, and Sunday simply never happens. Measured on
    6 Sep 2026, the container costs about $10.81 a month sitting idle against
    roughly $3 of actual work - it does nine hours of checking a week and is
    billed for all 168. So the app is left free to sleep and an external clock
    (a GitHub Actions cron) wakes it with this request.

    GET as well as POST on purpose: the wake-up is the request itself, and a
    scheduler that can only manage a plain GET should still be able to drive
    it. The key is the whole authentication, so it must be long and random.

    This is deliberately a thin trigger - it hands straight to the same
    _scheduled_check the internal cron calls, so the one-check-per-cycle
    guard, the retry, and every log line behave identically no matter which
    clock started the run. An unknown key gets 404 rather than 403: there is
    no reason to confirm the endpoint exists to somebody guessing.
    """
    key = os.environ.get("CRON_KEY", "")
    sent = (request.headers.get("X-Cron-Key")
            or request.args.get("key", ""))
    if not key or not secrets.compare_digest(sent, key):
        abort(404)
    threading.Thread(target=_scheduled_check, daemon=True).start()
    print("[cron] external trigger accepted", flush=True)
    return jsonify({"ok": True,
                    "note": "weekly check triggered; the cycle guard decides"
                            " whether it actually runs"})


def next_run_text():
    job = scheduler.get_job("check")
    if job and getattr(job, "next_run_time", None):
        return job.next_run_time.strftime("%a %d %b %Y, %H:%M")
    return "not scheduled"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def extract_urls(text):
    return list(dict.fromkeys(URL_RE.findall(text or "")))


def parse_uploaded_file(file_storage):
    """Accepts .xlsx, .csv or .txt and returns every URL found in it."""
    name = (file_storage.filename or "").lower()
    data = file_storage.read()
    urls = []
    if name.endswith(".xlsx"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for cell in row:
                    if cell:
                        urls.extend(extract_urls(str(cell)))
        wb.close()
    else:  # csv / txt / anything text-ish
        urls.extend(extract_urls(data.decode("utf-8", errors="replace")))
    return list(dict.fromkeys(urls))


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
# One set of page sizes for every list on the site - the dashboard, the
# content library and the export table. 20 is the default: a screenful
# you can read, rather than a hundred rows you scroll past.
PER_PAGE_CHOICES = (10, 20, 50, 100)
PER_PAGE = 20


def _per_page(default=None):
    """The page size asked for, if it is one we offer."""
    try:
        n = int(request.args.get("per", default or PER_PAGE))
    except (TypeError, ValueError):
        return default or PER_PAGE
    return n if n in PER_PAGE_CHOICES else (default or PER_PAGE)

def _has(cat):
    return ("EXISTS(SELECT 1 FROM items i WHERE i.url_id=u.id"
            f" AND i.category='{cat}')")


# "No X" is a claim about the PAGE, so it needs a good capture behind it: a
# blank/error capture is unknown (we never saw the page) and a 404 is dead -
# none of those genuinely "has no videos". They stay in the plain table and
# under their own state filters, but never inside a No-X attribute filter.
_KNOWN_GOOD = ("u.last_checked IS NOT NULL AND IFNULL(u.last_status,'')"
               " NOT IN ('blank','error','notlive')")


def _nohas(cat):
    # captured fine, and this category genuinely never appeared
    return _KNOWN_GOOD + " AND NOT " + _has(cat)


def _offered(label):
    """The page OFFERS this section - it is in the section nav or the
    Product-details sub-tabs - whether or not anything is behind it."""
    lit = chr(34) + label.lower() + chr(34)
    return ("(instr(lower(IFNULL(u.components,'')), '" + lit + "') > 0"
            " OR instr(lower(IFNULL(u.detail_tabs,'')), '" + lit + "') > 0)")


def _empty(cat, label):
    """Offered and empty: the section is on the page with nothing in it.

    This used to fall into "No X" alongside pages that never had the
    section at all, which reads them as one finding. They are not: a
    missing section is a page built differently, an empty one is a page
    built right and never filled in - and only the second is a mistake.
    """
    return _KNOWN_GOOD + " AND " + _offered(label) + " AND NOT " + _has(cat)


def _flag(block, key):
    """Filter on a presence-only boolean stored in urls.flags (JSON)."""
    return (f"(u.flags IS NOT NULL AND u.flags<>''"
            f" AND json_extract(u.flags, '$.{block}.{key}') = 1)")


def _noflag(block, key):
    return _KNOWN_GOOD + " AND NOT " + _flag(block, key)


# real monitoring states - what the watcher says about the page (OR-combined)
# One canonical name per state, used everywhere (stats, chips, this dropdown),
# so the same page-state never reads under two different words.
# A hand-picked test set: the 2 richest pages (most components) of every page
# shape. NOT wired to anything since 2026-07-29 on Anas's call - the shortcut
# chip and filter are gone, the list stays for the next time a change needs
# trying on a representative handful before it goes near all 4,000. To bring
# the filter back, re-add a STATUS_FILTERS entry pointing at these ids.
TEST_PAGE_IDS = (283, 144, 167, 92, 97, 155, 1, 3, 5, 1078, 1079, 1608, 1610,
                 4177, 4180, 4203, 4220, 526, 574, 221, 264, 238, 4167, 239,
                 269, 244, 278, 298, 535, 193, 194, 4233)

STATUS_FILTERS = {
    "metaflag":  ("⚠ Ownership mismatch", "IFNULL(u.meta_flag,'')<>''"),
    "changed":   ("Updated", "u.last_status='changed' AND u.active=1"),
    # no 'rejected' state: rejecting a capture means "not this one as the
    # baseline", after which the page is dealt with and reads as unchanged
    "unchanged": ("Unchanged", "u.last_status='unchanged' AND u.active=1"),
    "notlive":   ("Offline (404/410)", "u.last_http IN (404,410)"),
    "blank":     ("Blank page", "u.last_status='blank'"),
    "error":     ("Capture error", "u.last_status='error'"),
    "paused":    ("Paused", "u.active=0"),
    "new":       ("Not captured", "u.last_checked IS NULL"),
}

# What we captured from a page, in two filter groups the dashboard shows
# separately (all AND-combined). Every attribute gets a Has and a No half,
# generated together so the two can never drift apart or one be forgotten.
#   Content    = the actual assets on the page. A video counts wherever it
#                sits - including a video card inside News & highlights.
#   Components = the page's own structural sections, each tracked has/not-here.
#   stem: short, stable key fragment (used in URLs) -> library category, label
_CONTENT_ATTRS = [
    ("docs",       "documents",        "documents"),
    ("videos",     "videos",           "videos"),
    ("images",     "images",           "images"),
]
# section-content we already index. The nav-only components (Product details,
# Diagrams, ...) join this group from urls.components once a re-capture fills it.
_COMPONENT_ATTRS = [
    ("subheadline", "subheadline",     "subheadline"),
    ("design",     "design_resources", "design resources"),
    ("features",   "features",         "key features"),
    ("benefits",   "benefits",         "benefits"),
    ("apps",       "applications",     "applications"),
    ("categories", "categories",       "browse-by-category"),
    ("products",   "products",         "related products"),
    ("news",       "news",             "news & highlights"),
]

ATTR_FILTERS = {
    "checked":  ("Captured", "u.last_checked IS NOT NULL"),
    # "live" means a visitor gets the page. A blank body is its own state (not
    # live, not dead - it gets its own filter), and a blocked/erroring capture
    # is UNKNOWN, not dead: only 404/410 - the server saying "gone" - count as
    # not live. A 403/429/5xx is the CDN blocking us, never a page state.
    "live":     ("Live (HTTP OK)",
                 "u.last_http BETWEEN 200 AND 399"
                 " AND IFNULL(u.last_status,'') NOT IN ('blank', 'error')"),
    "notlive":  ("Not live (404)",
                 "u.last_http IN (404, 410)"),
}
for _stem, _cat, _label in _CONTENT_ATTRS + _COMPONENT_ATTRS:
    ATTR_FILTERS["has" + _stem] = ("Has " + _label, _has(_cat))
    # "Has an empty documents" is not English; this reads for singular and
    # plural labels alike and says exactly what the state is
    ATTR_FILTERS["empty" + _stem] = (
        _label[0].upper() + _label[1:] + " present but empty",
        _empty(_cat, _label))
    # NOT offered at all; the empty ones are their own answer above
    ATTR_FILTERS["no" + _stem] = (
        "No " + _label,
        _nohas(_cat) + " AND NOT " + _offered(_label))
# a video is a video wherever it appears - a video card inside News &
# highlights counts too ("if a video is in the news it should also count").
_VIDEO_SQL = ("EXISTS(SELECT 1 FROM items i WHERE i.url_id=u.id"
              " AND i.category IN ('videos','news'))")
ATTR_FILTERS["hasvideos"] = ("Has videos", _VIDEO_SQL)
ATTR_FILTERS["novideos"] = ("No videos",
                            _KNOWN_GOOD + " AND NOT " + _VIDEO_SQL)
# An EVAL BOARD linked in Design resources. The link is already captured on
# every page; what identifies it is its address, because an evaluation board
# lives under /evaluation-board/ whatever the entry happens to be titled.
# The inverse is the report people actually want - the part pages that offer
# no board to try the part on - so it is guarded on the page having been
# captured, or "no board" would include pages nobody has read yet.
_EVAL_SQL = ("EXISTS(SELECT 1 FROM items i WHERE i.url_id=u.id"
             " AND i.category='design_resources'"
             " AND lower(i.item_url) LIKE '%/evaluation-board/%')")
ATTR_FILTERS["haseval"] = ("Has an eval board linked", _EVAL_SQL)
ATTR_FILTERS["noeval"] = ("No eval board linked",
                          _KNOWN_GOOD + " AND NOT " + _EVAL_SQL)

# EDITORIAL LIMITS. copy_flags is '' until the page has been read under the
# rules, and the JSON of the findings once it has. "Within limits" therefore
# has to be guarded on the column being non-empty, or every page nobody has
# measured yet would be reported as passing.
#
# CLEAN IS '{}'. The findings are a dict keyed by block, so a page with
# nothing wrong stores '{}' - never '[]', which these two filters were
# written against and which nothing has ever stored. "Copy over its limits"
# therefore counted every clean page as an offender (3,625 where 2,357 are
# over) and "Copy within its limits" matched nothing at all. Both empty
# spellings are accepted now so neither can come back.
# ...and one per block, because "something on this page is too long" is not
# the question anyone actually asks. The finding is stored keyed by block, so
# each of these looks for its own key.
for _k, _lbl, _c, _lo, _hi in cmp.COPY_LIMITS:
    ATTR_FILTERS["over_" + _k] = (
        _lbl + " over its limit",
        "(instr(IFNULL(u.copy_flags,''), '\"%s\":') > 0)" % _k)

_COPY_EMPTY = "('', '{}', '[]')"
ATTR_FILTERS["copyover"] = (
    "Copy over its limits",
    "(IFNULL(u.copy_flags,'') NOT IN " + _COPY_EMPTY + ")")
ATTR_FILTERS["copyok"] = (
    "Copy within its limits",
    "(IFNULL(u.copy_flags,'') IN ('{}', '[]'))")

# price is captured text on component pages; "no price" only means something
# once the page has been through component detection ('' = not measured yet)
ATTR_FILTERS["hasprice"] = ("Has price",
                            "u.price IS NOT NULL AND u.price<>''")
ATTR_FILTERS["noprice"] = ("No price",
                           _KNOWN_GOOD +
                           " AND u.components IS NOT NULL AND u.components<>''"
                           " AND IFNULL(u.price,'')=''")
# presence-only blocks (content itself never stored) - each with its inverse
# The three blocks the capture already measures twice over - exists AND
# has_content - so their three states need no new signal, only saying.
# "Has X" means has X with something in it, which is what people read it as.
def _flag3(stem, block, label, empty_label):
    ATTR_FILTERS["has" + stem] = ("Has " + label,
                                  _flag(block, "has_content"))
    ATTR_FILTERS["empty" + stem] = (
        empty_label,
        "(" + _flag(block, "exists") + " AND NOT "
        + _flag(block, "has_content") + ")")
    ATTR_FILTERS["no" + stem] = ("No " + label, _noflag(block, "exists"))


_flag3("table", "product_table", "products table",
       "Products table present but empty")
_flag3("community", "developer_community", "developer community",
       "Developer community present but empty")
# a hero image that "exists" without content IS the site default placeholder,
# so its empty state is named for what you would actually see
_flag3("hero", "hero_image", "hero image", "Has the default hero image")

# Components read from the page's own section-nav / Product-details sub-tabs
# (urls.components / urls.detail_tabs, filled by the component re-capture).
# "No X" is guarded on the column being non-empty: '' = the page has not been
# through component detection yet (unknown is not the same as absent), while
# '[]' = detection ran and found none, which IS a real "No".
def _navjson(col, label):
    labels = label if isinstance(label, tuple) else (label,)
    hit = " OR ".join(f"instr(lower(u.{col}), '\"{l}\"') > 0" for l in labels)
    return f"(u.{col} IS NOT NULL AND u.{col}<>'' AND ({hit}))"


def _no_navjson(col, label):
    labels = label if isinstance(label, tuple) else (label,)
    miss = " AND ".join(f"instr(lower(u.{col}), '\"{l}\"') = 0" for l in labels)
    return (_KNOWN_GOOD +
            f" AND u.{col} IS NOT NULL AND u.{col}<>'' AND {miss}")


_NAV_FILTER_ATTRS = [
    ("buyopts",   "components",  "view buying options", "buying options"),
    ("pdetails",  "components",  "product details",     "product details"),
    ("diagrams",  "components",
     ("diagram", "diagrams", "block diagram", "block diagrams"), "diagram"),
    ("featured",  "components",  "featured products",   "featured products"),
    ("partners",  "components",  "partner solutions",   "partner solutions"),
    ("register",  "components",  "register product",    "register product"),
    ("viewprod",  "components",  "view products",       "view products"),
    ("bom",       "components",  "bill of material",    "bill of material"),
    ("parametrics", "detail_tabs", "parametrics",       "parametrics"),
    ("ordering",  "detail_tabs", "ordering",            "ordering"),
    ("descr",     "detail_tabs", "description",         "description tab"),
]
for _stem, _col, _lbl, _disp in _NAV_FILTER_ATTRS:
    ATTR_FILTERS["has" + _stem] = ("Has " + _disp, _navjson(_col, _lbl))
    ATTR_FILTERS["no" + _stem] = ("No " + _disp, _no_navjson(_col, _lbl))

# One question per dropdown. Content used to open with Captured / Live / Not
# live, which are page STATES and belong under Status, and it carried price but
# not images - so the one group that should have been three things and their
# opposites was four unrelated ones. Content is now the three kinds of asset;
# Components is every component; Status is every state.
CONTENT_KEYS = []
for _stem, _c, _l in _CONTENT_ATTRS:
    CONTENT_KEYS += ["has" + _stem, "empty" + _stem, "no" + _stem]
# an eval board is content a page offers, so it lives with the content -
# these existed as filters for a week with no way to reach them but a
# hand-typed URL, which is a feature only for whoever built it
CONTENT_KEYS += ["haseval", "noeval"]
# ...and the copy limits: one entry per block that can be over
CONTENT_KEYS += ["over_" + _k for _k, *_ in cmp.COPY_LIMITS]
COMPONENT_KEYS = []
for _stem, _c, _l in _COMPONENT_ATTRS:
    COMPONENT_KEYS += ["has" + _stem, "empty" + _stem, "no" + _stem]
for _stem in ("table", "community", "hero"):
    COMPONENT_KEYS += ["has" + _stem, "empty" + _stem, "no" + _stem]
COMPONENT_KEYS += ["hasprice", "noprice"]
# A nav entry is a LINK, not a container: there is nothing inside it to be
# empty, so these two states are the whole truth for them.
for _stem, _col, _lbl, _disp in _NAV_FILTER_ATTRS:
    COMPONENT_KEYS += ["has" + _stem, "no" + _stem]
# Qualifiers, not components: they say something ABOUT a component that is
# already listed above, so they have no meaningful opposite and sit last.
# (hastablerows / hasthreads / herodefault used to live here as one-off
#  qualifiers; each is now the middle state of its own component)
# Page states that live in ATTR_FILTERS rather than STATUS_FILTERS. They are
# still statuses, so they are shown with the statuses - the dropdown renders
# them as attr checkboxes because that is the parameter they filter on.
STATUS_ATTR_KEYS = ["checked", "live"]

# The canonical component registry - the ONE list of trackable components and,
# for each, how we know it is present on a page:
#   "cat"  -> the page has library items in this category
#   "flag" -> urls.flags[block].exists is true
#   "nav"  -> this label appears in the page's own section-nav (urls.components)
#   "tab"  -> this label appears in the Product-details sub-tabs (urls.detail_tabs)
# Body-section components (cat/flag) cover every page type; the nav-only ones
# are the structural sections PTP/EB pages expose that are not library content.
#   key, label, kind, arg
# Fixed display order (the user's spec), applied to BOTH the component chips
# and the page's content sections. A component/section only shows if present.
# The Videos component is intentionally gone: a video lives under News &
# highlights / Trainings or in Design resources, reachable from those links.
COMPONENTS = [
    # the subheadline leads: it is the line directly under the page title, so
    # it is the first thing the page says about itself
    ("subheadline",        "Subheadline",           "cat",  "subheadline"),
    ("hero_image",         "Hero image",            "flag", "hero_image"),
    ("overview",           "Overview",              "cat",  "overview"),
    ("parametrics",        "Parametrics",           "tab",  "parametrics"),
    ("ordering",           "Ordering",              "tab",  "ordering"),
    ("description",        "Description",           "tab",  "description"),
    ("features",           "Key features",          "cat",  "features"),
    ("benefits",           "Benefits",              "cat",  "benefits"),
    ("applications",       "Applications",          "cat",  "applications"),
    ("categories",         "Browse by category",    "cat",  "categories"),
    ("about",              "About",                 "cat",  "about"),
    # PTP pages label it "Diagrams", application pages "Diagram" - one component
    ("diagrams",           "Diagram",               "nav",
     ("diagram", "diagrams", "block diagram", "block diagrams")),
    ("products",           "Related products",      "cat",  "products"),
    ("documents",          "Documents",             "cat",  "documents"),
    ("design_resources",   "Design resources",      "cat",  "design_resources"),
    ("partner_solutions",  "Partner solutions",     "nav",  "partner solutions"),
    ("developer_community","Developer community",   "flag", "developer_community"),
    ("news",               "News & highlights",     "cat",  "news"),
    # product/part-specific components, after the canonical order above
    ("product_table",      "Products table",        "flag", "product_table"),
    ("buying_options",     "View buying options",   "nav",  "view buying options"),
    ("product_details",    "Product details",       "nav",  "product details"),
    ("featured_products",  "Featured products",     "nav",  "featured products"),
    ("register_product",   "Register product",      "nav",  "register product"),
    ("view_products",      "View products",         "nav",  "view products"),
    ("bom",                "Bill of material",      "nav",  "bill of material"),
]
COMPONENT_BY_KEY = {c[0]: c for c in COMPONENTS}
# category name -> component key, for turning captured categories into components
_CAT_TO_COMP = {arg: key for key, _l, kind, arg in COMPONENTS if kind == "cat"}


def _custom_components():
    """User-added components from the settings matrix - detected by their label
    appearing in the page's own section-nav, same as the built-in nav kind."""
    try:
        rows = json.loads(db.get_settings().get("custom_components") or "[]")
    except Exception:
        rows = []
    return [(r["key"], r["label"], "nav", r["label"].lower())
            for r in rows if isinstance(r, dict) and r.get("key") and r.get("label")]


def _all_components():
    return COMPONENTS + _custom_components()


# Until this moment the section-nav list could contain entries the page does
# not display: Infineon ships modules it has nothing for - Documents most
# often - collapsed to zero height, and the extractor read them through a
# textContent fallback. capture.py now requires a nav anchor to be rendered.
# Snapshots older than this were taken with the old rule, so their nav list
# cannot support an "offered but empty" claim; those components read as
# absent, which is what the page actually shows, until they are re-captured.
# The flag-backed blocks (hero image, developer community, products table)
# are unaffected - they measure exists/has_content on the block itself and
# never consult the nav.
NAV_VISIBILITY_FIX = "2026-07-19 11:00:00"


def _page_component_states(components_json, detail_tabs_json, flags, cat_present,
                           registry=None, captured_at=None):
    """Per component: "content", "empty" or "absent".

    Present and empty is a real, common state and it was being read as absent:
    2,285 pages carry Infineon's default placeholder where a hero image should
    be, 333 have a Developer community block with nothing in it, and 91 offer a
    Documents section holding no documents. "No hero image" and "a hero image
    that is the site's placeholder" are different findings - one is missing, the
    other is filled in wrong - and only one of them is anybody's fault.

    How each kind knows:
      cat  - items in the category means content; the page's own section-nav
             offering the section with no items behind it means empty
      flag - the capture already records exists AND has_content per block
      nav  - a nav entry is a link, not a container, so it has no empty state
    """
    def _loadset(j):
        try:
            return {s.lower() for s in json.loads(j or "[]")}
        except Exception:
            return set()
    nav, tabs = _loadset(components_json), _loadset(detail_tabs_json)

    def _in(arg, pool):
        # a component may go by several nav labels ("Diagram" / "Diagrams")
        return (any(a in pool for a in arg) if isinstance(arg, tuple)
                else arg in pool)

    out = {}
    for key, label, kind, arg in (registry if registry is not None
                                  else _all_components()):
        if kind == "cat":
            if arg in cat_present:
                out[key] = "content"
            # nav OR tabs: a page offers Benefits as a Product-details sub-tab,
            # never as a section-nav entry, so checking only the nav missed
            # every empty one of those
            elif ((label.lower() in nav or label.lower() in tabs)
                  and (captured_at or "") >= NAV_VISIBILITY_FIX):
                out[key] = "empty"
            else:
                out[key] = "absent"
        elif kind == "flag":
            b = flags.get(arg) or {}
            out[key] = ("content" if b.get("has_content")
                        else ("empty" if b.get("exists") else "absent"))
        elif kind == "nav":
            out[key] = "content" if _in(arg, nav) else "absent"
        elif kind == "tab":
            out[key] = "content" if _in(arg, tabs) else "absent"
        else:
            out[key] = "absent"
    return out


def _page_component_set(components_json, detail_tabs_json, flags, cat_present,
                        registry=None, captured_at=None):
    """The set of component keys PRESENT on one page - content OR empty.

    Kept as the presence answer for everything that only asks "is it there";
    _page_component_states is the one that distinguishes the two.
    """
    return {k for k, v in _page_component_states(
        components_json, detail_tabs_json, flags, cat_present, registry,
        captured_at).items()
        if v != "absent"}


# the levels that are their own page SHAPE: each gets its own component
# superset - a PC hub is not built like a PEFP end family, an ADP directory
# not like an ASP solution page. Order = display order in the matrix.
_LEVEL_TYPES = ("PC", "PFP", "PSFP", "PEFP", "ADP", "AOP", "ASP", "AFB",
                "DRC", "DRF", "DRSF", "HOME")
ETYPE_ORDER = ["HOME", "PC", "PFP", "PSFP", "PEFP", "PTP", "Evaluation board",
               "DRC", "DRF", "DRSF",
               "ADP", "AOP", "ASP", "AFB",
               "Product", "Application", "Design resources", "Promo"]


def _etype(ptype, url, level=""):
    """The page type the COMPONENT MODEL keys on - one superset per SHAPE:
    products by their level (PC/PFP/PSFP/PEFP), parts split into PTP and
    Evaluation board, applications by their level (ADP/AOP/ASP/AFB). A page
    with no level falls back to its sheet type."""
    if ptype == "Part":
        return "Evaluation board" if "/evaluation-board/" in (url or "") else "PTP"
    if (level or "") in _LEVEL_TYPES:
        return level
    return ptype


PRODUCT_LEVELS = ("PC", "PFP", "PSFP", "PEFP")


def _infer_level(con, row):
    """Work out a page's level from three independent signals and say why.

    1. SIBLINGS (weight 3): pages sharing the same parent URL - the strongest
       look-alike evidence (air-purifiers sits among five ASPs).
    2. HIERARCHY DEPTH (weight 1): the level distribution of same-type pages
       at the same breadcrumb depth.
    3. COMPONENT FINGERPRINT (weight 2): Jaccard similarity of the page's own
       component set against each level's superset - same content, same look.

    Returns (level, confidence 0..1, evidence-text). Parts are typed by URL.
    """
    ptype, url = row["ptype"], row["url"]
    if ptype == "Part":
        lvl = "EB" if "/evaluation-board/" in url else "PTP"
        return lvl, 1.0, "part pages are typed by their URL"
    if ptype == "Design resources":
        lvl = _dr_level_for(url)
        return (lvl, 1.0, "design-resource page type from its URL depth")             if lvl else ("", 0.0, "not a design-resource hierarchy page")
    scheme = (PRODUCT_LEVELS if ptype == "Product"
              else APP_LEVELS if ptype == "Application" else ())
    if not scheme:
        return "", 0.0, f"the {ptype or 'untyped'} type has no level scheme"

    peers = con.execute(
        "SELECT id, url, level, breadcrumb FROM urls"
        " WHERE ptype=? AND level<>'' AND id<>?", (ptype, row["id"])).fetchall()

    def crumb_depth(bc):
        try:
            return len([c for c in json.loads(bc or "[]") if c.get("title")])
        except Exception:
            return 0

    votes = {l: 0.0 for l in scheme}
    why = []
    # 1) siblings
    parent = url.rstrip("/").rsplit("/", 1)[0] + "/"
    sibs = [p for p in peers if p["url"].startswith(parent)
            and "/" not in p["url"][len(parent):].rstrip("/")]
    if sibs:
        w = 3.0 * min(1.0, len(sibs) / 3.0)   # one sibling is a hint, not proof
        for l in scheme:
            n = sum(1 for p in sibs if p["level"] == l)
            votes[l] += w * n / len(sibs)
        top = max(scheme, key=lambda l: sum(1 for p in sibs if p["level"] == l))
        why.append(f"{len(sibs)} sibling(s), mostly {top}")
    # 2) depth peers
    d = crumb_depth(row["breadcrumb"])
    if d:
        dp = [p for p in peers if crumb_depth(p["breadcrumb"]) == d]
        if dp:
            for l in scheme:
                votes[l] += 1.0 * sum(1 for p in dp if p["level"] == l) / len(dp)
            why.append(f"depth-{d} peers lean "
                       + max(scheme, key=lambda l: sum(
                           1 for p in dp if p["level"] == l)))
    # 3) component fingerprint vs each level's superset
    cats = {r["category"] for r in con.execute(
        "SELECT DISTINCT category FROM items WHERE url_id=?", (row["id"],))}
    try:
        flags = json.loads(row["flags"] or "{}")
    except Exception:
        flags = {}
    have = _page_component_set(row["components"], row["detail_tabs"], flags,
                               {c for c in _CAT_TO_COMP if c in cats})
    sups = _components_for_type(con)
    best_j = ("", 0.0)
    for l in scheme:
        sup = sups.get(l) or set()
        if not sup:
            continue
        j = len(have & sup) / (len(have | sup) or 1)
        votes[l] += 2.0 * j
        if j > best_j[1]:
            best_j = (l, j)
    if best_j[0]:
        why.append(f"components fit {best_j[0]} ({best_j[1]:.0%})")

    ranked = sorted(votes.items(), key=lambda kv: -kv[1])
    if not ranked or ranked[0][1] <= 0:
        return "", 0.0, "no evidence available yet (page not captured?)"
    best, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    conf = (top - second) / (top or 1)
    return best, round(conf, 2), "; ".join(why)


def _component_counts(con):
    """{etype: {component_key: n_pages}} - on how many captured pages of each
    (effective) type each component actually appears. The union of a type's
    keys IS its unified component list: the largest set seen across all pages
    of that type, which every page of the type is then measured against. Also
    the data-driven seed for the settings matrix and the prune (count 0 for a
    type = never showed up there). Manual settings edits are layered on top
    (the stored matrix wins once saved)."""
    comps = _all_components()
    out = {}

    def bump(etype, key):
        out.setdefault(etype, {})[key] = out.setdefault(etype, {}).get(key, 0) + 1

    cats_by_url = {}
    for r in con.execute("SELECT DISTINCT url_id, category FROM items"):
        cats_by_url.setdefault(r["url_id"], set()).add(r["category"])
    for r in con.execute(
            "SELECT id, ptype, url, level, flags, components, detail_tabs"
            " FROM urls WHERE ptype<>'' AND last_checked IS NOT NULL"):
        et = _etype(r["ptype"], r["url"], r["level"])
        for cat in cats_by_url.get(r["id"], ()):
            key = _CAT_TO_COMP.get(cat)
            if key:
                bump(et, key)
        try:
            fl = json.loads(r["flags"] or "{}")
        except Exception:
            fl = {}
        for block in ("product_table", "developer_community", "hero_image"):
            if (fl.get(block) or {}).get("exists"):
                bump(et, block)
        def _loadset(j):
            try:
                return {s.lower() for s in json.loads(j or "[]")}
            except Exception:
                return set()
        nav, tabs = _loadset(r["components"]), _loadset(r["detail_tabs"])
        for key, _l, kind, arg in comps:
            pool = nav if kind == "nav" else tabs if kind == "tab" else None
            if pool is None:
                continue
            hit = (any(a in pool for a in arg) if isinstance(arg, tuple)
                   else arg in pool)
            if hit:
                bump(et, key)
    return out


def _components_for_type(con):
    """{ptype: set(component_key)} - which components appear on at least one
    captured page of each type (derived from the counts)."""
    return {t: {k for k, n in d.items() if n}
            for t, d in _component_counts(con).items()}


def _effective_matrix(con):
    """The page-type x component matrix the app answers from: the user's saved
    settings matrix when one exists, else the data-driven seed. {etype: set(key)}.
    An empty stored dict does NOT count as saved - an accidental '{}' once
    blanked every type's unified list and un-unified the url pages."""
    try:
        stored = json.loads(db.get_settings().get("component_matrix") or "")
        if isinstance(stored, dict) and any(stored.values()):
            return {t: set(v) for t, v in stored.items()}
    except Exception:
        pass
    return _components_for_type(con)


def _matrix_autojoin():
    """Keep a SAVED component matrix learning: any component the live data now
    shows on a type but the saved matrix lacks is ADDED (a new section Infineon
    starts publishing must not stay invisible just because the matrix was saved
    before it existed). Removals stay manual - only a human prunes. Returns the
    list of 'etype+key' additions."""
    raw = (db.get_settings().get("component_matrix") or "").strip()
    if not raw:
        return []            # no saved matrix - live union already rules
    try:
        stored = json.loads(raw)
        if not (isinstance(stored, dict) and any(stored.values())):
            return []
    except Exception:
        return []
    con = db.connect()
    live = _components_for_type(con)
    con.close()
    added = []
    for et, keys in live.items():
        have = set(stored.get(et) or [])
        new = keys - have
        if new:
            stored[et] = sorted(have | new)
            added += [f"{et}+{k}" for k in sorted(new)]
    if added:
        db.set_setting("component_matrix", json.dumps(stored))
    return added


def _bulk_counts(con, filters):
    """{key: count} in ONE pass, for filters whose SQL touches only the urls
    row. Deriving these by hand is how Dead-page/Blank-page came to show 0
    while 101 existed; deriving them from the same SQL the filter uses keeps
    the count and the filter in lock-step. Category filters (EXISTS over the
    items table) are NOT put through here - 20+ correlated subqueries per row
    take seconds - they are answered from one GROUP BY in index() instead."""
    conds = {k: v[1] for k, v in filters.items() if "items i" not in v[1]}
    if not conds:
        return {}
    cols = ", ".join(f"SUM(CASE WHEN {c} THEN 1 ELSE 0 END) AS k{i}"
                     for i, c in enumerate(conds.values()))
    row = con.execute(f"SELECT {cols} FROM urls u").fetchone()
    return {k: (row[f"k{i}"] or 0) for i, k in enumerate(conds)}


_DASH_CHG = "(SELECT COUNT(*) FROM changes c WHERE c.url_id=u.id)"
_DASH_ORDER = {
    "page": "u.url COLLATE NOCASE ASC",
    "page_desc": "u.url COLLATE NOCASE DESC",
    "date": "u.last_checked IS NULL, u.last_checked DESC",
    "date_asc": "u.last_checked IS NULL, u.last_checked ASC",
    "changes": _DASH_CHG + " DESC",      # most-updated first
    "changes_asc": _DASH_CHG + " ASC",   # fewest first
}


def _dash_order(sort):
    """The table's ORDER BY. Shared, because a shift-click range that sorted
    differently from the rows on screen would select a different set from the
    one you counted."""
    return _DASH_ORDER.get(sort, _DASH_ORDER["page"])


def _url_filter(q, statuses, ptypes, attrs, bls=(), pls=()):
    """statuses OR'd, types OR'd, attributes AND'd - all multi-select.

    bl and pl are their own dimensions rather than attributes, because they
    hold VALUES rather than yes/no: ticking two business lines has to mean
    "either", the way two types do, and attributes are AND'd. Passed as
    keywords so the callers that do not offer them are unchanged.

    EVERY condition is wrapped in its own parentheses before being combined.
    Several filter conditions contain a top-level OR (e.g. "Not live" is
    404/500 OR blank), and AND binds tighter than OR in SQL, so an unwrapped
    `A AND B OR C` silently became `(A AND B) OR C` - which made combining any
    such filter with another quietly drop every other filter. That is the
    "multiple filters not working" the user saw.
    """
    where, params = [], []
    if q:
        # The page's OWN TITLE, which is the thing on screen and the thing
        # people type. Searching only url/label meant "page not found" matched
        # nothing while 682 pages were titled exactly that - the title lives in
        # the snapshot, so it needs the EXISTS (14 ms on the whole corpus).
        where.append("(u.url LIKE ? OR u.label LIKE ?"
                     " OR u.canonical_url LIKE ?"
                     " OR IFNULL(u.title_override,'') LIKE ?"
                     " OR EXISTS(SELECT 1 FROM snapshots s WHERE s.url_id=u.id"
                     "   AND s.status='ok' AND s.page_title LIKE ?))")
        params += [f"%{q}%"] * 5
    picked = ["(" + STATUS_FILTERS[s][1] + ")"
              for s in statuses if s in STATUS_FILTERS]
    if picked:
        where.append("(" + " OR ".join(picked) + ")")
    types = [t for t in ptypes if t]
    if types:
        # "Part" is split into two selectable types by URL shape: PTP (/part/)
        # and Evaluation board (/evaluation-board/). They are the same report
        # sheet (ptype='Part'), so the split lives here, not in the data.
        # a detail level is a selectable sub-type of its general type - same
        # pseudo-type pattern the PTP/EB split established
        level_of = {l: pt for pt, lvls in (("Product", PRODUCT_LEVELS),
                                           ("Application", APP_LEVELS),
                                           ("Design resources", DR_LEVELS))
                    for l in lvls}
        tconds = []
        for t in types:
            if t == "PTP":
                tconds.append("(u.ptype='Part' AND u.url LIKE '%/part/%')")
            elif t in ("EB", "Evaluation board"):
                tconds.append(
                    "(u.ptype='Part' AND u.url LIKE '%/evaluation-board/%')")
            elif t == "Products & Eval Boards":
                # the merged filter group: the whole product taxonomy,
                # parts included (a part type page is its bottom level)
                tconds.append("u.ptype IN ('Product','Part')")
            elif t in level_of:
                tconds.append("(u.ptype=? AND u.level=?)")
                params.extend([level_of[t], t])
            else:
                tconds.append("u.ptype=?")
                params.append(t)
        where.append("(" + " OR ".join(tconds) + ")")
    # A page with no BL at all is not "some other BL" - it is unassigned, and
    # on a third of pages that is the honest answer. Ticking (none) asks for
    # exactly those, so the filter can never quietly hide them.
    for vals, col in ((bls, "bl"), (pls, "pl")):
        vals = [v for v in vals if v]
        if not vals:
            continue
        conds = []
        for v in vals:
            if v == _UNASSIGNED:
                conds.append(f"IFNULL(u.{col},'')=''")
            else:
                conds.append(f"u.{col}=?")
                params.append(v)
        where.append("(" + " OR ".join(conds) + ")")
    for a in attrs:
        if a in ATTR_FILTERS:
            where.append("(" + ATTR_FILTERS[a][1] + ")")
    return ("WHERE " + " AND ".join(where)) if where else "", params


# the sentinel a "(none)" tick sends, kept out of the value space itself
_UNASSIGNED = "__unassigned__"


def _owner_values(con, col):
    """Every BL (or PL) in use, with how many pages carry it, plus the
    unassigned count. Read from the data rather than configured, so a value
    that appears in a new import needs no code change to become filterable."""
    rows = con.execute(
        f"SELECT IFNULL({col},'') v, COUNT(*) n FROM urls"
        f" GROUP BY v ORDER BY (v='') ASC, n DESC").fetchall()
    out = []
    for r in rows:
        out.append({"v": r["v"] or _UNASSIGNED,
                    "label": r["v"] or "(not assigned)",
                    "n": r["n"]})
    return out


_BAR_EXTRAS_CACHE = {}


def _bar_extras(con):
    """Latest updates/content/files plus inventory and offline deltas.

    Content updates are counted in ITEMS (documents, images, boards) across
    the changes the last finished run opened, exactly as the email counts
    them; inventory includes paused pages, independently of run size. Parsing
    ~1k summaries per page load would be rude, so the result is cached per
    (run, open-change-count) - the count in the key means a review or a new
    change invalidates it without any bookkeeping."""
    run = con.execute("SELECT * FROM runs WHERE finished IS NOT NULL"
                      " ORDER BY id DESC LIMIT 1").fetchone()
    if not run:
        return 0, 0, 0, 0, 0
    open_n = con.execute(
        "SELECT COUNT(*) FROM changes WHERE IFNULL(reviewed,0)=0 AND ts>=?",
        (run["started"],)).fetchone()[0]
    fu = con.execute(
        "SELECT COUNT(*) FROM asset_changes WHERE IFNULL(reviewed,0)=0"
        " AND ts>=?", (run["started"],)).fetchone()[0]
    tracked = con.execute("SELECT COUNT(*) FROM urls").fetchone()[0]
    # A file review or a watch-list edit can happen without changing open_n;
    # both belong in the key so the bar never holds their old numbers.
    key = (run["id"], open_n, fu, tracked)
    # read ONCE into a local: checking membership and then indexing let a
    # second request's clear() land between the two, and the dashboard 500'd
    # with a KeyError on a page that only wanted a cached number
    hit = _BAR_EXTRAS_CACHE.get(key)
    if hit is None:
        rows = con.execute(
            "SELECT summary, text_changed, images_added, images_removed"
            " FROM changes WHERE IFNULL(reviewed,0)=0 AND ts>=?",
            (run["started"],)).fetchall()
        items, _w, _c = mailer._content_items(rows)
        delta = db.run_pages_delta(con, run, tracked)
        offline_delta = db.run_offline_delta(con, run)
        hit = (open_n, sum(items.values()) + fu, fu, delta, offline_delta)
        # one live entry is plenty, and REPLACING the dict (rather than
        # clearing then filling it) means no reader can catch it empty
        _BAR_EXTRAS_CACHE.clear()
        _BAR_EXTRAS_CACHE[key] = hit
    return hit


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    statuses = [s for s in request.args.getlist("status") if s in STATUS_FILTERS]
    sel_types = request.args.getlist("ptype")
    sel_bl = request.args.getlist("bl")
    sel_pl = request.args.getlist("pl")
    attrs = [a for a in request.args.getlist("attr") if a in ATTR_FILTERS]
    sort = request.args.get("sort", "page")
    # Sort by page ADDRESS, not title: the URL is the site's own path, so
    # alphabetical-by-URL keeps the hierarchy grouped ("…/microcontroller/…"
    # rows sit together) AND a newly added page lands in place immediately -
    # its URL is known the moment it is added, long before it has a title.
    order_by = _dash_order(sort)
    # a page added within the last week wears a NEW badge (created_at is UTC).
    # new_badge_since is the user's reset watermark: pages added before it are
    # old content belatedly uploaded and never wear the badge; later adds do.
    from datetime import timedelta as _td
    new_cutoff = ((datetime.now(timezone.utc) - _td(days=7))
                  .strftime("%Y-%m-%d %H:%M:%S"))
    new_cutoff = max(new_cutoff,
                     (db.get_settings().get("new_badge_since") or ""))
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    wsql, params = _url_filter(q, statuses, sel_types, attrs, sel_bl, sel_pl)

    con = db.connect()
    total_match = con.execute(
        f"SELECT COUNT(*) FROM urls u {wsql}", params).fetchone()[0]
    per_page = _per_page()
    pages = max(1, -(-total_match // per_page))
    page = min(page, pages)
    # days on which a big batch was imported (the initial ~4k load) are NOT
    # "new pages you added" - only badge NEW for small, individual additions
    bulk_days = {r["d"] for r in con.execute(
        "SELECT substr(created_at,1,10) d, COUNT(*) n FROM urls"
        " WHERE created_at IS NOT NULL GROUP BY d HAVING n > 50")}
    # The "#" shown is a 1..N ALPHABETICAL rank, not the raw database id: #1 is
    # the alphabetically-first page and the whole column renumbers itself the
    # moment a page is added (the real id/PK never changes - it still keys the
    # checkboxes and links, so nothing downstream breaks).
    ranks = {r["id"]: r["seq"] for r in con.execute(
        "SELECT id, ROW_NUMBER() OVER (ORDER BY url COLLATE NOCASE, id) AS seq"
        " FROM urls")}
    urls = con.execute(f"""
        SELECT u.*,
          (SELECT COUNT(*) FROM changes c WHERE c.url_id=u.id AND c.reviewed=0)
            AS open_changes,
          (SELECT COUNT(*) FROM changes c WHERE c.url_id=u.id) AS change_total,
          (SELECT c2.id FROM changes c2 WHERE c2.url_id=u.id AND c2.reviewed=0
            ORDER BY c2.id DESC LIMIT 1) AS latest_change_id,
          (SELECT c3.components FROM changes c3 WHERE c3.url_id=u.id
            AND c3.reviewed=0 ORDER BY c3.id DESC LIMIT 1) AS latest_components,
          (SELECT s.page_title FROM snapshots s WHERE s.url_id=u.id
            AND s.status='ok' AND s.page_title<>'' ORDER BY s.id DESC LIMIT 1)
            AS page_title
        FROM urls u {wsql}
        ORDER BY {order_by}, u.id
        LIMIT ? OFFSET ?
    """, params + [per_page, (page - 1) * per_page]).fetchall()

    counts = db.dashboard_counts(con)
    last_run = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    total_open = con.execute(
        "SELECT COUNT(*) FROM changes WHERE reviewed=0").fetchone()[0]
    comp_pending = con.execute(
        "SELECT COUNT(*) FROM urls WHERE active=1"
        " AND (components IS NULL OR components='')").fetchone()[0]
    # pages that still NEED a capture (never checked / failed)
    # - the set the "check pending first" button runs, and the front of the
    # queue when checking everything
    pending_total = con.execute(
        "SELECT COUNT(*) FROM urls WHERE active=1 AND (last_checked IS NULL"
        " OR IFNULL(last_status,'') IN ('blank','error'))"
    ).fetchone()[0]
    ptypes_raw = con.execute(
        "SELECT ptype, COUNT(*) n FROM urls WHERE ptype<>'' GROUP BY ptype"
        " ORDER BY n DESC").fetchall()
    # per-level counts, so the Type dropdown can nest each general type's
    # detail levels (Product > PC/PFP/PSFP/PEFP, ...) under it
    lvl_counts = {(r["ptype"], r["level"]): r["n"] for r in con.execute(
        "SELECT ptype, level, COUNT(*) n FROM urls"
        " WHERE ptype<>'' AND IFNULL(level,'')<>'' GROUP BY ptype, level")}
    _SCHEMES = {"Product": PRODUCT_LEVELS, "Application": APP_LEVELS,
                "Design resources": DR_LEVELS}
    # products and parts are ONE taxonomy - a part type page is its bottom -
    # so the filter offers one group, "Products & Eval Boards", with every
    # shape nested inside it: PC/PFP/PSFP/PEFP and then PTP/EB, each with
    # its badge, instead of PTP and EB standing outside alone
    part_total = next((r["n"] for r in ptypes_raw if r["ptype"] == "Part"), 0)
    n_ptp = n_eb = 0
    if part_total:
        n_ptp = con.execute("SELECT COUNT(*) FROM urls WHERE ptype='Part'"
                            " AND url LIKE '%/part/%'").fetchone()[0]
        n_eb = con.execute("SELECT COUNT(*) FROM urls WHERE ptype='Part'"
                           " AND url LIKE '%/evaluation-board/%'").fetchone()[0]
    ptypes, merged_done = [], False
    for r in ptypes_raw:
        if r["ptype"] in ("Part", "Product"):
            if merged_done:
                continue
            merged_done = True
            prod_n = next((x["n"] for x in ptypes_raw
                           if x["ptype"] == "Product"), 0)
            levels = [{"lvl": l, "n": lvl_counts.get(("Product", l), 0)}
                      for l in PRODUCT_LEVELS
                      if lvl_counts.get(("Product", l))]
            if n_ptp:
                levels.append({"lvl": "PTP", "n": n_ptp})
            if n_eb:
                levels.append({"lvl": "EB", "n": n_eb})
            ptypes.append({"ptype": "Products & Eval Boards",
                           "n": prod_n + part_total, "levels": levels})
        else:
            levels = [{"lvl": l, "n": lvl_counts.get((r["ptype"], l), 0)}
                      for l in _SCHEMES.get(r["ptype"], ())
                      if lvl_counts.get((r["ptype"], l))]
            ptypes.append({"ptype": r["ptype"], "n": r["n"], "levels": levels})
    cat_pages, vid_pages, status_counts, attr_counts = _dash_counts(con)
    # read while this connection is still open - it closes on the next line
    bl_opts, pl_opts = _owner_values(con, "bl"), _owner_values(con, "pl")
    con.close()
    con2 = db.connect()
    # content/component has/no counts come from cat_pages: has = pages with the
    # category (identical to the EXISTS filter), no = captured pages without it
    checked = attr_counts.get("checked", 0)
    for _stem, _cat, _ in _CONTENT_ATTRS + _COMPONENT_ATTRS:
        has = cat_pages.get(_cat, 0)
        attr_counts["has" + _stem] = has
        attr_counts["no" + _stem] = checked - has
    # videos filter spans videos + news cards, so its count must too
    attr_counts["hasvideos"] = vid_pages
    attr_counts["novideos"] = checked - vid_pages

    # "Not live" = the server answers 4xx. A blank page is its own state,
    # counted separately - not folded in here.
    notlive_total = status_counts["notlive"]
    # quick chips: the handful of filters worth one-click access, shown only
    # when they actually match something. status kind links ?status=, attr ?attr=
    # NB: the field is "vals", NOT "keys" - a chip is a dict, and `c.keys` in a
    # Jinja template resolves to dict.keys (the method), which silently broke
    # every chip URL. Ordered by what needs you: attention, health, then states.
    # widest set first, narrowing to the specialised ones - the eye lands on
    # the whole corpus and walks down to the exceptions
    # no "everything" chip: an unfiltered table is the table's normal state,
    # and the stat card above already carries the total
    quick_chips = [c for c in (
        {"kind": "attr", "vals": ["live"], "label": "Live",
         "n": attr_counts["live"], "tone": "good"},
        {"kind": "status", "vals": ["unchanged"], "label": "Unchanged",
         "n": status_counts["unchanged"], "always": True},
        {"kind": "status", "vals": ["changed"], "label": "Updated",
         "n": status_counts["changed"], "always": True, "tone": "warn"},
        {"kind": "status", "vals": ["blank"], "label": "Blank page",
         "n": status_counts["blank"], "tone": "bad"},
        {"kind": "status", "vals": ["error"], "label": "Capture error",
         "n": status_counts["error"], "tone": "bad"},
        {"kind": "status", "vals": ["notlive"], "label": "Not live",
         "n": notlive_total, "tone": "bad"},
        {"kind": "status", "vals": ["new"], "label": "Not captured",
         "n": status_counts["new"]},
        {"kind": "status", "vals": ["paused"], "label": "Paused",
         "n": status_counts["paused"]},
        {"kind": "status", "vals": ["metaflag"], "label": "Ownership mismatch",
         "n": status_counts["metaflag"], "tone": "warn"},
    ) if c["n"] or c.get("always")]
    # A chip is one more filter, not a fresh start. It used to link to
    # ?status=x with everything else dropped, so picking a type and then a chip
    # silently threw the type away - and the chip could never light up while
    # any other filter was set, so it did not even look selected.
    for c in quick_chips:
        cur = statuses if c["kind"] == "status" else attrs
        c["on"] = all(v in cur for v in c["vals"])
        nxt = ([v for v in cur if v not in c["vals"]] if c["on"]
               else cur + [v for v in c["vals"] if v not in cur])
        c["href"] = url_for(
            "index", q=q or None, ptype=sel_types or None,
            status=(nxt if c["kind"] == "status" else statuses) or None,
            attr=(nxt if c["kind"] == "attr" else attrs) or None)

    # The dropdown numbers are the FACETED ones, worked out here rather than
    # left to the browser to correct after paint. Shipping corpus-wide counts
    # and fixing them by fetch meant opening Content with Blank page selected
    # showed 3,024 documents for one frame - and for longer than a frame if you
    # were quick. The chips keep the corpus-wide counts: a chip says how many
    # pages are in that state, not how many are left after the other filters.
    try:
        facets = _dash_facets(con2, q, statuses, sel_types, attrs,
                              sel_bl, sel_pl)
    finally:
        con2.close()
    con3 = db.connect()
    try:
        asset_open = con3.execute(
            "SELECT COUNT(*) FROM asset_changes WHERE reviewed=0"
        ).fetchone()[0]
        # the Mentions tile appears once anything has been collected
        mention_total = con3.execute(
            "SELECT COUNT(*) FROM mentions").fetchone()[0]
        latest_updates, content_updates, file_updates, pages_delta, offline_delta = \
            _bar_extras(con3)
    finally:
        con3.close()
    return render_template("index.html", urls=urls, state=runner.RUN_STATE,
                           asset_open=asset_open, mention_total=mention_total,
                           latest_updates=latest_updates,
                           content_updates=content_updates,
                           file_updates=file_updates,
                           pages_delta=pages_delta,
                           offline_delta=offline_delta,
                           facets=facets,
                           bls=bl_opts, pls=pl_opts,
                           sel_bl=sel_bl, sel_pl=sel_pl,
                           unassigned=_UNASSIGNED,
                           last_run=last_run, total_open=total_open,
                           next_run=next_run_text(), settings=db.get_settings(),
                           secs_per_page=seconds_per_page(),
                           q=q, page=page, pages=pages, sort=sort,
                           new_cutoff=new_cutoff, bulk_days=bulk_days, ranks=ranks,
                           notlive_total=notlive_total, comp_pending=comp_pending,
                           pending_total=pending_total,
                           level_names=LEVEL_NAMES,
                           total_match=total_match, counts=counts,
                           per_page=per_page, per_choices=PER_PAGE_CHOICES,
                           ptypes=ptypes,
                           sel_status=statuses, sel_types=sel_types,
                           sel_attrs=attrs, status_filters=STATUS_FILTERS,
                           attr_filters=ATTR_FILTERS,
                           content_keys=CONTENT_KEYS, component_keys=COMPONENT_KEYS,
                           status_attr_keys=STATUS_ATTR_KEYS,
                           sel_content_n=sum(1 for a in attrs if a in CONTENT_KEYS),
                           sel_comp_n=sum(1 for a in attrs if a in COMPONENT_KEYS),
                           status_counts=status_counts, attr_counts=attr_counts,
                           quick_chips=quick_chips)


@app.route("/urls/bulk", methods=["POST"])
def urls_bulk():
    import shutil
    action = request.form.get("action", "")
    nxt = request.form.get("next") or url_for("index")
    try:
        ids = [int(i) for i in request.form.getlist("ids")]
    except ValueError:
        ids = []
    if request.form.get("all_matching") == "1":
        wsql, params = _url_filter(request.form.get("q", "").strip(),
                                   request.form.getlist("status"),
                                   request.form.getlist("ptype"),
                                   request.form.getlist("attr"),
                                   request.form.getlist("bl"),
                                   request.form.getlist("pl"))
        con0 = db.connect()
        ids = [r["id"] for r in
               con0.execute(f"SELECT u.id FROM urls u {wsql}", params)]
        con0.close()
    if not ids:
        flash("No URLs selected — tick the boxes first.")
        return redirect(nxt)
    marks = ",".join("?" * len(ids))
    con = db.connect()
    if action == "delete":
        con.execute(f"DELETE FROM urls WHERE id IN ({marks})", ids)
        # FTS has no FK cascade - purge with the rows (see urls_delete)
        con.execute(f"DELETE FROM page_fts WHERE rowid IN ({marks})", ids)
        con.commit()
        for uid in ids:
            shutil.rmtree(db.SNAP_DIR / str(uid), ignore_errors=True)
            shutil.rmtree(db.CHANGE_DIR / str(uid), ignore_errors=True)
        flash(f"Removed {len(ids)} URL(s) and their stored snapshots.")
    elif action in ("pause", "resume"):
        con.execute(f"UPDATE urls SET active=? WHERE id IN ({marks})",
                    [1 if action == "resume" else 0] + ids)
        con.commit()
        flash(f"{'Resumed' if action == 'resume' else 'Paused'} {len(ids)} URL(s).")
    elif action == "accept":
        con.execute(f"UPDATE changes SET reviewed=1, resolution='approve'"
                    f" WHERE url_id IN ({marks}) AND reviewed=0", ids)
        con.execute(f"UPDATE urls SET last_status='unchanged'"
                    f" WHERE id IN ({marks}) AND last_status IN ('changed','rejected')",
                    ids)
        con.commit()
        flash(f"Approved updates on {len(ids)} URL(s) — marked unchanged.")
    elif action == "check":
        if runner.run_check_background(kind="manual", url_ids=ids):
            flash(f"Checking {len(ids)} selected URL(s) now…")
        else:
            flash("A check is already running — try again when it finishes.")
    else:
        flash("Unknown bulk action.")
    con.close()
    return redirect(nxt)


@app.route("/urls/add", methods=["POST"])
def urls_add():
    urls = extract_urls(request.form.get("urls", ""))
    added, skipped = db.add_urls(urls)
    if added:
        # a new Product/Design-resources URL can be leveled from its URL alone
        # - run the structure walk NOW so the page wears its level from the
        # moment it appears, not after the next run happens to finish
        _rebuild_structure()
    flash(f"Added {added} URL(s). {skipped} duplicate(s)/invalid skipped.")
    return redirect(url_for("index"))


@app.route("/urls/upload", methods=["POST"])
def urls_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.")
        return redirect(url_for("index"))
    try:
        urls = parse_uploaded_file(f)
    except Exception as exc:
        flash(f"Could not read file: {exc}")
        return redirect(url_for("index"))
    added, skipped = db.add_urls(urls)
    if added:
        _rebuild_structure()   # same rule as single add: level at add time
    flash(f"File contained {len(urls)} URL(s): added {added}, skipped {skipped} duplicate(s).")
    return redirect(url_for("index"))


@app.route("/urls/<int:url_id>/delete", methods=["POST"])
def urls_delete(url_id):
    import shutil
    con = db.connect()
    con.execute("DELETE FROM urls WHERE id=?", (url_id,))
    # page_fts is an FTS5 virtual table - no FK cascade reaches it, and url ids
    # are not AUTOINCREMENT, so a reused id would inherit the deleted page's
    # text in search. Purge it with the row.
    con.execute("DELETE FROM page_fts WHERE rowid=?", (url_id,))
    con.commit()
    con.close()
    shutil.rmtree(db.SNAP_DIR / str(url_id), ignore_errors=True)
    shutil.rmtree(db.CHANGE_DIR / str(url_id), ignore_errors=True)
    flash("URL removed (including its stored snapshots).")
    return redirect(url_for("index"))


@app.route("/urls/<int:url_id>/toggle", methods=["POST"])
def urls_toggle(url_id):
    con = db.connect()
    con.execute("UPDATE urls SET active = 1 - active WHERE id=?", (url_id,))
    con.commit()
    con.close()
    return redirect(url_for("index"))


# note: capture also stores an "about" category in sections.json; it is
# deliberately NOT listed here - the user wants About content tracked for
# text comparison only, never displayed as a library section.
# Content sections in the fixed display order. Videos has NO component pill
# (see COMPONENTS) but keeps its content section - some pages carry a video
# outside News/Trainings. It sits after News & highlights, before Images;
# Trainings & webinars sits just below Design resources & boards.
SECTION_LABELS = [
    # the subheadline is the line directly under the page title - the first
    # thing written on the page, so the first thing listed about it
    ("subheadline", "Subheadline"),
    ("overview", "Overview"),
    ("features", "Key features"),
    ("benefits", "Benefits"),
    ("applications", "Applications"),
    ("categories", "Browse by category"),
    ("products", "Related products"),
    ("documents", "Documents"),
    ("design_resources", "Design resources & boards"),
    ("trainings", "Trainings & webinars"),
    ("partners", "Partner solutions"),
    ("news", "News & highlights"),
    ("videos", "Videos"),
    ("images", "Images"),
]

# ---- the search page's pills: the user's fixed taxonomy, not raw categories.
# A pill is (key, label, match-fn over a cached item row); fn None = special
# ("all" = every item, "pages" = the tracked pages themselves).
def _doclike_url(u):
    """The document DELIVERY systems - a /gated/ whitepaper or an item served
    from the /documents/ store IS a document. A bare .pdf extension is NOT
    enough: format is not type (user's rule)."""
    u = (u or "").lower()
    return "/gated/" in u or "/documents/" in u


# design-resources groups whose members ARE documents by type - PCB design
# data and simulation models sit on Infineon's own document-type sheet
_DOC_TYPED_GRP = re.compile(
    r"^(pcb design data|simulation models?|documents?|development tools)$",
    re.I)


def _doc_typed(d):
    """A design-resources item that is a DOCUMENT by its TYPE - its group is a
    document type (PCB design data, Simulation models). Format is never the
    criterion: a PDF or ZIP in a tools pane stays a tool (user's rule)."""
    return bool(_DOC_TYPED_GRP.match((d.get("grp") or "").strip()))


def _videolike(d):
    r"""A news/trainings card that is really a video.

    Four signals, any one of which is enough: an .mp4, a /videos/ path, an
    m:ss duration, or the site's own "Video" label as a field of the meta line.

    It used to accept any /is/content/ URL, which is just Infineon's asset
    host - it serves stills and documents too, so that read "delivered by the
    CDN" as "is a video". The last signal is what the loose rule was standing
    in for, and it is exact: three gated items say Video in the meta and have
    no direct URL at all to look at, so nothing else would have found them.
    """
    u = (d.get("url") or "").lower()
    m = d.get("meta") or ""
    return (".mp4" in u or "/videos/" in u
            or bool(re.search(r"\d{1,2}:\d{2}", m))
            or "video" in [f.strip().lower() for f in m.split("|")])


SEARCH_PILLS = [
    ("all",      "Everything",       None),
    ("pages",    "Live Pages",       None),
    ("docs",     "Documents",
     lambda d: d["cat"] == "documents"
     or (d["cat"] == "design_resources"
         and (_doclike_url(d.get("url")) or _doc_typed(d)))),
    ("videos",   "Videos",
     lambda d: d["cat"] == "videos"
     or (d["cat"] in ("news", "trainings") and _videolike(d))),
    ("images",   "Images", lambda d: d["cat"] == "images"),
    # boards ARE design resources - they were a sub-slice of the same category,
    # so two tiles split one library and the counts overlapped
    ("design",   "Design resources & boards",
     lambda d: d["cat"] == "design_resources"),
    ("products", "Related products", lambda d: d["cat"] == "products"),
]
PILL_FN = {k: f for k, _l, f in SEARCH_PILLS}
PILL_LABELS = {k: l for k, l, _f in SEARCH_PILLS}
PILL_LABELS["text"] = "In page text"   # a mode without a tile still needs a name
# categories each pill can live in - lets the SQL fallback prefilter
PILL_SQL_CATS = {
    "design": ("design_resources",), "docs": ("documents", "design_resources"),
    "images": ("images",), "products": ("products",),
    "videos": ("videos", "news", "trainings"),
}
# page-section PROSE (key features, benefits, applications) is no content
# type: it lives in the page's text, and the In-page-text search names the
# section a match sits in. Keeping them as asset chips doubled every answer.
PROSE_CATS = ("features", "benefits", "applications", "subheadline")
# a page's own words rather than a shared asset: never deduped across pages by
# title, because two pages saying the same sentence are not sharing an object
NON_ASSET_CATS = frozenset(PROSE_CATS) | {"about", "overview"}
# old links used raw category names - keep them working. Boards folded INTO
# design resources, so that one redirects. "text" is different: it lost its
# tile because Everything now searches page text too, but it is still a real
# mode - a page-text-only view with no assets mixed in - so it stays reachable
# and must NOT be aliased to "all", or it starts answering with items.
_LEGACY_PILL = {"documents": "docs", "design_resources": "design",
                "news": "videos", "trainings": "videos",
                "boards": "design",
                "features": "text", "benefits": "text",
                "applications": "text"}


# real search modes that have no tile of their own: page text lost its tile to
# Everything but is still its own view, so it has to survive _norm_pill
UNTILED_MODES = {"text"}


def _norm_pill(cat):
    cat = (cat or "all").strip()
    if cat in PILL_FN or cat in UNTILED_MODES:
        return cat
    return _LEGACY_PILL.get(cat, "all")


_TM_RE = re.compile("[™®©]")   # (tm) (r) (c)


def _searchable(t):
    """One normal form for search: lowercase, trademark marks gone, and every
    dash/underscore/dot/slash a space - so 'psoc 6' finds both the page titled
    'PSOC(tm) 6' and the url .../psoc-6-m4-mcu, and 'asic hero image' finds
    asic-hero-image.png."""
    t = _TM_RE.sub("", (t or "").lower())
    t = re.sub(r"[-_/.:]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# an image whose title is this junk gets a readable one from its URL instead
_JUNK_TITLES = {"", "name", "image", "img", "logo", "icon", "banner",
                "picture", "photo", "thumbnail"}


_SLOT_ALT = re.compile(
    r"^(banner|hero|carousel|application-product-carousel|block\s*diagram|"
    r"blockdiagram|placeholder|background|bg)([-_ ]?image)?$", re.I)


def pretty_image_title(title, url):
    """Images should read like documents: 'Biometric Payment Hero', not
    'banner-image' or 'asic-hero-image.png:asset-680-2x1'.

    A real alt-text sentence is kept. But a junk word ('banner'), a
    slot-describing alt ('banner-image', 'application-product-carousel-image')
    or any hyphen-slug that names the SLOT not the picture yields to the
    image's own filename - the name after the last '/' and before the
    extension - which is what actually identifies the asset.
    """
    t = (title or "").strip()
    # a block/architecture diagram (alt 'blockdiagram-image', file
    # 'infineon-13452-blockdiagram-en.svg') reads as 'Block Diagram', not the
    # part number in its filename
    if re.search(r"block[-_ ]?diagram", (t + " " + (url or "")), re.I):
        return "Block Diagram"
    slug = " " not in t and bool(re.search(r"[-_]", t))
    if (t.lower() in _JUNK_TITLES or len(t) < 3 or slug
            or _SLOT_ALT.match(t)):
        seg = asset_name("images", url)                # filename, no ext/query
        nice = re.sub(r"[-_]+", " ", seg).strip().title()
        if nice:
            return nice
    return t or "Image"


# --------------------------------------------------------------------------
# Where an asset lives in the backend (from Infineon's DAM/SAMI migration
# sheet). Images and videos are always DAM; documents split by document type.
# SAMI is Windchill now, so the SAMI list is reported under that name.
DAM_DOC_TYPES = [
    "Additional Product Information", "Application Examples", "Application Brief",
    "Customer Connector", "FAQ Document", "Application Presentation", "Article",
    "Application Brochure", "Editorials", "Fighting Guide", "Product Catalogue",
    "Product Family Overview", "Product Selection Guide", "Product Brief",
    "Product Brochure", "New Product Introduction", "Product Presentation",
    "Presentations", "Product Information", "Product Overview", "Tool Brief",
    "Roadmap", "Success Story", "Whitepaper", "Training", "Webinar",
]
WINDCHILL_DOC_TYPES = [        # the sheet's "SAMI Documents" column
    "Additional Technical Information", "PCB Design Data", "Application Notes",
    "User Manual", "Packing", "Block Diagram", "Code Example",
    "Target Data Sheet", "Software", "Certificates", "Simulation Tool",
    "Simulation Models", "Data Sheet", "Development Tools", "Errata Sheet",
    "Software Module Datasheets", "Delta Sheet", "Simulation Data",
    "Reference Design", "Product Qualification Report",
    "Programming Specifications", "FIT Report", "Getting Started",
    "Preliminary Data Sheet", "Firmware", "Footprint",
    "Material Content Data Sheet", "Package Data", "Material Content Sheet",
    "Package",
]

ALWAYS_DAM = {"images", "videos"}


def _norm_doc_type(t):
    """Match the sheet's wording to the site's.

    The sheet says "Data Sheet" and "Application Notes"; the pages render
    "Datasheet" and "Application note". Collapse spacing/punctuation and a
    plural 's' so the two vocabularies meet.
    """
    s = re.sub(r"[^a-z0-9]+", "", (t or "").lower())
    return s[:-1] if s.endswith("s") and len(s) > 3 else s


DOC_TYPE_SYSTEM = {}
for _t in DAM_DOC_TYPES:
    DOC_TYPE_SYSTEM[_norm_doc_type(_t)] = "DAM"
for _t in WINDCHILL_DOC_TYPES:
    DOC_TYPE_SYSTEM[_norm_doc_type(_t)] = "Windchill"


def is_video_item(category, url="", meta=""):
    """A video, wherever it sits: the videos category itself, OR a card under
    News & highlights / Trainings that is really a video (media URL or a
    m:ss duration). Such a video lives in DAM like any other."""
    return category == "videos" or (
        category in ("news", "trainings")
        and _videolike({"url": url, "meta": meta}))


def backend_system(category, meta, doc_type=None, grp=None, url=""):
    """DAM / Windchill / '' (type not on the migration sheet).

    Per document, never per page: one product page carries a datasheet
    (Windchill) and a product brief (DAM) side by side. Design-resources
    items whose group is a document type (Development tools, PCB design
    data, Simulation models) are documents living in Windchill - per the
    user's own mapping.

    doc_type is the row's own Type column, captured as its own field. The meta
    fallback exists only for snapshots taken before that was stored.
    """
    if category in ALWAYS_DAM or is_video_item(category, url, meta):
        return "DAM"
    if category == "design_resources":
        return "Windchill" if _DOC_TYPED_GRP.match((grp or "").strip()) else ""
    if category != "documents":
        return ""
    return DOC_TYPE_SYSTEM.get(
        _norm_doc_type(doc_type or document_type_of(meta)), "")


@app.template_filter("aemlink")
def _aemlink(page_url, base):
    """The AEM author link for a public page. '{path}' in the editable base
    marks where the page's hierarchy path goes. The path comes from the LONG
    (canonical) URL the caller hands in - the breadcrumb's own address - never
    a shortened link; a language prefix (/de/, /ja/) is dropped because the
    author tree is language-masters/en. Pages living off www.infineon.com
    (softwaretools, …) get no link at all: their authoring is different."""
    base = (base or "").strip()
    u = db.strip_lang(page_url or "")
    try:
        parts = urlsplit(u)
        host, path = parts.netloc.lower(), parts.path.rstrip("/")
    except Exception:
        host, path = "", ""
    if not base or host not in ("www.infineon.com", "infineon.com"):
        return ""
    # the HOMEPAGE is the one page whose path is empty: its author node is
    # the language root itself, so the link simply ends language-masters/en
    # (with the base's own .html suffix), no path segment appended
    # a /gated/ or /document-promo/ address IS a document, not an authored
    # page - there is no AEM editor behind it (the _uuid suffix is the tell)
    low = path.lower()
    if (low.startswith("/gated/") or low.startswith("/document-promo/")
            or re.search(r"_[0-9a-f]{8}-[0-9a-f]{4}-", low)):
        return ""
    return base.replace("{path}", path) if "{path}" in base else base + path


_OFFICE_EXT = {"ppt", "pptx", "doc", "docx", "xls", "xlsx", "xlsm"}
_IMG_EXT = {"svg", "png", "jpg", "jpeg", "gif", "webp"}
_ARCHIVE_EXT = {"zip", "gz", "tgz", "xz", "bz2", "rar", "7z"}


def doc_preview(url):
    """(kind, src) for previewing a file in the browser: pdf/txt render
    natively in an iframe, images inline, Office files through Microsoft's
    public embed viewer (the file URL is passed to view.officeapps.live.com -
    public assets only), zips get a file card, gated documents a lock."""
    u = (url or "").split("#")[0]
    low = u.lower()
    if not u:
        return "", ""
    if "/gated/" in low:
        return "gated", ""
    tail = low.split("?")[0].rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1] if "." in tail else ""
    if ext == "pdf":
        return "pdf", u
    if ext in _IMG_EXT:
        return "img", u
    if ext == "txt":
        return "txt", u
    if ext in _OFFICE_EXT:
        # rendered to a front-page IMAGE by our own server (LibreOffice ->
        # PDF -> PNG), not an interactive/external embed. The label rides
        # along for the fallback card when no image can be made.
        return "office", ext.upper()
    if ext in _ARCHIVE_EXT:
        return "zip", ext.upper()
    # web pages (php/cfm/html/aspx tool links) get no card - they are links,
    # not files. Every other real FILE gets an honest type card.
    if ext and ext not in ("php", "cfm", "html", "htm", "aspx", "jsp"):
        return "file", ext.upper()
    return "", ""


def document_type_of(meta):
    """Fallback only: the first meta field is the Type column by convention.
    Prefer the captured doc_type - position is what a "New" badge broke."""
    return (meta or "").split("|")[0].strip()


# trailing language token on document slugs (…-usermanual-EN) - dropped so the
# backend search finds the asset, not one translation of it
_LANG_SUFFIX = re.compile(
    r"-(en|de|ja|jp|cn|zh|tw|ko|kr|fr|it|es|pt|ru)$", re.I)


def asset_name(category, url):
    """The name to search the backend for, read from the asset's own URL.

    videos:  the last path segment as-is
             …/is/content/infineon/security-solutions-…-optiga-tpm
    images:  last segment, before the first '.' (rendition params follow)
             …/asic-hero-image.png:asset-680-2x1?qlt=… -> asic-hero-image
    gated documents:  last segment, '_<uuid>' cut, language dropped
             …/gated/…-applicationnotes-en_4c53… -> …-applicationnotes
    public documents: last segment, extension and language dropped
             …/documents/10/44/…-usermanual-en.pdf -> …-usermanual
    """
    if not url:
        return ""
    seg = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    seg = seg.split(":", 1)[0]                     # image rendition suffix
    if category == "documents" and "/gated/" in url:
        seg = seg.split("_", 1)[0]                 # uuid rides after '_'
    seg = seg.split(".", 1)[0]                     # extension / .png:…
    if category == "documents":
        seg = _LANG_SUFFIX.sub("", seg)
    return seg


def backend_search_link(backend, name, settings):
    """The backend's search page for this asset, built from the EDITABLE
    settings URL (Settings -> Backends) so a changed DAM/Windchill link is a
    ten-second fix, not a code change. '{q}' marks where the name goes. A URL
    WITHOUT '{q}' opens as-is - for backends like Infineon's AEM omnisearch
    that keep the query out of the URL entirely; there the click also copies
    the asset name, so it is one paste away."""
    if not name or not backend:
        return ""
    key = {"DAM": "dam_search_url",
           "Windchill": "windchill_search_url"}.get(backend)
    base = (settings.get(key) or "").strip() if key else ""
    if not base:
        return ""
    return base.replace("{q}", urlquote(name, safe=""))


def _load_sections(snap_dir):
    p = Path(snap_dir) / "sections.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cats_present_for_type(con, ptype):
    """Which library categories this page TYPE ever has anything in.

    Returns None while the type is still being captured: an empty category on
    a half-captured type means "not yet", not "never". Once every page of the
    type has been checked, a category that is 0 across all of them genuinely
    does not exist on that shape of page (Parts have no benefits), so the
    library stops showing an empty row for it. One page with one entry is
    enough to keep it - this is measured, never hardcoded.
    """
    row = con.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN last_checked IS NULL THEN 1 ELSE 0 END)"
        " pending FROM urls WHERE active=1 AND ptype=?", (ptype,)).fetchone()
    if not row or not row["n"] or row["pending"]:
        return None
    return {r["category"] for r in con.execute(
        "SELECT DISTINCT i.category FROM items i JOIN urls u ON u.id=i.url_id"
        " WHERE u.ptype=?", (ptype,))}


# Infineon's page levels, from the source report's own "Page Type" column.
# A part IS a part type page and belongs inside the product tree - PC > PFP >
# PSFP > PEFP > PTP is one taxonomy, not two. Never inferred from URL depth:
# the sheet has PEFPs at 2, 3, 4 and 5 path segments.
LEVEL_NAMES = {
    "PC": "Product category", "PFP": "Product family",
    "PSFP": "Product sub-family", "PEFP": "Product end family",
    "PTP": "Part type page", "EB": "Evaluation board",
    "HOME": "Homepage — the site root",
    "ADP": "Application division", "AOP": "Application overview",
    "ASP": "Application solution", "AFB": "Application focus",
    "DRC": "Design resource category", "DRF": "Design resource family",
    "DRSF": "Design resource sub-family",
}
# the levels that belong to the application taxonomy (report-sourced, never
# from URL depth - the sheet is the authority for where an app page sits)
APP_LEVELS = ("ADP", "AOP", "ASP", "AFB")
# design-resource page types, from the design system (Components Design
# resources.txt): Category > Family > Sub-Family. No report ground-truth, so
# assigned by URL depth and correctable in the level editor.
DR_LEVELS = ("DRC", "DRF", "DRSF")
# only pages inside these hierarchies are DR type-pages. A softwaretools tool
# page or the homepage is Design-resources by sheet, but not a hierarchy page,
# so it wears no level (user: "these don't get any tags").
def _dr_level_for(url):
    return db.dr_level_for(url)
# page types that share one tree, in the order their levels nest
MERGED_TABS = [("Products & Eval Boards", ["Product", "Part"])]


def _hierarchy_tabs(con):
    """Tabs, discovered from the data: one per page type, except that parts
    are folded into the product tree rather than standing alone."""
    types = [r["ptype"] for r in con.execute(
        "SELECT ptype, COUNT(*) n FROM urls WHERE active=1 AND ptype<>''"
        " GROUP BY ptype ORDER BY n DESC")]
    # the homepage is no tab: every hierarchy starts from it, so it renders
    # at the TOP of each tree instead of standing alone
    types = [t for t in types if t != "Home"]
    tabs, merged = [], set()
    for label, members in MERGED_TABS:
        present = [t for t in members if t in types]
        if present:
            tabs.append((label, present))
            merged.update(present)
    tabs += [(t, [t]) for t in types if t not in merged]
    return tabs


def _url_ancestor(a, b):
    """True when path `a` is `b` itself or a segment-wise ancestor of it.

    Segment-wise so /microcontroller/legacy never counts as an ancestor of
    /microcontroller/legacy-mcus. Query/fragment and trailing slash are
    ignored; comparison is case-insensitive.
    """
    norm = lambda u: (u or "").split("#")[0].split("?")[0].rstrip("/").lower()
    a, b = norm(a), norm(b)
    return bool(a) and (a == b or b.startswith(a + "/"))


_PROD_ROOT = "/products/"


def _product_url(u):
    """The URL if it addresses a product taxonomy node (a non-empty path under
    /products/), else ''. Original case is preserved for linking."""
    n = (u or "").split("#")[0].split("?")[0].rstrip("/")
    i = n.lower().find(_PROD_ROOT)
    return n if (i != -1 and n[i + len(_PROD_ROOT):]) else ""


def _taxonomy_url(ptype, url, crumb_urls):
    """The product-taxonomy URL whose path segments define this page's place.

    A product's own URL IS its address (/products/cat/family/...). A part's URL
    is flat (/part/CY8C20110-LDX2I), so its place is the deepest product URL in
    its breadcrumb - the family it belongs to. Everything else (applications,
    design resources) keeps its own trail and returns ''.
    """
    if ptype == "Product":
        return _product_url(url)
    if ptype == "Part":
        best = ""
        for u in crumb_urls:
            pu = _product_url(u)
            if pu and len(pu) > len(best):
                best = pu
        return best
    return ""


def _url_trail(tax, title_by_url):
    """Rebuild an ordered (titles, urls) trail from a taxonomy URL's segments.

    The site's breadcrumb is unreliable - it both DROPS real ancestors and
    SPLICES in cross-links that are not on the URL path - so the same family
    lands at different depths and renders two or three times at two or three
    levels. The URL path is the one consistent signal, so the product tree is
    built from it; breadcrumb text only supplies each node's display name
    (harvested across every page), falling back to the prettified segment.
    """
    m = re.search(r"(?i)(.*?/products)/(.+)", tax)
    if not m:
        return [], []
    base, rest = m.group(1), m.group(2)
    path, urls, acc = [], [], base
    for seg in [s for s in rest.split("/") if s]:
        acc = acc + "/" + seg
        title = title_by_url.get(_norm_url(acc)) or seg.replace("-", " ").strip().title()
        path.append(title)
        urls.append(acc)
    return path, urls


def _breadcrumbs(con, ptypes):
    marks = ",".join("?" * len(ptypes))
    rows = con.execute(
        "SELECT id, url, ptype, level, level_src, breadcrumb,"
        " NULLIF(title_override,'') AS title_override,"
        " COALESCE(NULLIF(title_override,''),"
        "   (SELECT s.page_title FROM snapshots s WHERE s.url_id=urls.id"
        "     AND s.status='ok' AND s.page_title<>'' ORDER BY s.id DESC LIMIT 1))"
        "   AS page_title"
        f" FROM urls WHERE active=1 AND ptype IN ({marks})"
        " AND breadcrumb NOT IN ('', '[]') AND breadcrumb IS NOT NULL"
        # dead and blank pages are not part of the tree: a 404 publishes a
        # breadcrumb of its own ("Error Pages > Page Not Found") and would
        # otherwise appear in the product hierarchy as though it were a product
        " AND IFNULL(last_status,'') NOT IN ('notlive','blank','error')",
        list(ptypes)).fetchall()
    # children of a page the site MOVED follow it to its new home in the tree
    aliases = _redirect_aliases(con)
    # Pass 1 - parse every breadcrumb once and harvest a clean display name for
    # each taxonomy URL seen anywhere, so a node the current page's own crumb
    # dropped can still be named when we rebuild the trail from the URL. The
    # page's OWN title wins for its OWN url (canonical), else the first crumb
    # text seen. Titles are stored suffix-stripped ("... | Infineon" removed).
    parsed, title_by_url = [], {}
    for r in rows:
        try:
            crumbs = json.loads(r["breadcrumb"])
        except Exception:
            crumbs = []
        crumbs = [c for c in crumbs if c.get("title")
                  and c["title"].strip().lower() != "home"]
        parsed.append((r, crumbs))
        for c in crumbs:
            u = _norm_url(c.get("url") or "")
            t = _norm_node(c.get("title") or "").strip()
            if u and t:
                title_by_url.setdefault(u, c["title"].strip())
        own = _norm_url(r["url"])
        if own and r["page_title"]:
            title_by_url[own] = re.sub(
                r"\s*[|–-]\s*Infineon Technologies\s*$", "",
                r["page_title"]).strip()

    out = []
    for r, crumbs in parsed:
        if not crumbs:
            continue
        # The full crumb path is the page's address in the taxonomy, self
        # included. A product's breadcrumb ends with itself, and a part's ends
        # with the product family it belongs to - so both land on the SAME
        # node, and the part sits inside its product instead of beside it.
        path = [c["title"].strip() for c in crumbs]
        # every crumb carries the live Infineon URL of that level - keep it so a
        # grouping node can link to the real page it stands for
        crumb_urls = [(c.get("url") or "").strip() for c in crumbs]
        # --- structure from the URL, not the breadcrumb ------------------
        # Infineon's breadcrumb is unreliable for products: it DROPS real
        # ancestors (a part shows "Microcontroller > CAPSENSE controllers",
        # skipping Sensing controllers) AND SPLICES in cross-links off the URL
        # path ("... > Legacy MCUs > Sensing controllers ..."). Either way the
        # same family lands at different depths and renders two or three times
        # at two or three levels. The URL path is the one consistent signal, so
        # rebuild the whole product/part trail from the taxonomy URL's segments;
        # breadcrumb text only names the nodes. Applications (report-driven
        # levels) and design resources keep their own trail, lightly pruned of
        # phantom cross-links.
        tax = _taxonomy_url(r["ptype"], r["url"], crumb_urls)
        if tax:
            if aliases:
                # keep it absolute - tree nodes link to the live site
                tax = ("https://www.infineon.com"
                       + _apply_aliases(_tax_norm(tax), aliases))
            path, crumb_urls = _url_trail(tax, title_by_url)
        else:
            anchor = next((u for u in reversed(crumb_urls) if u), "")
            if anchor:
                keep = [i for i, u in enumerate(crumb_urls)
                        if not u or _url_ancestor(u, anchor)]
                path = [path[i] for i in keep]
                crumb_urls = [crumb_urls[i] for i in keep]
        if not path:
            continue
        out.append({"id": r["id"], "url": r["url"], "ptype": r["ptype"],
                    "level": r["level"] or "",
                    "level_src": r["level_src"] or "",
                    "level_name": LEVEL_NAMES.get(r["level"] or "", ""),
                    "title": r["page_title"] or "", "crumbs": path,
                    "crumb_urls": crumb_urls})
    return out


def _hierarchy_tree(pages, tracked=None, product_tree=True):
    """Nest pages under their own breadcrumb path.

    The site publishes a JSON-LD breadcrumb on every page, and a part's
    breadcrumb ends at its product family - so parts land under their product
    without scraping a parametric table off all 190 product pages. Types whose
    pages have no depth (a flat /part/ list) simply come out flat.

    `tracked` maps a normalised URL to the id of the page we captured for it, so
    each grouping node can link to its real page (ours if we have it, else the
    live site) instead of rendering as dead text.
    """
    root = {"name": "", "children": {}, "pages": [], "count": 0, "url": ""}
    for p in pages:
        node = root
        node["count"] += 1
        crumb_urls = p.get("crumb_urls") or []
        for i, part in enumerate(p["crumbs"]):
            child = node["children"].get(part)
            if child is None:
                child = {"name": part, "children": {}, "pages": [],
                         "count": 0, "url": ""}
                node["children"][part] = child
            node = child
            node["count"] += 1
            if not node["url"] and i < len(crumb_urls) and crumb_urls[i]:
                node["url"] = crumb_urls[i]
        node["pages"].append(p)

    def freeze(n, depth=0):
        return {"name": n["name"], "count": n["count"], "depth": depth,
                "url": n.get("url", ""),
                "pages": sorted(n["pages"], key=lambda x: x["title"] or x["url"]),
                "children": [freeze(c, depth + 1)
                             for c in sorted(n["children"].values(),
                                             key=lambda c: c["name"])]}
    tree = freeze(root)
    _live_levels(tree, product_tree=product_tree)
    _attach_self_pages(tree)
    _link_nodes(tree, tracked or {})
    return tree


# pure string reductions called ~90k and ~43k times per hierarchy render, over
# a few thousand distinct values - memoising is most of that work removed
@lru_cache(maxsize=32768)
def _norm_url(u):
    return (u or "").split("#")[0].split("?")[0].rstrip("/").lower()


def _link_nodes(node, tracked):
    """Give each grouping node a link to the real page it stands for. Every
    breadcrumb crumb carries that level's live Infineon URL: if we captured it,
    the node links to OUR library (page_id); otherwise it links to the live page
    (its url). Without this, an intermediate family/category page whose own
    breadcrumb happens to root elsewhere renders as plain, dead text even though
    it is a real page we may already track."""
    if node.get("self_page") is None and node.get("url"):
        hit = tracked.get(_norm_url(node["url"]))
        if hit:
            node["page_id"] = hit
    for c in node["children"]:
        _link_nodes(c, tracked)


@lru_cache(maxsize=32768)
def _norm_node(s):
    """Node/page name reduced to its bare identity for matching."""
    s = re.sub(r"\s+", " ", (s or "")).strip().lower()
    for suf in (" | infineon technologies", " – infineon technologies",
                " - infineon technologies"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s.strip()


def _attach_self_pages(node):
    """Let a navigation node BE its own page instead of repeating it inside.

    A product family's breadcrumb ends at itself, so the Microcontroller page
    lands in the "Microcontroller" node's own page list while its sub-products
    also branch beneath it. Rendered naively that shows a "Microcontroller"
    folder with a "Microcontroller | Infineon Technologies" leaf inside - the
    same name twice. Here the family/category page is lifted onto the node
    (badge + link on the folder header) and removed from the leaf list. Parts
    are never a node's identity: they are real items that live on it, so they
    stay as leaves.
    """
    self_page = None
    nn = _norm_node(node["name"])
    non_part = [p for p in node["pages"] if p.get("ptype") != "Part"]
    if non_part:
        # a family/category page whose breadcrumb ends at this node - its title
        # may be truncated or carry a suffix, so fall back to the first one
        exact = [p for p in non_part if _norm_node(p.get("title") or "") == nn]
        self_page = (exact or non_part)[0]
    else:
        # No family page here. A Part is a node's identity ONLY when its name
        # IS the node's (a standalone tool page whose breadcrumb terminates at
        # itself, e.g. "Evaluation Board Finder"). A genuine part terminates at
        # its FAMILY node, whose name differs, so it correctly stays a leaf.
        exact_part = [p for p in node["pages"]
                      if _norm_node(p.get("title") or "") == nn]
        self_page = exact_part[0] if exact_part else None
    if self_page is not None:
        node["pages"] = [p for p in node["pages"] if p is not self_page]
    node["self_page"] = self_page
    for c in node["children"]:
        _attach_self_pages(c)


def _live_levels(node, depth=0, product_tree=True):
    """Work out each page's level from the LIVE tree it actually sits in.

        depth 1  product category   depth 2  product family
        leaf     end family         between  sub-family     a part  part type

    The PC/PFP/PSFP/PEFP/PTP scheme is PRODUCTS ONLY. Applications wear their
    own ADP > AOP > ASP / AFB scheme, taken from the report's Page Type column
    (never URL depth - the sheet is the authority). Design resources and promo
    pages have no levels at all.
    """
    if not product_tree:
        # applications and design resources carry their own stored levels
        # (ADP/AOP/ASP/AFB, DRC/DRF/DRSF) - show them in the tree too
        _own = set(APP_LEVELS) | set(DR_LEVELS)
        node["live_level"] = ""
        for p in node["pages"]:
            p["live_level"] = p["level"] if p.get("level") in _own else ""
            p["level_disagrees"] = False
        for c in node["children"]:
            _live_levels(c, depth + 1, product_tree)
        return
    # the node's OWN level (for its header badge when the node links to a page):
    # depth 1 category, depth 2 family, a leaf grouping end-family, else sub-family
    if depth == 0:
        node["live_level"] = ""
    elif depth == 1:
        node["live_level"] = "PC"
    elif depth == 2:
        node["live_level"] = "PFP"
    elif not node["children"]:
        node["live_level"] = "PEFP"
    else:
        node["live_level"] = "PSFP"
    for p in node["pages"]:
        if p.get("ptype") == "Part":
            # a part page is a PTP; an evaluation board wears its own badge
            p["live_level"] = ("EB" if "/evaluation-board/" in (p.get("url") or "")
                               else "PTP")
        elif p.get("level_src") in ("manual", "structure") and p.get("level"):
            # manual is the human's call; structure is the site's own walk,
            # which knows children we do not track (its tiles), so both beat
            # the tracked-subtree guess. Never outlined, never overwritten.
            p["live_level"] = p["level"]
            p["level_disagrees"] = False
            continue
        elif depth <= 1:
            p["live_level"] = "PC"
        elif depth == 2:
            p["live_level"] = "PFP"
        elif not node["children"]:
            p["live_level"] = "PEFP"
        else:
            p["live_level"] = "PSFP"
        p["level_disagrees"] = bool(
            p.get("level") and p["live_level"] != p["level"])
    for c in node["children"]:
        _live_levels(c, depth + 1, product_tree)


def _redirect_target(snap_dir, url):
    """The page a URL actually served, when it is not the URL you asked for.

    Some listed URLs 301 elsewhere, so the library can describe a different
    page than the address implies (one /medical-wearables... URL serves the
    Digital health hub). We follow the redirect - that IS the live page - but
    the reader has to be told, or the library quietly answers a question they
    did not ask.
    """
    try:
        final = json.loads((Path(snap_dir) / "meta.json")
                           .read_text(encoding="utf-8")).get("final_url") or ""
    except Exception:
        return ""
    # A QUERY STRING IS NOT A DIFFERENT PAGE. /presence-sensing serving
    # /presence-sensing?ftab=04 is the same page opening on a tab, and calling
    # that a redirect put a warning banner on pages nothing was wrong with.
    # The runner's own _same_page has always stripped both; this did not, so
    # the list correctly refused to repoint while the page shouted anyway.
    strip = lambda u: u.split("#")[0].split("?")[0].rstrip("/")
    return final.split("#")[0] if strip(final) and strip(final) != strip(url) else ""


def _category_tree(items):
    """Nest 'Browse by category' links by their URL path so they read as the
    hierarchy they are (parent section -> children)."""
    nodes = []
    for it in items:
        url = (it.get("url") or "").rstrip("/")
        if not url:
            continue
        path = urlsplit(url).path.strip("/").split("/")
        nodes.append({"title": it.get("title") or url, "url": url,
                      "page_id": it.get("page_id"), "path": path,
                      "children": []})
    nodes.sort(key=lambda n: len(n["path"]))
    roots = []
    for n in nodes:
        parent = None
        for cand in nodes:
            if cand is n:
                continue
            if len(cand["path"]) < len(n["path"]) and \
                    n["path"][:len(cand["path"])] == cand["path"]:
                if parent is None or len(cand["path"]) > len(parent["path"]):
                    parent = cand
        (parent["children"] if parent else roots).append(n)
    return roots


def _sections_diff(old_dir, new_dir):
    """What the library diff shows the human - a thin label-mapping over
    compare.library_diff_detail, which uses the DETECTOR'S own item rules.

    This used to be a parallel implementation (added/removed only, raw title
    keys, its own category list) and it disagreed with the detector in both
    directions: a modified-in-place document ('Documents (~1)', change #5584)
    rendered as nothing, while a casing-only retitle the detector deliberately
    ignores would have rendered as churn. Deriving from the same primitives
    makes disagreement structurally impossible: whatever is counted is shown,
    whatever is shown was counted."""
    labels = dict(SECTION_LABELS)
    detail = cmp.library_diff_detail(old_dir, new_dir)
    return {labels.get(cat, cat.replace("_", " ").capitalize()): d
            for cat, d in detail.items()}


@app.route("/url/<int:url_id>")
def url_detail(url_id):
    con = db.connect()
    u = con.execute("SELECT * FROM urls WHERE id=?", (url_id,)).fetchone()
    if not u:
        con.close()
        abort(404)
    snaps = con.execute(
        "SELECT * FROM snapshots WHERE url_id=? ORDER BY id DESC LIMIT 50",
        (url_id,)).fetchall()
    changes = con.execute(
        "SELECT * FROM changes WHERE url_id=? ORDER BY id DESC LIMIT 100",
        (url_id,)).fetchall()
    latest = con.execute(
        "SELECT * FROM snapshots WHERE url_id=? AND status='ok' AND dir<>''"
        " AND rejected=0 ORDER BY id DESC LIMIT 1", (url_id,)).fetchone()
    con.close()
    sections = _load_sections(latest["dir"]) if latest else None
    redirect_to = _redirect_target(latest["dir"], u["url"]) if latest else ""
    # resolve item links that point at pages we track -> open OUR content
    tracked = {}
    if sections:
        links = {it.get("url", "").rstrip("/")
                 for items in sections.values() if isinstance(items, list)
                 for it in items if isinstance(it, dict) and it.get("url")}
        if links:
            con2 = db.connect()
            marks = ",".join("?" * len(links))
            for r in con2.execute(
                    f"SELECT id, url FROM urls WHERE rtrim(url,'/') IN ({marks})",
                    [l for l in links]):
                tracked[r["url"].rstrip("/")] = r["id"]
            con2.close()
        for items in sections.values():
            if not isinstance(items, list):
                continue
            for it in items:
                if isinstance(it, dict) and it.get("url"):
                    it["page_id"] = tracked.get(it["url"].rstrip("/"))

    # What this page's copy breaks, keyed by block, so the flag can sit beside
    # the block it is about rather than in a list at the bottom that nobody
    # reads. Measured live from the capture on screen, not from the stored
    # column, so an edited limit shows the moment it is saved.
    copy_detail = cmp.copy_detail(latest["dir"], db.get_settings()) if latest else {}
    copy_flags = {k: "; ".join(d["problems"]) for k, d in copy_detail.items()
                  if d["problems"]}
    copy_labels = {k: l for k, l, _c, _lo, _hi in cmp.COPY_LIMITS}
    # The per-line limit for each section that HAS one on this capture, so a
    # bullet can carry its own length. "54 over 40" on the heading says the
    # worst one exists without saying which of twenty-three it is. Built from
    # copy_detail, so a block the rules skip - a parameter table under a
    # Features heading - is not marked up line by line either.
    copy_chars = {k: d["chars"] for k, d in copy_detail.items() if d["chars"]}

    cat_tree = _category_tree(sections.get("categories", []) if sections else [])

    con3 = db.connect()
    present = _cats_present_for_type(con3, u["ptype"] or "")
    con3.close()
    try:
        flags = json.loads(u["flags"] or "{}")
    except Exception:
        flags = {}

    library = []
    _settings_now = db.get_settings()   # backend search URLs, read once
    _generic_imgs = _generic_images()
    if sections:
        for cat, label in SECTION_LABELS:
            if cat == "categories":
                continue                      # rendered as a tree instead
            items = sections.get(cat, [])
            if cat == "images":
                # site furniture (spinner, header/footer logos) is identical
                # on every page - not this page's content. Everything else gets
                # a real name: a slot-slug alt ('banner-image') or an empty one
                # yields to the image's own filename, and responsive renditions
                # of one picture (?wid=1400/600) collapse to a single item.
                items = [i for i in items
                         if not (isinstance(i, dict)
                                 and runner.is_furniture_img(i.get("url", "")))]
                # the page's captured hero/main image, matched by URL so a
                # part's product photo (class ptp-product-img, named after its
                # package - not "hero") still reads as THE main one. The src is
                # already stored in the hero_image flag; we just honour it here.
                # Only when it has_content: a DEFAULT placeholder (missing-
                # product-images) is not this page's picture and must never be
                # starred as the main one.
                _hero = flags.get("hero_image") or {}
                _hero_src = (_hero.get("src") or "") if _hero.get("has_content") else ""
                _hero_src = _hero_src.split("?")[0].split(":asset")[0]
                _seen_img, _deduped = set(), []
                for i in items:
                    if not isinstance(i, dict):
                        continue
                    canon = (i.get("url", "") or "").split("?")[0].split(":asset")[0]
                    if canon in _seen_img:
                        continue
                    _seen_img.add(canon)
                    orig = i.get("title", "") or ""
                    # The row was INDEXED under the raw alt and the raw URL.
                    # Below we give it a readable title and a canonical URL for
                    # display - deriving the item key from those instead sent
                    # every prettified image ("name" -> "Absint") to a 404,
                    # because no row is stored under that name. Keep the
                    # identity the indexer used; mirror its empty-alt fallback.
                    i["_key_title"] = (orig.strip()
                                       or runner.img_title_fallback(
                                           i.get("url", "") or ""))
                    i["_key_url"] = i.get("url", "") or ""
                    # the page's main visual: its alt/url calls it a banner,
                    # hero or key-visual - flag it so it reads as THE main one
                    i["is_main"] = bool(re.search(
                        r"banner|hero|key.?visual", orig + " " + canon, re.I)) \
                        or (bool(_hero_src) and canon == _hero_src)
                    i["url"] = canon
                    i["title"] = pretty_image_title(orig, canon)
                    # a logo or tool icon that rides along on a large slice of
                    # the site is the site's furniture, not this page's picture.
                    # NEVER the page's own banner though - a hero photo shared
                    # by a family of application pages is exactly the image
                    # someone opened this page to look at.
                    i["is_generic"] = (canon in _generic_imgs
                                       and not i["is_main"])
                    _deduped.append(i)
                # the main image floats to the top of the Images section
                items = sorted(_deduped, key=lambda x: not x.get("is_main"))
            # once every page of this type is captured, a category that is
            # empty across ALL of them does not belong to this page shape
            if not items and present is not None and cat not in present:
                continue
            for it in items:
                if isinstance(it, dict):
                    it["item_key"] = runner.item_key(
                        cat, it.get("_key_title", it.get("title", "")),
                        it.get("_key_url", it.get("url", "")))
                    it["backend"] = backend_system(cat, it.get("meta", ""),
                                                   it.get("dtype"),
                                                   it.get("group"),
                                                   it.get("url", ""))
                    it["aname"] = asset_name(cat, it.get("url", ""))
                    it["bsearch"] = backend_search_link(
                        it["backend"], it["aname"], _settings_now)
            groups = []
            if any(i.get("group") for i in items):
                order = []
                by_group = {}
                for i in items:
                    g = i.get("group", "") or "Other"
                    if g not in by_group:
                        by_group[g] = []
                        order.append(g)
                    by_group[g].append(i)
                groups = [(g, by_group[g]) for g in order]
            library.append({"label": label, "items": items, "groups": groups,
                            "cat": cat})

    # ONE unified component list per page type: the union of every component
    # ever seen on any page of this type (PTP and Evaluation board are separate
    # types). Every page of the type shows that same list - each entry present
    # here or "not on this page" - so pages of a type always read alike. The
    # union comes from the settings matrix once saved, else from the data.
    this_cats = {cat for cat in _CAT_TO_COMP
                 if sections and sections.get(cat)}
    states = _page_component_states(u["components"], u["detail_tabs"], flags,
                                    this_cats,
                                    captured_at=u["last_checked"])
    have = {k for k, v in states.items() if v != "absent"}
    con5 = db.connect()
    etype = _etype(u["ptype"] or "", u["url"], u["level"])
    type_comps = _effective_matrix(con5).get(etype, set())
    con5.close()
    # hero_image rides on every page ("almost all should have it") even where
    # its shape has no measurement yet - the chip then says so instead of lying
    comp_rows = [{"key": k, "label": lbl, "exists": k in have,
                  "state": states.get(k, "absent")}
                 for k, lbl, _kind, _arg in _all_components()
                 if k in type_comps or k in have or k == "hero_image"]

    crumbs, nav = _page_nav(u)
    # ownership fields to show/edit: the page type's set, plus any field that
    # carries an imported value even if not in the type's default set
    _mfields = list(META_FIELDS.get(u["ptype"], []))
    for f in ("bl", "pl", "cm", "owner"):
        if (u[f] or "") and f not in _mfields:
            _mfields.append(f)
    notes, _n, n_open = _notes_for("page", url_id)
    # what people say about this page's product family, when anything does
    say_rows, say_n, say_entity, _say_labels = [], 0, "", {}
    try:
        import sentiment as sn
        _say_labels = sn.SOURCE_LABELS
        # latest is None when no capture has a dir on disk - the newest
        # snapshot row still carries the page title
        title = (u["title_override"] or (latest and latest["page_title"])
                 or (snaps and snaps[0]["page_title"]) or "")
        say_entity = sn.entity_for(title) if title else ""
        if say_entity:
            con = db.connect()
            say_n = con.execute("SELECT COUNT(*) FROM mentions WHERE entity=?",
                                (say_entity,)).fetchone()[0]
            if say_n:
                say_rows = con.execute(
                    "SELECT * FROM mentions WHERE entity=? ORDER BY"
                    " COALESCE(NULLIF(posted,''), fetched) DESC, id DESC"
                    " LIMIT 3", (say_entity,)).fetchall()
            con.close()
    except Exception:
        pass
    return render_template("url_detail.html", u=u, snaps=snaps, changes=changes,
                           latest=latest, library=library, cat_tree=cat_tree,
                           redirect_to=redirect_to, flags=flags,
                           copy_flags=copy_flags, copy_labels=copy_labels,
                           copy_chars=copy_chars,
                           components=comp_rows, price=(u["price"] or ""),
                           settings=_settings_now, level_names=LEVEL_NAMES,
                           meta_fields=_mfields, meta_labels=META_LABELS,
                           crumbs=crumbs, siblings=nav["siblings"],
                           children=nav["children"],
                           say_rows=say_rows, say_n=say_n,
                           say_entity=say_entity, say_labels=_say_labels,
                           notes=notes, notes_open=n_open, notes_kind="page",
                           notes_target=url_id, notes_label=u["url"])


@app.route("/change/<int:change_id>")
def change_view(change_id):
    con = db.connect()
    c = con.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
    if not c:
        con.close()
        abort(404)
    u = con.execute("SELECT * FROM urls WHERE id=?", (c["url_id"],)).fetchone()
    old_snap = con.execute("SELECT * FROM snapshots WHERE id=?",
                           (c["old_snapshot_id"],)).fetchone()
    new_snap = con.execute("SELECT * FROM snapshots WHERE id=?",
                           (c["new_snapshot_id"],)).fetchone()
    con.close()

    # Show ONLY the signals this update was actually flagged on. The raw
    # text/image diff still "sees" things the detector deliberately disregarded
    # - text across a rev boundary (the excluded feeds / mega-menu), a pure
    # reorder - so on a VISUAL-only update it would otherwise open with a "Text
    # updates" card listing the date and menu lines that were the whole reason
    # we did NOT flag on text. Gate each card on the stored verdict so the real
    # difference is the first thing shown.
    hunks, img_added, img_removed = [], [], []
    renorm = []
    if old_snap and new_snap and old_snap["dir"] and new_snap["dir"]:
        s = db.get_settings()
        ignore = (s.get("ignore_patterns") or "").splitlines()
        ignore_img = (s.get("ignore_image_patterns") or "").splitlines()
        if c["text_changed"]:
            _, _, _, hunks = cmp.text_diff(old_snap["dir"], new_snap["dir"],
                                           ignore)
        if c["images_changed"]:
            _, img_added, img_removed = cmp.images_diff(
                old_snap["dir"], new_snap["dir"], ignore_img)
        # When a signal was NOT judged because the two snapshots were made by
        # different extractor revisions, SAY so - otherwise a page flagged on
        # its library alone reads as "where did the text diff go?" (#5597).
        od, nd = old_snap["dir"], new_snap["dir"]
        if not c["text_changed"] and cmp._text_rev(od) != cmp._text_rev(nd):
            renorm.append("text")
        if not c["images_changed"] and cmp._img_rev(od) != cmp._img_rev(nd):
            renorm.append("images")
        if not c["visual_changed"] and cmp._shot_rev(od) != cmp._shot_rev(nd):
            renorm.append("screenshot")

    art = {"boxed": False, "regions": [], "legacy": False,
           "old_boxed": "", "new_boxed": ""}
    if c["dir"] and Path(c["dir"]).exists():
        d = Path(c["dir"])
        # webp first, png second: the pictures became webp on 6 Sep 2026 and
        # every stored one was converted, but a change directory the migration
        # could not read must still show its evidence rather than nothing
        for ext in ("webp", "png"):
            o, n = d / ("old_boxed." + ext), d / ("new_boxed." + ext)
            if o.exists() and n.exists():
                art["boxed"] = True
                art["old_boxed"], art["new_boxed"] = o.name, n.name
                break
        art["regions"] = sorted(
            p.name for p in d.iterdir()
            if p.name.startswith("region_")
            and p.suffix.lower() in (".webp", ".png"))
        art["legacy"] = (d / "visual_diff.png").exists()
    lib_diff = {}
    if old_snap and new_snap and old_snap["dir"] and new_snap["dir"]:
        try:
            lib_diff = _sections_diff(old_snap["dir"], new_snap["dir"])
        except Exception:
            lib_diff = {}
    notes, _n, n_open = _notes_for("change", change_id)
    return render_template("change_view.html", c=c, u=u,
                           old_snap=old_snap, new_snap=new_snap, hunks=hunks,
                           img_added=img_added, img_removed=img_removed,
                           art=art, lib_diff=lib_diff, renorm=renorm,
                           notes=notes, notes_open=n_open, notes_kind="change",
                           notes_target=change_id,
                           notes_label=(u["url"] if u else ""))


ART_NAME_RE = re.compile(r"^[a-z0-9_]+\.(?:png|webp)$")


@app.route("/change/<int:change_id>/art/<name>")
def change_art(change_id, name):
    if not ART_NAME_RE.match(name):
        abort(404)
    con = db.connect()
    c = con.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
    con.close()
    if not c or not c["dir"]:
        abort(404)
    p = Path(c["dir"]) / name
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="image/webp" if p.suffix == ".webp"
                     else "image/png")


@app.route("/change/<int:change_id>/review", methods=["POST"])
@app.route("/change/<int:change_id>/resolve", methods=["POST"])
def change_resolve(change_id):
    resolution = request.form.get("resolution", "approve")
    con = db.connect()
    c = con.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
    if not c:
        con.close()
        abort(404)
    con.execute("UPDATE changes SET reviewed=1, resolution=? WHERE id=?",
                (resolution, change_id))
    # HAS THE PAGE BEEN READ AGAIN SINCE? A change compares two captures, and
    # a reject rewinds the library to the older of them. That is right while
    # the newer one is still what the page currently holds - and wrong the
    # moment a THIRD capture exists, because the rewind would then throw away
    # a reading nobody has objected to. All 127 updates left over from the 5
    # September check were in exactly that state after the pages were read
    # again on the 9th: rejecting one would have restored its 5 September
    # library, tab-escape bug and all.
    current = con.execute(
        "SELECT id FROM snapshots WHERE url_id=? AND status='ok' AND dir<>''"
        " AND rejected=0 ORDER BY id DESC LIMIT 1", (c["url_id"],)).fetchone()
    superseded = bool(current and current["id"] != c["new_snapshot_id"])
    if resolution == "reject" and superseded:
        flash("Filed as rejected. The page has been read again since this "
              "update, so nothing was rolled back - the newer capture stays "
              "current.")
    elif resolution == "reject":
        # Reject means one thing: do not adopt THIS capture as the baseline.
        # The old snapshot stays the current version and this one is never
        # compared against again.
        con.execute("UPDATE snapshots SET rejected=1 WHERE id=?",
                    (c["new_snapshot_id"],))
        # The library/search index was already refreshed from the new capture
        # AT CAPTURE TIME - leaving it would show the very content the reject
        # just declared wrong. Restore items + full-text from the snapshot the
        # reject says is the truth.
        old = con.execute("SELECT dir FROM snapshots WHERE id=?",
                          (c["old_snapshot_id"],)).fetchone()
        if old and old["dir"] and Path(old["dir"]).exists():
            runner.index_items(con, c["url_id"], old["dir"])
        flash("Rejected — this capture will not be used as the baseline. "
              "The old version stays current.")
    elif superseded:
        flash("Approved. The page has been read again since this update, so "
              "the newer capture is the current one.")
    else:
        flash("Approved — this is now the current version.")
    # Either way the page is DEALT WITH and goes back to normal. Reject used to
    # park it in its own 'rejected' state, shown as "Awaiting fix", where it sat
    # until the website changed back - a queue that only ever grew, for a
    # decision that had already been made. Rejecting says "not this capture",
    # not "chase this page forever".
    remaining = con.execute(
        "SELECT COUNT(*) FROM changes WHERE url_id=? AND reviewed=0",
        (c["url_id"],)).fetchone()[0]
    if remaining == 0:
        con.execute("UPDATE urls SET last_status='unchanged'"
                    " WHERE id=? AND last_status IN ('changed','rejected')",
                    (c["url_id"],))
    con.commit()
    con.close()
    nxt = request.form.get("next") or url_for("index")
    return redirect(nxt)


# ---- File updates (asset fingerprints) -------------------------------------
# A datasheet or image republished at the SAME address is invisible to the
# page compare (no URL changed, no text moved). asset_watch stores each file's
# server validators after every full run; when one moves, it lands here.

@app.route("/assets")
def assets_page():
    import asset_watch
    con = db.connect()
    rows = con.execute(
        "SELECT * FROM asset_changes WHERE reviewed=0"
        " ORDER BY ts DESC, id DESC LIMIT 500").fetchall()
    pages = {}
    for r in rows:
        # page titles live on the SNAPSHOT, not the url row (u.page_title does
        # not exist - it 500'd the whole page); take the latest good capture's.
        ps = con.execute(
            "SELECT DISTINCT u.id,"
            " COALESCE(NULLIF(u.title_override,''), NULLIF((SELECT page_title"
            "   FROM snapshots WHERE url_id=u.id AND status='ok'"
            "   AND page_title<>'' ORDER BY id DESC LIMIT 1),''),"
            " NULLIF(u.label,''), u.url) t"
            " FROM items i JOIN urls u ON u.id=i.url_id"
            " WHERE i.item_url=? LIMIT 6", (r["url"],)).fetchall()
        pages[r["id"]] = ps
    seeded = con.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    reviewed_n = con.execute(
        "SELECT COUNT(*) FROM asset_changes WHERE reviewed=1").fetchone()[0]
    con.close()
    return render_template("assets.html", rows=rows, pages=pages,
                           seeded=seeded, reviewed_n=reviewed_n,
                           astate=asset_watch.ASSET_STATE)


@app.route("/assets/review", methods=["POST"])
def assets_review():
    ids = request.form.getlist("id")
    con = db.connect()
    if request.form.get("all"):
        con.execute("UPDATE asset_changes SET reviewed=1 WHERE reviewed=0")
        flash("All file updates marked as reviewed.")
    elif ids:
        marks = ",".join("?" * len(ids))
        con.execute(
            f"UPDATE asset_changes SET reviewed=1 WHERE id IN ({marks})", ids)
        flash(f"{len(ids)} file update(s) marked as reviewed.")
    con.commit()
    con.close()
    return redirect(url_for("assets_page"))


@app.route("/assets/run", methods=["POST"])
def assets_run():
    import asset_watch
    if runner.RUN_STATE.get("running"):
        flash("A page check is running — the file check starts by itself when "
              "it finishes.")
    elif asset_watch.run_in_background():
        flash("Checking all referenced files (headers only, no downloads) — "
              "first pass records each file's fingerprint, later passes "
              "report the ones that changed.")
    else:
        flash("The file check is already running.")
    return redirect(url_for("assets_page"))


# ---- Sentiment analysis (mentions of the watched products) -----------------
# Parked 2026-07-21, brought back on Anas's request 2026-07-28. The one guard
# that stays is the capture run: collection and a check share the network, so
# collect waits its turn. Both POSTs are admin-only via the default-deny rule.

_MARK_CACHE = {}
_FAM_CACHE = {}


@app.template_filter("mark")
def mark_filter(text, entity):
    """Bold the tracked words inside a mention, safely.

    The text is ESCAPED FIRST and the tags are added after, so a title that
    contains markup stays inert. The pattern per family - its name, its
    aliases, and "infineon" itself - is compiled once and cached; the same
    word-edges as the relevance gate, so what got a mention kept is exactly
    what lights up."""
    import re as _re

    import markupsafe
    import sentiment as sn
    rx = _MARK_CACHE.get(entity)
    if rx is None:
        terms = {entity, "infineon"} | set(sn._ALIASES.get(entity, ()))
        terms |= {w for w in entity.split() if len(w) > 3}
        pat = "|".join(sorted((_re.escape(t) for t in terms if t),
                              key=len, reverse=True))
        rx = _re.compile("(?<![A-Za-z0-9])(" + pat + ")(?![A-Za-z])", _re.I)
        _MARK_CACHE[entity] = rx
    escaped = str(markupsafe.escape(text or ""))
    return markupsafe.Markup(rx.sub(r"<b>\1</b>", escaped))


@app.template_filter("evmark")
def evmark_filter(text, entity, reason):
    """The family name in bold and the words the score was read from lit up.

    The marking itself lives in sentiment.evidence_html, where it can be
    tested without standing up Flask; this end knows only which terms name
    the family - the same set the relevance gate uses, so what kept a
    mention is exactly what stays bold inside it."""
    import markupsafe
    import sentiment as sn
    fam = _FAM_CACHE.get(entity)
    if fam is None:
        fam = {entity, "infineon"} | set(sn._ALIASES.get(entity, ()))
        fam |= {w for w in entity.split() if len(w) > 3}
        _FAM_CACHE[entity] = fam
    return markupsafe.Markup(sn.evidence_html(text, fam, reason))


# The orders a pulse feed is read in. Newest first stays the default: the
# question is almost always "what is being said NOW".
SENT_SORTS = (("", "Newest first"), ("oldest", "Oldest first"),
              ("worst", "Most negative"), ("best", "Most positive"))


@app.route("/sentiment")
def sentiment_page():
    import sentiment as sn
    ent = request.args.get("entity", "")
    src = request.args.get("source", "")
    q = (request.args.get("q") or "").strip()
    period = request.args.get("period", "")
    sort = request.args.get("sort", "")
    # YYYY-MM-DD or nothing: anything else is a typed URL, not a date picker,
    # and it must not reach the query
    _date = lambda v: v if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v or "") else ""
    d_from = _date(request.args.get("from", ""))
    d_to = _date(request.args.get("to", ""))
    sel_pl = request.args.get("pl", "")
    origin = request.args.get("origin", "")
    # Which terms belong to the chosen product line, and which to the chosen
    # kind of term. Both narrow the SAME thing - the set of entities asked
    # for - so they are resolved to a term list once and applied as one IN.
    lines = sn.entity_lines()
    terms_all = sn.entities()
    want = None
    if sel_pl:
        want = {t["entity"] for t in terms_all
                if sel_pl in (lines.get(t["entity"], {}).get("pls") or [])}
    if origin:
        of = {t["entity"] for t in terms_all if t["origin"] == origin}
        want = of if want is None else (want & of)
    con = db.connect()
    # one SUM per registered source, so a new collector shows up here and in
    # the template with no query edit - the registry is the only list
    sums = ", ".join(
        "SUM(CASE WHEN source='%s' THEN 1 ELSE 0 END) src_%s"
        % (s["key"], s["key"]) for s in sn.SOURCES)
    per_entity = con.execute(
        "SELECT entity, COUNT(*) n FROM sentiment_mentions GROUP BY entity").fetchall()
    family_count = len(per_entity)
    mention_counts = {r["entity"]: r["n"] for r in per_entity}
    src_totals = {r["source"]: r["n"] for r in con.execute(
        "SELECT source, COUNT(DISTINCT url) n FROM sentiment_mentions GROUP BY source")}
    where, args = [], []
    if ent:
        where.append("entity=?")
        args.append(ent)
    if src:
        where.append("source=?")
        args.append(src)
    if q:
        where.append("(title LIKE ? OR body LIKE ? OR fulltext LIKE ? OR entity LIKE ?"
                     " OR author LIKE ?)")
        like = "%" + q + "%"
        args += [like, like, like, like, like]
    # posted is what the market said WHEN; fetched is only when we heard it,
    # and a mention with no posted date would otherwise drop out of every
    # window the moment a collection ran.
    # A named window, or two dates. Dates win when either is given, because
    # somebody who typed one meant it - and either end alone is a valid
    # question ("everything since the launch", "everything before the recall").
    if d_from or d_to:
        if d_from:
            where.append("COALESCE(NULLIF(posted,''), fetched) >= ?")
            args.append(d_from + " 00:00:00")
        if d_to:
            # inclusive of the whole closing day, which is what a person
            # picking 30 September means by "to 30 September"
            where.append("COALESCE(NULLIF(posted,''), fetched) <= ?")
            args.append(d_to + " 23:59:59")
    elif sn.period_days(period):
        where.append("COALESCE(NULLIF(posted,''), fetched) >= ?")
        args.append((datetime.now(timezone.utc)
                     - timedelta(days=sn.period_days(period))
                     ).strftime("%Y-%m-%d %H:%M:%S"))
    if want is not None:
        if not want:
            where.append("1=0")          # a line with no terms has no mentions
        else:
            where.append("entity IN (" + ",".join("?" * len(want)) + ")")
            args += sorted(want)
    # One video about GaN drivers legitimately files under two families -
    # but the unfiltered feed must not show it twice. Without an entity
    # filter, one row per LINK; with one, the family's own copy.
    base = "FROM sentiment_mentions"
    if where:
        base += " WHERE " + " AND ".join(where)
    # Bound the rendered family grid, not the tracked inventory. The same
    # filters narrow both the family overview and the feed below it.
    def page_window(name, count, size):
        last = max(1, (count + size - 1) // size)
        page = max(1, min(request.args.get(name, 1, type=int) or 1, last))
        return page, last

    family_total = con.execute(
        "SELECT COUNT(DISTINCT entity) " + base, args).fetchone()[0]
    family_page, family_last = page_window("family_page", family_total, 12)
    per_entity = con.execute(
        "SELECT entity, COUNT(*) n, AVG(score) avg_score, " + sums + " "
        + base + " GROUP BY entity ORDER BY n DESC, entity COLLATE NOCASE, entity"
        " LIMIT 12 OFFSET ?", args + [(family_page - 1) * 12]).fetchall()
    if ent:
        sql = "SELECT * " + base
    else:
        sql = ("SELECT * FROM sentiment_mentions WHERE id IN"
               " (SELECT MIN(id) " + base + " GROUP BY url)")
    filtered_total = con.execute(
        "SELECT COUNT(*) FROM (" + sql + ")", args).fetchone()[0]
    mention_page, mention_last = page_window("page", filtered_total, 50)
    # WHEN it was said, or how it was said. Newest first is the pulse check;
    # oldest first is how you read a story forward; the two score orders are
    # "show me the complaints" and "show me what is working", which is the
    # question a sentiment feed exists to answer and could not be asked.
    # A mention with no score sorts last either way rather than counting as 0.
    sql += {
        "oldest": " ORDER BY COALESCE(NULLIF(posted,''), fetched) ASC, id ASC",
        "worst": " ORDER BY score IS NULL, score ASC,"
                 " COALESCE(NULLIF(posted,''), fetched) DESC, id DESC",
        "best": " ORDER BY score IS NULL, score DESC,"
                " COALESCE(NULLIF(posted,''), fetched) DESC, id DESC",
    }.get(sort, " ORDER BY COALESCE(NULLIF(posted,''), fetched) DESC, id DESC")
    sql += " LIMIT 50 OFFSET ?"
    rows = con.execute(sql, args + [(mention_page - 1) * 50]).fetchall()
    source_attempts = {r['source']: dict(r) for r in con.execute('SELECT * FROM sentiment_sources')}
    total = con.execute("SELECT COUNT(DISTINCT url) FROM sentiment_mentions").fetchone()[0]
    scored = con.execute(
        "SELECT COUNT(*) n, AVG(score) avg FROM sentiment_mentions"
        " WHERE id IN (SELECT MIN(id) FROM sentiment_mentions GROUP BY url)"
        " AND score IS NOT NULL").fetchone()
    unscored = con.execute(
        "SELECT COUNT(*) FROM sentiment_mentions WHERE score IS NULL"
        " AND id IN (SELECT MIN(id) FROM sentiment_mentions GROUP BY url)").fetchone()[0]
    con.close()
    last_collected = con2 = None
    try:
        con2 = db.connect()
        last_collected = con2.execute(
            "SELECT MAX(fetched) FROM sentiment_mentions").fetchone()[0]
    finally:
        if con2 is not None:
            con2.close()
    terms = sn.entities()
    # Only lines and kinds that actually have something to show. An empty
    # option is a dead end somebody has to click to discover.
    pl_options = sorted({pl for t in terms
                         for pl in (lines.get(t["entity"], {}).get("pls") or [])
                         if mention_counts.get(t["entity"])})
    origin_options = sorted({t["origin"] for t in terms
                             if mention_counts.get(t["entity"])})
    return render_template("sentiment.html", per_entity=per_entity, rows=rows,
                           last_collection_attempt=max((v['last_attempt'] for v in source_attempts.values()), default=None),
                           family_count=family_count, family_total=family_total,
                           family_page=family_page, family_last=family_last,
                           mention_page=mention_page, mention_last=mention_last,
                           total=total, scored_n=scored["n"],
                           unscored=unscored,
                           avg_all=scored["avg"], src_totals=src_totals,
                           filtered_total=filtered_total,
                           last_collected=last_collected,
                           capture_running=bool(runner.RUN_STATE.get("running")),
                           sources=sn.SOURCES,
                           ready_keys={s["key"] for s in sn.sources_ready()},
                           sel_entity=ent, sel_source=src, q=q,
                           lines=lines, periods=sn.PERIODS,
                           sel_period=period, sel_pl=sel_pl,
                           sel_sort=sort, sorts=SENT_SORTS,
                           sel_from=d_from, sel_to=d_to,
                           sel_origin=origin, pl_options=pl_options,
                           origin_options=origin_options,
                           sstate=sn.SENT_STATE,
                           has_llm_key=bool(os.environ.get("ANTHROPIC_API_KEY")))


@app.route("/sentiment/rows")
def sentiment_rows():
    """One family's mentions as a rendered fragment - what a family card
    pulls in when it expands. Same row markup as the main feed, one source
    of truth via the shared partial."""
    import sentiment as sn
    ent = request.args.get("entity", "")
    src = request.args.get("source", "")
    if not ent:
        abort(404)
    con = db.connect()
    if src:
        rows = con.execute(
            "SELECT * FROM sentiment_mentions WHERE entity=? AND source=? ORDER BY"
            " COALESCE(NULLIF(posted,''), fetched) DESC, id DESC LIMIT 500",
            (ent, src)).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM sentiment_mentions WHERE entity=? ORDER BY"
            " COALESCE(NULLIF(posted,''), fetched) DESC, id DESC LIMIT 500",
            (ent,)).fetchall()
    con.close()
    return render_template("_mention_rows.html", rows=rows,
                           sources=sn.SOURCES, sel_entity=ent)


@app.route("/sentiment/terms", methods=["POST"])
def sentiment_terms():
    """The managed term list: add a term, remove one (whatever its origin),
    put a removed one back. Admin-only via default-deny."""
    import sentiment as sn
    action = request.form.get("action", "")
    name = (request.form.get("entity") or "").strip()[:60]
    if not name:
        flash("Name the term first.")
        return redirect(url_for("sentiment_page"))
    try:
        removed = set(json.loads(db.get_settings().get("sn_removed") or "[]"))
    except Exception:
        removed = set()
    try:
        custom = json.loads(db.get_settings().get("sn_custom") or "[]")
    except Exception:
        custom = []
    if action == "add":
        q = (request.form.get("query") or "").strip()[:120]
        custom = [c for c in custom if c.get("entity") != name]
        custom.append({"entity": name, "queries": [q] if q else [name]})
        removed.discard(name)
        flash("Now tracking \u201c%s\u201d — the next collection searches it." % name)
    elif action == "remove":
        custom = [c for c in custom if c.get("entity") != name]
        removed.add(name)
        flash("\u201c%s\u201d is no longer searched. Its stored mentions stay." % name)
    elif action == "restore":
        removed.discard(name)
        flash("\u201c%s\u201d is searched again." % name)
    db.set_setting("sn_removed", json.dumps(sorted(removed)))
    db.set_setting("sn_custom", json.dumps(custom))
    sn._ENT_CACHE["val"] = None
    return redirect(url_for("sentiment_page"))


@app.route('/sentiment/excluded')
def sentiment_excluded():
    if not is_admin():
        abort(403)
    con = db.connect()
    count = con.execute('SELECT COUNT(*) FROM mentions WHERE id NOT IN '
                        '(SELECT id FROM sentiment_mentions)').fetchone()[0]
    last = max(1, (count + 49) // 50)
    page = max(1, min(request.args.get('page', 1, type=int) or 1, last))
    rows = con.execute('SELECT * FROM mentions WHERE id NOT IN '
                       '(SELECT id FROM sentiment_mentions) ORDER BY id DESC LIMIT 50 OFFSET ?',
                       ((page-1)*50,)).fetchall()
    con.close()
    return render_template('sentiment_excluded.html', rows=rows, total=count,
                           page=page, last=last)


@app.route('/sentiment/relevance', methods=['POST'])
def sentiment_relevance():
    mid = request.form.get('id', type=int)
    action = request.form.get('action')
    if not mid or action not in ('restore', 'exclude', 'auto'):
        abort(400)
    con = db.connect()
    con.execute('UPDATE mentions SET relevance_override=? WHERE id=?',
                ({'restore': 1, 'exclude': -1, 'auto': 0}[action], mid))
    con.commit()
    con.close()
    flash('Mention restored to analysis.' if action == 'restore' else
          'Mention hidden from analysis; the original is retained.' if action == 'exclude'
          else 'Automatic relevance check restored.')
    return redirect(url_for('sentiment_excluded' if action == 'restore' else 'sentiment_page'))


@app.route("/sentiment/status")
def sentiment_status():
    """The collection's live numbers - plus whether a page check is running,
    because the bar's idle label says what it is waiting for and must keep
    saying the truth as states change under an open page."""
    import sentiment as sn
    out = dict(sn.SENT_STATE)
    out["capture_running"] = bool(runner.RUN_STATE.get("running"))
    return jsonify(out)


@app.route("/sentiment/collect", methods=["POST"])
def sentiment_collect():
    import sentiment as sn
    if runner.RUN_STATE.get("running"):
        flash("A page check is running — collect mentions after it finishes "
              "(they share the network).")
    elif sn.run_collect_background():
        live = ", ".join(s["label"] for s in sn.sources_ready())
        flash("Collecting mentions from " + live + " — a few minutes.")
    else:
        flash("Collection is already running.")
    return redirect(url_for("sentiment_page"))


@app.route("/sentiment/score", methods=["POST"])
def sentiment_score():
    import sentiment as sn
    # all=1 scores every unscored mention, in the background: at max effort
    # that is hours of work and nobody is holding a request open for it
    if request.form.get("all") == "1" or request.args.get("all") == "1":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            flash("No ANTHROPIC_API_KEY configured.")
        elif sn.score_background(0):
            flash("Scoring every unscored mention with %s. The bar shows how "
                  "far it has got." % sn.SCORE_MODEL)
        else:
            flash("Scoring is already running.")
        return redirect(url_for("sentiment_page"))
    n = sn.score_pending()
    if n < 0:
        flash("No ANTHROPIC_API_KEY configured — set it in the environment "
              "before starting the app, then scoring works. Mentions are "
              "still collected and browsable unscored.")
    else:
        flash(f"Scored {n} mention(s).")
    return redirect(url_for("sentiment_page"))


@app.route("/snapshot/<int:snap_id>/text")
def snapshot_text(snap_id):
    con = db.connect()
    s = con.execute("SELECT * FROM snapshots WHERE id=?", (snap_id,)).fetchone()
    con.close()
    if not s or not s["dir"]:
        abort(404)
    p = Path(s["dir"]) / "text.txt"
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="text/plain")


@app.route("/snapshot/<int:snap_id>/images")
def snapshot_images(snap_id):
    con = db.connect()
    s = con.execute("SELECT * FROM snapshots WHERE id=?", (snap_id,)).fetchone()
    con.close()
    if not s or not s["dir"]:
        abort(404)
    p = Path(s["dir"]) / "images.json"
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="application/json")


@app.route("/snapshot/<int:snap_id>/screenshot")
def snapshot_screenshot(snap_id):
    con = db.connect()
    s = con.execute("SELECT * FROM snapshots WHERE id=?", (snap_id,)).fetchone()
    con.close()
    if not s or not s["dir"]:
        abort(404)
    p = db.shot_path(s["dir"])
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="image/png")


@app.route("/snapshot/<int:snap_id>/thumb")
def snapshot_thumb(snap_id):
    """A small JPEG of the page, for lists that show many pages at once.

    The stored screenshot is the WHOLE page - often a megabyte and 20,000px
    tall - so a grid of sixty would be sixty megabytes and unreadable at that
    aspect anyway. This is the top of the page at card size, written next to
    the snapshot the first time it is asked for: a few KB each, and only for
    pages someone actually looks at.
    """
    con = db.connect()
    s = con.execute("SELECT dir FROM snapshots WHERE id=?", (snap_id,)).fetchone()
    con.close()
    if not s or not s["dir"]:
        abort(404)
    full = db.shot_path(s["dir"])
    if not full.exists():
        abort(404)
    thumb = Path(s["dir"]) / "thumb.jpg"
    if not thumb.exists() or thumb.stat().st_mtime < full.stat().st_mtime:
        try:
            from PIL import Image
            im = Image.open(full)
            w, h = im.size
            # keep the masthead, not a sliver of a very tall page
            im = im.crop((0, 0, w, min(h, int(w * 0.62))))
            im.thumbnail((420, 420), Image.Resampling.LANCZOS)
            im.convert("RGB").save(thumb, "JPEG", quality=72, optimize=True)
        except Exception:
            return send_file(full, mimetype="image/png")
    return send_file(thumb, mimetype="image/jpeg")


# Item-image thumbnails: a video poster, a related-product picture or a content
# image is otherwise loaded full-size from Infineon's CDN on every page view.
# This fetches it ONCE, shrinks it to a small JPEG and caches it under a hash of
# the URL, so previews come off local disk. The full-size CDN image still backs
# the click-through - this is only the preview. Restricted to Infineon hosts (no
# fetching arbitrary URLs), and anything we cannot raster (SVG, a fetch error)
# 404s so the <img>'s onerror falls back to the CDN URL.
_THUMB_HOSTS = ("https://assets.infineon.com/", "https://www.infineon.com/")
_THUMB_RASTER_RE = re.compile(r"\.(png|jpe?g|webp|gif|bmp)(\?|$)|/is/image/", re.I)


def imgthumb(url):
    """Template helper: the local-thumbnail URL for an Infineon RASTER image,
    else the URL unchanged (doc previews, SVGs and anything off-host pass
    through so they are not sent on a pointless round-trip)."""
    if (url and url.startswith(_THUMB_HOSTS)
            and _THUMB_RASTER_RE.search(url)
            and ".svg" not in url.lower()):
        return url_for("img_thumb", u=url)
    return url or ""


app.jinja_env.globals["imgthumb"] = imgthumb


@app.route("/imgthumb")
def img_thumb():
    url = request.args.get("u", "")
    if not url.startswith(_THUMB_HOSTS):
        abort(404)
    cache = db.DATA_DIR / "imgthumbs"
    cache.mkdir(exist_ok=True)
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:24]
    out = cache / (key + ".jpg")
    fail = cache / (key + ".x")          # negative-cache: could not thumbnail
    if out.exists():
        return send_file(out, mimetype="image/jpeg")
    if fail.exists():
        abort(404)
    try:
        data = _fetch_bytes(url, cap=15_000_000)
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        # 480px covers the largest preview (the item page ~460px) while still
        # being a small file; search cards use it far smaller
        im.thumbnail((480, 480), Image.Resampling.LANCZOS)
        # flatten any transparency onto white so the JPEG reads right
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            bg = Image.new("RGB", im.size, "#ffffff")
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        im.convert("RGB").save(out, "JPEG", quality=62, optimize=True)
    except Exception:
        fail.touch()
        abort(404)
    return send_file(out, mimetype="image/jpeg")


@app.route("/change/<int:change_id>/visualdiff")
def change_visualdiff(change_id):
    con = db.connect()
    c = con.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
    con.close()
    if not c or not c["dir"]:
        abort(404)
    p = Path(c["dir"]) / "visual_diff.png"
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="image/png")


@app.route("/run-now", methods=["POST"])
def run_now():
    ids = None
    if request.form.get("only_new") == "1" or request.args.get("only_new") == "1":
        con = db.connect()
        ids = [r["id"] for r in con.execute(
            "SELECT id FROM urls WHERE active=1 AND last_checked IS NULL")]
        con.close()
        if not ids:
            flash("Every page has been checked already — nothing to resume.")
            return redirect(url_for("index"))
    elif request.form.get("pending") == "1" or request.args.get("pending") == "1":
        # only the pages that still NEED a capture: never checked or
        # failed (blank/error). Skips every page whose current library
        # is already known-good.
        con = db.connect()
        ids = [r["id"] for r in con.execute(
            "SELECT id FROM urls WHERE active=1 AND (last_checked IS NULL"
            " OR IFNULL(last_status,'') IN ('blank','error'))")]
        con.close()
        if not ids:
            flash("No pending pages — everything has a checked, current library.")
            return redirect(url_for("index"))
    elif (request.form.get("pending_components") == "1"
          or request.args.get("pending_components") == "1"):
        # resume the component pass: pages not yet captured with component
        # detection. '' = never ran (the marker); '[]' = ran, page has no nav.
        con = db.connect()
        ids = [r["id"] for r in con.execute(
            "SELECT id FROM urls WHERE active=1"
            " AND (components IS NULL OR components='')")]
        con.close()
        if not ids:
            flash("Component detection has run on every page already.")
            return redirect(url_for("index"))
    elif (request.form.get("skip_gone") == "1"
          or request.args.get("skip_gone") == "1"
          or request.form.get("skip_gone_blank") == "1"
          or request.args.get("skip_gone_blank") == "1"):
        # Everything EXCEPT the pages that are gone (404) - and optionally the
        # blank ones too. Those are the pages that spend the longest in
        # retries, so leaving them out is how a near-full run finishes fast.
        # The scheduled full check still covers them, so a page coming back
        # to life is never missed for more than a week.
        skip = ("notlive", "blank")             if (request.form.get("skip_gone_blank") == "1"
                or request.args.get("skip_gone_blank") == "1") else ("notlive",)
        con = db.connect()
        ids = [r["id"] for r in con.execute(
            "SELECT id FROM urls WHERE active=1 AND IFNULL(last_status,'')"
            " NOT IN (%s)" % ",".join("?" * len(skip)), skip)]
        con.close()
        if not ids:
            flash("Nothing left to check once those are skipped.")
            return redirect(url_for("index"))
    elif (request.form.get("refresh_price") == "1"
          or request.args.get("refresh_price") == "1"):
        # PTPs captured before the volume-pricing fix: their price lacks the
        # volume range - re-capture just those
        con = db.connect()
        ids = [r["id"] for r in con.execute(
            "SELECT id FROM urls WHERE active=1 AND ptype='Part'"
            " AND url LIKE '%/part/%' AND components<>''"
            " AND price<>'' AND price NOT LIKE '%Volume pricing%'")]
        con.close()
        if not ids:
            flash("Every priced part already carries its volume range.")
            return redirect(url_for("index"))
    elif (request.form.get("ids") or request.args.get("ids")):
        # NAMED PAGES, nothing else. Every other selector here describes a
        # population; this one is for the handful you already know about -
        # two evaluation boards whose price widget had not loaded when they
        # were captured, say. Parsed as "every number in the string" so a
        # list pasted with spaces, commas or newlines all work, and matched
        # against active pages so a stale id is dropped rather than obeyed.
        import re as _re
        want = sorted({int(x) for x in
                       _re.findall(r"\d+", request.form.get("ids")
                                   or request.args.get("ids") or "")})[:500]
        con = db.connect()
        ids = [r["id"] for r in con.execute(
            "SELECT id FROM urls WHERE active=1 AND id IN (%s)"
            % ",".join("?" * len(want)), want)] if want else []
        con.close()
        if not ids:
            flash("No live page matches those ids.")
            return redirect(url_for("index"))
    elif (request.form.get("resections") == "1"
          or request.args.get("resections") == "1"):
        # Pages whose Key features / Benefits may have been swept out of their
        # own tab. The walk up from those headings used to escape a .tab-pane
        # into .tab-content, which holds every OTHER pane, so the parametric
        # table landed in the section - 38 "benefits" on CY8C6316BZI-BLF03.
        #
        # THE PRECONDITION, not the symptom: the page is tabbed AND carries one
        # of those sections. That is 602 pages where 24 show the damage
        # outright (the two sections sharing lines, or one of them past 15
        # entries). Which of the rest escaped cannot be known from the stored
        # capture - only from reading the page again - so the superset is what
        # gets re-read. A page that was already right re-reads to the same
        # thing and costs one capture.
        con = db.connect()
        ids = [r["id"] for r in con.execute(
            "SELECT id FROM urls u WHERE u.active=1"
            " AND IFNULL(u.detail_tabs,'') NOT IN ('', '[]')"
            " AND EXISTS (SELECT 1 FROM items i WHERE i.url_id=u.id"
            "             AND i.category IN ('features','benefits'))")]
        con.close()
        if not ids:
            flash("No tabbed page carries a features or benefits section.")
            return redirect(url_for("index"))
    # baseline=1: fresh-start run after a detection-method change - every ok
    # capture becomes the page's baseline, nothing is compared or flagged
    if request.form.get("baseline") == "1" or request.args.get("baseline") == "1":
        if runner.run_check_background(kind="baseline", url_ids=ids,
                                       baseline=True):
            flash("Baseline run started — every capture becomes the new "
                  "baseline, nothing is compared. The NEXT run detects edits.")
        else:
            flash("A check is already running.")
        return redirect(url_for("index"))
    want_report = request.form.get("email_report", request.form.get("email_digest")) == "1"
    # the file sweep is part of the run that owes it: starting a page check on
    # top of one means 14k HEAD requests and a fresh capture wave hitting the
    # same throttled store at once, and the sweep's own report going out
    # mid-recapture. RUN_STATE alone said "idle" for the whole 25 minutes.
    import asset_watch as _aw
    if _aw.ASSET_STATE.get("running"):
        flash("The file check from the last run is still going — it finishes "
              "in a few minutes, then a new check can start.")
        return redirect(url_for("index"))
    started_at = db.now_iso()
    if runner.run_check_background(kind="manual", url_ids=ids):
        if ids is None:
            # Bound to THIS run, and only once it really started. Stored as the
            # moment of starting rather than "1": a bare boolean was global,
            # so a check started while the previous run's file sweep was still
            # going had its choice spent on the PREVIOUS run's mail - the
            # earlier run mailed everyone against its starter's wishes and the
            # later run's request was silently eaten. A refused start used to
            # arm it too, leaving a request with no run behind it. The reader
            # only honours a stamp that falls inside the run's own lifetime.
            db.set_setting("email_after_manual_run",
                           started_at if want_report else "")
        if ids:
            flash(f"Checking {len(ids)} page(s)…")
        elif want_report:
            flash("Check started - the report goes out to everyone opted in "
                  "when it finishes.")
        else:
            flash("Check started - no report email for this one.")
    else:
        flash("A check is already running.")
    return redirect(url_for("index"))


@app.route("/pause", methods=["POST"])
def pause_run():
    """Hold a running capture: in-flight pages finish, no new ones start."""
    if not runner.RUN_STATE.get("running"):
        return jsonify({"ok": False, "paused": False})
    runner.RUN_STATE["paused"] = True
    return jsonify({"ok": True, "paused": True})


@app.route("/resume", methods=["POST"])
def resume_run():
    """Release a paused capture so workers pick up where they left off."""
    runner.RUN_STATE["paused"] = False
    return jsonify({"ok": True, "paused": False})


@app.route("/stop", methods=["POST"])
def stop_run():
    """End a running capture for good: in-flight pages finish and are saved,
    nothing new starts, and the run finalises. Everything captured so far is
    kept (each page was committed as it completed)."""
    if not runner.RUN_STATE.get("running"):
        return jsonify({"ok": False})
    runner.RUN_STATE["stopping"] = True
    runner.RUN_STATE["paused"] = False       # release any hold so it can wind down
    return jsonify({"ok": True, "stopping": True})


@app.route("/status")
def status():
    import time as _time
    s = dict(runner.RUN_STATE)
    if not s.get("running"):
        # the file sweep is the run's LAST phase - it used to happen off in
        # the dark ("0%" over a machine 20 minutes from done), so when pages
        # are finished but the sweep is going, the bar shows the sweep
        import asset_watch
        a = asset_watch.ASSET_STATE
        if a.get("running"):
            return jsonify({
                "running": True, "phase": "files", "paused": False,
                "stopping": False, "done": a.get("done", 0),
                "total": a.get("total", 0), "phase_done": a.get("done", 0),
                "phase_total": a.get("total", 0),
                "changed": a.get("changed", 0), "pages_total": 0,
                # carried from the page run that owed this sweep: without
                # them the stepper forgets which passes ran and repaints the
                # finished phases the moment the sweep starts
                "verify_ran": s.get("verify_ran"),
                "retry_ran": s.get("retry_ran"), "sweep_due": True,
                "message": a.get("message", "")})
    if s.get("running") and s.get("started"):
        # How many are actually committed to the database so far - counted
        # rather than trusted, because a page is only really done once its row
        # says so. But "started" is stamped fresh on every RESUME, so this
        # counts THIS session only: on its own it threw away the 1,875 pages a
        # resumed run had carried forward and the bar read "0 no update" over
        # a run that was 55% through. saved0 is that carried base.
        con = db.connect()
        base = s.get("started",)
        # Count each band from the database instead of letting the bar derive
        # green by subtraction. A transiently blocked page increments the error
        # counter but does NOT stamp last_checked (its retry has the last
        # word), so "saved minus errors" subtracted pages saved never held and
        # ran green backwards. Counted directly, each page sits in exactly the
        # state its row says, and the bands cannot contradict each other.
        row = con.execute(
            "SELECT"
            "  SUM(CASE WHEN last_checked >= ? THEN 1 ELSE 0 END) saved,"
            "  SUM(CASE WHEN last_checked >= ? AND IFNULL(last_status,'')"
            "      NOT IN ('error','blank') THEN 1 ELSE 0 END) settled"
            " FROM urls", (base, base)).fetchone()
        s["saved"] = s.get("saved0", 0) + (row["saved"] or 0)
        # green = pages that reached a NON-error final state this session,
        # plus any carried by a resume; the changed ones are peeled off in the
        # bar, the errors live in their own red band from the counter
        s["settled"] = s.get("saved0", 0) + (row["settled"] or 0)
        con.close()
    # rate THIS session only: a resumed run starts with the pages it had
    # already finished on the clock, and dividing those by two seconds of
    # elapsed time reads as "done in a moment"
    did = s.get("done", 0) - s.get("done0", 0)
    if s.get("running") and did >= 3 and s.get("started_ts"):
        elapsed = _time.time() - s["started_ts"]
        if elapsed > 0:
            rate = did / elapsed
            # each parked suspect owes ONE more capture in the verify pass -
            # count that debt now, or the ETA quietly ignores the whole tail
            # and "time doesn't move" once verification starts
            remaining = (max(0, s.get("total", 0) - s["done"])
                         + s.get("suspects", 0))
            s["eta_s"] = int(remaining / rate) if rate > 0 else None
    return jsonify(s)


def _resume_on_boot():
    """Carry on a check the last process was in the middle of.

    RUN_STATE lives in memory, so a restart - a redeploy, a crash, the host
    moving the container - loses the thread while the run row and its per-page
    state survive. Until now that meant every restart ended the check and
    somebody had to notice and press Resume; a check interrupted at 2am sat
    dead until morning, and shipping any change at all cost the run.

    Only a run that is genuinely unfinished is picked up. "End check" marks the
    run finished, so a deliberate stop stays stopped.
    """
    time.sleep(25)                      # let the web server answer first
    if runner.RUN_STATE.get("running"):
        return
    con = db.connect()
    try:
        r = runner._resumable_run(con, include_stopped=False)
        left = con.execute(
            "SELECT COUNT(*) FROM run_items WHERE run_id=? AND state<>'done'",
            (r["id"],)).fetchone()[0] if r else 0
    except Exception:
        return
    finally:
        con.close()
    if not r:
        return
    print(f"[boot] run {r['id']} was interrupted with {left} page(s) left -"
          f" carrying on", flush=True)
    runner.run_check_background(kind="resumed")


@app.route("/settings/backup.db")
def settings_backup():
    """Download a consistent copy of the database.

    The volume this runs on is not backed up by anything, and the database is
    the part that cannot be rebuilt: the page list, who owns what, every
    update anyone has reviewed, every note, the accounts. The screenshots
    beside it are four-fifths of the disk and none of the loss - lose those
    and the next check simply re-baselines.

    sqlite3's own backup API, not a file copy: a copy taken while a check is
    writing is a torn file that opens fine and is wrong in places. This walks
    the database under a read lock, so what comes back is a real point in
    time and safe to take mid-run.

    Gzipped on the way out - a SQLite file is mostly repeated structure and
    compresses to a fraction, which matters over a home connection.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    tmp = Path(tempfile.gettempdir()) / f"iradar-backup-{stamp}.db"
    src = db.connect()
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    def stream():
        # compressed in flight rather than to a second temp file: the copy is
        # already the size of the database, and this box does not have room to
        # hold two of it
        buf = io.BytesIO()
        gz = gzip.GzipFile(fileobj=buf, mode="wb")
        try:
            with open(tmp, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    gz.write(chunk)
                    if buf.tell() >= (1 << 20):
                        yield buf.getvalue()
                        buf.seek(0)
                        buf.truncate(0)
            gz.close()
            if buf.tell():
                yield buf.getvalue()
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    who = current_user()
    print("[backup] %s requested by %s" % (tmp.name, who["email"] if who else "?"),
          flush=True)
    return Response(stream(), mimetype="application/gzip", headers={
        "Content-Disposition":
            'attachment; filename="iradar-backup-%s.db.gz"' % stamp})


@app.route("/settings", methods=["GET", "POST"])
def settings_view():
    if request.method == "POST":
        # the day list arrives as repeated checkboxes; an empty tick set
        # would silently mean "never run", so it falls back to what is stored
        mode = request.form.get("schedule_mode", "days")
        db.set_setting("schedule_mode",
                       mode if mode in SCHEDULE_MODES else "days")
        picked = [d for d in request.form.getlist("check_days") if d in WEEKDAYS]
        if picked:
            db.set_setting("check_days", ",".join(picked))
        elif mode == "days":
            # ticking nothing would mean "never run", which is not a schedule
            # anyone chose on purpose - keep the days already stored
            flash("Pick at least one day, or switch to an interval.")
        # An unticked checkbox sends NOTHING, so the switch is read as
        # present-or-absent - but so does a form that never RENDERED it, and
        # reading that as "off" silently closed the shared link every time a
        # non-owner admin pressed Save. The hidden marker separates the two.
        if request.form.get("guest_link_shown"):
            db.set_setting("guest_link_enabled",
                           "1" if request.form.get("guest_link_enabled") else "0")
        for key in ("hour", "interval_days", "concurrency",
                    "keep_snapshots",
                    "visual_threshold", "height_threshold", "ignore_patterns",
                    "ignore_image_patterns", "dam_search_url", "pim_url",
                    "windchill_search_url", "aem_author_url",
                    *[k for k, _d in cmp.COPY_SETTING_KEYS]):
            if key in request.form:
                val = request.form[key].strip()
                # AN EMPTY LIMIT BOX MEANS "NO LIMIT", which is stored as 0.
                # Storing the empty string instead would read back through
                # copy_limits() as `"" or fallback` - the built-in default -
                # so clearing a box would quietly restore the limit it was
                # clearing. The box shows blank for 0, so this round-trips.
                if key.startswith("copy_") and val == "":
                    val = "0"
                db.set_setting(key, val)
        reschedule()
        flash("Settings saved. Next automatic check: " + next_run_text())
        return redirect(url_for("settings_view"))
    # the Components tab shows the real grid, not a button that leads to it,
    # so it needs everything the matrix needs
    con = db.connect()
    try:
        matrix_ctx = _matrix_context(con)
    finally:
        con.close()
    # the shared-link summary is the owner's, so only pay for it for them
    guest_tot = None
    if is_owner():
        c2 = db.connect()
        try:
            guest_tot = c2.execute(
                "SELECT COUNT(*) v, IFNULL(SUM(hits),0) h, MAX(last_seen) l"
                " FROM guest_visits").fetchone()
        finally:
            c2.close()
    return render_template("settings.html", s=db.get_settings(),
                           next_run=next_run_text(), weekdays=WEEKDAYS,
                           picked_days=check_days(),
                           schedule_hour=schedule_hour(),
                           schedule_mode=schedule_mode(),
                           interval_days=interval_days(),
                           guest_key=GUEST_KEY, base_url=_base_url(),
                           copy_limits=cmp.copy_limits(db.get_settings()),
                           guest_tot=guest_tot, **matrix_ctx)


@app.route("/url/<int:url_id>/level", methods=["POST"])
def url_set_level(url_id):
    """Edit a page's level: an explicit pick is 'manual', 'auto' runs the
    sibling/hierarchy/fingerprint inference, empty clears it."""
    con = db.connect()
    row = con.execute("SELECT * FROM urls WHERE id=?", (url_id,)).fetchone()
    if not row:
        con.close()
        abort(404)
    want = (request.form.get("level") or "").strip()
    valid = set(LEVEL_NAMES) | {""}
    if want == "auto":
        lvl, conf, why = _infer_level(con, row)
        if lvl:
            con.execute("UPDATE urls SET level=?, level_src='inferred'"
                        " WHERE id=?", (lvl, url_id))
            con.commit()
            flash(f"Inferred {lvl} ({conf:.0%} confidence) — {why}")
        else:
            flash(f"Could not infer a level: {why}")
    elif want in valid:
        con.execute("UPDATE urls SET level=?, level_src=? WHERE id=?",
                    (want, "manual" if want else "", url_id))
        con.commit()
        flash(("Level set to " + want + " (manual).") if want
              else "Level cleared.")
    con.close()
    return redirect(url_for("url_detail", url_id=url_id))


# which ownership fields a page type shows (and their display labels), from
# the CSS report: products carry all four, parts BL+PL, apps CM+Owner, promos
# just CM. Design-resources / Home carry none unless a value was imported.
# Just the names. Whoever reads this page knows what BL and PL are, and
# spelling them out every time made the label longer than the value beside it.
META_LABELS = {"bl": "BL", "pl": "PL", "cm": "CM", "owner": "Owner"}
META_FIELDS = {
    "Product": ["bl", "pl", "cm", "owner"],
    "Part": ["bl", "pl"],
    "Application": ["cm", "owner"],
    "Promo": ["cm"],
}


def _page_nav(u):
    """The little breadcrumb bar on a page: its ancestors back to Home, and the
    siblings sitting beside it, so you can move around WITHOUT going back to
    the hierarchy. Ancestors come from the page's own captured breadcrumb (so
    it matches Infineon's own trail); each crumb links to OUR page when we
    track it, else to the live one. Siblings are the pages sharing this page's
    parent path, found by URL prefix - one query, capped, current page marked.
    """
    con = db.connect()
    try:
        try:
            raw = json.loads(u["breadcrumb"] or "[]")
        except Exception:
            raw = []
        raw = [c for c in raw if isinstance(c, dict) and (c.get("title") or "").strip()
               and (c.get("title") or "").strip().lower() != "home"]
        # look up ONLY the handful of crumb URLs (not the whole table): this
        # bar renders on every page view, so it stays two indexed queries
        tracked = {}
        want = [_norm_url(c.get("url") or "") for c in raw if c.get("url")]
        if want:
            marks = ",".join("?" * len(want))
            for r in con.execute(
                    "SELECT id, url, canonical_url FROM urls WHERE active=1"
                    f" AND (rtrim(lower(url),'/') IN ({marks})"
                    f"      OR rtrim(lower(canonical_url),'/') IN ({marks}))",
                    want + want):
                tracked[_norm_url(r["url"])] = r["id"]
                if r["canonical_url"]:
                    tracked.setdefault(_norm_url(r["canonical_url"]), r["id"])
        home = con.execute("SELECT id FROM urls WHERE ptype='Home'"
                           " AND active=1 LIMIT 1").fetchone()
        crumbs = []
        if home:
            crumbs.append({"title": "Home", "page_id": home["id"], "url": "",
                           "here": False})
        for c in raw:
            cu = (c.get("url") or "").strip()
            crumbs.append({"title": c["title"].strip(), "url": cu,
                           "page_id": tracked.get(_norm_url(cu)),
                           "here": False})
        # the page itself is the last crumb (its own breadcrumb usually ends at
        # it; a part's ends at its family, so add it when missing)
        if not crumbs or crumbs[-1].get("page_id") != u["id"]:
            crumbs.append({"title": "", "url": u["url"], "page_id": u["id"],
                           "here": True})
        else:
            crumbs[-1]["here"] = True

        # siblings: pages directly under this page's parent path
        tax = _tax_norm(u["canonical_url"] or u["url"])
        parent = tax.rsplit("/", 1)[0] if "/" in tax.strip("/") else ""
        siblings = []
        if parent and parent.count("/") >= 1:
            # a PREFIX match, so SQLite can use the url index instead of
            # walking the table; depth is then checked in python
            pfx = "https://www.infineon.com" + parent + "/"
            for r in con.execute(
                    "SELECT id, url, canonical_url,"
                    " COALESCE(NULLIF(title_override,''),"
                    "  (SELECT s.page_title FROM snapshots s WHERE s.url_id=urls.id"
                    "    AND s.status='ok' AND s.page_title<>''"
                    "    ORDER BY s.id DESC LIMIT 1)) AS t"
                    " FROM urls WHERE active=1"
                    " AND IFNULL(last_status,'') NOT IN ('notlive','blank')"
                    " AND (url LIKE ? OR canonical_url LIKE ?) LIMIT 400",
                    (pfx + "%", pfx + "%")):
                p = _tax_norm(r["canonical_url"] or r["url"])
                if p.rsplit("/", 1)[0] == parent and p != parent:
                    siblings.append({"id": r["id"],
                                     "title": (r["t"] or r["url"].rstrip("/")
                                               .rsplit("/", 1)[-1]),
                                     "here": r["id"] == u["id"]})
        siblings.sort(key=lambda s: s["title"].lower())

        # children: the pages one level BENEATH this one, so the bar navigates
        # forward as well as back
        children = []
        if tax.count("/") >= 1:
            cpfx = "https://www.infineon.com" + tax + "/"
            for r in con.execute(
                    "SELECT id, url, canonical_url,"
                    " COALESCE(NULLIF(title_override,''),"
                    "  (SELECT s.page_title FROM snapshots s WHERE s.url_id=urls.id"
                    "    AND s.status='ok' AND s.page_title<>''"
                    "    ORDER BY s.id DESC LIMIT 1)) AS t"
                    " FROM urls WHERE active=1 AND id<>?"
                    " AND IFNULL(last_status,'') NOT IN ('notlive','blank')"
                    " AND (url LIKE ? OR canonical_url LIKE ?) LIMIT 400",
                    (u["id"], cpfx + "%", cpfx + "%")):
                p = _tax_norm(r["canonical_url"] or r["url"])
                if p.rsplit("/", 1)[0] == tax:
                    children.append({"id": r["id"],
                                     "title": (r["t"] or r["url"].rstrip("/")
                                               .rsplit("/", 1)[-1])})
        children.sort(key=lambda s: s["title"].lower())
        return crumbs, {"siblings": siblings[:40], "children": children[:40]}
    finally:
        con.close()


@app.route("/url/<int:url_id>/meta", methods=["POST"])
def url_set_meta(url_id):
    """Edit the ownership fields (BL / PL / CM / Owner) - hand-editable like the
    level, kept on the url row so they survive re-capture. Editing clears the
    import misalignment flag: the human has now had the last word."""
    con = db.connect()
    if not con.execute("SELECT 1 FROM urls WHERE id=?", (url_id,)).fetchone():
        con.close(); abort(404)
    for f in ("bl", "pl", "cm", "owner"):
        if f in request.form:
            con.execute(f"UPDATE urls SET {f}=? WHERE id=?",
                        ((request.form.get(f) or "").strip()[:200], url_id))
    con.execute("UPDATE urls SET meta_flag='' WHERE id=?", (url_id,))
    con.commit(); con.close()
    flash("Ownership fields saved.")
    return redirect(url_for("url_detail", url_id=url_id))


@app.route("/url/<int:url_id>/title", methods=["POST"])
def url_set_title(url_id):
    """Override a page's display name (kept on the url row, so it survives
    re-capture and shows everywhere the name appears). Empty clears it and the
    page's own captured <title> takes over again."""
    con = db.connect()
    if not con.execute("SELECT 1 FROM urls WHERE id=?", (url_id,)).fetchone():
        con.close()
        abort(404)
    name = " ".join((request.form.get("title") or "").split())[:200]
    con.execute("UPDATE urls SET title_override=? WHERE id=?", (name, url_id))
    con.commit()
    con.close()
    flash(f"Name set to “{name}”." if name
          else "Custom name cleared — the page's own title shows again.")
    return redirect(url_for("url_detail", url_id=url_id))


def _tax_norm(u):
    """A taxonomy URL reduced to its bare site path: scheme/host, query,
    fragment, trailing slash and /de/ /ja/ language prefixes dropped."""
    n = (u or "").split("#")[0].split("?")[0].rstrip("/")
    n = re.sub(r"^https?://[^/]+", "", n).lower()
    return re.sub(r"^/(de|ja|cn|tw|kr|jp)(?=/)", "", n)


def _redirect_aliases(con):
    """{old_taxonomy_path: new_path} for every page the site MOVED (the
    redirect repoint keeps the old address in redirect_from). Children whose
    URLs still live under the old path follow the page to its new home -
    in the tree and in the level walk alike - instead of dangling under an
    address that no longer answers."""
    out = {}
    for r in con.execute("SELECT url, canonical_url, redirect_from FROM urls"
                         " WHERE IFNULL(redirect_from,'')<>''"):
        old = _tax_norm(r["redirect_from"])
        new = _tax_norm(r["canonical_url"] or r["url"])
        if (old.startswith("/products/") and new.startswith("/products/")
                and old != new):
            out[old] = new
    return out


def _apply_aliases(path, aliases):
    """Rewrite a taxonomy path whose prefix the site moved elsewhere
    (longest moved prefix wins)."""
    if not aliases:
        return path
    for old in sorted(aliases, key=len, reverse=True):
        if path == old or path.startswith(old + "/"):
            return aliases[old] + path[len(old):]
    return path


def _log_level(con, url_id, old, new, src, why):
    con.execute("INSERT INTO level_log(ts, url_id, old_level, new_level,"
                " src, why) VALUES(?,?,?,?,?,?)",
                (db.now_iso(), url_id, old or "", new or "", src, why))


def _structure_registry(con, aliases=None):
    """Every product-taxonomy URL the SITE itself declares: each tracked
    page's canonical/short URL plus every child tile from the captured
    category/family navigation modules. The tiles matter: they tell us a page
    has children even when we do not track those children, so a sub-family is
    never mistaken for an end family just because our list stops there."""
    reg = set()
    # A dead or blank page is not a child. This registry is what decides
    # sub-family from end family - "the site declares something beneath it" -
    # and a 404 sitting under a page declares nothing except that an address
    # used to work. Counting it turns a real end family into a sub-family.
    # (Today 25 addresses here exist only because of dead pages, and no live
    # page is mis-levelled by them yet; that is luck, not design.)
    for r in con.execute(
            "SELECT url, canonical_url FROM urls WHERE active=1"
            " AND IFNULL(last_status,'') NOT IN ('notlive','blank','error')"):
        for u in (r["url"], r["canonical_url"]):
            n = _tax_norm(u)
            if n.startswith("/products/"):
                reg.add(_apply_aliases(n, aliases))
    # the navigation tiles stay: those come from a LIVE page's own category
    # module, so they are the site declaring children even where we track none
    for r in con.execute("SELECT DISTINCT item_url FROM items"
                         " WHERE category IN ('categories','products')"
                         " AND item_url<>''"):
        n = _tax_norm(r["item_url"])
        if n.startswith("/products/"):
            reg.add(_apply_aliases(n, aliases))
    return reg


def _rebuild_structure():
    """Assign levels TOP-DOWN from the site's own structure.

    Walk the taxonomy from the roots: a page's level is its place on the walk
    (segments under /products/ - 1 category, 2 family, deeper sub-family when
    the site declares children beneath it, else end family; design resources
    likewise via dr_level_for). Parts hang off their breadcrumb parent (the
    OPN table's contents are deliberately not stored), promo pages have no
    hierarchy, and three things are NEVER touched: manual edits, EB boards,
    and AFB pages (kinds, not depths). Applications are placed top-down in
    the tree but keep report/manual levels - the calibration shows URL depth
    cannot split AOP from ASP (depth 2 is 19 AOP / 9 ASP), so writing depth
    there would corrupt data. Returns (changed, filled, kept_manual)."""
    con = db.connect()
    aliases = _redirect_aliases(con)
    reg = _structure_registry(con, aliases)
    changed, filled, kept = [], [], 0
    # Only pages a visitor actually gets. A 404 or a blank shell is not part of
    # the structure - it is already kept out of the tree and out of the counts,
    # and level inference already skips it, but this walk was still writing
    # levels onto 706 of them. Whatever level a page last held stays, as the
    # record of where it was; nothing new is written to a page that is gone.
    for r in con.execute("SELECT id, url, canonical_url, ptype, level,"
                         " level_src FROM urls WHERE active=1"
                         " AND ptype IN ('Product','Design resources')"
                         " AND IFNULL(last_status,'')"
                         "     NOT IN ('notlive','blank','error')"):
        cur, src = r["level"] or "", r["level_src"] or ""
        if src == "manual":
            kept += 1
            continue
        tax = _apply_aliases(_tax_norm(r["canonical_url"] or r["url"]),
                             aliases)
        if r["ptype"] == "Product":
            if not tax.startswith("/products/"):
                continue
            s = [x for x in tax[len("/products/"):].split("/") if x]
            if not s:
                continue
            if len(s) == 1:
                calc, why = "PC", "site walk: 1 segment under /products/"
            elif len(s) == 2:
                calc, why = "PFP", "site walk: 2 segments under /products/"
            elif any(x.startswith(tax + "/") for x in reg):
                calc = "PSFP"
                why = (f"site walk: {len(s)} segments, the site declares"
                       " children beneath it")
            else:
                calc = "PEFP"
                why = f"site walk: {len(s)} segments, dead end (no children)"
        else:
            calc = db.dr_level_for(r["canonical_url"] or r["url"])
            if not calc:
                continue
            why = "design-resources depth"
        if calc == cur:
            continue
        con.execute("UPDATE urls SET level=?, level_src='structure'"
                    " WHERE id=?", (calc, r["id"]))
        _log_level(con, r["id"], cur, calc, "structure", why)
        (filled if not cur else changed).append(
            f"#{r['id']} {cur or '·'}→{calc}")
    con.commit()
    con.close()
    return changed, filled, kept


def _infer_missing_levels():
    """Assign a level to every CAPTURED Product/Application page that lacks one,
    via the three-signal inference. Shared by the manual button and the
    post-run watcher. Returns (assigned_labels, skipped_count)."""
    con = db.connect()
    # Only pages we actually CAPTURED (a live, non-dead snapshot). Inference
    # reads siblings/breadcrumb/components off the real page, so a dead page
    # (404 'notlive', or a 'blank' shell) has nothing to infer from - guessing
    # a level for a page we could not open would be fabrication. Type and the
    # URL-depth levels (DR/Part) are assigned deterministically elsewhere and
    # are unaffected by this filter.
    rows = con.execute(
        "SELECT * FROM urls WHERE ptype IN ('Product','Application','Part')"
        " AND IFNULL(level,'')='' AND last_checked IS NOT NULL"
        " AND IFNULL(last_status,'') NOT IN ('notlive','blank')"
        " AND IFNULL(last_http,200) NOT IN (404,410)").fetchall()
    done, skipped = [], 0
    for row in rows:
        lvl, conf, why = _infer_level(con, row)
        if lvl:
            con.execute("UPDATE urls SET level=?, level_src='inferred'"
                        " WHERE id=?", (lvl, row["id"]))
            _log_level(con, row["id"], row["level"] or "", lvl, "inferred",
                       f"{conf:.0%} confidence — {why}")
            done.append(f"{row['url'].rsplit('/', 1)[-1]}→{lvl}")
        else:
            skipped += 1
    con.commit()
    con.close()
    return done, skipped


# The CSS report was a one-off. Its ownership (BL/PL/CM/Owner) now lives in
# our own database, which IS the report from here on - re-reading a workbook on
# every start would only let a stale file overwrite edits made here. The
# importer is still in the repo if a genuinely new report ever arrives; nothing
# calls it.


def _housekeeping(why):
    """Every tidy-up job, in dependency order, run as one unit.

    These used to be startup-only, which was backwards: a RUN is what creates
    the mess they clean. A run adds pages it was redirected to, writes fresh
    item rows, and re-flags heroes - so the tracking-param strip, the furniture
    sweep, the responsive-image dedup and the hero re-check all had to wait for
    the next restart to catch up. On a weekly schedule that could be days of
    the dashboard describing a state that had already moved.

    All five together cost about 150 ms on the full corpus, so there is no
    reason to be clever about when they run. Order matters, though: the sweeps
    rewrite item titles and urls, so the search index is rebuilt after them,
    and the site-wide-image decision is refreshed last because it reads the
    swept, deduplicated result.
    """
    t0 = time.time()
    _normalize_statuses()      # retired status names -> current ones
    _normalize_urls()          # strip tracking params, merge duplicate pages
    _sweep_furniture()         # drop spinner/logo rows that are not content
    _sweep_images()            # collapse ?wid= renditions, drop avatars
    _sweep_hero_flags()        # a placeholder hero is not a real hero
    _refresh_item_index(why)   # substring search index, after the rewrites
    _prune_history()           # cap the archive: N captures per page, no more
    _cap_preview_cache()       # the document thumbnails are not allowed to grow forever
    try:
        _AGG_CACHE.pop("generic_images", None)
        n = len(_generic_images())
        print(f"[housekeeping] {why}: done in {time.time() - t0:.1f}s"
              f" ({n} site-wide images)", flush=True)
    except Exception as e:
        print(f"[housekeeping] {why}: {e}", flush=True)


PREVIEW_CACHE_CAP = 400 * 1024 ** 2        # 400 MB


def _cap_preview_cache(limit=PREVIEW_CACHE_CAP):
    """Hold the document-thumbnail cache under a ceiling, oldest first.

    The cache only ever dropped ORPHANS - previews whose document had moved on
    - so its floor was "one image per document in the corpus" and it grew with
    the catalogue: cleared to nothing on 6 Sep, it was back to 1.1 GB inside a
    day and helped push a 4.6 GB volume to 94%, having already killed one run
    outright when the disk hit 100%. Every file here is regenerable on demand,
    so the only real cost of evicting one is re-rendering it if somebody asks.
    Least-recently-used goes first, which keeps whatever people actually open.
    """
    try:
        cache = db.DATA_DIR / "previews"
        if not cache.exists():
            return
        files = []
        for f in cache.glob("*.*"):
            try:
                st = f.stat()
                files.append((st.st_atime, st.st_size, f))
            except OSError:
                continue
        total = sum(f[1] for f in files)
        if total <= limit:
            return
        files.sort()                        # oldest access first
        freed = 0
        for _atime, size, f in files:
            if total - freed <= limit:
                break
            try:
                f.unlink()
                freed += size
            except OSError:
                pass
        print("[housekeeping] preview cache %.0f MB -> %.0f MB (freed %.0f MB)"
              % (total / 1024 ** 2, (total - freed) / 1024 ** 2,
                 freed / 1024 ** 2), flush=True)
    except Exception as e:
        print("[housekeeping] preview cap skipped: %s" % e, flush=True)


def _prune_history():
    """Hold the capture archive at the configured depth (Settings -> history).

    Deliberately last in housekeeping and wrapped: reclaiming disk must never
    be the thing that sinks a run's tidy-up, and it reads the change table,
    which the steps above have just finished writing.
    """
    try:
        gone, freed = db.prune_history()
        if gone:
            print(f"[housekeeping] pruned {gone} old capture(s),"
                  f" freed {freed / 1024 ** 3:.2f} GB", flush=True)
    except Exception as e:
        print(f"[housekeeping] prune skipped: {e}", flush=True)


def _refresh_item_index(why):
    """Rebuild the item substring index if items have moved since it was built.

    Search checks freshness itself and falls back to scanning, so this is only
    about keeping the fast path available - never about correctness.
    """
    con = db.connect()
    try:
        if db.items_fts_fresh(con):
            return 0
        t0 = time.time()
        n = db.rebuild_items_fts(con)
        print(f"[search] item index rebuilt ({why}): {n:,} items"
              f" in {time.time() - t0:.1f}s", flush=True)
        return n
    except Exception as e:
        print(f"[search] item index rebuild failed ({why}): {e}", flush=True)
        return 0
    finally:
        con.close()


def _post_run_watcher():
    """After each capture run finishes, auto-infer the levels of any new
    Product/Application pages - so a freshly-added or newly-reached page gets
    its PC/PFP/ADP/... shape without waiting for a manual click."""
    prev = False
    while True:
        time.sleep(30)
        running = runner.RUN_STATE.get("running", False)
        if prev and not running:
            # ONE try around all four made the last of them optional. They are
            # independent jobs sharing only a moment, and _housekeeping is the
            # one that rebuilds the substring search index - so a throw in the
            # structure pass above it took the index rebuild with it, silently,
            # and every library search fell back to scanning 199,820 rows until
            # somebody restarted the app. The bare `except Exception: pass`
            # meant no log line said so either. Each job now stands alone and
            # says when it fails.
            #
            # structure first (deterministic, from the site's own walk),
            # inference after for what structure cannot place (apps/parts)
            try:
                chg, fill, _kept = _rebuild_structure()
                if chg or fill:
                    print(f"[post-run] structure: {len(chg)} corrected,"
                          f" {len(fill)} filled", flush=True)
            except Exception as e:
                print(f"[post-run] structure failed: {e}", flush=True)
            try:
                done, _ = _infer_missing_levels()
                if done:
                    print(f"[post-run] inferred {len(done)} level(s)", flush=True)
            except Exception as e:
                print(f"[post-run] level inference failed: {e}", flush=True)
            try:
                joined = _matrix_autojoin()
                if joined:
                    print(f"[post-run] matrix learned {len(joined)} new"
                          f" component slot(s): {', '.join(joined[:6])}"
                          + ("…" if len(joined) > 6 else ""), flush=True)
            except Exception as e:
                print(f"[post-run] matrix autojoin failed: {e}", flush=True)
            try:
                # a run is what CREATES the mess these clean up, so this is
                # where they belong - not only at the next restart
                _housekeeping("after run")
            except Exception as e:
                print(f"[post-run] housekeeping failed: {e}", flush=True)
            try:
                # the file sweep runs INSIDE the chain, synchronously: the
                # report below quotes its count, so mailing first meant the
                # email's file number was minutes stale every single week.
                # /status surfaces it as the run's last visible phase.
                if runner.RUN_STATE.pop("asset_check_due", False):
                    import asset_watch
                    asset_watch.run_asset_check()
            except Exception as e:
                print(f"[post-run] file sweep failed: {e}", flush=True)
            _collect_mentions_after_run()
            try:
                _email_after_run()
            except Exception as e:
                print(f"[post-run] email report failed: {e}", flush=True)
        prev = running


def _email_after_run():
    """Email the report to everyone opted in, once a REAL check finishes.

    Guarded the same way mention collection is: never for a token run - a
    three-page manual poke does not earn a bulletin - and dark unless mail is
    configured. Each person is sent individually to their own address, so one
    bad address cannot stop the rest, and a send never raises into the caller.
    """
    if not mailer.configured():
        return
    con = db.connect()
    try:
        run = con.execute("SELECT * FROM runs WHERE finished IS NOT NULL"
                          " ORDER BY id DESC LIMIT 1").fetchone()
        if not run or (run["total"] or 0) < 50:
            return                       # token run, or nothing to summarise
        # WHO asked for this mail. The weekly scheduled check always reports;
        # a manual run only when whoever started it ticked "email the report"
        # - a by-hand re-check is usually a private look, and before this
        # gate every full manual run mailed all seven people unasked. A
        # baseline run never mails: by definition everything "changed".
        # ONE BATCH PER RUN, EVER - and this is checked FIRST, before the
        # request flag is touched. Checked after, a stop/crash/resume cycle
        # spent the ticked request on a run that had already mailed, and the
        # run the person actually asked about went out silent.
        if str(db.get_settings().get("last_emailed_run") or "") == str(run["id"]):
            print("[email] run %s already emailed - skipping" % run["id"],
                  flush=True)
            return
        kind = (run["kind"] or "").lower()
        if kind != "scheduled":
            # the request is honoured only when it was made DURING this run -
            # a stamp from a later run cannot mail an earlier one, and one
            # left over from an abandoned run cannot mail anything
            stamp = db.get_settings().get("email_after_manual_run") or ""
            wanted = bool(stamp) and (run["started"] or "") <= stamp <= (
                run["finished"] or "9999")
            if kind == "manual" and wanted:
                db.set_setting("email_after_manual_run", "")
            else:
                print("[email] %s run %s - report not requested, skipping"
                      % (kind or "unkinded", run["id"]), flush=True)
                return
        recipients = con.execute(
            "SELECT email FROM users WHERE active=1 AND IFNULL(email_opt_in,0)=1"
            " AND email<>''").fetchall()
        if not recipients:
            return
        # READ THE PREVIOUS MARKER FIRST - it is overwritten a line below, and
        # reading it after would only ever hand back this run's own start.
        # The window opens where the last REPORTED run opened, not where this
        # one did: a manual check nobody asked to mail still opens changes,
        # and scoped to this run alone they would never appear in any report.
        prev = con.execute(
            "SELECT started FROM runs WHERE id=?",
            (db.get_settings().get("last_emailed_run") or -1,)).fetchone()
        since = prev["started"] if prev else None
        # claimed only once there is somebody to mail: burning the marker on a
        # run with no recipients spent the run's one batch on nothing
        db.set_setting("last_emailed_run", str(run["id"]))
        d = mailer.run_report(con, run, since=since)
        when = _report_when(run)
        html = mailer.render_report(d, _base_url(), when)
        subj = "iRadar report — %s" % (
            "no changes" if d["updates"] == 0 and d["file_updates"] == 0
            else "%d update%s to review"
            % (d["updates"] or d["file_updates"],
               "" if (d["updates"] or d["file_updates"]) == 1 else "s"))
    finally:
        con.close()
    sent = fail = 0
    for r in recipients:
        err = mailer.send(r["email"], subj, html)
        if err:
            fail += 1
            print(f"[email] {r['email']}: {err}", flush=True)
        else:
            sent += 1
    print(f"[email] report sent to {sent} recipient(s), {fail} failed",
          flush=True)


def _collect_mentions_after_run():
    """Mentions refresh themselves after every real check, so nobody has to
    remember a button. The guard mirrors the button's own: never while a
    capture is running (they share the network), and a tiny partial run - a
    three-page manual poke - does not earn a forty-minute collection pass."""
    try:
        import sentiment as sn
        con = db.connect()
        last = con.execute(
            "SELECT total FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        con.close()
        if not last or (last["total"] or 0) < 50:
            return
        if runner.RUN_STATE.get("running"):
            return
        if sn.run_collect_background():
            print("[post-run] collecting mentions across "
                  + ", ".join(s["label"] for s in sn.sources_ready()),
                  flush=True)
    except Exception as exc:
        print(f"[post-run] mention collection not started: {exc}", flush=True)


@app.route("/levels/infer-missing", methods=["POST"])
def levels_infer_missing():
    """One click: every level-less Product/Application page gets the
    three-signal inference; each assignment is reported."""
    done, skipped = _infer_missing_levels()
    flash((f"Inferred {len(done)} level(s): " + ", ".join(done[:10])
           + ("…" if len(done) > 10 else "")) if done
          else "Every typed page already has a level.")
    if skipped:
        flash(f"{skipped} page(s) had no evidence yet (not captured).")
    return redirect(request.referrer or url_for("settings_components"))


def _matrix_context(con):
    """Everything the component matrix needs to render.

    Pulled out of settings_components so the Settings tab can show the grid
    itself rather than a button that leads to it. One gatherer, so the two
    never drift into showing different numbers for the same thing.
    """
    counts = _component_counts(con)
    present = {_etype(r["ptype"], r["url"], r["level"]) for r in con.execute(
        "SELECT ptype, url, level FROM urls WHERE ptype<>''")}
    types = ([t for t in ETYPE_ORDER if t in present]
             + sorted(present - set(ETYPE_ORDER)))
    matrix = _effective_matrix(con)
    comps = _all_components()
    maxn = {t: max(list(counts.get(t, {}).values()) or [1]) for t in types}
    # a component with zero everywhere and no manual tick has no business on
    # the grid - its row hides unless the empty cells are opened for editing
    row_any = {key: (any(counts.get(t, {}).get(key) for t in types)
                     or any(key in matrix.get(t, ()) for t in types))
               for key, _l, _k, _a in comps}
    return {
        "comps": comps, "types": types, "matrix": matrix, "counts": counts,
        "maxn": maxn, "row_any": row_any,
        "custom_keys": {c[0] for c in _custom_components()},
        "pending": con.execute(
            "SELECT COUNT(*) FROM urls WHERE active=1"
            " AND (components IS NULL OR components='')").fetchone()[0],
        "missing_levels": con.execute(
            "SELECT COUNT(*) FROM urls WHERE ptype IN"
            " ('Product','Application','Part')"
            " AND (level IS NULL OR level='')").fetchone()[0],
        "saved": bool((db.get_settings().get("component_matrix") or "").strip()),
        "state": runner.RUN_STATE,
    }


@app.route("/settings/components", methods=["GET", "POST"])
def settings_components():
    """The page-type x component matrix: which components each page type is
    expected to carry. Seeded from the captured data (cell counts show how many
    pages of the type actually have the component); every cell is editable, a
    custom component can be added (detected by its section-nav label) or
    deleted, and Prune unchecks everything that appears on zero pages."""
    con = db.connect()
    counts = _component_counts(con)
    # the matrix speaks effective types - one column per page SHAPE: product
    # levels, PTP/EB, application levels - in taxonomy order
    present = {_etype(r["ptype"], r["url"], r["level"]) for r in con.execute(
        "SELECT ptype, url, level FROM urls WHERE ptype<>''")}
    types = ([t for t in ETYPE_ORDER if t in present]
             + sorted(present - set(ETYPE_ORDER)))
    pending = con.execute(
        "SELECT COUNT(*) FROM urls WHERE active=1"
        " AND (components IS NULL OR components='')").fetchone()[0]
    missing_levels = con.execute(
        "SELECT COUNT(*) FROM urls WHERE ptype IN"
        " ('Product','Application','Part')"
        " AND (level IS NULL OR level='')").fetchone()[0]
    # computed ONCE, before first use: row_any below reads it, and Python
    # treats `matrix` as local for the whole function - assigning it only
    # later in the POST branches made any zero-count component (e.g. a
    # freshly added custom one) crash the page with UnboundLocalError
    matrix = _effective_matrix(con)
    con.close()
    # per-type max count, for the heatmap tint (each column self-normalises)
    maxn = {t: max(list(counts.get(t, {}).values()) or [1]) for t in types}
    # a component with zero everywhere (and no manual tick) has no business on
    # the grid - its row hides unless the user opens the empty cells to edit
    row_any = {}
    for key, _l, _k, _a in _all_components():
        row_any[key] = (any(counts.get(t, {}).get(key) for t in types)
                        or any(key in matrix.get(t, ()) for t in types))

    if request.method == "POST":
        action = request.form.get("action", "save")
        if action == "add_custom":
            label = (request.form.get("label") or "").strip()[:60]
            if label:
                customs = _custom_components()
                key = "custom_" + re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
                if key not in {c[0] for c in customs} and key not in COMPONENT_BY_KEY:
                    stored = [{"key": c[0], "label": c[1]} for c in customs]
                    stored.append({"key": key, "label": label})
                    db.set_setting("custom_components",
                                   json.dumps(stored, ensure_ascii=False))
                    flash(f"Added component “{label}” — it is detected by that "
                          "label appearing in a page's own section navigation.")
                else:
                    flash("That component already exists.")
            return redirect(url_for("settings_components"))
        if action.startswith("del_custom"):
            key = action.split(":", 1)[1] if ":" in action else ""
            stored = [{"key": c[0], "label": c[1]} for c in _custom_components()
                      if c[0] != key]
            db.set_setting("custom_components",
                           json.dumps(stored, ensure_ascii=False))
            # drop it from the saved matrix too - but ONLY if one is actually
            # saved: writing '{}' here once overrode the data-driven matrix
            # with "no type has any component" and un-unified every url page
            try:
                raw = (db.get_settings().get("component_matrix") or "").strip()
                if raw:
                    m = json.loads(raw)
                    if isinstance(m, dict) and m:
                        m = {t: [k for k in v if k != key] for t, v in m.items()}
                        db.set_setting("component_matrix", json.dumps(m))
            except Exception:
                pass
            flash("Component removed.")
            return redirect(url_for("settings_components"))
        if action == "prune":
            # after the run: a component that appears on ZERO pages of a type
            # never showed up there - drop it from that type's expected list
            # (matrix already computed above, before the connection closed)
            removed = []
            for t in list(matrix):
                for k in list(matrix[t]):
                    if not counts.get(t, {}).get(k):
                        matrix[t].discard(k)
                        lbl = next((c[1] for c in _all_components()
                                    if c[0] == k), k)
                        removed.append(f"{lbl} ({t})")
            db.set_setting("component_matrix",
                           json.dumps({t: sorted(v) for t, v in matrix.items()}))
            flash(("Unticked " + str(len(removed)) + " never-seen pairing(s): "
                   + ", ".join(removed[:12])
                   + ("…" if len(removed) > 12 else "")) if removed
                  else "Nothing to untick — every expected component appears somewhere.")
            return redirect(url_for("settings_components"))
        # default: save the whole grid as drawn
        matrix = {}
        for t in types:
            matrix[t] = [c[0] for c in _all_components()
                         if request.form.get(f"cell|{t}|{c[0]}")]
        db.set_setting("component_matrix", json.dumps(matrix))
        flash("Component matrix saved — the url pages now answer from it.")
        return redirect(url_for("settings_components"))

    saved = bool((db.get_settings().get("component_matrix") or "").strip())
    return render_template("settings_components.html",
                           comps=_all_components(), types=types,
                           matrix=matrix, counts=counts, saved=saved, maxn=maxn,
                           row_any=row_any,
                           custom_keys={c[0] for c in _custom_components()},
                           pending=pending, missing_levels=missing_levels,
                           state=runner.RUN_STATE)


# --------------------------------------------------------------------------
# Typo-tolerant content search. Every distinct item (title + key + category +
# meta) is cached and scored with rapidfuzz, so a spelling mistake
# ("aplication note", "datsheet", "optiga tmp") still finds the document. The
# cache refreshes in a background thread, so a live capture's new items show up
# within ~a minute and a keystroke never waits on the rebuild.
_SEARCH_TTL = 60.0
_search_cache = {"ts": -1e9, "data": [], "titles": [], "building": False}


def _rebuild_search_cache():
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT item_key AS k, MIN(title) AS t, MIN(category) AS c,"
            " MIN(meta) AS m, MIN(item_url) AS u, MIN(grp) AS g,"
            " COUNT(DISTINCT url_id) AS n FROM items"
            " WHERE category<>'about' AND item_key<>'' GROUP BY item_key"
        ).fetchall()
    finally:
        con.close()
    data = []
    for r in rows:
        cat, title, url = r["c"], r["t"] or "", r["u"] or ""
        if cat == "images":
            # site chrome - the spinner, logos, partner carousels that ride on
            # every page - is not page content and never belongs in search
            if r["n"] > 100:
                continue
            title = pretty_image_title(title, url)
        # searching works on title OR url: the url's last segment, de-dashed,
        # joins the searchable string ("asic hero image png asset 680 2x1")
        slug = ""
        if url:
            try:
                slug = urlsplit(url).path.rsplit("/", 1)[-1]
            except Exception:
                slug = ""
        data.append({"key": r["k"], "title": title, "cat": cat,
                     "meta": r["m"] or "", "url": url, "grp": r["g"] or "",
                     "tl": title.lower(),
                     "sl": _searchable(title + " " + slug)})
    _search_cache.update(ts=time.monotonic(), data=data,
                         titles=[d["sl"] for d in data], building=False)


def _choices():
    """Cached (data, titles) for every distinct item; rebuilt in the background
    once stale so it never stalls a request after the first build."""
    if not _search_cache["data"]:
        _rebuild_search_cache()
    elif (time.monotonic() - _search_cache["ts"] > _SEARCH_TTL
          and not _search_cache["building"]):
        _search_cache["building"] = True
        threading.Thread(target=_rebuild_search_cache, daemon=True).start()
    return _search_cache["data"], _search_cache["titles"]


def _fuzzy_items(q, cat, limit, exclude=None):
    """Distinct items ranked by typo-tolerant relevance to q, best first.
    Exact substring hits always rank above fuzzy ones. `exclude` drops whole
    categories (e.g. 'overview' from the type-ahead - it is page text, not an
    asset you jump to)."""
    data, titles = _choices()
    ql = _searchable(q)
    if not ql:
        return []
    if exclude:
        pairs = [(d, t) for d, t in zip(data, titles) if d["cat"] not in exclude]
        data = [p[0] for p in pairs]
        titles = [p[1] for p in pairs]
    fn = PILL_FN.get(_norm_pill(cat))
    if fn:
        pairs = [(d, t) for d, t in zip(data, titles) if fn(d)]
        data = [p[0] for p in pairs]
        titles = [p[1] for p in pairs]
    scored = {}                      # key -> (tier, score, data)
    for d in data:                   # exact substring on title OR url: top tier
        if ql in d["sl"]:
            scored[d["key"]] = (2, 100, d)
    # rapidfuzz WRatio forgives spelling mistakes and word reordering
    for _t, score, i in process.extract(
            ql, titles, scorer=fuzz.WRatio, limit=max(limit * 3, 40),
            score_cutoff=72, processor=str.lower):
        d = data[i]
        scored.setdefault(d["key"], (1, score, d))
    ranked = sorted(scored.values(),
                    key=lambda s: (-s[0], -s[1], len(s[2]["title"])))
    return [s[2] for s in ranked[:limit]]


def _pages_for_keys(con, keys):
    if not keys:
        return {}
    marks = ",".join("?" * len(keys))
    return {r["item_key"]: r["n"] for r in con.execute(
        f"SELECT item_key, COUNT(DISTINCT url_id) n FROM items"
        f" WHERE item_key IN ({marks}) GROUP BY item_key", keys)}


def _search_keys(con, q, cat, limit):
    """Ranked item_keys for a query: typo-tolerant title matches first, then
    any exact substring hit on a title, URL fragment (doc numbers) or meta."""
    ordered = [d["key"] for d in _fuzzy_items(q, cat, limit)]
    seen = set(ordered)
    toks = [t for t in q.lower().split() if len(t) >= 2][:6]
    if toks:
        for k in _substring_keys(con, toks, cat):
            if k not in seen:
                seen.add(k)
                ordered.append(k)
    return ordered[:limit]


def _substring_keys(con, toks, cat):
    """item_keys whose title, meta or URL contains EVERY token.

    Served by the trigram index, which answers the same substring question as
    LIKE '%tok%' but from an index instead of a 199k-row scan - two seconds
    down to under a millisecond. Trigram needs 3 characters to work with, and
    the index can only be trusted while it matches the items table, so a short
    token or a stale index falls back to the scan this replaced.
    """
    short = [t for t in toks if len(t) < 3]
    if not short and db.items_fts_fresh(con):
        # each token is a quoted phrase; ANDed, matching the old query
        match = " AND ".join('"%s"' % t.replace('"', '""') for t in toks)
        cats = list(PILL_SQL_CATS.get(_norm_pill(cat)) or ())
        # the index holds one row per DISTINCT item and knows no category, so a
        # pill-scoped search joins back to items. The join has to happen INSIDE
        # the query: capping first and filtering after would spend the 500-row
        # budget on items the pill excludes, and silently drop real matches.
        if cats:
            sql = ("SELECT DISTINCT f.item_key AS item_key FROM items_fts f"
                   " JOIN items i ON i.item_key = f.item_key"
                   " WHERE f MATCH ? AND i.category IN ("
                   + ",".join("?" * len(cats)) + ") LIMIT 500")
            args = [match] + cats
        else:
            sql = ("SELECT item_key FROM items_fts WHERE items_fts MATCH ?"
                   " LIMIT 500")
            args = [match]
        try:
            return [r["item_key"] for r in con.execute(sql, args)]
        except sqlite3.OperationalError:
            pass                             # malformed MATCH - scan instead
    conds, params = [], []
    for t in toks:
        conds.append("(lower(i.title) LIKE ? OR lower(i.meta) LIKE ?"
                     " OR lower(i.item_url) LIKE ?)")
        params += [f"%{t}%"] * 3
    catf, catp = "", []
    pill_cats = PILL_SQL_CATS.get(_norm_pill(cat))
    if pill_cats:
        catf = " AND i.category IN (" + ",".join("?" * len(pill_cats)) + ")"
        catp = list(pill_cats)
    return [r["k"] for r in con.execute(
        f"SELECT DISTINCT i.item_key AS k FROM items i"
        f" WHERE i.category<>'about' AND i.item_key<>''"
        f" AND {' AND '.join(conds)}{catf} LIMIT 500", params + catp)]


BROWSE_PER_PAGE = PER_PAGE
# How many per page, and how large the cards are. Fixed sets rather than free
# numbers: any value is a valid query string, and an unbounded one is a way to
# ask the server for 200,000 rows.
SIZE_CHOICES = ("s", "m", "l")

# browse-mode SQL per chip: plain, index-friendly filters (search mode keeps
# the richer per-item functions; browse trades the edge cases for pagination)
# mirrors _videolike: a news/trainings card that is actually a video - a media
# URL or a duration in its meta. Named once because three places need to agree
# on it; two of them need its negation, to say "the news cards that are NOT
# already counted as videos" without double-counting.
_VIDEOLIKE_SQL = ("(lower(item_url) LIKE '%.mp4%'"
                  " OR lower(item_url) LIKE '%/videos/%'"
                  " OR meta GLOB '*[0-9]:[0-9][0-9]*'"
                  # the site's own label, as a whole field of the meta line -
                  # a bare LIKE '%video%' would swallow "Video Doorbell"
                  " OR ('|' || replace(meta,' ','') || '|') LIKE '%|Video|%')")

_BROWSE_SQL = {
    "docs":     "category='documents'",
    "videos":   "(category='videos' OR (category IN ('news','trainings')"
                " AND " + _VIDEOLIKE_SQL + "))",
    "images":   "category='images'",
    "design":   "category='design_resources'",
    "boards":   "category='design_resources' AND lower(IFNULL(grp,''))"
                " LIKE '%board%'",
    "products": "category='products'",
}

# The library tiles are six COUNT(DISTINCT)s over 199k items. They cost about a
# second a load, and they only move when a run writes - so they are cached
# against the database's own write clock. Any commit lands in the WAL and bumps
# its mtime, so a stale cache is impossible without a write, and a checkpoint
# costs at most one recount.
_AGG_CACHE = {}
_AGG_LOCK = threading.Lock()


def _db_write_stamp():
    """Newest mtime across the db and its WAL - changes on any commit."""
    st = 0
    for p in (db.DB_PATH, str(db.DB_PATH) + "-wal"):
        try:
            st = max(st, os.stat(p).st_mtime_ns)
        except OSError:
            pass
    return st


def _cached_by_db(name, build):
    """Hold an aggregate until the database is next written to.

    Corpus-wide counts cost hundreds of ms and cannot change without a commit,
    so the write clock is the cache key: no staleness is possible without a
    write, and the worst a WAL checkpoint costs is one extra recount.
    """
    key = _db_write_stamp()
    with _AGG_LOCK:
        hit = _AGG_CACHE.get(name)
        if hit and hit[0] == key:
            return hit[1]
    val = build()
    with _AGG_LOCK:
        _AGG_CACHE[name] = (key, val)
    return val


# Rows in `items` that are page TEXT stored for the diff - about, overview,
# features... - not assets anyone can browse. Every count shown to a person
# excludes them with this one expression, so Help and Content can never
# disagree by fourteen thousand again.
_NON_ASSET_SQL = ("('about','overview','subheadline','features','benefits',"
                  "'applications')")


def _library_counts(con, cat=""):
    """Per-tile counts for the content library, cached until the db changes.

    Only TILED categories are counted. Two of these used to be computed on
    every load and rendered nowhere - a COUNT(*) over the FTS index (504 ms)
    left behind when the page-text tile went away, and the boards count (107 ms)
    left behind when boards folded into design resources. Together they were
    over half the page's cost. Deriving the list from SEARCH_PILLS means a tile
    that goes away stops being counted.
    """
    def build():
        # 'about' is tracked for text comparison only - never surfaced.
        # 'overview' is a page's own text, not an asset type.
        out = {"all": con.execute(
            "SELECT COUNT(*) FROM items WHERE category NOT IN "
            + _NON_ASSET_SQL).fetchone()[0]}
        # the pages tile says Live Pages, so it counts LIVE pages: a 404 or a
        # page that never answered has no library to browse
        out["pages"] = con.execute(
            "SELECT COUNT(*) FROM urls u WHERE u.active=1 AND "
            + ATTR_FILTERS["live"][1]).fetchone()[0]
        for k, _lbl, _fn in SEARCH_PILLS:
            if k in _BROWSE_SQL:
                out[k] = _count_browse(con, k)
        return out

    hit = dict(_cached_by_db("library", build))
    # an untiled category being browsed still needs its own total
    if cat in _BROWSE_SQL and cat not in hit:
        hit[cat] = _count_browse(con, cat)
    return hit


def _count_browse(con, key):
    return con.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM items WHERE " + _BROWSE_SQL[key] +
        " GROUP BY lower(title), item_url)").fetchone()[0]


# Ordering for the browse lists. A-Z is the default because it is the only one
# you can hold in your head; the rest exist because "show me the other end" is
# a real question a 3,500-page list cannot otherwise answer.
_TITLE_EXPR = "lower(COALESCE(NULLIF(u.title_override,''), u.label, u.url))"
_PAGE_SORT_SQL = {
    "az":      _TITLE_EXPR + " ASC",
    "za":      _TITLE_EXPR + " DESC",
    "newest":  "u.last_checked DESC, " + _TITLE_EXPR,
    "oldest":  "u.last_checked ASC, " + _TITLE_EXPR,
}
_ITEM_SORT_SQL = {
    "az":     "lower(title), item_url",
    "za":     "lower(title) DESC, item_url DESC",
    "newest": "npages DESC, lower(title)",     # most widely used first
    "oldest": "npages ASC, lower(title)",
}
SORT_LABELS = [("az", "A → Z"), ("za", "Z → A"),
               ("newest", "Newest checked"), ("oldest", "Oldest checked")]
ITEM_SORT_LABELS = [("az", "A → Z"), ("za", "Z → A"),
                    ("newest", "On most pages"), ("oldest", "On fewest pages")]

_IMG_URL_RE = re.compile(r"\.(png|jpe?g|gif|webp|svg)(\?|$)|/is/image/", re.I)


def is_gated(meta):
    """Does this document sit behind a MyInfineon login?

    It matters for the preview: our server fetches the file to render its first
    page, and for a gated one it gets the login wall instead. So these show a
    locked icon rather than a preview that was never going to arrive - the
    absence of a picture is explained instead of just being an absence.
    """
    return "myinfineon" in (meta or "").lower()


def item_thumb(cat, item_url, img, item_key="", meta=""):
    """The picture to show beside an item, or "" for none.

    Nothing new is stored: every one of these is an address the capture already
    recorded, so the browser fetches it from Infineon's CDN exactly as the real
    page does. Documents are the exception - their first page is rendered by
    this app and cached on disk, which already existed for the item pages.
    """
    if cat == "images":
        return item_url if _IMG_URL_RE.search(item_url or "") else ""
    if img:                       # partners, and design-resource cards
        return img
    # A design resource is either a THING with a picture (a board, a tool) or a
    # FILE with none - an application note, a schematic, a hex dump. The ones
    # with a picture were served it above; the rest fell through to a grey box
    # even when they were PDFs we could render, and doc_preview_png has always
    # accepted this category. Same treatment as a document, because that is
    # what they are.
    if cat in ("documents", "design_resources") and item_key \
            and not is_gated(meta):
        return url_for("doc_preview_png", key=item_key)
    return ""


def _generic_images():
    """Canonical URLs of images that are site furniture rather than a page's
    own content, judged by how many pages carry them.

    Partner logos and tool icons ride along on nearly every page - one sits on
    2,357 of 3,506 - and they crowd out the handful of pictures that actually
    belong to the page being looked at. The corpus tells us which those are
    without anyone maintaining a list: a picture on a twentieth of the site is
    the site's, not this page's. The measured gap is wide, 24 images above 150
    pages and the next band down at 20-59, so the exact cut is not delicate.

    They stay captured, stored and assigned - this only decides what the page
    SHOWS. A change to one is still detected and still flagged.
    """
    def build():
        # its own connection: the caller's is long closed by render time, and
        # a cache hit needs no database at all
        c = db.connect()
        try:
            live = c.execute(
                "SELECT COUNT(*) FROM urls WHERE active=1"
                " AND last_http IS NOT NULL"
                " AND last_http NOT IN (404,410)").fetchone()[0]
            cut = max(25, int(live * 0.04))
            return {r[0] for r in c.execute(
                "SELECT item_url FROM items WHERE category='images'"
                " AND item_url<>'' GROUP BY item_url"
                " HAVING COUNT(DISTINCT url_id) >= ?", (cut,))}
        finally:
            c.close()

    return _cached_by_db("generic_images", build)


def _dash_counts(con):
    """The dashboard's four corpus-wide passes, cached until the next write.

    Returns copies: the caller derives has*/no* entries onto attr_counts, and
    handing out the cached dict itself would let that accumulate across loads.
    """
    def build():
        return {
            # how many captured pages carry each category - one GROUP BY
            "cat": {r["category"]: r["n"] for r in con.execute(
                "SELECT category, COUNT(DISTINCT url_id) n FROM items"
                " GROUP BY category")},
            # pages with a video anywhere (standalone OR a News & highlights
            # card), counted distinctly so a page with both is not doubled
            "vid": con.execute(
                "SELECT COUNT(DISTINCT url_id) n FROM items"
                " WHERE category IN ('videos','news')").fetchone()["n"],
            "status": _bulk_counts(con, STATUS_FILTERS),
            "attr": _bulk_counts(con, ATTR_FILTERS),
        }

    d = _cached_by_db("dash", build)
    return dict(d["cat"]), d["vid"], dict(d["status"]), dict(d["attr"])


def _dash_facets(con, q, statuses, ptypes, attrs, bls=(), pls=()):
    """What each filter option would still be worth, given the others.

    Faceted, not flat: a dimension is narrowed by the OTHER dimensions and
    never by itself. Narrowing Type by the types you already picked would
    show 0 beside every type you had not picked, and you could never add a
    second one - the count has to answer "how many more would this let in",
    not "how many did I already choose".
    """
    def where(skip):
        # bl and pl narrow every other dimension, and like the others they do
        # not narrow themselves - "how many more would this let in".
        return _url_filter("" if skip == "q" else q,
                           [] if skip == "status" else statuses,
                           [] if skip == "type" else ptypes,
                           [] if skip == "attr" else attrs,
                           () if skip == "owner" else bls,
                           () if skip == "owner" else pls)

    out = {"s_" + k: 0 for k in STATUS_FILTERS}
    out.update({"a_" + k: 0 for k in ATTR_FILTERS})
    out.update({"t_" + t: 0 for t in
                ("Products & Eval Boards", "PTP", "EB", *PRODUCT_LEVELS,
                 *APP_LEVELS, *DR_LEVELS, "Product", "Part", "Application",
                 "Design resources", "Promo", "Home")})
    # statuses and the urls-only attributes: one SUM(CASE) pass each
    for skip, filters, prefix in (("status", STATUS_FILTERS, "s_"),
                                  ("attr", ATTR_FILTERS, "a_")):
        w, params = where(skip)
        conds = {k: v[1] for k, v in filters.items() if "items i" not in v[1]}
        cols = ", ".join(f"SUM(CASE WHEN {c} THEN 1 ELSE 0 END) AS k{i}"
                         for i, c in enumerate(conds.values()))
        row = con.execute(f"SELECT {cols} FROM urls u {w}", params).fetchone()
        out.update({prefix + k: (row[f"k{i}"] or 0)
                    for i, k in enumerate(conds)})
    # the category attributes (has-documents, has-videos ...) are an EXISTS
    # over items - twenty correlated subqueries per row takes seconds, so they
    # come from one GROUP BY over the pages that survive the other filters
    w, params = where("attr")
    cat_n = {r["category"]: r["n"] for r in con.execute(
        "SELECT i.category, COUNT(DISTINCT i.url_id) n FROM items i"
        " JOIN urls u ON u.id=i.url_id " + w + " GROUP BY i.category", params)}
    vid_n = con.execute(
        "SELECT COUNT(DISTINCT i.url_id) n FROM items i JOIN urls u"
        " ON u.id=i.url_id " + w + (" AND " if w else " WHERE ")
        + "i.category IN ('videos','news')", params).fetchone()["n"]
    checked = out.get("a_checked", 0)
    # derived exactly as index() derives the static ones, so the live count
    # and the page you land on after Apply cannot disagree
    for _stem, _cat, _ in _CONTENT_ATTRS + _COMPONENT_ATTRS:
        have = vid_n if _stem == "videos" else cat_n.get(_cat, 0)
        out["a_has" + _stem] = have
        out["a_no" + _stem] = max(0, checked - have)
    # types, and the levels nested under them
    w, params = where("type")
    for r in con.execute("SELECT ptype, IFNULL(level,'') lv, url, COUNT(*) n"
                         " FROM urls u " + w + " GROUP BY ptype, lv, "
                         " CASE WHEN url LIKE '%/part/%' THEN 1"
                         "      WHEN url LIKE '%/evaluation-board/%' THEN 2"
                         "      ELSE 0 END", params):
        pt, lv, url = r["ptype"] or "", r["lv"], r["url"] or ""
        if pt == "Part":
            key = "PTP" if "/part/" in url else (
                "EB" if "/evaluation-board/" in url else pt)
            out["t_" + key] = out.get("t_" + key, 0) + r["n"]
        else:
            out["t_" + pt] = out.get("t_" + pt, 0) + r["n"]
            if lv:
                out["t_" + lv] = out.get("t_" + lv, 0) + r["n"]
    out["t_Products & Eval Boards"] = (out.get("t_Product", 0)
                                       + out.get("t_PTP", 0)
                                       + out.get("t_EB", 0))
    w, params = where(None)
    out["_total"] = con.execute("SELECT COUNT(*) FROM urls u " + w,
                                params).fetchone()[0]
    return out


@app.route("/urls/range")
def urls_range():
    """The page ids between two positions of the CURRENT ordering.

    Shift-click across a page boundary needs the rows in between, and most of
    them are not rendered. Same filter and same sort as the table, so the range
    is the one you can see yourself counting.
    """
    try:
        a = max(1, int(request.args.get("from", "1")))
        b = max(1, int(request.args.get("to", "1")))
    except ValueError:
        return jsonify({"ids": []})
    lo, hi = min(a, b), max(a, b)
    con = db.connect()
    try:
        where, params = _url_filter(
            request.args.get("q", "").strip(),
            [x for x in request.args.getlist("status") if x in STATUS_FILTERS],
            request.args.getlist("ptype"),
            [x for x in request.args.getlist("attr") if x in ATTR_FILTERS])
        order = _dash_order(request.args.get("sort", "page"))
        rows = con.execute(
            "SELECT u.id FROM urls u " + where + " ORDER BY " + order
            + " LIMIT ? OFFSET ?", params + [hi - lo + 1, lo - 1]).fetchall()
    finally:
        con.close()
    return jsonify({"ids": [r["id"] for r in rows]})


@app.route("/dash/avail")
def dash_avail():
    """Live facet counts for the dashboard's filter dropdowns."""
    con = db.connect()
    try:
        data = _dash_facets(
            con, request.args.get("q", "").strip(),
            [s for s in request.args.getlist("status") if s in STATUS_FILTERS],
            request.args.getlist("ptype"),
            [a for a in request.args.getlist("attr") if a in ATTR_FILTERS],
            request.args.getlist("bl"), request.args.getlist("pl"))
    finally:
        con.close()
    return jsonify(data)


@app.route("/search")
def search_items():
    """The CONTENT library: every captured asset - documents, videos, images,
    boards, design resources, related products - plus the live pages and the
    full page text. Empty search = browse it all; the chips filter; the search
    box works before or after a chip. Page-section prose (key features,
    benefits, applications) lives under In page text, which names the section
    each match sits in."""
    q = request.args.get("q", "").strip()
    cat = _norm_pill(request.args.get("cat", "all"))
    sort = request.args.get("sort", "az")
    if sort not in _PAGE_SORT_SQL:
        sort = "az"
    try:
        per_page = int(request.args.get("per", BROWSE_PER_PAGE))
    except Exception:
        per_page = BROWSE_PER_PAGE
    if per_page not in PER_PAGE_CHOICES:
        per_page = BROWSE_PER_PAGE
    size = request.args.get("size", "m")
    if size not in SIZE_CHOICES:
        size = "m"
    try:
        pageno = max(1, int(request.args.get("page", 1)))
    except Exception:
        pageno = 1
    groups, total, page_hits = [], 0, []
    browse, browse_total = False, 0
    con = db.connect()
    ccounts = _library_counts(con, cat)
    indexed = ccounts["all"]

    # ---- BROWSE mode: no query -> the library itself, paginated ----------
    if not q and cat == "pages":
        browse = True
        browse_total = ccounts["pages"]
        prows = con.execute(
            "SELECT u.id, u.url, u.level, u.label, u.ptype, u.last_checked,"
            " COALESCE(NULLIF(u.title_override,''),"
            "  (SELECT s.page_title FROM snapshots s WHERE s.url_id=u.id"
            "    AND s.status='ok' AND s.page_title<>''"
            "    ORDER BY s.id DESC LIMIT 1)) AS page_title,"
            "  (SELECT s.id FROM snapshots s WHERE s.url_id=u.id"
            "    AND s.status='ok' ORDER BY s.id DESC LIMIT 1) AS snap_id"
            " FROM urls u WHERE u.active=1 AND " + ATTR_FILTERS["live"][1] +
            " ORDER BY " + _PAGE_SORT_SQL[sort] +
            " LIMIT ? OFFSET ?",
            (per_page, (pageno - 1) * per_page)).fetchall()
        page_hits = [{"id": r["id"], "url": r["url"],
                      "level": r["level"] or "", "ptype": r["ptype"] or "",
                      "snap_id": r["snap_id"],
                      "title": (r["page_title"] or r["label"]
                                or r["url"].rstrip("/").rsplit("/", 1)[-1])}
                     for r in prows]
        total = browse_total
    elif not q and cat in _BROWSE_SQL:
        browse = True
        browse_total = ccounts[cat]
        _sset = db.get_settings()
        rows = con.execute(
            # a bare category under GROUP BY is whichever row SQLite reached
            # first; the videos library needs every section the asset was found
            # in, since eighteen sit in two
            "SELECT title, item_url, category,"
            " group_concat(DISTINCT category) AS cats, grp, meta,"
            " MIN(item_key) AS item_key, MIN(doc_type) AS doc_type,"
            " MAX(IFNULL(img,'')) AS img,"
            " COUNT(DISTINCT url_id) AS npages"
            " FROM items WHERE " + _BROWSE_SQL[cat] +
            " GROUP BY lower(title), item_url"
            # ordering on the FULL grouping key, not just its first column, is
            # what lets this walk idx_items_cat_ltitle and stop at the page
            # size: sorting every group first cost 922 ms, this costs 4 ms.
            # It also makes paging deterministic when titles repeat.
            " ORDER BY " + _ITEM_SORT_SQL[sort] + " LIMIT ? OFFSET ?",
            (per_page, (pageno - 1) * per_page)).fetchall()
        for r in rows:
            _bk = backend_system(r["category"], r["meta"], r["doc_type"],
                                 r["grp"], r["item_url"])
            _ttl = (pretty_image_title(r["title"], r["item_url"])
                    if r["category"] == "images" else r["title"])
            groups.append({"title": _ttl, "url": r["item_url"],
                           "category": r["category"],
                           "cats": (r["cats"] or "").split(","),
                           "grp": r["grp"],
                           "meta": r["meta"], "item_key": r["item_key"],
                           "npages": r["npages"], "pages": [],
                           "backend": _bk,
                           "thumb": item_thumb(r["category"], r["item_url"],
                                               r["img"], r["item_key"],
                                               r["meta"]),
                           "gated": is_gated(r["meta"]),
                           "is_video": is_video_item(r["category"],
                                                     r["item_url"], r["meta"]),
                           "aname": asset_name(r["category"], r["item_url"]),
                           "bsearch": backend_search_link(
                               _bk, asset_name(r["category"], r["item_url"]),
                               _sset)})
        total = browse_total
    if q and cat == "pages":
        # the tracked pages themselves: typo-tolerant over title AND url
        prows = con.execute(
            "SELECT id, url, canonical_url, label, ptype, level,"
            " COALESCE(NULLIF(title_override,''),"
            "  (SELECT s.page_title FROM snapshots s WHERE s.url_id=urls.id"
            "    AND s.status='ok' AND s.page_title<>''"
            "    ORDER BY s.id DESC LIMIT 1)) AS page_title,"
            "  (SELECT s.id FROM snapshots s WHERE s.url_id=urls.id"
            "    AND s.status='ok' ORDER BY s.id DESC LIMIT 1) AS snap_id"
            " FROM urls").fetchall()
        pdata = [{"id": r["id"], "url": r["url"], "level": r["level"] or "",
                  "snap_id": r["snap_id"],
                  "title": (r["page_title"] or r["label"]
                            or r["url"].rstrip("/").rsplit("/", 1)[-1]),
                  "sl": _searchable((r["page_title"] or r["label"] or "")
                                    + " " + r["url"] + " "
                                    + (r["canonical_url"] or ""))}
                 for r in prows]
        ql = _searchable(q)
        hits = {d["id"]: (2, 100, d) for d in pdata if ql in d["sl"]}
        for _t, score, i in process.extract(
                ql, [d["sl"] for d in pdata], scorer=fuzz.WRatio, limit=80,
                score_cutoff=72, processor=str.lower):
            hits.setdefault(pdata[i]["id"], (1, score, pdata[i]))
        page_hits = [h[2] for h in sorted(hits.values(),
                                          key=lambda h: (-h[0], -h[1]))][:60]
        total = len(page_hits)
    text_hits = []
    if q and cat in ("text", "all"):
        # look INSIDE the pages: FTS over each page's full captured text, and
        # then say WHICH COMPONENT the words live in by checking the page's
        # own indexed items for the same tokens. This runs for Everything too -
        # searching the whole library should say where in a page the words are,
        # not just which assets are named that.
        from markupsafe import escape as _esc
        # prefix matching: 'eval' also finds evaluation / EVAL_24V - generous
        # like a substring title search; the POSITIONAL attribution then names
        # whichever section each matching line actually sits in
        fq = " ".join('"' + t.replace('"', "") + '"*'
                      for t in q.split() if t.strip())
        try:
            frows = con.execute(
                "SELECT rowid AS url_id,"
                " snippet(page_fts, 0, '[[', ']]', ' … ', 14) AS snip"
                " FROM page_fts WHERE page_fts MATCH ?"
                " ORDER BY rank LIMIT 60",
                (fq,)).fetchall()
        except Exception:
            frows = []
        toks = [t.lower() for t in q.split() if len(t) >= 2][:4]
        # section headings we can anchor a match to: the standard sections plus
        # whatever THIS page's own nav and Product-details tabs are named
        _BASE_HEADS = {"parametrics", "ordering", "description", "features",
                       "key features", "benefits", "documents",
                       "design resources", "technical documents", "diagrams",
                       "diagram", "partner solutions", "developer community",
                       "news & highlights", "related products", "overview",
                       "about", "trainings & webinars", "videos",
                       "view buying options", "featured products",
                       "register product", "boards & designs",
                       "products table (all rows)"}
        for fr in frows:
            u2 = con.execute(
                "SELECT id, url, level, label, components, detail_tabs,"
                " COALESCE(NULLIF(title_override,''),"
                "  (SELECT s.page_title FROM snapshots s WHERE s.url_id=urls.id"
                "    AND s.status='ok' AND s.page_title<>''"
                "    ORDER BY s.id DESC LIMIT 1)) AS page_title"
                " FROM urls WHERE id=?", (fr["url_id"],)).fetchone()
            if not u2:
                continue
            # WHERE in the page do the matched words sit? Walk the page's own
            # text: each match line, then upward to the nearest section
            # heading. Token match, not substring - 'eval' must never claim a
            # Benefits bullet that merely says 'evaluation'.
            comps = []
            trow = con.execute("SELECT txt FROM page_fts WHERE rowid=?",
                               (u2["id"],)).fetchone()
            if trow and toks:
                heads = set(_BASE_HEADS)
                for col in ("components", "detail_tabs"):
                    try:
                        heads |= {str(x).strip().lower()
                                  for x in json.loads(u2[col] or "[]")}
                    except Exception:
                        pass
                lines = trow["txt"].splitlines()
                lows = [l.strip().lower() for l in lines]
                pats = [re.compile(r"(?<![a-z0-9])" + re.escape(t))
                        for t in toks]   # prefix, mirroring the FTS query
                _weak = {"overview", "about"}   # also sub-tab names inside panes
                for i, ll in enumerate(lows):
                    if len(comps) >= 3:
                        break
                    # the regexes all require their token to appear literally,
                    # so a plain substring test is a free necessary condition -
                    # it skips the regex on the ~99% of lines that cannot match
                    if not any(t in ll for t in toks):
                        continue
                    if not any(pt.search(ll) for pt in pats):
                        continue
                    for j in range(i, -1, -1):
                        cand = lows[j]
                        if cand in heads and len(cand) < 40:
                            nice = lines[j].strip()
                            if cand in _weak:
                                # an Overview/About right under a real section
                                # heading is that section's first TAB, not the
                                # page's Overview - prefer the section
                                for k in range(j - 1, max(-1, j - 9), -1):
                                    if lows[k] in heads and lows[k] not in _weak:
                                        nice = lines[k].strip()
                                        break
                            if nice not in comps:
                                comps.append(nice)
                            break
            snip = str(_esc(fr["snip"])).replace("[[", "<mark>").replace(
                "]]", "</mark>")
            text_hits.append({"id": u2["id"], "url": u2["url"],
                              "level": u2["level"] or "",
                              "title": (u2["page_title"] or u2["label"]
                                        or u2["url"].rstrip("/").rsplit("/", 1)[-1]),
                              "snip": snip,
                              "comps": comps or ["page body text"]})
        # ---- the prose sections as a PRECISION layer -------------------
        # Key features / Benefits / Applications were captured as their own
        # items: exact bullets that already know their section. Match them
        # too - they confirm the positional guess with certainty, quote the
        # exact bullet, and catch matches FTS tokenization misses.
        toks_all = [t for t in q.split() if t.strip()][:6]
        prose_rows = []
        if toks_all:
            conds = " AND ".join(
                ["instr(lower(i.title), lower(?)) > 0"] * len(toks_all))
            prose_rows = con.execute(
                "SELECT i.url_id, i.category, i.title FROM items i"
                " WHERE i.category IN ('features','benefits','applications',"
                "                      'overview','about','subheadline')"
                " AND " + conds + " LIMIT 300", toks_all).fetchall()
        _PLBL = {"features": "Key features", "benefits": "Benefits",
                 "applications": "Applications", "overview": "Overview",
                 "about": "About", "subheadline": "Subheadline"}

        def _clip(s):
            # overview/about are paragraphs, not bullets - quote the window
            # around the first matched word, not the whole wall of text
            if len(s) <= 200:
                return s
            low = s.lower()
            hits = [low.find(t.lower()) for t in toks_all]
            pos = min((p for p in hits if p >= 0), default=0)
            start, end = max(0, pos - 70), min(len(s), pos + 150)
            return (("… " if start else "") + s[start:end]
                    + (" …" if end < len(s) else ""))

        def _markup(s):
            out = str(_esc(_clip(s)))
            for t in sorted({t for t in toks_all}, key=len, reverse=True):
                out = re.sub("(?i)" + re.escape(str(_esc(t))),
                             lambda m: "<mark>" + m.group(0) + "</mark>", out)
            return out
        by_page = {}
        for pr in prose_rows:
            by_page.setdefault(pr["url_id"], []).append(
                (_PLBL[pr["category"]], pr["title"]))
        for h in text_hits:
            bl = by_page.get(h["id"])
            if not bl:
                continue
            h["bullets"] = [(lbl, _markup(t)) for lbl, t in bl[:3]]
            for lbl, _t in bl:
                if lbl not in h["comps"]:
                    h["comps"].append(lbl)
            if "page body text" in h["comps"] and len(h["comps"]) > 1:
                h["comps"].remove("page body text")
            h["comps"] = h["comps"][:4]
        seen_ids = {h["id"] for h in text_hits}
        for uid in [u for u in by_page if u not in seen_ids][:30]:
            u2 = con.execute(
                "SELECT id, url, level, label,"
                " COALESCE(NULLIF(title_override,''),"
                "  (SELECT s.page_title FROM snapshots s WHERE s.url_id=urls.id"
                "    AND s.status='ok' AND s.page_title<>''"
                "    ORDER BY s.id DESC LIMIT 1)) AS page_title"
                " FROM urls WHERE id=?", (uid,)).fetchone()
            if not u2:
                continue
            bl = by_page[uid]
            text_hits.append({"id": uid, "url": u2["url"],
                              "level": u2["level"] or "",
                              "title": (u2["page_title"] or u2["label"]
                                        or u2["url"].rstrip("/").rsplit("/", 1)[-1]),
                              "snip": _markup(bl[0][1]),
                              "comps": list(dict.fromkeys(
                                  lbl for lbl, _ in bl))[:3],
                              "bullets": [(lbl, _markup(t))
                                          for lbl, t in bl[:3]]})
        # in Everything the item search below owns the headline count; here
        # the page-text hits are a section of their own
        if cat == "text":
            total = len(text_hits)
    if q and cat not in ("pages", "text"):
        keys = _search_keys(con, q, cat, 250)
        if keys:
            marks = ",".join("?" * len(keys))
            rows = con.execute(f"""
                SELECT i.category, i.grp, i.title, i.item_url, i.meta, i.url_id,
                       i.item_key, i.doc_type, i.img, u.url AS page_url, u.label,
                       COALESCE(NULLIF(u.title_override,''),
                         (SELECT s.page_title FROM snapshots s WHERE s.url_id=u.id
                           AND s.status='ok' AND s.page_title<>''
                           ORDER BY s.id DESC LIMIT 1)) AS page_title
                FROM items i JOIN urls u ON u.id=i.url_id
                WHERE i.item_key IN ({marks})""", keys).fetchall()
        else:
            rows = []
        rank = {k: i for i, k in enumerate(keys)}
        by_item, order = {}, []
        _sset = db.get_settings()       # backend search URLs, read once
        _pfn = PILL_FN.get(cat)
        for r in rows:
            if _pfn and not _pfn({"cat": r["category"], "url": r["item_url"],
                                  "meta": r["meta"], "grp": r["grp"]}):
                continue
            # prose sections are In-page-text territory, not content assets
            if cat == "all" and r["category"] in PROSE_CATS:
                continue
            gk = (r["title"].lower(), r["item_url"])
            if gk not in by_item:
                _bk = backend_system(r["category"], r["meta"], r["doc_type"],
                                     r["grp"], r["item_url"])
                _ttl = (pretty_image_title(r["title"], r["item_url"])
                        if r["category"] == "images" else r["title"])
                by_item[gk] = {"title": _ttl, "url": r["item_url"],
                               "category": r["category"], "cats": set(),
                               "grp": r["grp"],
                               "meta": r["meta"], "pages": [],
                               "item_key": r["item_key"],
                               "rank": rank.get(r["item_key"], 10 ** 9),
                               "backend": _bk,
                               "is_video": is_video_item(r["category"],
                                                         r["item_url"],
                                                         r["meta"]),
                               "aname": asset_name(r["category"],
                                                   r["item_url"]),
                               "thumb": item_thumb(r["category"], r["item_url"],
                                                   r["img"], r["item_key"],
                                                   r["meta"]),
                               "gated": is_gated(r["meta"]),
                               "bsearch": backend_search_link(
                                   _bk, asset_name(r["category"],
                                                   r["item_url"]), _sset)}
                order.append(gk)
            else:
                by_item[gk]["rank"] = min(by_item[gk]["rank"],
                                          rank.get(r["item_key"], 10 ** 9))
            by_item[gk]["cats"].add(r["category"])
            by_item[gk]["pages"].append(
                {"id": r["url_id"], "url": r["page_url"],
                 "title": r["page_title"] or r["label"] or r["page_url"]})
        groups = [by_item[k] for k in order]
        groups.sort(key=lambda g: (g["rank"], -len(g["pages"])))
        total = sum(len(g["pages"]) for g in groups)
    con.close()
    has_more = browse and pageno * per_page < browse_total
    return render_template("search.html", q=q, cat=cat,
                           run_state=dict(runner.RUN_STATE),
                           pills=[(k, l) for k, l, _f in SEARCH_PILLS],
                           pill_labels=PILL_LABELS, page_hits=page_hits,
                           text_hits=text_hits,
                           text_fams=by_family(text_hits),
                           page_fams=by_family(page_hits),
                           groups=groups, kind_groups=by_kind(groups),
                           total=total, indexed=indexed,
                           labels=dict(SECTION_LABELS),
                           ccounts=ccounts, browse=browse, pageno=pageno,
                           has_more=has_more, per_page=per_page, size=size,
                           per_choices=PER_PAGE_CHOICES,
                           sort=sort,
                           sort_opts=(SORT_LABELS if cat == "pages"
                                      else ITEM_SORT_LABELS))


@app.route("/api/suggest")
def api_suggest():
    """Type-ahead for content search: typo-tolerant, best match first."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    cat = _norm_pill(request.args.get("cat", "all"))
    # overview is a page's own text, not an asset to jump to; prose sections
    # (features/benefits/applications) live under In page text
    raw = _fuzzy_items(q, cat, 30,
                       exclude={"overview", "subheadline", "features", "benefits",
                                "applications"})
    con = db.connect()
    pages = _pages_for_keys(con, [d["key"] for d in raw])
    con.close()
    labels = dict(SECTION_LABELS)
    # Collapse the SAME title in the SAME language (distinct asset URLs of one
    # document) into a single suggestion so the dropdown does not read as
    # duplicates; the results page still lists each distinct file. Different
    # languages stay separate, so EN/JA/CN variants remain visible.
    by_ident, order = {}, []
    for d in raw:
        lang = _meta_lang(d["meta"])
        ident = (re.sub(r"\s+", " ", d["title"]).strip().lower(), lang, d["cat"])
        if ident in by_ident:
            by_ident[ident]["pages"] += pages.get(d["key"], 1)
            continue
        by_ident[ident] = {"title": d["title"], "cat": d["cat"],
                           "category": labels.get(d["cat"], d["cat"]),
                           "lang": lang, "pages": pages.get(d["key"], 1)}
        order.append(ident)
    return jsonify([by_ident[i] for i in order[:10]])


def _meta_lang(meta):
    """The 2-letter language tag a document's meta carries (EN/JA/CN…), or ''.
    Meta reads 'Application note | PDF | Public | JA | 09_00 | …', so the tag is
    the lone two-letter all-caps pipe token."""
    for tok in (meta or "").split("|"):
        t = tok.strip()
        if len(t) == 2 and t.isalpha() and t.isupper():
            return t
    return ""


def _soffice():
    """Path to LibreOffice's headless converter, or None. It is what turns an
    Office file into a PDF we can render - the only reliable local way to make
    a picture of ppt/doc/xls (old binary formats included)."""
    import shutil
    cands = [shutil.which("soffice"), shutil.which("soffice.exe"),
             r"C:\Program Files\LibreOffice\program\soffice.exe",
             r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def _office_to_pdf(data, ext):
    """Office bytes -> PDF bytes via LibreOffice headless, or None."""
    so = _soffice()
    if not so:
        return None
    import tempfile
    import subprocess
    d = tempfile.mkdtemp()
    try:
        src = os.path.join(d, "in." + ext)
        with open(src, "wb") as f:
            f.write(data)
        subprocess.run([so, "--headless", "--convert-to", "pdf",
                        "--outdir", d, src], timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pdf = os.path.join(d, "in.pdf")
        if os.path.exists(pdf):
            with open(pdf, "rb") as f:
                return f.read()
        return None
    except Exception:
        return None
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def _fetch_bytes(url, cap=40_000_000):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.read(cap)


# Document previews are WEBP, not PNG. PNG is lossless, which for a rendered
# page means storing every anti-aliasing artefact exactly - the cache reached
# 1.1 GB on a 4.6 GB volume and helped starve a run to death. Measured over the
# twelve largest real previews on 6 Sep 2026, webp/82 is 90% smaller (12.5 MB
# -> 1.3 MB) and side-by-side indistinguishable, including on photographic
# regions; jpeg at the same quality is only 84% smaller and worse on text. So
# the saving is a FORMAT change, not a quality cut, and the previews stay
# sharp. method=6 spends the most encoder effort - paid once, at render time.
PREVIEW_FORMAT = "WEBP"
PREVIEW_QUALITY = 82


def _pdf_first_png(pdf_bytes, out_path):
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(pdf_bytes)
    pil = pdf[0].render(scale=1.6).to_pil()
    pil.thumbnail((1100, 1400))
    pil.convert("RGB").save(out_path, PREVIEW_FORMAT,
                            quality=PREVIEW_QUALITY, method=6)
    pdf.close()


import hashlib


def _doc_tag(meta):
    """A short token that CHANGES when a document is updated - built from the
    version, date and size in its meta - so a re-captured newer file gets a
    fresh preview instead of the stale cached one (the URL usually stays the
    same across versions, so the URL alone cannot tell us it changed)."""
    parts = [p.strip() for p in (meta or "").split("|")]
    ver = next((p for p in parts if re.fullmatch(r"\d{2}_\d{2}", p)), "")
    date = next((p for p in parts if re.search(r"[A-Za-z]{3}", p)
                 and re.search(r"\d{4}", p)), "")
    size = next((p for p in parts if re.search(r"\d\s*[kmg]?b\b", p, re.I)), "")
    sig = (ver + "|" + date + "|" + size) or (meta or "")
    return hashlib.sha1(sig.encode("utf-8", "replace")).hexdigest()[:8]


def _preview_files(cache, key, meta):
    """(png, fail) paths for an item's preview, version-stamped so an updated
    document renders anew and the stale image is left as an orphan to clean."""
    stem = re.sub(r"[^a-f0-9]", "", key) + "_" + _doc_tag(meta)
    # .webp, so every leftover .png from the lossless era is an orphan the
    # existing sweep removes on its own - no migration step to remember
    return cache / (stem + ".webp"), cache / (stem + ".fail")


# background pre-render of EVERY document's front page so any user opens an item
# instantly. Version-aware (see _doc_tag): an updated doc re-renders. Throttled
# to stay under Infineon's bot-guard; resumable; office batched via LibreOffice.
PREWARM = {"running": False, "done": 0, "total": 0}


def _prewarm_docs():
    if PREWARM["running"]:
        return
    PREWARM["running"] = True
    try:
        import tempfile
        import subprocess
        import shutil as _sh
        # never compete with a live capture: a 7k-file download storm shares
        # Infineon's bot-guard with the page captures. Wait until the run is
        # idle, then pre-render. On-demand clicks still render instantly.
        while runner.RUN_STATE.get("running"):
            time.sleep(60)
        so = _soffice()
        cache = db.DATA_DIR / "previews"
        cache.mkdir(exist_ok=True)
        con = db.connect()
        rows = con.execute(
            "SELECT item_key, MIN(item_url) u, MIN(meta) m FROM items"
            " WHERE item_key<>'' AND item_url<>''"
            " AND category IN ('documents','design_resources')"
            " GROUP BY item_key").fetchall()
        con.close()
        # clean orphans: previews whose (key, version) no longer match any item
        # (an updated doc's old image, a removed doc) - keeps the cache honest
        valid = set()
        for r in rows:
            png, fail = _preview_files(cache, r["item_key"], r["m"])
            valid.add(png.name)
            valid.add(fail.name)
        for f in cache.glob("*.*"):
            if f.name not in valid:
                try:
                    f.unlink()
                except Exception:
                    pass
        pdfs, offs = [], []
        for r in rows:
            low = (r["u"] or "").lower().split("?")[0]
            if "/gated/" in low:
                continue
            tail = low.rsplit("/", 1)[-1]
            ext = tail.rsplit(".", 1)[-1] if "." in tail else ""
            png, fail = _preview_files(cache, r["item_key"], r["m"])
            if png.exists() or fail.exists():
                continue
            if ext == "pdf":
                pdfs.append((r["item_key"], r["u"], r["m"]))
            elif ext in _OFFICE_EXT and so:
                offs.append((r["item_key"], r["u"], r["m"]))
        PREWARM.update(total=len(pdfs) + len(offs), done=0)

        # PDFs one at a time (fast render, no soffice) - gentle throttle, and
        # back off hard on a run of failures (a bot-guard 403 storm)
        misses = 0
        for key, url, meta in pdfs:
            png, fail = _preview_files(cache, key, meta)
            if not png.exists():
                try:
                    data = _fetch_bytes(url)
                    if data.startswith(b"%PDF"):
                        _pdf_first_png(data, png)
                        misses = 0
                    else:
                        fail.touch()             # gated / not a real pdf
                except Exception:
                    misses += 1
                    if misses and misses % 8 == 0:
                        time.sleep(90)           # likely rate-limited - cool off
            PREWARM["done"] += 1
            time.sleep(0.4)                      # ~2/sec, easy on the server

        for i in range(0, len(offs), 20):
            chunk = offs[i:i + 20]
            d = tempfile.mkdtemp()
            stem = {}
            try:
                for key, url, meta in chunk:
                    ext = url.lower().split("?")[0].rsplit(".", 1)[-1]
                    try:
                        data = _fetch_bytes(url)
                    except Exception:
                        continue
                    png, fail = _preview_files(cache, key, meta)
                    if not (data[:2] == b"PK" or data[:4] == b"\xd0\xcf\x11\xe0"):
                        fail.touch()
                        continue
                    s = re.sub(r"[^a-f0-9]", "", key)
                    with open(os.path.join(d, s + "." + ext), "wb") as f:
                        f.write(data)
                    stem[s] = (key, meta)
                if stem:
                    srcs = [os.path.join(d, f) for f in os.listdir(d)
                            if not f.endswith(".pdf")]
                    try:
                        subprocess.run([so, "--headless", "--convert-to", "pdf",
                                        "--outdir", d] + srcs, timeout=300,
                                       stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
                    for s, (key, meta) in stem.items():
                        pdf = os.path.join(d, s + ".pdf")
                        png, fail = _preview_files(cache, key, meta)
                        if os.path.exists(pdf):
                            try:
                                with open(pdf, "rb") as f:
                                    _pdf_first_png(f.read(), png)
                            except Exception:
                                fail.touch()
                        else:
                            fail.touch()
            finally:
                _sh.rmtree(d, ignore_errors=True)
            PREWARM["done"] += len(chunk)
            time.sleep(1)
    finally:
        PREWARM["running"] = False


@app.route("/docpreview/<key>.png")
def doc_preview_png(key):
    """FRONT PAGE of a document as an image - enough to verify it is the right
    file. Our OWN server fetches it (Infineon refuses cross-origin embedding),
    renders page 1 and caches the PNG under a version-stamped name, so an
    updated document (new date/version) renders fresh. PDFs render directly;
    Office files convert to PDF via LibreOffice first. A file that comes back as
    an HTML login page (MyInfineon-gated) can never be pictured - negative-cached
    so the item page shows a clean card instead."""
    cache = db.DATA_DIR / "previews"
    cache.mkdir(exist_ok=True)
    con = db.connect()
    row = con.execute(
        "SELECT MIN(item_url) u, MIN(meta) m FROM items WHERE item_key=?"
        " AND category IN ('documents','design_resources')", (key,)).fetchone()
    con.close()
    url = (row["u"] if row else "") or ""
    meta = (row["m"] if row else "") or ""
    out, fail = _preview_files(cache, key, meta)
    if out.exists():
        # the ROUTE still ends .png (it is just a path, and templates and any
        # bookmarked link keep working); the bytes are webp and the header
        # says so, which is what every browser actually reads
        return send_file(out, mimetype="image/webp")
    if fail.exists():
        abort(404)
    low = url.lower().split("?")[0]
    tail = low.rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1] if "." in tail else ""
    if "/gated/" in low or ext not in ({"pdf"} | _OFFICE_EXT):
        abort(404)
    if ext in _OFFICE_EXT and not _soffice():
        abort(404)
    try:
        data = _fetch_bytes(url)
        if ext == "pdf":
            pdfdata = data if data.startswith(b"%PDF") else None
        elif data[:2] == b"PK" or data[:4] == b"\xd0\xcf\x11\xe0":
            pdfdata = _office_to_pdf(data, ext)
        else:
            pdfdata = None                          # HTML login page = gated
        if pdfdata:
            _pdf_first_png(pdfdata, out)
        else:
            fail.touch()
    except ImportError as exc:
        # The renderer is MISSING, which is not this document's fault - do not
        # negative-cache it, or every document gets permanently marked
        # unpreviewable and installing the library later fixes nothing. Say so
        # instead: this exact failure was invisible in production for a day
        # because it landed in a bare `except: pass`.
        print(f"[preview] cannot render - {exc}. Is pypdfium2 installed?",
              flush=True)
    except Exception as exc:
        print(f"[preview] {key}: {type(exc).__name__}: {exc}"[:200], flush=True)
    if out.exists():
        # the freshly-rendered path serves the SAME webp bytes as the cached
        # path above - it kept saying image/png and had to be caught by
        # sniffing a live response instead of by the header
        return send_file(out, mimetype="image/webp")
    abort(404)


@app.route("/item/<key>")
def item_detail(key):
    """One real-world item - a document, video, image, board - and every page
    it is assigned to. The same datasheet can sit on forty product pages; this
    is the view that answers "where does this thing appear?".

    Keyed on the ASSET (its title and link), not on one item_key. A thing filed
    under two headings gets a key per heading, so keying on one of them made
    this page contradict the library that linked to it: 177 assets showed
    fewer pages here than the badge you clicked promised, hiding 1,959
    assignments between them - a video in both a News card and the page's own
    video section, a product also listed under Browse by category.
    """
    con = db.connect()
    ident = con.execute("SELECT title, item_url, category FROM items"
                        " WHERE item_key=? LIMIT 1", (key,)).fetchone()
    if not ident:
        con.close()
        abort(404)
    # Prose is not an asset: a key features bullet and a Browse-by-category
    # card can read the same and point at the same link without being the same
    # thing, so widening to title+url must not sweep the prose in. Matching the
    # library's own exclusion keeps this page and the badge that linked to it
    # counting the same set.
    if ident["category"] in NON_ASSET_CATS:
        where, args = "i.item_key=?", (key,)
    else:
        where = ("lower(i.title)=lower(?) AND i.item_url=? AND i.category NOT IN ("
                 + ",".join("?" * len(NON_ASSET_CATS)) + ")")
        args = (ident["title"], ident["item_url"], *sorted(NON_ASSET_CATS))
    rows = con.execute("""
        SELECT i.*, u.url AS page_url, u.ptype, u.level, u.label, u.last_status,
               u.flags AS page_flags,
               (SELECT s.page_title FROM snapshots s WHERE s.url_id=u.id
                 AND s.status='ok' AND s.page_title<>''
                 ORDER BY s.id DESC LIMIT 1) AS page_title,
               (SELECT s.id FROM snapshots s WHERE s.url_id=u.id
                 AND s.status='ok' ORDER BY s.id DESC LIMIT 1) AS snap_id
        FROM items i JOIN urls u ON u.id=i.url_id
        WHERE """ + where + " ORDER BY u.ptype, page_title", args).fetchall()
    if not rows:
        con.close()
        abort(404)
    first = rows[0]
    # an overview is a page's own text, not a shared asset - there is no
    # "assigned to these pages" answer for it, so send it straight to its page.
    if first["category"] == "overview":
        con.close()
        return redirect(url_for("url_detail", url_id=first["url_id"]))
    # the richest meta wins: the same item can be listed tersely on one page
    meta = max((r["meta"] or "" for r in rows), key=len)
    dtype = next((r["doc_type"] for r in rows if r["doc_type"]), "")
    _bk = backend_system(first["category"], meta, dtype, first["grp"],
                         first["item_url"])
    _aname = asset_name(first["category"], first["item_url"])
    _img = next((r["img"] for r in rows if r["img"]), "")
    _pk, _psrc = ("", "")
    if first["category"] in ("documents", "design_resources"):
        _pk, _psrc = doc_preview(first["item_url"])
    _isvid = is_video_item(first["category"], first["item_url"], meta)
    item = {"title": first["title"], "url": first["item_url"],
            "category": first["category"], "grp": first["grp"], "meta": meta,
            "backend": _bk, "asset_name": _aname, "img": _img,
            "is_video": _isvid,
            "preview_kind": _pk, "preview_src": _psrc,
            "bsearch": backend_search_link(_bk, _aname, db.get_settings()),
            "doc_type": (dtype or document_type_of(meta))
            if first["category"] == "documents" else "",
            "label": dict(SECTION_LABELS).get(first["category"],
                                              first["category"]),
            "source": item_source({r["category"] for r in rows})}
    pages, _seen_pg = [], set()
    for r in rows:
        if r["url_id"] in _seen_pg:      # same asset twice on one page
            continue
        _seen_pg.add(r["url_id"])
        # IS THIS IMAGE THE PAGE'S HERO, or just an image that sits on it?
        # The whole point of the question somebody arrives here with. A search
        # for "dummy" lists every page CARRYING a placeholder anywhere - a
        # related-board tile, a tool icon - and two of those pages were read
        # as "the hero is a dummy" when their hero was a real board photo.
        hero = ""
        if first["category"] == "images":
            try:
                hero = ((json.loads(r["page_flags"] or "{}").get("hero_image")
                         or {}).get("src") or "")
            except Exception:
                hero = ""
        pages.append({"id": r["url_id"], "url": r["page_url"],
                      "title": r["page_title"] or r["label"] or "",
                      "ptype": r["ptype"] or "", "grp": r["grp"] or "",
                      "level": r["level"] or "", "snap_id": r["snap_id"],
                      "status": r["last_status"] or "",
                      "is_hero": bool(hero) and _same_asset(hero,
                                                            first["item_url"])})
    by_type = {}
    for p in pages:
        by_type.setdefault(p["ptype"] or "Other", []).append(p)
    con.close()
    tid, tlabel = item_target(key)
    notes, _n, n_open = _notes_for("item", tid)
    return render_template("item.html", item=item, pages=pages, item_key=key,
                           hero_on=sum(1 for p in pages if p["is_hero"]),
                           by_type=sorted(by_type.items()),
                           notes=notes, notes_open=n_open, notes_kind="item",
                           notes_target=tid, notes_label=tlabel,
                           notes_hint=key)


@app.route("/hierarchy")
def hierarchy():
    """The WHOLE site as one tree, rooted at the homepage: every type's
    hierarchy is a branch group on a single page, built from URL structure
    with breadcrumb text as names. The type pills are quick FILTERS -
    client-side, animated - not separate pages: clicking one keeps that
    branch and smoothly hides the rest."""
    con = db.connect()
    tabs = _hierarchy_tabs(con)
    # every live page we track, by URL, so a grouping node can link to our
    # captured library when we have the page it stands for
    tracked = {_norm_url(r["url"]): r["id"] for r in con.execute(
        "SELECT id, url FROM urls WHERE active=1"
        " AND IFNULL(last_status,'') NOT IN ('notlive','blank','error')")}
    order = list(LEVEL_NAMES)
    groups, all_levels, disagreements = [], set(), 0
    for lbl, mem in tabs:
        marks = ",".join("?" * len(mem))
        # dead pages are not part of any tree - keep them out of the
        # "x of y" numbers too, or the counts promise pages the tree lacks
        total = con.execute(
            f"SELECT COUNT(*) FROM urls WHERE active=1 AND ptype IN ({marks})"
            " AND IFNULL(last_status,'') NOT IN ('notlive','blank','error')",
            mem).fetchone()[0]
        pages = _breadcrumbs(con, mem)
        product_tree = any(m in ("Product", "Part") for m in mem)
        tree = _hierarchy_tree(pages, tracked, product_tree)
        mine = {p.get("live_level") for p in pages if p.get("live_level")}
        all_levels |= mine
        disagreements += sum(1 for p in pages if p.get("level_disagrees"))
        # which badges this group actually uses, so picking a filter can narrow
        # the legend to it - a promo filter has no business explaining PSFP
        groups.append({"label": lbl, "tree": tree, "shown": len(pages),
                       "total": total, "levels": sorted(mine)})
    # the audit trail: every automatic level assignment, newest first, so
    # "did anything move?" is a glance, not a guess. Empty after a run means
    # exactly that - the site's structure did not change.
    level_log = con.execute("""
        SELECT l.ts, l.old_level, l.new_level, l.src, l.why, l.url_id,
               u.url FROM level_log l JOIN urls u ON u.id=l.url_id
        ORDER BY l.id DESC LIMIT 30""").fetchall()
    # the homepage roots the whole tree (promo pages are the one branch that
    # does not descend from it - standalone landing pages)
    home = con.execute(
        "SELECT id, url,"
        " COALESCE(NULLIF(title_override,''), (SELECT s.page_title FROM"
        "  snapshots s WHERE s.url_id=urls.id AND s.status='ok'"
        "  AND s.page_title<>'' ORDER BY s.id DESC LIMIT 1)) AS title"
        " FROM urls WHERE ptype='Home' AND active=1 LIMIT 1").fetchone()
    con.close()
    sel = request.args.get("type") or "All"
    if sel == "Products & parts":          # old deep links keep working
        sel = "Products & Eval Boards"
    seen_levels = sorted(all_levels,
                         key=lambda l: order.index(l) if l in order else 99)
    # level -> the groups that use it, for the client-side legend filter
    lvl_groups = {}
    for g in groups:
        for l in g["levels"]:
            lvl_groups.setdefault(l, []).append(g["label"])
    return render_template("hierarchy.html", groups=groups, sel=sel,
                           lvl_groups=lvl_groups,
                           levels=seen_levels, level_names=LEVEL_NAMES,
                           disagreements=disagreements, level_log=level_log,
                           home=home)


def _export_meta_cols():
    """The ownership columns, in the order the CSS report used."""
    return [("bl", "BL"), ("pl", "PL"), ("cm", "CM"), ("owner", "Owner")]


# an Excel cell holds 32,767 characters; a page with two hundred documents can
# reach that, and a truncated cell is better than a corrupt workbook
_CELL_CAP = 30000


def _all_page_columns():
    """Every column the per-page export can carry, for the one-click
    "everything" download - owners, each meta field, every component's
    content, the page-status fields. One definition so the quick button and
    the tick-panel can never drift on what "everything" means."""
    cols = [k for k, _l in _export_meta_cols()]
    cols += [k for k, _l, _kind, _a in _all_components()]
    cols += [k for k, _l in PAGE_FIELDS]
    return cols


def _export_rows(con, levels, cols, verbose=False, ids=None):
    """One row per page: its identity, then whichever columns were ticked.

    Component presence comes from the same resolver the page view uses, so a
    "yes" here means exactly what a green chip there means - never a second
    definition of the same fact drifting away from the first.

    verbose=True fills each component cell with what is actually ON the page
    rather than yes/no. On screen yes/no is what you want - a wall of document
    titles is unreadable at a glance - but in a spreadsheet the cell should
    carry the thing itself, because that is what you opened the spreadsheet
    for. Only the eleven components whose content we store can do this; the
    other fourteen are nav labels and flags with no text behind them, so they
    stay yes/no rather than pretending to be empty.
    """
    registry = _all_components()          # resolved ONCE, not per page
    comp_keys = [k for k, _l, _kind, _a in registry]
    want_comps = [k for k in comp_keys if k in cols]
    want_meta = [k for k, _l in _export_meta_cols() if k in cols]
    want_page = [k for k, _l in PAGE_FIELDS if k in cols]

    cat_of = {k: a for k, _l, kind, a in registry if kind == "cat"}
    q = ("SELECT u.id, u.url, u.ptype, u.level, u.bl, u.pl, u.cm, u.owner,"
         " u.components, u.detail_tabs, u.flags, u.last_status, u.last_http,"
         " u.last_checked,"
         " COALESCE(NULLIF(u.title_override,''),"
         "  (SELECT s.page_title FROM snapshots s WHERE s.url_id=u.id"
         "    AND s.status='ok' AND s.page_title<>''"
         "    ORDER BY s.id DESC LIMIT 1)) AS title"
         " FROM urls u WHERE u.active=1")
    args = []
    # A NAMED SET BEATS A FILTER. Ticking rows on the dashboard and asking for
    # those is not the same question as "every EB page" - it is the answer to
    # a conversation somebody is already having, and rebuilding it out of
    # level and type filters usually cannot be done at all.
    if ids:
        q += " AND u.id IN (" + ",".join("?" * len(ids)) + ")"
        args += list(ids)
    elif levels:
        q += " AND IFNULL(u.level,'') IN (" + ",".join("?" * len(levels)) + ")"
        args += levels
    q += " ORDER BY u.level, lower(COALESCE(NULLIF(u.title_override,''), u.url))"
    pages = con.execute(q, args).fetchall()

    open_changes = {}
    if "changes" in cols:
        for r in con.execute("SELECT url_id, COUNT(*) n FROM changes"
                             " WHERE reviewed=0 GROUP BY url_id"):
            open_changes[r["url_id"]] = r["n"]
    # which content categories each page actually captured - one query for the
    # whole set rather than one per page
    cats = {}
    texts = {}
    if want_comps and pages:
        ids = [p["id"] for p in pages]
        for i in range(0, len(ids), 800):
            chunk = ids[i:i + 800]
            for r in con.execute(
                    "SELECT url_id, category FROM items WHERE url_id IN ("
                    + ",".join("?" * len(chunk)) + ") GROUP BY url_id, category",
                    chunk):
                cats.setdefault(r["url_id"], set()).add(r["category"])
        if verbose:
            wanted = {cat_of[k] for k in want_comps if k in cat_of}
            if wanted:
                for i in range(0, len(ids), 800):
                    chunk = ids[i:i + 800]
                    for r in con.execute(
                            "SELECT url_id, category, title FROM items"
                            " WHERE url_id IN (" + ",".join("?" * len(chunk))
                            + ") AND category IN ("
                            + ",".join("?" * len(wanted)) + ")"
                            " ORDER BY id", chunk + list(wanted)):
                        texts.setdefault((r["url_id"], r["category"]),
                                         []).append(r["title"] or "")

    out = []
    for p in pages:
        try:
            flags = json.loads(p["flags"] or "{}")
        except Exception:
            flags = {}
        row = {"id": p["id"], "url": p["url"], "title": p["title"] or "",
               "ptype": p["ptype"] or "", "level": p["level"] or "",
               "status": p["last_status"] or "", "vals": []}
        for k in want_meta:
            row["vals"].append(p[k] or "")
        for k in want_page:
            if k == "live":
                # Three answers, not two. A blank page answered - it is not a
                # 404 - and it shows a visitor nothing, so calling it either
                # yes or no loses the only interesting thing about it. A
                # capture error is neither: we did not see the page.
                http, st = p["last_http"], (p["last_status"] or "")
                row["vals"].append(
                    "no" if http in (404, 410)
                    else ("empty" if st == "blank"
                          else ("—" if st == "error" or not http else "yes")))
            elif k == "status":
                row["vals"].append(p["last_status"] or "not captured yet")
            elif k == "last_captured":
                row["vals"].append((p["last_checked"] or "")[:19])
            else:
                row["vals"].append(open_changes.get(p["id"], 0))
        if want_comps:
            states = _page_component_states(
                p["components"], p["detail_tabs"], flags,
                cats.get(p["id"], set()), registry,
                captured_at=p["last_checked"])
            for k in want_comps:
                st = states.get(k, "absent")
                # "empty" is its own answer here too - a column of yes/no
                # files a section that exists with nothing in it under the
                # same word as one that was never built. A hero image that
                # exists without content IS the site default, so it says so.
                word = ("yes" if st == "content"
                        else ("default" if (st == "empty" and k == "hero_image")
                              else ("empty" if st == "empty" else "no")))
                if not verbose:
                    row["vals"].append(word)
                elif k in cat_of:
                    lines = texts.get((p["id"], cat_of[k]), [])
                    row["vals"].append(chr(10).join(lines)[:_CELL_CAP] or word)
                elif k == "hero_image":
                    # TWO cells: the verdict, then the link behind it. A
                    # column of addresses cannot be sorted or counted - you
                    # cannot see how many boards are on a placeholder without
                    # reading 229 URLs - and the address alone does not say,
                    # because two of the three placeholder paths are named
                    # after the product rather than after being a placeholder.
                    h = flags.get("hero_image") or {}
                    src = h.get("src", "") or ""
                    row["vals"].append(
                        "no image" if not h.get("exists")
                        else ("real image" if h.get("has_content")
                              else "placeholder"))
                    row["vals"].append(src or word)
                else:
                    # a nav label: a link, so present or not is all there is
                    row["vals"].append(word)
        out.append(row)
    return out, want_meta, want_comps, want_page


def _xl_link(ws, text, url):
    """A cell that is the link, not a cell next to one.

    A spreadsheet full of bare addresses makes you copy and paste to follow
    anything; the backend column in particular is the same click the badge on
    the site gives you. Falls back to plain text when there is nothing to open.
    """
    if not text:
        return ""
    if not url:
        return text
    from openpyxl.cell import WriteOnlyCell
    c = WriteOnlyCell(ws, value=text)
    c.hyperlink = url
    c.style = "Hyperlink"
    return c


def _wanted_ids():
    """The page ids asked for by name, or None for "no such request".

    None and empty are different answers: no ids means the level filters
    decide, an ids list that resolves to nothing means the caller asked for
    pages that are not here and should get an empty sheet rather than all
    4,348 of them.

    "Select all matching" is answered the way the bulk actions answer it - by
    re-running the dashboard's own filter - so Export covers the same rows the
    other buttons on that bar would act on rather than only the ticked page.
    """
    if request.form.get("all_matching") == "1":
        wsql, params = _url_filter(request.form.get("q", "").strip(),
                                   request.form.getlist("status"),
                                   request.form.getlist("ptype"),
                                   request.form.getlist("attr"),
                                   request.form.getlist("bl"),
                                   request.form.getlist("pl"))
        con = db.connect()
        try:
            return [r["id"] for r in
                    con.execute("SELECT u.id FROM urls u " + wsql, params)]
        finally:
            con.close()
    raw = " ".join(request.form.getlist("ids")) or request.args.get("ids") or ""
    if not raw.strip():
        return None
    return sorted({int(x) for x in re.findall(r"\d+", raw)})[:5000]


def _export_headers(want_meta, want_comps, want_page=(), verbose=False):
    labels = dict(_export_meta_cols())
    plabels = dict(PAGE_FIELDS)
    clabels = {k: l for k, l, _k2, _a in _all_components()}
    out = [labels[k] for k in want_meta] + [plabels[k] for k in want_page]
    for k in want_comps:
        # hero_image writes its verdict and its link as two cells in verbose
        # mode, so the header has to agree or every column after it shifts
        if k == "hero_image" and verbose:
            out += ["Hero image", "Hero image link"]
            continue
        out.append(clabels.get(k, k))
    return out


# Page facts that are not components and not ownership - the dashboard columns.
EXPORT_PER_PAGE = PER_PAGE

PAGE_FIELDS = [("live", "Live"), ("status", "Status"),
               ("last_captured", "Last captured"), ("changes", "Updates")]

# What an item's meta line can yield. The order of those fields differs by
# category - a document reads "Type | PDF | Public | EN | 01_00 | 1.9MB | date"
# and a design resource "ZIP | 01_00 | EN | 11.39 mb | date" - so each field is
# recognised by its SHAPE rather than its position. Position-based parsing
# would quietly file a version as a language the moment a category differs.
# In the order a row of them reads: what the thing is, then what it is made
# of, then when. A videos sheet drops the columns it has nothing for and comes
# out Time / Language / Date; a documents sheet keeps the lot.
ITEM_FIELDS = [("source", "Found in"),
               ("dtype", "Document type"), ("format", "File type"),
               ("duration", "Time"), ("access", "Access"),
               ("language", "Language"), ("version", "Version"),
               ("size", "Size"), ("date", "Date"),
               ("backend", "Backend"), ("group", "Group"), ("link", "Link")]

_MF_DATE = re.compile(r"^[A-Z][a-z]{2}\.?\s+\d{1,2},\s*\d{4}$")
_MF_SIZE = re.compile(r"^[\d.,]+\s*[KMG]B$", re.I)
_MF_VER = re.compile(r"^v?\d{1,2}[._]\d{1,2}[A-Za-z]?$")
_MF_LANG = re.compile(r"^[A-Z]{2}(\s*,\s*[A-Z]{2})*$")
_MF_FMT = re.compile(r"^(PDF|ZIP|EXE|PPTX?|DOCX?|XLSX?|TXT|CSV|MP4|HTML?|"
                     r"RAR|7Z|MSI|DMG|JPG|PNG|SVG|GZ|TAR|BIN|HEX)$", re.I)
_MF_DUR = re.compile(r"^\d{1,3}:\d{2}(:\d{2})?$")
_MF_ACCESS = re.compile(r"^(public|myinfineon|login|restricted)$", re.I)


def parse_item_meta(meta, doc_type=""):
    """Split an item's meta line into named fields, by shape not position."""
    out = {"dtype": (doc_type or "").strip()}
    for raw in (meta or "").split("|"):
        p = raw.strip()
        if not p:
            continue
        if _MF_DATE.match(p):
            out.setdefault("date", p)
        elif _MF_SIZE.match(p):
            out.setdefault("size", p)
        elif _MF_VER.match(p):
            out.setdefault("version", p)
        elif _MF_DUR.match(p):
            out.setdefault("duration", p)
        elif _MF_FMT.match(p):
            out.setdefault("format", p.upper())
        elif _MF_ACCESS.match(p):
            out.setdefault("access", p)
        elif _MF_LANG.match(p):
            out.setdefault("language", p.replace(" ", ""))
        elif not out.get("dtype"):
            out["dtype"] = p          # the leading descriptive field
    return out


# What each row-kind MEANS, as one predicate over items - the Content tab's own
# definitions, not the raw scrape category. Most videos are captured as News or
# Trainings cards, so category='videos' finds 35 of the 349 videos that exist;
# an export that disagreed with the library about what a video is would be
# describing a different corpus from the rest of the site.
ITEM_KIND_SQL = {
    "docs": _BROWSE_SQL["docs"],
    "videos": _BROWSE_SQL["videos"],
    "images": _BROWSE_SQL["images"],
    "design": _BROWSE_SQL["design"],
    "products": _BROWSE_SQL["products"],
    # Not tiles on the Content tab, but real captured content, so they stay
    # exportable. Defined as NOT video-like precisely because Videos above
    # already claims the news and training cards that are videos - without the
    # negation those items would appear under two kinds and the totals would
    # over-count, which is the same overlap that folding boards into design
    # resources removed.
    "trainings": "(category='trainings' AND NOT " + _VIDEOLIKE_SQL + ")",
    "news": "(category='news' AND NOT " + _VIDEOLIKE_SQL + ")",
    "partners": "category='partners'",
}
# The predicates above are written against a bare items table, because that is
# how the Content tab uses them. Joined against urls they need the alias, and
# hand-maintaining a second aliased copy is how the two drift apart.
_ITEM_COLS = ("category", "item_url", "meta", "grp", "title")
_Q_ITEMS_RE = re.compile(r"\b(?<!\.)(" + "|".join(_ITEM_COLS) + r")\b")


def _q_items(sql):
    """Qualify a bare items predicate with the i. alias."""
    return _Q_ITEMS_RE.sub(r"i.\1", sql)


def _item_dedup_key(r):
    """One row per title+link - the Content tab's own grouping.

    Grouping by item_key instead counts 367 videos where the library lists
    349: the same asset placed under two different section headings gets two
    keys. Two views of one library must not report two sizes.
    """
    return ((r["title"] or "").lower(), r["item_url"] or "")


# Page types as the site's own taxonomy, matching the Type filter elsewhere:
# products and parts are ONE family down to the part type page, so PTP and EB
# nest with them rather than standing alone. A parent is a family, not a value -
# no page's level is "Applications" - so ticking one can only mean ticking its
# children, and the parent itself is never submitted.
LEVEL_GROUPS = [
    ("Products & eval boards", ("PC", "PFP", "PSFP", "PEFP", "PTP", "EB")),
    ("Applications", APP_LEVELS),
    ("Design resources", DR_LEVELS),
]
_GROUPED_LEVELS = {l for _t, ls in LEVEL_GROUPS for l in ls}
_FAMILY_OF = {l: t for t, ls in LEVEL_GROUPS for l in ls}

# WHERE an item was found, which only becomes a question once a library stops
# matching the section it was scraped from. Videos are the case: the same
# library holds the page's own video section, the videos inside News &
# highlights cards and the ones inside Trainings & webinars cards, and "which
# of the three" is a real property of the asset that nothing else records.
ITEM_SOURCE_LABELS = dict(SECTION_LABELS)
ITEM_SOURCE_LABELS["videos"] = "On page"


_SOURCE_ORDER = [c for c, _l in SECTION_LABELS]


def item_source(cats):
    """The section(s) an item was captured from, in the site's own order.

    A set rather than a value: eighteen videos sit in a News & highlights card
    on one page and in the page's own video section on another, so picking
    whichever row the dedup happened to see first would label them at random.
    """
    if isinstance(cats, str):
        cats = [cats]
    seen = [c for c in _SOURCE_ORDER if c in set(cats)]
    seen += sorted(set(cats) - set(_SOURCE_ORDER))
    return ", ".join(ITEM_SOURCE_LABELS.get(c, c) for c in seen if c)


@app.template_filter("isource")
def _isource(cats):
    return item_source(cats)
# above this a group is clutter rather than content, so it arrives folded. Below
# it nothing is hidden - a search that returns six pages should still just show
# six pages rather than making you open a box to find them.
FAMILY_FOLD_AT = 12


def item_kind(category, url="", meta=""):
    """Which library an item belongs to - the Python twin of ITEM_KIND_SQL.

    Same order and same rules, so a search result lands in the group whose
    count the tile above it shows. Videos first, because a video captured
    from a News card is a video and not a news item.
    """
    if is_video_item(category, url, meta):
        return "videos"
    return {"documents": "docs", "images": "images",
            "design_resources": "design", "products": "products",
            "trainings": "trainings", "news": "news",
            "partners": "partners"}.get(category, category)


def by_kind(groups):
    """Search hits split into the libraries, biggest first.

    A flat list mixes documents, boards and images into one column and
    leaves you counting which is which; the page-text hits above them have
    been grouped since the families went in, and these are the same problem.
    """
    buckets = {}
    for g in groups:
        buckets.setdefault(
            item_kind(g.get("category", ""), g.get("url", ""),
                      g.get("meta", "")), []).append(g)
    order = [k for k, _l in ITEM_KINDS_ALL] + sorted(
        set(buckets) - {k for k, _l in ITEM_KINDS_ALL})
    labels = dict(ITEM_KINDS_ALL)
    out = [{"key": k, "name": labels.get(k, SECTION_LABELS_D.get(k, k)),
            "hits": buckets[k], "n": len(buckets[k])}
           for k in order if k in buckets]
    # one group is not a grouping - open it, and open the largest either way
    for i, gr in enumerate(out):
        gr["open"] = len(out) == 1 or i == 0
    return out


def by_family(hits):
    """Search results split into the page families, in the site's own order.

    Sixty page cards in one column is a wall - and on a term that appears in
    the shared navigation, fifty-six of them are the same kind of page saying
    the same thing. The same sixty under "Products & eval boards 56 /
    Applications 3 / Design resources 1" can be read without opening anything.
    """
    buckets = {}
    for h in hits:
        buckets.setdefault(_FAMILY_OF.get(h.get("level") or "",
                                          "Other pages"), []).append(h)
    order = [t for t, _ls in LEVEL_GROUPS] + ["Other pages"]
    return [{"name": t, "hits": buckets[t], "n": len(buckets[t]),
             "open": len(buckets[t]) <= FAMILY_FOLD_AT}
            for t in order if t in buckets]


# the five the Content tab tiles, in its order, then the rest
ITEM_KINDS = [("docs", "Documents"), ("videos", "Videos"),
              ("images", "Images"),
              ("design", "Design resources & boards"),
              ("products", "Related products")]
ITEM_KINDS_MORE = [("trainings", "Trainings & webinars"),
                   ("news", "News & highlights"),
                   ("partners", "Partner solutions")]
ITEM_KINDS_ALL = ITEM_KINDS + ITEM_KINDS_MORE
SECTION_LABELS_D = dict(SECTION_LABELS)
# two kinds are keyed by what they ARE rather than by the category they were
# scraped from. An old link carrying the scrape name must not silently drop to
# "every kind" - that answers a different question and still looks like a
# valid export.
_LEGACY_KIND = {"documents": "docs", "design_resources": "design"}


def _wanted_kinds(raw):
    valid = dict(ITEM_KINDS_ALL)
    out = []
    for k in raw:
        k = _LEGACY_KIND.get(k, k)
        if k in valid and k not in out:
            out.append(k)
    return out


def _export_item_rows(con, kinds, levels, cols=()):
    """One row per ITEM, with a column per page type saying where it is used.

    The other view answers "what does this page have". This answers the
    reverse - "where does this document live" - which is the question you
    actually have when a datasheet needs replacing and you need to know every
    page that will change.
    """
    if not kinds:
        kinds = [k for k, _l in ITEM_KINDS_ALL]
    want_f = [f for f, _l in ITEM_FIELDS if "f_" + f in cols]
    _sset = db.get_settings()          # once per build, not once per item
    kind_sql = " OR ".join("(" + _q_items(ITEM_KIND_SQL[k]) + ")" for k in kinds)
    # Label each row by the kind it MATCHED, not by the category it was scraped
    # from: a video captured as a news card is a video, and saying "News &
    # highlights" in the Kind column of a Videos export is just wrong. First
    # match wins, in the order the kinds are listed, so an asset that is both a
    # document and a training reads as the former consistently.
    kind_case = ("CASE " + " ".join(
        "WHEN (" + _q_items(ITEM_KIND_SQL[k]) + ") THEN " + repr(k)
        for k in kinds) + " END AS kind")
    q = ("SELECT " + kind_case + ", i.item_key, i.category, i.title,"
         " i.item_url, i.meta,"
         " i.doc_type, i.grp, IFNULL(u.level,'') AS lv, u.id AS pid,"
         " u.url AS purl,"
         " COALESCE(NULLIF(u.title_override,''),"
         "  (SELECT s.page_title FROM snapshots s WHERE s.url_id=u.id"
         "    AND s.status='ok' AND s.page_title<>''"
         "    ORDER BY s.id DESC LIMIT 1)) AS ptitle"
         " FROM items i JOIN urls u ON u.id=i.url_id"
         " WHERE u.active=1 AND (" + kind_sql + ")")
    args = []
    if levels:
        q += " AND IFNULL(u.level,'') IN (" + ",".join("?" * len(levels)) + ")"
        args += levels
    by = {}
    for r in con.execute(q, args):
        e = by.setdefault(_item_dedup_key(r), {
            "key": r["item_key"], "category": r["category"],
            "kind": r["kind"] or r["category"], "cats": set(),
            "title": r["title"] or "", "url": r["item_url"] or "",
            "meta": r["meta"] or "", "dtype": r["doc_type"] or "",
            "fields": {}, "levels": {}, "pages": []})
        e["cats"].add(r["category"])
        if want_f and not e["fields"]:
            got = parse_item_meta(r["meta"], r["doc_type"])
            got["group"] = r["grp"] or ""
            got["link"] = r["item_url"] or ""
            got["backend"] = backend_system(r["category"], r["meta"],
                                            r["doc_type"], r["grp"],
                                            r["item_url"] or "")
            got["backend_url"] = backend_search_link(
                got["backend"], asset_name(r["category"], r["item_url"] or ""),
                _sset) if got["backend"] else ""
            e["fields"] = {f: got.get(f, "") for f in want_f}
            e["backend_url"] = got.get("backend_url", "")
        e["levels"][r["lv"]] = e["levels"].get(r["lv"], 0) + 1
        if len(e["pages"]) < 40:
            e["pages"].append({"id": r["pid"], "title": r["ptitle"] or r["purl"]})
    for e in by.values():
        e["source"] = item_source(e["cats"])
        if "source" in want_f:          # only known once every row is in
            e["fields"]["source"] = e["source"]
    rows = sorted(by.values(),
                  key=lambda e: (e["category"], e["title"].lower()))
    used = [l for l in (levels or sorted(
        {l for e in rows for l in e["levels"]}))]
    return rows, used, want_f


def _export_pair_rows(con, kinds, levels):
    """One row per document ON each page - the join of the other two shapes.

    "A row per page" says what a page has, by title only. "A row per item"
    says where a document lives, with its metadata, but one line per document
    however many pages carry it. Anyone assessing content completeness per
    product page was left joining the two by hand: this is that join, done
    here. Every metadata field the page view shows rides along as its own
    column, parsed once per row from the same meta line the dashboard reads.
    """
    if not kinds:
        kinds = ["docs"]
    _sset = db.get_settings()
    kind_sql = " OR ".join("(" + _q_items(ITEM_KIND_SQL[k]) + ")"
                           for k in kinds)
    kind_case = ("CASE " + " ".join(
        "WHEN (" + _q_items(ITEM_KIND_SQL[k]) + ") THEN " + repr(k)
        for k in kinds) + " END AS kind")
    q = ("SELECT " + kind_case + ", i.category, i.title, i.item_url, i.meta,"
         " i.doc_type, i.grp, IFNULL(u.level,'') AS lv, u.url AS purl,"
         " IFNULL(u.bl,'') AS bl, IFNULL(u.pl,'') AS pl,"
         " COALESCE(NULLIF(u.title_override,''),"
         "  (SELECT s.page_title FROM snapshots s WHERE s.url_id=u.id"
         "    AND s.status='ok' AND s.page_title<>''"
         "    ORDER BY s.id DESC LIMIT 1), u.url) AS ptitle"
         " FROM items i JOIN urls u ON u.id=i.url_id"
         " WHERE u.active=1 AND (" + kind_sql + ")")
    args = []
    if levels:
        q += " AND IFNULL(u.level,'') IN (" + ",".join("?" * len(levels)) + ")"
        args += levels
    q += " ORDER BY u.url, i.category, i.grp, i.title"
    fields = [f for f, _l in ITEM_FIELDS if f not in ("group", "link")]
    rows = []
    for r in con.execute(q, args):
        got = parse_item_meta(r["meta"], r["doc_type"])
        got["backend"] = backend_system(r["category"], r["meta"],
                                        r["doc_type"], r["grp"],
                                        r["item_url"] or "")
        got["source"] = item_source({r["category"]})
        rows.append({
            "level": r["lv"], "bl": r["bl"], "pl": r["pl"],
            "ptitle": r["ptitle"], "purl": r["purl"],
            "grp": r["grp"] or "", "kind": r["kind"] or r["category"],
            "title": r["title"] or "", "url": r["item_url"] or "",
            "fields": {f: got.get(f, "") for f in fields}})
    return rows, fields


def _item_fields_present(r):
    """The data fields one item row carries, from a SINGLE parse of its meta.

    One definition, used both to count a field and to decide whether a row
    still qualifies once that field is asked for - so the two numbers on
    screen cannot contradict each other. Asking field by field re-parsed the
    same meta line eleven times a row and took the whole panel from 450 ms
    to over a second.
    """
    got = parse_item_meta(r["meta"], r["doc_type"])
    out = {f for f, _l in ITEM_FIELDS if got.get(f)}
    if (r["grp"] or "").strip():
        out.add("group")
    if (r["item_url"] or "").strip():
        out.add("link")
    if item_source(r["category"]):
        out.add("source")
    if backend_system(r["category"], r["meta"], r["doc_type"], r["grp"],
                      r["item_url"] or ""):
        out.add("backend")
    return out


def _export_avail(con, mode, levels, kinds, cols=()):
    """Cached against the database's own write clock.

    The answer only moves when a run writes, and the panel asks again on every
    tick - including for combinations you have already seen while toggling a
    box on and off. Keyed on the exact selection, so nothing is shared between
    two different questions.
    """
    key = "avail|%s|%s|%s|%s" % (mode, ",".join(sorted(levels or [])),
                                 ",".join(sorted(kinds or [])),
                                 ",".join(sorted(cols or [])))
    return _cached_by_db(key, lambda: _export_avail_build(
        con, mode, levels, kinds, cols))


def _export_avail_build(con, mode, levels, kinds, cols=()):
    """How much of each option actually exists for the current selection.

    Shown beside every checkbox so the answer to "do these pages even have
    ordering tabs" is on screen before you build anything - and an option with
    nothing behind it greys out instead of producing a column of dashes.
    """
    out = {}
    if mode == "items":
        ks = kinds or [k for k, _l in ITEM_KINDS_ALL]
        lv_sql, lv_args = "", []
        if levels:
            lv_sql = (" AND IFNULL(u.level,'') IN ("
                      + ",".join("?" * len(levels)) + ")")
            lv_args = list(levels)
        # ONE pass. Each deduped row is labelled with the kind it matched -
        # first match wins, the same order the rows themselves use - and both
        # the kind counts and the field counts are read off that. A data column
        # narrows the kinds the same way a page type does: ask for Time and
        # Documents says 0, because a document has no duration.
        want_f = [f for f, _l in ITEM_FIELDS if "f_" + f in cols]
        all_k = [k for k, _l in ITEM_KINDS_ALL]
        case = ("CASE " + " ".join(
            "WHEN (" + _q_items(ITEM_KIND_SQL[k]) + ") THEN " + repr(k)
            for k in all_k) + " END AS kind")
        rows = con.execute(
            "SELECT " + case + ", i.meta, i.doc_type, i.grp, i.item_url,"
            " i.category FROM items i JOIN urls u ON u.id=i.url_id"
            " WHERE u.active=1 AND ("
            + " OR ".join("(" + _q_items(ITEM_KIND_SQL[k]) + ")" for k in all_k)
            + ")" + lv_sql + " GROUP BY lower(i.title), i.item_url",
            lv_args).fetchall()

        out.update({k: 0 for k in all_k})
        seen = {f: 0 for f, _l in ITEM_FIELDS}
        for r in rows:
            fields = _item_fields_present(r)
            # the field tally counts every row of the CHOSEN kinds...
            if r["kind"] in ks:
                for f in fields:
                    seen[f] += 1
            # ...while a kind counts the rows that carry every chosen field
            if r["kind"] and all(f in fields for f in want_f):
                out[r["kind"]] += 1
        out.update({"f_" + k: v for k, v in seen.items()})
        # and how many pages of each level carry any of the chosen kinds
        for lv in con.execute(
                "SELECT IFNULL(u.level,'') lv, COUNT(DISTINCT u.id) n"
                " FROM urls u JOIN items i ON i.url_id=u.id"
                " WHERE u.active=1 AND ("
                + " OR ".join("(" + _q_items(ITEM_KIND_SQL[k]) + ")"
                              for k in ks)
                + ") GROUP BY lv"):
            out["lv_" + lv["lv"]] = lv["n"]
        return out

    # pages mode: how many of the chosen pages carry each component
    lv_sql, lv_args = "", []
    if levels:
        lv_sql = (" AND IFNULL(level,'') IN ("
                  + ",".join("?" * len(levels)) + ")")
        lv_args = list(levels)
    total = con.execute("SELECT COUNT(*) FROM urls WHERE active=1" + lv_sql,
                        lv_args).fetchone()[0]
    out["_total"] = total

    # It has to narrow BOTH ways. Picking Evaluation board told you which
    # columns have anything for it, but picking Images did not tell you
    # which page types have images - so the same question answered or did
    # not depending on which pane you happened to start in. A level now
    # counts the pages of that level carrying EVERY component column you
    # have ticked, and drops out when that is none of them.
    reg_by_key = {kk: (kd, a) for kk, _l2, kd, a in _all_components()}
    conds, cargs = [], []
    # Ownership columns narrow too: asking for BL should show which page
    # types actually have one. Page-status columns deliberately do not -
    # every page has a status, so requiring one would exclude nothing and
    # the number would only pretend to have moved.
    for k, _l in _export_meta_cols():
        if k in cols:
            conds.append("IFNULL(u." + k + ",'')<>''")
    for k in [c for c in cols if c in reg_by_key]:
        kind, arg = reg_by_key[k]
        if kind == "cat":
            conds.append("EXISTS(SELECT 1 FROM items i WHERE i.url_id=u.id"
                         " AND i.category=?)")
            cargs.append(arg)
        elif kind == "flag":
            conds.append("json_valid(u.flags) AND json_extract(u.flags,"
                         " '$.' || ? || '.exists') = 1")
            cargs.append(arg)
        else:
            args = arg if isinstance(arg, tuple) else (arg,)
            col = "detail_tabs" if kind == "tab" else "components"
            conds.append("(" + " OR ".join(
                "instr(lower(IFNULL(u." + col + ",'')), ?) > 0"
                for _ in args) + ")")
            cargs.extend(chr(34) + a.lower() + chr(34) for a in args)
    where = (" AND " + " AND ".join(conds)) if conds else ""
    for r in con.execute(
            "SELECT IFNULL(u.level,'') lv, COUNT(*) n FROM urls u"
            " WHERE u.active=1" + where + " GROUP BY lv", cargs):
        out["lv_" + r["lv"]] = r["n"]
    for k, _l in _export_meta_cols():
        out[k] = con.execute(
            f"SELECT COUNT(*) FROM urls WHERE active=1 AND IFNULL({k},'')<>''"
            + lv_sql, lv_args).fetchone()[0]
    for k, _l in PAGE_FIELDS:
        out[k] = total
    cat_of = {k: a for k, _l, kind, a in _all_components() if kind == "cat"}
    for k, a in cat_of.items():
        out[k] = con.execute(
            "SELECT COUNT(DISTINCT u.id) FROM urls u JOIN items i ON i.url_id=u.id"
            " WHERE u.active=1 AND i.category=?" + lv_sql,
            [a] + lv_args).fetchone()[0]
    # nav/flag/tab components live in the page's own component list
    navs = [(k, a) for k, _l, kind, a in _all_components() if kind in ("nav", "tab")]
    if navs:
        rows = con.execute(
            "SELECT components, detail_tabs FROM urls WHERE active=1" + lv_sql,
            lv_args).fetchall()
        pools = []
        for r in rows:
            try:
                nav = {s.lower() for s in json.loads(r["components"] or "[]")}
                nav |= {s.lower() for s in json.loads(r["detail_tabs"] or "[]")}
            except Exception:
                nav = set()
            pools.append(nav)
        for k, a in navs:
            args = a if isinstance(a, tuple) else (a,)
            out[k] = sum(1 for p in pools if any(x in p for x in args))
    # json_valid first: a row with an empty or half-written flags column makes
    # json_extract abort the whole query rather than skip that row
    for k, a in [(k, a) for k, _l, kind, a in _all_components() if kind == "flag"]:
        out[k] = con.execute(
            "SELECT COUNT(*) FROM urls WHERE active=1 AND json_valid(flags)"
            f" AND json_extract(flags, '$.{a}.exists') = 1" + lv_sql,
            lv_args).fetchone()[0]
    return out


@app.route("/export/avail")
def export_avail():
    """Live counts for the builder's checkboxes - no page reload, no build."""
    con = db.connect()
    try:
        data = _export_avail(con, request.args.get("mode", "pages"),
                             request.args.getlist("lv"),
                             _wanted_kinds(request.args.getlist("kind")),
                             request.args.getlist("col"))
    finally:
        con.close()
    return jsonify(data)


@app.route("/export")
def export_builder():
    """Build your own table: pick the page types for the rows, pick the columns,
    read it here, take it away as a spreadsheet.

    The two fixed exports this replaces answered one shape of question each.
    This answers the question actually being asked - "which of THESE pages have
    THAT" - and the sheet you download is the table you were just looking at,
    not a different one produced by different code.
    """
    con = db.connect()
    lv_rows = con.execute(
        "SELECT IFNULL(level,'') lv, COUNT(*) n FROM urls WHERE active=1"
        " GROUP BY lv").fetchall()
    counts = {r["lv"]: r["n"] for r in lv_rows}
    # taxonomy order, so the rows read top-down the way the site nests:
    # product levels, then parts, then applications, then design resources
    order = (("HOME",) + PRODUCT_LEVELS + ("PTP", "EB")
             + APP_LEVELS + DR_LEVELS)
    known = [l for l in order if counts.get(l)]
    known += sorted(l for l in counts if l and l not in order)

    levels = [l for l in request.args.getlist("lv") if l in counts]
    cols = request.args.getlist("col")
    kinds = _wanted_kinds(request.args.getlist("kind"))
    mode = "items" if request.args.get("mode") == "items" else "pages"
    ran = bool(request.args.get("go"))
    rows, want_meta, want_comps, want_page = [], [], [], []
    irows, ilevels, ifields = [], [], []
    if ran and mode == "items":
        irows, ilevels, ifields = _export_item_rows(con, kinds, levels, cols)
    elif ran:
        rows, want_meta, want_comps, want_page = _export_rows(con, levels, cols)
    try:
        avail = _export_avail(con, mode, levels, kinds, cols)
    finally:
        con.close()
    # The table is paged like every other list here. The counts above it
    # still describe the whole result - you are looking at a window on it,
    # and the spreadsheet still contains all of it.
    shown_total = len(irows) if mode == "items" else len(rows)
    xper = _per_page(EXPORT_PER_PAGE)
    xpages = max(1, -(-shown_total // xper))
    try:
        xpage = min(max(1, int(request.args.get("page", 1))), xpages)
    except ValueError:
        xpage = 1
    _lo = (xpage - 1) * xper
    if mode == "items":
        irows = irows[_lo:_lo + xper]
    else:
        rows = rows[_lo:_lo + xper]
    return render_template(
        "export.html", levels_all=known, counts=counts, sel_levels=levels,
        meta_cols=_export_meta_cols(), page_fields=PAGE_FIELDS,
        item_fields=ITEM_FIELDS,
        comp_cols=[(k, l) for k, l, _k, _a in _all_components()],
        sel_cols=cols, rows=rows, ran=ran, mode=mode, avail=avail,
        page=xpage, pages=xpages, per_page=xper,
        per_choices=PER_PAGE_CHOICES,
        first_row=(xpage - 1) * xper,
        kinds_all=ITEM_KINDS, kinds_more=ITEM_KINDS_MORE,
        level_groups=LEVEL_GROUPS, loose_levels=[
            l for l in known if l not in _GROUPED_LEVELS],
        sel_kinds=kinds, labels=dict(ITEM_KINDS_ALL),
        irows=irows, ilevels=ilevels,
        headers=_export_headers(want_meta, want_comps, want_page),
        ifields=ifields, level_names=LEVEL_NAMES,
        shown=shown_total)


PREVIEW_COL_PX = 150            # thumbnail width in the spreadsheet


def _thumb_bytes(shot, px=PREVIEW_COL_PX):
    """A small JPEG of a page's screenshot, for embedding in a spreadsheet.

    JPEG rather than the stored webp: openpyxl will embed webp bytes happily,
    but older Excel cannot draw them and the reader gets an empty frame with
    no clue why. draft() lets the decoder skip most of the work by scaling as
    it reads, which is what makes 4,300 pages about a minute rather than ten.
    """
    from PIL import Image as _Im
    try:
        im = _Im.open(shot)
        im.draft("RGB", (px * 2, px * 2))
        im = im.convert("RGB")
        im.thumbnail((px, px * 4))       # tall pages keep their proportions
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=60, optimize=True)
        buf.seek(0)
        return buf, im.width, im.height
    except Exception:
        return None, 0, 0


def _latest_shots(con):
    """url_id -> path of its newest good screenshot, for the export preview."""
    out = {}
    for r in con.execute(
            "SELECT s.url_id, s.dir FROM snapshots s"
            " JOIN (SELECT url_id, MAX(id) mid FROM snapshots"
            "       WHERE status='ok' AND dir<>'' GROUP BY url_id) m"
            " ON s.id=m.mid"):
        try:
            p = db.shot_path(r["dir"])
            if p.exists():
                out[r["url_id"]] = p
        except Exception:
            pass
    return out


@app.route("/export/download", methods=["GET", "POST"])
def export_builder_download():
    """The table you are looking at, as a spreadsheet - same code path, so the
    file can never disagree with the screen."""
    import tempfile
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    con = db.connect()
    levels = request.args.getlist("lv")
    cols = request.args.getlist("col")
    mode = request.args.get("mode")
    wb = Workbook(write_only=True)
    if mode == "pairs":
        kinds = _wanted_kinds(request.args.getlist("kind"))
        prows, pfields = _export_pair_rows(con, kinds, levels)
        flab = dict(ITEM_FIELDS)
        klab = dict(ITEM_KINDS_ALL)
        ws = wb.create_sheet("Documents by page")
        ws.append(["Level", "BL", "PL", "Page", "Page URL",
                   "Section on page", "Kind", "Title"]
                  + [flab[f] for f in pfields] + ["Link"])
        for e in prows:
            ws.append([e["level"], e["bl"], e["pl"], e["ptitle"], e["purl"],
                       e["grp"], klab.get(e["kind"], e["kind"]), e["title"]]
                      + [e["fields"].get(f, "") for f in pfields]
                      + [e["url"]])
    elif mode == "items":
        kinds = _wanted_kinds(request.args.getlist("kind"))
        irows, ilevels, ifields = _export_item_rows(con, kinds, levels, cols)
        flab = dict(ITEM_FIELDS)
        ws = wb.create_sheet("Items")
        ws.append(["Kind", "Title", "URL"]
                  + [flab[f] for f in ifields]
                  + ["Pages"] + [l or "No level" for l in ilevels]
                  + ["Assigned to"])
        klab = dict(ITEM_KINDS_ALL)
        for e in irows:
            ws.append([klab.get(e["kind"], e["kind"]), e["title"],
                       e["url"]]
                      + [e["fields"].get(f, "") for f in ifields]
                      + [sum(e["levels"].values())]
                      + [e["levels"].get(l, 0) for l in ilevels]
                      + ["; ".join(p["title"] for p in e["pages"])])
    else:
        # "Everything per page" quick download: no columns ticked AND the
        # all=1 flag means give them the lot - every owner, every component's
        # content, the main image - one row per page, URL first. Otherwise
        # honour exactly what was ticked.
        if request.args.get("all") == "1":
            cols = _all_page_columns()
        # verbose: the sheet carries what is on the page, not a yes/no you would
        # then have to open the page to resolve
        rows, want_meta, want_comps, want_page = _export_rows(
            con, levels, cols, verbose=True, ids=_wanted_ids())
        ws = wb.create_sheet("Pages")
        # A PICTURE OF THE PAGE, second column. A row of URLs and yes/nos
        # makes you open the site to know what you are even looking at; the
        # screenshot we already hold answers that in the sheet itself. It sits
        # beside the URL because those two together identify the row.
        shots = _latest_shots(con)
        ws.column_dimensions["B"].width = 24
        # URL first: it is the join key people paste into their own tools
        ws.append(["URL", "Preview", "Page", "Level", "Type"]
                  + _export_headers(want_meta, want_comps, want_page,
                                    verbose=True))
        for i, r in enumerate(rows, start=2):
            shot = shots.get(r["id"])
            buf = None
            if shot is not None:
                buf, _w, h = _thumb_bytes(shot)
            if buf is not None:
                # the row has to be as tall as the picture or Excel overlaps
                # them all into an unreadable stack (points = px * 0.75)
                ws.row_dimensions[i].height = max(18, min(h, 300) * 0.75)
            ws.append([r["url"], "", r["title"], r["level"], r["ptype"]]
                      + r["vals"])
            if buf is not None:
                try:
                    img = XLImage(buf)
                    img.width, img.height = _w, h
                    img.anchor = "B%d" % i
                    ws.add_image(img)
                except Exception:
                    pass
    con.close()
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    f.close()
    wb.save(f.name)
    stamp = datetime.now().strftime("%Y-%m-%d")
    return send_file(f.name, as_attachment=True,
                     download_name=f"infineonwatch-table-{stamp}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument."
                              "spreadsheetml.sheet")


# /export.xlsx is the address this feature had before the export rework, and
# for a while it decorated a helper by mistake - a bookmark from July fetched
# a JSON array of column names. An old address should keep doing the old job.
@app.route("/export.xlsx")
@app.route("/export/all")
def export_xlsx():
    """Everything, as a workbook: every page with all of its content, then one
    sheet per kind of content with that kind's own data and every page it is
    assigned to.

    The old version put all items on one "Library items" sheet, which meant
    filtering a 190,000-row sheet by hand to look at documents. A sheet each
    costs nothing to write and is the shape people actually work in.
    """
    import tempfile
    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    _sset = db.get_settings()          # backend search URLs, read once
    cat_keys = [c for c, _ in SECTION_LABELS]
    registry = _all_components()
    comp_labels = [l for _k, l, _x, _y in registry]

    ws = wb.create_sheet("Pages")
    ws.append(["ID", "URL", "Name", "Page type", "Level", "Status", "HTTP",
               "Last checked (UTC)", "BL", "PL", "CM", "Owner",
               "Text lines", "Image count"]
              + [label for _c, label in SECTION_LABELS]
              + comp_labels + ["Overview text"])

    con = db.connect()
    # every page's captured categories in one pass, for the component answer
    cats = {}
    for r in con.execute("SELECT url_id, category FROM items"
                         " GROUP BY url_id, category"):
        cats.setdefault(r["url_id"], set()).add(r["category"])

    # per-category counts and the overview line come from the index, not from
    # re-reading 4,190 sections.json files off disk - that alone was a minute
    counts_by = {}
    for r in con.execute("SELECT url_id, category, COUNT(*) n FROM items"
                         " GROUP BY url_id, category"):
        counts_by.setdefault(r["url_id"], {})[r["category"]] = r["n"]
    overview_by = {}
    for r in con.execute("SELECT url_id, title FROM items"
                         " WHERE category='overview'"):
        overview_by.setdefault(r["url_id"], r["title"] or "")
    # every stored component's actual text, so the workbook carries content
    # rather than a yes you would have to go and resolve
    cat_args = {a for _k, _l, kind, a in registry if kind == "cat"}
    text_by = {}
    for r in con.execute(
            "SELECT url_id, category, title FROM items WHERE category IN ("
            + ",".join("?" * len(cat_args)) + ") ORDER BY id",
            list(cat_args)):
        text_by.setdefault((r["url_id"], r["category"]), []).append(
            r["title"] or "")
    snap_by = {}
    for r in con.execute(
            "SELECT url_id, MAX(id) AS id, text_lines, image_count"
            " FROM snapshots WHERE status='ok' AND dir<>'' AND rejected=0"
            " GROUP BY url_id"):
        snap_by[r["url_id"]] = r

    for u in con.execute("SELECT * FROM urls WHERE active=1 ORDER BY id"):
        snap = snap_by.get(u["id"])
        cb = counts_by.get(u["id"], {})
        counts = [cb.get(c, 0) for c in cat_keys]
        ov = [{"title": overview_by.get(u["id"], "")}] if overview_by.get(u["id"]) else []
        try:
            flags = json.loads(u["flags"] or "{}")
        except Exception:
            flags = {}
        have = _page_component_set(u["components"], u["detail_tabs"], flags,
                                   cats.get(u["id"], set()), registry,
                                   captured_at=u["last_checked"])
        comp_cells = []
        for k, _l, kind, arg in registry:
            if kind == "cat":
                comp_cells.append(chr(10).join(
                    text_by.get((u["id"], arg), []))[:_CELL_CAP])
            elif k == "hero_image":
                comp_cells.append((flags.get("hero_image") or {}).get("src", "")
                                  or ("yes" if k in have else "no"))
            else:
                comp_cells.append("yes" if k in have else "no")
        ws.append([u["id"], u["url"], u["label"] or "", u["ptype"] or "",
                   u["level"] or "", u["last_status"] or "", u["last_http"],
                   u["last_checked"] or "", u["bl"] or "", u["pl"] or "",
                   u["cm"] or "", u["owner"] or "",
                   snap["text_lines"] if snap else 0,
                   snap["image_count"] if snap else 0]
                  + counts
                  + comp_cells
                  + [ov[0].get("title", "") if ov else ""])

    # One sheet per kind. The content kinds come from ITEM_KIND_SQL so the
    # workbook says the same thing as the builder and the Content tab - by raw
    # category the Videos sheet held 35 of the 349 videos that exist. Page
    # PROSE (subheadline, key features, ...) is not a content kind and stays
    # keyed by its own category.
    prose = [(c, l) for c, l in SECTION_LABELS
             if c in PROSE_CATS or c in ("overview", "categories")]
    sheets = ([(l, "i.category=" + repr(c)) for c, l in prose]
              + [(l, _q_items(ITEM_KIND_SQL[k])) for k, l in ITEM_KINDS_ALL])
    for label, pred in sheets:
        rows = con.execute(
            "SELECT i.item_key, i.title, i.item_url, i.meta, i.doc_type, i.grp,"
            " i.img, i.category, u.id AS pid, u.url AS purl,"
            " IFNULL(u.level,'') AS lv,"
            " u.ptype FROM items i JOIN urls u ON u.id=i.url_id"
            " WHERE u.active=1 AND (" + pred + ")"
            " ORDER BY lower(i.title)").fetchall()
        if not rows:
            continue
        by = {}
        for r in rows:
            e = by.setdefault(_item_dedup_key(r), {
                "title": r["title"] or "", "url": r["item_url"] or "",
                "meta": r["meta"] or "", "dtype": r["doc_type"] or "",
                "cat": r["category"], "cats": set(),
                "grp": r["grp"] or "", "img": r["img"] or "", "pages": []})
            e["cats"].add(r["category"])
            e["pages"].append(r)
        for e in by.values():
            e["f"] = parse_item_meta(e["meta"], e["dtype"])
        # One column per fact, not one cell holding "Article | PDF | Public |
        # EN | 01_00 | 0.48MB | May 04, 2017" - you cannot sort or filter a
        # spreadsheet on the fourth thing after the third pipe. Only the fields
        # this kind actually carries get a column, so the videos sheet is Time /
        # Language / Date rather than a row of empty document columns.
        have = [(k, l) for k, l in ITEM_FIELDS
                if k not in ("link", "backend", "source", "group")
                and any(e["f"].get(k) for e in by.values())]
        multi = len({c for e in by.values() for c in e["cats"]}) > 1
        wsx = wb.create_sheet(label[:31])
        wsx.append(["Title"] + (["Found in"] if multi else [])
                   + [l for _k, l in have]
                   + ["Group", "Link", "Thumbnail", "Backend", "On pages",
                      "Levels", "Assigned to (URLs)"])
        for e in by.values():
            lv = sorted({p["lv"] for p in e["pages"] if p["lv"]})
            bk = backend_system(e["cat"], e["meta"], e["dtype"], e["grp"],
                                e["url"]) or ""
            wsx.append(
                [e["title"]] + ([item_source(e["cats"])] if multi else [])
                + [e["f"].get(k, "") for k, _l in have]
                + [e["grp"],
                   _xl_link(wsx, e["url"], e["url"]),
                   e["img"],
                   # the backend badge on the site opens a search for this
                   # asset; the cell should be the same click, not a word you
                   # then have to go and look up by hand
                   _xl_link(wsx, bk, backend_search_link(
                       bk, asset_name(e["cat"], e["url"]), _sset)),
                   len(e["pages"]), ", ".join(lv),
                   chr(10).join(p["purl"] for p in e["pages"][:200])])

    ws_ch = wb.create_sheet("Updates")
    ws_ch.append(["Update ID", "URL", "When (UTC)", "Summary", "Components",
                  "Verdict"])
    for c in con.execute(
            "SELECT c.*, u.url AS u_url FROM changes c"
            " JOIN urls u ON u.id=c.url_id ORDER BY c.id"):
        verdict = ("to review" if not c["reviewed"] else
                   ("rejected" if c["resolution"] == "reject" else "approved"))
        ws_ch.append([c["id"], c["u_url"], c["ts"], c["summary"],
                      (c["components"] if "components" in c.keys() else "") or "",
                      verdict])
    con.close()

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    name = f"infineonwatch-everything-{stamp}.xlsx"
    out = Path(tempfile.gettempdir()) / name
    wb.save(out)
    return send_file(out, as_attachment=True, download_name=name)


# The blocks in the order an editor reads a page, which is also the order Anas
# asked for: what the page claims, then what it says, then what search sees.
_COPY_SHEET_ORDER = ["features", "benefits", "about", "overview",
                     "subheadline", "meta_description"]


def _copy_cell(det):
    """One block's text, as it goes in the cell.

    Numbered for a list, because the flag beside it says WHICH lines are over
    and a number is how you find line 7 of 14. Bare for a single block, so the
    cell holds exactly the sentence to be rewritten and nothing to strip out.
    """
    blocks = det["blocks"]
    if len(blocks) == 1 and not det["hi"]:
        return blocks[0][:_CELL_CAP]
    return chr(10).join("%d. %s" % (i + 1, b)
                        for i, b in enumerate(blocks))[:_CELL_CAP]


def _copy_flag(det):
    """What is wrong with this block, in the words the page badge uses, plus
    the part a rewriter needs: how many lines are over, and WHICH.

    The badge can afford "68 over 40" because it sits beside the block. A
    spreadsheet cell cannot: against thirteen bullets that does not say which
    one to shorten, and how many are over is the difference between a
    five-minute edit and an afternoon.
    """
    bits = [b for b in det["problems"] if " over " not in b]   # the count rule
    over = det["over"]
    if over:
        longest = max(len(det["blocks"][i]) for i in over)
        n = len(det["blocks"])
        if n == 1:
            bits.append("%d over %d" % (longest, det["chars"]))
        else:
            bits.append("%d of %d line%s over %d (line%s %s), longest %d"
                        % (len(over), n, "s" if n != 1 else "", det["chars"],
                           "s" if len(over) != 1 else "",
                           ", ".join(str(i + 1) for i in over), longest))
    return "; ".join(bits)


@app.route("/export/copy.xlsx")
@app.route("/export/overlimit")
def export_copy_limits():
    """The pages whose copy breaks an editorial limit, WITH the copy.

    The dashboard can already filter to "Features over its limit" and the page
    itself carries a badge, but neither hands you the words. Rewriting is done
    to the text, in bulk, usually by somebody who is not going to open 2,357
    pages one at a time - so this is one row per page, a column per block, and
    the offending block's own text sitting in the cell to be edited.

    A BLOCK THAT IS FINE IS LEFT BLANK. Filling every cell would bury the
    handful of pages with a real problem under a page of correct copy, and the
    point of this sheet is that everything visible in it is something to fix.
    """
    import tempfile
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    sset = db.get_settings()
    limits = {k: (lbl, chars, lo, hi)
              for k, lbl, chars, lo, hi in cmp.copy_limits(sset)}
    order = [k for k in _COPY_SHEET_ORDER if k in limits]

    con = db.connect()
    # the capture the flag was computed from: latest settled, same pick as the
    # page itself makes, so the sheet and the badge quote the same words
    dirs = {}
    for r in con.execute(
            "SELECT url_id, dir FROM snapshots WHERE status='ok' AND dir<>''"
            " AND rejected=0 AND id IN (SELECT MAX(id) FROM snapshots"
            " WHERE status='ok' AND dir<>'' AND rejected=0 GROUP BY url_id)"):
        dirs[r["url_id"]] = r["dir"]

    rows, unread, stale, tally, overage = [], 0, 0, {}, {}
    for u in con.execute(
            "SELECT id, url, label, ptype, level FROM urls WHERE active=1"
            " AND IFNULL(copy_flags,'') NOT IN ('', '{}', '[]')"
            " ORDER BY IFNULL(ptype,''), IFNULL(label,''), id"):
        det = cmp.copy_detail(dirs.get(u["id"], ""), sset)
        bad = {k: d for k, d in det.items() if d["problems"]}
        if not bad:
            # Two different things, and the summary says which: the capture is
            # gone from disk (pruned, so there is nothing to quote), or it read
            # fine and the page no longer breaks the rule - a stored flag from
            # before a limit moved or a rule learned to skip parameter tables.
            # Neither is a row here, and neither is a silent drop.
            if det:
                stale += 1
            else:
                unread += 1
            continue
        for k, d in bad.items():
            tally[k] = tally.get(k, 0) + 1
            if d["over"]:
                worst = max(len(d["blocks"][i]) for i in d["over"])
                overage.setdefault(k, []).append(worst - d["chars"])
        rows.append((u, bad))
    con.close()

    wb = Workbook()
    ws = wb.active
    ws.title = "Over limit"
    head = ["ID", "Page title", "Page URL", "Page type", "Blocks over"]
    for k in order:
        head += [limits[k][0] + " - what is over", limits[k][0]]
    ws.append(head)
    for u, bad in rows:
        line = [u["id"], u["label"] or "", u["url"], u["ptype"] or "",
                len(bad)]
        for k in order:
            d = bad.get(k)
            line += [_copy_flag(d), _copy_cell(d)] if d else ["", ""]
        ws.append(line)

    # readable without touching a single column width: the text columns are
    # the whole point of the sheet and they hold paragraphs
    ws.freeze_panes = "B2"
    for c in ws[1]:
        c.font = Font(bold=True)
    widths = [7, 38, 52, 12, 11] + [26, 60] * len(order)
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")

    # The second sheet is the one that decides what to DO. 2,274 meta
    # descriptions over 150 is either a fortnight of rewriting or a setting
    # that is 10 characters too tight, and the median overage is what tells
    # you which.
    ws2 = wb.create_sheet("Summary")
    ws2.append(["Block", "Limit: chars", "Blocks allowed", "Pages over",
                "Median chars over", "Worst chars over"])
    for c in ws2[1]:
        c.font = Font(bold=True)
    for k in order:
        lbl, chars, lo, hi = limits[k]
        ov = sorted(overage.get(k, []))
        med = ov[len(ov) // 2] if ov else ""
        ws2.append([lbl, chars or "no limit",
                    ("%d-%d" % (lo, hi)) if hi else ("%d+" % lo if lo else "any"),
                    tally.get(k, 0), med, max(ov) if ov else ""])
    ws2.append([])
    ws2.append(["Pages on this sheet", len(rows)])
    ws2.append(["Flagged, but nothing is over any more (re-measured now)", stale])
    ws2.append(["Flagged, but their capture is no longer on disk", unread])
    ws2.append(["Limits are set in", "Settings -> Advanced -> Editorial limits"])
    for i, w in enumerate([22, 13, 15, 12, 18, 18], start=1):
        ws2.column_dimensions[get_column_letter(i)].width = w

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    name = "infineonwatch-over-limit-%s.xlsx" % stamp
    out = Path(tempfile.gettempdir()) / name
    wb.save(out)
    return send_file(out, as_attachment=True, download_name=name)


@app.route("/history")
def history():
    con = db.connect()
    runs = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 100").fetchall()
    con.close()
    return render_template("history.html", runs=runs)


@app.route("/guests")
def guests_page():
    """Who has opened the shared link. Any admin may read it.

    One row per visit rather than per page view, so this reads as a list of
    people who came rather than a server log. There is no name to show and
    deliberately no address; what distinguishes one line from another is when
    it arrived, what it was reading on, and how much of the site it read.
    """
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT * FROM guest_visits ORDER BY last_seen DESC LIMIT 200"
        ).fetchall()
        tot = con.execute(
            "SELECT COUNT(*) v, IFNULL(SUM(hits),0) h, MIN(first_seen) f,"
            " MAX(last_seen) l FROM guest_visits").fetchone()
        # "today" in the reader's timezone, not the server's
        since = (to_local(db.now_iso()).replace(hour=0, minute=0, second=0)
                 .astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        today = con.execute(
            "SELECT COUNT(*) FROM guest_visits WHERE last_seen>=?",
            (since,)).fetchone()[0]
    finally:
        con.close()
    return render_template("guests.html", rows=rows, tot=tot, today=today,
                           link_on=bool(GUEST_KEY))


@app.route("/guests/<int:vid>/delete", methods=["POST"])
def guests_delete(vid):
    """Forget one visit. Owner only - see OWNER_ONLY_VIEWS.

    The browser that made it keeps its session id, so if that person is still
    reading they will reappear as a NEW line rather than silently resuming the
    deleted one. That is the honest behaviour: this forgets a record, it does
    not evict anybody.
    """
    con = db.connect()
    try:
        con.execute("DELETE FROM guest_visits WHERE id=?", (vid,))
        con.commit()
    finally:
        con.close()
    flash("That visit is off the register.")
    return redirect(url_for("guests_page"))


@app.route("/guests/clear", methods=["POST"])
def guests_clear():
    """Empty the register. Owner only - see OWNER_ONLY_VIEWS."""
    con = db.connect()
    try:
        n = con.execute("DELETE FROM guest_visits").rowcount
        con.commit()
    finally:
        con.close()
    flash("Register cleared - %d visit%s forgotten." % (n, "" if n == 1 else "s"))
    return redirect(url_for("guests_page"))


@app.route("/prewarm", methods=["POST"])
def prewarm():
    if not PREWARM["running"]:
        threading.Thread(target=_prewarm_docs, daemon=True).start()
    return jsonify(PREWARM)


def _normalize_statuses():
    """Boot migration for retired states.

    'baseline' - a first capture now lands as 'unchanged' (the next run either
    keeps it there or moves it to Updated).
    'rejected' - rejecting a capture used to park the page in its own state,
    shown as "Awaiting fix", where it sat until the website changed back. That
    is a queue that only ever grows, for a decision already made. Rejecting now
    means only "not this capture as the baseline", so the page reads as
    unchanged; the SNAPSHOT stays flagged rejected and is still never compared
    against, which is the part that does the work.
    """
    con = db.connect()
    n = con.execute("UPDATE urls SET last_status='unchanged'"
                    " WHERE last_status IN ('baseline','rejected')").rowcount
    n += db.reconcile_live_statuses(con)
    con.commit()
    con.close()
    if n:
        print(f"  normalized {n} page(s) from a retired status")


def _normalize_urls():
    """Boot migration: strip tracking junk (?intc=…, utm_*, wcmmode) from
    tracked URLs. A redirect once glued a campaign tag onto its destination,
    and the tagged address slipped past the twin check - one page, tracked
    twice. If the cleaned address is already tracked, the junk row IS that
    page: its short-link origin is grafted onto the keeper and the row goes;
    otherwise the row is simply renamed to its clean address."""
    import shutil as _sh
    con = db.connect()
    for r in con.execute("SELECT id, url, redirect_from, bl, pl, cm, owner"
                         " FROM urls WHERE url LIKE '%?%'").fetchall():
        clean = db.strip_junk_params(r["url"])
        if clean == r["url"]:
            continue
        twin = con.execute("SELECT id, redirect_from, bl, pl, cm, owner"
                           " FROM urls WHERE rtrim(url,'/')=? AND id<>?",
                           (clean.rstrip("/"), r["id"])).fetchone()
        if twin:
            if r["redirect_from"] and not twin["redirect_from"]:
                con.execute("UPDATE urls SET redirect_from=? WHERE id=?",
                            (r["redirect_from"], twin["id"]))
            # the junk row may be the one the CSS report landed its ownership
            # on - carry it over, filling only what the keeper is missing
            for f in ("bl", "pl", "cm", "owner"):
                if (r[f] or "").strip() and not (twin[f] or "").strip():
                    con.execute(f"UPDATE urls SET {f}=? WHERE id=?",
                                (r[f], twin["id"]))
            con.execute("DELETE FROM urls WHERE id=?", (r["id"],))
            con.execute("DELETE FROM page_fts WHERE rowid=?", (r["id"],))
            _sh.rmtree(db.SNAP_DIR / str(r["id"]), ignore_errors=True)
            _sh.rmtree(db.CHANGE_DIR / str(r["id"]), ignore_errors=True)
            print(f"  merged junk-param duplicate #{r['id']} into #{twin['id']}"
                  f" ({clean.rsplit('/', 1)[-1]})")
        else:
            con.execute("UPDATE urls SET url=? WHERE id=?", (clean, r["id"]))
            print(f"  cleaned #{r['id']} -> {clean[:70]}")
    con.commit()
    con.close()


# MUST stay in step with the same test inside SECTIONS_JS - a page read
# before the two agreed keeps its old verdict until this sweep runs over
# it. test_hero_default_test_is_the_same_on_both_sides pins them together.
def _same_asset(a, b):
    """Two image addresses pointing at the same asset.

    Scene7 serves the same picture under a query string ("?$ifx_...") and
    either protocol, so a plain string compare answers no to two spellings of
    one file.
    """
    def k(u):
        u = (u or "").split("?")[0].split("#")[0].rstrip("/").lower()
        return re.sub(r"^https?://", "", u)
    return bool(a) and bool(b) and k(a) == k(b)


_HERO_DEFAULT_RE = re.compile(
    r"default|placeholder|fallback|no-?image|missing-product-images|dumm",
    re.I)


def _sweep_hero_flags():
    """Boot migration: a hero served from Infineon's placeholder set
    (…/missing-product-images/…) is the DEFAULT image, not a page-specific
    one, and must read amber '(default)' rather than green. The original test
    looked for default/placeholder/fallback and missed that folder entirely,
    so pages with a generic part photo claimed a real hero. Recomputed from
    each page's stored hero src - no re-capture needed. Idempotent."""
    con = db.connect()
    fixed = 0
    for r in con.execute("SELECT id, flags FROM urls"
                         " WHERE flags LIKE '%hero_image%'").fetchall():
        try:
            f = json.loads(r["flags"] or "{}")
        except Exception:
            continue
        h = f.get("hero_image") or {}
        if not h.get("exists"):
            continue
        want = not bool(_HERO_DEFAULT_RE.search(h.get("src") or ""))
        if bool(h.get("has_content")) != want:
            h["has_content"] = want
            f["hero_image"] = h
            con.execute("UPDATE urls SET flags=? WHERE id=?",
                        (json.dumps(f, ensure_ascii=False), r["id"]))
            fixed += 1
    con.commit()
    con.close()
    if fixed:
        print(f"  hero flags: {fixed} page(s) corrected to the default image")


def _sweep_images():
    """Boot migration for the image library: collapse responsive renditions of
    one picture (…/foo.jpg?wid=1400 / ?wid=600 / the Scene7 ':asset-…' preset)
    to a single canonical item. Idempotent; runs at every start so data an
    older runner wrote is cleaned regardless of when each page is re-captured.
    (Gifs are content and are kept - only dev-community avatars are dropped,
    and that happens at capture time, by container.)"""
    con = db.connect()
    # developer-community thread avatars (community.infineon.com) + the
    # community section icon are user furniture, not page content - drop them
    # corpus-wide by URL, no re-capture needed (they sit on ~2,469 pages)
    av = con.execute(
        "DELETE FROM items WHERE category='images' AND"
        " (lower(item_url) LIKE '%community.infineon.com%'"
        "  OR lower(item_url) LIKE '%infineon-developer-community.png%')"
    ).rowcount
    # rows filed as images that are not images. A lightbox trigger is written
    # <img src="#">, which the browser resolves against the document - so the
    # page's own address arrived in the picture library, titled "modalImg", on
    # 126 pages. The capture refuses these now; this clears the ones already
    # stored, which would otherwise sit there until each page is next captured.
    notimg = con.execute(
        "DELETE FROM items WHERE category='images'"
        " AND item_url NOT LIKE '%/is/image/%'"
        " AND item_url NOT LIKE '%/jcr:content/renditions/%'"
        " AND lower(item_url) NOT LIKE '%.png%'"
        " AND lower(item_url) NOT LIKE '%.jpg%'"
        " AND lower(item_url) NOT LIKE '%.jpeg%'"
        " AND lower(item_url) NOT LIKE '%.gif%'"
        " AND lower(item_url) NOT LIKE '%.webp%'"
        " AND lower(item_url) NOT LIKE '%.svg%'"
        " AND lower(item_url) NOT LIKE '%.avif%'"
        " AND lower(item_url) NOT LIKE '%.bmp%'"
        " AND lower(item_url) NOT LIKE '%.ico%'").rowcount
    if notimg:
        print(f"  dropped {notimg} row(s) filed as images that are not images")
    seen, dropped = {}, 0
    for r in con.execute("SELECT id, url_id, item_url FROM items"
                         " WHERE category='images' ORDER BY id").fetchall():
        canon = (r["item_url"] or "").split("?")[0].split(":asset")[0]
        key = (r["url_id"], canon)
        if key in seen:
            con.execute("DELETE FROM items WHERE id=?", (r["id"],))
            dropped += 1
        else:
            seen[key] = r["id"]
            if canon != r["item_url"]:
                con.execute("UPDATE items SET item_url=? WHERE id=?",
                            (canon, r["id"]))
    con.commit()
    con.close()
    if av or dropped:
        print(f"  images: dropped {av} community avatar(s), collapsed {dropped}"
              " responsive duplicate(s)")


def _sweep_furniture():
    """Startup sweep: drop template-chrome images (spinner, logos) that an
    OLDER runner indexed before the furniture filter existed. index_items now
    refuses them, but rows written by a run that predates the code - or by a
    server that was mid-run when the filter landed - linger until their page's
    next capture. Idempotent, milliseconds, every boot."""
    con = db.connect()
    n = con.execute(
        "DELETE FROM items WHERE category='images' AND (item_url LIKE"
        " '%/etc.clientlibs/%' OR lower(item_url) LIKE"
        " '%/content/dam/ifx/infineon_logo%')").rowcount
    con.commit()
    con.close()
    if n:
        print(f"  swept {n} furniture image row(s) from the library index")


# ----------------------------------------------------------------- notes ----
# A change monitor answers "what moved". It could not answer "and what did we
# decide about it" - that lived in someone's memory, and by the next run it was
# gone. Every object page carries the same notes section.

def item_target(key, con=None):
    """The identity a note on an ASSET hangs off, and a label for it.

    Must match how the item page itself is keyed: on what the asset IS (title
    plus link), not on one item_key. The same datasheet filed under two
    headings has two keys, and both must open the same conversation - keying a
    note on the key you happened to arrive by would split it in half.
    """
    own = con is None
    con = con or db.connect()
    try:
        r = con.execute("SELECT title, item_url, category FROM items"
                        " WHERE item_key=? LIMIT 1", (key,)).fetchone()
        if not r:
            return None, ""
        if r["category"] in NON_ASSET_CATS:
            return "key:" + key, (r["title"] or key)[:200]
        ident = (r["title"] or "").strip().lower() + "\x00" + (r["item_url"] or "")
        digest = hashlib.sha1(ident.encode("utf-8")).hexdigest()[:20]
        return "asset:" + digest, (r["title"] or r["item_url"] or key)[:200]
    finally:
        if own:
            con.close()


def _notes_for(kind, target_id, con=None):
    """(list, total, open) - what every object page needs to render."""
    rows = comments.for_target(kind, target_id, con=con)
    return rows, len(rows), sum(1 for r in rows if not r["resolved"])


def _back(default="index"):
    nxt = request.form.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return url_for(default)


@app.route("/comment/add", methods=["POST"])
def comment_add():
    kind = request.form.get("kind", "")
    target = request.form.get("target", "")
    label = request.form.get("label", "")
    # for an asset, the key the writer had open - the way back to it later
    hint = request.form.get("hint", "")
    _, err = comments.add(kind, target, request.form.get("body", ""),
                          current_user(), label=label, hint=hint)
    if err:
        flash(err)
    return redirect(_back() + "#notes")


@app.route("/comment/<int:comment_id>/edit", methods=["POST"])
def comment_edit(comment_id):
    err = comments.edit(comment_id, request.form.get("body", ""), current_user())
    if err:
        flash(err)
    return redirect(_back() + "#notes")


@app.route("/comment/<int:comment_id>/delete", methods=["POST"])
def comment_delete(comment_id):
    err = comments.remove(comment_id, current_user(), is_admin=is_admin())
    if err:
        flash(err)
    return redirect(_back() + "#notes")


@app.route("/comment/<int:comment_id>/resolve", methods=["POST"])
def comment_resolve(comment_id):
    err = comments.set_resolved(comment_id, request.form.get("on") == "1",
                                current_user())
    if err:
        flash(err)
    return redirect(_back() + "#notes")


@app.route("/guide")
def guide_page():
    """What the tool does and how the check works, for the team.

    In the app rather than a wiki deliberately: a wiki copy goes stale the
    first time the behaviour changes and nobody notices for months. The bug
    box at the bottom is the ordinary notes section pointed at iRadar itself,
    so reports land in the same feed everything else does.
    """
    # Everything quoted on the page is read live. A number typed into help text
    # is wrong the day someone changes the setting, and nobody notices for
    # months - so the thresholds, the schedule, the counts and even the stock
    # example come from the running system.
    s = db.get_settings()
    con = db.connect()
    try:
        q = lambda sql: con.execute(sql).fetchone()[0]
        facts = {
            "pages": q("SELECT COUNT(*) FROM urls WHERE active=1"),
            "captured": q("SELECT COUNT(DISTINCT url_id) FROM snapshots"
                          " WHERE status='ok'"),
            "docs": q("SELECT COUNT(DISTINCT item_key) FROM items"
                      " WHERE category='documents'"),
            # NOT "items": in a template f.items resolves to dict.items, the
            # built-in method, and formatting it throws
            "tracked": q("SELECT COUNT(*) FROM items WHERE category"
                         " NOT IN " + _NON_ASSET_SQL),
            "open": q("SELECT COUNT(*) FROM changes WHERE IFNULL(reviewed,0)=0"),
            # updates and PAGES are different numbers - one page can carry
            # more than one. The tile links to the page list, so it has to
            # count what that list will show or the two disagree on screen.
            "open_pages": q("SELECT COUNT(DISTINCT url_id) FROM changes"
                            " WHERE IFNULL(reviewed,0)=0"),
            "people": q("SELECT COUNT(*) FROM users WHERE active=1"),
        }
        last = con.execute(
            "SELECT started, finished, total, changed FROM runs"
            " WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        con.close()
    # how long a check takes, from the measured per-page rate x the pages it
    # covers - NOT the last run's wall clock, which counts a resumed run's
    # downtime as work and once read "56 hours" for nine of real capture.
    hours = ""
    spp = seconds_per_page()
    npages = facts.get("live") or facts.get("pages") or 0
    if spp and npages:
        h = spp * npages / 3600.0
        hours = ("about an hour" if h < 1.5
                 else "%.0f hours" % round(h))
    # said from the ticked days themselves: "every day", "every Sunday", or
    # "every Monday and Thursday" - one sentence whatever the shape
    if schedule_mode(s) == "interval":
        n = interval_days(s)
        every = "every day" if n == 1 else "every %d days" % n
    else:
        _names = [_WEEKDAY_NAME.get(d, d.capitalize()) for d in check_days(s)]
        if len(_names) == 7:
            every = "every day"
        elif len(_names) == 1:
            every = "every %s" % _names[0]
        else:
            every = "every %s and %s" % (", ".join(_names[:-1]), _names[-1])
    return render_template(
        "guide.html", f=facts, s=s, every=every,
        at_time="%02d:00" % schedule_hour(s),
        next_run=next_run_text(), run_hours=hours, last=last)


# What changed, newest first. Here rather than in the template because it is
# content, not layout - and here rather than in the database because it is
# written when the change ships, by whoever shipped it.
#
# Keep it to things a REVIEWER would notice. "Refactored the compare module"
# belongs in the git log; "removals of a datasheet now raise the page" belongs
# here, because it changes what lands in somebody's queue on Monday.
# Dates come from when the work actually landed, not from when it was
# written up: the logo, dark mode and the Help rewrite are all the 28th, and
# accounts and notes are the 27th - the 20th was nothing but a guess.
_WEEKDAY_NAME = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
                 "thu": "Thursday", "fri": "Friday", "sat": "Saturday",
                 "sun": "Sunday"}

# what one page costs when there is no history to measure - only used on a
# fresh install, and replaced by the real figure after the first run
_FALLBACK_SECS_PER_PAGE = 9.0


def seconds_per_page(con=None):
    """How long a page actually takes, from the runs that have finished.

    Typed-in estimates rot. This one was 90 seconds against a real 8, so a
    confirmation box offered to spend 86 hours on a job that takes 9 - which
    reads as "do not press this" and is the opposite of informed consent.

    The MEDIAN of the last few runs, not the mean and not the latest: elapsed
    time is wall clock, so a run that was interrupted and resumed hours later
    counts the downtime as work and comes out two or three times too slow. One
    such run drags a mean badly and is a disaster as the latest; the median
    steps over it as long as most runs finished in one go.
    """
    own = con is None
    con = con or db.connect()
    try:
        rates = []
        for r in con.execute(
                "SELECT started, finished, total, ok, errors, IFNULL(stopped,'')"
                " stopped FROM runs"
                " WHERE finished IS NOT NULL AND IFNULL(total,0) > 50"
                " ORDER BY id DESC LIMIT 12"):
            # A run that was stopped, or that did not finish most of its own
            # pages, spent its wall clock on downtime rather than work - its
            # elapsed/page is fiction. run 10 read 56 hours for 12 of real
            # capture because a deploy left it open for two days. Rate is only
            # trustworthy from a run that ran start to finish in one sitting.
            if r["stopped"]:
                continue
            done = (r["ok"] or 0) + (r["errors"] or 0)
            if done < r["total"] * 0.9:
                continue
            try:
                a = datetime.strptime(r["started"], "%Y-%m-%d %H:%M:%S")
                b = datetime.strptime(r["finished"], "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                continue
            secs = (b - a).total_seconds()
            per = secs / r["total"] if r["total"] else 0
            # a real page is 8-70s; anything past 180s/page is a run whose
            # wall clock swallowed downtime and must not set expectations
            if 0 < per <= 180:
                rates.append(per)
    finally:
        if own:
            con.close()
    if not rates:
        return _FALLBACK_SECS_PER_PAGE
    rates.sort()
    return rates[len(rates) // 2]


@app.route("/notes")
def notes_page():
    """Every note anyone has written, newest first. A note nobody can find is a
    note nobody reads, so the conversation has a front door of its own."""
    only_open = request.args.get("open") == "1"
    rows = comments.recent(300, only_open=only_open)
    con = db.connect()
    try:
        # resolve each note back to something clickable
        out = []
        for c in rows:
            link, what = None, c["target_label"] or c["target_id"]
            if c["target_kind"] == "page":
                r = con.execute("SELECT id, url FROM urls WHERE id=?",
                                (c["target_id"],)).fetchone()
                if r:
                    link, what = url_for("url_detail", url_id=r["id"]), \
                        (c["target_label"] or r["url"])
            elif c["target_kind"] == "app":
                # No link. It used to point at Help, because that is where the
                # box lived - it is on this page now, so the link went from
                # here to Help and back for no reason. The chip is a label.
                link, what = None, "iRadar itself"
            elif c["target_kind"] == "change":
                r = con.execute("SELECT id FROM changes WHERE id=?",
                                (c["target_id"],)).fetchone()
                if r:
                    link = url_for("change_view", change_id=r["id"])
            elif c["target_kind"] == "item":
                # The key that was open when the note was written, remembered
                # at write time. This used to search every item per note, which
                # is quadratic in the size of the feed: 1.85s for 40 notes and
                # heading for ~14s at 300. Now it is one lookup, and if that
                # key has since gone the note simply loses its link rather than
                # costing a scan to maybe find another one.
                k = None
                if c["target_id"].startswith("key:"):
                    k = c["target_id"][4:]
                elif c["target_hint"]:
                    k = c["target_hint"]
                if k and not con.execute("SELECT 1 FROM items WHERE item_key=?"
                                         " LIMIT 1", (k,)).fetchone():
                    k = None
                if k:
                    link = url_for("item_detail", key=k)
            out.append({"c": c, "link": link, "what": what})
    finally:
        con.close()
    return render_template("notes.html", rows=out, only_open=only_open,
                           open_total=comments.open_total())


# ---------------------------------------------------------------- accounts --

def _safe_next(target):
    """Only ever redirect to a path on this site - an open redirect turns our
    login page into a convincing springboard to someone else's."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return url_for("index")
    return target


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user() is not None:
        return redirect(url_for("index"))
    nxt = request.values.get("next", "")
    if request.method == "POST":
        email = request.form.get("email", "")
        pw = request.form.get("password", "")
        # A POST means this browser was handed the sign-in page, and the page
        # always sets a cookie. Coming back without one means the browser is
        # not keeping it, and no password can survive the redirect that
        # follows - which is what twenty-four attempts looked like from the
        # outside: the form, again, with nothing said. It is a NOTE and never
        # a refusal; being wrong about this must not become a locked door.
        no_cookie = not request.cookies
        user, err = auth.check_login(email, pw, ip=_client_ip())
        if err:
            return render_template("login.html", error=err, email=email,
                                   next=nxt, no_cookie=no_cookie), 401
        session.clear()          # new session id on sign-in: no fixation
        session["uid"] = user["id"]
        session["pwfp"] = auth.pw_fingerprint(user)
        session.permanent = True
        if user["must_change_pw"]:
            return redirect(url_for("account_password"))
        return redirect(_safe_next(nxt))
    # Reaching the sign-in form means whatever session this browser was
    # carrying is of no further use - an expired one, or one signed with a key
    # this app no longer has. Throw it away and hand back a clean, freshly
    # signed cookie, so a stale cookie cannot shadow the new one forever.
    # THE SILENT LOOP. A correct password sets the session, redirects, and the
    # browser returns without the cookie - so it lands back here with no error
    # to show, and looks exactly like a typo. A same-site referrer with no
    # cookie at all is that bounce; a first visit has no referrer from us.
    bounced = (not request.cookies
               and "iradar" in (request.headers.get("Referer") or ""))
    session.clear()
    csrf_token()
    return render_template("login.html", error=None, email="", next=nxt,
                           no_cookie=bounced)


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))


def _pw_tpl(forced):
    """The forced first-password screen is navless on purpose: it is a lockout,
    and every nav link there would bounce straight back to it. Once past that,
    the profile is an ordinary page in the app like any other."""
    return "first_password.html" if forced else "profile.html"


@app.route("/account", methods=["GET"])
@app.route("/account/password", methods=["GET", "POST"])
def account_password():
    me = current_user()
    forced = bool(me["must_change_pw"])
    if request.method == "POST":
        cur = request.form.get("current", "")
        new = request.form.get("new", "")
        again = request.form.get("again", "")
        # Even when forced, prove they know the current password: a shared
        # machine left signed in should not let a passer-by seize the account.
        if not auth.check_password_hash(me["pw_hash"], cur):
            return render_template(_pw_tpl(forced), forced=forced,
                                   error="Current password is not right."), 400
        if new != again:
            return render_template(_pw_tpl(forced), forced=forced,
                                   error="The two new passwords do not match."), 400
        problem = auth.password_problem(new)
        if problem:
            return render_template(_pw_tpl(forced), forced=forced,
                                   error=problem), 400
        auth.set_password(me["id"], new, must_change=False)
        # Re-stamp OUR session with the new password so this browser stays
        # signed in; every other session for the account now fails its check.
        session["pwfp"] = auth.pw_fingerprint(auth.get_user(me["id"]))
        flash("Password changed. Any other browser signed in as you has been "
              "signed out.")
        return redirect(url_for("index"))
    return render_template(_pw_tpl(forced), forced=forced, error=None)


@app.route("/account/name", methods=["POST"])
def account_name():
    """Set your own display name. No password needed - it is not a credential,
    and asking for one to change how your name is spelled is the kind of thing
    that stops people fixing it."""
    me = current_user()
    auth.set_name(me["id"], request.form.get("name", ""))
    flash("Name updated.")
    return redirect(url_for("account_password"))


@app.route("/account/alerts", methods=["POST"])
def account_alerts():
    """Turn the after-run email report on or off for yourself. It is a
    preference, not a credential, so no password - like the theme toggle."""
    me = current_user()
    on = 1 if request.form.get("on") == "1" else 0
    con = db.connect()
    con.execute("UPDATE users SET email_opt_in=? WHERE id=?", (on, me["id"]))
    con.commit()
    con.close()
    if on and not mailer.configured():
        flash("Email alerts are on for you — but the server has no mail account"
              " set up yet, so nothing can send until an admin adds one.")
    else:
        flash("Email alerts are ON — you'll get a report after scheduled checks."
              if on else "Email alerts are off.")
    return redirect(request.referrer or url_for("account_password"))


@app.route("/digest-chart.png")  # Keep images in already-sent emails working.
@app.route("/report-chart.png")
def report_chart():
    """The donut chart for an email report, rendered as a PNG on demand.
    Public: it is fetched by the recipient's mail client, which has no session
    - and it only draws the aggregate counts handed to it in the query, so
    there is nothing here a signed-out request should not see. Each `d` is one
    "Label:value" slice; the run's own numbers make the URL unique, so a mail
    proxy's per-URL cache never serves last week's chart."""
    pairs = []
    for item in request.args.getlist("d")[:12]:
        lab, _sep, val = item.rpartition(":")
        try:
            v = int(val)
        except ValueError:
            continue
        if v > 0 and lab.strip():
            pairs.append((lab.strip(), v))
    png = mailer.render_donut(mailer.cap_slices(dict(pairs))) if pairs         else mailer.render_donut([("No data", 1)])
    resp = app.response_class(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


@app.route("/account/test-email", methods=["POST"])  # Older open account pages.
@app.route("/account/latest-report", methods=["POST"])
def account_latest_report():
    """Send the latest completed report only to the signed-in person.

    This is independent of opt-in and the scheduled once-per-run mail marker.
    """
    me = current_user()
    if not mailer.configured():
        flash("No mail account is set up on the server yet — an admin needs to"
              " add the MAIL_* settings first.")
        return redirect(request.referrer or url_for("account_password"))
    con = db.connect()
    run = con.execute("SELECT * FROM runs WHERE finished IS NOT NULL"
                      " ORDER BY id DESC LIMIT 1").fetchone()
    if not run:
        con.close()
        flash("No finished check yet to summarise.")
        return redirect(request.referrer or url_for("account_password"))
    d = mailer.run_report(con, run)
    con.close()
    when = _report_when(run)
    html = mailer.render_report(d, _base_url(), when)
    err = mailer.send(me["email"], "iRadar — latest report · " + when, html)
    flash("Latest report sent to %s." % me["email"] if not err
          else "Could not send: %s" % err)
    return redirect(request.referrer or url_for("account_password"))


@app.route("/users")
def users_page():
    return render_template("users.html", users=auth.list_users(),
                           seed_password=auth.SEED_PASSWORD)


@app.route("/users/add", methods=["POST"])
def users_add():
    uid, err = auth.create_user(request.form.get("email", ""),
                                request.form.get("name", ""),
                                request.form.get("role", "editor"))
    flash(err if err else "Account created - starting password is %s."
          % auth.SEED_PASSWORD)
    return redirect(url_for("users_page"))


@app.route("/users/<int:user_id>/role", methods=["POST"])
def users_role(user_id):
    err = auth.set_role(user_id, request.form.get("role", "editor"))
    flash(err if err else "Role updated.")
    return redirect(url_for("users_page"))


@app.route("/users/<int:user_id>/active", methods=["POST"])
def users_active(user_id):
    on = request.form.get("active") == "1"
    if user_id == current_user()["id"] and not on:
        flash("You cannot switch off your own account.")
        return redirect(url_for("users_page"))
    err = auth.set_active(user_id, on)
    flash(err if err else ("Account switched on." if on
                           else "Account switched off."))
    return redirect(url_for("users_page"))


@app.route("/users/<int:user_id>/reset", methods=["POST"])
def users_reset(user_id):
    auth.set_password(user_id, auth.SEED_PASSWORD, must_change=True)
    flash("Password reset to %s - they must change it at next sign-in."
          % auth.SEED_PASSWORD)
    return redirect(url_for("users_page"))


@app.route("/users/<int:user_id>/delete", methods=["POST"])
def users_delete(user_id):
    if user_id == current_user()["id"]:
        flash("You cannot delete your own account.")
        return redirect(url_for("users_page"))
    err = auth.delete_user(user_id)
    flash(err if err else "Account removed.")
    return redirect(url_for("users_page"))


def _seed_accounts():
    """Give a FRESH database one admin, so a new install is reachable at all.

    Does nothing once any admin exists. The earlier version re-created a fixed
    address on every start, which quietly undid deleting it: the account came
    back at the next restart. Seeding is a first-run step, not an invariant to
    re-assert - after that, accounts belong to whoever manages them.
    """
    if auth.count_admins():
        return
    owner = os.environ.get("OWNER_EMAIL", "")
    if not owner:
        print("  No accounts yet. Set OWNER_EMAIL to seed the first admin,")
        print("  e.g.  set OWNER_EMAIL=you@company.com  then restart.")
        return
    spec = [(owner, "", "admin")]
    for entry in os.environ.get("SEED_TEAM", "").split(","):
        entry = entry.strip()
        if entry:
            email, _, role = entry.partition(":")
            spec.append((email, "", role or "editor"))
    made = auth.seed_users(spec)
    if made:
        print("  Created account(s): " + ", ".join(made))
        print("  Starting password:  " + auth.SEED_PASSWORD
              + "   (must be changed at first sign-in)")


def _reap_children():
    """Collect finished child processes, because nothing else will.

    In the container this process is PID 1, and PID 1 inherits every orphan.
    A normal init reaps them; python does not, so each Chromium the capture
    finished with stayed in the process table as a zombie - holding no memory
    and no CPU, just a slot. 128 of them had accumulated by the second run,
    and a full table is why the container reached "Cannot fork" and a run
    stopped dead while still reporting itself as running.

    SIGCHLD arrives whenever a child ends; the loop drains every one that is
    ready, since signals coalesce and one delivery can stand for several
    exits. Harmless off the container, where this process is not PID 1 and
    there is nothing to collect.
    """
    if not hasattr(os, "WNOHANG"):
        return                   # Windows has no orphans to collect
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except (ChildProcessError, OSError):
            return
        if pid == 0:
            return


if __name__ == "__main__":
    # before anything is allowed to start a browser
    try:
        import signal
        signal.signal(signal.SIGCHLD, lambda *_: _reap_children())
    except (AttributeError, ValueError):
        pass                     # no SIGCHLD on Windows; nothing to reap there
    db.init_db()
    _seed_accounts()
    _housekeeping("startup")
    scheduler.start()
    reschedule()
    # warm the document previews in the background so item pages open instantly
    threading.Thread(target=_prewarm_docs, daemon=True).start()
    # auto-infer product/application levels after every run
    threading.Thread(target=_post_run_watcher, daemon=True).start()
    # and pick up a check the last process was in the middle of
    threading.Thread(target=_resume_on_boot, daemon=True).start()
    print()
    print("  iRadar is running:  http://127.0.0.1:5000")
    print("  From another device on the same network, use this PC's IP,")
    print("  e.g. http://192.168.x.x:5000 (see README).")
    print("  Next scheduled check:      " + next_run_text())
    print("  Keep this window open. Press Ctrl+C to stop.")

    # Werkzeug's server is a DEVELOPMENT server - its own docs say not to run it
    # in production, and this one is about to sit behind a public domain serving
    # a whole team. waitress is a real WSGI server, pure python, and unlike
    # gunicorn it runs on Windows. Falls back to the dev server only if waitress
    # is missing, so a fresh clone still starts.
    #
    # threads: the capture run lives in THIS process and holds a worker for
    # hours; 8 leaves plenty for the dashboard while a run is going.
    # 0.0.0.0 = reachable from other devices on the local network too.
    # A hosting platform picks the port and tells you through PORT; bind
    # anything else and it decides the service never came up and kills it.
    # Locally there is no PORT, so 5000 stays the default and nothing changes.
    port = int(os.environ.get("PORT", "5000"))
    try:
        from waitress import serve
        print(f"  Server:                    waitress (production) on :{port}")
        print()
        serve(app, host="0.0.0.0", port=port, threads=8,
              ident="iRadar",           # do not advertise the stack in headers
              channel_timeout=120)
    except ImportError:
        print("  Server:                    werkzeug DEV server"
              " - run 'pip install waitress' before going public")
        print()
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False,
                threaded=True)
