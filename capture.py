"""Page capture engine.

Renders each URL in a real Chromium browser (Playwright), then saves three
artifacts per page into a snapshot folder:

  text.txt      - every text node in the DOM, including content hidden behind
                  inactive tabs / collapsed accordions (a screenshot alone
                  would miss those)
  images.json   - sorted list of every image URL on the page
  shot.png      - full-page screenshot (after auto-scroll + tab expansion)
  meta.json     - title, final URL, timings

Dynamic-content handling: waits for JS, auto-scrolls to trigger lazy loading,
clicks tabs / expands accordions / presses "load more" buttons (outside the
site header/nav), then extracts.
"""
import asyncio
import json
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

VIEWPORT = {"width": 1440, "height": 900}
NAV_TIMEOUT_MS = 60_000
PAGES_PER_BROWSER = 100          # restart the browser periodically (memory hygiene)

# Testing hook: if set to a JS function string, it is evaluated on the page
# right before extraction/screenshot. Used by the self-test to simulate a
# website change (edit text/images in the live DOM). Never set in normal runs.
INJECT_JS = None

# Cookie/consent overlays are hidden so they never pollute screenshots.
HIDE_CSS = """
#onetrust-consent-sdk, #usercentrics-root, #usercentrics-cmp-ui,
#CybotCookiebotDialog, #truste-consent-track, .cc-window,
[id*="cookie-banner" i], [class*="cookie-banner" i],
[id*="cookieconsent" i], [class*="cookieconsent" i] {
    display: none !important; visibility: hidden !important;
}
html, body { overflow: auto !important; }
*, *::before, *::after {
    animation: none !important;
    transition: none !important;
    caret-color: transparent !important;
}
"""

BANNER_SELECTORS = ("#onetrust-consent-sdk, #usercentrics-root, #usercentrics-cmp-ui, "
                    "#CybotCookiebotDialog, #truste-consent-track, .cc-window")

# Extracts ALL text nodes (textContent walk), so text inside hidden tab
# panels and collapsed sections is captured too.
TEXT_JS = r"""
() => {
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','IFRAME',
                        'SVG','CANVAS','VIDEO','AUDIO','OBJECT']);
  const out = [];
  const walk = (node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      const t = node.textContent.replace(/\s+/g, ' ').trim();
      if (t) out.push(t);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (SKIP.has(node.tagName)) return;
    for (const c of node.childNodes) walk(c);
  };
  if (document.body) walk(document.body);
  return out.join('\n');
}
"""

IMAGES_JS = r"""
() => {
  const urls = new Set();
  document.querySelectorAll('img').forEach(img => {
    const src = img.currentSrc || img.src;
    if (src && !src.startsWith('data:')) urls.add(src);
  });
  document.querySelectorAll('source[srcset]').forEach(s => {
    s.srcset.split(',').forEach(part => {
      const u = part.trim().split(/\s+/)[0];
      if (u && !u.startsWith('data:')) {
        try { urls.add(new URL(u, location.href).href); } catch (e) {}
      }
    });
  });
  document.querySelectorAll('video[poster]').forEach(v => {
    try { urls.add(new URL(v.getAttribute('poster'), location.href).href); } catch (e) {}
  });
  return Array.from(urls).sort();
}
"""

