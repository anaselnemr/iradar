"""After-run email digest.

Sends each opted-in person a summary of the check when it finishes. SMTP is
read from the environment - unset means the whole feature is dark, so a fresh
clone never tries to send. Nothing here may raise into the run: a mail server
being down is not a reason to fail a nine-hour capture.

The account it sends FROM is the team Gmail; Gmail needs an APP PASSWORD, not
the account password, once 2-step is on. Set:

  MAIL_SMTP_HOST   smtp.gmail.com        (default)
  MAIL_SMTP_PORT   587                   (default, STARTTLS)
  MAIL_USER        esinfineon@gmail.com
  MAIL_PASS        the 16-char app password
  MAIL_FROM        optional; defaults to MAIL_USER
  MAIL_FROM_NAME   optional; defaults to "iRadar"

Railway, measured 2026-09-05, silently drops ALL outbound SMTP - 25, 465,
587, even 2525 - while HTTPS egress is open. So on Railway the SMTP path
above can never connect, and mail goes over an HTTPS API instead:

  RESEND_API_KEY   send via api.resend.com (HTTPS). When set, this path is
                   used and the SMTP settings are ignored. The From address
                   is MAIL_FROM (a verified domain on the Resend account).
"""
import os
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import db


def configured():
    """Is enough set to send at all - either path?"""
    return bool(os.environ.get("RESEND_API_KEY")
                or (os.environ.get("MAIL_USER") and os.environ.get("MAIL_PASS")))


def _from():
    user = os.environ.get("MAIL_USER", "")
    return (os.environ.get("MAIL_FROM_NAME") or "iRadar",
            os.environ.get("MAIL_FROM") or user or "iradar@iradar.net")


def _logo_part():
    """The iRadar mark as an inline (CID) image - survives Gmail and Outlook
    where a data: URI does not. Cached after the first read."""
    global _LOGO_CACHE
    try:
        if _LOGO_CACHE is None:
            from email.mime.image import MIMEImage
            path = os.path.join(os.path.dirname(__file__), "static", "logo.png")
            with open(path, "rb") as f:
                img = MIMEImage(f.read(), _subtype="png")
            img.add_header("Content-ID", "<iradarlogo>")
            img.add_header("Content-Disposition", "inline", filename="iradar.png")
            _LOGO_CACHE = img
        return _LOGO_CACHE
    except Exception:
        return None


_LOGO_CACHE = None


