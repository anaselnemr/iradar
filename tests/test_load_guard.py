"""Tests for the section-load-failure guard (runner._load_failure_added).

Run 124 recorded five captures where a section's items were replaced by the
"Content could not be loaded" apology, each reading as a big "items removed"
update with a mirror "items returned" on the next run. The apology's presence
alone means nothing - the pricing box on most part pages shows it permanently -
so the guard keys on the sentence being ADDED relative to the baseline.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "iradar"))
import runner      # noqa: E402

APOLOGY = "Unfortunately, we were unable to load the content for this section."
APOLOGY2 = "Content could not be loaded"


def _snap(lines):
    d = Path(tempfile.mkdtemp(prefix="ifxguard_"))
    (d / "text.txt").write_text("\n".join(lines), encoding="utf-8")
    return str(d)


def test_apology_on_both_sides_is_invisible():
    # the part-page pricing box shows the apology permanently (~75% of
    # snapshots) - present in baseline AND new capture must never trip
    old = _snap(["Datasheet", APOLOGY2, APOLOGY, "SRF 55V02P HC"])
    new = _snap(["Datasheet", APOLOGY2, APOLOGY, "SRF 55V02P HC", "New line"])
    assert runner._load_failure_added(old, new) is False


def test_newly_added_apology_trips():
    # baseline had a rendered section; new capture answered with the apology
    old = _snap(["Documents", "Datasheet A", "Datasheet B", "Board X"])
    new = _snap(["Documents", APOLOGY2, APOLOGY])
    assert runner._load_failure_added(old, new) is True


def test_second_apology_still_trips():
    # pricing apology on both sides AND a section newly broke: count-aware,
    # the extra occurrence is an addition even though the sentence existed
    old = _snap([APOLOGY, "Documents", "Datasheet A"])
    new = _snap([APOLOGY, "Documents", APOLOGY])
    assert runner._load_failure_added(old, new) is True


def test_recovery_does_not_trip():
    # the apology LEAVING (section rendered again) is a genuine update to
    # review, not a broken render - must pass through to normal flagging
    old = _snap(["Documents", APOLOGY2, APOLOGY])
    new = _snap(["Documents", "Datasheet A", "Datasheet B"])
    assert runner._load_failure_added(old, new) is False


def test_missing_dirs_do_not_crash_or_trip():
    old = _snap(["a"])
    assert runner._load_failure_added(old, str(Path(old) / "nope")) is False


# --- what gets a same-run retry (the recapture queue) -----------------------

import capture  # noqa: E402


def test_transient_failure_classification():
    # ok captures are never retried - 404s included ('gone' is a recorded state)
    assert runner._transient_failure({"status": "ok", "error": ""}) is False
    assert runner._transient_failure({"status": "ok", "http_status": 404}) is False
    # blank is judged against what the page was doing before: one that JUST
    # went blank is a suspected bad render and takes the retries, one that was
    # already blank is a steady state and stays it
    blank_res = {"status": "error",
                 "error": capture.BLANK_RENDER + ": empty body"}
    assert runner._transient_failure(blank_res,
                                     {"last_status": "unchanged"}) is True
    assert runner._transient_failure(blank_res,
                                     {"last_status": "blank"}) is False
    # CDN blocks, timeouts, loading hiccups - the liftable failures - retry
    assert runner._transient_failure(
        {"status": "error", "error": capture.BLOCKED_TRANSIENT + ": HTTP 403"}) is True
    assert runner._transient_failure(
        {"status": "error", "error": "TimeoutError: Page.goto: 45000ms"}) is True
    assert runner._transient_failure({"status": "error", "error": ""}) is True
    # degenerate input never crashes
    assert runner._transient_failure({}) is True
    assert runner._transient_failure(None) is True


# --- the browser is closed whatever the batch does --------------------------
# Two production incidents, one cause: the close sat below the gather, so
# anything the batch raised jumped over it. The leaked Chromium filled the
# container's process table until it could not fork, and the run then reported
# itself running while doing nothing at all.

def test_a_page_that_cannot_be_written_fails_alone():
    """The volume filling must cost one page, not the whole run."""
    import asyncio, capture
    class Boom:
        async def new_context(self, **kw):
            raise OSError(28, "No space left on device")
    res = asyncio.run(capture.capture_one(Boom(), "https://x/y", _unwritable()))
    assert res["status"] == "error", "a disk error must be reported, not raised"
    assert "space" in res["error"].lower()


def _unwritable():
    import tempfile
    from pathlib import Path
    return str(Path(tempfile.mkdtemp()) / "sub")


def test_the_browser_is_closed_even_when_the_batch_explodes():
    import asyncio, capture
    closed = []

    class FakeBrowser:
        async def close(self): closed.append(True)

    class FakePW:
        chromium = None
        def __init__(self): self.chromium = self
        async def launch(self, **kw): return FakeBrowser()

    class FakeCtx:
        async def __aenter__(self): return FakePW()
        async def __aexit__(self, *a): return False

    orig_pw, orig_one, orig_self = (capture.async_playwright,
                                    capture.capture_one, capture._selftest)
    async def _boom(browser, url, out_dir):
        raise RuntimeError("the batch blew up")
    async def _noop(browser): pass
    capture.async_playwright = lambda: FakeCtx()
    capture.capture_one = _boom
    capture._selftest = _noop
    try:
        try:
            asyncio.run(capture.capture_all([("https://x/a", _unwritable())],
                                            concurrency=1))
        except RuntimeError:
            pass
        assert closed, "the browser was left running when the batch raised"
    finally:
        capture.async_playwright, capture.capture_one, capture._selftest = (
            orig_pw, orig_one, orig_self)


def _fake_playwright(log):
    """A playwright stand-in that records launches, closes and open tabs."""
    class FakeBrowser:
        def __init__(self, n):
            self.n = n
        async def close(self):
            log["closed"].append(self.n)

    class FakePW:
        def __init__(self):
            self.chromium = self
        async def launch(self, **kw):
            log["launched"] += 1
            return FakeBrowser(log["launched"])

    class FakeCtx:
        async def __aenter__(self):
            return FakePW()
        async def __aexit__(self, *a):
            return False
    return FakeCtx


def test_browser_restarts_roll_instead_of_waiting_for_the_batch():
    # 10 pages, a new browser every 3, 2 tabs. Browser 1's last page is slow:
    # under the old batches pages 4-6 waited for it; rolling, browser 2 opens
    # and works alongside while browser 1 finishes, then browser 1 closes.
    import asyncio, capture
    log = {"launched": 0, "closed": [], "tabs": 0, "max_tabs": 0,
           "overlap": False}

    async def _one(browser, url, out_dir):
        log["tabs"] += 1
        log["max_tabs"] = max(log["max_tabs"], log["tabs"])
        if browser.n > 1 and 1 not in log["closed"]:
            log["overlap"] = True          # browser 2 working while 1 is open
        await asyncio.sleep(0.3 if url.endswith("/3") else 0.01)
        log["tabs"] -= 1
        return {"url": url, "status": "ok"}

    async def _noop(browser):
        pass

    orig = (capture.async_playwright, capture.capture_one, capture._selftest,
            capture.PAGES_PER_BROWSER)
    capture.async_playwright = _fake_playwright(log)
    capture.capture_one, capture._selftest = _one, _noop
    capture.PAGES_PER_BROWSER = 3
    try:
        jobs = [(f"https://x/{i}", "d") for i in range(1, 11)]
        res = asyncio.run(capture.capture_all(jobs, concurrency=2))
    finally:
        (capture.async_playwright, capture.capture_one, capture._selftest,
         capture.PAGES_PER_BROWSER) = orig
    assert sorted(r["url"] for r in res) == sorted(u for u, _ in jobs)
    assert log["launched"] == 4, log
    assert sorted(log["closed"]) == [1, 2, 3, 4], "a browser was left running"
    assert log["max_tabs"] <= 2, "more tabs than the concurrency setting"
    assert log["overlap"], "the next browser waited for the slow page"


def test_pause_holds_pages_that_were_already_queued():
    # the whole run queues on one semaphore now: a page that passed the pause
    # check before Pause was pressed must still not start while paused
    import asyncio, capture
    log = {"launched": 0, "closed": [], "started": []}
    state = {"paused": False}

    async def _one(browser, url, out_dir):
        log["started"].append((url, state["paused"]))
        if url.endswith("/1"):
            state["paused"] = True         # Pause pressed mid-capture
            asyncio.get_running_loop().call_later(
                1.5, lambda: state.update(paused=False))
        await asyncio.sleep(0.01)
        return {"url": url, "status": "ok"}

    async def _noop(browser):
        pass

    orig = (capture.async_playwright, capture.capture_one, capture._selftest)
    capture.async_playwright = _fake_playwright(log)
    capture.capture_one, capture._selftest = _one, _noop
    try:
        res = asyncio.run(capture.capture_all(
            [(f"https://x/{i}", "d") for i in range(1, 5)], concurrency=1,
            pause_check=lambda: state["paused"]))
    finally:
        capture.async_playwright, capture.capture_one, capture._selftest = orig
    assert len(res) == 4
    assert not any(p for u, p in log["started"]), log["started"]


def test_only_a_404_at_its_own_address_counts_as_still_gone():
    import capture
    answers = {
        "https://x/gone": (404, "https://x/gone"),
        "https://x/gone410": (410, "https://x/gone410/"),
        "https://x/back": (200, "https://x/back"),
        "https://x/moved": (404, "https://x/elsewhere"),
        "https://x/blocked": (403, "https://x/blocked"),
        "https://x/timeout": (None, ""),
    }
    orig = capture._knock
    capture._knock = lambda u, timeout: answers[u]
    try:
        assert capture.still_gone(list(answers)) == {
            "https://x/gone", "https://x/gone410"}
        assert capture.still_gone([]) == set()
    finally:
        capture._knock = orig
