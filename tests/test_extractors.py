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

    # lines that exist ONLY inside the mega-menu flyout panels in this DOM
    # (in the panels, and nowhere outside them) - used by the flyout-leak test
    _FLYOUT_ONLY_JS = r"""
    () => {
      const SUB = '.secondary-navigation-submenu';
      const grab = root => {
        const s = new Set();
        const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        let n;
        while (n = tw.nextNode()) {
          const t = n.textContent.replace(/\s+/g, ' ').trim();
          if (t.length > 15) {
            const inSub = !!(n.parentElement && n.parentElement.closest(SUB));
            if (root === document.body ? !inSub : true) s.add(t);
          }
        }
        return s;
      };
      const outside = grab(document.body);
      const inside = new Set();
      document.querySelectorAll(SUB).forEach(p => grab(p).forEach(t => inside.add(t)));
      return Array.from(inside).filter(t => !outside.has(t)).slice(0, 40);
    }
    """
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
            fly = pg.evaluate(_FLYOUT_ONLY_JS)
            # img srcs that exist ONLY inside the news box in this DOM
            newsimgs = pg.evaluate("""
              () => {
                const inside = new Set(), outside = new Set();
                document.querySelectorAll('img').forEach(i => {
                  const s = i.currentSrc || i.src;
                  if (!s) return;
                  (i.closest('.news-container') ? inside : outside).add(s);
                });
                return Array.from(inside).filter(s => !outside.has(s));
              }""")
            # lines that exist ONLY inside the volatile-but-structured
            # sections in this DOM (design-res / documents / partners / buy)
            vol = pg.evaluate("""
              (sel) => {
                const inside = new Set(), outside = new Set();
                const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let n;
                while (n = tw.nextNode()) {
                  const t = n.textContent.replace(/\\s+/g, ' ').trim();
                  if (t.length <= 12) continue;
                  let inSel = false;
                  try { inSel = !!(n.parentElement && n.parentElement.closest(sel)); } catch(e) {}
                  (inSel ? inside : outside).add(t);
                }
                return Array.from(inside).filter(t => !outside.has(t)).slice(0, 40);
              }""", C.VOLATILE_SEL)
            out[f.stem] = {"text1": t1, "text2": t2, "images": imgs,
                           "flyout_only": fly, "news_only_imgs": newsimgs,
                           "volatile_only": vol}
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


# --- live feeds (developer community + Latest news) are chrome, not text ----

def test_no_live_feed_leaks_into_text():
    # The #community forum widget and the .news-container "Latest news" feed
    # both re-sort / re-date themselves between two captures of an unchanged
    # page - they read as 48+ false "updates" in one run. Both are dropped from
    # the text walk (the forum is tracked on its own as developer_community; the
    # news feed is identical global chrome). These strings live INSIDE those
    # containers, so none may reappear in the extracted text. The stable
    # "Developer community" section-nav TAB label is deliberately NOT listed: it
    # sits outside the widget, is identical every capture, and must stay.
    FEED = ["Latest news", "View all news",              # .news-container
            "Ask the community", "View all discussions"]  # #community feed
    for name, r in _extract_all().items():
        leaked = [m for m in FEED if m in r["text1"]]
        assert not leaked, (f"{name}: live-feed text leaked into page text "
                            f"{leaked} - the #community / .news-container "
                            f"exclusion regressed")


# --- mega-menu flyout is chrome, not page text ------------------------------

def test_no_volatile_section_leaks_into_text():
    # The design-resource tabs / documents table / partners / buy-box are
    # tabbed/paginated/lazy - two captures of one page land on different tabs,
    # so the raw walk grabbed different content and 800+ pages flapped on a
    # fresh-vs-fresh run. They are tracked item-by-item in the structured
    # library instead. Self-calibrating: lines that exist ONLY inside those
    # containers in this DOM must not appear in the extracted text.
    for name, r in _extract_all().items():
        lines = set(r["text1"].split("\n"))
        leaked = [m for m in r.get("volatile_only", []) if m in lines]
        assert not leaked, (f"{name}: {len(leaked)} volatile-section line(s) "
                            f"leaked into page text, e.g. {leaked[:3]} - the "
                            f"design-res/documents/partner exclusion regressed")


def test_no_menu_flyout_leaks_into_text():
    # The mega-menu flyout panels (.secondary-navigation-submenu) carry the
    # same ~1,900-line global product taxonomy on every page, and it flaps
    # between captures (hydration + a live rebrand) - 10 of one run's 62 false
    # "updates" were nothing but this menu. The walk drops the panels; the
    # top-bar labels and the breadcrumb live outside them and must stay.
    # Markers are self-calibrating: lines that exist ONLY inside the panels in
    # this fixture's DOM, so a body line that legitimately repeats menu text
    # can never fail the test.
    for name, r in _extract_all().items():
        # exact-line match: a marker may legitimately occur as a SUBSTRING of a
        # longer body line ("... Product Information ...") - only a line that
        # IS the flyout text is a leak
        lines = set(r["text1"].split("\n"))
        leaked = [m for m in r["flyout_only"] if m in lines]
        assert not leaked, (
            f"{name}: {len(leaked)} mega-menu flyout line(s) leaked into page "
            f"text, e.g. {leaked[:3]} - the .secondary-navigation-submenu "
            f"exclusion regressed")


