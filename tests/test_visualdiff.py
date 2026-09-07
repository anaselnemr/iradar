"""The visual diff: highlights must sit on what changed, and the close-up
strips must show the SAME content before and after.

The template promises a reviewer: "Each strip shows the same spot on the
page: left = before, right = after." Every test here holds the algorithm to
that sentence, especially when content is inserted or removed - the case
where a naive pixel diff smears "changed" down the whole page and the strips
crop two different places while claiming they are the same spot.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "iradar"))

from PIL import Image, ImageDraw   # noqa: E402
import compare as cmp              # noqa: E402


def _page(height=1200, width=600, blocks=(), seed_rows=True):
    """A synthetic page: white ground, grey 'text rows', plus given blocks.

    blocks: list of (y, h, colour) rectangles standing in for content.
    seed_rows puts a light row texture everywhere so rows are distinguishable
    - a blank white page would let any alignment claim to succeed.
    """
    img = Image.new("RGB", (width, height), (255, 255, 255))
    d = ImageDraw.Draw(img)
    if seed_rows:
        for y in range(0, height - 14, 28):
            # every row distinct and APERIODIC, like real text: a periodic
            # texture makes alignment genuinely ambiguous (matching one period
            # off is tokenwise perfect), which no real page is
            g = 120 + ((y * 37) % 113)
            x = 40 + ((y * 13) % 60)
            d.rectangle([x, y + 8, width - 40 - ((y * 7) % 50), y + 18],
                        fill=(g, g, g))
    for y, h, colour in blocks:
        d.rectangle([60, y, width - 60, y + h], fill=colour)
    return img


def _snap(tmp, name, img):
    d = Path(tmp) / name
    d.mkdir(parents=True, exist_ok=True)
    img.save(d / "shot.png")
    return str(d)


def _insert_rows(img, at, h, colour=(30, 90, 200)):
    """The page with a coloured block of height h inserted at row `at`."""
    w = img.width
    out = Image.new("RGB", (w, img.height + h), (255, 255, 255))
    out.paste(img.crop((0, 0, w, at)), (0, 0))
    ImageDraw.Draw(out).rectangle([60, at, w - 60, at + h - 1], fill=colour)
    out.paste(img.crop((0, at, w, img.height)), (0, at + h))
    return out


def test_identical_pages_have_no_visual_difference():
    t = tempfile.mkdtemp()
    img = _page()
    a = _snap(t, "a", img)
    b = _snap(t, "b", img.copy())
    ratio, dh, n = cmp.visual_diff(a, b)
    assert ratio == 0.0 and n == 0, (ratio, n)


def test_an_inserted_block_does_not_smear_down_the_page():
    """THE bug. 100px inserted at y=300 shifts 900px of identical content;
    a naive diff calls all of it changed. The change is 100px tall."""
    t = tempfile.mkdtemp()
    img = _page(height=1200)
    a = _snap(t, "a", img)
    b = _snap(t, "b", _insert_rows(img, 300, 100))
    ratio, dh, n = cmp.visual_diff(a, b)
    # the insert is ~8% of the page; the smear would be ~75%
    assert ratio < 20.0, "insertion smeared: ratio=%.1f%%" % ratio
    assert n <= 3, "insertion shattered into %d regions" % n


def test_an_edit_far_below_an_insert_is_still_seen():
    """Alignment must not BURY real changes: an edit 600px below the insert
    has to flag even though everything around it shifted."""
    t = tempfile.mkdtemp()
    img = _page(height=1200, blocks=[(900, 40, (200, 40, 40))])
    edited = _insert_rows(_page(height=1200,
                                blocks=[(900, 40, (40, 160, 40))]), 300, 100)
    a = _snap(t, "a", img)
    b = _snap(t, "b", edited)
    change = Path(t) / "chg"
    ratio, dh, n = cmp.visual_diff(a, b, change_dir=str(change))
    assert n >= 2, "the shifted edit was lost (n=%d)" % n
    # and a box must land near the edit in EACH image's own coordinates:
    # y~900 in the old image, y~1000 (shifted by the insert) in the new
    boxes_old = cmp._last_boxes_old
    boxes_new = cmp._last_boxes_new
    assert any(y0 <= 920 and y1 >= 900 for _, y0, _, y1 in boxes_old), \
        "no box near the edit on the OLD image: %s" % (boxes_old,)
    assert any(y0 <= 1020 and y1 >= 1000 for _, y0, _, y1 in boxes_new), \
        "no box near the edit's SHIFTED position on the NEW image: %s" % (boxes_new,)


def test_the_close_up_strip_shows_the_same_spot():
    """After a 100px insert at y=300, the strip for an edit at y=900/1000 must
    crop y~900 from the old image and y~1000 from the new - comparing the
    strip's two halves outside the edit itself should find them near-equal."""
    t = tempfile.mkdtemp()
    img = _page(height=1200, blocks=[(900, 40, (200, 40, 40))])
    edited = _insert_rows(_page(height=1200,
                                blocks=[(900, 40, (40, 160, 40))]), 300, 100)
    a = _snap(t, "a", img)
    b = _snap(t, "b", edited)
    change = Path(t) / "chg"
    cmp.visual_diff(a, b, change_dir=str(change))
    # webp since 6 Sep 2026 (reviewer-facing art, never re-compared), png
    # before it - the assertion is about the crop, not the container
    strips = sorted(p for p in change.iterdir()
                    if p.name.startswith("region_")
                    and p.suffix.lower() in (".webp", ".png"))
    assert strips, "no close-up strips were written"
    # judge each strip's FRAME - the rows around the change, which are
    # unchanged content and so must agree between the halves. Under the old
    # algorithm the lower strip cropped two different places, so even its
    # surroundings disagreed. The bottom frame is only judged where both
    # sides actually hold this content: a pure insert has nothing below on
    # the before side, and that mismatch is the strip being honest.
    from PIL import ImageChops
    boxes_old, boxes_new = cmp._last_boxes_old, cmp._last_boxes_new
    worst = 0.0
    for i, path in enumerate(strips):
        strip = Image.open(path).convert("RGB")
        header, divider = 26, 10
        w = (strip.width - divider) // 2
        left = strip.crop((0, header, w, strip.height))
        right = strip.crop((w + divider, header, w * 2 + divider, strip.height))
        h_old = boxes_old[i][3] - boxes_old[i][1]
        h_new = boxes_new[i][3] - boxes_new[i][1]
        frame_h = 15
        bands = [(0, frame_h)]
        if abs(h_old - h_new) <= 10:
            bands.append((left.height - frame_h, left.height))
        for band in bands:
            l = left.crop((0, band[0], w, band[1]))
            r = right.crop((0, band[0], w, band[1]))
            diff = ImageChops.difference(l, r).convert("L")
            frac = diff.point(lambda p: 255 if p > 30 else 0).histogram()[255]                 / float(l.width * l.height)
            worst = max(worst, frac)
    assert worst < 0.15,         "a strip's surroundings disagree by %.0f%% - it is not the same spot"         % (worst * 100)


