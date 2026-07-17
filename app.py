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
from urllib.parse import urlsplit

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


URL_RE = re.compile(r"https?://[^\s\"',;<>]+")


@app.template_filter("clean_title")
def clean_title_filter(s):
    """Display titles: drop the site-name tail and Excel page-type codes."""
    if not s:
        return s
    s = re.sub(r"\s*\|\s*Infineon\s*Technologies.*$", "", s, flags=re.I)
    s = re.sub(r"\s*[—–-]+\s*[A-Z]{2,6}\s*$", "", s)
    return s.strip()


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

    scheduler.add_job(lambda: runner.run_check(kind="scheduled"),
                      trigger, id="check", replace_existing=True,
                      misfire_grace_time=3600 * 6)


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


def _nohas(cat):
    # captured, but this category never appeared. Guarded on last_checked so an
    # uncaptured page (content unknown) is not counted as "no X".
    return "u.last_checked IS NOT NULL AND NOT " + _has(cat)


def _flag(block, key):
    """Filter on a presence-only boolean stored in urls.flags (JSON)."""
    return (f"(u.flags IS NOT NULL AND u.flags<>''"
            f" AND json_extract(u.flags, '$.{block}.{key}') = 1)")


def _noflag(block, key):
    return "u.last_checked IS NOT NULL AND NOT " + _flag(block, key)


# real monitoring states - what the watcher says about the page (OR-combined)
STATUS_FILTERS = {
    "changed":   ("Changed", "u.last_status='changed' AND u.active=1"),
    "rejected":  ("Awaiting fix", "u.last_status='rejected'"),
    "unchanged": ("Unchanged", "u.last_status='unchanged' AND u.active=1"),
    "baseline":  ("Baseline saved", "u.last_status='baseline'"),
    "notlive":   ("Dead page (404/500)", "u.last_status='notlive'"),
    "blank":     ("Serves a blank page", "u.last_status='blank'"),
    "error":     ("Error", "u.last_status='error'"),
    "paused":    ("Paused (not monitored)", "u.active=0"),
    "new":       ("Not captured yet", "u.last_checked IS NULL"),
}

# properties of the page itself / what we captured from it (AND-combined).
# Every content attribute gets a Has and a No half, generated together so the
# two can never drift apart or one be forgotten.
#   stem: short, stable key fragment (used in URLs) -> library category, label
_CONTENT_ATTRS = [
    ("docs",       "documents",        "documents"),
    ("design",     "design_resources", "design resources"),
    ("features",   "features",         "key features"),
    ("benefits",   "benefits",         "benefits"),
    ("apps",       "applications",     "applications"),
    ("categories", "categories",       "browse-by-category"),
    ("products",   "products",         "related products"),
    ("news",       "news",             "news & highlights"),
    ("trainings",  "trainings",        "trainings & webinars"),
    ("videos",     "videos",           "videos"),
]

