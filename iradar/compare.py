"""Snapshot comparison: text diff, image inventory diff, screenshot pixel diff."""
import difflib
import json
import re
from collections import Counter
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

Image.MAX_IMAGE_PIXELS = None   # full-page screenshots are very tall

PIXEL_NOISE_THRESHOLD = 30      # per-channel delta below this = anti-aliasing noise
MAX_REGIONS = 12                # cap on highlighted areas per change
REGION_ROW_GAP = 28             # changed rows closer than this merge into one area


def _load_text_lines(snap_dir, ignore_patterns=None):
    p = Path(snap_dir) / "text.txt"
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    if ignore_patterns:
        compiled = []
        for pat in ignore_patterns:
            pat = pat.strip()
            if not pat:
                continue
            try:
                compiled.append(re.compile(pat, re.I))
            except re.error:
                pass
        if compiled:
            lines = [l for l in lines if not any(rx.search(l) for rx in compiled)]
    return lines


def _load_images(snap_dir):
    p = Path(snap_dir) / "images.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _meta_rev(snap_dir, key):
    """A revision stamp from meta.json (text_rev / shot_rev); 1 if absent.
    Used only to tell whether two snapshots' text/pictures are comparable."""
    p = Path(snap_dir) / "meta.json"
    if not p.exists():
        return 1
    try:
        return int(json.loads(p.read_text(encoding="utf-8")).get(key, 1))
    except Exception:
        return 1


def _text_rev(snap_dir):
    """Which revision of TEXT_JS produced this snapshot's text.txt."""
    return _meta_rev(snap_dir, "text_rev")


def _shot_rev(snap_dir):
    """Which revision of the screenshot produced this snapshot's shot.png -
    the feed-hidden picture (rev 2) is not comparable to the old full one."""
    return _meta_rev(snap_dir, "shot_rev")


def _img_rev(snap_dir):
    """Which revision of the image collector produced images.json - the
    feed-free inventory (rev 2) is not comparable to the old full one."""
    return _meta_rev(snap_dir, "img_rev")


def _sections_rev(snap_dir):
    """Which revision of SECTIONS_JS produced this snapshot's sections.json."""
    return _meta_rev(snap_dir, "sections_rev")


def text_diff(old_dir, new_dir, ignore_patterns=None):
    """Returns (changed, added_count, removed_count, hunks).

    hunks = list of {"old_start", "new_start", "lines": [(tag, text), ...]}
    where tag is ' ', '-', '+'."""
    old = _load_text_lines(old_dir, ignore_patterns)
    new = _load_text_lines(new_dir, ignore_patterns)
    sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    hunks, added, removed = [], 0, 0
    for group in sm.get_grouped_opcodes(n=2):
        lines = []
        for tag, i1, i2, j1, j2 in group:
            if tag in ("replace", "delete"):
                for l in old[i1:i2]:
                    lines.append(("-", l))
                    removed += 1
            if tag in ("replace", "insert"):
                for l in new[j1:j2]:
                    lines.append(("+", l))
                    added += 1
            if tag == "equal":
                for l in old[i1:i2]:
                    lines.append((" ", l))
        hunks.append({"old_start": group[0][1] + 1,
                      "new_start": group[0][3] + 1,
                      "lines": lines})
    changed = added > 0 or removed > 0
    return changed, added, removed, hunks


def _compile_patterns(patterns):
    compiled = []
    for pat in patterns or []:
        pat = pat.strip()
        if not pat:
            continue
        try:
            compiled.append(re.compile(pat, re.I))
        except re.error:
            pass
    return compiled


def images_diff(old_dir, new_dir, ignore_patterns=None):
    """Returns (changed, added_list, removed_list)."""
    old = set(_load_images(old_dir))
    new = set(_load_images(new_dir))
    rxs = _compile_patterns(ignore_patterns)
    if rxs:
        old = {u for u in old if not any(rx.search(u) for rx in rxs)}
        new = {u for u in new if not any(rx.search(u) for rx in rxs)}
    added = sorted(new - old)
    removed = sorted(old - new)
    return bool(added or removed), added, removed


# --- aligned visual diff ----------------------------------------------------
# A page is a COLUMN OF CONTENT, not a grid of pixels. Insert a banner at the
# top and every pixel below it moves down; a pixel-to-pixel diff then calls
# the whole page changed, the red boxes smear to the footer, and the close-up
# strips crop the same coordinates from two pictures whose content no longer
# lives at the same coordinates - a "BEFORE | AFTER" that shows two different
# places while the caption promises the same spot. That was the inconsistency
# reviewers saw on every update that changed the page's height.
#
# So the rows are aligned first, the same way the text diff aligns lines:
# each pixel row gets a coarse signature, difflib matches the two sequences,
# and only then are pixels compared - equal-length stretches diffed at their
# ALIGNED offsets, inserted or removed stretches taken as changes of exactly
# their own size. Every region that comes out carries its position in BOTH
# pictures, so a box lands where the content is in each, and a strip crops
# each side at its own coordinates.

# Rows beyond this skip alignment and fall back to the plain diff - a memory
# bound on the signature lists, not the real cost guard (below).
ALIGN_MAX_ROWS = 40_000

# The REAL cost of difflib is matching work: for every token of one side it
# walks every position of the other side holding the same token, so the bill
# is sum over tokens of count_a * count_b. Real pages measure ~80k; a page of
# one-pixel zebra striping measures 112 million and takes minutes. Rows never
# predicted that - 20,000 rows of stripes sat under the row cap at 176s per
# comparison. Above this estimate the positional diff takes over: wrong about
# shifts, bounded in time.
ALIGN_MAX_COST = 4_000_000

# A region below this many changed pixels is dust - palette jitter, a moved
# hairline - not something worth a red box and a close-up. It still counts
# toward the ratio; it just is not drawn.
REGION_MIN_PX = 30

# How far a band may slip vertically and still be called the same content.
# Two captures of one unchanged page routinely render a row a pixel or two
# apart - fonts settle differently, a flex gap rounds the other way - and at
# the noise threshold a one-pixel slip lights up both edges of every glyph:
# a thin full-width box around text nobody changed. Change #12394 carried
# five such bands beside four real ones.
SLIP_MAX = 3
SLIP_KEEP = 0.15          # below 15% of its pixels left, the band was a slip


def _row_signatures(img, cols=24):
    """One coarse token per pixel row, tolerant of palette jitter.

    The row is box-averaged into `cols` cells and quantized to 16 levels: two
    captures of the SAME row must produce the same token even though the two
    screenshots were quantized to different 256-colour palettes. Anything the
    averaging hides is caught later by the pixel diff inside aligned rows -
    a too-coarse signature degrades to detection, never to a miss.
    """
    w, h = img.size
    if not w or not h:
        return []
    g = img.convert("L").resize((cols, h), Image.Resampling.BOX)
    data = g.tobytes()
    return [bytes(v >> 4 for v in data[y * cols:(y + 1) * cols])
            for y in range(h)]


