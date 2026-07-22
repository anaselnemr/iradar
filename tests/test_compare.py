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


# --- screenshot-revision boundary (the feed-hidden re-baseline) -------------

def _snap_shot(lines, fill, text_rev=3, shot_rev=None, size=(300, 500)):
    """A snapshot with a solid-colour shot.png and rev stamps."""
    from PIL import Image
    d = Path(_snap(lines, []))
    Image.new("RGB", size, fill).save(d / "shot.png")
    meta = {"text_rev": text_rev}
    if shot_rev is not None:
        meta["shot_rev"] = shot_rev
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return str(d)


def test_visual_suppressed_across_shot_rev_change():
    # old shot = full page (rev 1), new shot = feed-hidden (rev 2): the pixels
    # differ everywhere the feeds used to be, but that must NOT flag - it is the
    # same re-normalisation as the text rev, one row over
    r = cmp.compare_snapshots(
        _snap_shot(["Home"], "#ffffff", shot_rev=1),
        _snap_shot(["Home"], "#202020", shot_rev=2),   # wildly different pixels
        change_dir=None, settings={})
    assert r["visual_changed"] is False, "visual not suppressed across shot_rev"
    assert r["pixel_ratio"] == 0 and r["height_delta"] == 0
    assert r["any_changed"] is False


def test_visual_active_within_same_shot_rev():
    # both on the new shot_rev -> normal pixel diffing resumes and a real
    # visual change flags
    r = cmp.compare_snapshots(
        _snap_shot(["Home"], "#ffffff", shot_rev=2),
        _snap_shot(["Home"], "#000000", shot_rev=2),
        change_dir=None, settings={})
    assert r["visual_changed"] is True, "same-rev visual change must still flag"


def test_text_still_fires_across_shot_rev_boundary():
    # shot_rev gate suppresses the PICTURE only - a real text edit within the
    # same text_rev must survive a screenshot-rev change
    r = cmp.compare_snapshots(
        _snap_shot(["Home", "Old copy"], "#ffffff", text_rev=3, shot_rev=1),
        _snap_shot(["Home", "New copy"], "#ffffff", text_rev=3, shot_rev=2),
        change_dir=None, settings={})
    assert r["text_changed"] is True, "text edit lost across shot_rev boundary"


# --- image-collector revision boundary (the feed-free inventory) ------------

def _snap_img_rev(lines, images, img_rev):
    d = Path(_snap(lines, images))
    (d / "meta.json").write_text(json.dumps({"text_rev": 3, "img_rev": img_rev}),
                                 encoding="utf-8")
    return str(d)


def test_images_suppressed_across_img_rev_change():
    # rev-1 inventory had the news thumbnail; rev-2 dropped it - the removal
    # must NOT flag (one retired press image flagged 585 pages in run 127)
    r = cmp.compare_snapshots(
        _snap_img_rev(["Home"], ["https://a/hero.png", "https://a/news-thumb.png"], 1),
        _snap_img_rev(["Home"], ["https://a/hero.png"], 2),
        change_dir=None, settings={})
    assert r["images_changed"] is False, "images not suppressed across img_rev"
    assert r["any_changed"] is False


def test_images_active_within_same_img_rev():
    r = cmp.compare_snapshots(
        _snap_img_rev(["Home"], ["https://a/hero.png"], 2),
        _snap_img_rev(["Home"], ["https://a/hero2.png"], 2),
        change_dir=None, settings={})
    assert r["images_changed"] is True, "same-rev image change must still flag"


def test_text_still_fires_across_img_rev_boundary():
    r = cmp.compare_snapshots(
        _snap_img_rev(["Home", "Old"], ["https://a/1.png"], 1),
        _snap_img_rev(["Home", "New"], ["https://a/1.png"], 2),
        change_dir=None, settings={})
    assert r["text_changed"] is True, "text edit lost across img_rev boundary"


# --- robustness ------------------------------------------------------------

