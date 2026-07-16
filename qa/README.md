# QA — the gate that decides whether the library can be trusted

A capture that is wrong but confident is worse than one that fails, because it
looks like data. Everything here exists to catch that.

## The pieces

| File | What it does |
|---|---|
| `phases.py` | Runs the whole corpus in stages: one page of **every** type → 10% of every type → the rest → retry errors/blanks. Each stage surfaces type-specific bugs before the next one costs hours. |
| `qa_daemon.py` | Samples captured pages continuously and checks each against the live page with independent logic. Ladder: 1-per-10 while hardening, relaxing to 1-per-20 → 1-per-50 → 1-per-100 after clean streaks; any WARN/FAIL drops back to the strictest rung. |
| `qa_probe.py` | Counts what the live page shows, independently of `capture.py`. |
| `qa_shots.py` | Photographs a live page (top, every documents page, every design-resources tab and its pages, news, full page) for an AI reviewer to look at. |
| `qa_agent_mission.md` | The mission handed to an AI reviewer: the full checklist, agreed point by point with the user. |

## Running it

    python qa/phases.py        # drives the staged capture (server must be up)
    python qa/qa_daemon.py     # samples and checks while the run proceeds

Both write `.jsonl` logs next to themselves; those are gitignored.

## Two rules learned the hard way

**The AI reviewer outranks the mechanical checker.** The checker has been wrong
more than once. It called a page's Development tools tab `live=70` when the tab
really holds 74 — an AI reviewer counted all 15 pagination pages by eye, and the
74 stored items carry 74 distinct titles. So `qa_daemon` is deliberately
asymmetric: storing *fewer* than the probe sees is evidence of a miss and warns;
storing *more* is recorded as a note and never resets the ladder. Real
over-collection shows up as duplicate titles, which is checked separately.

**A false alarm is not free.** Every WARN resets the sampling ladder, so a check
that cries wolf on all 30 promo pages doesn't just add noise — it pins the
regime at its strictest rung forever and buries the findings that matter. Fix
the check, don't tolerate the noise.
