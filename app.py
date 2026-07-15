"""InfineonWatch - website change monitor.

Run:  python app.py     then open http://127.0.0.1:5000
"""
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_file, url_for)

import compare as cmp
import db
import runner

app = Flask(__name__)
app.secret_key = "infineonwatch-local-only"

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


# real monitoring states - what the watcher says about the page (OR-combined)
STATUS_FILTERS = {
    "changed":   ("Changed", "u.last_status='changed' AND u.active=1"),
    "rejected":  ("Awaiting fix", "u.last_status='rejected'"),
    "unchanged": ("Unchanged", "u.last_status='unchanged' AND u.active=1"),
    "baseline":  ("Baseline saved", "u.last_status='baseline'"),
    "error":     ("Error", "u.last_status='error'"),
    "paused":    ("Paused (not monitored)", "u.active=0"),
    "new":       ("Not captured yet", "u.last_checked IS NULL"),
}

# properties of the page itself / what we captured from it (AND-combined)
ATTR_FILTERS = {
    "checked":      ("Captured", "u.last_checked IS NOT NULL"),
    "live":         ("Live (HTTP OK)",
                     "u.last_http IS NOT NULL AND u.last_http < 400"),
    "notlive":      ("Not live (404/500)",
                     "u.last_http IS NOT NULL AND u.last_http >= 400"),
    "hasdocs":      ("Has documents", _has("documents")),
    "nodocs":       ("No documents",
                     "u.last_checked IS NOT NULL AND NOT " + _has("documents")),
    "hasvideos":    ("Has videos (news & highlights)", _has("news")),
    "hastrainings": ("Has trainings & webinars", _has("trainings")),
    "hasdesign":    ("Has design resources", _has("design_resources")),
    "hasfeatures":  ("Has key features", _has("features")),
    "hasbenefits":  ("Has benefits", _has("benefits")),
    "hasapps":      ("Has applications", _has("applications")),
    "hascategories": ("Has browse-by-category", _has("categories")),
}
ATTR_CAT = {"hasdocs": "documents", "hasvideos": "news",
            "hastrainings": "trainings", "hasdesign": "design_resources",
            "hasfeatures": "features", "hasbenefits": "benefits",
            "hasapps": "applications", "hascategories": "categories"}


