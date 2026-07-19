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
import time
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

# Marks a capture that failed because the page itself served an empty body.
# The runner keys the "serves a blank page" state off this, so it lives here
# as one constant rather than as a magic string duplicated across modules.
BLANK_RENDER = "blank render"
# HTTP answers that describe the CONNECTION, not the page: bot-guard blocks and
# server hiccups. These must never be recorded as a page's state - the capture
# retries with backoff and otherwise fails as an error (retried next run).
BLOCKED_TRANSIENT = "blocked/transient"
TRANSIENT_HTTP = {403, 408, 425, 429, 500, 502, 503, 504}


# Many Infineon cards are React components whose anchors carry NO href at all
# (<a class="anchor-title" rel="">) - the click is wired in JavaScript, so the
# download URL exists nowhere in the DOM. The page fetches its own content from
# a /dataApi/ JSON feed which does carry it, so the feed is harvested during the
# capture and used to fill in the links the markup omits.
# Field order matters: assetDmPath is the delivery URL that actually serves the
# file (verified 200 application/pdf); promoPageLinkUrl is only a landing page,
# so it is the last resort.
_API_TITLE_KEYS = ("documentDisplayName", "imageCaption", "title", "displayName",
                   "headline", "name", "videoTitle", "cardTitle")
_API_URL_KEYS = ("assetDmPath", "videoUrl", "videoURL", "url", "link", "href",
                 "ctaLink", "promoPageLinkUrl")


def _norm_title(t):
    return re.sub(r"\s+", " ", (t or "")).strip().lower()


# A tile links to the same destination several times - its image, its title,
# its product table, and a "View product family" CTA - so a raw anchor sweep
# yields one real title plus a pile of CTA twins.
_CTA_TITLE_RE = re.compile(
    r"(?i)^(goto.?link|view ((all )?(products?|product famil(y|ies)|famil(y|ies)"
    r"|more|details?)|all)|explore( more)?|learn more|read more|discover( more)?"
    r"|get started|show (more|all)|see (more|all))$")


def _clean_link_list(items, page_base):
    """One entry per destination for tile/card lists (categories, products).

    Keeps the first real title seen for a URL, drops self-links and CTA-only
    titles, and prefers a family page over its product table.
    """
    cleaned, seen_url = [], set()
    for it in items:
        u_it = (it.get("url") or "").split("#")[0].rstrip("/")
        t_it = re.sub(r"\s+Overview$", "", (it.get("title") or "").strip())
        if not u_it or len(t_it) < 3 or u_it == page_base:
            continue
        if _CTA_TITLE_RE.match(t_it) or u_it in seen_url:
            continue
        seen_url.add(u_it)
        it["url"], it["title"] = u_it, t_it
        cleaned.append(it)
    best, order = {}, []
    for it in cleaned:
        k = it["title"].lower()
        if k not in best:
            best[k] = it
            order.append(k)
        elif "/product-table/" in best[k]["url"] \
                and "/product-table/" not in it["url"]:
            best[k] = it
    return [best[k] for k in order]


def _feed_lang(o):
    """The language a feed record is in, as an upper-case code, or ''."""
    v = o.get("languages") or o.get("language")
    if isinstance(v, list):
        v = v[0] if v else ""
    return re.sub(r"[^A-Za-z]", "", str(v or "")).upper()[:2]


def _api_url_map(bodies):
    """Map a title (and language) to its URL from the page's own dataApi feeds.

    Returns {normalized title: {lang: url, '': first_url}}. The language matters:
    a documents table lists EN and JA variants under one title, and matching on
    title alone gave BOTH the first variant's URL - so the English row pointed
    at the Japanese file. The '' key keeps the old title-only behaviour as a
    fallback for records with no stated language.
    """
    out = {}

    def visit(o):
        if isinstance(o, dict):
            title = ""
            for k in _API_TITLE_KEYS:
                v = o.get(k)
                if isinstance(v, str) and v.strip():
                    title = v
                    break
            if title:
                for k in _API_URL_KEYS:
                    v = o.get(k)
                    if not isinstance(v, str) or not v.strip():
                        continue
                    u = v.strip()
                    if u.startswith("/"):
                        u = "https://www.infineon.com" + u
                    if u.startswith("http"):
                        by_lang = out.setdefault(_norm_title(title), {})
                        by_lang.setdefault("", _delivery_url(u))
                        lang = _feed_lang(o)
                        if lang:
                            by_lang.setdefault(lang, _delivery_url(u))
                        break
            for v in o.values():
                visit(v)
        elif isinstance(o, list):
            for v in o:
                visit(v)

    for b in bodies:
        try:
            visit(json.loads(b))
        except Exception:
            continue
    return out


# A document's language sits in its meta ("... | JA | 04_00 | 1.03MB | ...").
_META_LANG_RE = re.compile(r"(?:^|\|)\s*([A-Z]{2}(?:,[A-Z]{2})*)\s*(?:\||$)")


def _item_lang(meta):
    """The language code from a library item's meta, or '' (first if several)."""
    m = _META_LANG_RE.search(meta or "")
    return m.group(1).split(",")[0] if m else ""

# Document titles concatenate badge spans (type/access/version/size/date),
# sometimes joined by zero-width characters: cut everything from the badge
# sequence onward. Applied in Python so it is deterministic and testable.
_ZERO_WIDTH_RE = re.compile("[​‌‍⁠﻿]")
_DOC_BADGE_RE = re.compile(
    r"\s*(New|Updated)?\s*(PDF|ZIP|XLSX?|TXT|PPTX?|DOCX?)\s*"
    r"(Public|Confidential|Restricted).*$", re.I)


# A video's authoring path (www.infineon.com/content/dam/infineon/...) is NOT
# downloadable - it 404s. The delivery URL for the same asset lives on the
# Scene7 host. Verified against training/webinar videos on several pages.
_DAM_VIDEO_RE = re.compile(
    r"^https?://(?:www\.)?infineon\.com/content/dam/infineon/(.+)$", re.I)


def _delivery_url(u):
    m = _DAM_VIDEO_RE.match(u or "")
    if not m:
        return u
    return "https://assets.infineon.com/is/content/infineon/infineon/" + m.group(1)


def _clean_doc_title(title):
    t = _ZERO_WIDTH_RE.sub("", title or "")
    cut = _DOC_BADGE_RE.sub("", t).strip()
    if len(cut) >= 4:      # short doc codes like "AN82156" are valid titles
        return re.sub(r"\s*(New|Updated)$", "", cut, flags=re.I).strip()
    return t.strip()


# Cookie/consent overlays are hidden so they never pollute screenshots.
HIDE_CSS = """
#onetrust-consent-sdk, #usercentrics-root, #usercentrics-cmp-ui,
#CybotCookiebotDialog, #truste-consent-track, .cc-window,
[id*="cookie-banner" i], [class*="cookie-banner" i],
[id*="cookieconsent" i], [class*="cookieconsent" i],
/* Qualtrics site-intercept "Feedback" tab + its (empty) iframe box:
   page furniture that appeared in every screenshot */
div[id^="QSI"], div[class^="QSI"], [id*="QSIFeedbackButton" i],
[class*="QSIFeedbackButton" i], iframe[src*="qualtrics" i],
iframe[src*="siteintercept" i], [id*="siteintercept" i],
[class*="feedback-button" i], [id*="feedback-btn" i] {
    display: none !important; visibility: hidden !important;
}
/* keep vertical scrolling possible (banners lock it) but never let a wide
   element (e.g. the 2500px products table) stretch the document: that made
   full-page screenshots much wider than the page, padded with white */
html, body { overflow-x: hidden !important; overflow-y: auto !important; }
*, *::before, *::after {
    animation: none !important;
    transition: none !important;
    caret-color: transparent !important;
}
"""

BANNER_SELECTORS = ("#onetrust-consent-sdk, #usercentrics-root, #usercentrics-cmp-ui, "
                    "#CybotCookiebotDialog, #truste-consent-track, .cc-window, "
                    "div[id^=\"QSI\"], div[class^=\"QSI\"], "
                    "iframe[src*=\"qualtrics\"], iframe[src*=\"siteintercept\"]")

# Extracts ALL text nodes (textContent walk), so text inside hidden tab
# panels and collapsed sections is captured too.
# A rotating carousel advances on a JS timer and physically moves its first
# slide to the end of the DOM. The CSS above stops ANIMATION, not that - so the
# slide order we observed was a function of how long the capture happened to
# take, and the homepage reported a text change every run with the same four
# AURIX part numbers cycling by one position. Slides are emitted in a fixed
# order instead: the same set of slides reads identically run to run, while a
# slide added, removed or edited still shows as a real change.
CAROUSEL_TRACK_SEL = ('.swiper-wrapper, .slick-track, .glide__slides,'
                      ' .splide__list, [class*="carousel__track" i]')

TEXT_JS = r"""
() => {
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','IFRAME',
                        'SVG','CANVAS','VIDEO','AUDIO','OBJECT']);
  const TRACK = '""" + CAROUSEL_TRACK_SEL + r"""';
  // The header language switcher (English / 中文 / 日本語) is the same on every
  // page and is not page content. Worse, the account-menu copy renders its
  // active item twice on some captures and once on others, so an identical
  // re-run reported a change: one stray "English" line added. There are three
  // of these widgets per page; skip them all. Nothing page-specific lives
  // here, so no real signal is lost - only the language chrome leaves.
  const LANG = '.language-dropdown, .lang-list';
  // The language switcher also has a toggle that shows the CURRENT language as
  // a bare word ("English"), and it hydrates on some captures and not others -
  // it flapped one stray "English" into an otherwise identical re-run, on a
  // different toggle element from the option lists LANG already covers. A bare
  // language name inside the site header is always that switcher, never page
  // content, so drop it. Restricted to <header>: a document's language cell
  // ("English") lives in the body and must be kept.
  const LANGNAMES = new Set(['English', '中文', '日本語']);
  const walk = (node, sink) => {
    if (node.nodeType === Node.TEXT_NODE) {
      const t = node.textContent.replace(/\s+/g, ' ').trim();
      if (t) {
        if (LANGNAMES.has(t) && node.parentElement &&
            node.parentElement.closest('header')) return;
        sink.push(t);
      }
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (SKIP.has(node.tagName)) return;
    if (node.matches && node.matches(LANG)) return;
    // Order WITHIN a slide is real content (title, then blurb) and is kept.
    // Only the order OF the slides is normalised, and nothing is dropped -
    // every line still appears exactly once.
    if (node.children.length > 1 && node.matches && node.matches(TRACK)) {
      const blocks = [];
      for (const slide of node.children) {
        const sub = [];
        walk(slide, sub);
        if (sub.length) blocks.push(sub);
      }
      blocks.sort((a, b) => {
        const x = a.join('\n'), y = b.join('\n');
        return x < y ? -1 : (x > y ? 1 : 0);
      });
      for (const b of blocks) for (const l of b) sink.push(l);
      return;
    }
    for (const c of node.childNodes) walk(c, sink);
  };
  const out = [];
  if (document.body) walk(document.body, out);
  return out.join('\n');
}
"""

# Injected into every image collector (and matched identically by the QA
# probe) so the stored inventory and the live count always compare like for
# like. A Qualtrics tracking pixel loads whenever the intercept fires and a
# "missing-product-images" placeholder is the site's own "no image available"
# graphic - neither is page content, and both drifted the count run to run.
# community.infineon.com is added here so the IMAGE inventory drops developer-
# community avatars the same way SECTIONS_JS already does (capture.py ~1194).
# The community widget fetches its recent-thread authors asynchronously, so
# which avatars are in the DOM at capture time varies run to run - and that was
# reported as an "images changed" every time the author mix shifted (e.g. 5->0
# on url 1078). Avatars are not page content; the thread churn is tracked
# separately under developer_community, not as pictures of the page.
NON_CONTENT_IMG_JS = \
    r"/qualtrics|siteintercept|wrqualtrics|graphic\.php|missing-product-images|community\.infineon\.com/i"

