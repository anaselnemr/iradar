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
    # only carry the inline logo if the HTML actually references it - the
    # digest dropped its masthead logo, and an unreferenced inline image can
    # surface as a stray attachment in some clients.
    if "cid:iradarlogo" in html:
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
# The page-type names as a reviewer reads them, pluralised. Anything not here
# (there should be nothing) falls through to its raw ptype.
_PTYPE_LABELS = {
    "Part": "Part pages",
    "Product": "Product pages",
    "Application": "Application pages",
    "Design resources": "Design resource pages",
    "Home": "Home page",
    "Board": "Evaluation boards",
}


# Which chart bucket each library section's items belong to. The summaries
# name sections ("Documents (+2/-1)", "section 'videos' added"); this folds
# the long tail of product-ish sections into one bucket.
_ITEM_CAT = {
    "documents": "New documents", "videos": "Videos",
    "design resources & boards": "Design resources",
    "design resources": "Design resources",
    "trainings & webinars": "Trainings", "partner solutions": "Partner solutions",
    "news & highlights": "News", "applications": "Applications",
    "related products": "Related products", "browse by category": "Related products",
    "bill of material": "Related products", "product details": "Related products",
    "related product families": "Related products",
    "related products & applications": "Related products",
    "register product": "Related products",
    "product family highlights": "Related products",
    "featured products": "Related products",
}

_ITEM_RE = None
_SECTION_RE = None


def _content_items(changes):
    """The content itself, counted in ITEMS, not pages: how many documents,
    videos, images, boards actually changed across the week. Images come from
    the change row's own exact counters; library items from the per-item
    counts in the summary ("Documents (+2/-1/~3)" = 6 items); a whole section
    appearing or vanishing counts once. Wording and the developer-community
    indicator are page-level facts with no item count, so they are returned
    separately for a footnote rather than distorting the item chart."""
    import re as _re
    global _ITEM_RE, _SECTION_RE
    if _ITEM_RE is None:
        _ITEM_RE = _re.compile(
            r"([A-Za-z][\w &]+) \((?:\+(\d+))?/?(?:-(\d+))?/?(?:~(\d+))?\)")
        _SECTION_RE = _re.compile(r"section '([^']+)' (?:added|removed|emptied|filled)")
    items = {}
    wording_pages = 0
    community_pages = 0

    def bump(cat, n=1):
        if n:
            items[cat] = items.get(cat, 0) + n

    for c in changes:
        if c["text_changed"]:
            wording_pages += 1
        bump("Images", (c["images_added"] or 0) + (c["images_removed"] or 0))
        community_here = False
        for clause in (c["summary"] or "").split(" · "):
            p = clause.strip()
            low = p.lower()
            if (not p or low.startswith("page text") or low.startswith("images")
                    or "visual layout" in low):
                continue
            if "developer community" in low:
                community_here = True
                continue
            if "hero image" in low:
                bump("Images")
                continue
            m = _ITEM_RE.fullmatch(p)
            if m:
                cat = _ITEM_CAT.get(m.group(1).strip().lower(),
                                    m.group(1).strip())
                bump(cat, sum(int(x) for x in m.groups()[1:] if x))
                continue
            m = _SECTION_RE.fullmatch(low)
            if m:
                bump(_ITEM_CAT.get(m.group(1), m.group(1).capitalize()))
                continue
            bump("Other")
        if community_here:
            community_pages += 1
    out = dict(sorted(items.items(), key=lambda kv: -kv[1]))
    return out, wording_pages, community_pages


# Component names for the structure chart, folding the long tail of
# product-ish sections into one and the wording sections into another.
_COMP_FOLD = {
    "documents": "Documents", "videos": "Videos",
    "design resources": "Design resources",
    "design resources & boards": "Design resources",
    "developer community": "Developer community",
    "trainings & webinars": "Trainings", "partner solutions": "Partner solutions",
    "news & highlights": "News", "applications": "Applications",
    "bill of material": "Products", "product details": "Products",
    "related products": "Products", "related product families": "Products",
    "related products & applications": "Products", "register product": "Products",
    "browse by category": "Products", "featured products": "Products",
    "product family highlights": "Products",
    "about": "Text sections", "overview": "Text sections",
    "key features": "Text sections", "benefits": "Text sections",
    "subheadline": "Text sections",
}


