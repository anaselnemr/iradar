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
sys.path.insert(0, str(ROOT))
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
