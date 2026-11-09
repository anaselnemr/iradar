# iRadar — website change monitor

iRadar watches several thousand pages of a website and reports what changed
since last week: wording, documents, design resources, partners, prices,
parametrics, images and the page's appearance. Every suspected change is
double-checked before anyone sees it, and the weekly report goes out by email.

The live app is at **https://iradar.net**. The in-app **Help** page
(`/guide`) explains the dashboard for everyday users. This file covers how it
runs and how to set it up.

---

## How the site runs

| Piece | What it does |
|---|---|
| **Server** | One Linux VPS (Ubuntu) running Docker Compose with two containers: the app and Caddy. |
| **App container** | Flask web app + scheduler + capture engine (Playwright / headless Chromium). |
| **Caddy** | Sits in front of the app on ports 80/443 and gets and renews the HTTPS certificate by itself. |
| **Data** | `/opt/iradar/data` on the server: the SQLite database, page captures and screenshots. Outside the code folder, so a deploy never touches it. |
| **Settings** | `/opt/iradar/.env` on the server (see *Server setup*). Never in the repo. |
| **Domain / DNS** | The domain's DNS points `iradar.net` and `www` at the server (DNS only, no proxy). The same DNS provider forwards the site's inbound addresses (support@, alerts@) to the team inbox. |
| **Email** | Reports and reminders go out through the Resend HTTPS API from `alerts@iradar.net`. |
| **Backups** | **Settings → Advanced → The database** downloads a full backup of the database. |

The server keeps its own clock: the weekly check and the Friday review
reminder are scheduled inside the app, so nothing outside the server has to
trigger them.

---

## The weekly check

Runs every **Sunday at 01:00 (Berlin time)** by default; change it under
**Settings**. One run, start to finish:

1. **Offline pages first.** Pages that were already "not found" get a quick
   status request instead of a full capture. Only a page that has come back
   is captured in full.
2. **Capture.** Every live page is opened in a real browser, scrolled, with
   tabs and accordions expanded, then its text, images, screenshot and
   structured sections are recorded. Several pages are captured in parallel
   (**Settings → Parallel browser tabs**).
3. **Compare** with the page's last approved version.
4. **Double-check.** Every page that looks changed is captured again at the
   end of the run. A change is only reported if the second capture agrees.
   If the two disagree, a third capture decides. Loading glitches never reach
   the report.
5. **Retry** pages that failed to load, twice more, before calling them errors.
6. **Report.** The summary email goes to everyone who opted in.

A run that is interrupted (restart, deploy) resumes where it stopped.

**Friday reminder:** if updates are still waiting for review on Friday
morning, everyone on the report gets a nudge, so a week is never reported
twice.

---

## What it notices

| Raises a page | Ignored on purpose |
|---|---|
| **Text**: wording added, removed or edited | Text that only moved, or only changed its spacing |
| **Library items**: documents, design resources, trainings, videos, partners, featured products, news, applications, parametrics. Added, removed, or changed in place (version, date, size, value) | Stock levels ("4387 in stock"); the price itself is watched |
| **Price**: unit price and volume pricing | The community feed's threads, the mega-menu, the site-wide news box |
| **Images**: a different picture added or removed | The same picture at a new size (size settings in the image address) |
| **Screenshot**: a visible area changed beyond the threshold, or the page height moved | Position labels such as the section menu's "Jump to" box |
| **Products table**: product count or columns changed | Timestamps and "3 days ago" |
| **File updates**: a document republished at the same address | |

Thresholds and ignore patterns live under **Settings**.

---

## The page library

Click any page to open its library: the full-page screenshot, the components
the page carries (green = present), the price box, and every section as
captured:

Subheadline · Overview · Description · Parametrics · Key features ·
Benefits · Applications · Browse by category · Related products · Featured
products · Documents · Design resources & boards · Trainings & webinars ·
Partner solutions · News & highlights · Videos · Images

These lists are the page's groundtruth. Approving an update makes the new
capture the reference for next week.

---

## Reviewing updates

- **Approve**: the change is real; the new version becomes the reference.
- **Reject**: the capture is wrong, or the change is a mistake that will be
  reverted; the previous version stays the reference.
- **"N awaiting review"** on the dashboard opens every unreviewed update,
  whatever the page's current status.

---

## Server setup (once)

On a fresh Ubuntu VPS, as root:

```
bash deploy/vps/setup.sh          # Docker, firewall, swap, folders
```

Put the code in `/opt/iradar/repo`, the data (if moving) in
`/opt/iradar/data`, and create `/opt/iradar/.env` with:

| Variable | Purpose |
|---|---|
| `PUBLIC_URL` | `https://iradar.net`, used in emails and links |
| `TZ` | `Europe/Berlin` |
| `RESEND_API_KEY` | Sending key for the report emails |
| `MAIL_FROM`, `MAIL_FROM_NAME` | Sender address and name |
| `OWNER_EMAIL` | The admin account created on first start |
| `CRON_KEY` | Protects the `/cron/*` endpoints |
| `GUEST_KEY` | Optional read-only guest access |

Then start it:

```
cd /opt/iradar/repo/deploy/vps
docker compose up -d --build
```

Point the domain's `A` records (`@` and `www`) at the server's IP. Caddy
fetches the certificate as soon as DNS resolves.

**Moving to a new server:** copy `/opt/iradar/data` and `/opt/iradar/.env`
across, start it as above, switch DNS, then run one **baseline** check. A
server in a different country sees some region-dependent content
differently; a baseline run records the new state silently instead of
reporting it as changes.

---

## Deploying an update

Not while a check is running. The dashboard shows when one is.

```
git archive --format=tar HEAD | ssh <server> 'sudo tar -x -C /opt/iradar/repo'
ssh <server> 'cd /opt/iradar/repo/deploy/vps && sudo docker compose up -d --build iradar'
```

Then check that `https://iradar.net/healthz` answers `{"ok": true}`. The
code is pushed to the team repository as usual. The server needs no access
to it.

---

## Running locally

- **Windows:** `local\SETUP.bat` once, then `local\START.bat`. Opens at
  http://127.0.0.1:5000.
- **Preview a change safely:**

  ```
  python scripts/preview.py                   # demo data
  python scripts/preview.py iradar-backup.gz  # a copy of real data
  ```

  Serves a *copy* of the database on http://127.0.0.1:5055, with captures
  disabled, so nothing can reach the live site or the real data.

- **Tests:** `python tests/run_tests.py`

---

## Where things live

- `iradar/`: the app (web app, runner, capture engine, comparison, mailer)
  plus `templates/` and `static/`
- `deploy/vps/`: `setup.sh`, `docker-compose.yml`, `Caddyfile`
- `tests/`: the test suite
- `scripts/`: tools run by hand (preview, CLI, cleanup, migration)
- `local/`: double-click launchers for running on Windows
- `docs/`: the Confluence write-up and its images, future ideas, the
  original URL list
- `data/`: local data when running on your own machine (on the server it is
  `/opt/iradar/data`)
