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
  never leaks into page text; tracking / community / no-image URLs never appear
  as images; no line is duplicated absurdly (the filter-dropdown / mega-menu
  bug). Skips cleanly if there are no fixtures or Playwright is absent.

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
