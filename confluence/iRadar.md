# iRadar — how it works

**Paste everything below the line into a new Confluence page.**

Confluence Cloud accepts Markdown on paste (`/markdown` if it does not convert
automatically). Images do not come across with the text — drag each file from
`confluence/images/` into the spot marked `[IMAGE: …]`, then delete the marker.

The same content lives inside the app at **https://iradar.net/guide**, where the
numbers update themselves. This copy will drift; link to the live one where you
can.

---

# iRadar

**https://iradar.net**

iRadar opens every Infineon.com page we track, once a week, and tells you what
actually changed since last time — wording, documents, images, videos, boards
and prices.

It exists because the alternative is opening a page and trying to remember what
it said last month. At the time of writing it watches **4,188 pages** and
**199,812 individual items** across them.

---

## Getting in

Sign in with your Infineon email address. Your starting password is
`Infineon@2026`, and it will ask you to replace it straight away.

> **Use a passphrase** — three or four random words, like
> `harbour-cactus-lantern-42`. Long beats complicated, and it is far stronger
> than something like `P@ssw0rd!`.

Your display name and password are under your avatar, top right.

[IMAGE: 01-sign-in.png]

---

## What happens on a check

Every **Monday at 23:00**, on its own. Nobody starts it. It takes about nine
hours and is finished long before Monday morning.

**1. Every page is opened in a real browser.**
Not downloaded as code — genuinely rendered, the way you would see it. It
scrolls to the bottom so anything that loads late appears, opens tabs and
accordions, and waits for the images to arrive.

*A page that only half-loads is thrown away instead of saved. If it were kept it
would become next week's reference, and then everything missing from it looks
like it was deleted.*

**2. What is on it is written down.**
The visible text, every image, the whole library — documents, videos, boards,
design resources, partners — the price, and a full-page screenshot.

**3. It is compared with last time.**
Five signals, listed below. Any one of them is enough to raise the page.

**4. Anything raised is captured again, immediately.**
Before the run ends. If the difference does not show up the second time, it was
the website being inconsistent rather than an edit, and it is dropped.

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
- **Start it again and it continues.** Nothing special to do.

Run history shows a check that has not finished with no end time. That is normal
while one is running, and a sign it was interrupted if the time has passed.

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
| **Test set** | A small fixed group used to try a change out before putting it near every page. |

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
| **Reject** | Says "do not use *this capture* as the reference". The previous version stays. | The capture is wrong — something did not load. |

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
Anything the page lists, iRadar lists — grouped by what it is.

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

---

## Leaving a note

Every page, document, board and update has a notes box at the bottom.

It is for the reasoning, not the fact — *"checked with marketing, the new
subheadline is intentional, leaving it"*. Whoever opens that page next reads it
without having to ask you. The decision outlives the conversation.

Edit or delete your own whenever, mark one **done** once it is handled, and read
everything anyone has written under **Notes**.

[IMAGE: 05-notes.png]

---

## Where things are

| | |
|---|---|
| **Dashboard** | Every tracked page, its state, and your review queue. Filter by type, status, or by what content a page has. |
| **Content** | Every document, video, image and board across all pages, and which pages each appears on. Answers *"where is this datasheet used?"* |
| **Notes** | Everything anyone has written, newest first. |
| **Reports** | Hierarchy (the site as a tree), Export (any of it as a spreadsheet), Run history (every check and what it found). |
| **Help** | This document, inside the app, with the numbers kept current. |
| **Your avatar** | Top right. Your profile, light or dark mode, sign out — and for admins, people and settings. |

[IMAGE: 06-content.png]

---

## Accounts and roles

| Role | Can |
|---|---|
| **Admin** | Everything — people, settings, the page list, and starting a check by hand. |
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
- **Which parts a page has is tracked too** — whether it carries a documents
  section, a video block, a partner list. Filter the Dashboard by them to find,
  say, every product page with no datasheets. A section that disappears entirely
  is a change worth seeing.
- **A page can be paused** when it is known to be in flux and you do not want it
  coming up every week. It stays in the list, marked *Paused*, and is simply not
  checked.
- **Redirects are followed and merged.** If two addresses land on the same page
  it is watched once, not twice, so you do not review the same change twice over.
- **Anything on screen can be exported.** **Export** turns any filter and any set
  of columns into a spreadsheet.

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

Raise them with Anas, or comment on this page.

The in-app version of this guide is at **https://iradar.net/guide** and always
shows the current numbers and settings.

[IMAGE: 07-help-in-app.png]
