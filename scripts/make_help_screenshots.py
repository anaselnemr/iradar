"""Shoot the Help/wiki screenshots off demo data, including a LIVE run bar.

The outcome bar only exists while a check is running, so a real screenshot of
it cannot be taken between runs - which is why the Help page draws a replica.
For the wiki, which cannot run CSS replicas, this fakes a run instead: it
stands the app up on invented data, sets RUN_STATE to a believable mid-run
position, and lets the dashboard's own JavaScript paint its own bar. What is
photographed is the real component with real colours, not a drawing of it.

    python scripts/make_help_screenshots.py

Nothing here touches the network or any real database: the data is the same
made-up set preview.py seeds, in a temp folder that is deleted afterwards, and
preview.py's guards against starting a capture are kept.

Sign-in is done by handing Playwright a session cookie signed with the app's
own key - the login form is never used and no password is typed.
"""
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "iradar"))
sys.path.insert(0, str(ROOT / "scripts"))

OUT = ROOT / "docs" / "confluence" / "images"

# A believable moment inside a nine-hour run: most of it settled, a healthy
# holding pen still waiting for its second capture, a few blocked pages that
# have not yet had their retries.
RUN_AT = dict(
    running=True, paused=False, stopping=False, kind="scheduled",
    total=4266, pages_total=4266, done=3180, done0=0,
    saved=2946, saved0=0, changed=41, errors=18, suspects=263, held=263,
    message="https://www.infineon.com/application/ev-charging",
    phase="capture", round=0, phase_total=4266, phase_done=3180,
)

SHOTS = [("light", "11-the-bar-live.png"), ("dark", "12-the-bar-live-dark.png")]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def stand_up(work: Path):
    """preview.py's own setup, reused so the demo data and the no-network
    guards are identical rather than a second copy that drifts."""
    import preview as pv
    import db

    db.DATA_DIR = work
    db.SNAP_DIR = work / "snapshots"
    db.CHANGE_DIR = work / "changes"
    db.DB_PATH = work / "watch.db"
    db.init_db()

    con = db.connect()
    try:
        pv._seed(con)
        con.commit()
    finally:
        con.close()

    import app
    import auth
    import runner
    import asset_watch

    # the same guards preview.py installs: no button here can start a capture
    runner.run_check_background = lambda *a, **k: False
    asset_watch.run_in_background = lambda *a, **k: False

    uid, _ = auth.create_user(pv.DEMO_EMAIL, "Preview User", "admin")
    auth.set_password(uid, pv.DEMO_PW, must_change=False)
    runner.RUN_STATE.update(RUN_AT)
    return app, auth, uid


def session_cookie(app, auth, uid):
    """A session the harness signs itself - no login form, no password.

    Returns (name, value): the app sets SESSION_COOKIE_NAME to iradar_session,
    and a cookie under Flask's default name is silently ignored.
    """
    from flask.sessions import SecureCookieSessionInterface
    ser = SecureCookieSessionInterface().get_signing_serializer(app.app)
    user = auth.get_user(uid)
    name = app.app.config.get("SESSION_COOKIE_NAME") or "session"
    return name, ser.dumps({"uid": uid, "pwfp": auth.pw_fingerprint(user)})


async def shoot(port, cookie):
    from playwright.async_api import async_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900},
                                        device_scale_factor=2)
        # the app renames the cookie, so read the name rather than assume
        # Flask's default - assuming it meant landing on the login page
        await ctx.add_cookies([{
            "name": cookie[0], "value": cookie[1],
            "domain": "127.0.0.1", "path": "/"}])
        page = await ctx.new_page()
        await page.goto("http://127.0.0.1:%d/" % port)
        # the bar is painted by the dashboard's own poller against /status
        await page.wait_for_selector("#rp-bar")
        await page.wait_for_timeout(2500)

        for theme, name in SHOTS:
            await page.evaluate(
                "t => document.documentElement.setAttribute('data-theme', t)",
                theme)
            await page.wait_for_timeout(350)
            # the whole run panel: bar, chips and the line under it
            # From the phase pills down to the chips - deliberately NOT the
            # whole card. The stat tiles above belong to the demo data (60
            # pages) while the faked run talks about 4,266, and a screenshot
            # showing both reads as a bug rather than as an illustration.
            box = await page.evaluate("""() => {
                const bar = document.getElementById('rp-bar');
                const pills = document.getElementById('step-retry');
                const chips = document.querySelector('.rp-chips');
                const msg = document.getElementById('run-msg');
                if (!bar || !pills) return null;
                const top = (pills.closest('div') || bar).getBoundingClientRect();
                const last = (msg && msg.getBoundingClientRect().height
                              ? msg : (chips || bar)).getBoundingClientRect();
                const left = bar.getBoundingClientRect();
                return {x: left.x - 14,
                        y: top.y - 12,
                        width: left.width + 28,
                        height: (last.bottom - top.y) + 24};
            }""")
            if not box:
                print("could not locate the run panel - skipped", name)
                continue
            await page.screenshot(path=str(OUT / name), clip=box)
            print("wrote", name)
        await browser.close()


def main():
    import asyncio
    work = Path(tempfile.mkdtemp(prefix="iradar_shots_"))
    try:
        app, auth, uid = stand_up(work)
        port = free_port()
        threading.Thread(
            target=lambda: app.app.run(host="127.0.0.1", port=port,
                                       debug=False, use_reloader=False),
            daemon=True).start()
        time.sleep(2.5)
        asyncio.run(shoot(port, session_cookie(app, auth, uid)))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
