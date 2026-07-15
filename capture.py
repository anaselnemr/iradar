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

# Document titles concatenate badge spans (type/access/version/size/date),
# sometimes joined by zero-width characters: cut everything from the badge
# sequence onward. Applied in Python so it is deterministic and testable.
_ZERO_WIDTH_RE = re.compile("[​‌‍⁠﻿]")
_DOC_BADGE_RE = re.compile(
    r"\s*(New|Updated)?\s*(PDF|ZIP|XLSX?|TXT|PPTX?|DOCX?)\s*"
    r"(Public|Confidential|Restricted).*$", re.I)


def _clean_doc_title(title):
    t = _ZERO_WIDTH_RE.sub("", title or "")
    cut = _DOC_BADGE_RE.sub("", t).strip()
    if len(cut) >= 8:
        return re.sub(r"\s*(New|Updated)$", "", cut, flags=re.I).strip()
    return t.strip()


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
    ['news',             /^(news( & highlights?)?|highlights?|videos?)$/i],
    ['trainings',        /^((support & )?trainings?( & webinars?)?|webinars?)$/i],
  ];
  const GENERIC_LINK = /^(explore more|learn more|read more.*|read on|get started|discover( more)?|show (more|all)|see (more|all)|view (more|all)|more|download|jump to|overview|add to cart.*|buy online.*|order now.*|boards? *& *designs?|development tools|embedded software|services)$/i;
  const NAV_LABEL = /^(product details|documents|design resources|support|about|overview|quicklinks?)$/i;

  const out = {documents: [], design_resources: [], news: [],
               trainings: [], features: [], benefits: [],
               overview: [], applications: [], about: [], categories: [],
               images: []};
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

  const CHIP_LABEL = /^(development tools?|embedded software|boards? *& *designs?|services|training|simulation (tools|models)|pcb design data|documents|partners)$/i;
  const cardTitle = (a) => {
    let el = a;
    for (let i = 0; i < 3 && el.parentElement; i++) {
      el = el.parentElement;
      const h = el.querySelector('h2,h3,h4,h5,strong,b,[class*="title" i],[class*="headline" i]');
      if (h) {
        const t = (h.textContent || '').replace(/\s+/g, ' ').trim();
        if (t.length > 2 && t.length < 150 && !GENERIC_LINK.test(t) &&
            !CHIP_LABEL.test(t)) return t;
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
            !CHIP_LABEL.test(t)) return t;
      }
    }
    return '';
  };

  const push = (cat, title, url, meta, group) => {
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
          meta.replace(/\s+/g, ' ').trim().slice(0, 260);
        return;
      }
      urlOwned.set(url, cat);
    }
    const key = cat + '|' + title + '|' + (url || '');
    if (seen.has(key)) return;
    seen.add(key);
    const item = {title: title, url: url || '',
                  meta: (meta || '').replace(/\s+/g, ' ').trim().slice(0, 260)};
    if (group) item.group = group;
    out[cat].push(item);
  };

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

  const collectAnchor = (cat, a, strictChrome) => {
    const href = a.href || '';
    if (href.startsWith('javascript:') || href.startsWith('mailto:')) return;
    if (strictChrome ? isPageChrome(a) : isTabStrip(a)) return;
    let t = (a.innerText || a.textContent || a.getAttribute('title') ||
             a.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    if (GENERIC_LINK.test(t)) {
      const ct = cardTitle(a);                     // teaser card: use its title
      if (!ct) return;                             // no real title -> skip junk
      t = ct;
    }
    let meta = '';
    const tr = a.closest('tr');
    if (tr) {
      const cells = Array.from(tr.querySelectorAll('td'))
        .map(td => (td.innerText || '').replace(/\s+/g, ' ').trim())
        .filter(Boolean);
      // Anchor text in document tables concatenates badge spans without
      // separators ("...GuideNewPDFPublicEN01_003.64MBApr 29, 2020"): cut the
      // title at the type+access badge sequence, or prefer a clean first cell.
      const badgeCut = t.search(/(New)?(PDF|ZIP|XLSX?|TXT|PPTX?)(Public|Confidential|Restricted)/);
      if (badgeCut > 8) {
        t = t.slice(0, badgeCut).replace(/\s*(New|Updated)$/i, '').trim();
      } else if (cells.length && cells[0].length > 8 &&
          (t.startsWith(cells[0].slice(0, 12)) || t.length > cells[0].length + 8)) {
        t = cells[0].replace(/\s*(New|Updated)$/i, '');
      }
      meta = cells.filter(x => x !== t).slice(0, 8).join(' | ');
    }
    push(cat, t, href, meta);
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
    const tabBtn = document.getElementById('ds-tab-' + suffix);
    const group = tabBtn
      ? (tabBtn.textContent || '').replace(/\s+/g, ' ').trim()
      : suffix.replace(/-/g, ' ');
    const cat = /training/i.test(suffix) ? 'trainings' : 'design_resources';
    pane.querySelectorAll('a[href]').forEach(a => {
      const li = a.closest('li, .boardlist');
      const href = a.href || '';
      if (href.startsWith('javascript:') || href.startsWith('mailto:')) return;
      if (isTabStrip(a)) return;
      let t = (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim();
      if (GENERIC_LINK.test(t)) {
        const ct = cardTitle(a);
        if (!ct) return;
        t = ct;
      }
      push(cat, t, href, li ? cardExtras(li) : '', group);
    });
    // href-less card/title anchors (trainings, CAD/IBIS file rows, gated
    // downloads); a video URL hides in the thumbnail rendition path
    pane.querySelectorAll('a:not([href])')
      .forEach(a2 => {
        const t = (a2.textContent || '').replace(/\s+/g, ' ').trim();
        if (t.length < 9 || t.length > 200 || GENERIC_LINK.test(t) ||
            /^(watch video|log ?in|clear all|sort by.*|download)$/i.test(t)) return;
        const li = a2.closest('li, .boardlist') || pane;
        const img = li.querySelector('img[src*=".mp4/"]');
        const vurl = img ? img.src.split('/jcr:content')[0] : '';
        push(cat, t, vurl, cardExtras(li), group);
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
    const thumb = card.querySelector('img[src*=".mp4/"]');
    push(cat, title, thumb ? thumb.src.split('/jcr:content')[0] : '',
         [...new Set(meta)].slice(0, 3).join(' | '));
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

  // Overview: the page's descriptive paragraph (outside the design-resources
  // widget and page chrome). Stored as one item.
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
    if (isPageChrome(h) || h.closest('.designresource-component, [id^="ds-"]')) continue;
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
    if (cells.length < 2 || cells[0].length < 8) return;
    push('documents', cells[0], '', cells.slice(1, 9).join(' | '));
  });

  // Pass 2: heading-based fallback, only for categories still empty
  for (const h of document.querySelectorAll('h1,h2,h3,h4,[role="heading"]')) {
    if (isTabStrip(h)) continue;                   // tab labels are not sections
    const ht = (h.textContent || '').replace(/\s+/g, ' ').trim();
    if (!ht || ht.length > 60) continue;
    for (const [cat, re] of CATS) {
      if (out[cat].length || !re.test(ht)) continue;
      if (cat === 'design_resources' && hasDsPanes) continue;
      const c = containerFor(h);
      if (!c) break;
      c.querySelectorAll('a[href]').forEach(a => collectAnchor(cat, a, true));
      break;
    }
  }

  document.querySelectorAll('video').forEach(v => {
    const srcEl = v.querySelector('source');
    const src = v.currentSrc || v.src || (srcEl ? srcEl.src : '');
    // placeholder <video> elements point at page URLs - only accept real media
    if (!v.poster && !/\.(mp4|webm|m3u8|mov)([?#]|$)|video-cdn|brightcove|youtube/i.test(src)) return;
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
    # DOM-level clicks - the sticky header intercepts coordinate clicks.
    try:
        for b in await page.query_selector_all('[id^="ds-tab-"]'):
            try:
                await b.evaluate("el => el.click()")
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
        net_state = _attach_inflight_tracker(page)

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

        # Design-resources panes paginate too (e.g. 11 dev tools shown 5 per
        # page): activate each tab and walk its pages, merging the items.
        try:
            for tb in await page.query_selector_all('[id^="ds-tab-"]'):
                try:
                    await tb.scroll_into_view_if_needed(timeout=1500)
                    # DOM-level click: sticky header intercepts coordinate
                    # clicks (even force=True) and the pane never activates
                    await tb.evaluate("el => el.click()")
                    await page.wait_for_timeout(300)
                    await _settle_quiet(page, 700)
                except Exception:
                    continue
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
        # Infineon intermittently serves blank shells (HTTP 200/202, empty
        # body). Never store those as baselines - fail the capture instead
        # so the page shows as an error and is retried on the next run.
        if len(text.strip()) < 50 and not title.strip():
            raise Exception(
                f"blank render (text={len(text.strip())} chars, "
                f"http={result['http_status']})")
        try:
            sections = await page.evaluate(SECTIONS_JS)
        except Exception:
            sections = {}
        # merge documents collected across the table's pages + clean titles.
        # Distinct documents may share a title (language variants), so entries
        # are NEVER merged across different links - only exact repeats are
        # dropped, plus meta-less secondary anchors of an already-seen title
        # (the same row collected again via its download-icon link).
        try:
            by_key = {}
            for d in list(sections.get("documents", [])) + docs_acc:
                d["title"] = _clean_doc_title(d.get("title", ""))
                key = (d["title"].lower(), d.get("url", ""))
                prev = by_key.get(key)
                if prev is None or len(d.get("meta", "")) > len(prev.get("meta", "")):
                    by_key[key] = d
            titled = {d["title"].lower() for d in by_key.values() if d.get("meta")}
            sections["documents"] = [
                d for d in by_key.values()
                if d.get("meta") or d["title"].lower() not in titled]
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
            for cat in ("design_resources", "trainings"):
                pool = list(sections.get(cat, [])) + panes_acc.get(cat, [])
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
                    # strip category label / DOWNLOAD prefix / description noise
                    t = _DESC.sub("", _DL.sub(
                        "", _PFX.sub("", it.get("title", "").strip()))).strip()
                    if len(t) >= 6:
                        it["title"] = t
                    title_l = it.get("title", "").lower()
                    if not it.get("group") and title_l in grouped_titles:
                        continue
                    # same link in the same group = same item (desktop/mobile
                    # variants of one card differ only in whitespace)
                    ident = it.get("url", "").lower() or title_l
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
                sections[cat] = [best[k] for k in order]
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
