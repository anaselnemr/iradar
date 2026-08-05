"""Camera for the AI QA reviewer: photographs a live page section by section.

Usage: python qa_shots.py <url> <outdir>
Saves numbered PNGs: page top, every documents-table page, every
design-resources tab (each of its pages), news section, full page.
Prints a manifest of what was shot.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "iradar"))
import capture  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

URL = sys.argv[1]
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def shot_el(page, selector, path):
    el = await page.query_selector(selector)
    if el is None:
        return False
    try:
        await el.scroll_into_view_if_needed(timeout=2000)
        await page.wait_for_timeout(400)
        await el.screenshot(path=str(path), timeout=15000)
        return True
    except Exception:
        return False


async def main():
    manifest = []
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
        await page.wait_for_timeout(2000)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(400)

        await page.screenshot(path=str(OUT / "00_top.png"))
        manifest.append("00_top.png: viewport at page top")

        # documents table, every page
        for i in range(8):
            ok = await shot_el(page, "#documents, .documents_ifx",
                               OUT / f"01_docs_p{i+1}.png")
            if ok:
                manifest.append(f"01_docs_p{i+1}.png: documents table page {i+1}")
            nxt = await page.query_selector(
                "#documents .pagination .right-page:not(.inactive), "
                ".documents_ifx .pagination .right-page:not(.inactive)")
            if nxt is None:
                break
            # DOM-level click, like the design-resources loop below: a
            # coordinate click throws "element is outside of the viewport" on
            # the documents pager and killed the whole walk at page 3, so a
            # reviewer never saw the later pages or any design-resources tab.
            await nxt.evaluate("el => el.click()")
            await page.wait_for_timeout(900)

        # every design-resources tab, every page of each
        for tb in await page.query_selector_all('[id^="ds-tab-"]'):
            tid = (await tb.get_attribute("id")).replace("ds-tab-", "")
            try:
                await tb.scroll_into_view_if_needed(timeout=1500)
                await tb.evaluate("el => el.click()")
                await page.wait_for_timeout(700)
            except Exception:
                continue
            for i in range(20):
                ok = await shot_el(page, ".designresource-component",
                                   OUT / f"02_ds_{tid}_p{i+1}.png")
                if ok:
                    manifest.append(
                        f"02_ds_{tid}_p{i+1}.png: design-resources tab"
                        f" '{tid}' page {i+1}")
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

        # news / highlights section
        found_news = await page.evaluate(r"""
          () => {
            for (const h of document.querySelectorAll('h2')) {
              if (/news|highlight/i.test(h.textContent)) {
                h.scrollIntoView(); return true;
              }
            }
            return false;
          }""")
        if found_news:
            await page.wait_for_timeout(600)
            await page.screenshot(path=str(OUT / "03_news.png"))
            manifest.append("03_news.png: viewport at news & highlights")

        await page.screenshot(path=str(OUT / "04_full.png"), full_page=True)
        manifest.append("04_full.png: full page")
        await b.close()

    print("SHOT MANIFEST for", URL)
    for m in manifest:
        print(" ", m)


asyncio.run(main())
