# -*- coding: utf-8 -*-
"""Backfill: read the page behind every mention already in the database.

The app does this for itself after each collection (sentiment.enrich_pending);
this is the one-off pass over the 7,512 mentions that were collected before it
learned to. It runs from a laptop rather than from the server because it is
long, chatty and restartable, and because a failure here should cost nothing.

    python scripts/fetch_mention_text.py work.json out.jsonl [--only youtube]

work.json is a list of {id, s(ource), u(rl), t(itle), b(ody)}, exported from
the mentions table. Output is one JSON object per line keyed by mention id, so
a re-run picks up exactly where it stopped. All the actual reading lives in
iradar/mention_text.py - both paths must read a page the same way or the
backfilled rows and the new ones would disagree.
"""
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "iradar"))
import mention_text as mt                                     # noqa: E402

# how many at once per source. Six lanes on YouTube earns a 429 about four
# hundred pages in and then every request fails, so it reads two at a time
# and finishes slower than it would have at six with nothing to show.
LANES = {"youtube": 2, "github": 6, "forum": 3, "hn": 5, "blog": 3,
         "stackexchange": 2}
_out = threading.Lock()


def main(argv):
    work = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    out = Path(argv[2])
    only = argv[argv.index("--only") + 1] if "--only" in argv else None

    seen = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["id"])
            except ValueError:
                pass
    print("already have %d" % len(seen), flush=True)

    fh = out.open("a", encoding="utf-8")

    def write(mid, got):
        rec = {"id": mid, "text": got[0], "extra": got[1], "author": got[2],
               "posted": got[3]}
        with _out:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()

    rows = [r for r in work if r["id"] not in seen
            and (not only or r["s"] == only)]

    # community first: 40 threads a call, and one thread explains many
    # mentions - the same thread is cited on every product page it touches
    by_thread = {}
    for r in rows:
        if r["s"] != "community":
            continue
        m = mt.THREAD_ID.search(r["u"] or "")
        if m:
            by_thread.setdefault(m.group(1), []).append(r["id"])
    keys = list(by_thread)
    if keys:
        print("community: %d threads behind %d mentions"
              % (len(keys), sum(len(v) for v in by_thread.values())), flush=True)
    for i in range(0, len(keys), 40):
        try:
            got = mt.community_batch(keys[i:i + 40])
        except Exception as exc:
            print("  batch %d failed: %s" % (i, str(exc)[:80]), flush=True)
            continue
        for key, val in got.items():
            for mid in by_thread.get(key, ()):
                write(mid, val)
        print("  community %d/%d" % (min(i + 40, len(keys)), len(keys)),
              flush=True)

    for src in mt.FETCHERS:
        todo = [r for r in rows if r["s"] == src]
        if not todo:
            continue
        print("%s: %d to read" % (src, len(todo)), flush=True)
        done = [0]

        def job(r):
            got = mt.fetch_one(r["s"], r["u"])
            if got:
                write(r["id"], got)
            done[0] += 1
            if done[0] % 100 == 0:
                print("  %s %d/%d" % (src, done[0], len(todo)), flush=True)

        with ThreadPoolExecutor(max_workers=LANES.get(src, 3)) as ex:
            list(ex.map(job, todo))
        print("%s: done" % src, flush=True)

    fh.close()
    print("finished", flush=True)


if __name__ == "__main__":
    main(sys.argv)
