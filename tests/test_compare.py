"""Tests for compare.py — the diff logic that decides whether a page updated.

Several of this session's worst bugs lived here: the positional line diff
calling a re-ordered block an update, duplication being mistaken for a move,
ignore-patterns. Each case below is a real regression guard, built from tiny
snapshot dirs written to a temp folder — no browser, no network, no database.
"""
import json
import tempfile
from pathlib import Path

import compare as cmp


# --- helpers ---------------------------------------------------------------

def _snap(lines=None, images=None):
    """A throwaway snapshot dir with text.txt and images.json."""
    d = Path(tempfile.mkdtemp(prefix="ifxtest_"))
    (d / "text.txt").write_text("\n".join(lines or []), encoding="utf-8")
    (d / "images.json").write_text(json.dumps(images or []), encoding="utf-8")
    return str(d)


def _cmp(old_lines, new_lines, old_imgs=None, new_imgs=None, settings=None):
    return cmp.compare_snapshots(
        _snap(old_lines, old_imgs), _snap(new_lines, new_imgs),
        change_dir=None, settings=settings or {})


def _snap_rev(lines=None, images=None, rev=None):
    """A snapshot whose meta.json carries a text_rev (or none, = rev 1)."""
    d = Path(_snap(lines, images))
    if rev is not None:
        (Path(d) / "meta.json").write_text(json.dumps({"text_rev": rev}),
                                           encoding="utf-8")
    return str(d)


# --- text diff -------------------------------------------------------------

def test_text_diff_counts_adds_and_removes():
    ch, added, removed, _ = cmp.text_diff(
        _snap(["a", "b", "c"]), _snap(["a", "c", "d"]))
    assert ch is True
    assert removed == 1 and added == 1, (added, removed)


def test_identical_text_is_no_change():
    r = _cmp(["a", "b", "c"], ["a", "b", "c"])
    assert r["any_changed"] is False
    assert r["text_changed"] is False


def test_ignore_patterns_drop_matching_lines():
    # a timestamp line changing must NOT count as an update
    pats = [r"^\d+ (day|hour|minute)s? ago$"]
    r = _cmp(["Overview", "2 hours ago"], ["Overview", "5 hours ago"],
             settings={"ignore_patterns": "\n".join(pats)})
    assert r["text_changed"] is False, "ignore-pattern line still flagged"


# --- the reorder classifier (the AURIX / expansion-order bug) --------------

def test_pure_reorder_is_not_an_update():
    # same lines, different order -> reordered, not changed
    old = ["View product", "TC489", "View product", "TC487"]
    new = ["View product", "TC487", "View product", "TC489"]
    r = _cmp(old, new)
    assert r["text_reordered"] is True, "reorder not detected"
    assert r["text_changed"] is False, "reorder wrongly counted as an update"
    assert r["any_changed"] is False, "reorder wrongly flagged the page"


def test_duplication_is_not_a_reorder():
    # a block rendered twice changes the multiset, so it must still flag -
    # otherwise the filter-dropdown / mega-menu duplication would hide
    old = ["Applications", "Automotive", "Motor control"]
    new = ["Applications", "Automotive", "Automotive", "Motor control"]
    r = _cmp(old, new)
    assert r["text_reordered"] is False, "duplication mistaken for a move"
    assert r["text_changed"] is True, "duplication (an added line) not flagged"


def test_real_add_still_flags():
    r = _cmp(["a", "b"], ["a", "b", "brand new line"])
    assert r["text_changed"] is True
    assert r["text_reordered"] is False
    assert r["lines_added"] == 1 and r["lines_removed"] == 0


def test_real_removal_still_flags():
    r = _cmp(["a", "b", "c"], ["a", "c"])
    assert r["text_changed"] is True
    assert r["lines_removed"] == 1


# --- image diff (URL set) --------------------------------------------------

