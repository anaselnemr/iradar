"""SQLite database layer for iRadar."""
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# data/ lives at the REPO root - one level above this package - because
# that is where the server's volume mounts (/app/data) and where every
# existing installation already keeps it. Deriving it from this file's own
# folder after the move into iradar/ would have pointed production at an
# empty /app/iradar/data while the real database sat right there.
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SNAP_DIR = DATA_DIR / "snapshots"


def shot_path(snap_dir):
    """The screenshot inside a snapshot directory, whatever format it is in.

    Screenshots are WEBP (lossless) since 6 Sep 2026 - PNG kept 1.19 GB of
    palette-quantised pixels on a 5 GB volume for no gain, and lossless webp
    of the same pixels is 42% smaller with a bit-identical decode, which the
    pixel diff requires: a lossy format would move the pixel ratio for reasons
    that have nothing to do with the page. Every stored .png was converted,
    but the fallback stays - it costs one stat() and it is what makes a run
    that is mid-flight during a deploy, or a directory the migration could not
    read, compare correctly instead of silently scoring 100% changed."""
    from pathlib import Path as _P
    d = _P(snap_dir)
    webp = d / "shot.webp"
    return webp if webp.exists() else d / "shot.png"
CHANGE_DIR = DATA_DIR / "changes"
DB_PATH = DATA_DIR / "watch.db"

DEFAULT_SETTINGS = {
    "frequency": "weekly",        # daily | bidaily | weekly
    "weekday": "mon",             # for weekly
    "hour": "6",                  # 24h clock, local time
    "concurrency": "4",           # parallel browser tabs
    # How many past captures to keep per page - the single number that decides
    # whether the archive grows without limit (it reached 25 GB in two weeks
    # before there was one).
    #
    # 2, measured rather than guessed: a capture is 267 KB on the real archive,
    # so across 4,188 pages 2 settles near 2.7 GB and 3 near 3.8 GB. On a 5 GB
    # volume that is the difference between 58% and 82% full. Nothing is lost
    # at 2 - a diff needs the current capture and the one before it, which is
    # exactly what 2 keeps; the third was a spare nothing routinely reads.
    #
    # A page's newest capture, anything an open update still needs, and
    # anything mid-check are always kept regardless of this.
    "keep_snapshots": "2",
    "visual_threshold": "2.5",    # % of pixels that must differ to flag a visual change
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
    # Part and evaluation-board pages are generated from PIM, not authored in
    # AEM - so their badge points here instead of at the author editor.
    "pim_url": "",
    "windchill_search_url": "",
    # AEM author editor: '{path}' = the page's public path (after /en), the
    # editor wants a trailing .html - all of it editable in Settings
    "aem_author_url": "https://author.infineon.com/editor.html/content/ifx/language-masters/en{path}.html",
    # one regex per line; matching image URLs are ignored when comparing.
    # Default: auto-generated video-player thumbnails (load flakily, not real
    # page content - removing a video still shows up via the text layer).
    "ignore_image_patterns": r"/jcr:content/renditions/cq5dam\.thumbnail\.",
    # Editorial limits, per content block: the longest a single block may be,
    # and how many blocks there should be. 0 means no bound, so a rule can be
    # switched off from Settings without removing it. Judged only on blocks a
    # page actually has - see compare.copy_findings.
    "copy_about_chars": "450",
    "copy_about_min": "1",
    "copy_about_max": "0",
    "copy_subheadline_chars": "150",
    "copy_subheadline_min": "1",
    "copy_subheadline_max": "0",
    "copy_meta_description_chars": "150",
    "copy_meta_description_min": "1",
    "copy_meta_description_max": "0",
    "copy_features_chars": "40",
    "copy_features_min": "2",
    "copy_features_max": "12",
    "copy_benefits_chars": "40",
    "copy_benefits_min": "2",
    "copy_benefits_max": "12",
    "copy_overview_chars": "450",
    "copy_overview_min": "1",
    "copy_overview_max": "0",
}


