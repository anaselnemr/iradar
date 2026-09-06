"""Inventory is independent of monitoring; offline means HTTP 404/410."""
from contextlib import contextmanager
from pathlib import Path
import tempfile

import db


@contextmanager
def database():
    names = ("DATA_DIR", "SNAP_DIR", "CHANGE_DIR", "DB_PATH")
    saved = {name: getattr(db, name) for name in names}
    with tempfile.TemporaryDirectory(prefix="iradar_stats_") as folder:
        root = Path(folder)
        db.DATA_DIR, db.SNAP_DIR = root, root / "snapshots"
        db.CHANGE_DIR, db.DB_PATH = root / "changes", root / "watch.db"
        db.init_db()
        con = db.connect()
        try:
            yield con
        finally:
            con.close()
            for name, value in saved.items():
                setattr(db, name, value)


def page(con, ident, http=200, status="unchanged", active=1,
         created="2026-08-01 00:00:00"):
    con.execute("INSERT INTO urls(id,url,last_http,last_status,active,created_at,"
                "last_checked) VALUES(?,?,?,?,?,?,?)",
                (ident, f"https://example.test/{ident}", http, status, active,
                 created, "2026-09-06 02:00:00"))


def snap(con, ident, http=200, ts="2026-09-06 02:00:00", status="ok", rejected=0):
    return con.execute("INSERT INTO snapshots(url_id,ts,dir,status,http_status,rejected)"
                       " VALUES(?,?,'',?,?,?)",
                       (ident, ts, status, http, rejected)).lastrowid


def run(con, ident=1, **counters):
    con.execute("INSERT INTO runs(id,started,finished,kind,total)"
                " VALUES(?,'2026-09-06 01:00:00','2026-09-06 03:00:00','scheduled',100)",
                (ident,))
    for key, value in counters.items():
        assert key in {"tracked_total", "offline_before", "offline_after"}
        con.execute(f"UPDATE runs SET {key}=? WHERE id=?", (value, ident))
    return con.execute("SELECT * FROM runs WHERE id=?", (ident,)).fetchone()


def test_inventory_and_offline_include_paused_but_not_blank_or_stale_status():
    with database() as con:
        page(con, 1, 404, "notlive", 0)
        page(con, 2, 410, "notlive")
        page(con, 3, 200, "blank")
        page(con, 4, 200, "notlive")
        page(con, 5, 503, "error")
        assert db.inventory_counts(con) == (5, 2)
        con.execute("UPDATE urls SET active=0 WHERE last_status='blank'")
        assert db.inventory_counts(con) == (5, 2)


def test_email_and_dashboard_share_all_seven_readings():
    import app
    import mailer
    with database() as con:
        page(con, 1, 404, "notlive", 0)
        page(con, 2, 200, "blank", 0)
        page(con, 3)
        page(con, 4, 200, "changed")
        page(con, 5, 503, "error")
        page(con, 6, 200, "unchanged", 0)
        con.execute("INSERT INTO runs(id,started,finished,total,tracked_total)"
                    " VALUES(1,'2026-08-20','2026-08-21',100,5)")
        r = run(con, 2, offline_before=2, offline_after=1)
        con.execute("INSERT INTO changes(url_id,ts,dir,reviewed,images_added)"
                    " VALUES(4,'2026-09-06 02:00:00','',0,2)")
        con.execute("INSERT INTO asset_changes(url,kind,ts,reviewed)"
                    " VALUES('https://example.test/doc.pdf','document','2026-09-06 02:00:00',0)")
        app._BAR_EXTRAS_CACHE.clear()
        counts = db.dashboard_counts(con)
        extra = app._bar_extras(con)
        report = mailer.run_report(con, r)
        assert report["pages"] == counts["total"] == 6
        assert report["live"] == counts["live"] == 3
        assert report["paused"] == counts["paused"] == 3
        assert report["paused_blank"] == counts["paused_blank"] == 1
        assert report["paused_offline"] == counts["paused_offline"] == 1
        assert report["notlive"] == counts["notlive"] == 1
        assert tuple(report[k] for k in ("updates","content_updates","file_updates",
                                        "pages_delta","offline_delta")) == extra
        assert report["pages_delta"] == 1 and report["offline_delta"] == -1
        assert report["open_updates"] == 1 and report["open_files"] == 1