def test_image_added_and_removed():
    r = _cmp(["x"], ["x"],
             old_imgs=["https://a/1.png", "https://a/2.png"],
             new_imgs=["https://a/2.png", "https://a/3.png"])
    assert r["images_changed"] is True
    assert r["images_added"] == 1 and r["images_removed"] == 1


def test_same_images_no_change():
    imgs = ["https://a/1.png", "https://a/2.png"]
    r = _cmp(["x"], ["x"], old_imgs=imgs, new_imgs=list(reversed(imgs)))
    assert r["images_changed"] is False, "image set order should not matter"


def test_ignore_image_patterns():
    r = _cmp(["x"], ["x"],
             old_imgs=["https://a/hero.png"],
             new_imgs=["https://a/hero.png", "https://a/etc.clientlibs/x.png"],
             settings={"ignore_image_patterns": r"/etc\.clientlibs/"})
    assert r["images_changed"] is False, "ignored image path still flagged"


# --- library diff ----------------------------------------------------------

def test_library_add_is_a_change():
    old = str(_mk_sections({"documents": [{"title": "Datasheet", "url": "u1"}]}))
    new = str(_mk_sections({"documents": [
        {"title": "Datasheet", "url": "u1"}, {"title": "App Note", "url": "u2"}]}))
    lib = cmp.library_diff(old, new)
    assert lib["changed"] is True and lib["added"] >= 1


def _mk_sections(sections):
    d = Path(tempfile.mkdtemp(prefix="ifxtest_"))
    (d / "sections.json").write_text(json.dumps(sections), encoding="utf-8")
    (d / "text.txt").write_text("", encoding="utf-8")
    (d / "images.json").write_text("[]", encoding="utf-8")
    return d


# --- text-extractor revision boundary (the silent re-baseline) -------------

def test_text_diff_suppressed_across_rev_change():
    # baseline on the old walk (rev 1), new capture on the new walk (rev 2):
    # the text is not comparable, so even a big line diff must NOT flag - this
    # is what stops 4,000 pages "updating" the run after a TEXT_JS change
    old = cmp.compare_snapshots(
        _snap_rev(["Home", "Forum thread A", "Member001"], rev=1),
        _snap_rev(["Home"], rev=2), change_dir=None, settings={})
    assert old["text_changed"] is False, "text diff not suppressed across revs"
    assert old["any_changed"] is False, "re-normalised page wrongly flagged"


def test_text_diff_active_within_same_rev():
    # both sides on the new rev -> normal diffing resumes and a real edit flags
    r = cmp.compare_snapshots(
        _snap_rev(["Home", "Old copy"], rev=2),
        _snap_rev(["Home", "New copy"], rev=2), change_dir=None, settings={})
    assert r["text_changed"] is True, "same-rev text edit must still flag"


def test_missing_meta_counts_as_rev1():
    # a legacy snapshot (no meta.json) is rev 1; against a rev-2 capture the
    # text is still cross-rev and suppressed
    r = cmp.compare_snapshots(
        _snap(["A", "B", "C"]),                       # no meta.json -> rev 1
        _snap_rev(["A"], rev=2), change_dir=None, settings={})
    assert r["text_changed"] is False


def test_non_text_signal_still_fires_across_rev():
    # the rev boundary suppresses TEXT ONLY. A genuine change that lands during
    # the re-baseline - here a swapped image - must still be caught, or the
    # re-baseline run would be a blind spot.
    r = cmp.compare_snapshots(
        _snap_rev(["Home"], images=["https://a/1.png"], rev=1),
        _snap_rev(["Home"], images=["https://a/2.png"], rev=2),
        change_dir=None, settings={})
    assert r["images_changed"] is True, "image change lost across rev boundary"
    assert r["any_changed"] is True, "real change hidden during re-baseline"


# --- whole verdict ---------------------------------------------------------

def test_summary_reads_no_change_when_nothing_moved():
    r = _cmp(["a"], ["a"])
    assert r["summary"] == "no change"
