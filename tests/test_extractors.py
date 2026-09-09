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


def _launch(pw):
    """Chromium, or a SKIP saying how to get one.

    Playwright pins a browser build to its own version, so a machine that has
    chromium-1234 while this Playwright wants 1228 fails with "Executable
    doesn't exist" - eleven red tests that look like broken extraction and are
    really one missing download. A skip that names the command is the honest
    result: these tests need a browser, and nothing here is failing without it.
    """
    try:
        return pw.chromium.launch()
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            raise Skip("no browser for this Playwright build - run:"
                       " python -m playwright install chromium-headless-shell")
        raise


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
        b = _launch(pw)
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
        b = _launch(pw)
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
        b = _launch(pw)
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


# --- the product submenu flyout ---------------------------------------------

def test_the_nav_submenu_is_chrome_and_stays_out_of_every_raw_signal():
    """The per-product submenu flyout published hundreds of one-word 'updates'
    on 2026-08-03 - bare 'Software' / 'Datasheet' caption lines leaving and
    returning as its hidden panels hydrated or did not. The state sticks for
    hours, so the same-run double-check agreed with the flap both times. A
    menu is navigation, never page content; it lives in VOLATILE_SEL so all
    four collectors treat it alike, and all four revision stamps moved with
    it (the rule the file states at VOLATILE_SEL)."""
    assert C._NAV_SUBMENU_SEL in C.VOLATILE_SEL, \
        "the submenu flyout is back in the raw signals"
    for sel in ("productsubmenu", "designresourcesubmenu"):
        assert sel in C._NAV_SUBMENU_SEL, f"{sel} dropped out of the exclusion"
    # the four stamps that must have moved together with this exclusion
    assert (C.TEXT_REV, C.IMG_REV, C.SHOT_REV, C.SECTIONS_REV) >= (7, 6, 8, 3), \
        "VOLATILE_SEL grew but a revision stamp stayed behind"


# --- hiding the cookie banner must never kill the capture --------------------

def test_hide_chrome_survives_the_pages_own_errors():
    """The OneTrust CSP violation surfaced through add_style_tag - the first
    call in flight after navigation - and killed 219 captures per run over a
    cookie banner. The guard tries the normal call, falls back to planting the
    <style> element itself, and only if BOTH fail does it fail the capture -
    loudly and as a transient, because a capture without HIDE_CSS would show
    the consent overlay and diff against every baseline without one."""
    import asyncio

    class Page:
        def __init__(self, style_fails=False, eval_fails=False):
            self.styled = self.evaled = None
            self.style_fails, self.eval_fails = style_fails, eval_fails

        async def add_style_tag(self, content=""):
            if self.style_fails:
                raise Exception("Page.add_style_tag: Connecting to "
                                "'https://cookies-data.onetrust.io' violates "
                                "the following Content Security Policy")
            self.styled = content

        async def evaluate(self, js, arg=None):
            if self.eval_fails:
                raise Exception("Execution context was destroyed")
            self.evaled = arg

    # the normal path: one call, no fallback
    p = Page()
    asyncio.run(C._hide_chrome(p))
    assert p.styled == C.HIDE_CSS and p.evaled is None

    # the CSP tantrum: fallback plants the style element with the same CSS
    p = Page(style_fails=True)
    asyncio.run(C._hide_chrome(p))
    assert p.evaled == C.HIDE_CSS, "the fallback must inject the same CSS"

    # both dead: the capture fails as TRANSIENT (the runner retries it),
    # never proceeds to photograph a page wearing the consent overlay
    p = Page(style_fails=True, eval_fails=True)
    try:
        asyncio.run(C._hide_chrome(p))
        assert False, "an un-maskable page must fail the capture"
    except Exception as exc:
        assert C.BLOCKED_TRANSIENT in str(exc), \
            "the failure must be transient so the retry rounds get it"


# --- the page under the camera must be the page on the watch list ------------

def test_a_wandered_page_is_an_error_and_our_own_clicks_are_not():
    """_expand_interactive clicks ~100 controls; one that hard-navigates used
    to leave every downstream read photographing the WRONG page under status
    ok - the maximal false update. The assertion holds the line at the two
    points of no return, ignores our own fragment/query churn, and fails as
    a transient so the runner retries with the baseline kept."""

    class Page:
        def __init__(self, url):
            self.url = url

    start = "https://www.infineon.com/part/XMC1100#news-highlights"
    # our own clicking appends fragments and queries - never an error
    C._assert_still_on(Page(start), start, "expansion")
    C._assert_still_on(Page("https://www.infineon.com/part/XMC1100#docs"),
                       start, "expansion")
    C._assert_still_on(Page("https://www.infineon.com/part/XMC1100?tab=2"),
                       start, "expansion")
    # a hard navigation to another page is the failure this exists for
    try:
        C._assert_still_on(Page("https://www.infineon.com/part/OTHER-PART"),
                           start, "expansion")
        assert False, "a foreign page must fail the capture"
    except Exception as exc:
        assert "wandered off" in str(exc)
        # and the runner must RETRY it (baseline kept), not record a verdict
        import runner as R
        res = {"status": "error", "error": str(exc)}
        assert R._transient_failure(res, {"last_status": "unchanged"}), \
            "a wander must land in the retry rounds, not as a final state"


