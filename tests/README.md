# Tests

Offline regression tests. No pytest, no install, no network, no live database.

```sh
.venv/Scripts/python.exe tests/run_tests.py            # everything (~3s)
.venv/Scripts/python.exe tests/run_tests.py compare    # only matching files
```

Exit code is non-zero if anything fails, so it drops straight into CI or a
pre-commit hook.

## Why this exists

For a long time a change to the extractor or the diff logic could only be
checked by a live, hours-long, network-dependent capture run — and several
plausible-but-wrong fixes shipped because there was no fast check. These run in
seconds. **When you find a bug, add a test:** it should fail before the fix and
pass after.

## What's covered

- **test_compare.py** — the diff logic that decides whether a page updated:
  add/remove counts, ignore-patterns, and the two that bit hardest —
  a re-ordered block is *not* an update (multiset check), and duplication is
  *not* a re-order (it must still flag). Pure Python, tiny temp-dir snapshots.
- **test_classification.py** — `db.type_level_for` / `ptype_for_url` /
  `dr_level_for` / `strip_lang` / `strip_junk_params`. Pure functions.
- **test_extractors.py** — runs the real `TEXT_JS` / `IMAGES_JS` against
  **frozen HTML fixtures** (offline). Encodes field fixes as invariants:
  extraction is deterministic on a fixed DOM; the header language switcher
  never leaks into page text; the live feeds (developer-community forum,
  "Latest news") and the mega-menu flyout panels never leak into page text;
  tracking / community / no-image URLs never appear as images; no line is
  duplicated absurdly. Skips cleanly if there are no fixtures or Playwright
  is absent.
- **test_sections.py** — runs the real `SECTIONS_JS` (the library extractor)
  against the fixtures: deterministic, part pages yield their documents and
  boards, application pages their subheadline and news, every item names
  itself. A markup rename that blinds the extractor fails here first.
- **test_dom_mutations.py** — the END-TO-END sight test: mutates the frozen
  page DOM itself (edit a feature line, rename a document, wipe the documents
  pane, edit the subheadline, remove an image), runs the full
  extract-then-compare chain, and asserts the change is seen. A field the
  extractor silently stopped reading fails here and nowhere else.
- **test_battery.py** — the compare mutation battery on synthetic snapshots:
  every class of edit that must flag (text/images/visual/videos/documents/
  subheadline/price), every trap that must stay silent (reorder, stock tick,
  lazy poster, ignored patterns, rev boundary), robustness (truncated
  screenshot, corrupt/missing sections.json), and the settle rule (an
  accepted mutant compares clean against itself).
- **test_load_guard.py** — the section-load-failure guard: the "could not be
  loaded" apology only counts when it is ADDED vs the baseline, so the
  permanent pricing-box apology on part pages never trips it.
- **test_assets.py** — the file-fingerprint logic that catches a document or
  image republished at the SAME URL (validator diff rules, asset-URL
  classification). Pure logic, no network.

## Fixtures

`test_extractors.py` needs frozen pages in `tests/fixtures/`. Capture or refresh
them (needs network, best when no run is active):

```sh
.venv/Scripts/python.exe tests/capture_fixtures.py            # all shapes
.venv/Scripts/python.exe tests/capture_fixtures.py home part  # just these
```

Each page is loaded, expanded exactly like a real capture, and its rendered DOM
frozen to `<name>.html` (plus `<name>.golden.json` for reference). The fixtures
deliberately contain the header language switcher, a carousel, the products
table and community images, so the invariant tests are testing something real.

Fixtures are large HTML; keep them out of git if the repo cares about size
(they regenerate in a minute).
