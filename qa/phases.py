"""Autonomous capture pipeline.

  phase 1  one page of EVERY type      (smoke test across all page shapes)
  phase 2  10% of every type           (surface type-specific bugs fast)
  phase 3  the remaining 90%
  phase 4  retry anything that errored (blank renders, timeouts)

Each phase waits for the previous one to finish, drives the running server
(so the dashboard shows progress), and logs a per-type summary. Runs
detached: no babysitting needed.
"""
import json
import random
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "iradar"))
import db  # noqa: E402

LOG = Path(__file__).parent / "phases_log.jsonl"
BASE = "http://127.0.0.1:5000"
random.seed(20260716)


def log(obj):
    obj["ts"] = datetime.now().strftime("%H:%M:%S")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def status():
    with urllib.request.urlopen(BASE + "/status", timeout=20) as r:
        return json.loads(r.read().decode())


def wait_idle(label):
    """Block until no run is in flight."""
    while True:
        try:
            s = status()
            if not s.get("running"):
                return
            done, total = s.get("done", 0), s.get("total", 0)
            saved = s.get("saved", 0)
            eta = s.get("eta_s")
            log({"kind": "waiting", "phase": label, "done": done,
                 "total": total, "saved": saved,
                 "eta_min": round(eta / 60) if eta else None})
        except Exception:
            pass
        time.sleep(120)


def start(ids):
    data = [("action", "check"), ("next", "/")] + [("ids", str(i)) for i in ids]
    req = urllib.request.Request(
        BASE + "/urls/bulk", data=urllib.parse.urlencode(data).encode(),
        method="POST")
    urllib.request.urlopen(req, timeout=120).read()


def summary(label):
    """Per-type tally. 'captured' counts only pages with a real library:
    a 404 or a blank page is reported on its own terms, never as a success."""
    con = db.connect()
    rows = con.execute(
        "SELECT ptype, COUNT(*) n,"
        " SUM(CASE WHEN last_status IN ('baseline','unchanged','changed')"
        "     THEN 1 ELSE 0 END) captured,"
        " SUM(CASE WHEN last_status='notlive' THEN 1 ELSE 0 END) dead,"
        " SUM(CASE WHEN last_status='blank' THEN 1 ELSE 0 END) blank,"
        " SUM(CASE WHEN last_status='error' THEN 1 ELSE 0 END) errors"
        " FROM urls WHERE active=1 GROUP BY ptype").fetchall()
    items = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    con.close()
    log({"kind": "summary", "phase": label, "items_indexed": items,
         "by_type": {r["ptype"]: {"total": r["n"], "captured": r["captured"],
                                  "dead404": r["dead"], "blank": r["blank"],
                                  "errors": r["errors"]} for r in rows}})


def pick(per_type=None, fraction=None, unchecked_only=True):
    con = db.connect()
    types = [r["ptype"] for r in con.execute(
        "SELECT DISTINCT ptype FROM urls WHERE active=1 AND ptype<>''")]
    ids = []
    for t in types:
        q = "SELECT id FROM urls WHERE active=1 AND ptype=?"
        if unchecked_only:
            q += " AND last_checked IS NULL"
        rows = [r["id"] for r in con.execute(q, (t,))]
        if not rows:
            continue
        if per_type is not None:
            k = min(per_type, len(rows))
        else:
            k = min(len(rows), max(3, round(len(rows) * fraction)))
        ids += random.sample(rows, k)
    con.close()
    return ids


def run_phase(label, ids):
    if not ids:
        log({"kind": "phase_skip", "phase": label, "reason": "nothing to do"})
        return
    wait_idle(label)
    log({"kind": "phase_start", "phase": label, "pages": len(ids)})
    start(ids)
    time.sleep(20)
    wait_idle(label)
    summary(label)
    log({"kind": "phase_done", "phase": label, "pages": len(ids)})


try:
    run_phase("1-one-per-type", pick(per_type=1))
    run_phase("2-ten-percent", pick(fraction=0.10))
    run_phase("3-the-rest", pick(fraction=1.0))

    # Blank renders are retried too: Infineon serves empty shells
    # intermittently, so one blank is not proof the page is broken. Whatever is
    # still blank after this retry is a real fault on the page.
    con = db.connect()
    errs = [r["id"] for r in con.execute(
        "SELECT id FROM urls WHERE active=1 AND last_status IN ('error','blank')")]
    con.close()
    run_phase("4-retry-errors", errs)

    summary("final")
    log({"kind": "all_done"})
except Exception:
    log({"kind": "fatal", "trace": traceback.format_exc()[-500:]})
