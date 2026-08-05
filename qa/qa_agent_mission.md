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
   `python qa/qa_shots.py "<PAGE_URL>" "<QA_DIR>"`  (from the repo root, in
   the project's virtual environment)
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

DOCUMENT TYPE + BACKEND  (check these explicitly)
- the documents table has a Type column (Document | Type | File | Access
  rights | Language | Version | Size | Date). Every stored document must
  carry `dtype` EQUAL to that row's Type cell - not its title, not its
  format. A "New" badge on a row must not disturb it.
- backend location is per DOCUMENT, never per page: DAM or Windchill by
  type. One page can legitimately show both.
- images and videos are always DAM.

FAMILY MODULE (related products / related applications)
- the same carousel is named differently per page ("Related products",
  "Related applications"). Every card in it must be stored, with its
  group = the page's own anchor-nav tab label for that section, and the
  CTA label ("View product family") in its meta.

BREADCRUMB / HIERARCHY
- `breadcrumb` must match the page's own JSON-LD BreadcrumbList
- a 404/error page's breadcrumb must NOT be stored

PRESENCE-ONLY FLAGS (contents deliberately NOT captured)
- `flags.product_table` and `flags.developer_community`: each must say
  whether the block exists and whether it has content. "Exists but empty"
  is a real state (the community renders "there are no questions") and is
  different from absent. Verify both against the live page. Do NOT report
  the absence of their content as a gap - that is deliberate.

GAPS
- name any content section visible on the page that has NO library
  category at all (except the two presence-only blocks above)

## Output (final message = the report)
VERDICT: PASS | WARN | FAIL
URL: <url>
FINDINGS:
- <one line per check with numbers, e.g. "documents stored=47 live=47 OK">
IMAGES: <what you verified>
GAPS: <sections with no category, or "none">
Also append one JSON line {"url":..., "verdict":..., "findings":[...]} to
the log named in your spawn prompt (qa/ai_qa_log.jsonl by default; the qa/
*.jsonl files are gitignored).

Rules: never modify project files; never start/stop servers or runs; a big
capture run is in progress — your read-only page loads are fine.