def send(to_addr, subject, html, text_fallback=""):
    """Send one message. Returns None on success, an error string otherwise."""
    if not configured():
        return "mail not configured"
    if os.environ.get("RESEND_API_KEY"):
        return _send_https(to_addr, subject, html, text_fallback)
    host = os.environ.get("MAIL_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("MAIL_SMTP_PORT", "587"))
    user = os.environ["MAIL_USER"]
    pw = os.environ["MAIL_PASS"]
    name, addr = _from()

    root = MIMEMultipart("related")
    root["Subject"] = subject
    root["From"] = formataddr((name, addr))
    root["To"] = to_addr
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_fallback or _strip(html), "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    root.attach(alt)
    logo = _logo_part()
    if logo is not None:
        root.attach(logo)
    msg = root
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as s:
                s.login(user, pw)
                s.sendmail(addr, [to_addr], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(user, pw)
                s.sendmail(addr, [to_addr], msg.as_string())
        return None
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)


def _send_https(to_addr, subject, html, text_fallback=""):
    """The HTTPS path (Resend). A CID image cannot ride an API call, so the
    logo points at the site's own public copy instead - same mark, same look."""
    import json
    import urllib.error
    import urllib.request
    base = os.environ.get("PUBLIC_URL", "https://iradar.net").rstrip("/")
    html = html.replace("cid:iradarlogo", base + "/static/logo.png?v=3")
    name, addr = _from()
    body = json.dumps({
        "from": "%s <%s>" % (name, addr),
        "to": [to_addr],
        "subject": subject,
        "html": html,
        "text": text_fallback or _strip(html),
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=body,
        headers={"Authorization": "Bearer " + os.environ["RESEND_API_KEY"],
                 "Content-Type": "application/json",
                 # api.resend.com sits behind a WAF that 403s the default
                 # Python-urllib agent (error 1010) - name ourselves honestly
                 "User-Agent": "iradar-mailer/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return None
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        return "HTTP %s: %s" % (e.code, detail)
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)


def _strip(html):
    import re, html as _h
    t = re.sub(r"<[^>]+>", " ", html)
    t = _h.unescape(t)
    return re.sub(r"\s+", " ", t).strip()[:4000]


# --------------------------------------------------------------------------
# The smart summary
# --------------------------------------------------------------------------
def run_digest(con, run):
    """The facts worth putting in front of a person after a check.

    Not just the four counters - the reviewer already sees those on the bar.
    What a digest is for is the SHAPE of the week: how much of it is losses
    (the expensive kind), what the biggest changes are by name, what came in
    new, and what is quietly wrong (ownership, errors). Everything is scoped
    to the changes THIS run opened - ts on or after the run started - so a
    resumed or re-run week does not re-announce last week's queue.
    """
    started = run["started"]
    d = {"run": run, "started": started}

    ch = con.execute(
        "SELECT c.*, u.url, u.ptype,"
        " COALESCE(NULLIF(u.title_override,''),"
        "  (SELECT s.page_title FROM snapshots s WHERE s.url_id=u.id"
        "    AND s.status='ok' AND s.page_title<>'' ORDER BY s.id DESC"
        "    LIMIT 1), u.url) AS title"
        " FROM changes c JOIN urls u ON u.id=c.url_id"
        " WHERE IFNULL(c.reviewed,0)=0 AND c.ts >= ?"
        " ORDER BY (c.lines_added + c.lines_removed) DESC, c.id DESC",
        (started,)).fetchall()
    d["updates"] = len(ch)

    # losses: a page that lost text lines or had a library item removed. These
    # are the ones a person most wants to see - a datasheet gone, a section
    # emptied - because a missing thing is invisible until something names it.
    losses = [c for c in ch if (c["lines_removed"] or 0) > (c["lines_added"] or 0)
              or (c["images_removed"] or 0) > 0]
    d["losses"] = len(losses)

    # GAINS are as worth naming as losses - a datasheet ADDED, a section that
    # grew, an image that appeared. A digest that only counts what vanished
    # tells half the week.
    gains = [c for c in ch if (c["lines_added"] or 0) > (c["lines_removed"] or 0)
             or (c["images_added"] or 0) > 0]
    d["gains"] = len(gains)

    # the changes table has no library column - a library change lives in the
    # summary text, written by the same comparison that raised the review. Two
    # phrasings exist: per-item counts ("Documents (+2/-1)") and whole-section
    # moves ("section 'documents' added"), so match the section NAME, not the
    # exact phrase, case-insensitively.
    LIB_MARKS = ("document", "video", "design resource", "training",
                 "partner solution")
    by_kind = {"text": 0, "images": 0, "visual": 0, "library": 0}
    for c in ch:
        if c["text_changed"]:
            by_kind["text"] += 1
        if c["images_changed"]:
            by_kind["images"] += 1
        if c["visual_changed"]:
            by_kind["visual"] += 1
        if any(m in (c["summary"] or "").lower() for m in LIB_MARKS):
            by_kind["library"] += 1
    d["by_kind"] = by_kind

    def _dir(c):
        add = (c["lines_added"] or 0) + (c["images_added"] or 0)
        rem = (c["lines_removed"] or 0) + (c["images_removed"] or 0)
        if rem > add:
            return "loss"
        if add > rem:
            return "gain"
        return "edit"

    d["top"] = [{
        "title": c["title"], "url": c["url"], "id": c["id"],
        "summary": c["summary"] or "", "dir": _dir(c),
    } for c in ch[:7]]

    fu = con.execute(
        "SELECT COUNT(*) FROM asset_changes WHERE IFNULL(reviewed,0)=0"
        " AND ts >= ?", (started,)).fetchone()[0]
    d["file_updates"] = fu

    # pages read for the very first time this run - the newly discovered/added
    first = con.execute(
        "SELECT COUNT(*) FROM urls WHERE active=1 AND last_checked >= ?"
        " AND created_at >= date(?, '-14 days')", (started, started)).fetchone()[0]
    d["new_pages"] = first

    d["errors"] = run["errors"] or 0
    d["mismatch"] = con.execute(
        "SELECT COUNT(*) FROM urls WHERE active=1 AND last_status='ownership'"
    ).fetchone()[0] if _has_status(con, "ownership") else 0
    d["pages"] = con.execute(
        "SELECT COUNT(*) FROM urls WHERE active=1").fetchone()[0]

    # how long the check actually worked, not its wall clock - a resumed run's
    # started-to-finished can span days of downtime
    d["worked"] = _work_span(con, run)
    return d


def _has_status(con, name):
    try:
        return bool(con.execute(
            "SELECT 1 FROM urls WHERE last_status=? LIMIT 1", (name,)).fetchone())
    except Exception:
        return False


def _work_span(con, run):
    """Real working time: first to last page finalised in this run."""
    try:
        r = con.execute(
            "SELECT MIN(u.last_checked) a, MAX(u.last_checked) b FROM urls u"
            " JOIN run_items ri ON ri.url_id=u.id AND ri.run_id=?"
            " WHERE u.last_checked >= ?", (run["id"], run["started"])).fetchone()
        if not r or not r["a"] or not r["b"]:
            return ""
        fmt = "%Y-%m-%d %H:%M:%S"
        secs = (datetime.strptime(r["b"], fmt)
                - datetime.strptime(r["a"], fmt)).total_seconds()
        h, m = int(secs // 3600), int(secs % 3600 // 60)
        return ("%dh %02dm" % (h, m)) if h else ("%dm" % m)
    except Exception:
        return ""


# --------------------------------------------------------------------------
# The email itself. Table layout and inline styles - the only thing every
# mail client renders alike (Outlook ignores most CSS and all of flexbox).
# iRadar's own palette: indigo brand, green for added, red for removed. The
# chart is table cells with proportional widths - a bar chart no client can
# refuse to draw. Everything reads in words, not diff notation: the reviewer
# team should never have to decode "(+59/-59)".
# --------------------------------------------------------------------------
import re as _re

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_INK = "#17161c"      # iRadar's near-black
_MUT = "#6b6a76"
_FAINT = "#9a99a6"
_LINE = "#e7e6ee"
_ACCENT = "#5e6ad2"   # iRadar indigo
_PAPER = "#f5f5f9"
_GOOD = "#16a34a"
_GOOD_BG = "#e7f6ec"
_BAD = "#dc2626"


def _esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _stat(label, value, colour):
    return (
        '<td align="center" width="25%" style="padding:16px 6px;">'
        '<div style="font:800 30px/1 ' + FONT + ';color:' + colour + ';">'
        + _esc(value) + '</div>'
        '<div style="font:700 10px/1.4 ' + FONT + ';letter-spacing:.07em;'
        'text-transform:uppercase;color:' + _MUT + ';margin-top:7px;">'
        + _esc(label) + '</div></td>')


def _bar(label, n, total, colour):
    pct = int(round(100.0 * n / total)) if total else 0
    fill = max(pct, 3) if n else 0
    return (
        '<tr>'
        '<td width="128" style="font:600 12px/1.5 ' + FONT + ';color:' + _INK
        + ';padding:5px 12px 5px 0;white-space:nowrap;">' + _esc(label) + '</td>'
        '<td style="padding:5px 0;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        '<tr><td style="background:' + _LINE + ';border-radius:5px;">'
        '<table role="presentation" width="' + str(fill) + '%" cellpadding="0" '
        'cellspacing="0"><tr><td style="background:' + colour + ';height:9px;'
        'border-radius:5px;font-size:0;line-height:0;">&nbsp;</td></tr></table>'
        '</td></tr></table></td>'
        '<td width="34" align="right" style="font:700 12px/1.5 ' + FONT + ';'
        'color:' + _MUT + ';padding-left:10px;">' + str(n) + '</td>'
        '</tr>')


def _plain(summary):
    """The change summary in words a reviewer reads, not diff notation.

    'page text (+59/-59 lines) . images (+9/-12)' becomes
    '59 lines added, 59 removed . 9 images added, 12 removed'."""
    def counts(a, r, noun, edited=None):
        def word(n):
            n = int(n)
            if not noun:
                return str(n)
            return "%d %s" % (n, noun[:-1] if n == 1 and noun.endswith("s")
                              else noun)
        bits = []
        if a:
            bits.append("%s added" % word(a))
        if r:
            bits.append(("%s removed" % word(r)) if not bits
                        else "%s removed" % int(r))
        if edited:
            bits.append(("%s edited" % word(edited)) if not bits
                        else "%s edited" % int(edited))
        return ", ".join(bits)

    out = []
    for part in (summary or "").split(" \u00b7 "):
        p = part.strip()
        m = _re.fullmatch(r"page text \(\+(\d+)/-(\d+) lines?\)", p)
        if m:
            out.append(counts(m.group(1), m.group(2), "lines")
                       or "text edited")
            continue
        m = _re.fullmatch(r"images? \((?:\+(\d+))?/?(?:-(\d+))?\)", p)
        if m and (m.group(1) or m.group(2)):
            out.append(counts(m.group(1), m.group(2), "images"))
            continue
        m = _re.fullmatch(r"section '([^']+)' (added|removed|emptied|filled)", p)
        if m:
            out.append("\u201c%s\u201d section %s"
                       % (m.group(1).capitalize(), m.group(2)))
            continue
        m = _re.fullmatch(
            r"([A-Z][\w &]+) \((?:\+(\d+))?/?(?:-(\d+))?/?(?:~(\d+))?\)", p)
        if m and (m.group(2) or m.group(3) or m.group(4)):
            body = counts(m.group(2), m.group(3), "", m.group(4)).replace("  ", " ")
            out.append("%s: %s" % (m.group(1), body.strip()))
            continue
        if p == "visual layout":
            out.append("layout shifted")
            continue
        out.append(p)
    return " \u00b7 ".join(x for x in out if x)


def render_digest(d, base_url, when_label):
    """The HTML body. `d` is run_digest(); base_url like https://iradar.net."""
    b = base_url.rstrip("/")
    dash = b + "/?status=changed"

    n = d["updates"]
    fu = d["file_updates"]
    if n == 0 and fu == 0:
        headline = "All quiet this week."
        sub = "Every tracked page read exactly as it did last time \u2014 nothing to review."
    else:
        parts = []
        if n:
            parts.append("<b>%d</b> page update%s" % (n, "" if n == 1 else "s"))
        if fu:
            parts.append("<b>%d</b> file update%s" % (fu, "" if fu == 1 else "s"))
        headline = " and ".join(parts) + " to review."
        good, bad = [], []
        if d.get("gains"):
            good.append("%d pages gained content" % d["gains"])
        if d.get("new_pages"):
            good.append("%d new page%s now watched"
                        % (d["new_pages"], "" if d["new_pages"] == 1 else "s"))
        if d["losses"]:
            bad.append("%d lost something" % d["losses"])
        chunks = good + bad
        sub = ((", ".join(chunks) + ".").capitalize() if chunks
               else "Waiting for you on the dashboard.")

    # each label says WHAT was added or removed - "Added" alone reads as
    # anything from new pages to new emails
    stats = (
        _stat("Page updates", n, _ACCENT)
        + _stat("Gained content", d.get("gains", 0),
                _GOOD if d.get("gains") else _FAINT)
        + _stat("Lost content", d["losses"], _BAD if d["losses"] else _FAINT)
        + _stat("File updates", fu, _INK if fu else _FAINT))

    k = d["by_kind"]
    kt = max(1, max(k.values()) if k.values() else 1)
    chart = ""
    if n:
        chart = (
            '<tr><td style="padding:2px 28px 4px;">'
            '<div style="font:800 12px/1.4 ' + FONT + ';letter-spacing:.06em;'
            'text-transform:uppercase;color:' + _MUT + ';padding:14px 0 8px;">'
            'What changed</div>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            + _bar("Wording", k.get("text", 0), kt, _ACCENT)
            + _bar("Documents & media", k.get("library", 0), kt, "#7c8af0")
            + _bar("Images", k.get("images", 0), kt, "#9aa6f5")
            + _bar("Visual layout", k.get("visual", 0), kt, "#b9c1f9")
            + '</table></td></tr>')

    newcall = ""
    if d.get("new_pages"):
        newcall = (
            '<tr><td style="padding:12px 28px 0;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="background:' + _GOOD_BG + ';border-radius:10px;">'
            '<tr><td align="center" style="padding:12px 16px;font:600 13px/1.5 '
            + FONT + ';color:#12703a;"><b>%d newly tracked page%s</b> read for '
            'the first time this week.'
            '</td></tr></table></td></tr>'
            % (d["new_pages"], "" if d["new_pages"] == 1 else "s"))

    rows_top = ""
    for t in d["top"]:
        rows_top += (
            '<tr><td style="padding:13px 0;border-top:1px solid ' + _LINE + ';">'
            '<a href="' + b + '/change/' + str(t["id"]) + '" '
            'style="font:600 14px/1.4 ' + FONT + ';color:' + _ACCENT + ';'
            'text-decoration:none;">' + _esc(t["title"]) + '</a>'
            '<div style="font:400 12px/1.5 ' + FONT + ';color:' + _MUT + ';'
            'margin-top:4px;">' + _esc(_plain(t["summary"])) + '</div>'
            '</td></tr>')
    big = ""
    if d["top"]:
        big = (
            '<tr><td style="padding:8px 28px 0;">'
            '<div style="font:800 12px/1.4 ' + FONT + ';letter-spacing:.06em;'
            'text-transform:uppercase;color:' + _MUT + ';">The biggest changes</div>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            + rows_top + '</table></td></tr>')

    extra = []
    if d["errors"]:
        extra.append('<span style="color:' + _BAD + ';">%d page%s errored</span>'
                     % (d["errors"], "" if d["errors"] == 1 else "s"))
    if d.get("mismatch"):
        extra.append("%d ownership mismatch%s"
                     % (d["mismatch"], "" if d["mismatch"] == 1 else "es"))
    meta = str(d["pages"]) + " pages watched" \
        + ((" \u00b7 " + " \u00b7 ".join(extra)) if extra else "")
    file_link = (
        '<div style="padding-top:12px;"><a href="' + b + '/assets" '
        'style="font:600 13px/1 ' + FONT + ';color:' + _ACCENT + ';'
        'text-decoration:none;">See the file updates \u2192</a></div>') if fu else ""

    return (
'<!doctype html><html><head><meta charset="utf-8">'
'<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
'<body style="margin:0;padding:0;background:' + _PAPER + ';">'
'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
'style="background:' + _PAPER + ';padding:24px 12px;"><tr><td align="center">'
'<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
'style="max-width:600px;width:100%;background:#ffffff;border:1px solid ' + _LINE + ';'
'border-radius:16px;overflow:hidden;">'

'<tr><td style="background:' + _INK + ';padding:20px 28px;">'
'<table role="presentation" cellpadding="0" cellspacing="0" align="center"><tr>'
'<td style="padding-right:11px;"><img src="cid:iradarlogo" width="30" height="30" '
'alt="iRadar" style="display:block;border-radius:8px;"></td>'
'<td style="font:800 19px/1 ' + FONT + ';color:#ffffff;letter-spacing:-.02em;">iRadar'
'<div style="font:400 12px/1.4 ' + FONT + ';color:#b9bdf0;margin-top:3px;'
'font-weight:400;">Weekly change check \u00b7 ' + _esc(when_label) + '</div>'
'</td></tr></table></td></tr>'

'<tr><td align="center" style="padding:24px 28px 4px;">'
'<div style="font:400 21px/1.4 ' + FONT + ';color:' + _INK + ';">' + headline + '</div>'
'<div style="font:400 13px/1.55 ' + FONT + ';color:' + _MUT + ';margin-top:7px;">'
+ sub + '</div></td></tr>'

'<tr><td style="padding:16px 18px 4px;">'
'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
'style="background:' + _PAPER + ';border-radius:12px;"><tr>' + stats + '</tr></table>'
'</td></tr>'

+ chart
+ newcall
+ big

+ '<tr><td align="center" style="padding:24px 28px 10px;">'
'<a href="' + dash + '" style="display:inline-block;background:' + _ACCENT + ';'
'color:#ffffff;font:700 14px/1 ' + FONT + ';text-decoration:none;padding:14px 30px;'
'border-radius:9px;">Review the queue \u2192</a>' + file_link + '</td></tr>'

'<tr><td align="center" style="padding:8px 28px 4px;">'
'<div style="font:400 11px/1.5 ' + FONT + ';color:' + _FAINT + ';">' + meta
+ '</div></td></tr>'
'<tr><td align="center" style="padding:14px 28px 22px;border-top:1px solid '
+ _LINE + ';">'
'<div style="font:400 11px/1.6 ' + FONT + ';color:' + _FAINT + ';">'
'Email alerts are on for your account \u00b7 '
'<a href="' + b + '/account" style="color:' + _ACCENT + ';text-decoration:none;">'
'Manage them here</a></div></td></tr>'

'</table></td></tr></table></body></html>')
