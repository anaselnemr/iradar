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
    # Default: relative timestamps ("13 days ago") change daily by themselves.
    "ignore_patterns": r"^\d+ (day|hour|minute|week|month|year)s? ago$",
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
    CREATE INDEX IF NOT EXISTS idx_snapshots_url ON snapshots(url_id, ts);
    CREATE INDEX IF NOT EXISTS idx_changes_url ON changes(url_id, ts);
    """)
    # lightweight migrations for databases created by older versions
    _ensure_column(con, "urls", "last_http", "INTEGER")
    _ensure_column(con, "snapshots", "http_status", "INTEGER")
    # rejected=1: reviewer said the OLD version was correct - this snapshot is
    # kept for the record but never used as a comparison baseline.
    _ensure_column(con, "snapshots", "rejected", "INTEGER DEFAULT 0")
    _ensure_column(con, "changes", "resolution", "TEXT DEFAULT ''")
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
    # the page's own section-nav = the authoritative list of components it
    # carries (JSON list); plus the Product-details sub-tabs and price text.
    _ensure_column(con, "urls", "components", "TEXT DEFAULT ''")
    _ensure_column(con, "urls", "detail_tabs", "TEXT DEFAULT ''")
    _ensure_column(con, "urls", "price", "TEXT DEFAULT ''")
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


def add_urls_labeled(pairs):
    """Insert (url, label) pairs. Language-prefixed infineon URLs are
    normalised first, and a URL matching an existing row's url OR its stored
    canonical (long) URL is refused as a duplicate. A label is backfilled onto
    an existing URL that has none. Returns (added, skipped)."""
    con = connect()
    added = 0
    total = 0
    for u, label in pairs:
        u = strip_lang((u or "").strip())
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
        cur = con.execute(
            "INSERT OR IGNORE INTO urls(url, label, created_at) VALUES(?, ?, ?)",
            (u, label, now_iso()))
        added += cur.rowcount
    con.commit()
    con.close()
    return added, total - added