def reconcile_live_statuses(con):
    """Repair stale offline verdicts confirmed by the latest settled capture.

    Keep open changes in the queue; never treat a pending, rejected or failed
    capture as a live verdict. Snapshot files may have been pruned, but their
    recorded HTTP result stays.
    """
    return con.execute(
        "UPDATE urls SET last_status=CASE WHEN EXISTS"
        " (SELECT 1 FROM changes c WHERE c.url_id=urls.id AND c.reviewed=0)"
        " THEN 'changed' ELSE 'unchanged' END"
        " WHERE last_status='notlive' AND last_http BETWEEN 200 AND 399"
        " AND EXISTS (SELECT 1 FROM snapshots s WHERE s.url_id=urls.id"
        " AND s.id=(SELECT MAX(s2.id) FROM snapshots s2 WHERE s2.url_id=urls.id)"
        " AND s.status='ok' AND IFNULL(s.rejected,0)=0"
        " AND s.http_status BETWEEN 200 AND 399"
        " AND s.ts>=urls.last_checked)"
    ).rowcount


def dashboard_counts(con):
    """The same current inventory and health readings for web and email."""
    return con.execute("""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS watched,
          SUM(CASE WHEN active=1 AND last_checked IS NOT NULL
                   THEN 1 ELSE 0 END) AS captured_active,
          SUM(CASE WHEN last_status='changed' AND active=1 THEN 1 ELSE 0 END) AS changed,
          SUM(CASE WHEN last_status='unchanged' AND active=1 THEN 1 ELSE 0 END) AS unchanged,
          SUM(CASE WHEN last_http BETWEEN 200 AND 399
                   AND IFNULL(last_status,'') NOT IN ('blank','error')
                   THEN 1 ELSE 0 END) AS live,
          SUM(CASE WHEN last_checked IS NOT NULL THEN 1 ELSE 0 END) AS checked,
          SUM(CASE WHEN last_status='rejected' THEN 1 ELSE 0 END) AS rejected,
          SUM(CASE WHEN last_status='error' THEN 1 ELSE 0 END) AS errors,
          SUM(CASE WHEN active=0 THEN 1 ELSE 0 END) AS paused,
          SUM(CASE WHEN active=0 AND last_status='blank' THEN 1 ELSE 0 END) AS paused_blank,
          SUM(CASE WHEN active=0 AND last_http IN (404,410) THEN 1 ELSE 0 END) AS paused_offline,
          SUM(CASE WHEN last_checked IS NULL THEN 1 ELSE 0 END) AS fresh,
          SUM(CASE WHEN last_status='blank' THEN 1 ELSE 0 END) AS blank,
          -- not live = the server says GONE (404/410). Blank bodies and
          -- blocked captures (403/429/5xx) are their own states, never this
          SUM(CASE WHEN last_http IN (404,410)
                   THEN 1 ELSE 0 END) AS notlive
        FROM urls""").fetchone()


def run_pages_delta(con, run, tracked=None):
    """Inventory change since the preceding full check, independent of pauses."""
    if tracked is None:
        tracked = con.execute("SELECT COUNT(*) FROM urls").fetchone()[0]
    prev = con.execute(
        "SELECT finished, tracked_total FROM runs WHERE finished IS NOT NULL AND id<?"
        " AND total>=50 ORDER BY id DESC LIMIT 1", (run["id"],)).fetchone()
    if not prev:
        return 0
    if prev["tracked_total"] is not None:
        return tracked - prev["tracked_total"]
    # Legacy runs recorded workload, not inventory; use actual additions.
    return con.execute("SELECT COUNT(*) FROM urls WHERE created_at>?",
                       (prev["finished"],)).fetchone()[0]


def inventory_counts(con):
    """All tracked pages and genuine HTTP 404/410s, including paused pages."""
    row = con.execute(
        "SELECT COUNT(*) total, COALESCE(SUM(last_http IN (404,410)),0) offline"
        " FROM urls").fetchone()
    return row["total"], row["offline"]


def finish_run_counts(con, run_id):
    total, offline = inventory_counts(con)
    con.execute("UPDATE runs SET tracked_total=?, offline_after=? WHERE id=?",
                (total, offline, run_id))


