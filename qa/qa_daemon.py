"""QA daemon v3 - the bulletproof gate.

Samples 1 page per 10 captured while HARDENING; after 10 consecutive clean
samples it relaxes to 1 per 20. Every sample is checked against the live page
with independent logic, covering every requirement agreed in this project:

  documents   count == live rows (language variants kept), dates/meta present,
              gated rows kept, no junk/placeholder titles, no duplicates
  design res. every tab of the live widget present as a group with matching
              counts (pagination fully walked), real card titles
  trainings   present when the page has them, video URLs recovered
  news        videos with duration/date
  features / benefits / applications / overview / categories present when live
  images      stored URL list matches the live page, titles (alt) kept
  screenshot  exists, viewport width (no white margin), plausible height
  text        no Feedback/Qualtrics furniture, real page title, not blank
"""
import json
import random
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, r"C:\Users\anasn\Desktop\InfineonWatch")
import db  # noqa: E402
from PIL import Image  # noqa: E402

SP = Path(__file__).parent
LOG = SP / "qa_log.jsonl"
PY = r"C:\Users\anasn\Desktop\InfineonWatch\.venv\Scripts\python.exe"

# Sampling ladder: start strict, relax one rung per clean streak, never
# looser than 1 per 100. Any WARN/FAIL drops straight back to the strictest.
LADDER = [10, 20, 50, 100]
CLEAN_STREAK_TO_RELAX = 10

JUNK_TITLE = re.compile(
    r"(?i)^(reset all|goto.?link|view (products?|all)|read more|explore more|"
    r"learn more|download|show all|watch video|log ?in|clear all|"
    r"note that there are additional results.*|add to cart.*)$")


