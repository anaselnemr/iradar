#!/usr/bin/env python
"""Capture frozen HTML fixtures for the offline extractor tests.

Run ONCE (or to refresh) when the machine is free:
    .venv/Scripts/python.exe tests/capture_fixtures.py

For each page it loads the real site, runs the same interactive-expansion the
capture does, and freezes the rendered DOM to tests/fixtures/<name>.html plus
the CURRENT extractor output to <name>.golden.json. test_extractors.py then
runs the extractors against the frozen DOM offline - no network - so a change
to the extractor can be checked in seconds.

Keep it small: a handful of pages, one per shape (home, product family, part,
application, design resource) is enough to exercise the header language
switcher, a carousel, the products table, and the image filters.
"""
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import capture as C                                   # noqa: E402
from playwright.async_api import async_playwright     # noqa: E402

FIX = HERE / "fixtures"

PAGES = {
    "home":        "https://www.infineon.com/",
    "product_fam": "https://www.infineon.com/products/microcontroller/32-bit-psoc-arm-cortex",
    "part":        "https://www.infineon.com/part/XMC1100-Q024F0008-AB",
    "application": "https://www.infineon.com/applications/consumer-electronics",
    "design_res":  "https://www.infineon.com/design-resources/development-tools",
}


async def one(browser, name, url):
    ctx = await browser.new_context()
    pg = await ctx.new_page()
    try:
        await pg.goto(url, wait_until="networkidle", timeout=90000)
        await pg.wait_for_timeout(2500)
        await C._expand_interactive(pg)
        html = await pg.evaluate("() => document.documentElement.outerHTML")
        text = await pg.evaluate(C.TEXT_JS)
        images = await pg.evaluate(C.IMAGES_JS)
        (FIX / f"{name}.html").write_text(html, encoding="utf-8")
        (FIX / f"{name}.golden.json").write_text(json.dumps({
            "url": url,
            "text_lines": len(text.split("\n")),
            "images": sorted(images),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  {name:<12} {len(html):>8} bytes html, "
              f"{len(text.split(chr(10)))} text lines, {len(images)} images")
    finally:
        await pg.close()
        await ctx.close()


async def main():
    FIX.mkdir(exist_ok=True)
    only = sys.argv[1:] or list(PAGES)
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        for name in only:
            if name not in PAGES:
                print(f"  ? unknown page {name}")
                continue
            try:
                await one(b, name, PAGES[name])
            except Exception as e:
                print(f"  {name}: FAILED {type(e).__name__}: {e}")
        await b.close()
    print("done — fixtures in", FIX)


if __name__ == "__main__":
    asyncio.run(main())