IMAGES_JS = r"""
() => {
  const NON_CONTENT_IMG = """ + NON_CONTENT_IMG_JS + r""";
  // An image has to actually BE one. A lightbox trigger is written
  // <img src="#">, and the browser resolves that against the document - so
  // ".../part/CYW54591#" was collected as a picture of the part, on 126 pages.
  // Scene7 serves from /is/image/ without an extension, and AEM from
  // /jcr:content/renditions/, so both of those count as well.
  const LOOKS_LIKE_IMG = u =>
    /\/is\/image\//i.test(u) ||
    /\/jcr:content\/renditions\//i.test(u) ||
    /\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)(\?|#|:|$)/i.test(u);
  const ok = u => u && !u.startsWith('data:') && !NON_CONTENT_IMG.test(u)
                    && LOOKS_LIKE_IMG(u);
  const urls = new Set();
  document.querySelectorAll('img').forEach(img => {
    const src = img.currentSrc || img.src;
    if (ok(src)) urls.add(src);
  });
  // srcset: split ONLY on commas that start a new candidate URL. Scene7 URLs
  // carry commas inside their query ("?qlt=55,0&op_usm=1.75,0.3,2,0"), and a
  // naive split shredded one real URL into four dead ones.
  document.querySelectorAll('source[srcset], img[srcset]').forEach(s => {
    (s.getAttribute('srcset') || '')
      .split(/,(?=\s*(?:https?:)?\/\/|\s*\/[^\/]|\s*[\w.-]+\.[a-z]{2,6}\/)/i)
      .forEach(part => {
        const u = part.trim().split(/\s+/)[0];
        if (u && !u.startsWith('data:') && !NON_CONTENT_IMG.test(u)) {
          try { urls.add(new URL(u, location.href).href); } catch (e) {}
        }
      });
  });
  document.querySelectorAll('video[poster]').forEach(v => {
    const p = v.getAttribute('poster');
    if (p && !NON_CONTENT_IMG.test(p)) {
      try { urls.add(new URL(p, location.href).href); } catch (e) {}
    }
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
    ['news',             /^(news( & highlights?)?|highlights?|videos?)$/i],
    ['trainings',        /^((support & )?trainings?( & webinars?)?|webinars?)$/i],
  ];
  // ONLY true call-to-action text: never category names ("Boards & designs",
  // "Documents"...) - on hub pages those ARE the teaser card titles, and
  // filtering them here made the card's description get stored instead.
  const GENERIC_LINK = /^(explore( more| now)?|learn more|read more.*|read on|get started|discover( more)?|show (more|all)|see (more|all)|view ((all )?(more|products?|product famil(y|ies)|famil(y|ies)|details?)|all)|more|download|jump to|goto.?link|add to cart.*|buy online.*|order now.*)$/i;
  const NAV_LABEL = /^(product details|documents|design resources|support|about|overview|quicklinks?)$/i;

  // NB: every category pushed to below MUST exist here. A push() to a key
  // that was never initialised throws, and the whole library comes back empty.
  // The page's anchor-nav strip. Each tab's href targets that section's own
  // wrapper, which makes it the most reliable way to find a section: heading
  // text lies (the only "Overview" heading on a product page is this nav tab,
  // and the heading above the family carousel can read "Block diagram").
  // NB "sticky-nav" as well as "stickynav": product pages use
  // ul.products-sticky-nav, which [class*="stickynav"] does NOT match, so the
  // strip went unseen there and every nav-based lookup silently fell back.
  const NAV_ANCHORS =
    '#stickyNavTab a[href^="#"], .maintabs a[href^="#"], ' +
    '[class*="tabbar" i] a[href^="#"], [class*="anchor-nav" i] a[href^="#"], ' +
    '[class*="stickynav" i] a[href^="#"], [class*="sticky-nav" i] a[href^="#"]';

  const out = {documents: [], design_resources: [], news: [],
               trainings: [], videos: [], features: [], benefits: [],
               overview: [], subheadline: [], applications: [], about: [],
               categories: [], products: [], partners: [], breadcrumb: [],
               images: []};
  const seen = new Set();
  const urlOwned = new Map();   // url -> category (cross-category dedupe)
  const bodyLen = document.body ? document.body.textContent.length : 0;

  // Which categories does this page render with a real AEM component? Where
  // one exists it is the ONLY groundtruth for that category and the
  // heading-text fallback below must not run - a component showing "there
  // aren't any results" means zero items, not "go look elsewhere".
  const hasComponent = {};
  for (const [cat, sel] of COMPONENTS) {
    if (document.querySelector(sel)) hasComponent[cat] = true;
  }

  // Tab strips / buttons are NEVER content, even inside a trusted component.
  const isTabStrip = (el) =>
    !!el.closest('button, [role="tablist"], [role="tab"], .nav-tabs, .maintabs, ' +
      '#stickyNavTab, [class*="breadcrumb" i], [class*="tabbar" i], [class*="tab-nav" i]');
  // Page chrome only disqualifies content in the heading-fallback pass -
  // some Infineon sections (e.g. the design-resources teaser strip) legally
  // live inside a <nav> landmark.
  const isPageChrome = (el) =>
    isTabStrip(el) || !!el.closest('header, footer, [role="navigation"], .nav');

  const CHIP_LABEL = /^(development tools?|embedded software|boards? *& *designs?|services|training|simulation (tools|models)|pcb design data|documents|partners)$/i;
  // First meaningful text line of a card root: its visual title.
  // strict=true only inside design-resource panes, where every card carries a
  // category chip ("Development Tools") above its real title. On hub pages
  // those same words ARE the teaser's title ("Boards & designs") - filtering
  // them there makes the card's description get used instead.
  const cardLine = (root, strict) => {
    if (!root) return '';
    for (const line of (root.innerText || '').split('\n')) {
      const t = line.replace(/\s+/g, ' ').trim();
      if (t.length > 2 && t.length < 130 && !GENERIC_LINK.test(t) &&
          !(strict && CHIP_LABEL.test(t))) return t;
    }
    return '';
  };
  // a container holding many links is a list/component, not one card
  const tooBig = (el) => !el || el.querySelectorAll('a').length > 8;
  const cardTitle = (a, strict) => {
    let el = a;
    for (let i = 0; i < 5 && el.parentElement; i++) {
      el = el.parentElement;
      if (tooBig(el)) break;                       // climbed out of the card
      const h = el.querySelector('h2,h3,h4,h5,strong,b,[class*="title" i],[class*="headline" i]');
      if (h) {
        const t = (h.textContent || '').replace(/\s+/g, ' ').trim();
        if (t.length > 2 && t.length < 150 && !GENERIC_LINK.test(t) &&
            !(strict && CHIP_LABEL.test(t))) return t;
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
        if (t.length > 2 && t.length < 120 && !GENERIC_LINK.test(t) &&
            !(strict && CHIP_LABEL.test(t))) return t;
      }
    }
    return '';
  };

  // The documents table publishes its own columns:
  //   Document | Type | File | Access rights | Language | Version | Size | Date
  // Read the document's type from the Type COLUMN, by header. Taking the first
  // field of the joined meta instead is positional, and positional is what let
  // a "New" badge push a title into the type slot - and rows do carry empty
  // cells mid-row, which shifts every index after them.
  const columnValue = (tr, header) => {
    const table = tr.closest('table');
    if (!table) return '';
    const heads = Array.from(table.querySelectorAll(
      'thead th, thead td, tr:first-child th'))
      .map(h => (h.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase());
    const i = heads.indexOf(header);
    if (i < 0) return '';
    const tds = tr.querySelectorAll('td');       // NOT filtered: index matters
    return i < tds.length
      ? (tds[i].innerText || '').replace(/\s+/g, ' ').trim() : '';
  };

  const normT = (s) => (s || '').toLowerCase().replace(/\s+/g, '');
  // An <img>'s REAL address. These cards ship with src set to a 1x1
  // transparent data: URI and swap the true URL in when their tab is shown, so
  // reading src alone recorded "iVBORw0KGgo..." as the tool's picture whenever
  // the swap had not happened yet - a placeholder stored as groundtruth, and
  // one that would compare unequal against the real icon on the next run. A
  // data: URI is never the answer; fall through to the lazy-load attributes.
  const realImgSrc = (im) => {
    if (!im) return '';
    const cands = [im.currentSrc, im.getAttribute('src'),
                   im.getAttribute('data-src'), im.getAttribute('data-original'),
                   im.getAttribute('data-lazy-src')];
    for (const ss of [im.getAttribute('srcset'), im.getAttribute('data-srcset')]) {
      if (ss) cands.push(ss.split(',')[0].trim().split(/\s+/)[0]);
    }
    for (const c of cands) {
      if (c && !/^data:/i.test(c.trim())) return c.trim();
    }
    return '';
  };

  const push = (cat, title, url, meta, group, dtype, exact, img) => {
    // 200 chars is a sane cap for a card title, but overview/about entries ARE
    // prose - their text is the item - and capping those chopped a paragraph
    // mid-sentence, hiding the tail from the library.
    const cap = (cat === 'overview' || cat === 'about') ? 4000 : 200;
    title = (title || '').replace(/\s+/g, ' ').trim().slice(0, cap);
    if (!title || title.length < 3) return;
    // NB: no nav-label filter here - "Documents"/"Design resources" are real
    // teaser-card titles on hub pages. Nav strips are excluded structurally
    // (isTabStrip / isPageChrome), which is where nav links actually live.
    if (/^note that there are additional results/i.test(title)) return;
    if (url) {
      const owner = urlOwned.get(url);
      if (owner && owner !== cat) return;         // already listed elsewhere
      // merge ONLY true twins (same link AND same title modulo whitespace):
      // distinct products can legitimately share one vendor URL
      const existing = out[cat].find(
        i => i.url === url && normT(i.title) === normT(title));
      if (existing) {
        if (title.length > existing.title.length
            && !GENERIC_LINK.test(title)) existing.title = title;
        if (meta && !existing.meta) existing.meta =
          meta.replace(/\s+/g, ' ').trim().slice(0, 260);
        if (dtype && !existing.dtype) existing.dtype = dtype;
        if (img && !existing.img) existing.img = img;
        return;
      }
      urlOwned.set(url, cat);
    }
    // A link tells two rows apart; without one, only the meta can. The
    // documents table lists login-gated rows with NO href, so an EN and a JA
    // variant of one title were identical under (title, url) and the JA one
    // vanished - a real document, lost silently. Their meta differs (the
    // language cell), so it joins the key when there is no link to use.
    const key = cat + '|' + title + '|' + (url || '') +
                (url ? '' : '|' + (meta || ''));
    if (seen.has(key)) return;
    seen.add(key);
    const item = {title: title, url: url || '',
                  meta: (meta || '').replace(/\s+/g, ' ').trim().slice(0, 260)};
    if (group) item.group = group;
    // the document's own Type column - what decides DAM vs Windchill
    if (dtype) item.dtype = dtype.slice(0, 80);
    // this title came from the card's own title element: it is the page's
    // text verbatim and must not be tidied up afterwards
    if (exact) item.exact = true;
    // the card's own thumbnail. Every design-resource card on the page shows
    // one, and none of them were being recorded - the icon only survived in
    // the page-wide image list, where identical icons collapse to a single
    // entry under whichever card happened to be read first. So a tool whose
    // icon changed, or which shared an icon with a neighbour, had no image of
    // its own to compare against.
    if (img) item.img = img.slice(0, 400);
    out[cat].push(item);
  };

  // A component that rendered an empty state is NOT a page without content:
  // it is a page with zero items, and that is the groundtruth.
  const EMPTY_STATE =
    /(there aren'?t any results|no results? (were )?found|no documents? (are )?available|sorry.{0,30}no results)/i;

  // Climb from a heading to the container holding that section's links.
  // Bounded by two rules, because an unbounded climb swallows the next
  // section whole (an empty "Documents" heading once ate 14 product cards):
  //   - stop once the candidate contains a heading of the same or higher
  //     level: that heading belongs to a sibling section, so we have left ours
  //   - stop once the candidate holds a third of the page's text
  const containerFor = (h) => {
    const lvl = /^H([1-6])$/.test(h.tagName) ? +h.tagName[1] : 3;
    const peers = 'h1,h2,h3,h4,h5,h6'.split(',').slice(0, lvl).join(',');
    let el = h, best = null;
    for (let i = 0; i < 5 && el.parentElement; i++) {
      el = el.parentElement;
      if (el.textContent.length > bodyLen * 0.34) break;   // grew past one section
      if (Array.from(el.querySelectorAll(peers)).some(o => o !== h)) break;
      const links = el.querySelectorAll('a[href]').length;
      if (links >= 2 || (links >= 1 && i >= 2)) best = el;
    }
    return best || h.parentElement;
  };

  const collectAnchor = (cat, a, strictChrome) => {
    const href = a.href || '';
    if (href.startsWith('javascript:') || href.startsWith('mailto:')) return;
    if (strictChrome ? isPageChrome(a) : isTabStrip(a)) return;
    let t = (a.innerText || a.textContent || a.getAttribute('title') ||
             a.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    let meta = '', dtype = '';
    const tr = a.closest('tr');
    if (tr) {
      dtype = columnValue(tr, 'type');       // the row's own Type column
      // A TABLE ROW IS NOT A CARD: its title must come from this row. Never
      // climb - a row's icon-only/"download" anchor would escape the table
      // and pick up page furniture (e.g. the filter bar's "Reset all").
      const cells = Array.from(tr.querySelectorAll('td'))
        .map(td => (td.innerText || '').replace(/\s+/g, ' ').trim())
        .filter(Boolean);
      // Anchor text in document tables concatenates badge spans without
      // separators ("...GuideNewPDFPublicEN01_003.64MBApr 29, 2020"): cut the
      // title at the type+access badge sequence, or prefer a clean first cell.
      const badgeCut = t.search(/(New)?(PDF|ZIP|XLSX?|TXT|PPTX?)(Public|Confidential|Restricted)/);
      if (badgeCut > 8) {
        t = t.slice(0, badgeCut).replace(/\s*(New|Updated)$/i, '').trim();
      } else if (GENERIC_LINK.test(t) || t.length < 3) {
        t = cells.length ? cells[0].replace(/\s*(New|Updated)$/i, '') : '';
      } else if (cells.length && cells[0].length > 8 &&
          (t.startsWith(cells[0].slice(0, 12)) || t.length > cells[0].length + 8)) {
        t = cells[0].replace(/\s*(New|Updated)$/i, '');
      }
      if (!t || t.length < 3) return;      // icon anchor in a title-less row
      // Drop the row's OWN title cell from its meta. Compare normalised: the
      // title has had its "New"/"Updated" badge stripped while the cell still
      // carries it, so a plain !== left the title sitting in front of the meta
      // - which made the document's TYPE read as its title, and the DAM /
      // Windchill lookup fail on every New-badged document.
      const normCell = (s) => s.replace(/\s*(New|Updated)$/i, '')
                               .replace(/\s+/g, ' ').trim().toLowerCase();
      const tNorm = normCell(t);
      meta = cells.filter(x => normCell(x) !== tNorm).slice(0, 8).join(' | ');
    } else if (GENERIC_LINK.test(t)) {
      // teaser card outside a pane: its heading IS the title (not strict)
      const ct = cardTitle(a, false);
      if (!ct) return;                             // no real title -> skip junk
      t = ct;
    }
    push(cat, t, href, meta, '', dtype);
  };

  // Design-resources tab panes FIRST (before the generic component pass, so
  // items keep their tab group): Boards & designs / PCB design data /
  // Simulation models / Development tools / Training. The Overview pane is
  // skipped: it is a union of the other tabs.
  const DATE_RE = /^[A-Z][a-z]{2,8} \d{1,2}, \d{4}$/;
  const LANG_RE = /^[A-Z]{2}(,[A-Z]{2})*$/;
  const cardExtras = (root) => {
    const bits = [];
    for (const line of (root.innerText || '').split('\n')) {
      const s = line.trim();
      if (DATE_RE.test(s) || LANG_RE.test(s) ||
          /^\d+(\.\d+)? ?mb$/i.test(s) || /^(zip|pdf|xlsx?|txt|exe)$/i.test(s) ||
          /^\d{2}_\d{2}$/.test(s) ||
          /^[A-Z][a-z]{2,8} \d{1,2}, \d{4}$/.test(s)) bits.push(s);
    }
    return [...new Set(bits)].slice(0, 5).join(' | ');
  };
  document.querySelectorAll('[id^="ds-tabpane-"]').forEach(pane => {
    const suffix = pane.id.replace('ds-tabpane-', '');
    if (/^overview$/i.test(suffix)) return;
    // ONLY the tab that is actually open. Paginating the Overview tab (the
    // union of every tab) re-renders the HIDDEN panes with union content, so
    // reading them filed other tabs' cards under whichever pane they landed
    // in - storing 17 "Embedded software" where the tab shows 11, and 39
    // where it shows 7. The caller opens each tab in turn; read what is open.
    if (pane.offsetParent === null) return;
    const tabBtn = document.getElementById('ds-tab-' + suffix);
    const group = tabBtn
      ? (tabBtn.textContent || '').replace(/\s+/g, ' ').trim()
      : suffix.replace(/-/g, ' ');
    const cat = /training/i.test(suffix) ? 'trainings' : 'design_resources';

    // ONE ITEM PER CARD - never per anchor. Each card carries several links
    // (thumbnail, title, "Read more", download, partner), and each card is
    // rendered twice (desktop + mobile). Iterating anchors produced the same
    // card up to 8 times, sometimes titled by its category chip.
    // Cards are NOT always <li>: the Services pane has none, and its only
    // outermost <li>s are partner badges + pagination - which got stored as
    // "Ubiquitous AI CorporationPreferred" instead of the real cards.
    let cards = Array.from(pane.querySelectorAll(
      '.boardlist, .wholepcbdata, .tab-tools, li.style-none'));
    if (!cards.length) cards = Array.from(pane.querySelectorAll('li'));
    cards = cards.filter(c =>
      // outermost card only, and never page furniture
      !cards.some(o => o !== c && o.contains(c)) &&
      !c.closest('.partner-ul-box, .pagination, .pagination-container, ' +
                 '#filter-container, .headerNote') &&
      !c.matches('li.box-style, li.partner-box, .pagination li'));
    cards.forEach(card => {
      // the chip above each card ("Wireless Modules", "Development Tools")
      // is a label, not a title - identify it structurally, not by name
      const chipEl = card.querySelector('.st-title-dsobject, .labelname');
      const chip = chipEl ? (chipEl.textContent || '').replace(/\s+/g, ' ').trim() : '';
      // never let the partner badge ("<vendor>Preferred") pose as the title.
      // NB .board-anchor: board cards carry no .anchor-title at all, and
      // without it the fallback grabbed the card's description paragraph and
      // stored THAT as a title ("PSOC 5LP I/O access in a breadboard-
      // compatible format...") - a phantom extra card on every board tab.
      const titleEl = card.querySelector(
        '.anchor-title, .board-anchor, .heading-dsobject a, .heading-dsobject, ' +
        'h3:not(.heading-partnerobject), h4:not(.heading-partnerobject)');
      let t = titleEl ? (titleEl.innerText || titleEl.textContent || '')
                          .replace(/\s+/g, ' ').trim() : '';
      // A title read from the card's own title element IS the page's text -
      // mark it, so the tidy-up regexes downstream (which exist only for the
      // reconstruct-from-innerText fallback below) leave it alone. Stripping
      // "DOWNLOAD - " off "DOWNLOAD - DAVE(TM) IDE" left the library holding
      // something the page never said, and a rename between the two forms
      // would have been invisible to the watcher.
      let exact = !!t && !GENERIC_LINK.test(t);
      if (!t || GENERIC_LINK.test(t)) {
        exact = false;
        const partner = card.querySelector('.heading-partnerobject');
        const partnerTxt = partner ?
          (partner.textContent || '').replace(/\s+/g, ' ').trim() : '';
        for (const line of (card.innerText || '').split('\n')) {
          const s = line.replace(/\s+/g, ' ').trim();
          if (s && s !== chip && s !== partnerTxt && s !== 'Preferred' &&
              s.length > 3 && s.length < 130 && !GENERIC_LINK.test(s) &&
              !/^(watch video|log ?in|clear all|sort by.*|preferred)$/i.test(s)) {
            t = s;
            break;
          }
        }
      }
      if (!t || t === chip || t.length < 3) return;
      let href = '';
      const a1 = card.querySelector(
        '.anchor-title[href], .board-anchor[href], .heading-dsobject a[href]');
      if (a1) {
        href = a1.href;
      } else {
        const a2 = Array.from(card.querySelectorAll('a[href]'))
          .find(a => !isTabStrip(a) && a.href &&
                     !a.href.startsWith('javascript:') &&
                     !a.href.startsWith('mailto:'));
        if (a2) href = a2.href;
      }
      if (!href) {          // training/video card: URL hides in the thumbnail
        const img = card.querySelector('img[src*=".mp4/"]');
        if (img) href = img.src.split('/jcr:content')[0];
      }
      // the card's icon. Skip the partner badge's logo, which belongs to the
      // vendor strip beside the card and not to the tool itself.
      let thumb = '';
      for (const im of card.querySelectorAll('img')) {
        if (im.closest('.partner-ul-box, .heading-partnerobject, .partner-box'))
          continue;
        thumb = realImgSrc(im);
        if (thumb) break;
      }
      push(cat, t, href, cardExtras(card), group, '', exact, thumb);
    });
  });

  // Pass 1: known components (reliable IDs/classes on infineon.com).
  // Roots are trusted; only inner tab strips are filtered out.
  const hasDsPanes = !!document.querySelector('[id^="ds-tabpane-"]');
  for (const [cat, sel] of COMPONENTS) {
    // when the tabbed design-resources widget exists, its panes (collected
    // above, with groups) are authoritative - skip the generic pass which
    // would re-collect the same section plus unrelated neighbours ungrouped
    if (cat === 'design_resources' && hasDsPanes) continue;
    let roots = [];
    try { roots = Array.from(document.querySelectorAll(sel)); } catch (e) {}
    for (const root of roots) {
      if (isTabStrip(root)) continue;
      if (root.textContent.length > bodyLen * 0.5) continue;
      root.querySelectorAll('a[href]').forEach(a => collectAnchor(cat, a, false));
    }
  }

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
          /^(training|webinar)\b/i.test(s) || DATE_RE.test(s) ||
          (/\b(19|20)\d{2}\b/.test(s) && s.length < 30)) meta.push(s);
    }
    const pane = card.closest('[id^="ds-tabpane-"]');
    let cat = 'news';
    if (pane) {
      cat = /training/i.test(pane.id) ? 'trainings' : 'design_resources';
    } else {
      // categorize by the closest section heading ABOVE the card in document
      // order (an ancestor can contain several h2s - nearest-preceding wins)
      let secHead = '';
      for (const h of document.querySelectorAll('h2')) {
        if (h.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING) {
          secHead = h.textContent || '';
        }
      }
      if (/webinar|training/i.test(secHead)) cat = 'trainings';
    }
    // The video URL is not in any href - the card is a div with
    // onclick="playVideo(this)". That function reads the media URL out of the
    // ID attribute of the .videos element:
    //     var n = $(e).find(".video-container").find(".videos").attr("id");
    //     document.querySelector(".model-pop .video").src = n;
    // so an id holding a URL is exactly where the site itself looks. (Training
    // cards instead carry a DAM thumbnail path: .../<name>.mp4/jcr:content/...)
    let vurl = '';
    const vEl = card.querySelector('.video-container .videos[id], .videos[id]');
    if (vEl && /^https?:\/\//i.test(vEl.id || '')) vurl = vEl.id;
    if (!vurl) {
      const thumb = card.querySelector('img[src*=".mp4/"]');
      if (thumb) vurl = thumb.src.split('/jcr:content')[0];
    }
    // playVideo() bails out to a login popup when the card holds an
    // .exclusive-video, so these have no public URL at all. Say so, rather
    // than leaving a blank that reads like something we failed to find.
    const metaParts = [...new Set(meta)].slice(0, 3);
    if (!vurl && card.querySelector('.exclusive-video'))
      metaParts.push('login required (MyInfineon)');
    push(cat, title, vurl, metaParts.join(' | '));
  });

  // The related-families carousel. ONE module that wears different names: it
  // is "Related products" on one page and "Related applications" on the next,
  // with a "View product family" / "View application family" button. Matching
  // it by name missed whole modules (a 7-card one stored as zero), and the
  // nearest heading above it lies too - on one page that is "Block diagram".
  // So: find it by its component class, and name it from the anchor-nav tab
  // that actually points at its section.
  // The module's real name is its anchor-nav tab ("Related applications" on
  // one page, "Related products" on the next). The tab's href points at that
  // section's own wrapper (#related = div.relAppcarousel-main), so containment
  // is the definitive answer - but measured against live pages the carousel
  // element sits just OUTSIDE that wrapper, so fall back to the first tab whose
  // section starts at or after the module. Picking the last tab BEFORE it is
  // what produced labels like "Block diagram".
  const navLabelFor = (el) => {
    let following = '';
    for (const a of document.querySelectorAll(NAV_ANCHORS)) {
      const id = (a.getAttribute('href') || '').slice(1);
      if (!id) continue;
      let marker = null;
      try { marker = document.getElementById(id); } catch (e) { continue; }
      if (!marker) continue;
      const txt = (a.innerText || '').replace(/\s+/g, ' ').trim();
      if (!txt) continue;
      if (marker.contains(el)) return txt;
      // PRECEDING here means el precedes the marker, i.e. the marker is after it
      if (!following &&
          (marker.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING))
        following = txt;
    }
    return following;
  };
  const famMods = Array.from(document.querySelectorAll(
    '.functionalrelatedcarousel, .relAppSwiper, ' +
    '[id*="related_application_carousel" i]'));
  for (const mod of famMods.filter(m => !famMods.some(o => o !== m && o.contains(m)))) {
    const label = navLabelFor(mod) || 'Related products';
    for (const card of mod.querySelectorAll('.card')) {
      const tEl = card.querySelector('.card-title');
      const title = tEl ? (tEl.textContent || '').replace(/\s+/g, ' ').trim() : '';
      if (!title) continue;
      // the family page, never its /product-table/ twin (a separate link in
      // the same card that _clean_link_list would otherwise have to guess at)
      let href = '';
      const main = card.querySelector('a.cardlink-hover[href], a.cardlink-ref[href]');
      if (main) href = main.href;
      if (!href) {
        const a = card.querySelector('a[href]');
        if (a) href = a.href;
      }
      // whatever the card carries inside - feature/benefit bullets - minus the
      // CTA, which is a list item of the same list
      const bullets = Array.from(card.querySelectorAll('ul li'))
        .filter(li => !li.classList.contains('label-style'))
        .map(li => (li.innerText || '').replace(/\s+/g, ' ').trim())
        .filter(t => t.length > 1);
      const cta = card.querySelector('.label-style');
      const bits = [];
      if (cta) bits.push((cta.innerText || '').replace(/\s+/g, ' ').trim());
      if (bullets.length) bits.push(bullets.slice(0, 8).join(' • '));
      // the card's product shot, same rule as the design-resource cards: a
      // data: URI is the lazy-load placeholder, never the picture
      let pthumb = '';
      for (const im of card.querySelectorAll('img')) {
        pthumb = realImgSrc(im);
        if (pthumb) break;
      }
      push('products', title, href, bits.join(' | '), label, '', false, pthumb);
    }
  }

  // The site's own canonical hierarchy for this page. Read from JSON-LD, not
  // from the DOM breadcrumb: that container also holds the Facebook/LinkedIn/
  // Email share row, and on product pages it repeats a level and drops the leaf.
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    if (out.breadcrumb.length) break;
    let j;
    try { j = JSON.parse(s.textContent); } catch (e) { continue; }
    for (const o of (Array.isArray(j) ? j : [j])) {
      if (!o || o['@type'] !== 'BreadcrumbList' ||
          !Array.isArray(o.itemListElement)) continue;
      for (const e of o.itemListElement) {
        const name = e.name || (e.item && e.item.name) || '';
        const u = typeof e.item === 'string' ? e.item
                                            : ((e.item && e.item['@id']) || '');
        if (name) out.breadcrumb.push({title: String(name).replace(/\s+/g, ' ').trim(),
                                       url: String(u || ''), meta: ''});
      }
      break;
    }
  }

  // Gated document promo pages (/gated/...): the whitepaper IS the page, and
  // it was being stored as nothing at all. There is no form and no link to
  // follow - the gate is a JS "Login to Download" button - but Infineon ships
  // the complete record to anonymous visitors in a hidden div[data-json] and
  // gates only the PDF bytes. So the document is captured with full metadata
  // and its real URL, without touching the gate.
  let docAbstract = '';          // fallback overview for gated promo pages
  document.querySelectorAll('[data-json]').forEach(d => {
    let rec;
    try { rec = JSON.parse(d.getAttribute('data-json') || ''); } catch (e) { return; }
    if (!rec || typeof rec !== 'object') return;
    if (!rec.title || !rec.extension || !rec.URL) return;   // not a doc record
    const bits = [];
    if (rec.documentType) bits.push(rec.documentType);
    bits.push(String(rec.extension).toUpperCase());
    // the asset itself 302s to the SSO login, so say so rather than leaving a
    // link that looks live but is not
    bits.push(rec.isGated ? 'login required'
                          : (rec.confidentialityLevel || 'Public'));
    if (Array.isArray(rec.language) && rec.language.length)
      bits.push(rec.language.join(',').toUpperCase());
    if (rec.docRevision) bits.push(rec.docRevision);
    if (rec.size) {
      const n = Number(rec.size);
      bits.push(n >= 1048576 ? (n / 1048576).toFixed(2) + ' mb'
                             : (n / 1024).toFixed(2) + ' kb');
    }
    if (rec.publicationDate) bits.push(rec.publicationDate);
    let u = String(rec.URL);
    if (u.startsWith('/')) u = 'https://www.infineon.com' + u;
    // the feed names the type outright - no parsing needed
    push('documents', rec.title, u, bits.join(' | '), '',
         rec.documentType || '');
    // On a gated promo page the document's abstract is the only prose there
    // is, so it stands in as the page's overview. It must NOT pre-empt a real
    // one: a product page carrying a document record put that document's short
    // description into overview first, and the DOM extractor below then skipped
    // the page's actual paragraph because overview was already non-empty -
    // storing 87 characters of a 253-character overview. Held back and used
    // only as a last resort, once nothing else has claimed overview.
    if (rec.description && rec.description !== rec.title && !docAbstract)
      docAbstract = rec.description;
  });

  // About / functional-tabbed widgets ("01 ... / 02 Features of ... / 03
  // ..."): each tab becomes ONE item in the dedicated About category -
  // these tabs are their own section, distinct from the Key Features block.
  document.querySelectorAll('.tab-comp').forEach(comp => {
    const headers = Array.from(comp.querySelectorAll('.tab-header'))
      .map(h => (h.textContent || '').replace(/\s+/g, ' ').trim());
    const panes = Array.from(comp.querySelectorAll('[role="tabpanel"], .tab-pane'));
    panes.forEach((pane, i) => {
      const title = headers[i] || '';
      if (!title) return;
      const lis = Array.from(pane.querySelectorAll('li'))
        .map(li => (li.innerText || '').replace(/\s+/g, ' ').trim())
        .filter(t => t.length > 3 && t.length < 250);
      let desc = lis.length
        ? lis.slice(0, 12).join(' • ')
        : (pane.innerText || '').replace(/\s+/g, ' ').trim();
      if (desc.length > 20) push('about', title.slice(0, 130), '', desc);
    });
  });

  // Overview, found through the anchor-nav tab that points at it. Searching
  // for an "Overview" heading finds only the NAV TAB on a product page - the
  // section's own heading is not an h1-h3 - so the paragraph went uncaptured
  // and a document's 87-character abstract stood in for a 253-character
  // overview. The tab's href targets the section's own wrapper (the same
  // markers the family module is named from), so ask it directly.
  for (const a of document.querySelectorAll(NAV_ANCHORS)) {
    if (out.overview.length) break;
    const t = (a.innerText || '').replace(/\s+/g, ' ').trim();
    if (!/^overview$/i.test(t)) continue;
    const id = (a.getAttribute('href') || '').slice(1);
    let sec = null;
    try { sec = id ? document.getElementById(id) : null; } catch (e) {}
    if (!sec) continue;
    const paras = Array.from(sec.querySelectorAll('p'))
      .map(p => (p.innerText || '').replace(/\s+/g, ' ').trim())
      .filter(x => x.length > 40);
    if (paras.length) push('overview', paras.join(' '), '', '');
    break;
  }

  // Fallback: pages whose overview section has a real heading and no nav tab.
  for (const h of document.querySelectorAll('h1,h2,h3')) {
    if (out.overview.length) break;
    if (isPageChrome(h) ||
        h.closest('.designresource-component, [id^="ds-"], #documents')) continue;
    const ht = (h.textContent || '').replace(/\s+/g, ' ').trim();
    if (!/^overview$/i.test(ht)) continue;
    let c = h.parentElement;
    for (let i = 0; i < 3 && c && c.textContent.replace(/\s+/g, ' ').trim().length < ht.length + 60; i++) {
      c = c.parentElement;
    }
    if (!c) break;
    let text = (c.innerText || '').replace(/\s+/g, ' ').trim();
    text = text.replace(/^Overview\s*/i, '');
    const cutAt = text.search(/Key features?|Benefits\b|Applications\b/i);
    if (cutAt > 40) text = text.slice(0, cutAt);
    text = text.trim();
    if (text.length > 40) {
      out.overview.push({title: text.slice(0, 700), url: '', meta: ''});
    }
    break;
  }

  // Browse by category: the product-category links on hub/family pages
  for (const h of document.querySelectorAll('h1,h2,h3')) {
    if (out.categories.length) break;
    if (isPageChrome(h)) continue;
    const ht = (h.textContent || '').replace(/\s+/g, ' ').trim();
    if (!/^browse by categor(y|ies)$/i.test(ht)) continue;
    const c = containerFor(h);
    if (!c) break;
    c.querySelectorAll('a[href]').forEach(a => collectAnchor('categories', a, true));
    break;
  }

  // Applications: linked application areas / use-case lists
  for (const h of document.querySelectorAll('h1,h2,h3,h4')) {
    // .tab-comp is the About widget: an "Applications" TAB inside it is About
    // content, not the page's Applications section (user rule). Tab labels
    // generally are never section headings.
    if (isPageChrome(h) || h.closest(
        '.designresource-component, [id^="ds-"], .tab-comp, [role="tablist"]')) continue;
    const ht = (h.textContent || '').replace(/\s+/g, ' ').trim();
    if (!/^applications?$/i.test(ht) || out.applications.length) continue;
    const c = containerFor(h);
    if (!c) break;
    c.querySelectorAll('a[href]').forEach(a => collectAnchor('applications', a, true));
    if (!out.applications.length) {
      Array.from(c.querySelectorAll('li'))
        .map(li => (li.innerText || '').replace(/\s+/g, ' ').trim())
        .filter(t => t.length > 3 && t.length < 200)
        .slice(0, 30).forEach(t => push('applications', t, '', ''));
    }
    break;
  }

  // Key features / Benefits: bullet or card lists without links
  const FEAT_CATS = [
    ['features', /^(key )?features?( (&|and) benefits?)?$/i],
    ['benefits', /^benefits?$/i],
  ];
  const featContainer = (h) => {
    let el = h;
    for (let i = 0; i < 5 && el.parentElement; i++) {
      el = el.parentElement;
      if (el.textContent.length > bodyLen * 0.34) break;
      if (el.querySelectorAll('li').length >= 2 ||
          el.querySelectorAll('h3, h4, [class*="title" i]').length >= 2) return el;
    }
    return null;
  };
  for (const h of document.querySelectorAll('h1,h2,h3,h4')) {
    if (isPageChrome(h)) continue;   // footer has a "Benefits" nav heading
    const ht = (h.textContent || '').replace(/\s+/g, ' ').trim();
    if (!ht || ht.length > 40) continue;
    for (const [cat, re] of FEAT_CATS) {
      if (out[cat].length || !re.test(ht)) continue;
      const c = featContainer(h);
      if (!c) break;
      let items = Array.from(c.querySelectorAll('li'))
        .map(li => (li.innerText || '').replace(/\s+/g, ' ').trim())
        .filter(t => t.length > 4 && t.length < 250);
      if (!items.length) {
        items = Array.from(c.querySelectorAll('h3, h4, [class*="title" i]'))
          .map(e => (e.textContent || '').replace(/\s+/g, ' ').trim())
          .filter(t => t.length > 4 && t.length < 250 && t !== ht);
      }
      items.slice(0, 40).forEach(t => push(cat, t, '', ''));
      break;
    }
  }

  // Login-gated document rows (MyInfineon lock icon) have no public link -
  // capture them by their table cells so the groundtruth stays complete.
  document.querySelectorAll('#documents tr, .documents_ifx tr').forEach(tr => {
    if (!tr.querySelector('td') || tr.querySelector('a[href]')) return;
    const cells = Array.from(tr.querySelectorAll('td'))
      .map(td => (td.innerText || '').replace(/\s+/g, ' ').trim())
      .filter(Boolean);
    const dtype = columnValue(tr, 'type');
    // A document code can be short - "TA1226" is 6 chars, like "AN82156" - so
    // a blunt length>=8 guard dropped three real gated documents. Keep the row
    // when it actually looks like a document (a Type cell, or a format/access
    // cell), which still rejects spacer/junk rows without those.
    const looksDoc = dtype || cells.some(c =>
      /^(new |updated )?(pdf|zip|xlsx?|txt|pptx?|docx?)$/i.test(c) ||
      /^(public|confidential|restricted|myinfineon)$/i.test(c));
    if (cells.length < 2 || cells[0].length < 3 || !looksDoc) return;
    push('documents', cells[0].replace(/\s*(New|Updated)$/i, ''), '',
         cells.slice(1, 9).join(' | '), '', dtype);
  });

  // Pass 2: heading-based fallback, only for categories still empty
  for (const h of document.querySelectorAll('h1,h2,h3,h4,[role="heading"]')) {
    if (isTabStrip(h)) continue;                   // tab labels are not sections
    const ht = (h.textContent || '').replace(/\s+/g, ' ').trim();
    if (!ht || ht.length > 60) continue;
    for (const [cat, re] of CATS) {
      if (out[cat].length || !re.test(ht)) continue;
      if (hasComponent[cat]) continue;      // the component already had its say
      if (cat === 'design_resources' && hasDsPanes) continue;
      const c = containerFor(h);
      if (!c || EMPTY_STATE.test(c.innerText || '')) break;
      c.querySelectorAll('a[href]').forEach(a => collectAnchor(cat, a, true));
      break;
    }
  }

  // Last resort only: a gated promo page has no prose but its document's
  // abstract. Every real overview on the page has had its chance by now.
  if (!out.overview.length && docAbstract)
    push('overview', docAbstract, '', '');

  // Subheadline: the lead tagline directly under the hero title (the <p> that
  // follows the page's <h1>, an AEM '.para-info'). Stored as its own text
  // section, shown like the overview.
  (() => {
    const h1 = document.querySelector('h1');
    let sub = '';
    if (h1) {
      let p = h1.nextElementSibling;
      while (p && !(p.tagName === 'P' && p.textContent.trim().length > 15))
        p = p.nextElementSibling;
      if (p) sub = p.textContent;
    }
    if (!sub) {
      const pi = document.querySelector('.para-info, [class*="subheadline" i]');
      if (pi) sub = pi.textContent;
    }
    // A PTP (part) page has no subheadline. Its title is followed straight by
    // the datasheet/package/board links - there is no tagline. The lead text
    // in the Product details "Description" pane is a DESCRIPTION, a different
    // field, and must not be promoted into this one.
    sub = (sub || '').replace(/\s+/g, ' ').trim();
    if (sub.length > 15) push('subheadline', sub.slice(0, 400), '', '');
  })();

  // Scene7 / Video.js declares each video on a [data-videourl] wrapper that
  // sits in the DOM even for TABS THE VISITOR NEVER OPENED - so scan those
  // FIRST, and a promo page whose six tab panels each hold a video yields all
  // six, not just the active tab's. The <video>'s own src is a runtime blob:
  // (useless); the wrapper's data-videourl/title/duration are the real thing.
  const vDone = new Set();
  document.querySelectorAll('[data-videourl]').forEach(vp => {
    const durl = (vp.getAttribute('data-videourl') || '').trim();
    if (!durl || vDone.has(durl)) return;
    vDone.add(durl);
    const title = (vp.getAttribute('data-title') || '').trim();
    const dsec = parseFloat(vp.getAttribute('data-duration') || '');
    // the poster IS the video's thumbnail, and it was already right here -
    // recorded as the words "has poster", which said a picture existed while
    // discarding the address of it. Store the address.
    const poster = (vp.getAttribute('data-poster') || '').trim();
    let meta = poster ? 'has poster' : '';
    if (dsec > 0) meta = Math.floor(dsec / 60) + ':' +
                         String(Math.round(dsec % 60)).padStart(2, '0');
    push('videos', title || decodeURIComponent(durl.split('/').pop()), durl,
         meta, '', '', false, /^data:/i.test(poster) ? '' : poster);
  });
  document.querySelectorAll('video').forEach(v => {
    const vp = v.closest('[data-videourl]');
    if (vp && vDone.has((vp.getAttribute('data-videourl') || '').trim())) return;
    const srcEl = v.querySelector('source');
    let src = v.currentSrc || v.src || (srcEl ? srcEl.src : '');
    let title = '', meta = v.poster ? 'has poster' : '';
    if (src.indexOf('blob:') === 0) src = '';   // a blob is never storable
    if (!title) {   // non-Scene7 players: fall back to a nearby caption
      const fig = v.closest('figure, [class*="video" i], section, article');
      const cap = fig && fig.querySelector('figcaption, h2, h3, h4, [class*="title" i]');
      if (cap) title = cap.textContent;
    }
    // only accept real media: a poster-bearing player, or a media-looking URL
    if (!v.poster && !/\.(mp4|webm|m3u8|mov)([?#]|$)|\/is\/content\/|video-cdn|brightcove|youtube/i.test(src)) return;
    const vposter = /^data:/i.test(v.poster || '') ? '' : (v.poster || '');
    push('videos', (title || '').replace(/\s+/g, ' ').trim() ||
         (src ? decodeURIComponent(src.split('/').pop()) : 'video'), src, meta,
         '', '', false, vposter);
  });
  document.querySelectorAll(
      'iframe[src*="youtube" i], iframe[src*="video" i], iframe[src*="brightcove" i]')
    .forEach(f => push('videos', f.title || 'embedded video', f.src, 'iframe'));

  const NON_CONTENT_IMG = """ + NON_CONTENT_IMG_JS + r""";
  const imgSeen = new Set();
  document.querySelectorAll('img').forEach(img => {
    const raw = img.currentSrc || img.src;
    if (!raw || raw.startsWith('data:') || NON_CONTENT_IMG.test(raw)) return;
    // the ONLY images dropped as furniture are the developer-community thread
    // avatars - user furniture, not this page's content - identified by their
    // own host (community.infineon.com) AND by container. Everything else,
    // gifs included, stays.
    if (/community\.infineon\.com|infineon-developer-community\.png/i.test(raw)) return;
    if (img.closest('#community, [id*="community" i], [class*="community" i]')) return;
    // collapse the responsive renditions of ONE picture (…/foo.jpg?wid=1400,
    // ?wid=600, and the Scene7 ':asset-…' preset) to a single canonical item
    const canon = raw.split('?')[0].split(':asset')[0];
    if (imgSeen.has(canon)) return;
    imgSeen.add(canon);
    out.images.push({url: canon,
      title: (img.alt || img.title || '').replace(/\s+/g, ' ').trim().slice(0, 150)});
  });

  // Two blocks we deliberately do NOT store the contents of - only whether the
  // page has them and whether they are empty. The parametric OPN table and the
  // Developer community threads both churn constantly (new posts, re-sorted
  // rows) and would swamp the change feed; their presence is the useful fact,
  // and "exists but empty" is a real third state, distinct from absent.
  // Booleans only, deliberately: these components nest (a wrapper and the
  // table share a class) and carry filter rows, so any row count taken here
  // would be a confident wrong number - 84 where the page says 7 results.
  const tableEls = Array.from(document.querySelectorAll(
    '.ifx_product_table_component, .producttablereference'));
  const tableHasRows = tableEls.some(e => e.querySelectorAll('tbody tr').length > 0);
  // NB #community only, never .btn-askcommunity: that "Go to community" button
  // is site-wide furniture and appears on pages with no community section.
  const comm = document.querySelector('react-component#community, #community');
  const commTxt = comm ? (comm.innerText || '') : '';
  out.flags = {
    product_table: {exists: tableEls.length > 0, has_content: tableHasRows},
    developer_community: {
      exists: !!comm,
      // the component renders its own empty state rather than disappearing
      has_content: !!comm && !/there are no questions/i.test(commTxt)
    }
  };

  // Partner solutions: every partner card is an OBJECT like a document -
  // name (from the logo's alt text), the partner's link, and the assigned
  // logo image for previewing.
  const pysec = document.querySelector('#partner-solutions, [class*="partner-solution" i]');
  if (pysec) {
    const seenP = new Set();
    pysec.querySelectorAll('img').forEach(img => {
      const alt = (img.alt || '').replace(/\s+/g, ' ').trim();
      let root = img.closest('a[href]');
      if (!root) root = img.closest('[class*="card" i], li, [class*="col" i]') || img.parentElement;
      const a = (root.matches && root.matches('a[href]')) ? root
        : (root.querySelector ? root.querySelector('a[href]') : null);
      let name = alt.replace(/\s*logo\s*$/i, '').trim();
      const rootTxt = (root.innerText || '').replace(/\s+/g, ' ').trim();
      if (!name) name = rootTxt.slice(0, 80);
      if (!name || seenP.has(name.toLowerCase())) return;
      seenP.add(name.toLowerCase());
      out.partners.push({title: name.slice(0, 120), url: a ? a.href : '',
                         meta: (rootTxt && rootTxt !== name) ? rootTxt.slice(0, 200) : '',
                         img: (img.currentSrc || img.src || '').slice(0, 400)});
    });
  }

  // The hero/stage image: the big image beside or behind the title at the
  // top of the page (its class differs per page type - hero, stage, banner,
  // product photo - so it is found by POSITION, not by name). Three states:
  // a page-specific image, the DEFAULT image (exists but has_content=false),
  // or nothing assigned. Header/nav logos never count.
  let heroSrc = '';
  const inChrome = el => !!el.closest('header, nav, [role="navigation"], footer');
  for (const im of document.querySelectorAll('img')) {
    const r = im.getBoundingClientRect();
    const top = r.top + window.scrollY;
    if (top > 1400 || r.width < 180 || r.height < 100 || inChrome(im)) continue;
    heroSrc = im.currentSrc || im.src || '';
    if (heroSrc) break;
  }
  if (!heroSrc) {
    // some stages paint the image as a CSS background instead
    for (const el of document.querySelectorAll(
        '[class*="hero" i], [class*="stage" i], [class*="banner" i], [class*="teaser" i]')) {
      const r = el.getBoundingClientRect();
      const top = r.top + window.scrollY;
      if (top > 1400 || r.width < 300 || r.height < 120 || inChrome(el)) continue;
      const m = (getComputedStyle(el).backgroundImage || '').match(/url\(["']?([^"')]+)/);
      if (m && m[1]) { heroSrc = m[1]; break; }
    }
  }
  // A hero served from Infineon's own placeholder set (…/missing-product-
  // images/…) is the DEFAULT image, not a page-specific one - the folder name
  // matches none of default/placeholder/fallback, so it had to be named.
  const heroDefault = !!heroSrc &&
    /default|placeholder|fallback|no-?image|missing-product-images/i.test(heroSrc);
  out.flags.hero_image = {exists: !!heroSrc,
                          has_content: !!heroSrc && !heroDefault,
                          src: (heroSrc || '').slice(0, 400)};

  // The page's OWN section navigation is the authoritative list of the
  // components it carries - read here, after full render, so it reflects the
  // live page. Each nav anchor is a real section (not a text match), which is
  // exactly "make sure they are components". Reuses the same NAV_ANCHORS the
  // section lookups trust; the nav is scrolled fully open first (it mounts its
  // anchors lazily as sections enter the viewport) - see _settle_section_nav.
  const navSeen = new Set();
  out.components = [];
  document.querySelectorAll(NAV_ANCHORS).forEach(a => {
    // A nav entry the page does not DISPLAY is not a component it carries.
    // Infineon's template ships modules it has nothing for - Documents is the
    // common one - collapsed to zero height rather than leaving them out, so
    // the anchor is in the DOM at 0x0. innerText is empty for those, and the
    // `|| a.textContent` fallback that used to be here read them anyway: the
    // adapters-chargers page reported a Documents section that nobody can see.
    const r = a.getBoundingClientRect();
    if (!r.width || !r.height) return;
    if (a.checkVisibility && !a.checkVisibility()) return;
    const t = (a.innerText || '').replace(/\s+/g, ' ').trim();
    const k = t.toLowerCase();
    if (t && t.length < 40 && !navSeen.has(k)) { navSeen.add(k); out.components.push(t); }
  });

  // "Product details" sub-tabs (Parametrics / Ordering / Description / Features
  // / Benefits) - the tabbed block inside a component page.
  const dt = new Set();
  document.querySelectorAll('[role="tab"], .nav-link, .tab-link, .nav-item a, ' +
    '[class*="tab" i] a, [class*="tab" i] button').forEach(el => {
    const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (/^(parametrics?|ordering|description|features?|benefits?)$/i.test(t)) dt.add(t.toLowerCase());
  });
  out.detail_tabs = Array.from(dt);

  // Price (component pages): the buying-options block, TEXT ONLY - to be
  // shown on the page like overview/features, not as its own object. The
  // price-container carries the WHOLE story - unit price, stock AND the
  // volume-pricing range - where the narrower product-price div stops at the
  // unit price. Button labels are not price text and get stripped.
  let priceTxt = '';
  const pe = document.querySelector('[class*="price-container" i]')
    || document.querySelector('[class*="buyingoption" i], [class*="buying-option" i], '
       + '[class*="pricing" i], [class*="product-price" i], [class*="unitprice" i]');
  if (pe) priceTxt = (pe.innerText || '').replace(/\s+/g, ' ')
    .replace(/Add to Cart|View distributor'?s? inventory|Buy online|Log ?in to myInfineon|Contact sales/gi, '')
    .replace(/\s+/g, ' ').trim().slice(0, 500);
  out.price = priceTxt;

  return out;
}
"""