def test_reconcile_only_settled_successes_preserving_open_reviews():
    with database() as con:
        for ident in range(1, 8):
            page(con, ident, 200, "notlive")
        snap(con, 1)
        sid = snap(con, 2)
        con.execute("INSERT INTO changes(url_id,ts,new_snapshot_id,dir,reviewed)"
                    " VALUES(2,'2026-09-06 02:00:00',?,'',0)", (sid,))
        snap(con, 3, status="pending")
        snap(con, 4, rejected=1)
        snap(con, 5, ts="2026-09-05 02:00:00")
        snap(con, 6)
        snap(con, 6, status="error")
        # 7 has no capture evidence, despite its HTTP metadata.
        assert db.reconcile_live_statuses(con) == 2
        assert [r[0] for r in con.execute("SELECT last_status FROM urls ORDER BY id")] == [
            "unchanged", "changed", "notlive", "notlive", "notlive", "notlive", "notlive"]
        assert con.execute("SELECT reviewed FROM changes").fetchone()[0] == 0
        assert db.reconcile_live_statuses(con) == 0


def test_healthy_recheck_clears_stale_offline_even_with_open_change():
    import runner
    with database() as con:
        page(con, 1, 200, "notlive")
        con.execute("INSERT INTO changes(url_id,ts,dir,reviewed) VALUES(1,'t','',0)")
        runner._set_checked(con, 1, "2026-09-06 04:00:00", "unchanged")
        assert con.execute("SELECT last_status FROM urls").fetchone()[0] == "changed"
        con.execute("UPDATE changes SET reviewed=1")
        runner._set_checked(con, 1, "2026-09-06 05:00:00", "unchanged")
        assert con.execute("SELECT last_status FROM urls").fetchone()[0] == "unchanged"


def test_run_counters_survive_pause_and_later_http_updates():
    with database() as con:
        page(con, 1, 404, "notlive", 0)
        page(con, 2)
        r = run(con, offline_before=1)
        con.execute("UPDATE urls SET last_http=410,last_status='notlive' WHERE id=2")
        db.finish_run_counts(con, r["id"])
        r = con.execute("SELECT * FROM runs").fetchone()
        assert r["tracked_total"] == 2 and r["offline_after"] == 2
        assert db.run_offline_delta(con, r) == 1
        con.execute("UPDATE urls SET active=0,last_http=200")
        assert db.run_offline_delta(con, r) == 1


def test_legacy_offline_delta_is_net_transitions_not_all_current_offline():
    with database() as con:
        for ident in range(1, 6):
            page(con, ident)
        page(con, 6, created="2026-09-06 01:30:00")
        # newly offline, recovered, still offline, blocked then offline,
        # old page with missing history, genuinely new offline page.
        for ident, before, after in [(1,200,404), (2,410,200), (3,404,404), (4,503,404)]:
            snap(con, ident, before, "2026-09-05 02:00:00")
            snap(con, ident, after)
        snap(con, 5, 404)
        snap(con, 6, 404)
        r = run(con)
        assert db.run_offline_delta(con, r) == 1
        snap(con, 1, 200, "2026-09-07 02:00:00")
        snap(con, 2, 404, "2026-09-07 02:00:00")
        assert db.run_offline_delta(con, r) == 1


def test_page_delta_uses_inventory_not_active_workload():
    import app
    with database() as con:
        for ident in range(1, 4):
            page(con, ident)
        con.execute("INSERT INTO runs(id,started,finished,total,tracked_total)"
                    " VALUES(1,'2026-08-20','2026-08-21',100,2)")
        run(con, 2)
        app._BAR_EXTRAS_CACHE.clear()
        assert app._bar_extras(con)[3] == 1
        con.execute("UPDATE urls SET active=0 WHERE id=3")
        app._BAR_EXTRAS_CACHE.clear()
        assert app._bar_extras(con)[3] == 1
        con.execute("DELETE FROM urls WHERE id IN (2,3)")
        app._BAR_EXTRAS_CACHE.clear()
        assert app._bar_extras(con)[3] == -1


def test_legacy_page_delta_counts_actual_additions_not_paused_pages():
    import app
    with database() as con:
        page(con, 1)
        page(con, 2, active=0, created="2026-09-04 00:00:00")
        con.execute("INSERT INTO runs(id,started,finished,total)"
                    " VALUES(1,'2026-08-31','2026-09-03',100)")
        run(con, 2)
        app._BAR_EXTRAS_CACHE.clear()
        assert app._bar_extras(con)[3] == 1
