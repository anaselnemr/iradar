"""InfineonWatch - website change monitor.

Run:  python app.py     then open http://127.0.0.1:5000
"""
import io
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as urlquote, urlsplit

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import (Flask, Response, abort, flash, jsonify, redirect,
                   render_template, request, send_file, url_for)
from rapidfuzz import fuzz, process

import compare as cmp
import db
import runner

app = Flask(__name__)
app.secret_key = "infineonwatch-local-only"
# Re-read templates from disk on every render, so tweaks to the HTML/CSS in
# templates/ take effect on a browser refresh WITHOUT restarting the server
# (a restart would interrupt an in-process capture run). Negligible overhead.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# Optional HTTP Basic Auth for when the dashboard is exposed through a public
# tunnel. Credentials come from the environment (DASH_USER / DASH_PASS) so they
# never live in the source or git history; when unset, auth is OFF and local
# use stays friction-free.
DASH_USER = os.environ.get("DASH_USER")
DASH_PASS = os.environ.get("DASH_PASS")


@app.before_request
def _require_login():
    if not DASH_USER:
        return  # no credentials configured -> open (local-only default)
    auth = request.authorization
    if not auth or auth.username != DASH_USER or auth.password != DASH_PASS:
        return Response(
            "InfineonWatch — sign in required.", 401,
            {"WWW-Authenticate": 'Basic realm="InfineonWatch", charset="UTF-8"'})


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


@app.template_filter("localtime")
def localtime_filter(s):
    """DB timestamps are UTC; show them in the user's local time."""
    if not s:
        return "—"
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).astimezone().strftime("%d %b %Y, %H:%M")
    except (ValueError, TypeError):
        return s

scheduler = BackgroundScheduler()

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------
def reschedule():
    s = db.get_settings()
    freq = s.get("frequency", "weekly")
    hour = int(s.get("hour", 6))
    weekday = s.get("weekday", "mon")
    if weekday not in WEEKDAYS:
        weekday = "mon"

    if freq == "daily":
        trigger = CronTrigger(hour=hour, minute=0)
    elif freq == "bidaily":
        trigger = CronTrigger(day="*/2", hour=hour, minute=0)
    else:  # weekly
        trigger = CronTrigger(day_of_week=weekday, hour=hour, minute=0)

    scheduler.add_job(_scheduled_check, trigger, id="check",
                      replace_existing=True, misfire_grace_time=3600 * 6)


def _scheduled_check(_retry=0):
    """The cron job, with a retry: run_check returns None when another run
    already holds the lock, and without a retry that whole scheduled check
    would silently vanish (on weekly cadence, a lost week). Re-try every
    15 minutes for up to 12 hours, then give up loudly."""
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


def next_run_text():
    job = scheduler.get_job("check")
    if job and job.next_run_time:
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
PER_PAGE = 100

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
# shape, for quick QA of the whole extractor from one filter. Temporary - drop
# the entry to remove it.
TEST_PAGE_IDS = (283, 144, 167, 92, 97, 155, 1, 3, 5, 1078, 1079, 1608, 1610,
                 4177, 4180, 4203, 4220, 526, 574, 221, 264, 238, 4167, 239,
                 269, 244, 278, 298, 535, 193, 194, 4233)

STATUS_FILTERS = {
    "test":      ("🧪 Test set",
                  "u.id IN (" + ",".join(map(str, TEST_PAGE_IDS)) + ")"),
    "changed":   ("Updated", "u.last_status='changed' AND u.active=1"),
    "rejected":  ("Awaiting fix", "u.last_status='rejected'"),
    "unchanged": ("Unchanged", "u.last_status='unchanged' AND u.active=1"),
    "notlive":   ("Not live (404)", "u.last_status='notlive'"),
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
]
# section-content we already index. The nav-only components (Product details,
# Diagrams, ...) join this group from urls.components once a re-capture fills it.
_COMPONENT_ATTRS = [
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
                 "u.last_http IS NOT NULL AND u.last_http NOT IN (404, 410)"
                 " AND IFNULL(u.last_status,'') NOT IN ('blank', 'error')"),
    "notlive":  ("Not live (404)",
                 "u.last_http IN (404, 410)"),
}
for _stem, _cat, _label in _CONTENT_ATTRS + _COMPONENT_ATTRS:
    ATTR_FILTERS["has" + _stem] = ("Has " + _label, _has(_cat))
    ATTR_FILTERS["no" + _stem] = ("No " + _label, _nohas(_cat))
# a video is a video wherever it appears - a video card inside News &
# highlights counts too ("if a video is in the news it should also count").
_VIDEO_SQL = ("EXISTS(SELECT 1 FROM items i WHERE i.url_id=u.id"
              " AND i.category IN ('videos','news'))")
ATTR_FILTERS["hasvideos"] = ("Has videos", _VIDEO_SQL)
ATTR_FILTERS["novideos"] = ("No videos",
                            _KNOWN_GOOD + " AND NOT " + _VIDEO_SQL)
# price is captured text on component pages; "no price" only means something
# once the page has been through component detection ('' = not measured yet)
ATTR_FILTERS["hasprice"] = ("Has price",
                            "u.price IS NOT NULL AND u.price<>''")
ATTR_FILTERS["noprice"] = ("No price",
                           _KNOWN_GOOD +
                           " AND u.components IS NOT NULL AND u.components<>''"
                           " AND IFNULL(u.price,'')=''")
# presence-only blocks (content itself never stored) - each with its inverse
ATTR_FILTERS["hastable"] = ("Has products table",
                            _flag("product_table", "exists"))