ATTR_FILTERS = {
    "checked":  ("Captured", "u.last_checked IS NOT NULL"),
    # "live" means a visitor gets the page: a 2xx with an empty body does not
    # count, so blank pages are excluded here and counted as not live below.
    "live":     ("Live (HTTP OK)",
                 "u.last_http IS NOT NULL AND u.last_http < 400"
                 " AND IFNULL(u.last_status,'') <> 'blank'"),
    "notlive":  ("Not live (404/500/blank)",
                 "(u.last_http IS NOT NULL AND u.last_http >= 400)"
                 " OR u.last_status='blank'"),
}
for _stem, _cat, _label in _CONTENT_ATTRS:
    ATTR_FILTERS["has" + _stem] = ("Has " + _label, _has(_cat))
    ATTR_FILTERS["no" + _stem] = ("No " + _label, _nohas(_cat))
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
        where.append("(u.url LIKE ? OR u.label LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    picked = ["(" + STATUS_FILTERS[s][1] + ")"
              for s in statuses if s in STATUS_FILTERS]
    if picked:
        where.append("(" + " OR ".join(picked) + ")")
    types = [t for t in ptypes if t]
    if types:
        where.append("u.ptype IN (" + ",".join("?" * len(types)) + ")")
        params += types
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
    urls = con.execute(f"""
        SELECT u.*,
          (SELECT COUNT(*) FROM changes c WHERE c.url_id=u.id AND c.reviewed=0)
            AS open_changes,
          (SELECT c2.id FROM changes c2 WHERE c2.url_id=u.id AND c2.reviewed=0
            ORDER BY c2.id DESC LIMIT 1) AS latest_change_id,
          (SELECT s.page_title FROM snapshots s WHERE s.url_id=u.id
            AND s.status='ok' AND s.page_title<>'' ORDER BY s.id DESC LIMIT 1)
            AS page_title
        FROM urls u {wsql}
        ORDER BY CASE
            WHEN u.last_status='changed' AND u.active=1 THEN 0
            WHEN u.last_status IN ('error', 'rejected') THEN 1
            ELSE 2 END, u.id
        LIMIT ? OFFSET ?
    """, params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()

    counts = con.execute("""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN last_status='changed' AND active=1 THEN 1 ELSE 0 END) AS changed,
          SUM(CASE WHEN last_status='unchanged' AND active=1 THEN 1 ELSE 0 END) AS unchanged,
          SUM(CASE WHEN last_status='baseline' THEN 1 ELSE 0 END) AS baseline,
          SUM(CASE WHEN last_checked IS NOT NULL THEN 1 ELSE 0 END) AS checked,
          SUM(CASE WHEN last_status='rejected' THEN 1 ELSE 0 END) AS rejected,
          SUM(CASE WHEN last_status='error' THEN 1 ELSE 0 END) AS errors,
          SUM(CASE WHEN active=0 THEN 1 ELSE 0 END) AS paused,
          SUM(CASE WHEN last_checked IS NULL THEN 1 ELSE 0 END) AS fresh,
          SUM(CASE WHEN last_status='blank' THEN 1 ELSE 0 END) AS blank,
          -- a page serving an empty body is not reachable content either,
          -- so it belongs in the same headline number as the 404s
          SUM(CASE WHEN (last_http IS NOT NULL AND last_http>=400)
                        OR last_status='blank' THEN 1 ELSE 0 END) AS notlive
        FROM urls""").fetchone()
    last_run = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    total_open = con.execute(
        "SELECT COUNT(*) FROM changes WHERE reviewed=0").fetchone()[0]
    ptypes = con.execute(
        "SELECT ptype, COUNT(*) n FROM urls WHERE ptype<>'' GROUP BY ptype"
        " ORDER BY n DESC").fetchall()
    # how many captured pages carry each category - one GROUP BY, indexed
    cat_pages = {r["category"]: r["n"] for r in con.execute(
        "SELECT category, COUNT(DISTINCT url_id) n FROM items GROUP BY category")}
    # status + urls-only attr filters: one bulk pass each
    status_counts = _bulk_counts(con, STATUS_FILTERS)
    attr_counts = _bulk_counts(con, ATTR_FILTERS)
    con.close()
    # content has/no counts come from cat_pages: has = pages with the category
    # (identical to the EXISTS filter), no = captured pages without it
    checked = attr_counts.get("checked", 0)
    for _stem, _cat, _ in _CONTENT_ATTRS:
        has = cat_pages.get(_cat, 0)
        attr_counts["has" + _stem] = has
        attr_counts["no" + _stem] = checked - has

    # quick chips: the handful of filters worth one-click access, shown only
    # when they actually match something. status kind links ?status=, attr ?attr=
    quick_chips = [c for c in (
        {"kind": "status", "key": "new", "label": "Not captured yet",
         "n": status_counts["new"]},
        {"kind": "status", "key": "baseline", "label": "Baseline saved",
         "n": status_counts["baseline"]},
        {"kind": "attr", "key": "live", "label": "Live",
         "n": attr_counts["live"]},
        {"kind": "status", "key": "notlive", "label": "Dead (404/500)",
         "n": status_counts["notlive"]},
        {"kind": "status", "key": "blank", "label": "Blank page",
         "n": status_counts["blank"]},
        {"kind": "status", "key": "changed", "label": "Changed",
         "n": status_counts["changed"]},
        {"kind": "status", "key": "rejected", "label": "Awaiting fix",
         "n": status_counts["rejected"]},
        {"kind": "status", "key": "error", "label": "Errors",
         "n": status_counts["error"]},
    ) if c["n"]]

    return render_template("index.html", urls=urls, state=runner.RUN_STATE,
                           last_run=last_run, total_open=total_open,
                           next_run=next_run_text(), settings=db.get_settings(),
                           q=q, page=page, pages=pages,
                           total_match=total_match, counts=counts,
                           per_page=PER_PAGE, ptypes=ptypes,
                           sel_status=statuses, sel_types=sel_types,
                           sel_attrs=attrs, status_filters=STATUS_FILTERS,
                           attr_filters=ATTR_FILTERS,
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
    flash(f"File contained {len(urls)} URL(s): added {added}, skipped {skipped} duplicate(s).")
    return redirect(url_for("index"))


@app.route("/urls/<int:url_id>/delete", methods=["POST"])
def urls_delete(url_id):
    import shutil
    con = db.connect()
    con.execute("DELETE FROM urls WHERE id=?", (url_id,))
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
    ("images", "Images"),
]


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


def backend_system(category, meta, doc_type=None):
    """DAM / Windchill / '' (type not on the migration sheet).

    Per document, never per page: one product page carries a datasheet
    (Windchill) and a product brief (DAM) side by side.

    doc_type is the row's own Type column, captured as its own field. The meta
    fallback exists only for snapshots taken before that was stored.
    """
    if category in ALWAYS_DAM:
        return "DAM"
    if category != "documents":
        return ""
    return DOC_TYPE_SYSTEM.get(
        _norm_doc_type(doc_type or document_type_of(meta)), "")


def document_type_of(meta):
    """Fallback only: the first meta field is the Type column by convention.
    Prefer the captured doc_type - position is what a "New" badge broke."""
    return (meta or "").split("|")[0].strip()


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
    "PTP": "Part type page",
    "ADP": "Application division", "AOP": "Application overview",
    "ASP": "Application solution", "AFB": "Application focus",
}
# page types that share one tree, in the order their levels nest
MERGED_TABS = [("Products & parts", ["Product", "Part"])]


