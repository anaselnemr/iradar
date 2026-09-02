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
