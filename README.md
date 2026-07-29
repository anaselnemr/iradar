# iRadar — website change monitor

Watches a list of web pages and **flags any change**: text (including content
hidden behind tabs), images, and visual appearance (full-page screenshot,
pixel-by-pixel). Everything runs locally on your PC — no accounts, no cloud.

## First-time setup (once)

1. Install Python 3.10+ from https://www.python.org/downloads/
   — during install, **tick "Add Python to PATH"**.
2. Double-click **`SETUP.bat`** and wait for "Setup finished".

## Daily use

1. Double-click **`START.bat`**. A browser tab opens at http://127.0.0.1:5000.
2. Add URLs: paste them into the box (one per line, or any messy text —
   URLs are auto-extracted), or upload an Excel/CSV/TXT file.
3. Click **▶ Run check now**. The first run saves a *baseline* for each page.
4. Every later run compares against the last snapshot. Anything that changed
   shows an orange **changed** badge — click it to see:
   - a red/green **text diff**,
   - **added/removed images**,
   - **before / after / difference screenshots** (changed pixels painted red).
5. Click **✔ Accept — mark unchanged** once you've handled a change; the page
   goes back to green until it changes again.

## The page library (groundtruth)

Clicking any URL opens its **library page**: the full-page screenshot plus
structured lists extracted from the fully-loaded page — **Documents**
(title + link + type/size/date), **Design resources & boards**,
**Trainings & webinars**, **Videos**, **News & highlights**, and **Images**
(URL + alt-text title). These lists are the groundtruth for that page:
when you approve a change, the new snapshot's lists become the current
truth; a change page also shows exactly which library items were added or
removed. Not every page type has every section (e.g. promo landing pages
have none) — an empty list means the live page really has none.

## Opening the dashboard from another device

- Same Wi-Fi/network: run START.bat, then open `http://<this-PC's-IP>:5000`
  on the other device (find the IP with `ipconfig`, IPv4 Address).
- From anywhere: run **START_SHARE.bat** — it prints a temporary
  `https://….loca.lt` link and opens the one-time tunnel password
  (this PC's public IP) in your browser.

## Working with thousands of URLs

- **Search** the list by any part of the URL.
- **Filter chips**: All / Changed / Errors / Not live / Paused / Not checked —
  problems always sort to the top of the list.
- **Multi-select**: tick rows (or the header checkbox for the whole page of
  results) and use the bulk bar: **Check now**, **Mark unchanged**, **Pause**,
  **Resume**, **Remove**.
- The list is paginated (100 per page); search + filters work across all pages.
- **Live column**: every check also records the HTTP status of the page —
  LIVE (200), HTTP 404/500 (page broken), or DOWN (unreachable).
- **Full data per snapshot**: in a URL's history, click the text-line count to
  see every extracted line, the image count to see every image URL, or open
  the stored screenshot.

## How false alarms are prevented

1. Captures wait for the network to settle and poll the image list until it
   stops changing.
2. If a page *looks* changed, it is **captured a second time** and only flagged
   if the change is still there — one-off loading glitches never reach you.
3. Auto-generated video-player thumbnails are excluded from the image
   comparison by default (Settings → ignore image patterns) because their
   loading is inherently flaky; removing an actual video still shows up in the
   text diff.
4. Anything page-specific you want muted: Settings → ignore text/image
   patterns (one regular expression per line).

## Scheduling

Settings → choose **daily / every 2 days / weekly**, plus day and hour.
The live site (https://iradar.net) runs on a server, so scheduled checks
fire on their own — nothing needs to stay open anywhere. (Running locally
instead, the check fires as long as the START.bat window is open.)

## How change detection works (3 layers)

| Layer | What it catches | How |
|---|---|---|
| Text | Any wording/spec/table change, even inside inactive tabs or collapsed accordions | Real Chrome renders the page, the tool auto-scrolls, clicks tabs / expands accordions / presses "load more", then extracts **every text node in the DOM** (hidden ones included) |
| Images | Added/removed/renamed images | Full inventory of every image URL on the page |
| Visual | Layout shifts, banner swaps, styling breakage | Full-page screenshot compared pixel-by-pixel; differences painted red |

Cookie banners are hidden/removed automatically so they never trigger false alarms.

## Scale notes (3,000–4,000 URLs)

- Set "Parallel browser tabs" to 4–6 in Settings. At ~15 s per page and
  5 tabs, ~3,500 URLs take roughly 3 hours per run — fine for a weekly check.
- Disk stays flat: only the newest snapshot per URL is kept, plus any
  snapshots referenced by a flagged change.

## Reducing false alarms

If a page has genuinely dynamic content (e.g. "3 new documents this week"),
add a matching pattern under Settings → *Ignore text lines matching* — one
regular expression per line. You can also raise the visual threshold.

## Two branches: live, and everything else

`master` is what **https://iradar.net** serves. Railway watches that branch
and only that branch, so a push to `master` is a deploy — within a couple of
minutes it is in front of the team.

`dev` is where work happens. Pushing it deploys nothing.

```
git checkout dev          # work here
...edit, commit...
python preview.py         # look at it, on your machine
git push origin dev       # saved to GitHub, still not live

# when it is ready to be live:
git checkout master
git merge dev
git push origin master    # THIS is the deploy
```

Merging to `master` restarts the app. That is safe on purpose: a capture run
that was in flight is picked up automatically where it left off, so shipping
never costs a run.

## Looking at a change before it is live

```
python preview.py                     demo data, made up on the spot
python preview.py data/watch.db       a copy of a real database
python preview.py iradar-backup.gz    a backup straight from Settings
```

Opens the whole app on http://127.0.0.1:5055, running whatever branch you
have checked out. It cannot damage anything:

- it **copies** the database you point it at and serves the copy, so the
  original is never opened;
- it **cannot start a capture** — both entry points are replaced before the
  server starts, so no button on the page can reach infineon.com;
- it has no scheduler and no resume-on-boot.

The most useful mode is the middle one: your changes, against real data, at
real scale, with nothing at risk. Download a backup from **Settings →
Advanced → The database** and point `preview.py` at the `.gz` it gives you.

## Where things live

- `app.py`, `runner.py`, `capture.py`, `compare.py`, `db.py`, `auth.py`,
  `comments.py`, `sentiment.py`, `asset_watch.py` — the app itself, flat at
  the root so the imports stay simple.
- `templates/`, `static/` — the pages and their assets.
- `tests/` — `python tests/run_tests.py` runs everything.
- `preview.py` — run this branch on a throwaway copy: `python preview.py`.
- `cli.py` — optional command line: `python cli.py add-file urls.txt`, `python cli.py run`.
- `scripts/` — one-off and occasional tools: the logo generator, disk
  cleanup, the laptop-to-server migration, the old tunnel launchers.
- `docs/` — the deploy notes, the Confluence page and its images, the
  future-ideas list.
- `data/watch.db` — the database (URLs, results). Back this up if you care.
- `data/snapshots/` — stored page captures.
- `data/changes/` — visual-diff images for flagged changes.