def _component_diffs(changes):
    """Whole components appearing, vanishing or going empty, counted in
    PAGES: 'Documents +5/-1' means five pages gained a documents section and
    one lost it this week. Reads the same summaries the review queue shows,
    de-duplicated per page and component."""
    import re as _re
    sec = _re.compile(r"section '([^']+)' (added|removed|emptied|filled)")
    flag = _re.compile(r"(developer community|hero image|products table)"
                       r" (added|removed|emptied|filled)")
    DIR = {"added": "gained", "filled": "gained",
           "removed": "lost", "emptied": "emptied"}
    FLAG_NAME = {"developer community": "Developer community",
                 "hero image": "Hero image", "products table": "Products table"}
    comp = {}
    for c in changes:
        seen = set()
        for clause in (c["summary"] or "").split(" · "):
            low = clause.strip().lower()
            m = sec.fullmatch(low)
            if m:
                seen.add((_COMP_FOLD.get(m.group(1), m.group(1).capitalize()),
                          DIR[m.group(2)]))
                continue
            m = flag.fullmatch(low)
            if m:
                seen.add((FLAG_NAME[m.group(1)], DIR[m.group(2)]))
        for name, d in seen:
            comp.setdefault(name, {"gained": 0, "lost": 0, "emptied": 0})
            comp[name][d] += 1
    out = {}
    for name, v in sorted(comp.items(),
                          key=lambda kv: -sum(kv[1].values())):
        bits = []
        if v["gained"]:
            bits.append("+%d" % v["gained"])
        if v["lost"]:
            bits.append("-%d" % v["lost"])
        label = "%s %s" % (name, "/".join(bits)) if bits else name
        if v["emptied"]:
            # words, not a symbol: the chart font has no glyph that reads as
            # "emptied" without looking like a minus
            label += " (%d emptied)" % v["emptied"]
        out[label] = sum(v.values())
    return out


def _patterns(changes, top=6):
    """Group the week's changes by their SHAPE, most-repeated first.

    A pattern is the change's clauses with every number stripped, so "page
    text (+59/-59 lines)" and "page text (+2/-1 lines)" are the same story
    told twice. Each row carries how many pages share it and one example to
    open. This is what a reviewer actually wants: not seven arbitrary pages,
    but "this exact edit landed on 204 of them"."""
    import re as _re
    counts = {}
    examples = {}
    for c in changes:
        seen = []
        for clause in (c["summary"] or "").split(" · "):
            p = clause.strip()
            low = p.lower()
            if not p:
                continue
            if low.startswith("page text"):
                w = "text edited"
            elif low.startswith("images ("):
                w = "images changed"
            elif low.startswith("visual layout"):
                w = "layout shifted"
            else:
                # drop the counts, and normalise the two ways a whole section
                # is announced ("X added" and "section 'x' added") into one
                p = _re.sub(r"\s*\((?:\+\d+)?/?(?:-\d+)?/?(?:~\d+)?\)", "", p)
                m = _re.fullmatch(r"section '([^']+)' (\w+)", p, _re.I)
                if m:
                    w = "%s %s" % (m.group(1).capitalize(), m.group(2))
                else:
                    w = p
            wl = w.lower()
            if wl not in [x.lower() for x in seen]:
                seen.append(w)
        if not seen:
            continue
        key = " · ".join(seen)
        counts[key] = counts.get(key, 0) + 1
        # keep the first example that is not the Home page, which changes
        # every week by design and makes a poor illustration
        if key not in examples or (examples[key]["ptype"] == "Home"
                                   and c["ptype"] != "Home"):
            examples[key] = {"id": c["id"], "title": c["title"],
                             "ptype": c["ptype"]}
    rows = sorted(counts.items(), key=lambda kv: -kv[1])[:top]
    return [{"label": k, "pages": n, "id": examples[k]["id"],
             "example": examples[k]["title"]} for k, n in rows]