def _page_tone(img):
    """The page's background, as its dominant luminance."""
    hist = img.convert("L").histogram()
    return max(range(256), key=lambda i: hist[i])


def _ink(img, box, background, noise=PIXEL_NOISE_THRESHOLD):
    """How much of this area is CONTENT - pixels far from the PAGE's tone.

    Content that exists on one side only (an inserted banner, a removed
    section) has no counterpart to pixel-diff against, so it is priced at its
    ink. Pricing it at its full rectangle counted every white pixel of an
    inserted paragraph as "changed" and tripled the visual flags on real
    pages; pricing whitespace growth at zero keeps a page that merely
    breathed out from flagging at all.

    Measured against the page's OWN background, never the band's: a band's
    dominant tone is the band, so a solid inserted banner scored zero against
    itself and the one change on the page went unboxed.
    """
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return 0
    hist = img.crop(box).convert("L").histogram()
    lo = max(0, background - noise)
    hi = min(255, background + noise)
    return sum(hist[:lo]) + sum(hist[hi + 1:])


def _mostly_black(img, box):
    """Is this area our own mask ink rather than page content?

    The feed areas are filled black before comparing. When the fill on one
    side is taller than on the other, the leftover black rows would read as
    an inserted block - a change made of our own paint. Anything this dark
    is not content; page content at half scale always carries structure.
    """
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return False
    hist = img.crop(box).convert("L").histogram()
    dark = sum(hist[:12])
    total = sum(hist)
    return total > 0 and dark / total > 0.98


def _mask_bands(mask):
    """Row bands with any changed pixel in `mask` -> [(y0, y1, x0, x1, px)]."""
    w, h = mask.size
    if not w or not h:
        return []
    cols = 16
    grid = mask.resize((cols, h), Image.Resampling.BOX)
    data = list(grid.getdata())
    flagged = [any(data[y * cols + i] for i in range(cols)) for y in range(h)]
    bands, start, prev = [], None, None
    for y, f in enumerate(flagged):
        if not f:
            continue
        if start is None:
            start, prev = y, y
        elif y - prev > REGION_ROW_GAP:
            bands.append((start, prev))
            start, prev = y, y
        else:
            prev = y
    if start is not None:
        bands.append((start, prev))
    out = []
    for y0, y1 in bands:
        strip = mask.crop((0, y0, w, y1 + 1))
        bb = strip.getbbox()
        if not bb:
            continue
        out.append((y0, y1 + 1, bb[0], bb[2], strip.histogram()[255]))
    return out


def _runs(sigs):
    """Consecutive identical row signatures collapsed to (sig, start, count).

    A page is mostly runs - whitespace between blocks, solid banners - and
    handing every one of those rows to difflib as its own token is both its
    quadratic worst case (thousands of copies of the same token) and how rows
    mis-align. On runs the matcher sees a few hundred tokens instead of
    fourteen thousand rows, each one distinctive.
    """
    out = []
    for i, sig in enumerate(sigs):
        if out and out[-1][0] == sig:
            out[-1][2] += 1
        else:
            out.append([sig, i, 1])
    return out


def _overlaps_painted(y0, y1, painted):
    """Does this row band mostly lie where the feed mask was painted?"""
    if y1 <= y0 or not painted:
        return False
    covered = 0
    for p0, p1 in painted:
        covered += max(0, min(y1, p1) - max(y0, p0))
    return covered / (y1 - y0) > 0.5