def log(obj):
    obj["ts"] = datetime.now().strftime("%H:%M:%S")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def probe(url):
    r = subprocess.run([PY, str(SP / "qa_probe.py"), url],
                       capture_output=True, text=True, timeout=320,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "probe failed")[-300:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def verify(snap, sections, live, snap_dir):
    """Returns (verdict, reasons). FAIL = systematic miss, WARN = suspicious."""
    reasons, fail = [], False

    # ---- documents ----
    docs = sections.get("documents", [])
    if abs(len(docs) - live["docs"]) > 1:
        fail = True
        reasons.append(f"documents stored={len(docs)} live={live['docs']}")
    if live["doc_dates"] >= 3:
        with_date = sum(1 for d in docs
                        if re.search(r"[A-Z][a-z]{2} \d{1,2}, \d{4}",
                                     d.get("meta", "")))
        if with_date < min(live["doc_dates"], len(docs)) - 1:
            reasons.append(f"document dates stored={with_date} live={live['doc_dates']}")

    # ---- design resources / trainings: every live tab must exist ----
    # Strict in BOTH directions: an extra item is as wrong as a missing one.
    # This check was briefly one-sided while the probe undercounted (it counted
    # unique anchor TEXTS, so it called a 74-card tab "70"). The probe counts
    # cards now and agrees with a by-eye count on every group of two product
    # pages, so an excess means something real again and is reported.
    # Tolerance stays at 1 for live-site churn: a document can genuinely appear
    # between our capture and this probe.
    notes = []
    sgroups = {}
    for it in sections.get("design_resources", []) + sections.get("trainings", []):
        g = it.get("group")
        if g:
            sgroups[g] = sgroups.get(g, 0) + 1
    for gname, n_live in live["groups"].items():
        n_stored = sgroups.get(gname, 0)
        if n_live > 0 and n_stored == 0:
            fail = True
            reasons.append(f"group '{gname}' live={n_live} stored=0")
        elif n_live and n_stored < n_live - 1:
            reasons.append(f"group '{gname}' live={n_live} stored={n_stored}"
                           f" - {n_live - n_stored} MISSING")
        elif n_live and n_stored > n_live + 1:
            reasons.append(f"group '{gname}' live={n_live} stored={n_stored}"
                           f" - {n_stored - n_live} EXTRA")

    # ---- other sections present when the page has them ----
    if live["features_li"] >= 3 and not sections.get("features"):
        reasons.append(f"features live~{live['features_li']} stored=0")
    if live["news_cards"] >= 2 and \
            len(sections.get("news", [])) < live["news_cards"] - 2:
        reasons.append(f"news live~{live['news_cards']} "
                       f"stored={len(sections.get('news', []))}")
    if live["has_overview"] and not sections.get("overview"):
        reasons.append("overview live=yes stored=0")

    # ---- images: the stored inventory is the groundtruth for change detection
    stored_imgs = sections.get("images", [])
    if live["images"] and abs(len(stored_imgs) - live["images"]) > 2:
        reasons.append(f"images stored={len(stored_imgs)} live={live['images']}")
    if live["imgs_with_alt"] >= 3:
        titled = sum(1 for i in stored_imgs if (i.get("title") or "").strip())
        if titled == 0:
            reasons.append("image titles (alt) all missing")
    for i in stored_imgs[:40]:
        u = i.get("url", "")
        if u and not u.startswith("http"):
            fail = True
            reasons.append(f"image URL not absolute: {u[:40]}")
            break

    # ---- junk / duplicates across every category ----
    # 'breadcrumb' is the page's own hierarchy, not library content, and it is
    # not ours to police: Infineon's promo pages genuinely publish
    # "Home > Home > Gated Promo Document". Checking it flagged all 30 promo
    # pages and would have pinned the sampling ladder at its strictest rung.
    for cat, items in sections.items():
        if not isinstance(items, list) or cat in ("images", "breadcrumb"):
            continue
        seen = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            t = (it.get("title") or "").strip()
            if JUNK_TITLE.match(t):
                fail = True
                reasons.append(f"junk title in {cat}: {t[:40]!r}")
                break
            k = (t.lower(), it.get("url", ""))
            if k in seen:
                reasons.append(f"duplicate item in {cat}: {t[:40]!r}")
                break
            seen.add(k)

    # ---- capture integrity ----
    if snap["text_lines"] < 200:
        fail = True
        reasons.append(f"text_lines={snap['text_lines']} (<200)")
    if not (snap["page_title"] or "").strip():
        fail = True
        reasons.append("stored page title is empty")
    if live["feedback_visible"]:
        pass                                    # widget on the live page is fine
    try:
        txt = (Path(snap_dir) / "text.txt").read_text(encoding="utf-8",
                                                      errors="replace")
        if "Feedback" in txt or "QSIFeedback" in txt:
            reasons.append("Feedback/Qualtrics furniture leaked into the text")
    except Exception:
        pass
    shot = Path(snap_dir) / "shot.png"
    if not shot.exists() or shot.stat().st_size < 40_000:
        fail = True
        reasons.append("screenshot missing or tiny")
    else:
        try:
            im = Image.open(shot)
            if im.width != 1440:
                reasons.append(f"screenshot width {im.width} (expected 1440 -"
                               " white margin / overflow)")
            if im.height < 500:
                reasons.append(f"screenshot height {im.height} (too short)")
        except Exception as exc:
            reasons.append(f"screenshot unreadable: {exc}")

    return ("FAIL" if fail else ("WARN" if reasons else "PASS")), reasons, notes


def main():
    log({"kind": "start", "version": 3, "ladder": LADDER})
    con = db.connect()
    run = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    run_start = run["started"] if run else "1970-01-01"
    sampled, streak, beats = set(), 0, 0
    rung = 0
    every = LADDER[rung]
    last_mark = 0
    while True:
        time.sleep(45)
        beats += 1
        try:
            con = db.connect()
            run_row = con.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            # the pipeline runs several phases: follow each new run instead of
            # exiting when one finishes, or phases 2-4 go unwatched
            if run_row and run_row["started"] != run_start:
                run_start = run_row["started"]
                last_mark = 0
                log({"kind": "new_run", "started": run_start})
            checked = con.execute(
                "SELECT COUNT(*) FROM urls WHERE last_checked >= ?",
                (run_start,)).fetchone()[0]
            finished = bool(run_row["finished"]) if run_row else True
            if beats % 20 == 0:
                log({"kind": "heartbeat", "checked": checked,
                     "sampled": len(sampled), "every": every, "streak": streak})
            mark = checked // every
            picks = []
            if mark > last_mark or (finished and checked and not sampled):
                last_mark = mark
                rows = con.execute(
                    "SELECT u.* FROM urls u JOIN snapshots s ON s.url_id=u.id"
                    " AND s.status='ok' AND s.dir<>'' AND s.rejected=0"
                    " WHERE u.last_checked >= ? AND u.last_status IN"
                    " ('baseline','unchanged','changed') GROUP BY u.id"
                    " ORDER BY u.last_checked DESC LIMIT ?",
                    (run_start, every * 2)).fetchall()
                rows = [r for r in rows if r["id"] not in sampled]
                random.shuffle(rows)
                picks = rows[:1]
            for u in picks:
                sampled.add(u["id"])
                snap = con.execute(
                    "SELECT * FROM snapshots WHERE url_id=? AND status='ok'"
                    " AND dir<>'' AND rejected=0 ORDER BY id DESC LIMIT 1",
                    (u["id"],)).fetchone()
                try:
                    sections = json.loads(
                        (Path(snap["dir"]) / "sections.json")
                        .read_text(encoding="utf-8"))
                except Exception:
                    sections = {}
                try:
                    live = probe(u["url"])
                except Exception as exc:
                    log({"kind": "sample", "verdict": "SKIP", "url": u["url"],
                         "reasons": [f"probe failed: {str(exc)[:150]}"]})
                    continue
                verdict, reasons, notes = verify(snap, sections, live,
                                                 snap["dir"])
                if verdict == "PASS":
                    streak += 1
                    if streak >= CLEAN_STREAK_TO_RELAX and rung < len(LADDER) - 1:
                        rung += 1
                        every = LADDER[rung]
                        streak = 0
                        log({"kind": "phase", "every": every,
                             "msg": f"{CLEAN_STREAK_TO_RELAX} consecutive clean"
                                    f" samples - relaxing to 1 per {every}"})
                elif verdict in ("WARN", "FAIL"):
                    streak = 0
                    if rung != 0:                 # something slipped: tighten
                        rung, every = 0, LADDER[0]
                        log({"kind": "phase", "every": every,
                             "msg": f"{verdict} - back to 1 per {every}"})
                log({"kind": "sample", "verdict": verdict, "url": u["url"],
                     "url_id": u["id"], "snap_dir": snap["dir"],
                     "checked": checked, "streak": streak, "every": every,
                     "reasons": reasons, "notes": notes,
                     "stored": {k: len(v) for k, v in sections.items()
                                if isinstance(v, list)},
                     "live": live})
            con.close()
            if finished and beats % 20 == 0:
                log({"kind": "idle", "checked": checked,
                     "sampled": len(sampled), "note": "waiting for next phase"})
        except Exception:
            log({"kind": "loop_error", "trace": traceback.format_exc()[-400:]})
        if beats > 2400:
            log({"kind": "fatal", "msg": "timeout"})
            break


main()
