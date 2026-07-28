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

> **Accept is the normal one.** Reject is for when iRadar got the page wrong, not
> for when you dislike what Infineon published. Either way the page returns to
> normal — nothing is left hanging.

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

## What it does not do

- **It does not find new pages by itself.** iRadar watches the pages it has been
  given. If Infineon publishes a new one, somebody has to add it under *Add
  pages*.
- **A few finder pages** only capture their first page of results.
- **Gated documents** — the ones behind a MyInfineon login — cannot be previewed,
  because all we can see is the login wall.
- **It reports; it does not judge.** Whether a change matters is your call.
  iRadar only promises to notice it.

---

## Questions and problems

Raise them with Anas, or comment on this page.

The in-app version of this guide is at **https://iradar.net/guide** and always
shows the current numbers and settings.

[IMAGE: 07-help-in-app.png]