# --- image filters ---------------------------------------------------------

def test_no_tracking_or_community_images():
    # community.cypress.com: the LEGACY community host - its rotating thread
    # avatars flagged 400 migrated part pages in one run before it joined
    # the exclusion alongside community.infineon.com
    bad = re.compile(r"qualtrics|siteintercept|community\.infineon\.com"
                     r"|community\.cypress\.com|missing-product-images", re.I)
    for name, r in _extract_all().items():
        leaked = [u for u in r["images"] if bad.search(u or "")]
        assert not leaked, f"{name}: non-content image leaked: {leaked[:2]}"


def test_no_news_box_thumbnails_in_images():
    # the "Latest news" press thumbnails rotate site-wide - one retired press
    # image removed itself from 585 pages' inventories in run 127. Markers are
    # self-calibrating: srcs that exist ONLY inside .news-container in the DOM.
    for name, r in _extract_all().items():
        leaked = [u for u in r["news_only_imgs"] if u in set(r["images"])]
        assert not leaked, (f"{name}: news-box thumbnail leaked into the image"
                            f" inventory: {leaked[:2]}")


def test_images_look_like_images():
    ok = re.compile(r"\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)(\?|#|:|$)"
                    r"|/is/image/|/jcr:content/renditions/", re.I)
    for name, r in _extract_all().items():
        junk = [u for u in r["images"] if u and not ok.search(u)]
        assert not junk, f"{name}: non-image URL collected: {junk[:2]}"


# --- the price / stock block -----------------------------------------------

def test_the_price_block_is_excluded_and_still_extracted():
    # It has to be BOTH: out of the raw walk (an orderable part reprints its
    # stock as a bare number that ticks daily - two parts flagged +3/-3 on
    # nothing but 4387 -> 4382) and still read into `price`, where a real price
    # move is news and "N in stock" is stripped before comparing. One selector
    # feeds both, so they cannot drift apart and quietly stop watching price.
    assert C.PRICE_SEL in C.VOLATILE_SEL, \
        "the price block is no longer excluded from the raw signals"
    assert "price-container" in C.SECTIONS_JS, \
        "the price is no longer extracted structurally"
    for sel in ("price-container", "buyingoption", "product-price"):
        assert sel in C.PRICE_SEL, f"{sel} dropped out of PRICE_SEL"


# --- lazy images ------------------------------------------------------------

def test_settle_promotes_lazy_images_and_waits():
    # Scrolling only STARTS a lazy image. Nothing waited for one to arrive, so
    # the inventory could be read mid-flight and a page came back with fewer
    # images than it has - four XMC parts flagged "+5 images" for partner logos
    # that were on the page the whole time.
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        raise Skip("playwright not installed")
    html = """
      <img id="a" loading="lazy" src="data:image/gif;base64,R0lGODlhAQABAAAAACw="
           data-src="https://example.invalid/real-a.png">
      <img id="b" data-original="https://example.invalid/real-b.png">
      <img id="c" src="https://example.invalid/real-c.png">
    """
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        # the host does not resolve, so every image ERRORS - which is the point:
        # settle waits for images to finish, not to succeed, and one dead URL
        # must not burn the whole budget
        pg.set_content(html, wait_until="load")
        import time
        t0 = time.time()
        res = pg.evaluate(C.IMAGES_SETTLE_JS, 4000)
        took = time.time() - t0
        srcs = pg.evaluate(
            "() => Array.from(document.images).map(i => i.getAttribute('src'))")
        b.close()

    assert res["total"] == 3
    assert any("real-a" in (s or "") for s in srcs), \
        "a data: placeholder was not replaced from data-src"
    assert any("real-b" in (s or "") for s in srcs), \
        "an empty src was not filled from data-original"
    assert pg.is_closed() or True
    assert took < 8, f"settle took {took:.1f}s - the cap is not holding"


def test_settle_never_blocks_forever_on_a_wedged_image():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        raise Skip("playwright not installed")
    import time
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        # a request that never answers: settle must give up on the cap
        pg.route("**/hang.png", lambda route: None)
        pg.set_content('<img src="https://example.invalid/hang.png">',
                       wait_until="domcontentloaded")
        t0 = time.time()
        pg.evaluate(C.IMAGES_SETTLE_JS, 1500)
        took = time.time() - t0
        b.close()
    assert took < 6, f"a wedged image held the capture for {took:.1f}s"


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
