"""Independent live-page probe: prints one JSON object to stdout.
Counts what a human would count, with its own logic (never imports the
extractor's selectors, so it can disagree).
Usage: python qa_probe.py <url>"""
import asyncio
import json
import sys

sys.path.insert(0, r"C:\Users\anasn\Desktop\InfineonWatch")
import capture  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

URL = sys.argv[1]

PROBE_JS = r"""
() => {
  const out = {doc_rows: [], doc_dates: 0, ds_tabs: [], features_li: 0,
               news_cards: 0, has_overview: false, applications: 0,
               images: [], imgs_with_alt: 0, title: document.title || '',
               feedback_visible: false, trainings_cards: 0};
  document.querySelectorAll('#documents tr, .documents_ifx tr').forEach(tr => {
    const tds = tr.querySelectorAll('td');
    if (!tds.length) return;
    const t = (tds[0].innerText || '').replace(/\s+/g, ' ').trim();
    if (t.length > 5) out.doc_rows.push(t.slice(0, 60));
    const row = (tr.innerText || '');
    if (/[A-Z][a-z]{2} \d{1,2}, \d{4}/.test(row)) out.doc_dates++;
  });
  document.querySelectorAll('[id^="ds-tab-"]').forEach(b =>
    out.ds_tabs.push({id: b.id, label: (b.textContent || '').trim()}));
  for (const h of document.querySelectorAll('h1,h2,h3,h4')) {
    const ht = (h.textContent || '').replace(/\s+/g, ' ').trim();
    if (/^(key )?features?$/i.test(ht) && !h.closest('header, footer, nav, .nav')) {
      let el = h;
      for (let i = 0; i < 5 && el.parentElement; i++) {
        el = el.parentElement;
        const n = el.querySelectorAll('li').length;
        if (n >= 2) { out.features_li = n; break; }
      }
    }
    if (/^overview$/i.test(ht) &&
        !h.closest('header, footer, nav, .nav, [id^="ds-"], #stickyNavTab'))
      out.has_overview = true;
    if (/^applications?$/i.test(ht) &&
        !h.closest('header, footer, nav, .nav, [id^="ds-"]'))
      out.applications = 1;
  }
  const seen = new Set();
  document.querySelectorAll('.fnVideoCard .card-title, .fnVideoCard h3').forEach(t => {
    const s = (t.textContent || '').trim();
    if (s) seen.add(s);
  });
  out.news_cards = seen.size;
  // images the way a person sees them: real <img> with a usable source.
  // Exclude exactly what the capture excludes (tracking pixels, missing-image
  // placeholders) or the live count outruns the stored one - a Qualtrics
  // Graphic.php pixel plus two "missing-product-images" placeholders read as
  // 3 phantom images and tripped the tolerance. Must stay in sync with
  // capture.py NON_CONTENT_IMG_JS.
  const NON_CONTENT_IMG =
    /qualtrics|siteintercept|wrqualtrics|graphic\.php|missing-product-images/i;
  const iu = new Set();
  document.querySelectorAll('img').forEach(im => {
    const s = im.currentSrc || im.src || '';
    if (!s || s.startsWith('data:') || NON_CONTENT_IMG.test(s)) return;
    iu.add(s);
    if ((im.alt || '').trim()) out.imgs_with_alt++;
  });
  out.images = Array.from(iu);
  // the Qualtrics "Feedback" tab must never survive into a capture
  out.feedback_visible = !!document.querySelector(
    'div[id^="QSI"], iframe[src*="qualtrics" i]');
  return out;
}
"""