def test_a_removed_block_is_boxed_on_the_old_image():
    t = tempfile.mkdtemp()
    tall = _insert_rows(_page(height=1200), 300, 120)
    a = _snap(t, "a", tall)                       # block present
    b = _snap(t, "b", _page(height=1200))         # block gone
    change = Path(t) / "chg"
    ratio, dh, n = cmp.visual_diff(a, b, change_dir=str(change))
    assert n >= 1
    boxes_old = cmp._last_boxes_old
    assert any(y0 <= 430 and y1 >= 290 for _, y0, _, y1 in boxes_old), \
        "the removed block is not boxed where it was: %s" % (boxes_old,)


def test_huge_pages_still_compare_within_budget():
    """Thousands of comparisons run per week; one pair must stay cheap."""
    import time
    t = tempfile.mkdtemp()
    img = _page(height=14000)
    a = _snap(t, "a", img)
    b = _snap(t, "b", _insert_rows(img, 2000, 90))
    t0 = time.time()
    cmp.visual_diff(a, b)
    took = time.time() - t0
    assert took < 5.0, "one 14000px pair took %.1fs" % took


# --- cases the adversarial review constructed, kept as regression tests ------

def test_a_page_that_grew_has_no_phantom_removal_at_its_bottom():
    """The min-height crop fed the aligner two equal-height pictures with the
    taller side's tail cut off - so every grown page carried a red box at the
    bottom claiming a section vanished. A fifth of real pairs grow."""
    t = tempfile.mkdtemp()
    img = _page(height=2000)
    a = _snap(t, "a", img)
    b = _snap(t, "b", _insert_rows(img, 100, 300))
    ratio, dh, n = cmp.visual_diff(a, b)
    for x0, y0, x1, y1 in cmp._last_boxes_old:
        assert y0 < 1650, \
            "phantom removal boxed at the old page's bottom: %s" % (
                (x0, y0, x1, y1),)
    # the banner is ~13%% of the grown page; the phantom-tail failure mode
    # counted it TWICE (once as the insert, once as a fake removal at the
    # cut line), which lands near double
    assert ratio < 15, "the cut tail doubled the ratio: %.1f%%" % ratio
    assert ratio > 5, "the insert went unpriced: %.1f%%" % ratio


def test_a_solid_banner_is_priced_and_boxed_where_it_is():
    """_ink once priced a band against its own dominant tone, so a solid
    banner scored zero against itself: the one change on the page went
    unboxed while a phantom at the bottom pointed reviewers the wrong way."""
    t = tempfile.mkdtemp()
    img = _page(height=2000)
    a = _snap(t, "a", img)
    b = _snap(t, "b", _insert_rows(img, 300, 300, colour=(40, 60, 180)))
    ratio, dh, n = cmp.visual_diff(a, b)
    assert n >= 1, "a 300px solid banner produced no region at all"
    assert any(y0 <= 620 and y1 >= 300 for _, y0, _, y1 in cmp._last_boxes_new), \
        "the banner is not boxed where it is: %s" % (cmp._last_boxes_new,)
    assert ratio > 1.0, "a full-width banner priced at %.2f%%" % ratio


