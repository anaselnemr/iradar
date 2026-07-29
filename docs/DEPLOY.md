# Deploying iRadar to Railway, on iradar.net

Written from the actual shape of this app, not a generic guide. The four things
that will bite you are called out where they bite.

## Before you start

DNS is already done: `iradar.net` is delegated to Cloudflare
(`love.ns.cloudflare.com`, `louis.ns.cloudflare.com`) and resolving through the
proxy. The five MX records survived, so email forwarding still works.

## 1. Create the service

Railway → **New Project** → **Deploy from GitHub repo** → `anaselnemr/iradar`.

`railway.json` tells Railway to build the Dockerfile rather than guess. Do not
let it pick a Python buildpack: the buildpack installs Python but not the sixty
system libraries Chromium needs, and the failure appears hours later on the
first capture, not at build time.

The first build takes a while. The Playwright base image is large because it
carries a whole browser.

## 2. Add the volume FIRST — before the first real boot

Railway → service → **Variables / Settings** → **Volumes** → mount path:

```
/app/data
```

**This is the one that silently ruins everything.** `data/` holds the database,
every snapshot and every screenshot. Without a volume it lives on the container
filesystem, which is thrown away on every deploy - so every deploy loses every
page's baseline, and the next run re-baselines all 4,188 pages and reports
nothing for a full cycle. It looks like the app "just stopped finding updates".

Size it at **5 GB** (the cap on the current plan).

That fits because the screenshot is stored at half size with a 256-colour
palette: 686 KB down to about 151 KB, so three captures of all 4,188 pages plus
the database land near **2.8 GB** rather than the 9.2 GB the full-size PNG
needed. Measured, not estimated - see SHOT_SCALE in capture.py.

If the archive ever approaches the cap, the dial is **Settings -> Captures kept
per page**. Dropping 3 to 2 takes roughly a third off.

## 3. Environment variables

| Variable | Value | Why |
|---|---|---|
| `OWNER_EMAIL` | `anas.elnemr@infineon.com` | Seeds the first admin on a fresh database. Starting password is `Infineon@2026`, and it must be changed at first sign-in. Only ever creates - it will not touch an account that exists. |
| `TZ` | `Europe/Berlin` | **The schedule is a local time.** Containers run UTC, so "Monday 23:00" silently becomes 01:00 Tuesday your time. |
| `SECURE_COOKIES` | `1` | Already set in the Dockerfile. Session cookies HTTPS-only, plus HSTS. |
| `BEHIND_PROXY` | `1` | Already set in the Dockerfile. Makes the sign-in throttle read the real client IP from Cloudflare instead of seeing every request as one address. |

`PORT` is injected by Railway. The app reads it. Do not set it yourself.

Do NOT set `SENTIMENT`. That layer is parked deliberately.

## 4. Keep it to ONE replica

`railway.json` pins `numReplicas: 1`. Leave it there. Two replicas means two
schedulers firing the same capture run at the same time, both writing one
SQLite file over a shared volume. This app is a stateful single instance by
design; it does not scale horizontally and does not need to.

## 5. Point the domain at it

Railway → **Settings** → **Networking** → **Custom Domain** → `iradar.net`.
It gives you a target like `xxxx.up.railway.app`.

Then in Cloudflare → DNS, **edit the existing A record** (currently Namecheap's
parking page at 162.255.119.243):

| Type | Name | Content | Proxy |
|---|---|---|---|
| CNAME | `iradar.net` | `xxxx.up.railway.app` | Proxied (orange) |
| CNAME | `www` | `xxxx.up.railway.app` | Proxied (orange) |

A CNAME on the bare domain is not normally legal DNS. Cloudflare flattens it,
which is exactly why the domain goes through Cloudflare rather than pointing
straight at Railway - otherwise only `www.iradar.net` would work.

Leave the MX and TXT records alone.

## 6. Memory, and the one dial to turn

Eight parallel Chromium instances peak around **3-4 GB**. If Railway kills the
container mid-run (look for OOM in the deploy logs), open Settings in the
dashboard and drop **Parallel browser tabs** from 8 to 4. The run goes from
about 9.5 hours to about 16, which is still fine for something that runs
overnight once a week. Do not raise it above 8 - at 16 tabs pages come back
quietly half-captured, which is worse than slow.

## 7. What happens on first boot

Fresh database, so: schema created, admin seeded from `OWNER_EMAIL`, no pages
tracked yet. Sign in, then add the page list - Dashboard → **Add pages**, or
paste the URL list.

The first run is a baseline: it captures all 4,188 pages and reports nothing,
because there is nothing to compare against yet. That is correct behaviour, not
a fault. The run after it detects normally.

## Moving the existing data across (optional, do it after it is up)

The laptop holds 4.32 GB of captures and a 379 MB database with your accounts,
notes, page metadata (PL/BL/CM, levels) and the 49 open updates.

Railway volumes have no direct file upload, which makes moving 4.32 GB awkward.
Three honest options:

1. **Start fresh on Railway** and re-add the page list. Simplest. You lose the
   notes, the ownership metadata and the open updates. The first run is a
   baseline either way.
2. **Move the database only** (379 MB) via `railway ssh` or a one-off restore.
   Keeps accounts, notes, page list and metadata. Snapshots do not come, so the
   first run is still a baseline.
3. **Move everything.** Keeps the baselines too, so the first run on Railway
   detects real updates immediately. Most work to set up.

Review the 49 open updates on the laptop before switching either way - they do
not survive options 1 or 2.
