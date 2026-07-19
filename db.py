"""SQLite database layer for InfineonWatch."""
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SNAP_DIR = DATA_DIR / "snapshots"
CHANGE_DIR = DATA_DIR / "changes"
DB_PATH = DATA_DIR / "watch.db"

DEFAULT_SETTINGS = {
    "frequency": "weekly",        # daily | bidaily | weekly
    "weekday": "mon",             # for weekly
    "hour": "6",                  # 24h clock, local time
    "concurrency": "4",           # parallel browser tabs
    "visual_threshold": "0.5",    # % of pixels that must differ to flag a visual change
    "height_threshold": "80",     # px difference in page height to flag
    # one regex per line; matching text lines are ignored.
    # Defaults: relative timestamps ("13 days ago") change by themselves, and
    # the "Infineon stock last updated: Jul 18, 2026, 7:48 AM" ticker stamp
    # refreshes every run - neither is content Infineon edited, so neither
    # should ever flag a page for review.
    "ignore_patterns": (r"^\d+ (day|hour|minute|week|month|year)s? ago$"
                        "\n"
                        r"^[A-Z][a-z]{2,8} \d{1,2}, \d{4}, \d{1,2}:\d{2}\s?[AP]M$"),
    # backend search pages: '{q}' marks where the asset name goes. Editable
    # in Settings -> Backends so a changed link is a ten-second fix.
    "dam_search_url": "",
    "windchill_search_url": "",
    # AEM author editor: '{path}' = the page's public path (after /en), the
    # editor wants a trailing .html - all of it editable in Settings
    "aem_author_url": "https://author.infineon.com/editor.html/content/ifx/language-masters/en{path}.html",
    # one regex per line; matching image URLs are ignored when comparing.
    # Default: auto-generated video-player thumbnails (load flakily, not real
    # page content - removing a video still shows up via the text layer).
    "ignore_image_patterns": r"/jcr:content/renditions/cq5dam\.thumbnail\.",
}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect():
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    SNAP_DIR.mkdir(exist_ok=True)
    CHANGE_DIR.mkdir(exist_ok=True)
    con = connect()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS urls(
        id INTEGER PRIMARY KEY,
        url TEXT UNIQUE NOT NULL,
        label TEXT DEFAULT '',
        active INTEGER DEFAULT 1,
        created_at TEXT,
        last_checked TEXT,
        last_status TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS snapshots(
        id INTEGER PRIMARY KEY,
        url_id INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
        ts TEXT,
        dir TEXT,
        status TEXT,
        error TEXT DEFAULT '',
        page_title TEXT DEFAULT '',
        text_lines INTEGER DEFAULT 0,
        image_count INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS changes(
        id INTEGER PRIMARY KEY,
        url_id INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
        ts TEXT,
        old_snapshot_id INTEGER,
        new_snapshot_id INTEGER,
        text_changed INTEGER DEFAULT 0,
        images_changed INTEGER DEFAULT 0,
        visual_changed INTEGER DEFAULT 0,
        pixel_ratio REAL DEFAULT 0,
        height_delta INTEGER DEFAULT 0,
        lines_added INTEGER DEFAULT 0,
        lines_removed INTEGER DEFAULT 0,
        images_added INTEGER DEFAULT 0,
        images_removed INTEGER DEFAULT 0,
        summary TEXT DEFAULT '',
        dir TEXT DEFAULT '',
        reviewed INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS runs(
        id INTEGER PRIMARY KEY,
        started TEXT,
        finished TEXT,
        kind TEXT,
        total INTEGER DEFAULT 0,
        ok INTEGER DEFAULT 0,
        changed INTEGER DEFAULT 0,
        errors INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    -- every AUTOMATIC level change, so hierarchy assignments are auditable:
    -- the user can see exactly what moved, when, and why - and that nothing
    -- moved at all on a run where the site's structure did not change
    CREATE TABLE IF NOT EXISTS level_log(
        id INTEGER PRIMARY KEY,
        ts TEXT,
        url_id INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
        old_level TEXT DEFAULT '',
        new_level TEXT DEFAULT '',
        src TEXT DEFAULT '',
        why TEXT DEFAULT ''
    );
    -- searchable index of every library item of every page's current snapshot
    CREATE TABLE IF NOT EXISTS items(
        id INTEGER PRIMARY KEY,
        url_id INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
        category TEXT,
        grp TEXT DEFAULT '',
        title TEXT,
        item_url TEXT DEFAULT '',
        meta TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_items_url ON items(url_id);
    CREATE INDEX IF NOT EXISTS idx_items_title ON items(title);
    CREATE INDEX IF NOT EXISTS idx_items_cat ON items(category);
    -- the content filters ask EXISTS(items WHERE url_id=? AND category=?) once
    -- per url; without this composite index that was a 37-SECOND scan of the
    -- items table per dashboard load. With it, each check is a single seek.
    CREATE INDEX IF NOT EXISTS idx_items_url_cat ON items(url_id, category);
    -- the content library counts one DISTINCT item per category, which is a
    -- GROUP BY lower(title), item_url. Sorting 199k rows per tile cost ~165 ms
    -- each; indexing the grouped expression itself turns that into a 25 ms
    -- index scan.
    CREATE INDEX IF NOT EXISTS idx_items_cat_ltitle
        ON items(category, lower(title), item_url);
    -- the dashboard asks "how many pages have documents / images / ..." as one
    -- GROUP BY category with COUNT(DISTINCT url_id). idx_items_url_cat has the
    -- columns the wrong way round for that, so it scanned the table (160 ms);
    -- this way round it is a covering index scan (16 ms).
    CREATE INDEX IF NOT EXISTS idx_items_cat_url ON items(category, url_id);
    -- the item page looks an asset up by what it IS (title + link) rather than
    -- by one of its per-section keys, so that a thing filed under two headings
    -- still lists every page it sits on. Without the category prefix the index
    -- above cannot serve that, and it was a 109 ms full scan per view.
    CREATE INDEX IF NOT EXISTS idx_items_ltitle_url
        ON items(lower(title), item_url);
    -- Substring search over item titles/meta/urls. A LIKE '%foo%' cannot use an
    -- index, so searching the library scanned all ~199k rows: two full seconds
    -- per query. The TRIGRAM tokenizer is the one FTS5 mode that indexes
    -- arbitrary substrings, so it keeps LIKE's exact semantics - including
    -- part-number fragments like "4248" inside CY8C4248LQI, which a word
    -- tokenizer would miss entirely. One row per DISTINCT item.
    CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
        item_key UNINDEXED, title, meta, item_url, tokenize='trigram');
    -- what the index was built from, so a stale one can be spotted and the
    -- search fall back to scanning rather than answer from a stale index
    CREATE TABLE IF NOT EXISTS items_fts_state(
        k INTEGER PRIMARY KEY CHECK(k=1), max_id INTEGER, n_rows INTEGER);
    CREATE INDEX IF NOT EXISTS idx_snapshots_url ON snapshots(url_id, ts);
    -- full text of every page's newest snapshot (rowid = url_id), so search
    -- can look INSIDE the pages, not just at their item titles
    CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(txt);
    CREATE INDEX IF NOT EXISTS idx_changes_url ON changes(url_id, ts);
    """)
    # lightweight migrations for databases created by older versions
    _ensure_column(con, "urls", "last_http", "INTEGER")
    _ensure_column(con, "snapshots", "http_status", "INTEGER")
    # rejected=1: reviewer said the OLD version was correct - this snapshot is
    # kept for the record but never used as a comparison baseline.
    _ensure_column(con, "snapshots", "rejected", "INTEGER DEFAULT 0")
    _ensure_column(con, "changes", "resolution", "TEXT DEFAULT ''")
    # WHICH named components changed in this review item, as a JSON list
    # ("Hero image added", "News & highlights (+2/-1)", "page text") - so a
    # review reads as a list of real things, not one opaque "something changed"
    _ensure_column(con, "changes", "components", "TEXT DEFAULT ''")
    # page type: Product / Application / Design resources / Promo / Part
    _ensure_column(con, "urls", "ptype", "TEXT DEFAULT ''")
    # Infineon's own page level from the source report's "Page Type" column:
    # PC > PFP > PSFP > PEFP > PTP for products (parts are PTPs and belong
    # INSIDE the product tree), ADP > AOP > ASP / AFB for applications.
    # Never inferred from URL depth - the sheet has PEFPs at 2, 3, 4 and 5
    # path segments, so depth says nothing about level.
    _ensure_column(con, "urls", "level", "TEXT DEFAULT ''")
    # the page's long/canonical URL (from its own breadcrumb): the true
    # hierarchy address behind a shortened or language-prefixed link. Used to
    # refuse duplicates at add time, by search, and to build author links.
    _ensure_column(con, "urls", "canonical_url", "TEXT DEFAULT ''")
    # a manual display-name override for a page whose own <title> reads poorly
    # (the homepage titles itself "Semiconductor & System Solutions"; we'd
    # rather show "Infineon"). Wins over the captured page_title everywhere the
    # name is shown, and survives re-capture since it lives on the url row.
    _ensure_column(con, "urls", "title_override", "TEXT DEFAULT ''")
    # where the level came from: report (the source sheet), inferred (the
    # sibling/hierarchy/fingerprint algorithm) or manual (the user's own edit)
    _ensure_column(con, "urls", "level_src", "TEXT DEFAULT ''")
    con.execute("UPDATE urls SET level_src='report'"
                " WHERE level<>'' AND IFNULL(level_src,'')=''")
    # the page's own JSON-LD breadcrumb, kept on the row so the per-type
    # hierarchy is one query rather than 4,145 file reads
    _ensure_column(con, "urls", "breadcrumb", "TEXT DEFAULT ''")
    # presence/emptiness of blocks whose CONTENT we deliberately do not store
    # (parametric OPN table, developer community) - booleans only
    _ensure_column(con, "urls", "flags", "TEXT DEFAULT ''")
    # when a listed address 301'd here, the address it came from - so the row
    # can wear a "redirect" sign: we track the destination, not the stale link
    _ensure_column(con, "urls", "redirect_from", "TEXT DEFAULT ''")
    # ownership metadata from the CSS Pages report (per page type): BL business
    # line, PL product line, CM content manager, Owner. Imported by URL match,
    # then hand-editable on the page like the level. meta_flag holds an import
    # warning (e.g. a PTP whose PL disagrees with its product family's).
    for _c in ("bl", "pl", "cm", "owner", "meta_flag"):
        _ensure_column(con, "urls", _c, "TEXT DEFAULT ''")
    # the page's own section-nav = the authoritative list of components it
    # carries (JSON list); plus the Product-details sub-tabs and price text.
    _ensure_column(con, "urls", "components", "TEXT DEFAULT ''")
    _ensure_column(con, "urls", "detail_tabs", "TEXT DEFAULT ''")
    _ensure_column(con, "urls", "price", "TEXT DEFAULT ''")
    # the image ASSIGNED to an item (a partner's logo) - previewable
    _ensure_column(con, "items", "img", "TEXT DEFAULT ''")
    # stable id for "this exact item, wherever it appears" - a document lives
    # on many pages and needs one page of its own
    _ensure_column(con, "items", "item_key", "TEXT DEFAULT ''")
    # the document's own Type column ("Datasheet", "Application note"): what
    # decides DAM vs Windchill, kept as its own field rather than parsed back
    # out of the meta string
    _ensure_column(con, "items", "doc_type", "TEXT DEFAULT ''")
    con.execute("CREATE INDEX IF NOT EXISTS idx_items_key ON items(item_key)")
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
    con.commit()
    con.close()


def _ensure_column(con, table, col, decl):
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def get_settings():
    con = connect()
    rows = con.execute("SELECT key, value FROM settings").fetchall()
    con.close()
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in rows})
    return s


def set_setting(key, value):
    con = connect()
    con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, str(value)))
    con.commit()
    con.close()


# Query params that are TRACKING/TOOLING noise, never page identity: campaign
# tags (intc, utm_*, gclid, fbclid), Infineon's redirect bookkeeping (redirId)
# and AEM's author-mode toggle (wcmmode). Left on, one page gets tracked twice
# - once clean, once with a campaign tag the site glued onto a redirect.
_JUNK_PARAMS = {"intc", "redirid", "redir_id", "wcmmode", "gclid", "fbclid",
                "ef_id", "s_kwcid", "msclkid", "mkt_tok", "mc_cid", "mc_eid"}

_INFINEON_HOST = re.compile(r"^https?://([a-z0-9-]+\.)*infineon\.com(/|$)", re.I)


def strip_junk_params(u):
    """The URL as the watch list should hold it: for infineon.com, WITHOUT any
    query string at all.

    Nothing after a '?' has ever been part of an infineon page's identity - the
    query is always a campaign tag, a redirect's own bookkeeping, or an
    author-mode flag, and the page answers the same without it. Naming the junk
    params instead was always one campaign behind: ef_id only got listed after
    it had already made /home-appliances a second page beside the clean one.
    Dropping the whole query closes that off for tags nobody has seen yet.

    This is the WATCH LIST rule only. Links found ON a page keep their query -
    thousands of design-resource items point at third-party tools where
    ?product_id= IS the address. Off-site hosts keep the named-junk behaviour
    for the same reason.
    """
    base, _, query = (u or "").partition("?")
    if not query:
        return u
    frag = ""
    if "#" in query:
        query, _, frag = query.partition("#")
    if _INFINEON_HOST.match(base or ""):
        keep = []
    else:
        keep = [p for p in query.split("&") if p and
                p.split("=")[0].strip().lower() not in _JUNK_PARAMS and
                not p.split("=")[0].strip().lower().startswith("utm_")]
    out = base + ("?" + "&".join(keep) if keep else "")
    return out + ("#" + frag if frag else "")


def items_fts_state(con):
    """(max item id, item row count) - what a fresh index would be built from."""
    r = con.execute("SELECT IFNULL(MAX(id),0), COUNT(*) FROM items").fetchone()
    return (r[0], r[1])


def items_fts_fresh(con):
    """Is the substring index still describing the items table?

    Cheap on purpose (~3 ms): the search asks this on every query and falls
    back to a plain scan when the answer is no, so being WRONG here is a
    correctness bug while being conservative only costs speed.
    """
    r = con.execute("SELECT max_id, n_rows FROM items_fts_state"
                    " WHERE k=1").fetchone()
    return bool(r) and (r["max_id"], r["n_rows"]) == items_fts_state(con)


def rebuild_items_fts(con=None):
    """Rebuild the substring index from the items table. Cheap enough (~3 s for
    20k distinct items) to just redo wholesale whenever items have moved."""
    own = con is None
    con = con or connect()
    try:
        con.execute("DELETE FROM items_fts")
        con.execute(
            "INSERT INTO items_fts(item_key, title, meta, item_url)"
            " SELECT item_key, IFNULL(title,''), IFNULL(meta,''),"
            "        IFNULL(item_url,'') FROM items"
            " WHERE item_key<>'' AND category<>'about' GROUP BY item_key")
        mx, n = items_fts_state(con)
        con.execute("INSERT OR REPLACE INTO items_fts_state(k, max_id, n_rows)"
                    " VALUES(1, ?, ?)", (mx, n))
        con.commit()
        return con.execute("SELECT COUNT(*) FROM items_fts").fetchone()[0]
    finally:
        if own:
            con.close()


def add_urls(url_list):
    """Insert URLs, ignoring duplicates. Returns (added, skipped)."""
    return add_urls_labeled([(u, "") for u in url_list])


# infineon.com language mirrors (/de/, /ja/, …) are the SAME page - always
# track the language-less address, never both
_LANG_SEG = re.compile(
    r"^(https?://(?:www\.)?infineon\.com)/"
    r"(de|ja|jp|cn|zh|tw|ko|kr|fr|it|es|pt|ru)(/|$)", re.I)


def strip_lang(url):
    """https://www.infineon.com/de/x -> https://www.infineon.com/x"""
    return _LANG_SEG.sub(r"\1\3", (url or "").strip())


_DR_ROOTS = ("/design-resources/", "/design-support/")


def dr_level_for(url):
    """Design-resource page level from its URL: the hierarchy depth under
    /design-resources/ or /design-support/. 1 seg = Category (a landing page
    with the category grid), 2 = Family, 3+ = Sub-Family. A softwaretools tool
    page or the homepage is not a hierarchy page -> no level."""
    root = next((r for r in _DR_ROOTS if r in (url or "")), None)
    if not root:
        return ""
    n = len([x for x in url.split(root, 1)[1].rstrip("/").split("/") if x])
    return "DRC" if n <= 1 else "DRF" if n == 2 else "DRSF"


def type_level_for(url):
    """(ptype, level, level_src) fully derivable from a URL - the ONE source of
    truth used at add time AND re-applied at capture, so a page that somehow
    entered without a type self-heals. Product/Application LEVELS are left blank
    (not URL-derivable - they come from the report or the inference engine)."""
    pt = ptype_for_url(url)
    lu = (url or "").lower()
    if pt == "Part":
        lvl = "EB" if "/evaluation-board/" in lu else "PTP"
    elif pt == "Design resources":
        lvl = dr_level_for(url)
    elif pt == "Home":
        lvl = "HOME"          # the root wears its own tag, like PTP or DRC
    else:
        lvl = ""
    return pt, lvl, ("inferred" if lvl else "")


def ptype_for_url(u):
    """The page type a URL belongs to, read from its shape - so a link added
    by hand gets typed exactly like the report-imported ones and every
    type-driven rule (shapes, badges, components) applies from day one."""
    u = (u or "").lower()
    host = u.split("//", 1)[-1].split("/", 1)[0]
    path = "/" + u.split("//", 1)[-1].split("/", 1)[-1] if "/" in u.split("//", 1)[-1] else "/"
    if "softwaretools.infineon.com" in host:
        return "Design resources"
    # the bare site root is its own kind of page: every hierarchy starts
    # from it, it belongs to none of them
    if host in ("www.infineon.com", "infineon.com") and path.rstrip("/") == "":
        return "Home"
    for prefix, t in (("/part/", "Part"), ("/evaluation-board/", "Part"),
                      ("/products/", "Product"),
                      ("/applications/", "Application"),
                      ("/application/", "Application"),
                      ("/design-resources/", "Design resources"),
                      ("/design-support/", "Design resources"),
                      ("/gated/", "Promo"), ("/document-promo/", "Promo"),
                      ("/promo", "Promo")):
        if path.startswith(prefix):
            return t
    return ""


def add_urls_labeled(pairs):
    """Insert (url, label) pairs. Language-prefixed infineon URLs are
    normalised first, and a URL matching an existing row's url OR its stored
    canonical (long) URL is refused as a duplicate. A label is backfilled onto
    an existing URL that has none. Returns (added, skipped)."""
    con = connect()
    added = 0
    total = 0
    for u, label in pairs:
        u = strip_junk_params(strip_lang((u or "").strip()))
        label = (label or "").strip()[:200]
        if not u:
            continue
        total += 1
        bare = u.rstrip("/")
        dup = con.execute(
            "SELECT id FROM urls WHERE rtrim(url,'/')=?"
            " OR (canonical_url<>'' AND rtrim(canonical_url,'/')=?)",
            (bare, bare)).fetchone()
        if dup:
            if label:
                con.execute("UPDATE urls SET label=? WHERE id=? AND label=''",
                            (label, dup["id"]))
            continue
        pt, lvl, lsrc = type_level_for(u)
        cur = con.execute(
            "INSERT OR IGNORE INTO urls(url, label, created_at, ptype,"
            " level, level_src) VALUES(?, ?, ?, ?, ?, ?)",
            (u, label, now_iso(), pt, lvl, lsrc))
        added += cur.rowcount
    con.commit()
    con.close()
    return added, total - added