# Structured extraction: documents / design resources / news & highlights /
# trainings & webinars / videos / images - each with title + URL (+ row meta
# like size/date for document tables). This is the per-page content library.
SECTIONS_JS = r"""
() => {
  // Infineon (AEM) component anchors - checked first; heading-text heuristics
  // are only a fallback for page types without these components.
  const COMPONENTS = [
    ['documents',        '#documents, .documents_ifx, .ifx_documents_products'],
    ['design_resources', '.designresource-component, div#design-resources'],
    ['news',             '[class*="news-highlight" i], [class*="newsHighlight" i]'],
    ['trainings',        '[class*="training-component" i], [id*="trainings" i]'],
  ];
  const CATS = [
    ['documents',        /^(documents?|documentation|further documentation)$/i],
    ['design_resources', /^(design resources?|boards?( & designs?)?|development tools|kits & boards)$/i],
    ['news',             /^(news( & highlights?)?|highlights?)$/i],
    ['trainings',        /^((support & )?trainings?( & webinars?)?|webinars?)$/i],
    ['videos',           /^videos?$/i],
  ];
  const GENERIC_LINK = /^(explore more|learn more|read more|read on|get started|discover( more)?|show (more|all)|see (more|all)|view (more|all)|more|download|jump to|overview)$/i;
  const NAV_LABEL = /^(product details|documents|design resources|support|about|overview|quicklinks?)$/i;

  const out = {documents: [], design_resources: [], news: [],
               trainings: [], videos: [], images: []};
  const seen = new Set();
  const urlOwned = new Map();   // url -> category (cross-category dedupe)
  const bodyLen = document.body ? document.body.textContent.length : 0;

  // Tab strips / buttons are NEVER content, even inside a trusted component.
  const isTabStrip = (el) =>
    !!el.closest('button, [role="tablist"], [role="tab"], .nav-tabs, .maintabs, ' +
      '#stickyNavTab, [class*="breadcrumb" i], [class*="tabbar" i], [class*="tab-nav" i]');
  // Page chrome only disqualifies content in the heading-fallback pass -
  // some Infineon sections (e.g. the design-resources teaser strip) legally
  // live inside a <nav> landmark.
  const isPageChrome = (el) =>
    isTabStrip(el) || !!el.closest('header, footer, [role="navigation"], .nav');

  const cardTitle = (a) => {
    let el = a;
    for (let i = 0; i < 3 && el.parentElement; i++) {
      el = el.parentElement;
      const h = el.querySelector('h2,h3,h4,h5,strong,b,[class*="title" i],[class*="headline" i]');
      if (h) {
        const t = (h.textContent || '').replace(/\s+/g, ' ').trim();
        if (t.length > 2 && t.length < 150 && !GENERIC_LINK.test(t)) return t;
      }
    }
    // no heading in the card: climb to the ancestor that holds more text
    // than the link itself, then use its first meaningful line
    const linkText = (a.innerText || '').trim();
    let card = a.parentElement;
    for (let i = 0; i < 4 && card; i++) {
      if ((card.innerText || '').trim().length > linkText.length + 8) break;
      card = card.parentElement;
    }
    if (card) {
      for (const line of (card.innerText || '').split('\n')) {
        const t = line.replace(/\s+/g, ' ').trim();
        if (t.length > 2 && t.length < 120 && !GENERIC_LINK.test(t)) return t;
      }
    }
    return '';
  };

  const push = (cat, title, url, meta) => {
    title = (title || '').replace(/\s+/g, ' ').trim().slice(0, 200);
    if (!title || title.length < 3) return;
    if (NAV_LABEL.test(title) && !meta) return;
    if (url) {
      const owner = urlOwned.get(url);
      if (owner && owner !== cat) return;         // already listed elsewhere
      const existing = out[cat].find(i => i.url === url);
      if (existing) {                              // same link twice: keep the
        if (title.length > existing.title.length   // more descriptive title
            && !GENERIC_LINK.test(title)) existing.title = title;
        if (meta && !existing.meta) existing.meta =
          meta.replace(/\s+/g, ' ').trim().slice(0, 140);
        return;
      }
      urlOwned.set(url, cat);
    }
    const key = cat + '|' + title + '|' + (url || '');
    if (seen.has(key)) return;
    seen.add(key);
    out[cat].push({title: title, url: url || '',
                   meta: (meta || '').replace(/\s+/g, ' ').trim().slice(0, 140)});
  };

  const collectAnchor = (cat, a, strictChrome) => {
    const href = a.href || '';
    if (href.startsWith('javascript:') || href.startsWith('mailto:')) return;
    if (strictChrome ? isPageChrome(a) : isTabStrip(a)) return;
    let t = (a.innerText || a.textContent || a.getAttribute('title') ||
             a.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    if (GENERIC_LINK.test(t)) {
      const ct = cardTitle(a);                     // teaser card: use its title
      if (ct) t = ct;
    }
    let meta = '';
    const tr = a.closest('tr');
    if (tr) {
      const cells = Array.from(tr.querySelectorAll('td'))
        .map(td => (td.innerText || '').replace(/\s+/g, ' ').trim())
        .filter(Boolean);
      // Anchor text in document tables concatenates badge spans without
      // separators ("...GuideNewPDFPublic..."); the first cell is the clean
      // human-readable title, so prefer it.
      if (cells.length && cells[0].length > 8 &&
          (t.startsWith(cells[0].slice(0, 12)) || t.length > cells[0].length + 8)) {
        t = cells[0].replace(/\s*(New|Updated)$/i, '');
      }
      meta = cells.filter(x => x !== t).slice(0, 4).join(' | ');
    }
    push(cat, t, href, meta);
  };

  // Pass 1: known components (reliable IDs/classes on infineon.com).
  // Roots are trusted; only inner tab strips are filtered out.
  for (const [cat, sel] of COMPONENTS) {
    let roots = [];
    try { roots = Array.from(document.querySelectorAll(sel)); } catch (e) {}
    for (const root of roots) {
      if (isTabStrip(root)) continue;
      if (root.textContent.length > bodyLen * 0.5) continue;
      root.querySelectorAll('a[href]').forEach(a => collectAnchor(cat, a, false));
    }
  }

  // Design-resources tab panes: category by pane suffix (training pane holds
  // trainings; boards/software/services/overview hold design resources).
  document.querySelectorAll('[id^="ds-tabpane-"]').forEach(pane => {
    const cat = /training/i.test(pane.id) ? 'trainings' : 'design_resources';
    pane.querySelectorAll('a[href]').forEach(a => collectAnchor(cat, a, false));
  });

  // Video cards (news & highlights carousels, training panes) have no links -
  // the "Watch video" button opens a modal. Extract title + duration/date.
  document.querySelectorAll('.fnVideoCard, [class*="videocard" i] .card').forEach(card => {
    const tEl = card.querySelector('.card-title, h3, h4');
    const title = tEl ? (tEl.textContent || '').replace(/\s+/g, ' ').trim() : '';
    if (!title) return;
    let meta = [];
    for (const line of (card.innerText || '').split('\n')) {
      const s = line.trim();
      if (/^\d{1,2}:\d{2}$/.test(s) || /^video\b/i.test(s) ||
          /^(training|webinar)\b/i.test(s)) meta.push(s);
    }
    const pane = card.closest('[id^="ds-tabpane-"]');
    let cat = 'videos';
    if (pane) cat = /training/i.test(pane.id) ? 'trainings' : 'design_resources';
    else if (card.closest('[class*="event" i], [class*="news" i]')) cat = 'news';
    push(cat, title, '', meta.join(' | '));
  });

  // Pass 2: heading-based fallback, only for categories still empty
  const containerFor = (h) => {
    let el = h, best = null;
    for (let i = 0; i < 5 && el.parentElement; i++) {
      el = el.parentElement;
      if (el.textContent.length > bodyLen * 0.34) break;   // grew past one section
      const links = el.querySelectorAll('a[href]').length;
      if (links >= 2 || (links >= 1 && i >= 2)) best = el;
    }
    return best || h.parentElement;
  };
  for (const h of document.querySelectorAll('h1,h2,h3,h4,[role="heading"]')) {
    if (isTabStrip(h)) continue;                   // tab labels are not sections
    const ht = (h.textContent || '').replace(/\s+/g, ' ').trim();
    if (!ht || ht.length > 60) continue;
    for (const [cat, re] of CATS) {
      if (out[cat].length || !re.test(ht)) continue;
      const c = containerFor(h);
      if (!c) break;
      c.querySelectorAll('a[href]').forEach(a => collectAnchor(cat, a, true));
      break;
    }
  }

  document.querySelectorAll('video').forEach(v => {
    const srcEl = v.querySelector('source');
    const src = v.currentSrc || v.src || (srcEl ? srcEl.src : '');
    let title = '';
    const fig = v.closest('figure, [class*="video" i], section, article');
    if (fig) {
      const cap = fig.querySelector('figcaption, h2, h3, h4, [class*="title" i]');
      if (cap) title = cap.textContent;
    }
    push('videos', title || (src ? decodeURIComponent(src.split('/').pop()) : 'video'),
         src, v.poster ? 'has poster' : '');
  });
  document.querySelectorAll(
      'iframe[src*="youtube" i], iframe[src*="video" i], iframe[src*="brightcove" i]')
    .forEach(f => push('videos', f.title || 'embedded video', f.src, 'iframe'));

  document.querySelectorAll('img').forEach(img => {
    const src = img.currentSrc || img.src;
    if (!src || src.startsWith('data:')) return;
    const key = 'img|' + src;
    if (seen.has(key)) return;
    seen.add(key);
    out.images.push({url: src,
      title: (img.alt || img.title || '').replace(/\s+/g, ' ').trim().slice(0, 150)});
  });
  return out;
}
"""