def _hierarchy_tabs(con):
    """Tabs, discovered from the data: one per page type, except that parts
    are folded into the product tree rather than standing alone."""
    types = [r["ptype"] for r in con.execute(
        "SELECT ptype, COUNT(*) n FROM urls WHERE active=1 AND ptype<>''"
        " GROUP BY ptype ORDER BY n DESC")]
    tabs, merged = [], set()
    for label, members in MERGED_TABS:
        present = [t for t in members if t in types]
        if present:
            tabs.append((label, present))
            merged.update(present)
    tabs += [(t, [t]) for t in types if t not in merged]
    return tabs


def _breadcrumbs(con, ptypes):
    marks = ",".join("?" * len(ptypes))
    rows = con.execute(
        "SELECT id, url, ptype, level, breadcrumb,"
        " (SELECT s.page_title FROM snapshots s WHERE s.url_id=urls.id"
        "   AND s.status='ok' AND s.page_title<>'' ORDER BY s.id DESC LIMIT 1)"
        "   AS page_title"
        f" FROM urls WHERE active=1 AND ptype IN ({marks})"
        " AND breadcrumb NOT IN ('', '[]') AND breadcrumb IS NOT NULL"
        # dead and blank pages are not part of the tree: a 404 publishes a
        # breadcrumb of its own ("Error Pages > Page Not Found") and would
        # otherwise appear in the product hierarchy as though it were a product
        " AND IFNULL(last_status,'') NOT IN ('notlive','blank','error')",
        list(ptypes)).fetchall()
    out = []
    for r in rows:
        try:
            crumbs = json.loads(r["breadcrumb"])
        except Exception:
            continue
        crumbs = [c for c in crumbs if c.get("title")
                  and c["title"].strip().lower() != "home"]
        if not crumbs:
            continue
        # The full crumb path is the page's address in the taxonomy, self
        # included. A product's breadcrumb ends with itself, and a part's ends
        # with the product family it belongs to - so both land on the SAME
        # node, and the part sits inside its product instead of beside it.
        # That is the PFP > PSFP > PEFP > PTP chain, from the site's own data.
        path = [c["title"].strip() for c in crumbs]
        out.append({"id": r["id"], "url": r["url"], "ptype": r["ptype"],
                    "level": r["level"] or "",
                    "level_name": LEVEL_NAMES.get(r["level"] or "", ""),
                    "title": r["page_title"] or "", "crumbs": path})
    return out