def _aligned_regions(a, b, noise=PIXEL_NOISE_THRESHOLD,
                     painted_a=(), painted_b=()):
    """Compare two page images with row alignment.

    Returns (changed_px, regions): each region is a dict carrying the change's
    y-range in the OLD image ('a'), in the NEW image ('b'), its x-extent, and
    its changed-pixel mass. A pure insertion has a zero-height 'a' range at
    its anchor; a pure removal the reverse.
    """
    w = min(a.width, b.width)
    runs_a = _runs(_row_signatures(a))
    runs_b = _runs(_row_signatures(b))
    tok_a = [r[0] for r in runs_a]
    tok_b = [r[0] for r in runs_b]
    # the actual difflib bill, estimated before it is paid (see ALIGN_MAX_COST)
    counts_b = Counter(tok_b)
    if sum(counts_b.get(t, 0) for t in tok_a) > ALIGN_MAX_COST:
        return None
    sm = difflib.SequenceMatcher(a=tok_a, b=tok_b, autojunk=False)
    tone_a, tone_b = _page_tone(a), _page_tone(b)
    changed_px = 0
    regions = []

    def slipped(ay0, ay1, by0, by1, base_px):
        """Does this band's difference vanish at a small vertical offset?

        The offsets difflib can express are whole runs; WITHIN a matched
        stretch a one-pixel rendering slip still pairs row k with row k, and
        the pixel diff lights the edges of every glyph. Content that matches
        a pixel or two away is the same content."""
        margin = SLIP_MAX + 1
        top = max(0, ay0 - margin)
        ca = a.crop((0, top, w, min(a.height, ay1 + margin)))
        best = base_px
        for dy in range(-SLIP_MAX, SLIP_MAX + 1):
            if dy == 0:
                continue
            bt = by0 - (ay0 - top) + dy
            if bt < 0 or bt + ca.height > b.height:
                continue
            d = ImageChops.difference(ca, b.crop((0, bt, w, bt + ca.height)))
            m = d.convert("L").point(lambda p: 255 if p > noise else 0)
            best = min(best, m.histogram()[255])
            if best <= max(REGION_MIN_PX, SLIP_KEEP * base_px):
                return True
        return False

    def diff_aligned(a0, a1, b0, b1):
        nonlocal changed_px
        ca = a.crop((0, a0, w, a1))
        cb = b.crop((0, b0, w, b1))
        d = ImageChops.difference(ca, cb).convert("L")
        m = d.point(lambda p: 255 if p > noise else 0)
        if not m.getbbox():
            return
        for y0, y1, x0, x1, px in _mask_bands(m):
            # a band where either side is our own feed-mask PAINT is paint
            # meeting content, not content meeting content - under a shift
            # the union-painted rows pair against unpainted ones, and one
            # such band once contributed 68% of a pair's "changed" pixels.
            # Judged by geometry AND colour: black alone is not proof, a
            # genuinely black page is content too - only black WHERE WE
            # PAINTED is ours.
            if (_overlaps_painted(a0 + y0, a0 + y1, painted_a)
                    and _mostly_black(a, (0, a0 + y0, w, a0 + y1))) \
                    or (_overlaps_painted(b0 + y0, b0 + y1, painted_b)
                        and _mostly_black(b, (0, b0 + y0, w, b0 + y1))):
                continue
            if slipped(a0 + y0, a0 + y1, b0 + y0, b0 + y1, px):
                continue
            changed_px += px
            regions.append({"a": (a0 + y0, a0 + y1), "b": (b0 + y0, b0 + y1),
                            "x": (x0, x1), "px": px})

    def band(a0, a1, b0, b1):
        """A stretch the tokens could not pair. The tokens are coarse and
        quantized, so this is a CLAIM, not a verdict: a cell average sitting
        on a quantization boundary flips between two captures of the same
        pixels, runs fragment differently, and visually identical content
        arrives here labelled inserted. So the overlap is pixel-verified like
        any aligned stretch - identical pixels contribute nothing however the
        tokens fragmented - and only the surplus, the rows that exist on one
        side alone, is judged by what is actually printed on it."""
        nonlocal changed_px
        ha, hb = a1 - a0, b1 - b0
        overlap = min(ha, hb)
        if overlap:
            diff_aligned(a0, a0 + overlap, b0, b0 + overlap)
        if ha == hb:
            return
        if ha > hb:
            img, sy0, sy1 = a, a0 + overlap, a1
            reg = {"a": (sy0, sy1), "b": (b1, b1)}
        else:
            img, sy0, sy1 = b, b0 + overlap, b1
            reg = {"a": (a1, a1), "b": (sy0, sy1)}
        painted = painted_a if img is a else painted_b
        if _overlaps_painted(sy0, sy1, painted) \
                and _mostly_black(img, (0, sy0, w, sy1)):
            return                            # our own feed-mask ink
        # A ONE-PIXEL SLIP arrives here too: at a run boundary the wobble
        # reads as six rows against seven - a tiny uneven stretch - and
        # took this path, where the slip test never ran. Change 12394
        # kept its five phantom bands through the first fix that way.
        # Only small stretches are tested: a banner is not a wobble.
        if max(a1 - a0, b1 - b0) <= 4 * SLIP_MAX \
                and slipped(a0, a1, b0, b1,
                            _ink(img, (0, sy0, w, sy1),
                                 tone_a if img is a else tone_b)):
            return
        px_here = _ink(img, (0, sy0, w, sy1),
                       tone_a if img is a else tone_b)
        if not px_here:
            return                            # whitespace grew; not content
        changed_px += px_here
        reg.update({"x": (0, w), "px": px_here})
        regions.append(reg)

    for tag, ra0, ra1, rb0, rb1 in sm.get_opcodes():
        if tag == "equal":
            # matched runs of the same signature. Run lengths can differ
            # (spacing that grew or shrank): pixel-check the overlap, and let
            # the surplus rows - repeats of a row that is provably still
            # there - pass as spacing rather than content. height_delta
            # already reports the page growing.
            for k in range(ra1 - ra0):
                sa, ca_ = runs_a[ra0 + k][1], runs_a[ra0 + k][2]
                sb, cb_ = runs_b[rb0 + k][1], runs_b[rb0 + k][2]
                n = min(ca_, cb_)
                diff_aligned(sa, sa + n, sb, sb + n)
            continue
        # replace / insert / delete on runs: the whole differing stretch,
        # in each image's own rows
        a0 = runs_a[ra0][1] if ra0 < len(runs_a) else a.height
        a1 = (runs_a[ra1 - 1][1] + runs_a[ra1 - 1][2]) if ra1 > ra0 else a0
        b0 = runs_b[rb0][1] if rb0 < len(runs_b) else b.height
        b1 = (runs_b[rb1 - 1][1] + runs_b[rb1 - 1][2]) if rb1 > rb0 else b0
        if tag == "replace" and (a1 - a0) == (b1 - b0):
            diff_aligned(a0, a1, b0, b1)     # in-place rewrite: pixels decide
        else:
            band(a0, a1, b0, b1)
    return changed_px, regions


def _merge_regions(regions):
    """Nearby regions become one area; more than MAX_REGIONS collapse.

    Gap is judged in the NEW image's space (it is what the reviewer reads
    first), and a merge unions both spaces so the pair stays a pair.
    """
    regions = sorted((r for r in regions if r["px"] >= REGION_MIN_PX),
                     key=lambda r: (r["b"][0], r["a"][0]))
    merged = []
    for r in regions:
        if merged and r["b"][0] - merged[-1]["b"][1] <= REGION_ROW_GAP \
                and r["a"][0] - merged[-1]["a"][1] <= REGION_ROW_GAP:
            m = merged[-1]
            m["a"] = (min(m["a"][0], r["a"][0]), max(m["a"][1], r["a"][1]))
            m["b"] = (min(m["b"][0], r["b"][0]), max(m["b"][1], r["b"][1]))
            m["x"] = (min(m["x"][0], r["x"][0]), max(m["x"][1], r["x"][1]))
            m["px"] += r["px"]
        else:
            merged.append(dict(r))
    while len(merged) > MAX_REGIONS:
        gaps = [(merged[i + 1]["b"][0] - merged[i]["b"][1], i)
                for i in range(len(merged) - 1)]
        _, i = min(gaps)
        m, n = merged[i], merged[i + 1]
        m["a"] = (min(m["a"][0], n["a"][0]), max(m["a"][1], n["a"][1]))
        m["b"] = (min(m["b"][0], n["b"][0]), max(m["b"][1], n["b"][1]))
        m["x"] = (min(m["x"][0], n["x"][0]), max(m["x"][1], n["x"][1]))
        m["px"] += n["px"]
        del merged[i + 1]
    return merged


def _boxes_for(regions, side, height):
    """Draw-ready boxes for one image. A zero-height range (content that only
    exists on the other side) becomes a thin marker at its anchor, so the
    picture still shows WHERE the content arrived or left."""
    boxes = []
    for r in regions:
        y0, y1 = r[side]
        if y1 - y0 < 6:
            y0 = max(0, y0 - 3)
            y1 = min(height, y1 + 3)
        boxes.append((r["x"][0], y0, r["x"][1], y1))
    return boxes