ATTR_FILTERS["notable"] = ("No products table",
                           _noflag("product_table", "exists"))
ATTR_FILTERS["hastablerows"] = ("Products table has rows",
                                _flag("product_table", "has_content"))
ATTR_FILTERS["hascommunity"] = ("Has developer community",
                                _flag("developer_community", "exists"))
ATTR_FILTERS["nocommunity"] = ("No developer community",
                               _noflag("developer_community", "exists"))
ATTR_FILTERS["hasthreads"] = ("Developer community has threads",
                              _flag("developer_community", "has_content"))
ATTR_FILTERS["hashero"] = ("Has hero image", _flag("hero_image", "exists"))
ATTR_FILTERS["nohero"] = ("No hero image", _noflag("hero_image", "exists"))
ATTR_FILTERS["herodefault"] = (
    "Hero image is the default",
    "(" + _flag("hero_image", "exists") + " AND NOT "
    + _flag("hero_image", "has_content") + ")")

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

# the two dropdown groups, in display order (Has + No shown together per row).
# Page-state (Captured/Live/Not live) stays at the top of Content, as before.
CONTENT_KEYS = ["checked", "live", "notlive"]
for _stem, _c, _l in _CONTENT_ATTRS:
    CONTENT_KEYS += ["has" + _stem, "no" + _stem]
CONTENT_KEYS += ["hasprice", "noprice"]
COMPONENT_KEYS = []
for _stem, _c, _l in _COMPONENT_ATTRS:
    COMPONENT_KEYS += ["has" + _stem, "no" + _stem]
COMPONENT_KEYS += ["hastable", "notable", "hastablerows",
                   "hascommunity", "nocommunity", "hasthreads",
                   "hashero", "nohero", "herodefault"]
for _stem, _col, _lbl, _disp in _NAV_FILTER_ATTRS:
    COMPONENT_KEYS += ["has" + _stem, "no" + _stem]