# "Fully loaded" has no single browser event on dynamic pages. This resolves
# when the DOM has been mutation-silent for `windowMs` AND no loading
# spinner/skeleton is visible (spinner check relaxes after 12s so a
# decorative element cannot hang the capture; hard cap 25s).
QUIESCENCE_JS = r"""
(windowMs) => new Promise(resolve => {
  const start = Date.now();
  const deadline = start + 25000;
  const spinnerGrace = start + 12000;
  let last = Date.now();
  const obs = new MutationObserver(muts => {
    for (const m of muts) {
      if ((m.type === 'childList' &&
           (m.addedNodes.length || m.removedNodes.length)) ||
          m.type === 'characterData') { last = Date.now(); return; }
    }
  });
  obs.observe(document.body,
              {childList: true, subtree: true, characterData: true});
  const spinnerVisible = () => {
    for (const el of document.querySelectorAll(
        '[class*="spinner" i], [class*="skeleton" i], [class*="shimmer" i], [class*="loading" i]')) {
      if (el.offsetParent === null) continue;
      const r = el.getBoundingClientRect();
      if (r.width > 8 && r.height > 8) return true;
    }
    return false;
  };
  const tick = () => {
    const now = Date.now();
    if (now > deadline) { obs.disconnect(); resolve('timeout'); return; }
    if (now - last >= windowMs &&
        (now > spinnerGrace || !spinnerVisible())) {
      obs.disconnect(); resolve('quiet'); return;
    }
    setTimeout(tick, 250);
  };
  tick();
})
"""