def test_truncated_screenshot_does_not_crash_the_comparison():
    # a capture killed mid-write leaves a truncated shot.png; the visual
    # signal must degrade to nothing instead of throwing and taking the
    # text/image/library signals down with it (found by the mutation battery)
    from PIL import Image
    old = _snap(["a", "b"], [])
    new = _snap(["a", "CHANGED"], [])
    Image.new("RGB", (300, 400), "#fff").save(Path(old) / "shot.png")
    Image.new("RGB", (300, 400), "#fff").save(Path(new) / "shot.png")
    good = (Path(new) / "shot.png").read_bytes()
    (Path(new) / "shot.png").write_bytes(good[: len(good) // 3])
    r = cmp.compare_snapshots(old, new, change_dir=None, settings={})
    assert r["visual_changed"] is False and r["pixel_ratio"] == 0
    assert r["text_changed"] is True, "other signals must survive a bad shot"


# --- library_diff_detail: the view derives from the detector's rules --------
# Change #5584 was counted 'Documents (~1)' by library_diff while the page
# showed nothing: the view had a PARALLEL diff (added/removed only, raw title
# keys). detail() now shares _item_id/_item_body, so count>0 <=> detail shown.

def _sects(sections):
    d = Path(tempfile.mkdtemp(prefix="ifxtest_"))
    (d / "sections.json").write_text(json.dumps(sections), encoding="utf-8")
    (d / "text.txt").write_text("", encoding="utf-8")
    (d / "images.json").write_text("[]", encoding="utf-8")
    return str(d)


def test_detail_shows_modified_in_place_item():
    # the 5584 case: same title+url, meta/dtype casing changed
    old = _sects({"documents": [{"title": "PQR-XMC4100", "url": "u1",
                                 "meta": "Product qualification report | 02_00",
                                 "dtype": "Product qualification report"}]})
    new = _sects({"documents": [{"title": "PQR-XMC4100", "url": "u1",
                                 "meta": "Product Qualification Report | 02_00",
                                 "dtype": "Product Qualification Report"}]})
    det = cmp.library_diff_detail(old, new)
    assert "documents" in det, "modified item invisible to the view again"
    ch = det["documents"]["changed"]
    assert len(ch) == 1 and ch[0]["title"] == "PQR-XMC4100"
    fields = {f for f, _, _ in ch[0]["fields"]}
    assert "meta" in fields and "dtype" in fields


def test_detail_and_count_agree_on_every_shape():
    # the invariant: whatever the detector counts, the detail shows - and
    # whatever the detail shows, the detector counted. Exercised over every
    # tricky shape: casing-only retitle (detector-silent), lazy poster
    # (silent), img both-present (real), price string, stock tick (exempt),
    # an unlabeled category (about), and a plain add/remove.
    cases = [
        # (old sections, new sections)
        ({"documents": [{"title": "Datasheet", "url": "u", "meta": "m"}]},
         {"documents": [{"title": "DATASHEET", "url": "u", "meta": "m"}]}),
        ({"videos": [{"title": "Tour", "url": "v", "img": "poster.png"}]},
         {"videos": [{"title": "Tour", "url": "v", "img": ""}]}),
        ({"videos": [{"title": "Tour", "url": "v", "img": "a.png"}]},
         {"videos": [{"title": "Tour", "url": "v", "img": "b.png"}]}),
        ({"price": "EUR 12 | 260 in stock"}, {"price": "EUR 12 | 259 in stock"}),
        ({"price": ""}, {"price": "EUR 249.00"}),
        ({"about": [{"title": "Intro", "url": "", "meta": "old words"}]},
         {"about": [{"title": "Intro", "url": "", "meta": "new words"}]}),
        ({"documents": [{"title": "A", "url": "1"}]},
         {"documents": [{"title": "A", "url": "1"}, {"title": "B", "url": "2"}]}),
        ({"images": [{"title": "x", "url": "i1"}]},        # _NOT_CONTENT: both silent
         {"images": [{"title": "y", "url": "i2"}]}),
    ]
    for old_s, new_s in cases:
        old, new = _sects(old_s), _sects(new_s)
        counted = cmp.library_diff(old, new)["changed"]
        shown = bool(cmp.library_diff_detail(old, new))
        assert counted == shown, (
            f"view and detector disagree on {old_s} -> {new_s}: "
            f"counted={counted} shown={shown}")


# --- whole verdict ---------------------------------------------------------

def test_summary_reads_no_change_when_nothing_moved():
    r = _cmp(["a"], ["a"])
    assert r["summary"] == "no change"