def _url_filter(q, statuses, ptypes, attrs):
    """statuses OR'd, types OR'd, attributes AND'd - all multi-select."""
    where, params = [], []
    if q:
        where.append("(u.url LIKE ? OR u.label LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    picked = [STATUS_FILTERS[s][1] for s in statuses if s in STATUS_FILTERS]
    if picked:
        where.append("(" + " OR ".join(picked) + ")")
    types = [t for t in ptypes if t]
    if types:
        where.append("u.ptype IN (" + ",".join("?" * len(types)) + ")")
        params += types
    for a in attrs:
        if a in ATTR_FILTERS:
            where.append(ATTR_FILTERS[a][1])
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
          SUM(CASE WHEN last_http IS NOT NULL AND last_http>=400 THEN 1 ELSE 0 END) AS notlive
        FROM urls""").fetchone()
    last_run = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    total_open = con.execute(
        "SELECT COUNT(*) FROM changes WHERE reviewed=0").fetchone()[0]
    ptypes = con.execute(
        "SELECT ptype, COUNT(*) n FROM urls WHERE ptype<>'' GROUP BY ptype"
        " ORDER BY n DESC").fetchall()
    # how many pages carry each kind of content (for the Content filter)
    cat_pages = {r["category"]: r["n"] for r in con.execute(
        "SELECT category, COUNT(DISTINCT url_id) n FROM items GROUP BY category")}
    con.close()

    status_counts = {"changed": counts["changed"] or 0,
                     "rejected": counts["rejected"] or 0,
                     "unchanged": counts["unchanged"] or 0,
                     "baseline": counts["baseline"] or 0,
                     "error": counts["errors"] or 0,
                     "paused": counts["paused"] or 0,
                     "new": counts["fresh"] or 0}
    attr_counts = {"checked": counts["checked"] or 0,
                   "live": (counts["checked"] or 0) - (counts["notlive"] or 0)
                           - (counts["errors"] or 0),
                   "notlive": counts["notlive"] or 0,
                   "nodocs": (counts["checked"] or 0)
                             - cat_pages.get("documents", 0)}
    for key, cat in ATTR_CAT.items():
        attr_counts[key] = cat_pages.get(cat, 0)

    return render_template("index.html", urls=urls, state=runner.RUN_STATE,
                           last_run=last_run, total_open=total_open,
                           next_run=next_run_text(), settings=db.get_settings(),
                           q=q, page=page, pages=pages,
                           total_match=total_match, counts=counts,
                           per_page=PER_PAGE, ptypes=ptypes,
                           sel_status=statuses, sel_types=sel_types,
                           sel_attrs=attrs, status_filters=STATUS_FILTERS,
                           attr_filters=ATTR_FILTERS,
                           status_counts=status_counts, attr_counts=attr_counts)


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
    ("documents", "Documents"),
    ("design_resources", "Design resources & boards"),
    ("features", "Key features"),
    ("benefits", "Benefits"),
    ("applications", "Applications"),
    ("trainings", "Trainings & webinars"),
    ("news", "News & highlights"),
    ("images", "Images"),
]


def _load_sections(snap_dir):
    p = Path(snap_dir) / "sections.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


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

    library = []
    if sections:
        for cat, label in SECTION_LABELS:
            if cat == "categories":
                continue                      # rendered as a tree instead
            items = sections.get(cat, [])
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
            library.append({"label": label, "items": items, "groups": groups})
    return render_template("url_detail.html", u=u, snaps=snaps, changes=changes,
                           latest=latest, library=library, cat_tree=cat_tree)


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


@app.route("/search")
def search_items():
    """Search every captured item (documents, boards, trainings, videos,
    features...) and show which pages carry it."""
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
        where = ["(i.title LIKE ? OR i.item_url LIKE ? OR i.meta LIKE ?)",
                 "i.category<>'about'"]
        params = [f"%{q}%"] * 3
        if cat != "all" and cat in cats:
            where.append("i.category=?")
            params.append(cat)
        wsql = "WHERE " + " AND ".join(where)
        total = con.execute(
            f"SELECT COUNT(*) FROM items i {wsql}", params).fetchone()[0]
        rows = con.execute(f"""
            SELECT i.category, i.grp, i.title, i.item_url, i.meta, i.url_id,
                   u.url AS page_url, u.label,
                   (SELECT s.page_title FROM snapshots s WHERE s.url_id=u.id
                     AND s.status='ok' AND s.page_title<>''
                     ORDER BY s.id DESC LIMIT 1) AS page_title
            FROM items i JOIN urls u ON u.id=i.url_id
            {wsql} ORDER BY i.title LIMIT 2000""", params).fetchall()
        # one entry per distinct item, listing every page that carries it
        by_item = {}
        order = []
        for r in rows:
            key = (r["title"].lower(), r["item_url"])
            if key not in by_item:
                by_item[key] = {"title": r["title"], "url": r["item_url"],
                                "category": r["category"], "grp": r["grp"],
                                "meta": r["meta"], "pages": []}
                order.append(key)
            by_item[key]["pages"].append(
                {"id": r["url_id"], "url": r["page_url"],
                 "title": r["page_title"] or r["label"] or r["page_url"]})
        groups = [by_item[k] for k in order]
        groups.sort(key=lambda g: -len(g["pages"]))
    con.close()
    return render_template("search.html", q=q, cat=cat, cats=cats,
                           groups=groups, total=total, indexed=indexed,
                           labels=dict(SECTION_LABELS))


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