def _hierarchy_tree(pages):
    """Nest pages under their own breadcrumb path.

    The site publishes a JSON-LD breadcrumb on every page, and a part's
    breadcrumb ends at its product family - so parts land under their product
    without scraping a parametric table off all 190 product pages. Types whose
    pages have no depth (a flat /part/ list) simply come out flat.
    """
    root = {"name": "", "children": {}, "pages": [], "count": 0}
    for p in pages:
        node = root
        node["count"] += 1
        for part in p["crumbs"]:
            child = node["children"].get(part)
            if child is None:
                child = {"name": part, "children": {}, "pages": [], "count": 0}
                node["children"][part] = child
            node = child
            node["count"] += 1
        node["pages"].append(p)

    def freeze(n, depth=0):
        return {"name": n["name"], "count": n["count"], "depth": depth,
                "pages": sorted(n["pages"], key=lambda x: x["title"] or x["url"]),
                "children": [freeze(c, depth + 1)
                             for c in sorted(n["children"].values(),
                                             key=lambda c: c["name"])]}
    tree = freeze(root)
    _live_levels(tree)
    _attach_self_pages(tree)
    return tree


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


def _live_levels(node, depth=0):
    """Work out each page's level from the LIVE tree it actually sits in,
    rather than taking the sheet's word for it.

        depth 1                     product category
        depth 2                     product family
        no child nodes below it     product end family
        anything in between         product sub-family
        a part page                 part type page

    A part shares its product's node (their breadcrumbs meet there), so a
    product with parts is still an end family - the parts are pages on it, not
    branches under it.

    Recomputed from whatever has been captured, so it sharpens as the run fills
    the tree in - and it follows the site rather than a report that can go
    stale. Where it disagrees with the sheet, the live answer is shown.
    """
    kind = "PTP" if not node["children"] else "PSFP"
    for p in node["pages"]:
        if p.get("ptype") == "Part":
            p["live_level"] = "PTP"
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
        _live_levels(c, depth + 1)


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
    if sections:
        for cat, label in SECTION_LABELS:
            if cat == "categories":
                continue                      # rendered as a tree instead
            items = sections.get(cat, [])
            # once every page of this type is captured, a category that is
            # empty across ALL of them does not belong to this page shape
            if not items and present is not None and cat not in present:
                continue
            for it in items:
                if isinstance(it, dict):
                    it["item_key"] = runner.item_key(cat, it.get("title", ""),
                                                     it.get("url", ""))
                    it["backend"] = backend_system(cat, it.get("meta", ""),
                                                   it.get("dtype"))
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
    return render_template("url_detail.html", u=u, snaps=snaps, changes=changes,
                           latest=latest, library=library, cat_tree=cat_tree,
                           redirect_to=redirect_to, flags=flags)


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
        # capture - the old snapshot stays the baseline, so the page will be
        # flagged again on every check until the website is fixed.
        con.execute("UPDATE snapshots SET rejected=1 WHERE id=?",
                    (c["new_snapshot_id"],))
        con.execute("UPDATE urls SET last_status='rejected' WHERE id=?",
                    (c["url_id"],))
        flash("Rejected — the old version stays the baseline. The page will be "
              "flagged again on the next check until the website matches it.")
    else:
        remaining = con.execute(
            "SELECT COUNT(*) FROM changes WHERE url_id=? AND reviewed=0",
            (c["url_id"],)).fetchone()[0]
        if remaining == 0:
            con.execute("UPDATE urls SET last_status='unchanged'"
                        " WHERE id=? AND last_status IN ('changed','rejected')",
                        (c["url_id"],))
        flash("Approved — this version is the new baseline.")
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
    if runner.run_check_background(kind="manual", url_ids=ids):
        flash(f"Checking {len(ids)} not-yet-checked page(s)…" if ids
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
            remaining = max(0, s.get("total", 0) - s["done"])
            s["eta_s"] = int(remaining / rate) if rate > 0 else None
    return jsonify(s)


@app.route("/settings", methods=["GET", "POST"])
def settings_view():
    if request.method == "POST":
        for key in ("frequency", "weekday", "hour", "concurrency",
                    "visual_threshold", "height_threshold", "ignore_patterns",
                    "ignore_image_patterns"):
            if key in request.form:
                db.set_setting(key, request.form[key].strip())
        reschedule()
        flash("Settings saved. Next automatic check: " + next_run_text())
        return redirect(url_for("settings_view"))
    return render_template("settings.html", s=db.get_settings(),
                           next_run=next_run_text(), weekdays=WEEKDAYS)


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
            " MIN(meta) AS m FROM items"
            " WHERE category<>'about' AND item_key<>'' GROUP BY item_key"
        ).fetchall()
    finally:
        con.close()
    data = [{"key": r["k"], "title": r["t"], "cat": r["c"],
             "meta": r["m"] or "", "tl": (r["t"] or "").lower()} for r in rows]
    _search_cache.update(ts=time.monotonic(), data=data,
                         titles=[d["title"] for d in data], building=False)


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