AUTO_SCROLL_JS = r"""
async () => {
  const delay = ms => new Promise(r => setTimeout(r, ms));
  let lastHeight = 0;
  for (let i = 0; i < 60; i++) {
    window.scrollBy(0, 800);
    await delay(180);
    const h = document.body.scrollHeight;
    if (window.innerHeight + window.scrollY >= h - 10 && h === lastHeight) break;
    lastHeight = h;
  }
  window.scrollTo(0, 0);
}
"""

IN_NAV_JS = "el => !!el.closest('header, nav, [role=\"navigation\"], footer')"

MORE_BUTTON_RE = re.compile(
    r"(load more|show more|view more|see more|show all|view all|load all|mehr anzeigen)", re.I)


def _base_url(u):
    """Scheme+host+path only. Tab clicks change #hash/?query on the same page;
    those must NOT count as having navigated away."""
    p = urlsplit(u)
    return (p.scheme, p.netloc, p.path)


async def _ensure_on(page, start_url):
    """If a click hard-navigated to a different page, go straight back to the
    original URL. Never uses go_back() - going 'back' through same-document
    history entries created by tab clicks can strand the page on about:blank."""
    try:
        if page.url == "about:blank" or _base_url(page.url) != _base_url(start_url):
            await page.goto(start_url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(2000)
    except Exception:
        pass


async def _expand_interactive(page):
    """Click tabs, expand accordions, press 'load more' buttons.

    Skips anything inside header/nav/footer so we don't pop open menus."""
    start_url = page.url

    # 1. Tabs — click each so tab panels that lazy-load via XHR get fetched.
    try:
        tabs = await page.query_selector_all('[role="tab"]')
        for tab in tabs[:40]:
            try:
                if await tab.evaluate(IN_NAV_JS):
                    continue
                await tab.click(timeout=1500)
                await page.wait_for_timeout(400)
            except Exception:
                pass
    except Exception:
        pass
    await _ensure_on(page, start_url)

    # 2. Collapsed accordions / expanders.
    try:
        btns = await page.query_selector_all(
            'button[aria-expanded="false"], [role="button"][aria-expanded="false"]')
        for btn in btns[:60]:
            try:
                if await btn.evaluate(IN_NAV_JS):
                    continue
                await btn.click(timeout=1200)
                await page.wait_for_timeout(200)
            except Exception:
                pass
    except Exception:
        pass
    await _ensure_on(page, start_url)

    # 2b. Design-resources tabs are <button>s (not role=tab): click each so
    # every pane hydrates (training items only load when their tab activates).
    try:
        for b in await page.query_selector_all('[id^="ds-tab-"]'):
            try:
                await b.click(timeout=1200)
                await page.wait_for_timeout(450)
            except Exception:
                pass
    except Exception:
        pass
    await _ensure_on(page, start_url)

    # 3. "Load more" style buttons — up to 3 rounds (paginated tables).
    for _ in range(3):
        clicked = False
        try:
            more = page.locator('button, [role="button"]').filter(has_text=MORE_BUTTON_RE)
            n = min(await more.count(), 8)
            for i in range(n):
                btn = more.nth(i)
                try:
                    if await btn.evaluate(IN_NAV_JS):
                        continue
                    if not await btn.is_visible():
                        continue
                    await btn.click(timeout=1500)
                    await page.wait_for_timeout(900)
                    clicked = True
                except Exception:
                    pass
        except Exception:
            pass
        if not clicked:
            break
    await _ensure_on(page, start_url)

    # Close any stray overlay, return to top for the screenshot.
    try:
        await page.keyboard.press("Escape")
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)
    except Exception:
        pass