def _find_regions(mask):
    """Group changed pixels into horizontal bands -> list of (x0,y0,x1,y1).

    Rows are detected via a 16-column box-downscale (any changed pixel in a
    ~90px cell survives the averaging), consecutive changed rows merge into
    bands, and each band's exact x-extent comes from its own bounding box."""
    w, h = mask.size
    cols = 16
    grid = mask.resize((cols, h), Image.Resampling.BOX)
    data = list(grid.getdata())
    flagged = [any(data[y * cols + i] for i in range(cols)) for y in range(h)]

    bands, start, prev = [], None, None
    for y, f in enumerate(flagged):
        if not f:
            continue
        if start is None:
            start, prev = y, y
        elif y - prev > REGION_ROW_GAP:
            bands.append((start, prev))
            start, prev = y, y
        else:
            prev = y
    if start is not None:
        bands.append((start, prev))

    while len(bands) > MAX_REGIONS:   # merge the two closest bands
        gaps = [(bands[i + 1][0] - bands[i][1], i) for i in range(len(bands) - 1)]
        _, i = min(gaps)
        bands[i] = (bands[i][0], bands[i + 1][1])
        del bands[i + 1]

    boxes = []
    for y0, y1 in bands:
        bb = mask.crop((0, y0, w, y1 + 1)).getbbox()
        if bb:
            boxes.append((bb[0], y0, bb[2], y1 + 1))
    return boxes


def _draw_boxes(img, boxes, out_path, pad=8):
    marked = img.copy()
    draw = ImageDraw.Draw(marked)
    for x0, y0, x1, y1 in boxes:
        draw.rectangle([max(0, x0 - pad), max(0, y0 - pad),
                        min(img.width - 1, x1 + pad),
                        min(img.height - 1, y1 + pad)],
                       outline=(220, 0, 40), width=5)
    marked.save(out_path, optimize=True)


# A strip taller than this is cut: an entire inserted section does not need
# a four-thousand-pixel close-up to say "this arrived", and the full shots
# show the whole of it anyway.
STRIP_MAX_H = 1000


# Every close-up gets at least this much page around it. Without a floor the
# strip for a 12px icon came out 92px wide while the strip above it was the
# whole page - and the template shows both at their own size, so the "zoom"
# jumped by a factor of two or three between areas of the same update.
STRIP_MIN_W = 280
STRIP_MIN_H = 90


