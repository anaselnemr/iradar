# Deep Dive — iRadar
_Harvested from the chat run of 2026-08-06 (session b30ec9f3). The same text lives in the chat._

---

Deep dive done. Three limits up front: the session is sandboxed to `iRadar\`, so `Projects\STATUS.md`, `Projects.md` and `Analysis.md` were unreadable (I have `Projects\CLAUDE.md` from context); writes are blocked, so nothing is corrected in place; and I could not run the test suite or query `watch.db`. Everything below is verified against raw files.

## First finding: the folder has no agent files

`iRadar\` has **no CLAUDE.md, no STATUS.md, no content map**. Only `.claude/settings.local.json` (three lines, disables one MCP server). `Projects\CLAUDE.md` says a chat titled `X` should read `Projects\X\CLAUDE.md` "if it has one" — the biggest project doesn't. Every session starts from `README.md`, which is the most stale document in the folder.

## What it actually is, verified

22,333 lines of Python across 9 app modules (`app.py` alone is 7,206 lines), 26 templates, 14 test files, 7 QA tools, 8 hand-run scripts. `data/` is **4.5 GB**, `watch.db` is **396 MB**. 385 commits — `Projects\CLAUDE.md` says 384, one behind.

**Two remotes, and the map only knows one:**
```
origin    https://github.com/anaselnemr/iradar.git
infineon  https://github.com/esinfineon/iradar.git
```
`Projects\CLAUDE.md` lists only the `anaselnemr` account. The project is also pushed to an Infineon-owned org, and `infineon/master` sits on the same commit as `master`. That is a work-relevant fact missing from the content map. It also means `docs/DEPLOY.md` §1 ("Deploy from GitHub repo → `anaselnemr/iradar`") may name the wrong repo now — I can't tell which one Railway watches.

## The branch story in README is inverted

README teaches: work on `dev`, merge to `master` to deploy. Reality:

- `master` — 2026-08-06, **11 commits ahead of `dev`**
- `dev` — 2026-08-04, **2 commits `master` doesn't have**

Every recent fix went straight to `master` (the deploy branch). And `dev` holds one real unlanded feature — `563212e`, the review highlighter: four-colour text marking, drag-a-box on screenshots, a rail listing every mark, and queue position with approve-goes-to-next. 806 lines across `app.py`, `db.py`, a new `iradar/highlights.py`, `change_view.html`, `preview.py` and `test_routes.py`. Written 31 July, never merged, and `master` has since moved 11 commits under it — that merge gets harder every week.

## Doc drift, ranked

`docs/confluence/iRadar.md` (updated 2026-08-06) is **accurate**. I checked its central claim against `runner.py` and it holds exactly: `_loss_survived_three_looks` demands unanimity when the first two captures agree, two-of-three when they disagree, and `f3 is None` counts as no agreement — precisely as the doc describes it. Trust that file.

`README.md` (last touched 2026-07-29, 12 behaviour commits ago) is wrong in ways that matter:

| README says | Code / current doc says |
|---|---|
| "only the newest snapshot per URL is kept" | `keep_snapshots: "2"` — two per page, plus anything an open update needs |
| "3 layers" (text, images, visual) | `compare_snapshots` returns **five** in-page signals — text, images, **library**, visual, **community tri-state** — plus file updates as a separate queue |
| "~15 s per page… ~3,500 URLs take roughly 3 hours" | `DEPLOY.md`: ~9.5 h for 4,188 pages at 8 tabs — about 65 s per page, over 4× slower |
| Set tabs to "4–6" | default is `4`; DEPLOY assumes `8`; Confluence says "Currently 8" |
| second capture confirms "the change is still there" | the two captures must agree on **what** changed, and losses need a third |

README also never mentions Content/items, Notes, Export, Hierarchy, accounts and roles, file updates, the in-app guide, components, levels, or the whole "Market analysis" feature — a 948-line module, a 25 KB template and 5,961 stored mentions. `scripts/` is listed but misses four tools; `qa/`, a top-level folder, isn't mentioned at all.

`docs/DEPLOY.md`: §3 says "Do NOT set `SENTIMENT`. That layer is parked deliberately." That environment gate no longer exists anywhere in the code — the only two survivors of the grep are that line and a stale comment at `tests/test_routes.py:85`. §2 also still reasons from 3 captures per page when the default is 2.

`tests/README.md` (2026-07-21) documents 8 test files. There are 14. Undocumented: `test_auth`, `test_comments`, `test_resume` (1,086 lines — the largest test file in the repo), `test_retention`, `test_routes`, `test_sentiment`. That's 2,826 lines, more than half the suite by volume. Its "~3s" runtime is from before all of them.

`qa/README.md` line 10 uses "corpus" — on your own banned-words list.

## Code-level notes

Two inline fallbacks contradict the real defaults. `compare.py:502` falls back to `visual_threshold` **0.5** where the default is **2.5** (5× more sensitive), and `db.py:407` falls back to `keep_snapshots` **3** where the default is **2**. Both are currently unreachable — `get_settings()` always seeds from `DEFAULT_SETTINGS`, so no caller can pass a dict missing those keys — but they read as the documented values and aren't.

Verified sound: the Dockerfile pin (`v1.61.0-noble`) matches `requirements.txt` (`playwright==1.61.0`), and the build actually launches Chromium rather than just importing it. `preview.py`'s safety claim is real — `runner.run_check_background` and `asset_watch.run_in_background` are the only two network entry points reachable from routes (app.py:1620, 2932, 3369, 3376, 3474), both stubbed, and the scheduler plus `_resume_on_boot` live in `__main__`, which `preview.py` never reaches. All three launchers `cd /d "%~dp0.."` exactly once — the double-`cd` bug is fixed. `sentiment.SOURCES` has 8 entries, as documented.

Three images in `docs/confluence/images/` are tracked in git but referenced nowhere in the markdown: `03-review-queue.png`, `10-the-bar.png` (the drawn bar the last commit replaced with a photographed one) and `12-the-bar-live-dark.png`.

## Memory that's now out of date

`run-crash-recovery` says restart the app then POST `/run-now` to resume — `_resume_on_boot()` now does it automatically 25 seconds after boot, no button. `sentiment-layer` says "on dev until merged" — it's on master and documented as "Market analysis". `queued-after-run124` lists the test harness and durable run state as pending — both shipped (`tests/`, the `run_items` table). Confirmed still true: `finder-pages-rows-untracked` (Confluence: "A few finder pages only capture their first page of results") and `text-extractor-versioning` (`TEXT_REV` is now 7, alongside `SHOT_REV` 8, `IMG_REV` 6, `SECTIONS_REV` 3).

## What I'd do, in order

1. **Merge or kill `563212e`.** A finished review feature rotting on a branch 11 commits behind is the most expensive thing here.
2. **Rewrite `README.md` from the Confluence doc.** It's already accurate; the README should point at it rather than contradict it.
3. **Give `iRadar\` a CLAUDE.md and a STATUS.md** so a session doesn't start from the stale file.
4. Fix the four small ones: the `SENTIMENT` line in DEPLOY.md, the stale comment in `test_routes.py:85`, `tests/README.md`'s missing six, and the two inline fallbacks.

Say the word and I'll write any of these — I just need the folder unlocked for writes.