async def capture_one(browser, url, out_dir):
    """Capture a single URL into out_dir. Returns a result dict."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"url": url, "status": "error", "error": "", "title": "",
              "text_lines": 0, "image_count": 0, "http_status": None}
    context = None
    try:
        context = await browser.new_context(
            viewport=VIEWPORT, user_agent=USER_AGENT, locale="en-US",
            ignore_https_errors=True)
        context.set_default_timeout(15_000)
        page = await context.new_page()

        # If any click opens a popup/new tab, close it immediately.
        async def _close_popup(p):
            try:
                await p.close()
            except Exception:
                pass
        context.on("page", lambda p: asyncio.ensure_future(_close_popup(p)))

        last_exc = None
        resp = None
        for attempt, wait_until in ((1, "domcontentloaded"), (2, "load")):
            try:
                resp = await page.goto(url, wait_until=wait_until,
                                       timeout=NAV_TIMEOUT_MS)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                await page.wait_for_timeout(2000)
        if last_exc is not None:
            raise last_exc
        result["http_status"] = resp.status if resp else None

        await page.add_style_tag(content=HIDE_CSS)
        try:                                       # all images/scripts finished
            await page.wait_for_load_state("load", timeout=20_000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)          # let JS widgets settle
        await page.evaluate(AUTO_SCROLL_JS)        # trigger lazy loading
        await _expand_interactive(page)            # tabs / accordions / load-more
        await page.wait_for_timeout(1000)
        # Deterministic settle: late-initializing widgets (document lists,
        # video players, search components) keep adding content for seconds
        # after 'load'. Wait for the network to go quiet, then poll BOTH the
        # total text length and the image inventory until neither changes
        # between polls - only then is the page considered fully loaded.
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        prev_sig = None
        for _ in range(8):
            text_len = await page.evaluate(
                "() => document.body ? document.body.textContent.length : 0")
            cur_imgs = await page.evaluate(IMAGES_JS)
            sig = (text_len, tuple(cur_imgs))
            if sig == prev_sig:
                break
            prev_sig = sig
            await page.wait_for_timeout(1200)

        # Document tables paginate client-side (page "1 2 ->"): walk every
        # page and merge the rows so no document is missed.
        docs_acc = []
        try:
            for _ in range(8):
                part = await page.evaluate(SECTIONS_JS)
                docs_acc.extend(part.get("documents", []))
                nxt = await page.query_selector(
                    "#documents .pagination .right-page:not(.inactive), "
                    ".documents_ifx .pagination .right-page:not(.inactive)")
                if nxt is None:
                    break
                await nxt.click(timeout=1500)
                await page.wait_for_timeout(900)
        except Exception:
            pass

        if INJECT_JS:                              # self-test tampering hook
            try:
                tampered = await page.evaluate(INJECT_JS)
                print(f"  [inject] {url[:60]}... -> {tampered}")
            except Exception as exc:
                print(f"  [inject] FAILED on {url[:60]}: {exc}")
        try:
            await page.add_style_tag(content=HIDE_CSS)  # re-apply in case of DOM swap
        except Exception:
            pass

        await page.screenshot(path=str(out_dir / "shot.png"), full_page=True,
                              timeout=60_000)

        # Remove consent overlays from the DOM so their text never appears.
        try:
            await page.evaluate(
                f"document.querySelectorAll('{BANNER_SELECTORS}').forEach(e => e.remove())")
        except Exception:
            pass

        text = await page.evaluate(TEXT_JS)
        images = await page.evaluate(IMAGES_JS)
        title = await page.title()
        try:
            sections = await page.evaluate(SECTIONS_JS)
        except Exception:
            sections = {}
        # merge documents collected across the table's pages
        try:
            docs = sections.get("documents", [])
            seen_docs = {(d.get("title"), d.get("url")) for d in docs}
            for d in docs_acc:
                key = (d.get("title"), d.get("url"))
                if key not in seen_docs:
                    docs.append(d)
                    seen_docs.add(key)
            sections["documents"] = docs
        except Exception:
            pass

        (out_dir / "text.txt").write_text(text, encoding="utf-8")
        (out_dir / "images.json").write_text(
            json.dumps(images, indent=1), encoding="utf-8")
        (out_dir / "sections.json").write_text(
            json.dumps(sections, indent=1, ensure_ascii=False), encoding="utf-8")
        (out_dir / "meta.json").write_text(json.dumps({
            "url": url,
            "final_url": page.url,
            "title": title,
            "http_status": result["http_status"],
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }, indent=1), encoding="utf-8")

        result.update(status="ok", title=title,
                      text_lines=text.count("\n") + 1 if text else 0,
                      image_count=len(images))
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
        (out_dir / "error.txt").write_text(
            traceback.format_exc(), encoding="utf-8")
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
    return result


async def capture_all(jobs, concurrency=4, progress=None):
    """Capture many URLs. jobs = [(url, out_dir), ...]. Returns list of result dicts.

    The browser is restarted every PAGES_PER_BROWSER pages to keep memory flat
    on large (3-4k URL) runs.
    """
    results = []
    async with async_playwright() as pw:
        for batch_start in range(0, len(jobs), PAGES_PER_BROWSER):
            batch = jobs[batch_start:batch_start + PAGES_PER_BROWSER]
            browser = await pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage"])
            sem = asyncio.Semaphore(concurrency)

            async def run_job(url, out_dir):
                async with sem:
                    res = await capture_one(browser, url, out_dir)
                    if progress:
                        progress(res)
                    return res

            batch_results = await asyncio.gather(
                *(run_job(u, d) for u, d in batch))
            results.extend(batch_results)
            try:
                await browser.close()
            except Exception:
                pass
    return results