def _sections_of(html):
    """Run the real SECTIONS_JS against a DOM built here, offline."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        raise Skip("playwright not installed")
    with sync_playwright() as pw:
        b = _launch(pw)
        pg = b.new_page()
        pg.set_content(html, wait_until="load")
        out = pg.evaluate(C.SECTIONS_JS)
        b.close()
    return out


# The shape of an Infineon part page: one .tab-content holding a pane per tab.
# The first pane is the parameter table; Features and Benefits are panes of
# their own, and Benefits carries a single bullet.
_TABBED = """
<div class="tab-content">
  <div class="tab-pane show active">
    <ul>
      <li>ADC (#, Max. Resolution @ Sample Rate) SAR (1, 12-bit @ 1 msps)</li>
      <li>BLE Power Output 4 dBm</li>
      <li>Flash 512 kByte</li>
      <li>Operating Temperature range -40 &deg;C to 85 &deg;C</li>
    </ul>
  </div>
  <div class="tab-pane">
    <h2>Features</h2>
    <ul><li>150-MHz Arm Cortex-M4F (CM4) CPU</li>
        <li>100-MHz Cortex-M0+ (CM0+) CPU</li></ul>
  </div>
  <div class="tab-pane">
    <h2>Benefits</h2>
    <ul><li>Support fast time-to-market</li></ul>
  </div>
</div>
"""


def test_a_tab_pane_is_the_section_and_the_walk_never_leaves_it():
    """CY8C6316BZI-BLF03 stored 38 benefits, of which 37 were the parameter
    table and the features bullets from neighbouring panes. The Benefits pane
    holds ONE bullet, which failed the "container needs 2 list items" test, so
    the walk went up to .tab-content - the box that holds every other tab."""
    sec = _sections_of(_TABBED)
    ben = [b["title"] for b in sec["benefits"]]
    feat = [f["title"] for f in sec["features"]]
    assert ben == ["Support fast time-to-market"], ben
    assert feat == ["150-MHz Arm Cortex-M4F (CM4) CPU",
                    "100-MHz Cortex-M0+ (CM0+) CPU"], feat
    # the parameter table belongs to neither, and lands in neither
    assert not [b for b in ben + feat if "BLE Power Output" in b]


def test_an_empty_tab_means_the_page_has_no_such_section():
    """The XMC5000 parts have a Features tab with nothing in it; the walk used
    to climb out and return the 26-row parameter table instead. No bullets in
    the pane is not a reason to go looking somewhere else."""
    sec = _sections_of("""
      <div class="tab-content">
        <div class="tab-pane show active">
          <ul><li>CAN Nodes 3</li><li>CPU ARM Cortex-M4F</li>
              <li>DSP Functionality Yes</li><li>Flash 576 kByte</li></ul>
        </div>
        <div class="tab-pane"><h2>Key features</h2></div>
      </div>""")
    assert sec["features"] == [], sec["features"]


def test_an_untabbed_page_still_finds_its_list_by_walking_up():
    """Product pages are not tabbed - the walk up is still how those work.

    The filler is not decoration. The walk stops climbing once a container
    passes a third of the page, so on a page that IS little more than this
    section the guard fires immediately and nothing is found. Real pages carry
    thousands of characters around the section, so this one has to as well.
    """
    filler = "<p>%s</p>" % ("Infineon page text. " * 300)
    sec = _sections_of("""
      <section><h2>Key features</h2>
        <div><ul><li>Exceptional small form factor</li>
                 <li>High accuracy CO2 level readings</li></ul></div>
      </section>""" + filler)
    assert [f["title"] for f in sec["features"]] == [
        "Exceptional small form factor", "High accuracy CO2 level readings"]


def test_the_one_third_guard_is_measured_against_the_page_a_reader_sees():
    """It is measured against document.body.textContent, which counts the
    text inside every <script> tag. On a real part page that is 495,145
    characters where the readable page is 8,727 - so the guard sits at 168,349
    and can never fire, on any section, on any page.

    Nothing depends on it any more for tabbed pages (a pane is the section),
    but the number is wrong and this test says so out loud rather than leaving
    it to be rediscovered.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        raise Skip("playwright not installed")
    blob = '{"x":"%s"}' % ("y" * 5000)
    with sync_playwright() as pw:
        b = _launch(pw)
        pg = b.new_page()
        pg.set_content("<p>Short visible page.</p>"
                       "<script type=\"application/json\">%s</script>" % blob,
                       wait_until="load")
        seen, real = pg.evaluate(
            "() => [document.body.textContent.length,"
            "       document.body.innerText.length]")
        b.close()
    assert seen > real * 50, (seen, real)
