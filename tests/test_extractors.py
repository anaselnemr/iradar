"""Tests for the browser extractors (TEXT_JS / IMAGES_JS in capture.py).

These run the CURRENT extractors against FROZEN HTML fixtures (offline, no
network), so a change to the extractor is checked in seconds instead of a
multi-hour live run. Each assertion encodes a fix from the field:

  - language switcher chrome must not appear as page text        (churn)
  - the same DOM must extract identically twice                  (determinism)
  - tracking/community/no-image URLs must not appear as images   (false images)
  - no line may be duplicated absurdly                           (dropdown/menu dup)

If there are no fixtures (run tests/capture_fixtures.py) or Playwright is not
installed, every test SKIPs cleanly rather than failing.
"""
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from run_tests import Skip                       # the runner's skip signal  # noqa: E402
import capture as C                                                          # noqa: E402

FIX = HERE / "fixtures"
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.I | re.S)

_CACHE = None


def _extract_all():
    """Load every fixture's frozen DOM once, run the extractors, cache results.
    Scripts are stripped so the frozen DOM cannot mutate itself - the point is
    to test the extractor against exactly what was captured."""
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
            t1 = pg.evaluate(C.TEXT_JS)
            t2 = pg.evaluate(C.TEXT_JS)
            imgs = pg.evaluate(C.IMAGES_JS)
            out[f.stem] = {"text1": t1, "text2": t2, "images": imgs}
        b.close()
    _CACHE = out
    return out


# --- determinism -----------------------------------------------------------

def test_extraction_is_deterministic_on_a_fixed_dom():
    for name, r in _extract_all().items():
        assert r["text1"] == r["text2"], f"{name}: same DOM extracted differently"


# --- language switcher is chrome, not page text ----------------------------

def test_no_bare_language_names_in_text():
    LANG = {"English", "中文", "日本語"}
    for name, r in _extract_all().items():
        lines = r["text1"].split("\n")
        hits = [l for l in lines if l.strip() in LANG]
        assert not hits, (f"{name}: language-switcher chrome leaked into text "
                          f"({len(hits)} line(s)) — header exclusion regressed")


# --- image filters ---------------------------------------------------------

def test_no_tracking_or_community_images():
    bad = re.compile(r"qualtrics|siteintercept|community\.infineon\.com"
                     r"|missing-product-images", re.I)
    for name, r in _extract_all().items():
        leaked = [u for u in r["images"] if bad.search(u or "")]
        assert not leaked, f"{name}: non-content image leaked: {leaked[:2]}"


def test_images_look_like_images():
    ok = re.compile(r"\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)(\?|#|:|$)"
                    r"|/is/image/|/jcr:content/renditions/", re.I)
    for name, r in _extract_all().items():
        junk = [u for u in r["images"] if u and not ok.search(u)]
        assert not junk, f"{name}: non-image URL collected: {junk[:2]}"


# --- duplication (filter dropdowns / mega-menu) ----------------------------

def test_no_line_is_duplicated_absurdly():
    # the filter-dropdown bug rendered the whole taxonomy 6-7x; a real page
    # repeats a short label a handful of times at most. 12 is a generous cap
    # that still catches a block emitted many times over.
    for name, r in _extract_all().items():
        counts = Counter(l for l in r["text1"].split("\n") if len(l) > 25)
        worst = counts.most_common(1)
        if worst and worst[0][1] > 12:
            raise AssertionError(
                f"{name}: line repeated {worst[0][1]}x — {worst[0][0][:50]!r} "
                f"(duplication regressed?)")