def test_a_solid_bar_replacing_the_first_rows_is_seen():
    t = tempfile.mkdtemp()
    img = _page(height=1200)
    edited = img.copy()
    ImageDraw.Draw(edited).rectangle([0, 0, img.width, 30], fill=(200, 30, 30))
    a = _snap(t, "a", img)
    b = _snap(t, "b", edited)
    ratio, dh, n = cmp.visual_diff(a, b)
    assert n >= 1 and ratio > 0, \
        "a solid bar over the first rows went unseen (ratio=%.3f n=%d)" % (ratio, n)


def test_pathological_striping_falls_back_within_budget():
    """difflib's bill is token duplication, not rows: one-pixel zebra pages
    sat under the row cap at minutes per comparison. The cost estimate must
    hand those to the positional diff instead."""
    import time
    t = tempfile.mkdtemp()
    w, h = 600, 6000
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for y in range(0, h, 2):
        d.rectangle([0, y, w, y], fill=(0, 0, 0))
    shifted = Image.new("RGB", (w, h), (255, 255, 255))
    shifted.paste(img.crop((0, 0, w, h - 1)), (0, 1))
    a = _snap(t, "a", img)
    b = _snap(t, "b", shifted)
    t0 = time.time()
    cmp.visual_diff(a, b)
    took = time.time() - t0
    assert took < 3.0, "the zebra pair took %.1fs - the cost guard failed" % took


def test_a_one_pixel_rendering_slip_is_not_a_change():
    """Two captures of an unchanged page routinely render a row 1-2px apart.
    At the noise threshold that lights both edges of every glyph - a thin
    full-width box around text nobody touched. Change #12394 carried five."""
    t = tempfile.mkdtemp()
    img = _page(height=1200)
    slipped = Image.new("RGB", (img.width, 1200), (255, 255, 255))
    # the lower half sits one pixel lower, the slack absorbed in a white gap
    # (that is where real reflow slack lives - splitting mid-glyph would tear
    # a real pixel row in two, which genuinely IS a difference)
    slipped.paste(img.crop((0, 0, img.width, 612)), (0, 0))
    slipped.paste(img.crop((0, 612, img.width, 1199)), (0, 613))
    a = _snap(t, "a", img)
    b = _snap(t, "b", slipped)
    ratio, dh, n = cmp.visual_diff(a, b)
    assert n == 0, "a 1px slip earned %d red boxes" % n


def test_a_real_edit_survives_the_slip_test():
    """The slip test must not eat genuine changes: a recoloured row stays
    different at every offset."""
    t = tempfile.mkdtemp()
    img = _page(height=1200, blocks=[(600, 30, (200, 40, 40))])
    edited = _page(height=1200, blocks=[(600, 30, (40, 160, 40))])
    a = _snap(t, "a", img)
    b = _snap(t, "b", edited)
    ratio, dh, n = cmp.visual_diff(a, b)
    assert n >= 1, "a recoloured row was eaten by the slip test"


def test_anti_aliasing_jitter_is_not_a_change():
    """Letter-spaced small-caps re-rendered half a pixel off light every
    glyph edge past the threshold at EVERY integer offset - the slip test
    cannot catch that. Under a one-pixel blur the jitter vanishes; a real
    edit does not."""
    t = tempfile.mkdtemp()
    img = _page(height=1200)
    jittered = img.copy()
    d = ImageDraw.Draw(jittered)
    # re-render one text row with soft half-intensity fringes above and
    # below - what a half-pixel vertical re-raster does to sharp edges
    y = 616
    g = 120 + ((y * 37) % 113)
    x = 40 + ((y * 13) % 60)
    x1 = 600 - 40 - ((y * 7) % 50)
    d.rectangle([x, y + 8, x1, y + 18], fill=(255, 255, 255))
    d.rectangle([x, y + 9, x1, y + 19], fill=(g, g, g))
    half = (g + 255) // 2
    d.rectangle([x, y + 8, x1, y + 8], fill=(half, half, half))
    a = _snap(t, "a", img)
    b = _snap(t, "b", jittered)
    ratio, dh, n = cmp.visual_diff(a, b)
    assert n == 0, "AA jitter earned %d red boxes" % n


def test_a_real_word_change_survives_the_blur():
    t = tempfile.mkdtemp()
    img = _page(height=1200)
    edited = img.copy()
    ImageDraw.Draw(edited).rectangle([100, 620, 420, 632], fill=(30, 30, 160))
    a = _snap(t, "a", img)
    b = _snap(t, "b", edited)
    ratio, dh, n = cmp.visual_diff(a, b)
    assert n >= 1, "a real edit was blurred away"
