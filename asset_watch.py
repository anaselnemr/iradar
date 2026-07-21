"""Asset fingerprint watch: catches a file republished at the SAME URL.

The page watch compares image URL SETS and library item URLs - so a datasheet
or product photo whose bytes are swapped behind an unchanged URL is invisible
to it (proven by the offline mutation battery: the one class of real update
with no signal). This closes that hole without downloading anything: after a
run, every unique asset URL gets a HEAD request and the server's own
validators (ETag / Last-Modified / Content-Length) are stored. When a
validator moves for a URL we already knew, that file's bytes changed - and it
is recorded in asset_changes for review on the dashboard.

Scope: FILE-like assets only (documents, Scene7 media, renditions). Plain
page URLs are excluded - pages are what the page watch itself covers, and an
HTML ETag moves on every deploy without the content changing.
"""
import json
import re
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import db
from capture import USER_AGENT

HOSTS = ("https://www.infineon.com/", "https://assets.infineon.com/")
# a URL is an asset when it points at a FILE, not a page
_FILE_RE = re.compile(
    r"\.(pdf|zip|xlsx?|docx?|pptx?|csv|txt|exe|msi|hex|bin|gz|7z|rar)(\?|#|$)"
    r"|/is/image/|/is/content/|/jcr:content/renditions/", re.I)

CONCURRENCY = 24
TIMEOUT = 12

# progress for the dashboard ("checking 3 120/14 000 assets…")
ASSET_STATE = {"running": False, "done": 0, "total": 0, "changed": 0,
               "message": ""}


def is_asset_url(url):
    return bool(url and url.startswith(HOSTS) and _FILE_RE.search(url))


def collect_asset_urls(con):
    """Every unique asset URL the library currently references:
    content images from the latest good snapshot of every active page, plus
    file-like item URLs (documents, videos, resources) from the items index."""
    urls = {}
    for r in con.execute(
            "SELECT DISTINCT item_url FROM items WHERE item_url<>''"):
        u = r["item_url"]
        if is_asset_url(u):
            urls[u] = "document"
    rows = con.execute(
        "SELECT s.dir FROM snapshots s"
        " JOIN (SELECT url_id, MAX(id) mid FROM snapshots"
        "       WHERE status='ok' AND dir<>'' GROUP BY url_id) m ON s.id=m.mid"
        " JOIN urls u ON u.id=s.url_id WHERE u.active=1").fetchall()
    for r in rows:
        p = Path(r["dir"]) / "images.json"
        if not p.exists():
            continue
        try:
            for u in json.loads(p.read_text(encoding="utf-8")):
                if is_asset_url(u):
                    urls.setdefault(u, "image")
        except Exception:
            pass
    return urls


def fingerprint(url):
    """One asset's server validators, without downloading the file.
    Returns {status, etag, last_modified, content_length} (empty on failure).
    Accept-Encoding: identity keeps the ETag stable (a gzip vs brotli answer
    would carry a different ETag for identical bytes)."""
    def _ask(method, extra=None):
        req = urllib.request.Request(url, method=method, headers={
            "User-Agent": USER_AGENT, "Accept-Encoding": "identity",
            **(extra or {})})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            h = resp.headers
            etag = (h.get("ETag") or "").strip()
            if etag.startswith(("W/", "w/")):
                etag = etag[2:]
            etag = etag.strip('"')
            try:
                # Content-Range total wins under a Range request
                cr = h.get("Content-Range") or ""
                length = int(cr.rsplit("/", 1)[1]) if "/" in cr else \
                    int(h.get("Content-Length") or 0)
            except Exception:
                length = 0
            return {"status": resp.status, "etag": etag,
                    "last_modified": (h.get("Last-Modified") or "").strip(),
                    "content_length": length or None}
    try:
        fp = _ask("HEAD")
        # some CDN paths answer HEAD without validators - one cheap retry as a
        # 1-byte ranged GET, which forces the full header set
        if not fp["etag"] and not fp["last_modified"]:
            try:
                fp = _ask("GET", {"Range": "bytes=0-0"})
            except Exception:
                pass
        return fp
    except Exception:
        try:
            return _ask("GET", {"Range": "bytes=0-0"})
        except Exception:
            return {"status": None, "etag": "", "last_modified": "",
                    "content_length": None}


def bytes_changed(old, fp):
    """Did the file behind this URL change, judged by validators only?
    old = the stored assets row (mapping-like), fp = a fresh fingerprint.
    Strongest signal first; a validator missing on EITHER side never judges,
    so a CDN that stops sending ETags cannot fake an update."""
    if not fp or fp.get("status") not in (200, 206):
        return False                       # unreachable now - not a change
    if (old["content_length"] or 0) and (fp["content_length"] or 0):
        if old["content_length"] != fp["content_length"]:
            return True
    if old["etag"] and fp["etag"]:
        return old["etag"] != fp["etag"]
    if old["last_modified"] and fp["last_modified"]:
        return old["last_modified"] != fp["last_modified"]
    return False


def run_asset_check():
    """Fingerprint every referenced asset; record the ones whose bytes moved.
    First sighting of a URL just seeds its fingerprint (no alarm)."""
    con = db.connect()
    try:
        targets = collect_asset_urls(con)
        known = {r["url"]: r for r in con.execute("SELECT * FROM assets")}
        ASSET_STATE.update(running=True, done=0, total=len(targets), changed=0,
                           message="checking asset files…")
        now = db.now_iso()
        changed = 0
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            futs = {ex.submit(fingerprint, u): u for u in targets}
            for fut in as_completed(futs):
                u = futs[fut]
                fp = fut.result()
                ASSET_STATE["done"] += 1
                old = known.get(u)
                if old is None:
                    con.execute(
                        "INSERT OR REPLACE INTO assets(url, kind, etag,"
                        " last_modified, content_length, http_status,"
                        " first_seen, last_checked) VALUES(?,?,?,?,?,?,?,?)",
                        (u, targets[u], fp["etag"], fp["last_modified"],
                         fp["content_length"], fp["status"], now, now))
                elif bytes_changed(old, fp):
                    changed += 1
                    ASSET_STATE["changed"] = changed
                    con.execute(
                        "INSERT INTO asset_changes(url, kind, ts, old_etag,"
                        " new_etag, old_modified, new_modified, old_length,"
                        " new_length) VALUES(?,?,?,?,?,?,?,?,?)",
                        (u, old["kind"] or targets[u], now, old["etag"],
                         fp["etag"], old["last_modified"], fp["last_modified"],
                         old["content_length"], fp["content_length"]))
                    con.execute(
                        "UPDATE assets SET etag=?, last_modified=?,"
                        " content_length=?, http_status=?, last_checked=?"
                        " WHERE url=?",
                        (fp["etag"], fp["last_modified"], fp["content_length"],
                         fp["status"], now, u))
                else:
                    con.execute(
                        "UPDATE assets SET http_status=?, last_checked=?"
                        " WHERE url=?", (fp["status"], now, u))
                if ASSET_STATE["done"] % 200 == 0:
                    ASSET_STATE["message"] = (
                        f"checking asset files… {ASSET_STATE['done']}"
                        f"/{ASSET_STATE['total']}")
                    con.commit()
        con.commit()
        print(f"[assets] checked {len(targets)} files: {changed} changed"
              " behind an unchanged URL", flush=True)
    finally:
        ASSET_STATE.update(running=False, message="")
        con.close()


def run_in_background():
    """Post-run hook: fire the check in a daemon thread (skips if running)."""
    if ASSET_STATE["running"]:
        return False
    t = threading.Thread(target=run_asset_check, daemon=True)
    t.start()
    return True