# The canonical component registry - the ONE list of trackable components and,
# for each, how we know it is present on a page:
#   "cat"  -> the page has library items in this category
#   "flag" -> urls.flags[block].exists is true
#   "nav"  -> this label appears in the page's own section-nav (urls.components)
#   "tab"  -> this label appears in the Product-details sub-tabs (urls.detail_tabs)
# Body-section components (cat/flag) cover every page type; the nav-only ones
# are the structural sections PTP/EB pages expose that are not library content.
#   key, label, kind, arg
COMPONENTS = [
    ("overview",           "Overview",              "cat",  "overview"),
    ("about",              "About",                 "cat",  "about"),
    ("documents",          "Documents",             "cat",  "documents"),
    ("design_resources",   "Design resources",      "cat",  "design_resources"),
    ("features",           "Key features",          "cat",  "features"),
    ("benefits",           "Benefits",              "cat",  "benefits"),
    ("applications",       "Applications",          "cat",  "applications"),
    ("categories",         "Browse by category",    "cat",  "categories"),
    ("products",           "Related products",      "cat",  "products"),
    ("news",               "News & highlights",     "cat",  "news"),
    ("videos",             "Videos",                "cat",  "videos"),
    ("product_table",      "Products table",        "flag", "product_table"),
    ("developer_community","Developer community",   "flag", "developer_community"),
    ("hero_image",         "Hero image",            "flag", "hero_image"),
    ("buying_options",     "View buying options",   "nav",  "view buying options"),
    ("product_details",    "Product details",       "nav",  "product details"),
    # PTP pages label it "Diagrams", application pages "Diagram" - one component
    ("diagrams",           "Diagram",               "nav",
     ("diagram", "diagrams", "block diagram", "block diagrams")),
    ("featured_products",  "Featured products",     "nav",  "featured products"),
    ("partner_solutions",  "Partner solutions",     "nav",  "partner solutions"),
    ("register_product",   "Register product",      "nav",  "register product"),
    ("view_products",      "View products",         "nav",  "view products"),
    ("bom",                "Bill of material",      "nav",  "bill of material"),
    ("parametrics",        "Parametrics",           "tab",  "parametrics"),
    ("ordering",           "Ordering",              "tab",  "ordering"),
    ("description",        "Description",           "tab",  "description"),
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


def _page_component_set(components_json, detail_tabs_json, flags, cat_present):
    """The set of component keys PRESENT on one page, from its four signals:
    captured categories (a set of category names), the Product-details sub-tabs,
    flags dict, and the nav labels stored in urls.components (JSON)."""
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

    have = set()
    for key, _label, kind, arg in _all_components():
        if kind == "cat" and arg in cat_present:
            have.add(key)
        elif kind == "flag" and (flags.get(arg) or {}).get("exists"):
            have.add(key)
        elif kind == "nav" and _in(arg, nav):
            have.add(key)
        elif kind == "tab" and _in(arg, tabs):
            have.add(key)
    return have


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


def _url_filter(q, statuses, ptypes, attrs):
    """statuses OR'd, types OR'd, attributes AND'd - all multi-select.

    EVERY condition is wrapped in its own parentheses before being combined.
    Several filter conditions contain a top-level OR (e.g. "Not live" is
    404/500 OR blank), and AND binds tighter than OR in SQL, so an unwrapped
    `A AND B OR C` silently became `(A AND B) OR C` - which made combining any
    such filter with another quietly drop every other filter. That is the
    "multiple filters not working" the user saw.
    """
    where, params = [], []
    if q:
        where.append("(u.url LIKE ? OR u.label LIKE ?"
                     " OR u.canonical_url LIKE ?)")
        params += [f"%{q}%"] * 3
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
    for a in attrs:
        if a in ATTR_FILTERS:
            where.append("(" + ATTR_FILTERS[a][1] + ")")
    return ("WHERE " + " AND ".join(where)) if where else "", params


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    statuses = [s for s in request.args.getlist("status") if s in STATUS_FILTERS]
    sel_types = request.args.getlist("ptype")
    attrs = [a for a in request.args.getlist("attr") if a in ATTR_FILTERS]
    sort = request.args.get("sort", "page")
    # Sort by page ADDRESS, not title: the URL is the site's own path, so
    # alphabetical-by-URL keeps the hierarchy grouped ("…/microcontroller/…"
    # rows sit together) AND a newly added page lands in place immediately -
    # its URL is known the moment it is added, long before it has a title.
    _chg = "(SELECT COUNT(*) FROM changes c WHERE c.url_id=u.id)"
    order_by = {
        "page": "u.url COLLATE NOCASE ASC",
        "page_desc": "u.url COLLATE NOCASE DESC",
        "date": "u.last_checked IS NULL, u.last_checked DESC",
        "date_asc": "u.last_checked IS NULL, u.last_checked ASC",
        "changes": _chg + " DESC",          # most-changed first
        "changes_asc": _chg + " ASC",       # least-changed first
    }.get(sort, "u.url COLLATE NOCASE ASC")
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

    wsql, params = _url_filter(q, statuses, sel_types, attrs)

    con = db.connect()
    total_match = con.execute(
        f"SELECT COUNT(*) FROM urls u {wsql}", params).fetchone()[0]
    pages = max(1, -(-total_match // PER_PAGE))
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
          (SELECT s.page_title FROM snapshots s WHERE s.url_id=u.id
            AND s.status='ok' AND s.page_title<>'' ORDER BY s.id DESC LIMIT 1)
            AS page_title
        FROM urls u {wsql}
        ORDER BY {order_by}, u.id
        LIMIT ? OFFSET ?
    """, params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()

    counts = con.execute("""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN last_status='changed' AND active=1 THEN 1 ELSE 0 END) AS changed,
          SUM(CASE WHEN last_status='unchanged' AND active=1 THEN 1 ELSE 0 END) AS unchanged,
          SUM(CASE WHEN last_http IS NOT NULL AND last_http NOT IN (404, 410)
                   AND IFNULL(last_status,'') NOT IN ('blank','error')
                   THEN 1 ELSE 0 END) AS live,
          SUM(CASE WHEN last_checked IS NOT NULL THEN 1 ELSE 0 END) AS checked,
          SUM(CASE WHEN last_status='rejected' THEN 1 ELSE 0 END) AS rejected,
          SUM(CASE WHEN last_status='error' THEN 1 ELSE 0 END) AS errors,
          SUM(CASE WHEN active=0 THEN 1 ELSE 0 END) AS paused,
          SUM(CASE WHEN last_checked IS NULL THEN 1 ELSE 0 END) AS fresh,
          SUM(CASE WHEN last_status='blank' THEN 1 ELSE 0 END) AS blank,
          -- not live = the server says GONE (404/410). Blank bodies and
          -- blocked captures (403/429/5xx) are their own states, never this
          SUM(CASE WHEN last_http IN (404, 410) THEN 1 ELSE 0 END) AS notlive
        FROM urls""").fetchone()
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
    # how many captured pages carry each category - one GROUP BY, indexed
    cat_pages = {r["category"]: r["n"] for r in con.execute(
        "SELECT category, COUNT(DISTINCT url_id) n FROM items GROUP BY category")}
    # pages with a video anywhere (standalone OR a News & highlights card) -
    # counted distinctly so a page with both is not double-counted
    vid_pages = con.execute(
        "SELECT COUNT(DISTINCT url_id) n FROM items"
        " WHERE category IN ('videos','news')").fetchone()["n"]
    # status + urls-only attr filters: one bulk pass each
    status_counts = _bulk_counts(con, STATUS_FILTERS)
    attr_counts = _bulk_counts(con, ATTR_FILTERS)
    con.close()
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
    quick_chips = [c for c in (
        {"kind": "status", "vals": ["test"], "label": "🧪 Test set",
         "n": len(TEST_PAGE_IDS), "always": True},
        {"kind": "status", "vals": ["changed"], "label": "Updated",
         "n": status_counts["changed"], "always": True, "tone": "warn"},
        {"kind": "status", "vals": ["rejected"], "label": "Awaiting fix",
         "n": status_counts["rejected"], "tone": "warn"},
        {"kind": "status", "vals": ["notlive"], "label": "Not live",
         "n": notlive_total, "tone": "bad"},
        {"kind": "status", "vals": ["blank"], "label": "Blank page",
         "n": status_counts["blank"], "tone": "bad"},
        {"kind": "status", "vals": ["error"], "label": "Capture error",
         "n": status_counts["error"], "tone": "bad"},
        {"kind": "attr", "vals": ["live"], "label": "Live",
         "n": attr_counts["live"], "tone": "good"},
        {"kind": "status", "vals": ["unchanged"], "label": "Unchanged",
         "n": status_counts["unchanged"], "always": True},
        {"kind": "status", "vals": ["new"], "label": "Not captured",
         "n": status_counts["new"]},
        {"kind": "status", "vals": ["paused"], "label": "Paused",
         "n": status_counts["paused"]},
    ) if c["n"] or c.get("always")]

    return render_template("index.html", urls=urls, state=runner.RUN_STATE,
                           last_run=last_run, total_open=total_open,
                           next_run=next_run_text(), settings=db.get_settings(),
                           q=q, page=page, pages=pages, sort=sort,
                           new_cutoff=new_cutoff, bulk_days=bulk_days, ranks=ranks,
                           notlive_total=notlive_total, comp_pending=comp_pending,
                           pending_total=pending_total,
                           level_names=LEVEL_NAMES,
                           total_match=total_match, counts=counts,
                           per_page=PER_PAGE, ptypes=ptypes,
                           sel_status=statuses, sel_types=sel_types,
                           sel_attrs=attrs, status_filters=STATUS_FILTERS,
                           attr_filters=ATTR_FILTERS,
                           content_keys=CONTENT_KEYS, component_keys=COMPONENT_KEYS,
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
                                   request.form.getlist("attr"))
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
        flash(f"Approved changes on {len(ids)} URL(s) — marked unchanged.")
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
SECTION_LABELS = [
    ("overview", "Overview"),
    ("categories", "Browse by category"),
    ("products", "Related products"),
    ("documents", "Documents"),
    ("design_resources", "Design resources & boards"),
    ("features", "Key features"),
    ("benefits", "Benefits"),
    ("applications", "Applications"),
    ("trainings", "Trainings & webinars"),
    ("videos", "Videos"),
    ("news", "News & highlights"),
    ("partners", "Partner solutions"),
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
    r"""A news/trainings item that is actually a video: a media URL or a
    duration (\d:\d\d) in its meta."""
    u = (d.get("url") or "").lower()
    return (".mp4" in u or "/is/content/" in u
            or bool(re.search(r"\d{1,2}:\d{2}", d.get("meta") or "")))


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
    ("boards",   "Boards",
     lambda d: d["cat"] == "design_resources"
     and "board" in (d.get("grp") or "").lower()),
    ("design",   "Design resources",
     lambda d: d["cat"] == "design_resources"),
    ("products", "Related products", lambda d: d["cat"] == "products"),
    ("text",     "In page text", None),
]
PILL_FN = {k: f for k, _l, f in SEARCH_PILLS}
PILL_LABELS = {k: l for k, l, _f in SEARCH_PILLS}
# categories each pill can live in - lets the SQL fallback prefilter
PILL_SQL_CATS = {
    "design": ("design_resources",), "docs": ("documents", "design_resources"),
    "images": ("images",), "products": ("products",),
    "boards": ("design_resources",),
    "videos": ("videos", "news", "trainings"),
}
# page-section PROSE (key features, benefits, applications) is no content
# type: it lives in the page's text, and the In-page-text search names the
# section a match sits in. Keeping them as asset chips doubled every answer.
PROSE_CATS = ("features", "benefits", "applications")
# old links used raw category names - keep them working
_LEGACY_PILL = {"documents": "docs", "design_resources": "design",
                "news": "videos", "trainings": "videos",
                "features": "text", "benefits": "text",
                "applications": "text"}


def _norm_pill(cat):
    cat = (cat or "all").strip()
    if cat in PILL_FN:
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
    # ignore the trailing #anchor our own tab-clicking leaves behind
    strip = lambda u: u.split("#")[0].rstrip("/")
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
    """Added/removed items per category between two snapshots' libraries."""
    o = _load_sections(old_dir) or {}
    n = _load_sections(new_dir) or {}
    key = lambda i: (i.get("title", ""), i.get("url", ""))
    diff = {}
    for cat, label in SECTION_LABELS:
        ok = {key(i) for i in o.get(cat, [])}
        nk = {key(i) for i in n.get(cat, [])}
        added = [i for i in n.get(cat, []) if key(i) not in ok]
        removed = [i for i in o.get(cat, []) if key(i) not in nk]
        if added or removed:
            diff[label] = {"added": added, "removed": removed}
    return diff


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
                _seen_img, _deduped = set(), []
                for i in items:
                    if not isinstance(i, dict):
                        continue
                    canon = (i.get("url", "") or "").split("?")[0].split(":asset")[0]
                    if canon in _seen_img:
                        continue
                    _seen_img.add(canon)
                    i["url"] = canon
                    i["title"] = pretty_image_title(i.get("title", ""), canon)
                    _deduped.append(i)
                items = _deduped
            # once every page of this type is captured, a category that is
            # empty across ALL of them does not belong to this page shape
            if not items and present is not None and cat not in present:
                continue
            for it in items:
                if isinstance(it, dict):
                    it["item_key"] = runner.item_key(cat, it.get("title", ""),
                                                     it.get("url", ""))
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
    have = _page_component_set(u["components"], u["detail_tabs"], flags,
                               this_cats)
    con5 = db.connect()
    etype = _etype(u["ptype"] or "", u["url"], u["level"])
    type_comps = _effective_matrix(con5).get(etype, set())
    con5.close()
    # hero_image rides on every page ("almost all should have it") even where
    # its shape has no measurement yet - the chip then says so instead of lying
    comp_rows = [{"key": k, "label": lbl, "exists": k in have}
                 for k, lbl, _kind, _arg in _all_components()
                 if k in type_comps or k in have or k == "hero_image"]

    return render_template("url_detail.html", u=u, snaps=snaps, changes=changes,
                           latest=latest, library=library, cat_tree=cat_tree,
                           redirect_to=redirect_to, flags=flags,
                           components=comp_rows, price=(u["price"] or ""),
                           settings=_settings_now, level_names=LEVEL_NAMES)


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

    hunks, img_added, img_removed = [], [], []
    if old_snap and new_snap and old_snap["dir"] and new_snap["dir"]:
        s = db.get_settings()
        ignore = (s.get("ignore_patterns") or "").splitlines()
        ignore_img = (s.get("ignore_image_patterns") or "").splitlines()
        _, _, _, hunks = cmp.text_diff(old_snap["dir"], new_snap["dir"], ignore)
        _, img_added, img_removed = cmp.images_diff(old_snap["dir"],
                                                    new_snap["dir"], ignore_img)

    art = {"boxed": False, "regions": [], "legacy": False}
    if c["dir"] and Path(c["dir"]).exists():
        d = Path(c["dir"])
        art["boxed"] = (d / "old_boxed.png").exists() and (d / "new_boxed.png").exists()
        art["regions"] = sorted(p.name for p in d.glob("region_*.png"))
        art["legacy"] = (d / "visual_diff.png").exists()
    lib_diff = {}
    if old_snap and new_snap and old_snap["dir"] and new_snap["dir"]:
        try:
            lib_diff = _sections_diff(old_snap["dir"], new_snap["dir"])
        except Exception:
            lib_diff = {}
    return render_template("change_view.html", c=c, u=u,
                           old_snap=old_snap, new_snap=new_snap, hunks=hunks,
                           img_added=img_added, img_removed=img_removed,
                           art=art, lib_diff=lib_diff)


ART_NAME_RE = re.compile(r"^[a-z0-9_]+\.png$")


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
    return send_file(p, mimetype="image/png")


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
    if resolution == "reject":
        # The OLD version was correct: never compare against the rejected
        # capture - the old snapshot stays the current version, so the page
        # flagged again on every check until the website is fixed.
        con.execute("UPDATE snapshots SET rejected=1 WHERE id=?",
                    (c["new_snapshot_id"],))
        con.execute("UPDATE urls SET last_status='rejected' WHERE id=?",
                    (c["url_id"],))
        # The library/search index was already refreshed from the new capture
        # AT CAPTURE TIME - leaving it would show the very content the reject
        # just declared wrong. Restore items + full-text from the snapshot the
        # reject says is the truth.
        old = con.execute("SELECT dir FROM snapshots WHERE id=?",
                          (c["old_snapshot_id"],)).fetchone()
        if old and old["dir"] and Path(old["dir"]).exists():
            runner.index_items(con, c["url_id"], old["dir"])
        flash("Rejected — the old version stays the current version (library "
              "and search restored to it). The page will be flagged again on "
              "the next check until the website matches it.")
    else:
        remaining = con.execute(
            "SELECT COUNT(*) FROM changes WHERE url_id=? AND reviewed=0",
            (c["url_id"],)).fetchone()[0]
        if remaining == 0:
            con.execute("UPDATE urls SET last_status='unchanged'"
                        " WHERE id=? AND last_status IN ('changed','rejected')",
                        (c["url_id"],))
        flash("Approved — this is now the current version.")
    con.commit()
    con.close()
    nxt = request.form.get("next") or url_for("index")
    return redirect(nxt)


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
    p = Path(s["dir"]) / "shot.png"
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="image/png")


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
    if runner.run_check_background(kind="manual", url_ids=ids):
        flash(f"Checking {len(ids)} pending page(s)…" if ids
              else "Check started - progress is shown below.")
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
    if s.get("running") and s.get("started"):
        # how many are actually committed to the database so far
        con = db.connect()
        s["saved"] = con.execute(
            "SELECT COUNT(*) FROM urls WHERE last_checked >= ?",
            (s["started"],)).fetchone()[0]
        con.close()
    if s.get("running") and s.get("done", 0) >= 3 and s.get("started_ts"):
        elapsed = _time.time() - s["started_ts"]
        if elapsed > 0:
            rate = s["done"] / elapsed
            # each parked suspect owes ONE more capture in the verify pass -
            # count that debt now, or the ETA quietly ignores the whole tail
            # and "time doesn't move" once verification starts
            remaining = (max(0, s.get("total", 0) - s["done"])
                         + s.get("suspects", 0))
            s["eta_s"] = int(remaining / rate) if rate > 0 else None
    return jsonify(s)


@app.route("/settings", methods=["GET", "POST"])
def settings_view():
    if request.method == "POST":
        for key in ("frequency", "weekday", "hour", "concurrency",
                    "visual_threshold", "height_threshold", "ignore_patterns",
                    "ignore_image_patterns", "dam_search_url",
                    "windchill_search_url", "aem_author_url"):
            if key in request.form:
                db.set_setting(key, request.form[key].strip())
        reschedule()
        flash("Settings saved. Next automatic check: " + next_run_text())
        return redirect(url_for("settings_view"))
    return render_template("settings.html", s=db.get_settings(),
                           next_run=next_run_text(), weekdays=WEEKDAYS)


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
    for r in con.execute("SELECT url, canonical_url FROM urls WHERE active=1"):
        for u in (r["url"], r["canonical_url"]):
            n = _tax_norm(u)
            if n.startswith("/products/"):
                reg.add(_apply_aliases(n, aliases))
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
    for r in con.execute("SELECT id, url, canonical_url, ptype, level,"
                         " level_src FROM urls WHERE active=1"
                         " AND ptype IN ('Product','Design resources')"):
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


def _post_run_watcher():
    """After each capture run finishes, auto-infer the levels of any new
    Product/Application pages - so a freshly-added or newly-reached page gets
    its PC/PFP/ADP/... shape without waiting for a manual click."""
    prev = False
    while True:
        time.sleep(30)
        running = runner.RUN_STATE.get("running", False)
        if prev and not running:
            try:
                # structure first (deterministic, from the site's own walk),
                # inference after for what structure cannot place (apps/parts)
                chg, fill, _kept = _rebuild_structure()
                if chg or fill:
                    print(f"[post-run] structure: {len(chg)} corrected,"
                          f" {len(fill)} filled", flush=True)
                done, _ = _infer_missing_levels()
                if done:
                    print(f"[post-run] inferred {len(done)} level(s)", flush=True)
                joined = _matrix_autojoin()
                if joined:
                    print(f"[post-run] matrix learned {len(joined)} new"
                          f" component slot(s): {', '.join(joined[:6])}"
                          + ("…" if len(joined) > 6 else ""), flush=True)
            except Exception:
                pass
        prev = running


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
            flash(("Pruned " + str(len(removed)) + " never-seen pairing(s): "
                   + ", ".join(removed[:12])
                   + ("…" if len(removed) > 12 else "")) if removed
                  else "Nothing to prune — every expected component appears somewhere.")
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
        conds, params = [], []
        for t in toks:
            conds.append("(lower(i.title) LIKE ? OR lower(i.meta) LIKE ?"
                         " OR lower(i.item_url) LIKE ?)")
            params += [f"%{t}%"] * 3
        catf, catp = "", []
        pill_cats = PILL_SQL_CATS.get(_norm_pill(cat))
        if pill_cats:
            catf = (" AND i.category IN ("
                    + ",".join("?" * len(pill_cats)) + ")")
            catp = list(pill_cats)
        for r in con.execute(
                f"SELECT DISTINCT i.item_key AS k FROM items i"
                f" WHERE i.category<>'about' AND i.item_key<>''"
                f" AND {' AND '.join(conds)}{catf} LIMIT 500", params + catp):
            if r["k"] not in seen:
                seen.add(r["k"])
                ordered.append(r["k"])
    return ordered[:limit]


BROWSE_PER_PAGE = 60

# browse-mode SQL per chip: plain, index-friendly filters (search mode keeps
# the richer per-item functions; browse trades the edge cases for pagination)
_BROWSE_SQL = {
    "docs":     "category='documents'",
    # mirrors _videolike: a news/trainings card that is actually a video -
    # a media URL or a duration in its meta
    "videos":   "(category='videos' OR (category IN ('news','trainings')"
                " AND (lower(item_url) LIKE '%.mp4%'"
                "  OR item_url LIKE '%/is/content/%'"
                "  OR meta GLOB '*[0-9]:[0-9][0-9]*')))",
    "images":   "category='images'",
    "design":   "category='design_resources'",
    "boards":   "category='design_resources' AND lower(IFNULL(grp,''))"
                " LIKE '%board%'",
    "products": "category='products'",
}


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
    try:
        pageno = max(1, int(request.args.get("page", 1)))
    except Exception:
        pageno = 1
    groups, total, page_hits = [], 0, []
    browse, browse_total = False, 0
    con = db.connect()
    # 'about' is tracked for text comparison only - never surfaced. 'overview'
    # is a page's own text, not an asset type. Prose sections live under text.
    indexed = con.execute(
        "SELECT COUNT(*) FROM items WHERE category NOT IN"
        " ('about','overview','features','benefits','applications')"
    ).fetchone()[0]
    # live counts for every chip - the "all of it in front of me" numbers
    ccounts = {"pages": con.execute(
        "SELECT COUNT(*) FROM urls WHERE active=1").fetchone()[0]}
    for key, wsql in _BROWSE_SQL.items():
        ccounts[key] = con.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM items WHERE " + wsql +
            " GROUP BY lower(title), item_url)").fetchone()[0]
    ccounts["all"] = indexed
    ccounts["text"] = con.execute(
        "SELECT COUNT(*) FROM page_fts").fetchone()[0]

    # ---- BROWSE mode: no query -> the library itself, paginated ----------
    if not q and cat == "pages":
        browse = True
        browse_total = ccounts["pages"]
        prows = con.execute(
            "SELECT id, url, level, label,"
            " COALESCE(NULLIF(title_override,''),"
            "  (SELECT s.page_title FROM snapshots s WHERE s.url_id=urls.id"
            "    AND s.status='ok' AND s.page_title<>''"
            "    ORDER BY s.id DESC LIMIT 1)) AS page_title"
            " FROM urls WHERE active=1"
            " ORDER BY lower(COALESCE(NULLIF(title_override,''), label, url))"
            " LIMIT ? OFFSET ?",
            (BROWSE_PER_PAGE, (pageno - 1) * BROWSE_PER_PAGE)).fetchall()
        page_hits = [{"id": r["id"], "url": r["url"],
                      "level": r["level"] or "",
                      "title": (r["page_title"] or r["label"]
                                or r["url"].rstrip("/").rsplit("/", 1)[-1])}
                     for r in prows]
        total = browse_total
    elif not q and cat in _BROWSE_SQL:
        browse = True
        browse_total = ccounts[cat]
        _sset = db.get_settings()
        rows = con.execute(
            "SELECT title, item_url, category, grp, meta,"
            " MIN(item_key) AS item_key, MIN(doc_type) AS doc_type,"
            " COUNT(DISTINCT url_id) AS npages"
            " FROM items WHERE " + _BROWSE_SQL[cat] +
            " GROUP BY lower(title), item_url"
            " ORDER BY lower(title) LIMIT ? OFFSET ?",
            (BROWSE_PER_PAGE, (pageno - 1) * BROWSE_PER_PAGE)).fetchall()
        for r in rows:
            _bk = backend_system(r["category"], r["meta"], r["doc_type"],
                                 r["grp"], r["item_url"])
            _ttl = (pretty_image_title(r["title"], r["item_url"])
                    if r["category"] == "images" else r["title"])
            groups.append({"title": _ttl, "url": r["item_url"],
                           "category": r["category"], "grp": r["grp"],
                           "meta": r["meta"], "item_key": r["item_key"],
                           "npages": r["npages"], "pages": [],
                           "backend": _bk,
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
            "    ORDER BY s.id DESC LIMIT 1)) AS page_title FROM urls").fetchall()
        pdata = [{"id": r["id"], "url": r["url"], "level": r["level"] or "",
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
    if q and cat == "text":
        # look INSIDE the pages: FTS over each page's full captured text, and
        # then say WHICH COMPONENT the words live in by checking the page's
        # own indexed items for the same tokens
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
                "                      'overview','about')"
                " AND " + conds + " LIMIT 300", toks_all).fetchall()
        _PLBL = {"features": "Key features", "benefits": "Benefits",
                 "applications": "Applications", "overview": "Overview",
                 "about": "About"}

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
        total = len(text_hits)
    if q and cat not in ("pages", "text"):
        keys = _search_keys(con, q, cat, 250)
        if keys:
            marks = ",".join("?" * len(keys))
            rows = con.execute(f"""
                SELECT i.category, i.grp, i.title, i.item_url, i.meta, i.url_id,
                       i.item_key, i.doc_type, u.url AS page_url, u.label,
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
                               "category": r["category"], "grp": r["grp"],
                               "meta": r["meta"], "pages": [],
                               "item_key": r["item_key"],
                               "rank": rank.get(r["item_key"], 10 ** 9),
                               "backend": _bk,
                               "is_video": is_video_item(r["category"],
                                                         r["item_url"],
                                                         r["meta"]),
                               "aname": asset_name(r["category"],
                                                   r["item_url"]),
                               "bsearch": backend_search_link(
                                   _bk, asset_name(r["category"],
                                                   r["item_url"]), _sset)}
                order.append(gk)
            else:
                by_item[gk]["rank"] = min(by_item[gk]["rank"],
                                          rank.get(r["item_key"], 10 ** 9))
            by_item[gk]["pages"].append(
                {"id": r["url_id"], "url": r["page_url"],
                 "title": r["page_title"] or r["label"] or r["page_url"]})
        groups = [by_item[k] for k in order]
        groups.sort(key=lambda g: (g["rank"], -len(g["pages"])))
        total = sum(len(g["pages"]) for g in groups)
    con.close()
    has_more = browse and pageno * BROWSE_PER_PAGE < browse_total
    return render_template("search.html", q=q, cat=cat,
                           pills=[(k, l) for k, l, _f in SEARCH_PILLS],
                           pill_labels=PILL_LABELS, page_hits=page_hits,
                           text_hits=text_hits,
                           groups=groups, total=total, indexed=indexed,
                           labels=dict(SECTION_LABELS),
                           ccounts=ccounts, browse=browse, pageno=pageno,
                           has_more=has_more, per_page=BROWSE_PER_PAGE)


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
                       exclude={"overview", "features", "benefits",
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


def _pdf_first_png(pdf_bytes, out_path):
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(pdf_bytes)
    pil = pdf[0].render(scale=1.6).to_pil()
    pil.thumbnail((1100, 1400))
    pil.save(out_path, "PNG")
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
    return cache / (stem + ".png"), cache / (stem + ".fail")


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
        return send_file(out, mimetype="image/png")
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
    except Exception:
        pass
    if out.exists():
        return send_file(out, mimetype="image/png")
    abort(404)


@app.route("/item/<key>")
def item_detail(key):
    """One real-world item - a document, video, image, board - and every page
    it is assigned to. The same datasheet can sit on forty product pages; this
    is the view that answers "where does this thing appear?"."""
    con = db.connect()
    rows = con.execute("""
        SELECT i.*, u.url AS page_url, u.ptype, u.label, u.last_status,
               (SELECT s.page_title FROM snapshots s WHERE s.url_id=u.id
                 AND s.status='ok' AND s.page_title<>''
                 ORDER BY s.id DESC LIMIT 1) AS page_title
        FROM items i JOIN urls u ON u.id=i.url_id
        WHERE i.item_key=? ORDER BY u.ptype, page_title""", (key,)).fetchall()
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
                                              first["category"])}
    pages = [{"id": r["url_id"], "url": r["page_url"],
              "title": r["page_title"] or r["label"] or "",
              "ptype": r["ptype"] or "", "grp": r["grp"] or "",
              "status": r["last_status"] or ""} for r in rows]
    by_type = {}
    for p in pages:
        by_type.setdefault(p["ptype"] or "Other", []).append(p)
    con.close()
    return render_template("item.html", item=item, pages=pages, item_key=key,
                           by_type=sorted(by_type.items()))


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
        all_levels |= {p.get("live_level") for p in pages
                       if p.get("live_level")}
        disagreements += sum(1 for p in pages if p.get("level_disagrees"))
        groups.append({"label": lbl, "tree": tree, "shown": len(pages),
                       "total": total})
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
    return render_template("hierarchy.html", groups=groups, sel=sel,
                           levels=seen_levels, level_names=LEVEL_NAMES,
                           disagreements=disagreements, level_log=level_log,
                           home=home)


@app.route("/export.xlsx")
def export_xlsx():
    """Excel export of ALL tracked URLs (never filtered/paged).

    mode=table  -> one sheet: the whole dashboard table + library counts
    mode=full   -> adds every extracted library item and all changes"""
    import tempfile
    from openpyxl import Workbook

    mode = request.args.get("mode", "full")
    wb = Workbook(write_only=True)
    ws_pages = wb.create_sheet("Pages")
    cat_keys = [c for c, _ in SECTION_LABELS if c != "images"]
    ws_pages.append(["ID", "URL", "Name", "Page type", "Active", "Status",
                     "HTTP", "Last checked (UTC)", "Text lines", "Image count"] +
                    [label for c, label in SECTION_LABELS if c != "images"] +
                    ["Overview text"])
    ws_items = ws_changes = None
    if mode == "full":
        ws_items = wb.create_sheet("Library items")
        ws_items.append(["URL ID", "URL", "Page type", "Category", "Group",
                         "Title", "Link", "Details"])
        ws_changes = wb.create_sheet("Changes")
        ws_changes.append(["Change ID", "URL", "When (UTC)", "Summary", "Verdict"])

    con = db.connect()
    label_by_cat = dict(SECTION_LABELS)
    for u in con.execute("SELECT * FROM urls ORDER BY id"):
        snap = con.execute(
            "SELECT * FROM snapshots WHERE url_id=? AND status='ok' AND dir<>''"
            " AND rejected=0 ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone()
        sections = _load_sections(snap["dir"]) if snap else None
        counts = []
        overview_text = ""
        if sections:
            for c in cat_keys:
                counts.append(len(sections.get(c, [])))
            ov = sections.get("overview") or []
            overview_text = ov[0].get("title", "") if ov else ""
            if ws_items is not None:
                for c in cat_keys:
                    if c == "overview":
                        continue
                    for it in sections.get(c, []):
                        ws_items.append([u["id"], u["url"], u["ptype"],
                                         label_by_cat[c], it.get("group", ""),
                                         it.get("title", ""), it.get("url", ""),
                                         it.get("meta", "")])
        else:
            counts = ["" for _ in cat_keys]
        ws_pages.append([u["id"], u["url"], u["label"], u["ptype"], u["active"],
                         u["last_status"] or "not checked", u["last_http"],
                         u["last_checked"],
                         snap["text_lines"] if snap else "",
                         snap["image_count"] if snap else ""] +
                        counts + [overview_text])
    if ws_changes is not None:
        for c in con.execute(
                "SELECT c.*, u.url AS u_url FROM changes c JOIN urls u ON u.id=c.url_id"
                " ORDER BY c.id"):
            verdict = ("to review" if not c["reviewed"] else
                       ("rejected" if c["resolution"] == "reject" else "approved"))
            ws_changes.append([c["id"], c["u_url"], c["ts"], c["summary"], verdict])
    con.close()

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    name = f"infineonwatch-{'table' if mode == 'table' else 'everything'}-{stamp}.xlsx"
    out = Path(tempfile.gettempdir()) / name
    wb.save(out)
    return send_file(out, as_attachment=True, download_name=name)


@app.route("/history")
def history():
    con = db.connect()
    runs = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 100").fetchall()
    con.close()
    return render_template("history.html", runs=runs)


@app.route("/prewarm", methods=["POST"])
def prewarm():
    if not PREWARM["running"]:
        threading.Thread(target=_prewarm_docs, daemon=True).start()
    return jsonify(PREWARM)


def _normalize_statuses():
    """Boot migration: the 'baseline' state is retired - a first capture now
    lands as 'unchanged' (next run either keeps it there or moves it to
    Updated). Rows written by an older runner normalize on every start."""
    con = db.connect()
    n = con.execute("UPDATE urls SET last_status='unchanged'"
                    " WHERE last_status='baseline'").rowcount
    con.commit()
    con.close()
    if n:
        print(f"  normalized {n} page(s) from the retired 'baseline' state")


def _normalize_urls():
    """Boot migration: strip tracking junk (?intc=…, utm_*, wcmmode) from
    tracked URLs. A redirect once glued a campaign tag onto its destination,
    and the tagged address slipped past the twin check - one page, tracked
    twice. If the cleaned address is already tracked, the junk row IS that
    page: its short-link origin is grafted onto the keeper and the row goes;
    otherwise the row is simply renamed to its clean address."""
    import shutil as _sh
    con = db.connect()
    for r in con.execute("SELECT id, url, redirect_from FROM urls"
                         " WHERE url LIKE '%?%'").fetchall():
        clean = db.strip_junk_params(r["url"])
        if clean == r["url"]:
            continue
        twin = con.execute("SELECT id, redirect_from FROM urls"
                           " WHERE rtrim(url,'/')=? AND id<>?",
                           (clean.rstrip("/"), r["id"])).fetchone()
        if twin:
            if r["redirect_from"] and not twin["redirect_from"]:
                con.execute("UPDATE urls SET redirect_from=? WHERE id=?",
                            (r["redirect_from"], twin["id"]))
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


if __name__ == "__main__":
    db.init_db()
    _normalize_statuses()
    _normalize_urls()
    _sweep_furniture()
    _sweep_images()
    scheduler.start()
    reschedule()
    # warm the document previews in the background so item pages open instantly
    threading.Thread(target=_prewarm_docs, daemon=True).start()
    # auto-infer product/application levels after every run
    threading.Thread(target=_post_run_watcher, daemon=True).start()
    print()
    print("  InfineonWatch is running:  http://127.0.0.1:5000")
    print("  From another device on the same network, use this PC's IP,")
    print("  e.g. http://192.168.x.x:5000 (see README).")
    print("  Next scheduled check:      " + next_run_text())
    print("  Keep this window open. Press Ctrl+C to stop.")
    print()
    # 0.0.0.0 = reachable from other devices on your local network too
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
