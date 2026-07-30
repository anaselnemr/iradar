"""The compare mutation battery, folded in as a permanent test.

The one-off lab version ran 56 mutations of real captured snapshots through
the production comparison: 39/39 real edits caught, 10/10 noise cases silent,
0 crashes. This is the same battery rebuilt on small synthetic snapshots so it
runs in seconds, forever, with no real data or browser - every class of edit
the system must catch, every trap it must ignore, and the settle rule that a
mutant accepted as baseline compares clean against itself next run.

Pure Python + PIL. No network, no Playwright, no database.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "iradar"))
import compare as cmp      # noqa: E402

# --- one synthetic but realistic snapshot -----------------------------------

TEXT = ["Heading %d and some body copy line %d" % (i, i) for i in range(60)] \
     + ["PSOC(tm) control line", "Price row | 12.40 | pcs",
        "Features overview", "32 MHz core / 64 MHz peripheral clock"]
IMAGES = ["https://assets.infineon.com/is/image/infineon/asset-%02d" % i
          for i in range(12)]
SECTIONS = {
    "videos": [
        {"title": "Product tour", "url": "https://a/is/content/tour",
         "meta": "2:39", "img": "https://a/is/image/tour"},
        {"title": "Teardown", "url": "https://a/is/content/teardown",
         "meta": "7:02", "img": "https://a/is/image/teardown"},
    ],
    "documents": [
        {"title": "Datasheet", "url": "https://a/d/ds.pdf",
         "meta": "PDF | EN | 01_00", "dtype": "Data Sheet"},
        {"title": "App note", "url": "https://a/d/an.pdf",
         "meta": "PDF | EN | 03_00", "dtype": "Application Notes"},
        {"title": "Errata", "url": "https://a/d/err.pdf",
         "meta": "PDF | EN | 01_01", "dtype": "Errata"},
    ],
    "subheadline": [{"title": "The original subheadline", "url": "", "meta": ""}],
    "trainings": [],
    "price": "",
}


def _shot(path, mark=None, height=900):
    im = Image.new("RGB", (400, height), (250, 250, 252))
    d = ImageDraw.Draw(im)
    for y in range(60, height - 60, 120):           # texture so diffs are real
        d.rectangle([40, y, 360, y + 60], fill=(70, 90, 140))
    if mark:
        d.rectangle(mark, fill=(200, 40, 40))
    im.save(path)


def _base():
    d = Path(tempfile.mkdtemp(prefix="ifxbat_"))
    (d / "text.txt").write_text("\n".join(TEXT), encoding="utf-8")
    (d / "images.json").write_text(json.dumps(IMAGES), encoding="utf-8")
    (d / "sections.json").write_text(json.dumps(SECTIONS), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"text_rev": 3}), encoding="utf-8")
    _shot(d / "shot.png")
    return d


def _mutant(mutate):
    """Copy of a fresh base with one mutation applied. Returns (old, new)."""
    old, new = _base(), Path(tempfile.mkdtemp(prefix="ifxbat_"))
    shutil.rmtree(new)
    shutil.copytree(old, new)
    mutate(new)
    return str(old), str(new)


def _cmp(old, new, settings=None):
    return cmp.compare_snapshots(old, new, change_dir=None,
                                 settings=settings or {})


def _edit_text(d, fn):
    lines = (Path(d) / "text.txt").read_text(encoding="utf-8").splitlines()
    (Path(d) / "text.txt").write_text("\n".join(fn(lines)), encoding="utf-8")


def _edit_sections(d, fn):
    s = json.loads((Path(d) / "sections.json").read_text(encoding="utf-8"))
    fn(s)
    (Path(d) / "sections.json").write_text(json.dumps(s), encoding="utf-8")


def _edit_images(d, fn):
    v = json.loads((Path(d) / "images.json").read_text(encoding="utf-8"))
    (Path(d) / "images.json").write_text(json.dumps(fn(v)), encoding="utf-8")


# ---------------------------------------------------------------- text ------

def test_one_character_edit_flags():
    old, new = _mutant(lambda d: _edit_text(
        d, lambda L: L[:30] + [L[30].replace("body", "bodx")] + L[31:]))
    r = _cmp(old, new)
    assert r["text_changed"] and r["any_changed"]


def test_added_and_removed_lines_flag():
    old, new = _mutant(lambda d: _edit_text(d, lambda L: L + ["Brand new row"]))
    assert _cmp(old, new)["text_changed"]
    old, new = _mutant(lambda d: _edit_text(d, lambda L: L[:-1]))
    assert _cmp(old, new)["text_changed"]


def test_pure_reorder_stays_silent_but_reorder_plus_edit_flags():
    def swap(L):
        L = L[:]
        L[10], L[11] = L[11], L[10]
        return L
    old, new = _mutant(lambda d: _edit_text(d, swap))
    r = _cmp(old, new)
    assert r["text_reordered"] and not r["text_changed"] and not r["any_changed"]

    def swap_and_edit(L):
        L = swap(L)
        L[20] = L[20] + " EDITED"
        return L
    old, new = _mutant(lambda d: _edit_text(d, swap_and_edit))
    r = _cmp(old, new)
    assert r["text_changed"] and not r["text_reordered"]


def test_duplicated_line_flags():
    old, new = _mutant(lambda d: _edit_text(d, lambda L: L[:15] + [L[15]] + L[15:]))
    r = _cmp(old, new)
    assert r["text_changed"] and not r["text_reordered"]


def test_emptied_text_flags():
    old, new = _mutant(lambda d: (Path(d) / "text.txt").write_text("", encoding="utf-8"))
    assert _cmp(old, new)["text_changed"]


# ---------------------------------------------------------------- rev gate --

def test_rev_boundary_suppresses_text_but_not_images():
    def m(d):
        _edit_text(d, lambda L: L[:-5])
        _edit_images(d, lambda v: v[:-1])
        (Path(d) / "meta.json").write_text(json.dumps({"text_rev": 4}),
                                           encoding="utf-8")
    old, new = _mutant(m)
    r = _cmp(old, new)
    assert not r["text_changed"], "cross-rev text must re-baseline silently"
    assert r["images_changed"], "non-text signals must survive the boundary"


# ---------------------------------------------------------------- images ----

def test_image_add_remove_swap_flag_and_reorder_does_not():
    old, new = _mutant(lambda d: _edit_images(d, lambda v: v + ["https://assets.infineon.com/is/image/infineon/new-one"]))
    assert _cmp(old, new)["images_changed"]
    old, new = _mutant(lambda d: _edit_images(d, lambda v: v[:-1]))
    assert _cmp(old, new)["images_changed"]
    old, new = _mutant(lambda d: _edit_images(d, lambda v: list(reversed(v))))
    assert not _cmp(old, new)["images_changed"]


def test_ignored_image_pattern_stays_silent():
    old, new = _mutant(lambda d: _edit_images(
        d, lambda v: v + ["https://www.infineon.com/etc.clientlibs/x.png"]))
    r = _cmp(old, new, settings={"ignore_image_patterns": r"/etc\.clientlibs/"})
    assert not r["images_changed"]


# ---------------------------------------------------------------- visual ----

def test_visible_patch_flags_and_tiny_patch_respects_threshold():
    old, new = _mutant(lambda d: _shot(Path(d) / "shot.png", mark=[100, 100, 180, 160]))
    r = _cmp(old, new)                       # 80x60 = 4800px of 360k = 1.3%
    assert r["visual_changed"], f"visible patch missed ({r['pixel_ratio']}%)"
    old, new = _mutant(lambda d: _shot(Path(d) / "shot.png", mark=[100, 100, 120, 115]))
    r = _cmp(old, new)                       # 20x15 = 300px = 0.08% < 0.5%
    assert not r["visual_changed"], "sub-threshold patch should not flag"


def test_height_change_thresholds():
    old, new = _mutant(lambda d: _shot(Path(d) / "shot.png", height=1000))
    assert _cmp(old, new)["visual_changed"], "100px growth must flag"
    old, new = _mutant(lambda d: _shot(Path(d) / "shot.png", height=940))
    assert not _cmp(old, new)["visual_changed"], "40px is below the floor"


def test_truncated_screenshot_degrades_not_crashes():
    def m(d):
        p = Path(d) / "shot.png"
        p.write_bytes(p.read_bytes()[:400])
        _edit_text(d, lambda L: L + ["still detected"])
    old, new = _mutant(m)
    r = _cmp(old, new)
    assert not r["visual_changed"] and r["text_changed"]


# ---------------------------------------------------------------- library ---

def test_video_lifecycle_is_caught():
    old, new = _mutant(lambda d: _edit_sections(d, lambda s: s["videos"].pop(0)))
    assert _cmp(old, new)["library"]["per_section"]["videos"]["removed"] == 1
    old, new = _mutant(lambda d: _edit_sections(
        d, lambda s: s["videos"].append({"title": "New video", "url": "https://a/is/content/new", "meta": "1:00", "img": ""})))
    assert _cmp(old, new)["library"]["per_section"]["videos"]["added"] == 1
    old, new = _mutant(lambda d: _edit_sections(
        d, lambda s: s["videos"][0].update(meta="3:05")))
    assert _cmp(old, new)["library"]["per_section"]["videos"]["changed"] == 1


def test_video_poster_lazy_rule():
    # poster dropped to empty = lazy load, silent; both present and different = real
    old, new = _mutant(lambda d: _edit_sections(
        d, lambda s: s["videos"][0].update(img="")))
    assert not _cmp(old, new)["library_changed"]
    old, new = _mutant(lambda d: _edit_sections(
        d, lambda s: s["videos"][0].update(img="https://a/is/image/tour-v2")))
    assert _cmp(old, new)["library_changed"]


def test_document_lifecycle_is_caught():
    old, new = _mutant(lambda d: _edit_sections(d, lambda s: s["documents"].pop(1)))
    assert _cmp(old, new)["library"]["per_section"]["documents"]["removed"] == 1
    old, new = _mutant(lambda d: _edit_sections(
        d, lambda s: s["documents"][0].update(meta="PDF | EN | 02_00")))
    assert _cmp(old, new)["library"]["per_section"]["documents"]["changed"] == 1
    old, new = _mutant(lambda d: _edit_sections(
        d, lambda s: s["documents"][0].update(title="Datasheet X")))
    per = _cmp(old, new)["library"]["per_section"]["documents"]
    assert per["added"] == 1 and per["removed"] == 1


def test_subheadline_and_price_are_caught_but_stock_is_exempt():
    old, new = _mutant(lambda d: _edit_sections(
        d, lambda s: s["subheadline"][0].update(title="A new subheadline")))
    assert "subheadline" in _cmp(old, new)["library"]["per_section"]
    old, new = _mutant(lambda d: _edit_sections(
        d, lambda s: s.update(price="EUR 249.00")))
    assert "price" in _cmp(old, new)["library"]["per_section"]
    # stock tick: same price string except the "N in stock" count
    def m(d):
        _edit_sections(d, lambda s: s.update(price="EUR 12.40 | 259 in stock"))
    old, new = _mutant(m)
    _edit_sections(Path(old), lambda s: s.update(price="EUR 12.40 | 260 in stock"))
    assert not _cmp(old, new)["library_changed"]


def test_corrupt_or_missing_sections_fail_loud_not_crash():
    def corrupt(d):
        p = Path(d) / "sections.json"
        p.write_text(p.read_text(encoding="utf-8")[:40], encoding="utf-8")
    old, new = _mutant(corrupt)
    r = _cmp(old, new)                       # parses to {} -> mass removals
    assert r["library_changed"]
    old, new = _mutant(lambda d: (Path(d) / "sections.json").unlink())
    assert _cmp(old, new)["library_changed"]


# ---------------------------------------------------------------- settle ----

def test_every_mutant_settles_clean_against_itself():
    # once accepted as baseline, the next identical capture must be silent -
    # including the nasty shapes (corrupt sections, truncated shot, empty text)
    mutants = []
    old, new = _mutant(lambda d: _edit_text(d, lambda L: L[:-3]))
    mutants.append(new)
    old, new = _mutant(lambda d: (Path(d) / "text.txt").write_text("", encoding="utf-8"))
    mutants.append(new)
    def corrupt(d):
        p = Path(d) / "sections.json"
        p.write_text("{broken", encoding="utf-8")
        q = Path(d) / "shot.png"
        q.write_bytes(q.read_bytes()[:300])
    old, new = _mutant(corrupt)
    mutants.append(new)
    for m in mutants:
        r = _cmp(m, m)
        assert not r["any_changed"], f"mutant still rings against itself: {r['summary']}"


def test_identical_copies_are_clean():
    old, new = _mutant(lambda d: None)
    assert not _cmp(old, new)["any_changed"]


def test_masking_the_feeds_never_invents_height():
    """#7534: two pictures of the SAME height, zero pixels different, reported
    as 120px taller. The masked list is an inventory of what was on screen and
    it flaps by itself - the price block was recorded once at 24px in one
    capture and three times at 28px in the next - so subtracting each side's
    total from equal heights manufactured growth on a page that had not moved.
    Discounting feeds may shrink the difference, never create one."""
    import json as _json

    def with_boxes(d, boxes, height=900):
        m = _json.loads((Path(d) / "meta.json").read_text(encoding="utf-8"))
        m["shot_ignore"] = boxes
        m["shot_scale"] = 0.5
        (Path(d) / "meta.json").write_text(_json.dumps(m), encoding="utf-8")
        _shot(Path(d) / "shot.png", height=height)
        return str(d)

    box = lambda y, h: {"x": 48, "y": y, "w": 624, "h": h}

    # same page height, a flapping inventory (one box vs three)
    old = with_boxes(_base(), [box(130, 24)])
    new = with_boxes(_base(), [box(130, 28), box(130, 28), box(130, 28)])
    ratio, dh, _n = cmp.visual_diff(old, new)
    assert dh == 0, f"identical heights can never be a height change (got {dh})"

    # a page that really did grow still reports it, feeds discounted
    old2 = with_boxes(_base(), [box(130, 100)], height=900)
    new2 = with_boxes(_base(), [box(130, 100)], height=1000)
    _r2, dh2, _n2 = cmp.visual_diff(old2, new2)
    assert dh2 == 200, f"100 stored px at half scale is 200 real px (got {dh2})"

    # growth that is ENTIRELY the feed getting taller is still discounted away
    old3 = with_boxes(_base(), [box(130, 100)], height=900)
    new3 = with_boxes(_base(), [box(130, 200)], height=1000)
    _r3, dh3, _n3 = cmp.visual_diff(old3, new3)
    assert dh3 == 0, f"the feed grew, the page did not (got {dh3})"
