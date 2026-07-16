"""SQLite database layer for InfineonWatch."""
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
    # the page's own JSON-LD breadcrumb, kept on the row so the per-type
    # hierarchy is one query rather than 4,145 file reads
    _ensure_column(con, "urls", "breadcrumb", "TEXT DEFAULT ''")
    # stable id for "this exact item, wherever it appears" - a document lives
    # on many pages and needs one page of its own
    _ensure_column(con, "items", "item_key", "TEXT DEFAULT ''")
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


def add_urls_labeled(pairs):
    """Insert (url, label) pairs, ignoring duplicate URLs. A label is also
    backfilled onto an existing URL that has none. Returns (added, skipped)."""
    con = connect()
    added = 0
    total = 0
    for u, label in pairs:
        u = (u or "").strip()
        label = (label or "").strip()[:200]
        if not u:
            continue
        total += 1
        cur = con.execute(
            "INSERT OR IGNORE INTO urls(url, label, created_at) VALUES(?, ?, ?)",
            (u, label, now_iso()))
        added += cur.rowcount
        if cur.rowcount == 0 and label:
            con.execute("UPDATE urls SET label=? WHERE url=? AND label=''",
                        (label, u))
    con.commit()
    con.close()
    return added, total - added