def run_offline_delta(con, run):
    """Offline arrivals minus recoveries during this completed run.

    New runs retain both counters so pruning snapshots cannot erase the
    comparison. Older runs use HTTP transitions in retained capture history,
    bounded by the run's end so later checks cannot change its result.
    """
    if run["offline_before"] is not None and run["offline_after"] is not None:
        return run["offline_after"] - run["offline_before"]
    row = con.execute("""
        WITH before_run AS (
          SELECT url_id, MAX(id) id FROM snapshots
          WHERE ts<? AND status='ok' AND IFNULL(rejected,0)=0 GROUP BY url_id
        ), in_run AS (
          SELECT url_id, MAX(id) id FROM snapshots
          WHERE ts>=? AND ts<=? AND status='ok' AND IFNULL(rejected,0)=0
          GROUP BY url_id
        )
        SELECT COALESCE(SUM(CASE
          WHEN current.http_status IN (404,410)
               AND (previous.http_status BETWEEN 200 AND 399
                    OR (previous.id IS NULL AND u.created_at>=?)) THEN 1
          WHEN current.http_status BETWEEN 200 AND 399
               AND previous.http_status IN (404,410) THEN -1
          ELSE 0 END),0)
        FROM in_run r JOIN snapshots current ON current.id=r.id
        JOIN urls u ON u.id=r.url_id
        LEFT JOIN before_run b ON b.url_id=r.url_id
        LEFT JOIN snapshots previous ON previous.id=b.id
    """, (run["started"], run["started"], run["finished"], run["started"])
    ).fetchone()
    return row[0]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect():
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    # A CONNECTION PER REQUEST, against a 396 MB database. Every request opens
    # one and closes it, so on stock settings nothing cached here ever survived
    # long enough to help - which is why these are set at all. The numbers
    # below were taken on a laptop SSD with the OS holding the whole file in
    # memory; the server keeps this database on a mounted volume, where the
    # reads they save are the expensive kind, so it should do better there.
    #
    # SIZED FOR A MEMORY BILL, not for the best number on a benchmark. The
    # host charges by gigabyte-minute, and the app sits idle about 158 hours
    # of every 168 - so the resting size of this process costs more over a
    # month than the weekly capture run does (62% against 38%). A cache that
    # buys 50 ms and carries a hundred megabytes all week is a bad trade here.
    #
    # Measured on a copy of the real database, peak RSS against page time:
    #
    #   settings                 RSS     dashboard   hierarchy   export counts
    #   stock                    77 MB     402 ms      397 ms       229 ms
    #   64 MB cache + 256 MB mmap 109 MB    242 ms      286 ms       105 ms
    #   16 MB cache, no mmap      82 MB     295 ms      298 ms       143 ms
    #   8 MB cache, no mmap       77 MB     441 ms      435 ms       226 ms
    #
    # 16 MB keeps most of the win for a sixth of the memory; 8 MB falls off a
    # cliff back to stock, so the useful working set for these queries sits
    # between the two. That is one connection - the web server runs eight
    # threads and each request opens its own, so the figure that matters is
    # this one multiplied, which is the whole reason 64 MB was the wrong call.
    #
    # cache_size  2 MB of pages is nothing against 200k items, and the
    #             GROUP BY / COUNT(DISTINCT) passes the dashboard and the
    #             library tiles run re-read the same pages all the way through.
    # temp_store  those passes sort. In MEMORY rather than into a file on the
    #             volume, which is the slowest disk this app touches. Costs
    #             nothing at rest: it is only held during a sort.
    # mmap_size   dropped. It made reads cheaper by mapping the database into
    #             the process, but a mapped page is a charged page - the host
    #             counts the file cache in the container's memory - so it was
    #             billing for up to 256 MB to save a few milliseconds.
    # synchronous NORMAL is the setting WAL is designed around: a commit still
    #             goes to the WAL, it just stops fsync-ing on every one. The
    #             documented loss is the last commits on a power cut, never a
    #             corrupt file - and the whole archive is rebuildable by a
    #             re-capture anyway. FULL costs an fsync per page, and a run
    #             commits once per page, 4,266 times a night. Free in memory.
    con.execute("PRAGMA cache_size=-16384")        # 16 MB per connection
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA synchronous=NORMAL")
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
    -- per-page state within a run, so an interrupted run resumes instead of
    -- redoing captured work. Each row moves queued -> done (settled: saved,
    -- unchanged, error) or queued -> suspect (held for the double-check) ->
    -- done. If the process dies mid-run, the next start reads these: pages
    -- already 'done' are skipped, 'suspect' pages resume their verify, and only
    -- 'queued' pages are captured. prev_snap_id / snap_id let a held page's
    -- verify be rebuilt without the in-memory suspect list.
    CREATE TABLE IF NOT EXISTS run_items(
        run_id INTEGER NOT NULL,
        url_id INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued',   -- queued | suspect | done
        prev_snap_id INTEGER,                   -- baseline, for a held suspect
        snap_id INTEGER,                        -- the (pending) capture
        PRIMARY KEY (run_id, url_id)
    );
    CREATE INDEX IF NOT EXISTS idx_run_items_state ON run_items(run_id, state);
    -- Asset fingerprints: a document or image republished at the SAME URL is
    -- invisible to the page watch (the URL set did not change). A HEAD pass
    -- after each run stores each file's server validators; when they move,
    -- that asset's bytes changed even though no page looked different.
    CREATE TABLE IF NOT EXISTS assets(
        url TEXT PRIMARY KEY,
        kind TEXT NOT NULL DEFAULT '',          -- image | document
        etag TEXT NOT NULL DEFAULT '',
        last_modified TEXT NOT NULL DEFAULT '',
        content_length INTEGER,
        http_status INTEGER,
        first_seen TEXT NOT NULL DEFAULT '',
        last_checked TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS asset_changes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT '',
        ts TEXT NOT NULL,
        old_etag TEXT NOT NULL DEFAULT '',  new_etag TEXT NOT NULL DEFAULT '',
        old_modified TEXT NOT NULL DEFAULT '', new_modified TEXT NOT NULL DEFAULT '',
        old_length INTEGER, new_length INTEGER,
        reviewed INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_asset_changes_rev ON asset_changes(reviewed, ts);
    -- What the world says about the products on the watched pages: one row
    -- per (source, url, entity) - a StackExchange question, a Reddit thread,
    -- an HN comment, a thread from Infineon's own developer community, or an
    -- LLM's web-grounded summary. score/topics stay NULL/'' until an LLM key
    -- is configured; mentions are still collected and browsable unscored.
    CREATE TABLE IF NOT EXISTS mentions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity TEXT NOT NULL,
        source TEXT NOT NULL,
        url TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        author TEXT NOT NULL DEFAULT '',
        posted TEXT NOT NULL DEFAULT '',
        fetched TEXT NOT NULL DEFAULT '',
        score REAL,
        topics TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        UNIQUE(source, url, entity)
    );
    CREATE INDEX IF NOT EXISTS idx_mentions_entity ON mentions(entity, source);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    -- team accounts. Password is never stored - pw_hash holds a salted scrypt
    -- (or pbkdf2) digest from werkzeug. role is 'admin' or 'editor': an editor
    -- reviews updates and edits page metadata, an admin additionally manages
    -- urls, settings, capture runs and these accounts. must_change_pw is set on
    -- every seeded or admin-reset account, so the shared starting password
    -- cannot survive first login.
    -- Notes people leave on a page or an asset, so a decision has somewhere to
    -- live. Deliberately NOT keyed by a foreign key: an item is identified by
    -- the ASSET it is (title + link), which is how the item page itself is
    -- keyed - the same datasheet filed under two headings has two item_keys
    -- and must still show one conversation.
    --
    -- author is frozen text, not just a user_id: a note has to keep saying who
    -- wrote it after that person's account is removed, and re-reading a name
    -- through a join cannot do that. user_id stays for "is this mine".
    CREATE TABLE IF NOT EXISTS comments(
        id INTEGER PRIMARY KEY,
        target_kind  TEXT NOT NULL,        -- 'page' | 'item'
        target_id    TEXT NOT NULL,        -- page id, or the asset's identity
        target_label TEXT NOT NULL DEFAULT '',
        user_id      INTEGER,
        author       TEXT NOT NULL DEFAULT '',
        body         TEXT NOT NULL,
        created      TEXT NOT NULL DEFAULT '',
        edited       TEXT NOT NULL DEFAULT '',
        resolved     INTEGER NOT NULL DEFAULT 0,
        resolved_by  TEXT NOT NULL DEFAULT '',
        resolved_at  TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_comments_target
        ON comments(target_kind, target_id, id);
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,          -- always stored lowercased
        name TEXT NOT NULL DEFAULT '',
        pw_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'editor',
        active INTEGER NOT NULL DEFAULT 1,
        must_change_pw INTEGER NOT NULL DEFAULT 1,
        created TEXT DEFAULT '',
        last_login TEXT DEFAULT '',
        -- last_login only moves when somebody submits the SIGN-IN FORM. A
        -- session lasts days, so a person can use iRadar every morning for a
        -- week and still show a week-old "last sign-in". last_seen is stamped
        -- while they are actually using it, which is the question an admin is
        -- really asking.
        last_seen TEXT DEFAULT ''
    );
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
    -- Who has opened the shared read-only link. ONE ROW PER VISIT, not per
    -- page view: `visit` is a random id minted into that browser's session
    -- when it first presents the key, so a person reading forty pages is one
    -- line rather than forty.
    --
    -- NO ADDRESS IS STORED, and none should be. Everyone arrives through one
    -- shared link, so an IP would be the only thing here capable of naming a
    -- person - which is a different thing from what this is for, and would
    -- make a log of colleagues' reading habits out of a "has anyone opened
    -- it". `agent` is a two-word summary ("Chrome on Windows"), derived and
    -- discarded, not the raw header.
    CREATE TABLE IF NOT EXISTS guest_visits(
        id INTEGER PRIMARY KEY,
        visit TEXT UNIQUE NOT NULL,
        first_seen TEXT,
        last_seen TEXT,
        hits INTEGER DEFAULT 0,
        agent TEXT DEFAULT '',
        last_path TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_guest_visits_seen
        ON guest_visits(last_seen DESC);
    """)
    # lightweight migrations for databases created by older versions
    # How to LINK BACK to the thing a note is about. An asset's note is keyed
    # on what the asset is (title + link), which cannot be turned back into a
    # url without searching every item - and doing that per note made the Notes
    # page 1.85s for 40 notes and would have been ~14s at 300. The key that was
    # open when the note was written is remembered here instead, so the link is
    # one lookup. It is a HINT, not the identity: if that key is gone the note
    # still belongs to the same asset.
    _ensure_column(con, "users", "last_seen", "TEXT DEFAULT ''")
    # 1 = this person wants the after-run digest emailed to them. Default 0:
    # nobody is signed up to receive mail they did not ask for.
    _ensure_column(con, "users", "email_opt_in", "INTEGER DEFAULT 0")
    _ensure_column(con, "comments", "target_hint", "TEXT DEFAULT ''")
    _ensure_column(con, "urls", "last_http", "INTEGER")
    # how many runs in a row this page has failed EVERY capture attempt -
    # the visible 'error' state waits for runner.ERROR_RUNS of these
    _ensure_column(con, "urls", "error_streak", "INTEGER DEFAULT 0")
    # set (with finished still NULL) when a person pressed End check while
    # suspects were held: the run is over for THEM, but its parked suspects
    # wait for the next check to finish their double-check
    _ensure_column(con, "runs", "stopped", "TEXT DEFAULT ''")
    # Capture workload excludes paused pages and cannot measure inventory.
    _ensure_column(con, "runs", "tracked_total", "INTEGER")
    _ensure_column(con, "runs", "offline_before", "INTEGER")
    _ensure_column(con, "runs", "offline_after", "INTEGER")
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
    # Which editorial limits this page's copy breaks, as a JSON list of short
    # findings. '' = never measured; '[]' = measured and within every limit,
    # which is a different fact and the filters rely on the difference.
    _ensure_column(con, "urls", "copy_flags", "TEXT DEFAULT ''")
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


def prune_history(keep=None, con=None, dry_run=False):
    """Keep only the newest `keep` captures of each page; delete the rest.

    Without this the archive grows by one full capture per page per run - about
    1.1 MB each, 94% of it the screenshot - which reached 25 GB across 145k
    files in two weeks. Runs automatically after every check.

    Four things are never deleted, whatever `keep` says:
      - a page's newest good capture, which the next run compares against
      - any capture an OPEN update points at, or its diff cannot be drawn
      - anything still being checked (status 'pending')
      - the row of a capture whose folder is already gone (nothing to reclaim)

    Returns (deleted, freed_bytes).
    """
    import shutil

    if keep is None:
        try:
            keep = int(get_settings().get("keep_snapshots", 3))
        except (TypeError, ValueError):
            keep = 3
    keep = max(1, keep)          # keeping zero would erase the next comparison

    own = con is None
    con = con or connect()
    try:
        protected = set()
        for r in con.execute("SELECT old_snapshot_id, new_snapshot_id FROM"
                             " changes WHERE IFNULL(reviewed,0)=0"):
            protected.update(x for x in (r[0], r[1]) if x)
        for r in con.execute("SELECT id FROM snapshots WHERE status='pending'"):
            protected.add(r[0])

        # newest first per page, so the first `keep` of each page survive
        doomed, seen = [], {}
        for s in con.execute("SELECT id, url_id, dir, status FROM snapshots"
                             " ORDER BY url_id, id DESC"):
            n = seen.get(s["url_id"], 0) + 1
            seen[s["url_id"]] = n
            if n <= keep or s["id"] in protected:
                continue
            doomed.append(s)

        freed = 0
        for s in doomed:
            d = Path(s["dir"]) if s["dir"] else None
            if d and d.exists():
                for f in d.rglob("*"):
                    if f.is_file():
                        try:
                            freed += f.stat().st_size
                        except OSError:
                            pass
                if not dry_run:
                    shutil.rmtree(d, ignore_errors=True)
            if not dry_run:
                con.execute("DELETE FROM snapshots WHERE id=?", (s["id"],))
        if doomed and not dry_run:
            con.commit()

        # The DIFF ARTWORK of finished updates. Every flagged update writes its
        # own before/after images - about 790 KB each - and nothing ever removed
        # them: only deleting a whole page did. At roughly 50 updates a week
        # that is 2 GB a year of pictures nobody can look at, because a reviewed
        # update whose captures have been pruned cannot render its diff anyway.
        # Tied to the captures rather than to a timer, so the artwork lives
        # exactly as long as the thing it illustrates.
        #
        # The change ROW stays. What happened and who decided it is the record;
        # the pictures are just how it was shown at the time.
        gone_art = 0
        for c in con.execute(
                "SELECT id, dir FROM changes WHERE IFNULL(reviewed,0)<>0"
                " AND IFNULL(dir,'')<>''").fetchall():
            still = con.execute(
                "SELECT 1 FROM snapshots s JOIN changes ch"
                " ON s.id IN (ch.old_snapshot_id, ch.new_snapshot_id)"
                " WHERE ch.id=? LIMIT 1", (c["id"],)).fetchone()
            if still:
                continue                 # its captures survive - so does the art
            d = Path(c["dir"])
            if not d.exists():
                continue
            for f in d.rglob("*"):
                if f.is_file():
                    try:
                        freed += f.stat().st_size
                    except OSError:
                        pass
            if not dry_run:
                shutil.rmtree(d, ignore_errors=True)
                con.execute("UPDATE changes SET dir='' WHERE id=?", (c["id"],))
            gone_art += 1
        if gone_art and not dry_run:
            con.commit()
        return len(doomed) + gone_art, freed
    finally:
        if own:
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
