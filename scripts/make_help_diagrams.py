"""Render the Help diagrams to PNG for the Confluence page.

The diagrams are Jinja macros in iradar/templates/_help_diagrams.html, inlined
into the in-app Help page so they follow its light/dark colours. Confluence
cannot use that copy, so this renders the SAME macros and shoots each to a PNG.

One source, two outputs - the wiki images are derived and never edited by hand.

    python scripts/make_help_diagrams.py

Note the bar diagram is Confluence-only: the Help page draws that one as a live
CSS replica instead, which cannot go stale when the palette moves.
"""
import asyncio
import http.server
import socketserver
import tempfile
import threading
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "iradar" / "templates"
OUT = ROOT / "docs" / "confluence" / "images"

SHOTS = [
    ("d1", "flow", "09-page-through-a-check.png"),
    ("d2", "vote", "08-the-vote.png"),
    ("d3", "bar", "10-the-bar.png"),
]

PAGE = """<meta charset="utf-8">
<style>body{margin:0;background:#fff;
  font-family:"Segoe UI",system-ui,sans-serif}
 .sheet{padding:24px}</style>
<div class="sheet">%s</div>"""


def build_html():
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    dia = env.get_template("_help_diagrams.html").module
    body = [str(dia.style())]
    for _, macro, _ in SHOTS:
        body.append(str(getattr(dia, macro)()))
    return PAGE % "\n".join(body)


def serve(directory):
    """Port 0 lets the OS pick a free one - a fixed port meant a leftover
    server from an earlier run took the address and this died on startup."""
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(
        *a, directory=directory, **k)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "d.html").write_text(build_html(), encoding="utf-8")
        httpd, port = serve(tmp)
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                # 2x so the image stays sharp when Confluence scales it
                page = await browser.new_page(
                    viewport={"width": 1000, "height": 900},
                    device_scale_factor=2)
                await page.goto("http://127.0.0.1:%d/d.html" % port)
                await page.wait_for_timeout(400)
                for ident, _, name in SHOTS:
                    el = await page.query_selector("#" + ident)
                    if el is None:
                        print("MISSING #%s - skipped" % ident)
                        continue
                    await el.screenshot(path=str(OUT / name))
                    print("wrote", name)
                await browser.close()
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