# analytics/marketing beacons never stop - exclude them from network-drain
_NOISE_HOSTS = ("google", "hotjar", "qualtrics", "facebook", "linkedin",
                "bing", "demdex", "clarity", "doubleclick", "newrelic",
                "nr-data", "omtrdc", "eloqua", "quantserve", "licdn",
                "reddit", "baidu", "adobedtm", "everesttech")


def _attach_inflight_tracker(page):
    """Counts the page's own pending XHR/fetch requests."""
    state = {"inflight": 0, "last_zero": time.monotonic()}

    def _is_content(req):
        if req.resource_type not in ("xhr", "fetch"):
            return False
        u = req.url.lower()
        return not any(h in u for h in _NOISE_HOSTS)

    def on_req(req):
        if _is_content(req):
            state["inflight"] += 1

    def on_done(req):
        if _is_content(req):
            state["inflight"] = max(0, state["inflight"] - 1)
            if state["inflight"] == 0:
                state["last_zero"] = time.monotonic()

    page.on("request", on_req)
    page.on("requestfinished", on_done)
    page.on("requestfailed", on_done)
    return state


async def _drain_network(page, state, hold_s=1.2, cap_s=15.0):
    """Waits until no first-party XHR/fetch has been in flight for hold_s."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < cap_s:
        if state["inflight"] == 0 and \
                time.monotonic() - state["last_zero"] >= hold_s:
            return True
        await page.wait_for_timeout(250)
    return False


async def _settle_quiet(page, window_ms=1500):
    """DOM-mutation silence + spinner absence (see QUIESCENCE_JS)."""
    try:
        return await page.evaluate(QUIESCENCE_JS, window_ms)
    except Exception:
        return "error"


# Sticky/fixed elements repeat down a stitched full-page screenshot; make them
# render once, in place. Run only right before the screenshot.
UNSTICK_JS = r"""
() => {
  let n = 0;
  const vw = document.documentElement.clientWidth;
  document.querySelectorAll('*').forEach(el => {
    const p = getComputedStyle(el).position;
    if (p !== 'sticky' && p !== 'fixed') return;
    const r = el.getBoundingClientRect();
    if (r.width > vw * 0.6 && r.height > 24) {
      // full-width sticky chrome (global header, section nav): it follows the
      // scroll and would be stitched down the whole image - and pinning it
      // static only drops it in mid-page. Hide it for the screenshot; the
      // text layer still records every word of it, so changes are not lost.
      el.style.setProperty('display', 'none', 'important');
    } else {
      el.style.setProperty('position', 'static', 'important');
    }
    n++;
  });
  // NB: no DOM-level width clipping here. Forcing max-width/overflow on the
  // wide products table's container squeezed the layout and cut its heading
  // down to "cts". The screenshot itself is clipped to the viewport width
  // instead, which leaves the page exactly as the browser laid it out.
  // Anything horizontally scrolled (the products table drifts right during
  // the walks) must go home, or the clipped screenshot shows its tail.
  document.querySelectorAll('*').forEach(el => {
    if (el.scrollLeft) el.scrollLeft = 0;
  });
  window.scrollTo(0, 0);
  return n;
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

# The canonical section-nav selector, mirrored from NAV_ANCHORS inside
# SECTIONS_JS. Kept in sync deliberately - the component read and this
# stabiliser must agree on what "the nav" is.
NAV_SEL = ('#stickyNavTab a[href^="#"], .maintabs a[href^="#"], '
           '[class*="tabbar" i] a[href^="#"], [class*="anchor-nav" i] a[href^="#"], '
           '[class*="stickynav" i] a[href^="#"], [class*="sticky-nav" i] a[href^="#"]')


async def _settle_section_nav(page, cap_s=12.0):
    """Make sure the sticky section-nav is FULLY populated before its anchors
    are read as the component list.

    The nav mounts its links lazily - each one appears only once its section
    has scrolled through the viewport - and then takes a beat to finish
    rendering. Reading too early silently drops real components. So: scroll the
    whole page through (top -> bottom -> top) to force every section to mount,
    then poll the nav's anchor count until it stops growing. Returns the final
    count (best-effort; never raises - a nav-less page just returns 0)."""
    try:
        await page.evaluate("""async () => {
          const d = ms => new Promise(r => setTimeout(r, ms));
          const step = Math.max(400, Math.floor(window.innerHeight * 0.9));
          const H = () => document.body ? document.body.scrollHeight : 0;
          for (let y = 0; y <= H(); y += step) { window.scrollTo(0, y); await d(90); }
          window.scrollTo(0, H()); await d(200);
          window.scrollTo(0, 0); await d(200);
        }""")
        # The full scroll above already mounts every section's anchor, so poll
        # until the count holds steady - INCLUDING steady at zero, so a page
        # with no section-nav (family/application pages) bails in ~1s instead
        # of burning the whole cap.
        last, stable = -1, 0
        for _ in range(max(4, int(cap_s / 0.4))):
            n = await page.evaluate(
                "sel => document.querySelectorAll(sel).length", NAV_SEL)
            if n == last:
                stable += 1
                if stable >= 2:      # two equal reads in a row = settled
                    return n
            else:
                stable = 0
            last = n
            await page.wait_for_timeout(400)
        return max(last, 0)
    except Exception as exc:
        if INJECT_JS:
            print(f"  [section-nav] settle failed: {exc}")
        return 0


IN_NAV_JS = "el => !!el.closest('header, nav, [role=\"navigation\"], footer')"

# A filter dropdown is not an accordion. These in-page menu toggles sit in the
# page body, not in header/nav, so IN_NAV_JS lets them through - and opening
# one injects the site's entire application/product taxonomy as menu options.
# A part page carries SEVEN copies of the same filter bar (duplicate ids and
# all), so expanding them added 1,824 lines of menu text: every part page then
# read as changed, and the extra DOM dragged the run down to 3.2 pages/min.
# Menu OPTIONS are not page content; the filtered content itself is already in
# the DOM and is captured either way.
MENU_TOGGLE_SEL = ('[data-bs-toggle="dropdown"], [data-toggle="dropdown"],'
                   ' .dropdown-toggle, .family-filter, #filter-container')
SKIP_CLICK_JS = ("el => !!el.closest('header, nav, [role=\"navigation\"], footer')"
                 " || !!el.closest('" + MENU_TOGGLE_SEL + "')")

# Bring any still-unswapped card image into view so the page loads it.
# These cards ship a 1x1 data: URI and swap the real icon in when the image
# intersects the viewport, so a card below the fold of a freshly-opened tab
# keeps its placeholder forever - the icon is on the page for a reader who
# scrolls down, and was simply never in ours. Returns how many are still
# unresolved, so the caller can stop as soon as there is nothing to wait for.
LAZY_IMG_JS = r"""
() => {
  const pend = [...document.querySelectorAll('img')].filter(im => {
    const s = im.getAttribute('src') || '';
    return /^data:/i.test(s) && im.offsetParent !== null;
  });
  // Put the page back EXACTLY where it was. Nudging these into view scrolls
  // the document, and the widget below re-renders around the viewport: parking
  // at the top afterwards left the design-resources pane showing different
  // cards than the rest of the capture had just read, and the page image list
  // came back short. Restore the offset, do not assume it was zero.
  const y = window.scrollY, x = window.scrollX;
  pend.forEach(im => {
    try { im.scrollIntoView({block: 'center', inline: 'nearest'}); } catch (e) {}
    im.loading = 'eager';
  });
  window.scrollTo(x, y);
  return pend.length;
}
"""


async def _load_lazy_images(page, rounds=3):
    """Nudge unresolved card icons into view until they stop being placeholders."""
    prev = None
    for _ in range(rounds):
        try:
            n = await page.evaluate(LAZY_IMG_JS)
        except Exception:
            return
        if not n or n == prev:
            break
        prev = n
        await page.wait_for_timeout(300)


async def _dom_click(el):
    """Fire the element's click handler directly, without Playwright's
    actionability wait.

    click() first insists the element is visible, stable, enabled and able to
    receive events. Infineon's sticky header covers most tabs, so that check
    ran to its FULL timeout and then threw - 43 controls on a part page, ~30 s
    spent waiting to do nothing, and the tab left unclicked either way. The
    codebase already used a DOM click for the design-resources tabs for exactly
    this reason; this is that, everywhere.

    The click SEQUENCE is deliberately unchanged - one control at a time, with
    the same pause after each. Firing them all in one pass is faster still, but
    a panel that hydrates over XHR needs to land before the next tab replaces
    it: doing that dropped the partner-solutions content from part pages.
    """
    await el.evaluate("el => el.click()")

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

    Skips anything inside header/nav/footer so we don't pop open menus.

    THE FLAT SLEEPS BELOW ARE LOAD-BEARING. They look like the obvious thing to
    optimise - ~47 s per part page - and they are not. Two things were measured:

    * Replacing them with "wait until the DOM goes quiet, cap well above the old
      sleep" was 4% faster and DROPPED an image: a partner logo on 1 of 22 pages
      that both baseline passes captured. Mutation silence is not the same as
      loaded; a lazily-fetched asset can arrive after the DOM has gone still.
    * The 47 s figure came from profiling ONE page at a time. At the real
      concurrency of 8 these sleeps overlap other pages' work, so they are very
      nearly free - which is why removing them bought almost nothing.

    Anything touched here must be diffed against two same-code baseline passes
    (the noise floor is essentially zero: 0/22 pages differ), never against a
    single run and never on wall-clock alone."""
    start_url = page.url

    # 1. Tabs — click each so tab panels that lazy-load via XHR get fetched.
    #    ONE AT A TIME, with a beat between: batching every click into a single
    #    pass was 40 s faster and lost content, because a panel that hydrates
    #    over XHR needs to land before the next tab replaces it.
    try:
        tabs = await page.query_selector_all('[role="tab"]')
        for tab in tabs[:40]:
            try:
                if await tab.evaluate(SKIP_CLICK_JS):
                    continue
                await _dom_click(tab)
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
                if await btn.evaluate(SKIP_CLICK_JS):
                    continue
                await _dom_click(btn)
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
                await _dom_click(b)
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
                    if await btn.evaluate(SKIP_CLICK_JS):
                        continue
                    if not await btn.is_visible():
                        continue
                    await _dom_click(btn)
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
        net_state = _attach_inflight_tracker(page)

        # Keep the page's own content feeds: they carry the URLs for cards
        # whose anchors are JS-driven (see _api_url_map).
        api_bodies = []

        async def _keep_api(resp):
            try:
                if "/dataapi/" not in resp.url.lower():
                    return
                if "json" not in (resp.headers or {}).get("content-type", "").lower():
                    return
                api_bodies.append(await resp.text())
            except Exception:
                pass

        page.on("response", lambda r: asyncio.ensure_future(_keep_api(r)))

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
        # A 403/429/5xx is the CDN's bot-guard or a hiccup, NOT the page's
        # state: 65 healthy evaluation boards were once branded "not live" by a
        # single 403 burst. Back off and renavigate - blocks lift in seconds.
        # Only 404/410 pass through: "gone" is a real page state to record.
        for backoff_ms in (10_000, 25_000):
            if resp is None or resp.status not in TRANSIENT_HTTP:
                break
            print(f"  [blocked] HTTP {resp.status} on {url[:60]} - "
                  f"retrying in {backoff_ms // 1000}s", flush=True)
            await page.wait_for_timeout(backoff_ms)
            try:
                resp = await page.goto(url, wait_until="load",
                                       timeout=NAV_TIMEOUT_MS) or resp
            except Exception:
                pass                       # keep the previous resp; loop decides
        if resp is not None and resp.status in TRANSIENT_HTTP:
            # Refuse to record this as the page's state. The capture fails as
            # an error: the runner keeps the previous good baseline and the
            # page is retried next run - a block must never poison the library.
            raise Exception(f"{BLOCKED_TRANSIENT}: HTTP {resp.status} "
                            f"(CDN block / server hiccup, not the page's state)")
        result["http_status"] = resp.status if resp else None
        # Where the SERVER sent us, read from the navigation response and not
        # from page.url later on. By then this capture has clicked every tab
        # and expanded every accordion - page.url reflects our own clicking
        # (it already picks up a #news-highlights fragment), so trusting it
        # would let a stray click repoint the user's watch list at whatever
        # page we wandered onto. Only a server redirect may move the list.
        result["final_url"] = (resp.url if resp else url).split("#")[0]

        await page.add_style_tag(content=HIDE_CSS)
        try:                                       # all images/scripts finished
            await page.wait_for_load_state("load", timeout=20_000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)          # let JS widgets settle
        await page.evaluate(AUTO_SCROLL_JS)        # trigger lazy loading
        await _expand_interactive(page)            # tabs / accordions / load-more
        await page.wait_for_timeout(1000)
        # "Fully loaded" stack for dynamic pages (no single event exists):
        # 1) first-party XHR/fetch drained for 1.2s (analytics ignored)
        # 2) DOM mutation-silent for 1.5s AND no visible spinner/skeleton
        # 3) text length + image inventory stable between polls
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        await _drain_network(page, net_state)
        await _settle_quiet(page, 1500)
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
        panes_acc = {"design_resources": [], "trainings": []}
        try:
            # walk until the next-arrow goes inactive (high safety cap; some
            # tables have 14+ pages - a hard low cap loses items)
            for _ in range(40):
                part = await page.evaluate(SECTIONS_JS)
                docs_acc.extend(part.get("documents", []))
                nxt = await page.query_selector(
                    "#documents .pagination .right-page:not(.inactive), "
                    ".documents_ifx .pagination .right-page:not(.inactive)")
                if nxt is None:
                    break
                # DOM-level click: the sticky header can intercept
                # coordinate clicks even with force=True
                await nxt.evaluate("el => el.click()")
                await page.wait_for_timeout(400)
                await _settle_quiet(page, 700)
            # return the table to page 1 so the text layer and screenshot
            # always reflect the same (first-page) state on every run
            first_pg = await page.query_selector(
                "#documents .pagination .page-numbers, "
                ".documents_ifx .pagination .page-numbers")
            if first_pg is not None:
                await first_pg.evaluate("el => el.click()")
                await page.wait_for_timeout(700)
        except Exception:
            pass

        # The OPN/products table paginates too: only the current page's rows
        # exist in the DOM at once, so the text layer used to hold ~20 of a
        # family's parts. Walk every table page and accumulate the component's
        # text - the block joins text.txt under its own heading, so EVERY row
        # is tracked (diffed, searchable, attributable) like any page text.
        table_text = ""
        try:
            _tsel = ".ifx_product_table_component, .producttablereference"
            _tnext = (".ifx_product_table_component .pagination"
                      " .right-page:not(.inactive),"
                      " .producttablereference .pagination"
                      " .right-page:not(.inactive),"
                      " .ifx_product_table_component"
                      " button[aria-label*='next' i]:not([disabled]),"
                      " .producttablereference"
                      " button[aria-label*='next' i]:not([disabled])")
            comp = await page.query_selector(_tsel)
            # A product FAMILY table is 1-3 pages. A finder / selection tool
            # reuses this SAME component over the whole global catalogue - the
            # simulation-model finder has 10,544 rows - so the walk ran to its
            # 60-page cap, stored ~120k lines of only 57% of the rows, took ~20
            # min, and then mislabelled the truncated result as "all rows".
            # These pages carry a "N matching results" readout; when it is far
            # past any family's size, skip the walk. Page 1 (~100 rows) is
            # already in the body text, and the walk's "all rows" heading is
            # only reached below when we did cover every row.
            total_results = None
            if comp is not None:
                try:
                    n_txt = await page.evaluate(
                        "() => (document.querySelector('.total-count')"
                        "||{}).textContent || ''")
                    m = re.search(r"\d[\d,]*", n_txt or "")
                    if m:
                        total_results = int(m.group(0).replace(",", ""))
                except Exception:
                    total_results = None
            big = total_results is not None and total_results > 300
            if comp is not None and not big:
                # widen the table first: the pager ships a per-page select
                # (10/20/50/100) - at 100 most families fit on one page and
                # every row lands straight in the body text
                try:
                    sel = await page.query_selector(
                        ".ifx_product_table_component select.per-page-value,"
                        " .producttablereference select.per-page-value")
                    if sel is not None:
                        await sel.select_option("100")
                        await page.wait_for_timeout(600)
                        await _settle_quiet(page, 800)
                except Exception:
                    pass
            if comp is not None and not big and await page.query_selector(_tnext):
                chunks = []
                for _ in range(60):
                    chunks.append(await comp.evaluate(
                        "el => el.innerText || ''"))
                    nxt = await page.query_selector(_tnext)
                    if nxt is None:
                        break
                    await nxt.evaluate("el => el.click()")
                    await page.wait_for_timeout(400)
                    await _settle_quiet(page, 700)
                # back to page 1, so screenshot + body text are stable per run
                first_tp = await page.query_selector(
                    ".ifx_product_table_component .pagination .page-numbers,"
                    " .producttablereference .pagination .page-numbers")
                if first_tp is not None:
                    await first_tp.evaluate("el => el.click()")
                    await page.wait_for_timeout(500)
                if len(chunks) > 1:
                    # page 1 is already in the body text - store the rest
                    table_text = "\n".join(chunks[1:])
        except Exception:
            table_text = ""

        # Design-resources panes paginate too (e.g. 11 dev tools shown 5 per
        # page): activate each tab and walk its pages, merging the items.
        try:
            for tb in await page.query_selector_all('[id^="ds-tab-"]'):
                tab_id = (await tb.get_attribute("id")) or ""
                # Never walk the Overview tab. It is the union of the others,
                # so it adds nothing - but paginating it re-renders the hidden
                # panes with union content AND leaves the shared pager on page
                # 3, so the next tab's walk started three pages in and the
                # union's cards got filed under whatever tab they landed in.
                if tab_id.lower().endswith("overview"):
                    continue
                try:
                    await tb.scroll_into_view_if_needed(timeout=1500)
                    # DOM-level click: sticky header intercepts coordinate
                    # clicks (even force=True) and the pane never activates
                    await tb.evaluate("el => el.click()")
                    await page.wait_for_timeout(300)
                    await _settle_quiet(page, 700)
                except Exception:
                    continue
                # Rewind to page 1 before walking - defensive only. Switching
                # tab does reset the pager, so this normally takes zero steps:
                # it only looked otherwise while the walk still paginated the
                # Overview union, which left the pager advanced. Kept because a
                # walk that silently starts on page 3 loses items quietly.
                for _ in range(40):
                    prv = None
                    for arrow in await page.query_selector_all(
                            ".designresource-component .pagination "
                            ".left-page-ds:not(.inactive), "
                            ".designresource-component .pagination "
                            ".left-page:not(.inactive)"):
                        if await arrow.is_visible():
                            prv = arrow
                            break
                    if prv is None:
                        break
                    await prv.evaluate("el => el.click()")
                    await page.wait_for_timeout(350)

                # walk ALL of this pane's pages (14+ seen in the wild)
                for _ in range(40):
                    part = await page.evaluate(SECTIONS_JS)
                    for cat in panes_acc:
                        panes_acc[cat].extend(part.get(cat, []))
                    # every pane has its own pagination bar in the DOM;
                    # only the active pane's arrow is visible
                    nxt = None
                    for arrow in await page.query_selector_all(
                            ".designresource-component .pagination "
                            ".right-page-ds:not(.inactive), "
                            ".designresource-component .pagination "
                            ".right-page:not(.inactive)"):
                        if await arrow.is_visible():
                            nxt = arrow
                            break
                    if nxt is None:
                        break
                    await nxt.evaluate("el => el.click()")
                    await page.wait_for_timeout(700)
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

        # Put every widget back to its opening state so the screenshot is the
        # same on every run (otherwise the documents table is frozen on
        # whatever page the walk ended on, and diffs go off for no reason).
        try:
            for sel in ("#documents .pagination .left-page:not(.inactive), "
                        ".documents_ifx .pagination .left-page:not(.inactive)",
                        ".designresource-component .pagination "
                        ".left-page-ds:not(.inactive)"):
                for _ in range(40):
                    target = None
                    for cand in await page.query_selector_all(sel):
                        if await cand.is_visible():
                            target = cand
                            break
                    if target is None:
                        break
                    await target.evaluate("el => el.click()")
                    await page.wait_for_timeout(280)
            first_tab = await page.query_selector('[id^="ds-tab-"]')
            if first_tab is not None:
                await first_tab.evaluate("el => el.click()")
                await page.wait_for_timeout(400)
        except Exception:
            pass

        # The section nav is position:fixed ONLY while scrolled down, so a
        # screenshot taken mid-page paints it across the middle of the image.
        # Scroll home, let the site's own scroll handler release it, and only
        # then neutralise whatever is still sticky.
        try:
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(900)
            n_unstuck = await page.evaluate(UNSTICK_JS)
            await page.wait_for_timeout(300)
            if INJECT_JS:
                print(f"  [unstick] neutralised {n_unstuck} sticky/fixed")
        except Exception as exc:
            print(f"  [unstick] FAILED: {exc}")   # never swallow this again

        # Clip to the viewport width: a wide element (products table ~2500px)
        # otherwise makes the full-page image as wide as itself, leaving the
        # page marooned in white. Height = the real document height.
        try:
            dims = await page.evaluate(
                "() => ({w: document.documentElement.clientWidth,"
                " h: Math.max(document.documentElement.scrollHeight,"
                " document.body ? document.body.scrollHeight : 0)})")
            await page.screenshot(
                path=str(out_dir / "shot.png"), full_page=True,
                clip={"x": 0, "y": 0, "width": dims["w"],
                      "height": min(dims["h"], 30000)}, timeout=60_000)
        except Exception:
            await page.screenshot(path=str(out_dir / "shot.png"),
                                  full_page=True, timeout=60_000)

        # Remove consent overlays from the DOM so their text never appears.
        try:
            await page.evaluate(
                f"document.querySelectorAll('{BANNER_SELECTORS}').forEach(e => e.remove())")
        except Exception:
            pass

        text = await page.evaluate(TEXT_JS)
        if table_text:
            # the walked table pages (2..N) join the tracked text under their
            # own heading - page 1's rows are already in the body text above
            text += "\nProducts table (all rows)\n" + table_text + "\n"
        images = await page.evaluate(IMAGES_JS)
        title = await page.title()
        # Infineon intermittently serves blank shells (HTTP 200/202, empty
        # body). Never store those as baselines - fail the capture instead
        # so the page is reported as blank and retried on the next run.
        if len(text.strip()) < 50 and not title.strip():
            raise Exception(
                f"{BLANK_RENDER}: page answered HTTP {result['http_status']} "
                f"with an empty body ({len(text.strip())} chars of text)")
        # The component list is read from the section-nav, which mounts its
        # anchors lazily on scroll and takes a beat to settle - open it fully
        # first so no real component is missed. (Screenshot/text/images above
        # are already captured, so this extra scroll cannot disturb them.)
        nav_n = await _settle_section_nav(page)
        if INJECT_JS:
            print(f"  [section-nav] {nav_n} components settled")
        # NOT wrapped in try/except on purpose. A JS error here used to yield
        # sections={} on a capture still marked "ok" - an EMPTY library stored
        # as groundtruth, silently. That has cost this project three separate
        # bugs. Let it raise: the page is then reported as an error and retried,
        # which is loud, correct, and recoverable. Never store a lie.
        sections = await page.evaluate(SECTIONS_JS)
        # merge documents collected across the table's pages + clean titles.
        # Distinct documents may share a title (language variants), so entries
        # are NEVER merged across different links - only exact repeats are
        # dropped, plus meta-less secondary anchors of an already-seen title
        # (the same row collected again via its download-icon link).
        try:
            by_key = {}
            for d in list(sections.get("documents", [])) + docs_acc:
                d["title"] = _clean_doc_title(d.get("title", ""))
                # url tells variants apart; login-gated rows have none, so two
                # language variants of one title were keyed identically and one
                # was dropped. Fall back to the meta (which carries the
                # language) when there is no url - matching the JS-layer key.
                url = d.get("url", "")
                key = (d["title"].lower(), url,
                       "" if url else d.get("meta", ""))
                prev = by_key.get(key)
                if prev is None or len(d.get("meta", "")) > len(prev.get("meta", "")):
                    by_key[key] = d
            # safety net: a row collected via a second anchor (download icon)
            # yields a meta-less twin sharing the row's URL - drop those
            titled = {d["title"].lower() for d in by_key.values() if d.get("meta")}
            titled_urls = {d.get("url") for d in by_key.values()
                           if d.get("meta") and d.get("url")}
            sections["documents"] = [
                d for d in by_key.values()
                if d.get("meta") or (d["title"].lower() not in titled and
                                     d.get("url") not in titled_urls)]
        except Exception:
            pass
        # Browse-by-category: one entry per destination page. The same page is
        # linked twice (tile + "<name> Overview" list link) - keep the clean
        # title, and drop generic CTA links to product tables.
        try:
            page_base = url.split("#")[0].rstrip("/")
            for cat in ("categories", "products"):
                sections[cat] = _clean_link_list(sections.get(cat, []),
                                                 page_base)
        except Exception:
            pass

        # merge paginated design-resource/training panes harvested per tab.
        # The same card is often captured several times across passes (with or
        # without its lazy-loaded video URL or group): dedupe by title+group,
        # preferring grouped entries and keeping the richest one.
        try:
            _PFX = re.compile(
                r"^(development tools?|embedded software|boards? *& *designs?|"
                r"services|training|pcb design data|simulation models)\s*",
                re.I)
            _DL = re.compile(r"^download\s*-?\s*", re.I)
            _DESC = re.compile(r"(?i)\s*click on read more.*$|\s*read more\s*$")
            _BLOB = re.compile(r"(?i)(watch video|read more|add to cart)\s*$")
            for cat in ("design_resources", "trainings"):
                pool = list(sections.get(cat, [])) + panes_acc.get(cat, [])
                # A card's own text glued together ("<title>EN,JA,CNWatch
                # Video") is never a real item: drop these blobs, and drop any
                # link-less entry whose title merely repeats a real one.
                pool = [it for it in pool
                        if not _BLOB.search(it.get("title", ""))]
                # Drop a link-less item only when it is the SAME item as one we
                # already have with a link - never when one title merely
                # contains another. A substring test deleted real documents:
                # the board "FM0-V48-S6E1A1" is a substring of the schematic
                # "SK-FM0-V48-S6E1A1 Evaluation Board Schematics", so a whole
                # PCB design data group vanished because a board card shared
                # part of its name.
                linked = {re.sub(r"\W+", "", it.get("title", "").lower())
                          for it in pool if it.get("url")}
                pool = [it for it in pool
                        if it.get("url") or
                        re.sub(r"\W+", "", it.get("title", "").lower())
                        not in linked]
                # tabbed pages: grouped (pane) items are authoritative - a
                # single flaky pass can inject ungrouped page-wide junk
                grouped_pool = [it for it in pool if it.get("group")]
                if grouped_pool:
                    pool = grouped_pool
                grouped_titles = {it.get("title", "").lower()
                                  for it in pool if it.get("group")}
                best = {}
                order = []
                for it in pool:
                    # These strip the category chip, a "DOWNLOAD - " prefix and
                    # trailing description noise - all artefacts of having to
                    # rebuild a title out of a card's concatenated innerText.
                    # A title the card gave us outright is the page's own text
                    # and is left exactly as the page says it, or the library
                    # would hold "DAVE(TM) IDE" for a card the page titles
                    # "DOWNLOAD - DAVE(TM) IDE" - and a rename between those
                    # two forms would never be flagged.
                    if not it.get("exact"):
                        t = _DESC.sub("", _DL.sub(
                            "", _PFX.sub("", it.get("title", "").strip()))).strip()
                        if len(t) >= 6:
                            it["title"] = t
                    title_l = it.get("title", "").lower()
                    if not it.get("group") and title_l in grouped_titles:
                        continue
                    # twins = same normalized title AND same link; distinct
                    # products can share one vendor URL, so title is part of
                    # the identity. A link-less (gated) card has no url to tell
                    # variants apart, so its language-bearing meta joins the key
                    # - or a CN and a JA "001-13679_SPIS" datasheet collapse
                    # into one and a real document is lost (same bug the
                    # documents table had).
                    norm_t = re.sub(r"\s+", "", title_l)
                    url_l = it.get("url", "").lower()
                    # language, not the whole meta: two real variants differ by
                    # language (SPIS CN vs JA), but the same card rendered for
                    # desktop and mobile has jittery meta yet one language, so
                    # keying on full meta wrongly kept those as separate copies.
                    ident = (norm_t, url_l or ("|" + _item_lang(it.get("meta", ""))))
                    k = (ident, it.get("group", ""))
                    prev = best.get(k)
                    if prev is None:
                        best[k] = it
                        order.append(k)
                    elif (len(it.get("meta", "")) +
                          (0 if _PFX.match(it.get("title", "")) else 20) +
                          -len(it.get("title", ""))) > \
                            (len(prev.get("meta", "")) +
                             (0 if _PFX.match(prev.get("title", "")) else 20) +
                             -len(prev.get("title", ""))):
                        best[k] = it
                merged = [best[k] for k in order]
                # a card can be collected twice: once via its (rare) href and
                # once href-less - drop url-less repeats of a titled+linked item
                titled_urls = {it.get("title", "").lower()
                               for it in merged if it.get("url")}
                sections[cat] = [
                    it for it in merged
                    if it.get("url") or
                    it.get("title", "").lower() not in titled_urls]
        except Exception:
            pass

        # Fill in links the markup never had. Infineon renders many cards with
        # an href-less <a> and wires the click in JS, so a card can be real,
        # titled and dated yet unreachable from the DOM. Its own feed knows the
        # URL - match on title and only ever ADD, never override a real href.
        try:
            api_map = _api_url_map(api_bodies)
            if api_map:
                filled = 0
                for cat, items in sections.items():
                    if cat == "images" or not isinstance(items, list):
                        continue
                    for it in items:
                        if not isinstance(it, dict) or it.get("url"):
                            continue
                        by_lang = api_map.get(_norm_title(it.get("title", "")))
                        if not by_lang:
                            continue
                        # give a row the URL for ITS language, so an EN and a JA
                        # variant of one title do not both get the first file
                        lang = _item_lang(it.get("meta", ""))
                        u = by_lang.get(lang) or by_lang.get("")
                        if u:
                            it["url"] = u
                            filled += 1
                result["api_links_filled"] = filled
        except Exception:
            pass

        # LAST step before writing: rewrite video authoring paths to the
        # delivery host. Must be after every merge - the pane walk pools in
        # its own copies of the items, so rewriting earlier gets overwritten.
        try:
            for cat, items in sections.items():
                if not isinstance(items, list):
                    continue
                for it in items:
                    if isinstance(it, dict) and it.get("url", "").lower().endswith(
                            (".mp4", ".webm", ".mov")):
                        it["url"] = _delivery_url(it["url"])
        except Exception:
            pass

        # the page's own hierarchy, kept on the URL row so the tree can be
        # built with one query instead of opening 4,145 snapshot files
        result["breadcrumb"] = [b for b in sections.get("breadcrumb", [])
                                if isinstance(b, dict) and b.get("title")]
        result["flags"] = sections.get("flags") or {}
        # authoritative component list (the page's own section nav) + the
        # Product-details sub-tabs + price text, for the per-type component map
        result["components"] = sections.get("components") or []
        result["detail_tabs"] = sections.get("detail_tabs") or []
        result["price"] = sections.get("price") or ""

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


async def _selftest(browser):
    """Parse-check the injected JS before a run touches a single real page.

    A syntax error in these scripts is invisible at import time (they are
    Python strings) and would otherwise surface as thousands of pages captured
    with empty libraries. Runs against about:blank in ~50ms.
    """
    page = await browser.new_page()
    try:
        await page.goto("about:blank")
        for name, js in (("SECTIONS_JS", SECTIONS_JS), ("TEXT_JS", TEXT_JS),
                         ("IMAGES_JS", IMAGES_JS), ("UNSTICK_JS", UNSTICK_JS)):
            try:
                await page.evaluate(js)
            except Exception as exc:
                raise RuntimeError(
                    f"{name} failed its parse/run self-test - refusing to start "
                    f"the run: {exc}") from None
    finally:
        await page.close()


async def capture_all(jobs, concurrency=4, progress=None, pause_check=None,
                      stop_check=None):
    """Capture many URLs. jobs = [(url, out_dir), ...]. Returns list of result dicts.

    The browser is restarted every PAGES_PER_BROWSER pages to keep memory flat
    on large (3-4k URL) runs.

    pause_check, if given, is a zero-arg callable polled before each capture: a
    truthy return holds the worker (no new page starts) until it clears, so the
    dashboard's Pause button stops the run without killing in-flight captures.

    stop_check, if given, ends the run: no new browser batch launches and any
    not-yet-started page is skipped, so Stop takes effect within one in-flight
    page. Everything already captured stays saved (the caller commits per page).
    """
    results = []
    async with async_playwright() as pw:
        for batch_start in range(0, len(jobs), PAGES_PER_BROWSER):
            if stop_check and stop_check():
                break
            batch = jobs[batch_start:batch_start + PAGES_PER_BROWSER]
            browser = await pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage"])
            if batch_start == 0:
                await _selftest(browser)
            sem = asyncio.Semaphore(concurrency)

            async def run_job(url, out_dir):
                # Hold OUTSIDE the semaphore so a paused worker frees its slot
                # instead of blocking a resume; the capture itself can't be
                # interrupted mid-flight, so pause takes effect within one page.
                while pause_check and pause_check():
                    if stop_check and stop_check():
                        return None
                    await asyncio.sleep(1)
                if stop_check and stop_check():
                    return None            # Stop requested: skip this page
                async with sem:
                    if stop_check and stop_check():
                        return None
                    res = await capture_one(browser, url, out_dir)
                    if progress:
                        progress(res)
                    return res

            batch_results = await asyncio.gather(
                *(run_job(u, d) for u, d in batch))
            results.extend(r for r in batch_results if r is not None)
            try:
                await browser.close()
            except Exception:
                pass
            if stop_check and stop_check():
                break
    return results
