# AI QA reviewer mission — the bulletproof gate

A crawler stored a "library" (structured content + screenshot + text) for an
infineon.com page. Your job: LOOK at the live page with your own eyes (via
screenshots you take) and judge whether the stored library is complete and
correct. You are the authority — the mechanical checker is often wrong.

## Inputs (in your spawn prompt)
- PAGE_URL — the live page
- SNAP_DIR — stored snapshot: sections.json (the library), shot.png
  (stored full-page screenshot), images.json (image inventory), text.txt
- QA_DIR — where to put your own screenshots

## Procedure
1. Photograph the live page yourself:
   `C:\Users\anasn\Desktop\InfineonWatch\.venv\Scripts\python.exe C:\Users\anasn\AppData\Local\Temp\claude\C--Users-anasn-Desktop-InfineonWatch\1dea52dc-b242-4778-bd23-7f25b9940f3d\scratchpad\qa_shots.py "<PAGE_URL>" "<QA_DIR>"`
   (shoots: page top, every documents-table page, every design-resources tab
   and each of its pages, the news section, the full page)
2. Read sections.json + images.json from SNAP_DIR.
3. VIEW your screenshots (Read tool on the PNGs) and compare like a
   meticulous human reviewer. Tolerate live-site variance (one new document,
   a swapped banner); be strict on systematic misses.

## The checklist — every point agreed with the user
DOCUMENTS
- count matches the live table across ALL pagination pages
- EN/JA/CN variants of one title are separate documents (never merged)
- login-gated (MyInfineon lock) rows are present
- meta carries type | format | access | language | version | size | DATE
- titles are clean (no "…GuideNewPDFPublicEN01_003.64MB…" concatenation,
  no bare codes lost, no "Reset all"/filter text)

DESIGN RESOURCES
- EVERY tab of the live widget (Boards & designs, PCB design data,
  Simulation models, Development tools, Embedded software, Services,
  Partners, Training, whatever exists) is a group in the library
- each group's count matches the live tab, pagination fully walked
  (tabs can have 14+ pages at 5/page — click through ALL of them)
- card titles are the card's real title (not its description, not the
  category chip, not "Read more"); distinct products that share one vendor
  URL are all present (e.g. Multilink Universal / Multilink ARM)

OTHER SECTIONS
- Trainings & webinars: present when the page has them, with video URLs
  and language lists
- News & highlights: video cards with duration and date
- Key features / Benefits / Applications / Overview: present when visible
- Browse by category: every category link, once each, pointing at the
  category page (not its product table)
- About tabs: captured in sections.json but deliberately NOT displayed —
  their presence in the JSON is correct, do not report it as a defect

LINKS  (check these explicitly)
- fetch a sample of the stored item URLs: they must return HTTP 200. A
  library of dead links is not a groundtruth.
- Infineon wires many cards in JavaScript: <a class="anchor-title" rel="">
  with NO href, or a card div with onclick="playVideo(this)". The URL is
  recovered from the page's own /dataApi/ JSON feed (assetDmPath) or from
  the id attribute of the .videos element. So a card being href-less in the
  DOM is NOT an excuse for a missing link - report any item that has a real
  destination on the live page but no URL in the library.
- the ONLY acceptable link-less items are genuinely login-gated ones
  (.exclusive-video / MyInfineon lock), and those must say so in their meta
  ("login required"). A silent blank URL is a defect.

IMAGES  (check these explicitly)
- images.json / sections.json images list every real image on the page
  (data: URIs excluded); count is within ~2 of what you see
- alt-text titles are preserved where the page has them
- every image URL is absolute (starts with http) and points at the asset
- spot-check 2-3 image URLs against what is visibly on the page

CAPTURE INTEGRITY
- stored shot.png: fully rendered (not blank, not half-loaded), exactly
  1440px wide (no white margin from the wide products table), tall enough
  to cover the page
- NO "Feedback"/Qualtrics widget anywhere in the screenshot or the text
- NO sticky navigation bar repeated down the screenshot
- stored page title is the real page title
- text.txt contains hidden-tab content (that is by design)

GAPS
- name any content section visible on the page that has NO library
  category at all

## Output (final message = the report)
VERDICT: PASS | WARN | FAIL
URL: <url>
FINDINGS:
- <one line per check with numbers, e.g. "documents stored=47 live=47 OK">
IMAGES: <what you verified>
GAPS: <sections with no category, or "none">
Also append one JSON line {"url":..., "verdict":..., "findings":[...]} to
C:\Users\anasn\AppData\Local\Temp\claude\C--Users-anasn-Desktop-InfineonWatch\1dea52dc-b242-4778-bd23-7f25b9940f3d\scratchpad\ai_qa_log.jsonl

Rules: never modify project files; never start/stop servers or runs; a big
capture run is in progress — your read-only page loads are fine.
