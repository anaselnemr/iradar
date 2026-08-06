# iRadar — how it works

**Paste everything below the line into a new Confluence page.**

Confluence Cloud accepts Markdown on paste (`/markdown` if it does not convert
automatically). Images do not come across with the text — drag each file from
`confluence/images/` into the spot marked `[IMAGE: …]`, then delete the marker.

The same content lives inside the app at **https://iradar.net/guide**, where
every number is read live. This copy is a snapshot and will drift; link to the
live one where you can.

---

# iRadar

**https://iradar.net**

iRadar opens every Infineon.com page we track, once a week, and tells you what
actually changed since last time — wording, documents, images, videos, boards
and prices.

It exists because the alternative is opening a page and trying to remember what
it said last month. At the time of writing it watches **4,267 pages** and
**202,378 individual items** across them, including **6,550 distinct documents**.

## Contents

1. [Getting in](#getting-in)
2. [What happens on a check](#what-happens-on-a-check)
   - [Three kinds of check](#three-kinds-of-check)
   - [The bar you watch while it runs](#the-bar-you-watch-while-it-runs)
   - [If a check stops half way](#if-a-check-stops-half-way)
   - [Page states you will see](#page-states-you-will-see)
3. [What it notices, and what it lets pass](#what-it-notices-and-what-it-lets-pass)
4. [Reviewing an update](#reviewing-an-update)
5. [Content, and file updates](#content-every-document-and-where-it-is-used)
6. [Market analysis](#market-analysis-what-the-world-says-back)
7. [Leaving a note](#leaving-a-note)
8. [Export](#export-getting-it-out-as-a-spreadsheet)
9. [The component matrix](#which-components-a-page-type-should-have)
10. [Changing the schedule](#changing-when-it-runs)
11. [Where things are](#where-things-are)
12. [Accounts and roles](#accounts-and-roles)
13. [Worth knowing](#worth-knowing)
14. [What it does not do](#what-it-does-not-do)

---

## Getting in

Sign in with your Infineon email address. Your starting password is
`Infineon@2026`, and it will ask you to replace it straight away.

> **Use a passphrase** — three or four random words, like
> `harbour-cactus-lantern-42`. Long beats complicated, and it is far stronger
> than something like `P@ssw0rd!`.

Your display name and password are under your avatar, top right. Everything on
screen is in **your local time**, not the server's.

[IMAGE: 01-sign-in.png]

---

## What happens on a check

Every **Monday at 23:00**, on its own. Nobody starts it. It takes about nine
hours and is finished long before Monday morning.

[IMAGE: 09-page-through-a-check.png]

**1. Every page is opened in a real browser.**
Not downloaded as code — genuinely rendered, the way you would see it. It
scrolls to the bottom so anything that loads late appears, opens tabs and
accordions, waits for the images to arrive, and waits for the page's fonts.

*A page that only half-loads is thrown away instead of saved. If it were kept it
would become next week's reference, and then everything missing from it looks
like it was deleted.*

**2. What is on it is written down.**
The visible text, every image, the whole library — documents, videos, boards,
design resources, partners — the price, and a full-page screenshot.

**3. It is compared with last time.**
Five signals, listed below. Any one of them is enough to raise the page.

**4. Anything raised goes to a vote.**
Before the run ends, the page is captured again — and the two captures must
agree on **what** changed, not merely that something did. This is stricter than
it sounds, and it is where most false alarms die.

[IMAGE: 08-the-vote.png]

| What happens | Outcome |
|---|---|
| The first two captures agree | Published to your queue. |
| They disagree about what moved | A third capture breaks the tie — two of three wins. |
| The second capture shows nothing | The site was being inconsistent. Dropped silently, and you never see it. |

**Something disappearing is judged harder than something appearing.** A page
that LOST content — a datasheet gone, a section emptied — has to be **unanimous**
when the first two captures agree, and still two-of-three when they do not. The
reason is that a half-loaded page looks exactly like a page that lost half its
content, so losses carry the higher bar. The asymmetry is deliberate: a false
"it vanished" costs more than a missed edit.

*This one step removes most false alarms before anybody sees them.*

**5. Pages that fail get two more tries.**
Retried at the end of the run. Only something that fails every attempt is
recorded as an error — one bad moment is not a verdict.

**6. What survives lands in your queue.**
On the Dashboard, marked **Updated**. Nothing is altered or deleted; the
previous version stays until you decide.

[IMAGE: 02-dashboard.png]

*The number under **Updated** is your queue — click it to see only those.
**File updates** beside it is a separate queue, for files that changed behind a
link that did not.*

### Three kinds of check

Most weeks you only ever see the first one.

| | What it covers | When |
|---|---|---|
| **Full check** | Every page that is not paused. This is what the Monday schedule runs, and what **Check all pages** does. | Weekly, on its own. |
| **Partial check** | A slice, from the **Partial runs** menu beside the button: *just the pending ones* (never captured, or failed last time) or *just the not-yet-captured*. Also what you get by ticking pages and choosing Check. | When you want an answer now without waiting nine hours. |
| **Baseline check** | Reads every page and deliberately reports **nothing**. Each capture simply becomes the new reference. | After the way pages are read has been changed. |

**Why a baseline check reports nothing on purpose.** When the capture method
itself changes — a new part of the page starts being read, or a screenshot is
taken at a different size — every page would differ from its old reference, and
all of it would be the method rather than the site. A baseline run throws the
comparison away for one pass and re-references everything, so the *next* normal
check goes back to finding real edits. A run that reported 4,000 updates would
tell you nothing; a run that reports none, once, tells you it worked.

### The bar you watch while it runs

While a check is going, the Dashboard shows one bar with every page in it. Each
page sits in exactly one colour, and the five always add up to the total.

[IMAGE: 11-the-bar-live.png]

*A real check, three quarters through.* Read left to right: **2,887** pages
settled with no update, **41** confirmed as real updates, **263** still held for
their second capture, **18** blocked so far and owed their retries, and **1,057**
not yet reached. That is 4,266 — the whole list, every page in exactly one band.

The row of pills above it is the run's shape: **Capture** first, then
**Reconfirming updates** once every page has been read, then **Retry blocked**
last. The striped band is the holding pen being worked through.

| | |
|---|---|
| **No update** | Captured, compared, nothing moved. On a normal check this is nearly the whole bar. |
| **Updated** | Confirmed. It survived the second capture and is in your queue. |
| **Double-checking** | The page looked different and is being held for a second capture. **Not an update yet.** These are settled at the very end, so this number climbs all the way through and then empties in one go — most of it into *no update*. |
| **Failed (will retry)** | Did not load this time. Every one gets two more tries at the end, so this usually falls near the end rather than rising. |
| **Waiting** | Not reached yet. |

> A big **double-checking** number part way through is normal and is not that
> many updates waiting for you. It is the holding pen, and the second capture
> is what empties it.

### If a check stops half way

It picks up where it left off. Every page is recorded as it is done, so a check
that is interrupted — the machine restarts, the server redeploys, somebody
presses stop — resumes from the first page it had not reached, rather than
starting the whole thing again.

- **Pages already checked are not re-checked.** Their results stand.
- **Pages caught mid-capture are simply redone.** A half-written capture is
  never kept, so an interruption cannot leave a corrupted reference behind.
- **Updates already found are still in your queue.** Nothing waiting for review
  is lost.
- **The Dashboard says so** — a check left open with nobody working on it shows
  how far it got and offers a button to carry on.

**Interrupted and stopped are not the same thing.** A check that was cut off —
the server redeployed, the machine restarted — picks itself back up
automatically the moment it is running again. A check that a *person* stopped
stays stopped, through any number of restarts. It is not forgotten: the pages it
was holding are parked, and the next check somebody starts collects them. The
distinction exists so that pressing stop actually means stop, rather than
meaning "pause until the next deploy".

### Page states you will see

These are the chips along the top of the Dashboard. Each one is a filter.

| | |
|---|---|
| **Unchanged** | Checked, nothing moved. The normal state. |
| **Updated** | Something changed and it is waiting for you. |
| **Blank page** | The address answered but there was nothing on it. Usually Infineon serving an empty shell; retried next check. |
| **Capture error** | Failed all three attempts. Almost always the site being slow or refusing us, not the page being broken. |
| **Not live** | The address answers 4xx. The page is gone, or was never published. It stays in the list so you can see it went. |
| **Not captured** | Added but never successfully read yet, so there is nothing to compare against. It gets its first reading next check. |
| **Paused** | Deliberately skipped. Still listed, not checked. |
| **Ownership mismatch** | The owner or business line on the page does not agree with what was imported. Flagged for a human, never auto-corrected. |

---

## What it notices, and what it lets pass

The right-hand column is why something you expected did not come up. Each one
caused a flood of false alarms before it was handled.

| Raises a page | Ignored on purpose |
|---|---|
| **Text** — wording added, removed or edited. Text that only *moved* does not count; same lines in a new order is layout. | **Stock levels** — "4387 in stock" reads 4382 by morning. The price is still watched. |
| **Library** — a document, video, board, design resource or partner appears or disappears, or its version, date, size or price changes. | **The community feed** — re-sorts itself constantly. Whether the widget is present, empty or busy *is* tracked. |
| **Images** — a picture appears on or disappears from the page. | **Latest news and the mega-menu** — identical on every page. One retired thumbnail once raised 585 pages at once. |
| **Screenshot** — more than 1.5% of pixels differ, or the page height moves more than 80px. | **Tabs loading differently** — two visits can land on different tabs. Their contents are tracked item by item instead. |
| **File updates** — a file republished at the same address. The page is identical; the datasheet behind it is new. | **Timestamps and "3 days ago"** — they change with nobody editing anything. |
| | **The same words, set differently** — a page is photographed only once its fonts have arrived, so text that wraps at a different word is never mistaken for text that changed. |

*Thresholds are the live values as of writing; an admin can change them under
Settings, and the in-app Help page always shows the current ones.*

---

## Reviewing an update

Open one from the Dashboard. You see exactly what moved — the wording before and
after, which documents came and went, and the two screenshots with the changed
areas boxed.

[IMAGE: 04-an-update.png]

Then one of two buttons:

| | What it does | Use it when |
|---|---|---|
| **Accept** | Confirms this is the page now. It becomes the reference the next check compares against. | The change is real — whether it matters or not. |
| **Reject** | Says "do not use *this capture* as the reference". The previous version stays. | The capture is wrong, or the change is going to be undone. |

### Two reasons to reject

Both keep the previous version as the reference, and that is exactly what makes
them work:

- **The capture is wrong.** Something did not load and iRadar saw half a page.
  Next week it captures properly, matches the version you kept, and says nothing.
- **The change is real but wrong, and will be put back.** Somebody published a
  mistake. Reject it, tell whoever owns the page, and the reference stays on the
  correct version — so when they fix it, the page matches again and goes quiet
  on its own. *Until it is fixed the page will come up again on every check.
  That is the point: it is a standing reminder that the site still does not say
  what it should.*

> **Accept is the normal one.** Reject is for a capture that is wrong or a change
> that is going to be undone — not for a change you simply dislike. Accepting
> does not endorse anything; it only records what the page says today.

---

## Content: every document, and where it is used

Every check reads the whole library off each page, so **Content** is not a
separate list somebody maintains — it is what the site actually says today.

**It is organised by the thing, not by the page.** One datasheet used on forty
product pages is *one* entry with forty pages under it, not forty copies. Open it
and you get the answer to the question that is otherwise a morning's work:

- **Which pages carry this document** — every one, with its type and the heading
  it sits under on that page
- **Its details** — version, language, date, size, and whether it is public or
  behind a MyInfineon login
- **A picture of the first page**, rendered here, so you can tell at a glance
  whether it is the right file without downloading it
- **Where to edit it** — the DAM and Windchill badges go straight to the source
  system

The same is true of videos, images, boards, design resources and partner logos.

> **This is what makes a removal visible.** When a datasheet disappears from a
> page, iRadar knows because it compares the whole library, not just the words.
> That is the **Library** signal, and it is the one that catches the changes
> reading the page would not.

### File updates: the other queue

A datasheet can be replaced without the page moving at all — same link, same
title, new file behind it. Nothing on the page changed, so no page comes up, and
reading the site would never tell you. iRadar asks each file for its size and
date without downloading it, and when those move it goes to **File updates**
instead.

- **Its own queue, its own Accept and Reject**, separate from page updates so a
  quiet republish does not get lost among them.
- **Every page carrying that file is listed** — one replaced datasheet can be
  forty pages' worth of news.
- **Accept** records the new file as the one we know about. **Reject** keeps the
  old reading, for when the server is reporting nonsense rather than the file
  genuinely changing.

[IMAGE: 06-content.png]

---

## Market analysis: what the world says back

The page watch answers *what changed on infineon.com*. This answers the other
half — **how engineers reacted to it**, in public, in their own words.

It is grouped by **product family** (PSOC™ MCUs, AURIX™, OPTIGA™ …), taken from
the Product-family pages already being watched. Not by part number: individual
OPNs have almost no public chatter, families have plenty.

### Where it looks

Eight public sources, polled directly. Nothing is scraped from behind a login
and nothing is bought.

| Source | What it is |
|---|---|
| **Infineon community** | Infineon's own developer-community threads, read from what each capture already stores |
| **YouTube** | Videos and talks mentioning a family |
| **GitHub** | Issues and discussions in real projects |
| **Forums** | Independent electronics forums |
| **Reddit** | Engineering and embedded subreddits |
| **Hacker News** | Stories and comments |
| **Blogs** | Posts on independent blogs |
| **StackExchange** | electronics.stackexchange and Stack Overflow |

### When it runs

**Automatically, after every check** — nobody has to remember a button. Three
rules keep it out of the way:

- **Never while a check is running.** They share the same bandwidth and the same
  bot-guards, so collection waits its turn.
- **Not after a token run.** A three-page manual poke does not earn a
  forty-minute collection pass; the run has to be a real one.
- **It paces itself** per site, deliberately politely.

There is a **Collect mentions** button for admins if you want it sooner, and the
bar underneath tells you when it last happened.

### What it has today

**5,961 mentions across 381 families**, from all eight sources — the Infineon
community and YouTube are the two loudest by a distance:

| | | | |
|---|---|---|---|
| Infineon community 1,836 | YouTube 1,700 | GitHub 970 | Forums 504 |
| Reddit 379 | Hacker News 350 | Blogs 141 | StackExchange 81 |

**Tone scoring is switched off.** Collecting mentions is pure pattern-matching
and needs no AI at all — but rating each one from negative to positive, with
topic tags, calls a language model, and that needs an API key which is not
configured. So the **Tone** figure reads "not scored yet" and will keep reading
that until somebody sets one.

*Nothing is lost in the meantime.* Every mention is collected, searchable and
browsable exactly as it is; scoring is a layer on top that can be switched on
later and applied to everything already gathered.

---

## Leaving a note

Every page, document, board and update has a notes box at the bottom.

It is for the reasoning, not the fact — *"checked with marketing, the new
subheadline is intentional, leaving it"*. Whoever opens that page next reads it
without having to ask you. The decision outlives the conversation.

Edit or delete your own whenever, mark one **done** once it is handled, and read
everything anyone has written under **Notes**.

**Something broken, confusing or missing?** There is a box at the top of the
**Notes** page for exactly that. It lands in the same feed as everything else,
so it gets read — rather than sitting in somebody's inbox.

[IMAGE: 05-notes.png]

---

## Export: getting it out as a spreadsheet

Anything on screen can leave as a workbook. You pick the rows and you pick the
columns; the numbers beside every option tell you how much you would get before
you ask for it.

**Two shapes, and which you want depends on the question.**

| | One row per | Use it for |
|---|---|---|
| **A row per page** | each tracked page | "every product page missing datasheets", or handing a business line its own list |
| **A row per document, video, board…** | each item, with where it is used | "which datasheets are on the most pages", or finding the ones on none |

- **Rows are what to include.** Tick the page types you want; ticking a family
  ticks everything under it. Nothing ticked means everything.
- **Columns are what to say about each one.** The count beside each column is how
  many of the rows you picked actually have it, so a column showing 0 will come
  out empty and you can leave it off.

> **The counts move as you tick.** They are not a fixed list — they recount
> against the rows you have chosen, so you can see the shape of the answer
> before you download it.

**Export everything** skips the building: one workbook with every page and all of
its content, then a sheet per kind of content with its own pages beside it.

---

## Which components a page type should have

A grid of page types against components, under **Settings → Components**
(admin only). It is what makes "this page is missing something" a question
iRadar can answer.

Every cell holds a number: **how many captured pages of that type actually carry
that component**. So a **0** means it has never been seen there, and a large
number means it is normal for that type.

**You tick what SHOULD be there.** The numbers describe what is; the ticks say
what is expected. Once saved, a page can be read against its type — a product
page with no documents section stands out because its type says it should have
one.

- **Until you save, nothing is expected.** The grid opens showing what the
  captured data says today, as a starting point.
- **Prune** clears components that appear on zero pages of a type — tidying, not
  a decision. Do it after a check finishes, not during one, or you will prune
  against half-collected counts.
- **Pages with no level yet** can be given one automatically from their siblings,
  their depth in the hierarchy and which components they carry. Every page's
  level stays editable on its own page.

---

## Changing when it runs

**Settings → Schedule** (admin only). It runs **every Monday at 23:00** today.

| Setting | What it means |
|---|---|
| **How often** | Every day, every two days, or weekly. Weekly is the normal choice: Infineon does not republish a page most days, and a daily check mostly re-reads pages that have not moved. |
| **Which day** | For a weekly check. |
| **What hour** | 0 to 23, **in your time**, not the server's. A check takes several hours, so an evening start finishes overnight and the queue is waiting when people arrive. |
| **Parallel browser tabs** | How many pages are opened at once. Higher is faster and heavier; too high and Infineon starts refusing us, which shows up as capture errors rather than as speed. Currently 8. |

> Changing the schedule does not touch a check already running, and never loses
> one: the next run starts at the new time. To run it now instead, use
> **Check all pages**.

---

## Where things are

| | |
|---|---|
| **Dashboard** | Every tracked page, its state, and your review queue. Filter by type, status, or by what content a page has. |
| **Content** | Every document, video, image and board across all pages, and which pages each appears on. Answers *"where is this datasheet used?"* |
| **Notes** | Everything anyone has written, newest first — and the box for telling us something is broken, confusing or missing. |
| **Reports** | Hierarchy (the site as a tree), Export (any of it as a spreadsheet), Run history (every check and what it found). |
| **Help** | This document, inside the app, with the numbers kept current. |
| **Your avatar** | Top right. Your profile, light or dark mode, sign out — and for admins, people, settings and the backup. |

---

## Accounts and roles

| Role | Can |
|---|---|
| **Admin** | Everything — people, settings, the page list, the backup, and starting a check by hand. |
| **Editor** | Review updates and edit page details. Not settings, the page list, or runs. |

Everyone can write notes. Admins manage people under the avatar menu →
**Manage people**.

Suspending someone blocks their sign-in without deleting anything they wrote,
and can be undone. Deleting cannot.

---

## Worth knowing

The things people ask about in the second week.

- **Only the last two captures of a page are kept.** Enough to compare against
  and to show you a before and after — not an archive. You cannot ask what a
  page looked like in March. *Anything an open update needs is never removed,
  however old, so nothing waiting for review can be cleared out from under you.*
- **The site is also a tree.** **Hierarchy** lays the pages out the way
  infineon.com nests them, so you can see a whole product family and everything
  under it rather than a flat list.
- **Pages carry who owns them** — BL, PL, CM and the marketing owner, shown on
  the page and available as export columns. Where a page disagrees with what was
  imported it is flagged *Ownership mismatch* for somebody to settle, never
  quietly corrected.
- **There is a small test set** — a fixed handful of pages used to try a change
  out before it goes near all of them. It is a filter on the Dashboard, not
  something that happens to a page.
- **A page can be paused** when it is known to be in flux and you do not want it
  coming up every week. It stays in the list, marked *Paused*, and is not checked.
- **Redirects are followed and merged.** If two addresses land on the same page
  it is watched once, not twice. Where a page's own address is not the one that
  answered, the page says so in a **Redirects** box under its snapshots. A
  `?tab=` on the end is not a redirect — that is the same page opening on a tab,
  often one iRadar clicked itself.
- **The server's disk is not backed up by anything**, so somebody has to keep a
  copy. An admin can download the database from **Settings → Backup**. It holds
  the page list, the ownership, every update anyone has reviewed and every note;
  the screenshots are four-fifths of the disk and none of the loss.
- **Anything on screen can be exported.**

---

## What it does not do

- **It does not find new pages by itself.** iRadar watches the pages it has been
  given. If Infineon publishes a new one, somebody has to add it under *Add
  pages*.
- **A few finder pages** only capture their first page of results.
- **Gated documents** — the ones behind a MyInfineon login — cannot be previewed,
  because all we can see is the login wall.
- **It does not watch continuously.** A change published and undone between two
  checks is a change nobody sees.
- **It does not tell anyone.** No email, no alert — the queue waits on the
  Dashboard until somebody opens it.
- **It reports; it does not judge.** Whether a change matters is your call.
  iRadar only promises to notice it.

---

## Questions and problems

Use the box at the top of the **Notes** page — it goes to the same feed as
everything else, so it gets read. For anything else, raise it with Anas or
comment on this page.

The in-app version of this guide is at **https://iradar.net/guide** and always
shows the current numbers and settings.

[IMAGE: 07-help-in-app.png]