PANE_COUNT_JS = r"""
() => {
  const act = Array.from(document.querySelectorAll('[id^="ds-tabpane-"]'))
    .find(p => p.offsetParent !== null);
  if (!act) return null;
  // Count RESULTS, the way the widget's own paginator counts them.
  //
  // Twice this has been wrong, and both times it cried EXTRA at correct data:
  //  - it counted a Set of unique anchor TEXTS, which missed titles of eight
  //    characters or fewer (C-STAT, C-RUN), collapsed two cards sharing a
  //    label into one, and scored a card whose only anchor says "Read more"
  //    as nothing.
  //  - it then demanded a card carry .anchor-title / .board-anchor /
  //    .card-title / .heading-dsobject. But a tab holds two KINDS of result:
  //    partner tiles (thumbnail + description) and Infineon download rows
  //    (filetype icon, title, size, "Download"). Download rows carry none of
  //    those, so a 16-result tab counted 11 - page 3 as 1 and page 4 as 0.
  //    The widget's own paginator counts them (5+5+5+1), and an AI reviewer
  //    counted them off the screenshots.
  // A result is a container not nested inside another one. Furniture is
  // excluded by name rather than by demanding a title.
  let cards = Array.from(act.querySelectorAll(
    '.boardlist, .wholepcbdata, .tab-tools, li.style-none'));
  if (!cards.length) cards = Array.from(act.querySelectorAll('li'));
  cards = cards.filter(el =>
    !el.closest('.partner-ul-box, .pagination, .pagination-container, ' +
                '#filter-container, .headerNote') &&
    !el.matches('li.box-style, li.partner-box, .pagination li'));
  // one result, not three: li > .mainboard > .wholepcbdata all match, so keep
  // only the outermost of any nest
  cards = cards.filter(c => !cards.some(o => o !== c && o.contains(c)));
  return {id: act.id, cards: cards.length};
}
"""


async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        ctx = await b.new_context(viewport=capture.VIEWPORT,
                                  user_agent=capture.USER_AGENT,
                                  ignore_https_errors=True)
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        await page.add_style_tag(content=capture.HIDE_CSS)
        await page.wait_for_timeout(2500)
        await page.evaluate(capture.AUTO_SCROLL_JS)
        await page.wait_for_timeout(2500)
        # count what the capture counts: consent/Qualtrics furniture is
        # deliberately removed from the groundtruth, so drop it here too
        try:
            await page.evaluate(
                f"document.querySelectorAll('{capture.BANNER_SELECTORS}')"
                ".forEach(e => e.remove())")
        except Exception:
            pass

        docs_total, docs_dates = 0, 0
        for _ in range(40):
            part = await page.evaluate(PROBE_JS)
            docs_total += len(part["doc_rows"])
            docs_dates += part["doc_dates"]
            nxt = await page.query_selector(
                "#documents .pagination .right-page:not(.inactive), "
                ".documents_ifx .pagination .right-page:not(.inactive)")
            if nxt is None:
                break
            await nxt.evaluate("el => el.click()")
            await page.wait_for_timeout(900)
        base = await page.evaluate(PROBE_JS)

        groups = {}
        for tab in base["ds_tabs"]:
            if tab["id"].endswith("overview"):
                continue
            btn = await page.query_selector("#" + tab["id"])
            if btn is None:
                continue
            try:
                await btn.scroll_into_view_if_needed(timeout=1500)
                await btn.evaluate("el => el.click()")
                await page.wait_for_timeout(700)
            except Exception:
                continue
            # Rewind to page 1 - defensive only. Switching tab does reset the
            # pager (the left arrow is already inactive here, so this loop
            # normally takes zero steps). It looked otherwise while the walk
            # still paginated the Overview union, which left the pager
            # advanced; that no longer happens. Kept because a walk that
            # silently starts on page 3 is expensive to notice.
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
            total = 0
            for _ in range(40):
                info = await page.evaluate(PANE_COUNT_JS)
                if info:
                    total += info["cards"]
                nxt = None
                for arrow in await page.query_selector_all(
                        ".designresource-component .pagination "
                        ".right-page-ds:not(.inactive)"):
                    if await arrow.is_visible():
                        nxt = arrow
                        break
                if nxt is None:
                    break
                await nxt.evaluate("el => el.click()")
                await page.wait_for_timeout(700)
            groups[tab["label"]] = total
        await b.close()
        print(json.dumps({
            "docs": docs_total, "doc_dates": docs_dates, "groups": groups,
            "features_li": base["features_li"], "news_cards": base["news_cards"],
            "has_overview": base["has_overview"],
            "has_applications": bool(base["applications"]),
            "images": len(base["images"]), "imgs_with_alt": base["imgs_with_alt"],
            "title": base["title"], "feedback_visible": base["feedback_visible"],
        }))


asyncio.run(asyncio.wait_for(main(), timeout=280))