def _fuzzy_items(q, cat, limit):
    """Distinct items ranked by typo-tolerant relevance to q, best first.
    Exact substring hits always rank above fuzzy ones."""
    data, titles = _choices()
    ql = q.strip().lower()
    if not ql:
        return []
    if cat and cat != "all":
        pairs = [(d, t) for d, t in zip(data, titles) if d["cat"] == cat]
        data = [p[0] for p in pairs]
        titles = [p[1] for p in pairs]
    scored = {}                      # key -> (tier, score, data)
    for d in data:                   # exact substring: guaranteed, top tier
        if ql in d["tl"]:
            scored[d["key"]] = (2, 100, d)
    # rapidfuzz WRatio forgives spelling mistakes and word reordering
    for _t, score, i in process.extract(
            q, titles, scorer=fuzz.WRatio, limit=max(limit * 3, 40),
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
        if cat and cat != "all":
            catf, catp = " AND i.category=?", [cat]
        for r in con.execute(
                f"SELECT DISTINCT i.item_key AS k FROM items i"
                f" WHERE i.category<>'about' AND i.item_key<>''"
                f" AND {' AND '.join(conds)}{catf} LIMIT 500", params + catp):
            if r["k"] not in seen:
                seen.add(r["k"])
                ordered.append(r["k"])
    return ordered[:limit]


@app.route("/search")
def search_items():
    """Search every captured item (documents, boards, trainings, videos,
    features...) and show which pages carry it. Typo-tolerant."""
    q = request.args.get("q", "").strip()
    cat = request.args.get("cat", "all")
    groups, total, cats = [], 0, []
    con = db.connect()
    # 'about' is tracked for text comparison only - never surfaced (user's call)
    cats = [r["category"] for r in con.execute(
        "SELECT category, COUNT(*) n FROM items WHERE category<>'about'"
        " GROUP BY category ORDER BY n DESC")]
    indexed = con.execute(
        "SELECT COUNT(*) FROM items WHERE category<>'about'").fetchone()[0]
    if q:
        keys = _search_keys(con, q, cat if cat in cats else "all", 250)
        if keys:
            marks = ",".join("?" * len(keys))
            rows = con.execute(f"""
                SELECT i.category, i.grp, i.title, i.item_url, i.meta, i.url_id,
                       i.item_key, i.doc_type, u.url AS page_url, u.label,
                       (SELECT s.page_title FROM snapshots s WHERE s.url_id=u.id
                         AND s.status='ok' AND s.page_title<>''
                         ORDER BY s.id DESC LIMIT 1) AS page_title
                FROM items i JOIN urls u ON u.id=i.url_id
                WHERE i.item_key IN ({marks})""", keys).fetchall()
        else:
            rows = []
        rank = {k: i for i, k in enumerate(keys)}
        by_item, order = {}, []
        for r in rows:
            gk = (r["title"].lower(), r["item_url"])
            if gk not in by_item:
                by_item[gk] = {"title": r["title"], "url": r["item_url"],
                               "category": r["category"], "grp": r["grp"],
                               "meta": r["meta"], "pages": [],
                               "item_key": r["item_key"],
                               "rank": rank.get(r["item_key"], 10 ** 9),
                               "backend": backend_system(r["category"],
                                                         r["meta"],
                                                         r["doc_type"])}
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
    return render_template("search.html", q=q, cat=cat, cats=cats,
                           groups=groups, total=total, indexed=indexed,
                           labels=dict(SECTION_LABELS))


@app.route("/api/suggest")
def api_suggest():
    """Type-ahead for content search: typo-tolerant, best match first."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    cat = request.args.get("cat", "all")
    raw = _fuzzy_items(q, cat, 30)          # over-fetch; we collapse below
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
    # the richest meta wins: the same item can be listed tersely on one page
    meta = max((r["meta"] or "" for r in rows), key=len)
    dtype = next((r["doc_type"] for r in rows if r["doc_type"]), "")
    item = {"title": first["title"], "url": first["item_url"],
            "category": first["category"], "grp": first["grp"], "meta": meta,
            "backend": backend_system(first["category"], meta, dtype),
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
    return render_template("item.html", item=item, pages=pages,
                           by_type=sorted(by_type.items()))


@app.route("/hierarchy")
def hierarchy():
    """A tree per page type, built from each page's own JSON-LD breadcrumb -
    so it follows the site's structure and grows with new entries instead of
    being a hand-maintained list. Parts nest inside products, because a part
    type page is the bottom of the product taxonomy, not a separate one."""
    con = db.connect()
    tabs = _hierarchy_tabs(con)
    labels = [t[0] for t in tabs]
    sel = request.args.get("type") or (labels[0] if labels else "")
    members = next((m for lbl, m in tabs if lbl == sel), [])
    pages = _breadcrumbs(con, members) if members else []
    counts, totals = {}, {}
    for lbl, mem in tabs:
        marks = ",".join("?" * len(mem))
        counts[lbl] = con.execute(
            f"SELECT COUNT(*) FROM urls WHERE active=1 AND ptype IN ({marks})"
            " AND breadcrumb NOT IN ('','[]') AND breadcrumb IS NOT NULL",
            mem).fetchone()[0]
        totals[lbl] = con.execute(
            f"SELECT COUNT(*) FROM urls WHERE active=1 AND ptype IN ({marks})",
            mem).fetchone()[0]
    con.close()
    tree = _hierarchy_tree(pages)          # also assigns each page's live_level
    # the levels present in this tab, in taxonomy order, for the legend
    order = list(LEVEL_NAMES)
    seen_levels = sorted({p.get("live_level") for p in pages
                          if p.get("live_level")},
                         key=lambda l: order.index(l) if l in order else 99)
    disagreements = sum(1 for p in pages if p.get("level_disagrees"))
    return render_template("hierarchy.html", types=labels, sel=sel, tree=tree,
                           counts=counts, total=totals.get(sel, 0),
                           shown=len(pages), levels=seen_levels,
                           level_names=LEVEL_NAMES,
                           disagreements=disagreements)


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


if __name__ == "__main__":
    db.init_db()
    scheduler.start()
    reschedule()
    print()
    print("  InfineonWatch is running:  http://127.0.0.1:5000")
    print("  From another device on the same network, use this PC's IP,")
    print("  e.g. http://192.168.x.x:5000 (see README).")
    print("  Next scheduled check:      " + next_run_text())
    print("  Keep this window open. Press Ctrl+C to stop.")
    print()
    # 0.0.0.0 = reachable from other devices on your local network too
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
