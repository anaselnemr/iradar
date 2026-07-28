# iRadar — website change monitoring

> **Paste-ready for Confluence.** Copy from the line below into a new page.
> Images live in `docs-images/` next to this file — drag each one in where the
> `[IMAGE: …]` marker says. Delete the markers and this block afterwards.

---

# iRadar

**https://iradar.net**

iRadar watches **4,188 Infineon.com pages** and tells you what actually changed
on them — text, documents, images, videos, boards, prices. It exists so nobody
has to open a page and try to remember what it said last month.

It checks every page once a week, on its own, and puts anything that changed in
a review queue.

---

## Getting in

Sign in with your Infineon email address. Your starting password is
`Infineon@2026`, and it will ask you to replace it immediately.

> **Please use a passphrase** — three or four random words, like
> `harbour-cactus-lantern-42`. Long beats complicated, and it is far stronger
> than something like `P@ssw0rd!`.

Your display name and password are under the avatar menu, top right.

[IMAGE: 01-sign-in.png]

---

## The weekly check

Every **Monday at 23:00**, automatically. Nobody starts it. It takes about nine
hours and is finished long before Monday morning.

1. **Every page is opened in a real browser.**
   Not fetched as HTML — actually rendered, scrolled to the bottom so anything
   that loads late appears, with tabs and accordions expanded. A page that only
   half-loads is thrown away rather than saved, because a half-loaded page
   becomes next week's reference and invents changes that never happened.

2. **What is on it is written down.**
   The visible text, every image, the whole library (documents, videos, boards,
   design resources, partners), the price, and a full-page screenshot.

3. **It is compared with last week.**
   Four independent signals — any one is enough to flag. See below.

4. **Anything flagged is captured a second time, immediately.**
   If the difference does not reappear, it was the website being inconsistent
   rather than an edit, and it is dropped. This is what removes most false
   alarms before anyone sees them.

5. **Failures get two more tries.**
   A page that times out is retried at the end of the run. Only something that
   fails every attempt is recorded as an error — a bad moment is not a verdict.

6. **What survives lands in the review queue.**

[IMAGE: 02-dashboard.png]

---

## What counts as a change

| Signal | Flags when |
|---|---|
| **Text** | Wording is added, removed or edited. Text that merely *moved* does not count — the same lines in a new order is a layout change, not an edit. |
| **Library** | A document, video, board, design resource or partner is added or removed, or one of their details changes (version, date, size, price). |
| **Images** | A picture appears on or disappears from the page. |
| **Screenshot** | More than 1.5% of the pixels differ — a real visual change, not a shifted word. |
| **File updates** | A datasheet is **republished at the same address**. The page looks identical; the file behind it is not. Listed separately under *File updates*. |

---

## What it deliberately ignores

Every one of these caused a flood of false alarms before it was handled. If you
ever wonder why something did **not** flag, it is probably here.

| Ignored | Why |
|---|---|
| Stock levels | "4387 in stock" becomes 4382 overnight. The **price** is still watched — a real price move flags. |
| The developer community feed | Re-sorts and re-dates itself constantly. Whether the widget is present, empty, or has discussions **is** still tracked. |
| "Latest news" and the mega-menu | Identical on every page. One retired thumbnail once flagged 585 pages at once. |
| Tabbed sections loading differently | Two visits can land on different tabs. Their contents are tracked item by item instead, which does not flap. |
| Timestamps and "3 days ago" | They change by themselves. |

---

## Reviewing an update

Open one from the Dashboard. You see exactly what moved, before and after.

[IMAGE: 04-an-update.png]

Then one of two buttons:

| Button | What it means | Use it when |
|---|---|---|
| **Accept** | This is the page now. It becomes the reference the next check compares against. | The change is real — whether it matters or not. |
| **Reject** | Do not use *this capture* as the reference. The previous version stays. | The capture is wrong — something did not load properly. |

**Accept is the normal one.** Reject is for when iRadar got the page wrong, not
for when you dislike the change. Either way the page returns to normal
afterwards; nothing is left hanging.

---

## Notes

Every page, document, board and update has a **Notes** section at the bottom.
Use it for the reasoning — *"checked with marketing, the new subheadline is
intentional"*. Whoever opens that page next reads it without having to ask.

You can edit or delete your own, mark any note **done** once it is handled, and
see everything in one place under **Notes** in the top bar.

[IMAGE: 06-notes.png]

---

## The rest of the site

| Section | What it is for |
|---|---|
| **Dashboard** | Every tracked page, its state, and the review queue. Filter by type, status, or what content a page has. |
| **Content** | Every document, video, image and board across all pages, and which pages each appears on. The place to answer *"where is this datasheet used?"* |
| **Hierarchy** | The whole site as a tree, built from the site's own structure. |
| **Export** | Any of it as a spreadsheet. |
| **Run history** | Every check, when it ran, and what it found. |
| **How it works** | This document, kept inside the app so it cannot go out of date. |

[IMAGE: 07-content-library.png]

---

## Accounts and roles

| Role | Can |
|---|---|
| **Admin** | Everything — manage people, settings, the page list, and start checks. |
| **Editor** | Review updates and edit page details. Not settings, the page list, or runs. |

Everyone can write notes. Admins manage people under the avatar menu →
**Manage people**.

[IMAGE: 11-manage-people.png]

---

## Known limits

- **New pages are not discovered automatically.** iRadar watches the pages it
  has been given. If Infineon publishes a new one, someone has to add it under
  *Add pages*.
- **Five finder pages** only capture their first page of results.
- **Gated documents** (behind a MyInfineon login) cannot be previewed — we only
  ever see the login wall.

---

## Problems and suggestions

Add a comment on this page, or raise it with Anas.