def _region_strip(old_img, new_img, region, out_path, pad=40):
    """Side-by-side close-up of one changed area: left = before, right = after.

    Each side is cropped AT ITS OWN COORDINATES - the whole point of the
    aligned diff. Before it, both sides were cropped at the same y, so any
    page whose height changed produced strips whose halves showed different
    places while the caption promised the same spot. The changed area itself
    is outlined in amber inside each half, because a close-up that spans a
    whole section still needs to say which line of it moved.
    """
    ay0, ay1 = region["a"]
    by0, by1 = region["b"]
    max_w = min(old_img.width, new_img.width)
    x0 = max(0, region["x"][0] - pad)
    x1 = min(max_w, region["x"][1] + pad)
    if x1 - x0 < STRIP_MIN_W:                # centre out to the floor width
        cx = (x0 + x1) // 2
        x0 = max(0, cx - STRIP_MIN_W // 2)
        x1 = min(max_w, x0 + STRIP_MIN_W)
        x0 = max(0, x1 - STRIP_MIN_W)

    def crop(img, y0, y1):
        # the TOP edge is the same distance above the change on both sides,
        # whatever else happens - the halves are compared by eye, and a few
        # rows of vertical slip between them reads as "not the same spot"
        top = max(0, y0 - pad)
        bot = min(img.height, max(y1 + pad, top + STRIP_MIN_H))
        if bot - top > STRIP_MAX_H:
            bot = top + STRIP_MAX_H
        return img.crop((x0, top, x1, bot)), top

    o, o_top = crop(old_img, ay0, ay1)
    n, n_top = crop(new_img, by0, by1)
    w = x1 - x0
    h = max(o.height, n.height)
    header, divider = 26, 10
    canvas = Image.new("RGB", (w * 2 + divider, h + header), (245, 246, 248))
    draw = ImageDraw.Draw(canvas)
    draw.text((6, 6), "BEFORE", fill=(150, 20, 40))
    draw.text((w + divider + 6, 6), "AFTER", fill=(20, 110, 50))
    canvas.paste(o, (0, header))
    canvas.paste(n, (w + divider, header))
    # the changed area, outlined inside each half at its own offset
    hl = (245, 158, 11)
    for (y0, y1), top, dx in ((region["a"], o_top, 0),
                              (region["b"], n_top, w + divider)):
        if y1 - y0 < 2:
            continue
        draw.rectangle([dx + max(0, region["x"][0] - x0) - 2,
                        header + (y0 - top) - 2,
                        dx + min(w, region["x"][1] - x0) + 2,
                        header + (y1 - top) + 2],
                       outline=hl, width=2)
    canvas.save(out_path, optimize=True)


def _shot_ignore(snap_dir):
    """The live-feed regions recorded at capture (meta.json shot_ignore):
    the screenshot KEEPS the community/news boxes so a reviewer sees the whole
    page, and the compare disregards exactly these areas instead."""
    p = Path(snap_dir) / "meta.json"
    if not p.exists():
        return []
    try:
        regs = json.loads(p.read_text(encoding="utf-8")).get("shot_ignore")
        return [r for r in (regs or [])
                if isinstance(r, dict) and r.get("w", 0) > 0 and r.get("h", 0) > 0]
    except Exception:
        return []


def _shot_scale(snap_dir):
    """How big the stored screenshot is relative to the real page.

    Absent on anything captured before the picture started being stored at half
    size, and those are full size - so the default is 1.0 and old snapshots
    keep comparing exactly as they did.
    """
    p = Path(snap_dir) / "meta.json"
    if not p.exists():
        return 1.0
    try:
        v = float(json.loads(p.read_text(encoding="utf-8")).get("shot_scale", 1.0))
        return v if 0 < v <= 1.0 else 1.0
    except Exception:
        return 1.0


def visual_diff(old_dir, new_dir, change_dir=None):
    """Pixel-compares the two full-page screenshots.

    Returns (pixel_ratio_percent, height_delta_px, region_count). If
    change_dir is given and there are differences, writes:
      old_boxed.png / new_boxed.png - full screenshots with changed areas boxed
      region_NN.png                 - before|after close-up per changed area"""
    a_path = Path(old_dir) / "shot.png"
    b_path = Path(new_dir) / "shot.png"
    if not a_path.exists() or not b_path.exists():
        return 0.0, 0, 0
    # A truncated screenshot (capture killed mid-write, disk full) must not
    # crash the whole comparison - it took the text/image/library signals down
    # with it. Degrade to "no visual signal"; the other three still run.
    try:
        a = Image.open(a_path).convert("RGB")
        b = Image.open(b_path).convert("RGB")
    except Exception:
        return 0.0, 0, 0
    regs_a, regs_b = _shot_ignore(old_dir), _shot_ignore(new_dir)
    # Height honesty with feeds in the picture: the community box growing by
    # three threads is not the PAGE getting taller. Judge height with each
    # side's feed heights taken out.
    fh_a = sum(r["h"] for r in regs_a)
    fh_b = sum(r["h"] for r in regs_b)
    # The picture may be stored smaller than the page (shot_scale). Heights
    # measured on it are in STORED pixels; the threshold is set in real page
    # pixels on the settings screen. Convert, or a half-size image quietly
    # doubles the threshold and a page that grew 80px reads as 40.
    scale = min(_shot_scale(old_dir), _shot_scale(new_dir))
    # Taking the feeds out may only ever SHRINK the difference, never create
    # one. The masked list is an inventory of what was on screen, and it flaps
    # on its own: #7534 recorded the price block once at 24px in one capture
    # and three times at 28px in the next, so subtracting each side's total
    # from two pictures of IDENTICAL height manufactured a 120px growth on a
    # page that had not moved a pixel. What the page actually did is the raw
    # difference between the two heights; discounting feeds can explain some
    # of that away, and nothing more.
    raw = abs(a.height - b.height)
    discounted = abs((a.height - fh_a) - (b.height - fh_b))
    height_delta = min(raw, discounted) / (scale or 1.0)
    w = min(a.width, b.width)
    if w == 0 or min(a.height, b.height) == 0:
        return 0.0, height_delta, 0
    # FULL heights from here on. The old positional diff cropped both sides
    # to the shorter page, which was harmless when every pixel below min(h)
    # was ignored anyway - but the aligner exists precisely to read the
    # difference in height, and cropping first fed it two equal-height
    # pictures with the taller side's tail cut off: the shorter side's last
    # rows then matched nothing and every grown page carried a phantom
    # "section removed" box at the bottom. A fifth of real pairs grow.
    h_a, h_b = a.height, b.height
    truncated = False
    # When the feeds CHANGED SIZE, everything below them shifted - those
    # pixels would all read as different while nothing real moved. Compare
    # only above the topmost feed box that run; the area below is judged
    # again next run, when the feed heights match (their usual state).
    if regs_a or regs_b:
        if abs(fh_a - fh_b) > 8:
            tops = [r["y"] for r in regs_a + regs_b]
            cut = max(1, min(min(tops), h_a, h_b))
            h_a = h_b = cut
            truncated = True
    a_c = a.crop((0, 0, w, h_a))
    b_c = b.crop((0, 0, w, h_b))
    # Mask the feed areas on BOTH sides: their churn (threads rotating, news
    # re-dating) must never count as the page changing. The stored pictures
    # stay complete - only the judgement blindfolds itself here.
    painted_a, painted_b = [], []
    if regs_a or regs_b:
        da, db_ = ImageDraw.Draw(a_c), ImageDraw.Draw(b_c)
        for r in regs_a + regs_b:
            x0, y0 = max(0, r["x"]), max(0, r["y"])
            x1 = min(w, r["x"] + r["w"])
            # clamp to each crop's own height - the two sides keep their own
            # heights now, that being the whole point of the aligned diff
            if x1 > x0:
                if min(h_a, r["y"] + r["h"]) > y0:
                    da.rectangle((x0, y0, x1, min(h_a, r["y"] + r["h"])),
                                 fill=(0, 0, 0))
                    painted_a.append((y0, min(h_a, r["y"] + r["h"])))
                if min(h_b, r["y"] + r["h"]) > y0:
                    db_.rectangle((x0, y0, x1, min(h_b, r["y"] + r["h"])),
                                  fill=(0, 0, 0))
                    painted_b.append((y0, min(h_b, r["y"] + r["h"])))
    # ROW ALIGNMENT before any pixel is judged. Without it, content inserted
    # anywhere shifts every pixel below into "changed": the ratio explodes,
    # the boxes smear to the footer, and the strips show two different places.
    # Beyond the row cap, fall back to the old positional diff - wrong about
    # shifts but bounded in time - rather than hand difflib a quadratic case.
    aligned = (_aligned_regions(a_c, b_c,
                                painted_a=painted_a, painted_b=painted_b)
               if max(a_c.height, b_c.height) <= ALIGN_MAX_ROWS else None)
    if aligned is not None:
        changed_px, raw = aligned
        if truncated:
            # the window was cut above the feeds, so content near the cut
            # exists half-in half-out: a band hugging the bottom edge is the
            # cut, not the page. That area is judged next run, when the feed
            # heights match again - the same deferral the cut itself makes.
            edge = a_c.height - REGION_ROW_GAP
            kept = []
            for r in raw:
                if r["a"][1] >= edge or r["b"][1] >= edge:
                    changed_px = max(0, changed_px - r["px"])
                else:
                    kept.append(r)
            raw = kept
        regions = _merge_regions(raw) if raw else []
        denom = float(w * max(a_c.height, b_c.height, 1))
        ratio_pct = 100.0 * changed_px / denom
        boxes_old = _boxes_for(regions, "a", a_c.height)
        boxes_new = _boxes_for(regions, "b", b_c.height)
    else:
        diff = ImageChops.difference(a_c.crop((0, 0, w, min(a_c.height, b_c.height))),
                                     b_c.crop((0, 0, w, min(a_c.height, b_c.height)))
                                     ).convert("L")
        mask = diff.point(lambda p: 255 if p > PIXEL_NOISE_THRESHOLD else 0)
        changed_px = mask.histogram()[255]
        ratio_pct = 100.0 * changed_px / float(w * min(a_c.height, b_c.height))
        boxes = _find_regions(mask) if changed_px else []
        regions = [{"a": (y0, y1), "b": (y0, y1), "x": (x0, x1), "px": 0}
                   for x0, y0, x1, y1 in boxes]
        boxes_old = boxes_new = boxes

    # for tests and callers that want to know WHERE, per image
    global _last_boxes_old, _last_boxes_new
    _last_boxes_old, _last_boxes_new = boxes_old, boxes_new

    if change_dir and regions:
        change_dir = Path(change_dir)
        change_dir.mkdir(parents=True, exist_ok=True)
        _draw_boxes(a, boxes_old, change_dir / "old_boxed.png")
        _draw_boxes(b, boxes_new, change_dir / "new_boxed.png")
        for i, region in enumerate(regions):
            _region_strip(a, b, region, change_dir / f"region_{i:02d}.png")
    return ratio_pct, height_delta, len(regions)


# Where the last visual_diff drew its boxes, in each image's own coordinates.
# Introspection for tests; the artwork is the product.
_last_boxes_old = []
_last_boxes_new = []


# Sections this comparison does NOT own. 'flags' and 'components' are derived
# by us and reported through their own path. 'images' is already a signal of
# its own - and images_diff applies the ignore list, which this would bypass:
# re-comparing them here re-raised a spinner or a partner logo that had simply
# not loaded, on a page images_diff had correctly let pass.
_NOT_CONTENT = {"flags", "components", "images"}

# A part page reprints its stock level, and that ticks constantly. The price
# itself is news; "986 in stock" becoming "985 in stock" is not, and comparing
# it would put every orderable part into review every single run.
_STOCK_RE = re.compile(r"[\d.,]*\d\s*in\s+stock", re.I)


def _load_sections(snap_dir):
    p = Path(snap_dir) / "sections.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _norm_item_url(u):
    """An item's link, normalised so the SAME item is not counted as removed
    AND re-added when a capture appends a #fragment, a ?tracking query, a
    trailing slash or different casing (a board 'SmartSense2X' flapped
    add+remove between two captures of one page on exactly this wobble)."""
    u = str(u or "").split("#")[0].split("?")[0].rstrip("/").lower()
    return u


# Trademark marks are DECORATION, not identity. Infineon renders them
# inconsistently - the same datasheet came back as "PSOC™ 4 …" one week and
# "PSOC 4 …" the next, and elsewhere as "AIROC(TM)" against "AIROC" - so an
# identity built on the raw title turns one document into two: removed here,
# added there. Run 9 raised 2,772 of 3,584 pages on exactly that, every one of
# them a datasheet that had not changed, and the double-check it triggered
# filled the disk and killed the run.
#
# Stripped on BOTH sides at comparison time, which is why this needs no
# re-baseline: a capture stored with the glyph and one stored without now
# answer to the same name.
_MARKS_RE = re.compile(r"(?<=\w)(?:[™®©℠]|\((?:tm|r)\))", re.I)


def strip_marks(text):
    """A title without its trademark decoration, whitespace collapsed."""
    return re.sub(r"\s+", " ", _MARKS_RE.sub("", text or "")).strip()


def _item_id(d):
    """What makes an item itself: its name and where it points."""
    return (strip_marks(str(d.get("title", ""))).lower(),
            _norm_item_url(d.get("url", "")))


def _real_img(u):
    """A real image, not a lazy-load placeholder. A data: URI is what an
    <img> holds BEFORE its real src resolves - a partner logo flapped
    'changed' as its base64 placeholder became the CDN URL on a fuller load.
    Treat a data: URI like a missing image: not evidence of a real change."""
    u = str(u or "")
    return bool(u) and not u.startswith("data:")


def _item_body(d):
    """Everything ELSE about it - the part that can change while it stays the
    same item: its meta (dates, sizes, durations, languages), its document
    type, the group it sits under, its icon.

    An icon that is missing on either side is NOT a difference. These load
    lazily and resolve on roughly two runs in three, so demanding they match
    would raise a review for a slow image rather than a changed one. Two icons
    that are both present and different is a real change and does count.
    """
    return {k: str(v) for k, v in sorted(d.items())
            if k not in ("title", "url", "img", "exact")}


# --- editorial limits -------------------------------------------------------
# What the copy is supposed to fit in, as given by the people who own it.
#
#   key            the section the capture already stores
#   label          what it is called in the brief
#   chars          the longest a single block may be
#   lo, hi         how many blocks there should be (None = no bound)
#
# CHARS IS PER BLOCK, not per section. Features and benefits are bullet lists
# where each bullet has its own 40, and a page carrying several prose blocks
# under one heading should be judged on the longest rather than on their sum -
# a page with three short paragraphs has not written a 1,200-character
# description.
#
# "Description" is read from `about`, which is where the capture files a
# page's own prose. If the brief means the Description TAB instead, this one
# line is the change - the tab is stored separately, in urls.detail_tabs.
# key, label, and the DEFAULTS. The live numbers come from Settings, because
# an editorial limit is an editorial decision - the people who own the copy
# change it without anyone touching code.
COPY_LIMITS = [
    ("about",            "Description",      450, 1, 0),
    ("subheadline",      "Sub-headline",     150, 1, 0),
    ("meta_description", "Meta description", 150, 1, 0),
    ("features",         "Features",          40, 2, 12),
    ("benefits",         "Benefits",          40, 2, 12),
    ("overview",         "Overview",         450, 1, 0),
]
# 0 for a bound means "no bound", so a limit can be switched off from Settings
# without removing the row.
COPY_SETTING_KEYS = [("copy_%s_%s" % (k, part), default)
                     for k, _lbl, chars, lo, hi in COPY_LIMITS
                     for part, default in (("chars", chars), ("min", lo),
                                           ("max", hi))]


def copy_limits(settings=None):
    """The limits in force, defaults where Settings has not been touched."""
    settings = settings or {}
    out = []
    for key, label, chars, lo, hi in COPY_LIMITS:
        def num(part, fallback):
            try:
                return int(str(settings.get("copy_%s_%s" % (key, part),
                                            fallback)).strip() or fallback)
            except (TypeError, ValueError):
                return fallback
        out.append((key, label, num("chars", chars), num("min", lo),
                    num("max", hi)))
    return out


def _blocks(sections, key):
    """The text blocks a section holds, however that section is shaped."""
    v = sections.get(key)
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    out = []
    for it in (v or []):
        t = (it.get("title") if isinstance(it, dict) else it) or ""
        t = str(t).strip()
        if t:
            out.append(t)
    return out


def copy_findings(snap_dir, settings=None):
    """Which editorial limits this capture breaks, keyed by the block.

    {"overview": "451 over 450", "features": "1, needs 2-12"} - keyed rather
    than a flat list so the dashboard can filter on one block ("Overview over
    its limit") and the page can put the flag beside the block it belongs to.

    A BLOCK THAT IS NOT THERE AT ALL IS NOT A BREACH. Most of what this app
    watches are part pages, and a part page carries no marketing overview or
    feature bullets - it never should. Judging them for its absence flagged
    2,819 of 2,820 measured pages, which is not a report. Whether a page OUGHT
    to carry a block is the component matrix's question, per page type, and
    that is where it belongs.
    """
    sec = _load_sections(snap_dir)
    if not sec:
        return {}
    out = {}
    for key, label, chars, lo, hi in copy_limits(settings):
        blocks = _blocks(sec, key)
        n = len(blocks)
        if n == 0:
            continue
        if lo and n < lo:
            out[key] = "%d, needs %d%s" % (n, lo, "-%d" % hi if hi else "+")
        elif hi and n > hi:
            out[key] = "%d, allows %d-%d" % (n, lo or 1, hi)
        longest = max((len(b) for b in blocks), default=0)
        if chars and longest > chars:
            out[key] = "%d over %d" % (longest, chars)
    return out


def _table_state(d):
    """(how many products the table says it holds, which columns it has).

    ORDER IS DELIBERATELY DROPPED. Columns come back as a frozenset, because a
    table whose dimensions are dealt in a different sequence is the same table
    - and this app already treats re-ordering as layout rather than an edit
    everywhere else. What is worth raising a page for is a column appearing or
    disappearing, or the number of products moving.

    The count is the page's own "N matching results" readout. A row count
    would be wrong: the components nest and carry filter rows, so counting
    rows reports 84 where the page says 7.

    Returns (None, None) when the page has no table at all, which is different
    from a table with nothing in it.
    """
    f = (_load_sections(d).get("flags") or {}).get("product_table") or {}
    if not f.get("exists"):
        return (None, None)
    cols = f.get("columns")
    return (f.get("count"),
            frozenset(cols) if isinstance(cols, list) else None)


def _community_state(snap_dir):
    """The developer-community widget's tri-state on this capture:
    'absent' (not on the page) / 'empty' (widget there, no questions) /
    'has discussions'. THE STATE is what is tracked - which threads sit in
    the widget, their dates and their pixels are live-feed churn and are
    excluded from every other signal on purpose."""
    dc = (_load_sections(snap_dir).get("flags") or {}).get(
        "developer_community") or {}
    if not dc.get("exists"):
        return "absent"
    # 'has_content' is read from the widget's placeholder TEXT, which is
    # unreliable mid-load: one capture caught the box still resolving
    # (has_content=False) while it already held a thread, the next caught it
    # settled with five - the SAME widget flagged 'empty -> has discussions'
    # on 232 pages. The extracted thread LIST is the structured, reliable
    # signal: any thread => discussions, whatever the placeholder said.
    if (dc.get("threads") or []) or dc.get("has_content"):
        return "has discussions"
    return "empty"


def library_diff_detail(old_dir, new_dir):
    """The reviewable version of library_diff: WHICH items, not just counts.

    Built on the exact same primitives (_item_id / _item_body / _NOT_CONTENT /
    the stock exemption), so the diff VIEW can never disagree with the
    detector again - change #5584 was flagged 'Documents (~1)' by the detector
    while the view's own parallel implementation (added/removed only, raw
    title keys) rendered nothing. One source of truth; anything this returns
    is exactly what library_diff counted, item by item.

    Returns {cat: {"added": [item...], "removed": [item...],
                   "changed": [{"title","url","fields":[(name,old,new)...]}]}}
    String categories (price) come back as a single changed entry whose
    fields carry the old/new strings (stock count stripped, like the count)."""
    old, new = _load_sections(old_dir), _load_sections(new_dir)
    out = {}
    for cat in sorted(set(old) | set(new)):
        if cat in _NOT_CONTENT:
            continue
        a_raw, b_raw = old.get(cat), new.get(cat)
        if isinstance(a_raw, str) or isinstance(b_raw, str):   # price
            a_s = _STOCK_RE.sub("", str(a_raw or "")).split()
            b_s = _STOCK_RE.sub("", str(b_raw or "")).split()
            if a_s != b_s:
                out[cat] = {"added": [], "removed": [], "changed": [{
                    "title": cat, "url": "",
                    "fields": [(cat, " ".join(a_s), " ".join(b_s))]}]}
            continue
        a = {_item_id(d): d for d in (a_raw or []) if isinstance(d, dict)}
        b = {_item_id(d): d for d in (b_raw or []) if isinstance(d, dict)}
        added = [b[k] for k in b if k not in a]
        removed = [a[k] for k in a if k not in b]
        changed = []
        for k in set(a) & set(b):
            fields = []
            ba, bb = _item_body(a[k]), _item_body(b[k])
            for f in sorted(set(ba) | set(bb)):
                if ba.get(f, "") != bb.get(f, ""):
                    fields.append((f, ba.get(f, ""), bb.get(f, "")))
            ia, ib = str(a[k].get("img", "")), str(b[k].get("img", ""))
            if _real_img(ia) and _real_img(ib) and ia != ib:
                fields.append(("img", ia, ib))
            if fields:
                changed.append({"title": b[k].get("title", ""),
                                "url": b[k].get("url", ""), "fields": fields})
        if added or removed or changed:
            out[cat] = {"added": added, "removed": removed, "changed": changed}
    return out


def library_diff(old_dir, new_dir):
    """Every item of every component: added, removed, or changed in place.

    This is what makes the watch a CONTENT review and not just a page review.
    A document repointed to a different file, a training that changed date, a
    board that quietly disappeared from one tab - none of those need move a
    single visible word, and before this none of them raised anything.
    """
    old, new = _load_sections(old_dir), _load_sections(new_dir)
    per, added, removed, changed = {}, 0, 0, 0
    for cat in sorted(set(old) | set(new)):
        if cat in _NOT_CONTENT:
            continue
        a_raw, b_raw = old.get(cat), new.get(cat)
        if isinstance(a_raw, str) or isinstance(b_raw, str):   # price
            a_s = _STOCK_RE.sub("", str(a_raw or "")).split()
            b_s = _STOCK_RE.sub("", str(b_raw or "")).split()
            if a_s != b_s:
                per[cat] = {"added": 0, "removed": 0, "changed": 1}
                changed += 1
            continue
        a = {_item_id(d): d for d in (a_raw or []) if isinstance(d, dict)}
        b = {_item_id(d): d for d in (b_raw or []) if isinstance(d, dict)}
        n_add = len(set(b) - set(a))
        n_rem = len(set(a) - set(b))
        n_chg = 0
        for k in set(a) & set(b):
            if _item_body(a[k]) != _item_body(b[k]):
                n_chg += 1
                continue
            ia, ib = str(a[k].get("img", "")), str(b[k].get("img", ""))
            if _real_img(ia) and _real_img(ib) and ia != ib:
                n_chg += 1
        if n_add or n_rem or n_chg:
            per[cat] = {"added": n_add, "removed": n_rem, "changed": n_chg}
            added += n_add
            removed += n_rem
            changed += n_chg
    return {"changed": bool(per), "per_section": per,
            "added": added, "removed": removed, "modified": changed}


def compare_snapshots(old_dir, new_dir, change_dir=None, settings=None):
    """Full comparison. Returns a dict describing what changed."""
    settings = settings or {}
    ignore_patterns = (settings.get("ignore_patterns") or "").splitlines()
    ignore_image_patterns = (settings.get("ignore_image_patterns") or "").splitlines()
    visual_threshold = float(settings.get("visual_threshold", 0.5))
    height_threshold = int(settings.get("height_threshold", 80))

    # ACROSS A TEXT-EXTRACTOR REVISION the text is not comparable: when the
    # baseline was captured under an older TEXT_REV, its text.txt was built by a
    # different walk (e.g. one that still included the forum/news feeds), so a
    # line diff would report every page that was re-normalised as an "update".
    # Skip the text signal for that one comparison and let the new capture
    # become the clean baseline; the image / screenshot / library signals still
    # apply, so a genuine change in the same run is not missed, and the very
    # next run - both sides on the new rev - resumes normal text diffing.
    t_reordered = False
    if _text_rev(old_dir) != _text_rev(new_dir):
        t_changed, t_added, t_removed = False, 0, 0
    else:
        t_changed, t_added, t_removed, _ = text_diff(old_dir, new_dir, ignore_patterns)
        # SAME LINES, NEW ORDER is not a content change. The line diff is
        # positional, so a block that merely moved reads as removals plus
        # additions - the homepage reported the same four AURIX part numbers
        # cycling by one position, and the interactive-expansion pass returns
        # its panels in whatever order they hydrate. Compare the line MULTISET:
        # equal multisets mean nothing was added, removed or edited, only
        # rearranged. Anything genuinely new, gone or reworded changes the
        # counts and still flags. Duplication changes them too, so a block
        # rendered twice is not mistaken for a move.
        if t_changed:
            if Counter(_load_text_lines(old_dir, ignore_patterns)) == \
               Counter(_load_text_lines(new_dir, ignore_patterns)):
                t_reordered, t_changed = True, False
    # ACROSS AN IMAGE-COLLECTOR REVISION the URL sets are not comparable (the
    # rev-2 inventory dropped the news-box thumbnails and legacy community
    # avatars), so the removals would flag every re-normalised page once.
    # Same silent re-baseline as the text and screenshot revisions.
    if _img_rev(old_dir) != _img_rev(new_dir):
        i_changed, i_added, i_removed = False, [], []
    else:
        i_changed, i_added, i_removed = images_diff(old_dir, new_dir,
                                                    ignore_image_patterns)
    # ACROSS A STRUCTURED-READ REVISION the sections are not comparable, the
    # same as the text, the images and the screenshot. This was the one signal
    # without a stamp, and when rev 5 moved the price/stock block it was also
    # the only signal left un-suppressed - so every page came up with a section
    # difference and nothing a human could see behind it.
    if _sections_rev(old_dir) != _sections_rev(new_dir):
        lib = library_diff(old_dir, old_dir)      # itself: no differences
    else:
        lib = library_diff(old_dir, new_dir)

    # The developer-community widget is tracked as a TRI-STATE, every run:
    # absent / empty / has discussions. A transition between those states is
    # an update worth reviewing (the community arrived, emptied, or vanished);
    # everything INSIDE the widget - thread titles, authors, dates, avatars,
    # pixels - is live-feed churn and is excluded from the text, image and
    # screenshot signals on purpose.
    comm_from = _community_state(old_dir)
    comm_to = _community_state(new_dir)
    comm_changed = comm_from != comm_to

    # THE PRODUCT TABLE'S SHAPE, compared across a sections revision the same
    # way everything else is: rev 4 first stored it, so a rev-3 baseline has
    # nothing to compare and must not read as "the table lost all its columns".
    tbl_from = tbl_to = (None, None)
    tbl_changed = False
    if _sections_rev(old_dir) == _sections_rev(new_dir):
        tbl_from, tbl_to = _table_state(old_dir), _table_state(new_dir)
        tbl_changed = tbl_from != tbl_to

    # ACROSS A SCREENSHOT REVISION the picture is not comparable either: the
    # feed-hidden shot (rev 2) differs from the old full one everywhere the
    # community/news boxes used to be, so a pixel diff would flag every page
    # that was re-normalised. Suppress the visual signal for that one
    # comparison - text/images/library still apply - and the next run, both on
    # the new shot_rev, resumes normal pixel diffing (now free of the feeds).
    if _shot_rev(old_dir) != _shot_rev(new_dir):
        ratio_pct, height_delta, n_regions = 0.0, 0, 0
    else:
        ratio_pct, height_delta, n_regions = visual_diff(old_dir, new_dir,
                                                          change_dir)
    v_changed = ratio_pct >= visual_threshold or height_delta >= height_threshold

    parts = []
    if t_changed:
        parts.append(f"text: +{t_added}/-{t_removed} lines")
    if i_changed:
        parts.append(f"images: +{len(i_added)}/-{len(i_removed)}")
    if lib["changed"]:
        parts.append(f"library: +{lib['added']}/-{lib['removed']}"
                     f"/~{lib['modified']}")
    if v_changed:
        parts.append(f"visual: {n_regions} area(s), {ratio_pct:.2f}% of pixels")
    if comm_changed:
        parts.append(f"developer community: {comm_from} → {comm_to}")
    if tbl_changed:
        n0, c0 = tbl_from
        n1, c1 = tbl_to
        bits = []
        if n0 != n1:
            bits.append("%s → %s products" % ("?" if n0 is None else n0,
                                              "?" if n1 is None else n1))
        gone = sorted((c0 or frozenset()) - (c1 or frozenset()))
        came = sorted((c1 or frozenset()) - (c0 or frozenset()))
        if came:
            bits.append("columns added: " + ", ".join(came[:4]))
        if gone:
            bits.append("columns removed: " + ", ".join(gone[:4]))
        parts.append("products table: " + ("; ".join(bits) or "changed"))

    return {
        # the library counts too: an item can be repointed, redated or removed
        # without moving one visible word or one pixel over the threshold -
        # and so does a community tri-state transition
        "any_changed": (t_changed or i_changed or v_changed or lib["changed"]
                        or comm_changed or tbl_changed),
        "table_changed": tbl_changed,
        "table_from": tbl_from,
        "table_to": tbl_to,
        "community_changed": comm_changed,
        "community_from": comm_from,
        "community_to": comm_to,
        "library_changed": lib["changed"],
        "library": lib,
        "text_changed": t_changed,
        "text_reordered": t_reordered,
        "lines_added": t_added,
        "lines_removed": t_removed,
        "images_changed": i_changed,
        "images_added": len(i_added),
        "images_removed": len(i_removed),
        "images_added_list": i_added,
        "images_removed_list": i_removed,
        "visual_changed": v_changed,
        "pixel_ratio": round(ratio_pct, 4),
        "height_delta": height_delta,
        "summary": "; ".join(parts) if parts else "no change",
    }