def _file_types(con, started):
    """File updates grouped by document type. Reads each changed asset's own
    item row for the real type; falls back to the file extension."""
    import os
    import re as _re
    rows = con.execute(
        "SELECT url, kind FROM asset_changes WHERE IFNULL(reviewed,0)=0"
        " AND ts >= ?", (started,)).fetchall()
    out = {}
    for r in rows:
        u = r["url"]
        label = None
        try:
            it = con.execute("SELECT meta FROM items WHERE item_url=? LIMIT 1",
                             (u,)).fetchone()
            if it and it["meta"]:
                first = it["meta"].split("|")[0].strip()
                if first:
                    label = first
        except Exception:
            pass
        if not label:
            m = _re.search(r"\.([a-z0-9]{2,4})(?:\?|$)",
                           os.path.basename(u.split("?")[0]).lower())
            label = (m.group(1).upper() if m
                     else (r["kind"] or "file").capitalize())
        out[label] = out.get(label, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


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

    # PAGES AFFECTED, BY PAGE TYPE. The reviewer's first question is "what
    # KIND of pages moved this week" - 300 part pages is a routine catalogue
    # refresh, three product pages is worth a look. ptype is the page's own
    # classification (Part / Product / Application / Design resources / Home).
    by_ptype = {}
    for c in ch:
        label = _PTYPE_LABELS.get(c["ptype"], c["ptype"] or "Other")
        by_ptype[label] = by_ptype.get(label, 0) + 1
    d["by_ptype"] = dict(sorted(by_ptype.items(), key=lambda kv: -kv[1]))

    # FILE UPDATES, BY DOCUMENT TYPE. asset_changes only knows document/image/
    # video, but the file's own item row carries the real type in its meta
    # ("Datasheet | PDF | Public | EN") - so a datasheet revision reads as a
    # Datasheet, not a nameless "document". Fall back to the file extension for
    # the few assets with no matching item row.
    d["by_filetype"] = _file_types(con, started)

    # THE CONTENT ITSELF, IN ITEMS: how many documents, videos, images, boards
    # actually changed across those pages - a different unit from the page
    # counts above, so the two charts tell two stories instead of one twice.
    # Wording and the community indicator have no item count; they ride along
    # as page tallies for a footnote under the chart.
    d["by_items"], d["wording_pages"], d["community_pages"] = _content_items(ch)

    def _dir(c):
        add = (c["lines_added"] or 0) + (c["images_added"] or 0)
        rem = (c["lines_removed"] or 0) + (c["images_removed"] or 0)
        if rem > add:
            return "loss"
        if add > rem:
            return "gain"
        return "edit"

    # THE RECURRING CHANGES. Seven individual pages told a reviewer almost
    # nothing - they were seven arbitrary members of one enormous crowd. The
    # week's real story is which change REPEATED: 378 pages this run collapse
    # into 28 patterns, one of which covers 204 of them. Group by the shape of
    # the change (the same clauses with the numbers stripped), count the pages,
    # and keep one example so a reviewer can open a real case.
    d["patterns"] = _patterns(ch)

    fu = con.execute(
        "SELECT COUNT(*) FROM asset_changes WHERE IFNULL(reviewed,0)=0"
        " AND ts >= ?", (started,)).fetchone()[0]
    d["file_updates"] = fu
    # the two document stories, side by side in one chart: "New documents"
    # (links added to pages) and "Document updates" (the file behind an
    # existing link replaced with a new version - the page shows nothing)
    if fu:
        d["by_items"]["Document updates"] = fu
        d["by_items"] = dict(sorted(d["by_items"].items(),
                                    key=lambda kv: -kv[1]))
    d["content_updates"] = sum(d["by_items"].values())

    # pages read for the very first time this run - the newly discovered/added
    first = con.execute(
        "SELECT COUNT(*) FROM urls WHERE active=1 AND last_checked >= ?"
        " AND created_at >= date(?, '-14 days')", (started, started)).fetchone()[0]
    d["new_pages"] = first

    d["errors"] = run["errors"] or 0
    # HEALTH, not content: pages the check found broken rather than edited - a
    # page that 404s, or renders blank. These are the "something is wrong on
    # the site" signals, separate from any content change, and the ones most
    # worth a reviewer's eye. Current state (not just this run) so a page that
    # has been down for weeks keeps showing until it is fixed.
    d["notlive"] = con.execute(
        "SELECT COUNT(*) FROM urls WHERE active=1 AND last_status='notlive'"
    ).fetchone()[0]
    d["blank"] = con.execute(
        "SELECT COUNT(*) FROM urls WHERE active=1 AND last_status='blank'"
    ).fetchone()[0]
    d["pages"] = con.execute(
        "SELECT COUNT(*) FROM urls WHERE active=1").fetchone()[0]
    d["errors_total"] = (d["notlive"] + d["blank"] + (d["errors"] or 0))
    d["by_component"] = _component_diffs(ch)
    # watched-pages delta against the previous check: extra pages since last
    # run show as +N beside the count, removals as -N
    prev = con.execute(
        "SELECT total FROM runs WHERE finished IS NOT NULL AND id<?"
        " AND total>=50 ORDER BY id DESC LIMIT 1", (run["id"],)).fetchone()
    d["pages_delta"] = (d["pages"] - prev["total"]) if prev and prev["total"]         else 0

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


def _stat(label, value, colour, suffix=""):
    """One tile. The label is ONE short word on purpose: a phone gives each of
    four tiles about 70px, where "Pages watched" wraps to two lines and its
    neighbours do not, so the row came out ragged. Any delta rides on the
    label line - beside a 26px number it overflowed the tile."""
    return (
        '<td align="center" width="25%" style="padding:16px 4px;">'
        '<div style="font:800 26px/1 ' + FONT + ';color:' + colour + ';'
        'white-space:nowrap;">' + _esc(value) + '</div>'
        '<div style="font:700 10px/1.4 ' + FONT + ';letter-spacing:.06em;'
        'text-transform:uppercase;color:' + _MUT + ';margin-top:7px;'
        'white-space:nowrap;">' + _esc(label) + suffix + '</div></td>')


# The donut palette: iRadar indigo leads, then a spread of distinct hues so
# neighbouring slices never read as the same colour. Grey is reserved for the
# "Other" catch-all so it never competes with a real category.
_DONUT_PAL = ["#5e6ad2", "#e0803a", "#1ea896", "#d6537a", "#8b5cc7",
              "#3b8ce0", "#6fa83a", "#d9a621"]
_DONUT_GREY = "#c9c8d2"


def cap_slices(data, top=8):
    """At most `top` named slices; the rest folded into 'Other'. Labels stay
    unique - overflow merges into an existing 'Other' rather than appending a
    second one (two 'Other' slices collapse to one when the query is decoded,
    silently losing a slice's worth of the total)."""
    items = list(data.items())
    if len(items) <= top:
        return items
    kept = items[:top]
    rest = sum(v for _l, v in items[top:])
    if rest:
        for i, (lab, v) in enumerate(kept):
            if lab == "Other":
                kept[i] = ("Other", v + rest)
                break
        else:
            kept.append(("Other", rest))
    return kept


def render_donut(pairs):
    """A donut chart with a legend, as PNG bytes. Pure PIL so both the web
    route and a local preview can call it. `pairs` is [(label, value), ...]
    already capped and ordered. Email clients strip SVG and CSS charts, so a
    real raster image is the only chart every inbox will show."""
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont
    pairs = [(str(l)[:44], int(v)) for l, v in pairs if int(v) > 0][:9]
    total = sum(v for _l, v in pairs)
    S = 4                                   # supersample, then downscale
    Wf = 540
    rows = max(1, len(pairs))
    Hf = max(210, 24 + rows * 30)
    W, H = Wf * S, Hf * S
    img = Image.new("RGB", (W, H), "#ffffff")
    dr = ImageDraw.Draw(img)

    def font(sz, bold=False):
        try:
            return ImageFont.load_default(size=sz * S)
        except TypeError:
            return ImageFont.load_default()

    def colour(i, label):
        return _DONUT_GREY if label.lower() == "other" \
            else _DONUT_PAL[i % len(_DONUT_PAL)]

    D = 176 * S
    cx = 18 * S + D // 2
    cy = H // 2
    box = [cx - D // 2, cy - D // 2, cx + D // 2, cy + D // 2]
    ang = -90.0
    if total <= 0:
        dr.ellipse(box, fill=_DONUT_GREY)
    else:
        for i, (lab, v) in enumerate(pairs):
            sweep = 360.0 * v / total
            dr.pieslice(box, ang, ang + sweep, fill=colour(i, lab))
            ang += sweep
    hole = int(D * 0.31)
    dr.ellipse([cx - hole, cy - hole, cx + hole, cy + hole], fill="#ffffff")
    ftot = font(24, True)
    tt = str(total)
    tb = dr.textbbox((0, 0), tt, font=ftot)
    dr.text((cx - (tb[2] - tb[0]) / 2, cy - (tb[3] - tb[1]) / 2 - tb[1]),
            tt, font=ftot, fill="#17161c")

    lx = 18 * S + D + 26 * S
    flab = font(13)
    fval = font(13, True)
    ly = int((H - rows * 28 * S) / 2) + 3 * S
    for i, (lab, v) in enumerate(pairs):
        sw = 13 * S
        dr.rounded_rectangle([lx, ly + 2 * S, lx + sw, ly + 2 * S + sw],
                             radius=3 * S, fill=colour(i, lab))
        dr.text((lx + sw + 9 * S, ly), lab, font=flab, fill="#3a3a44")
        pct = round(100.0 * v / total) if total else 0
        vs = "%d  ·  %d%%" % (v, pct)
        vb = dr.textbbox((0, 0), vs, font=fval)
        dr.text((W - 16 * S - (vb[2] - vb[0]), ly), vs, font=fval, fill="#6b6a76")
        ly += 28 * S

    img = img.resize((Wf * 2, Hf * 2), Image.LANCZOS)   # 2x for high-DPI
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


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

    # The tiles ARE the headline - a sentence above them repeated the same
    # numbers in words, so it went. Four independent counts, plus the
    # watched-pages delta as a small +N/-N beside the count.
    delta = d.get("pages_delta", 0)
    dsuf = ""
    if delta:
        dsuf = ('&nbsp;<span style="color:'
                + (_GOOD if delta > 0 else _BAD) + ';">'
                + ("+%d" % delta if delta > 0 else "\u2212%d" % -delta)
                + '</span>')
    nl = d.get("notlive", 0)
    # four numbers, four colours - the watched total and the content count
    # both read as plain ink before, so the eye could not tell them apart
    stats = (
        _stat("Pages", "{:,}".format(d.get("pages", 0)), _INK, dsuf)
        + _stat("Updated", n, _ACCENT)
        + _stat("Content", d.get("content_updates", 0),
                "#1ea896" if d.get("content_updates") else _FAINT)
        + _stat("Offline", nl, _BAD if nl else _FAINT))

    def _chart_img(title, data, footnote=""):
        if not data:
            return ""
        from urllib.parse import quote
        q = "&".join("d=" + quote("%s:%d" % (lab, v))
                     for lab, v in cap_slices(data))
        src = b + "/digest-chart.png?" + q
        return (
            '<tr><td style="padding:6px 28px 2px;">'
            '<div style="font:800 12px/1.4 ' + FONT + ';letter-spacing:.06em;'
            'text-transform:uppercase;color:' + _MUT + ';padding:16px 0 8px;">'
            + title + '</div>'
            '<img src="' + src + '" width="540" alt="' + _esc(title) + '" '
            'style="display:block;width:100%;max-width:540px;height:auto;'
            'border:0;outline:none;">'
            + (('<div style="font:400 11px/1.5 ' + FONT + ';color:' + _FAINT
                + ';padding-top:6px;">' + _esc(footnote) + '</div>')
               if footnote else '')
            + '</td></tr>')

    chart = ""
    if n:
        chart += _chart_img("Pages affected, by type", d.get("by_ptype"))
        chart += _chart_img("Content updates, by type", d.get("by_items"))
        chart += _chart_img(
            "Component changes, in pages", d.get("by_component"))
    newcall = ""

    rows_top = ""
    for t in d.get("patterns", []):
        rows_top += (
            '<tr><td width="58" valign="top" style="padding:13px 12px 13px 0;'
            'border-top:1px solid ' + _LINE + ';font:800 17px/1.2 ' + FONT + ';'
            'color:' + _ACCENT + ';white-space:nowrap;">'
            + "{:,}".format(t["pages"]) + '</td>'
            '<td style="padding:13px 0;border-top:1px solid ' + _LINE + ';">'
            '<div style="font:600 13px/1.45 ' + FONT + ';color:' + _INK + ';">'
            + _esc(t["label"]) + '</div>'
            '<div style="font:400 12px/1.5 ' + FONT + ';color:' + _MUT + ';'
            'margin-top:3px;">e.g. <a href="' + b + '/change/' + str(t["id"])
            + '" style="color:' + _ACCENT + ';text-decoration:none;">'
            + _esc(t["example"]) + '</a></div></td></tr>')
    big = ""
    if d.get("patterns"):
        big = (
            '<tr><td style="padding:8px 28px 0;">'
            '<div style="font:800 12px/1.4 ' + FONT + ';letter-spacing:.06em;'
            'text-transform:uppercase;color:' + _MUT + ';">'
            'What repeated, in pages</div>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            + rows_top + '</table></td></tr>')

    # No amber strip: the Not-live tile carries the health signal now, and
    # the owner asked the highlighted line to go.
    health = ""
    meta = ""
    file_link = (
        '<div style="padding-top:12px;"><a href="' + b + '/assets" '
        'style="font:600 13px/1 ' + FONT + ';color:' + _ACCENT + ';'
        'text-decoration:none;">See the document updates \u2192</a></div>') if fu else ""

    return (
'<!doctype html><html><head><meta charset="utf-8">'
'<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
'<body style="margin:0;padding:0;background:' + _PAPER + ';">'
'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
'style="background:' + _PAPER + ';padding:24px 12px;"><tr><td align="center">'
'<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
'style="max-width:600px;width:100%;background:#ffffff;border:1px solid ' + _LINE + ';'
'border-radius:16px;overflow:hidden;">'

'<tr><td style="background:' + _INK + ';padding:18px 28px;">'
'<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
'<td style="padding-right:11px;"><img src="cid:iradarlogo" width="32" height="32" '
'alt="iRadar" style="display:block;border-radius:8px;"></td>'
'<td style="font:800 20px/1 ' + FONT + ';letter-spacing:-.02em;">'
'<span style="color:#ffffff;">i</span><span style="color:#8b5cf6;">Radar</span>'
'<div style="font:400 12px/1.4 ' + FONT + ';color:#b9bdf0;margin-top:4px;'
'letter-spacing:0;font-weight:400;">Weekly change check \u00b7 '
+ _esc(when_label) + '</div>'
'</td></tr></table></td></tr>'

'<tr><td style="padding:12px 18px 4px;">'
'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
'style="background:' + _PAPER + ';border-radius:12px;"><tr>' + stats + '</tr></table>'
'</td></tr>'

+ chart
+ newcall
+ big
+ health

+ '<tr><td align="center" style="padding:24px 28px 10px;">'
'<a href="' + dash + '" style="display:inline-block;background:' + _ACCENT + ';'
'color:#ffffff;font:700 14px/1 ' + FONT + ';text-decoration:none;padding:14px 30px;'
'border-radius:9px;">Review the queue \u2192</a>' + file_link + '</td></tr>'

+ (('<tr><td align="center" style="padding:8px 28px 4px;">'
    '<div style="font:400 11px/1.5 ' + FONT + ';color:' + _FAINT + ';">'
    + meta + '</div></td></tr>') if meta else '')
+ '<tr><td align="center" style="padding:14px 28px 22px;border-top:1px solid '
+ _LINE + ';">'
'<div style="font:400 11px/1.6 ' + FONT + ';color:' + _FAINT + ';">'
'Email alerts are on for your account \u00b7 '
'<a href="' + b + '/account" style="color:' + _ACCENT + ';text-decoration:none;">'
'Manage them here</a></div></td></tr>'

'</table></td></tr></table></body></html>')
