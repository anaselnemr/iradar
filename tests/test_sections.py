"""Tests for SECTIONS_JS - the ~1,000-line extractor that builds the per-page
library (documents, boards, features, subheadline, news...). It feeds
everything library_diff compares, and until now had ZERO offline coverage: an
AEM markup rename that silently blinded it would read as "items removed" on
every page at once, with no test failing first.

Runs the real extractor against the frozen fixtures. Skips cleanly without
fixtures or Playwright (same contract as test_extractors).
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from run_tests import Skip                       # noqa: E402
import capture as C                              # noqa: E402

FIX = HERE / "fixtures"
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.I | re.S)
_CACHE = None

# sections whose items must be {title/url/meta} dicts (not strings/derived)
ITEM_SECTIONS = ("documents", "design_resources", "videos", "trainings",
                 "partners", "news", "categories", "applications",
                 "features", "subheadline", "benefits", "products")


def _extract_sections():
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    fixtures = sorted(FIX.glob("*.html"))
    if not fixtures:
        raise Skip("no fixtures — run tests/capture_fixtures.py")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        raise Skip("playwright not installed")
    out = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        for f in fixtures:
            html = _SCRIPT_RE.sub("", f.read_text(encoding="utf-8"))
            pg.set_content(html, wait_until="load")
            s1 = pg.evaluate(C.SECTIONS_JS)
            s2 = pg.evaluate(C.SECTIONS_JS)
            out[f.stem] = (s1, s2)
        b.close()
    _CACHE = out
    return out


def test_sections_extraction_is_deterministic():
    for name, (s1, s2) in _extract_sections().items():
        assert json.dumps(s1, sort_keys=True) == json.dumps(s2, sort_keys=True), \
            f"{name}: same DOM produced two different section libraries"


def test_part_page_yields_its_library():
    # the part fixture rendered 13 documents and 23 boards when frozen; a
    # floor (not exact count) so a markup change that blinds the extractor
    # fails loudly while an extractor IMPROVEMENT does not
    s = _extract_sections()["part"][0]
    assert len(s.get("documents") or []) >= 5, "documents extraction went blind"
    assert len(s.get("design_resources") or []) >= 5, "boards extraction went blind"
    assert len(s.get("features") or []) >= 1
    assert isinstance(s.get("flags"), dict) and s["flags"], "page flags missing"


def test_application_page_yields_subheadline_and_news():
    s = _extract_sections()["application"][0]
    subs = s.get("subheadline") or []
    assert len(subs) == 1 and (subs[0].get("title") or "").strip(), \
        "subheadline extraction went blind"
    assert len(s.get("news") or []) >= 1, "news extraction went blind"
    assert len(s.get("categories") or []) >= 10, "categories extraction went blind"


def test_items_carry_title_or_url():
    # every item in an item-section must be a dict that names itself - a shape
    # break here would make library_diff compare garbage keys
    for name, (s, _) in _extract_sections().items():
        for cat in ITEM_SECTIONS:
            for it in (s.get(cat) or []):
                assert isinstance(it, dict), f"{name}.{cat}: non-dict item {it!r}"
                assert (it.get("title") or it.get("url")), \
                    f"{name}.{cat}: item with neither title nor url: {it!r}"
