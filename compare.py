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


def _region_strip(old_img, new_img, box, out_path, pad=40):
    """Side-by-side close-up of one changed area: left = before, right = after."""
    max_w = min(old_img.width, new_img.width)
    max_h = min(old_img.height, new_img.height)
    x0 = max(0, box[0] - pad)
    y0 = max(0, box[1] - pad)
    x1 = min(max_w, box[2] + pad)
    y1 = min(max_h, box[3] + pad)
    o = old_img.crop((x0, y0, x1, y1))
    n = new_img.crop((x0, y0, x1, y1))
    w, h = o.size
    header, divider = 26, 10
    canvas = Image.new("RGB", (w * 2 + divider, h + header), (245, 246, 248))
    draw = ImageDraw.Draw(canvas)
    draw.text((6, 6), "BEFORE", fill=(150, 20, 40))
    draw.text((w + divider + 6, 6), "AFTER", fill=(20, 110, 50))
    canvas.paste(o, (0, header))
    canvas.paste(n, (w + divider, header))
    canvas.save(out_path, optimize=True)


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
    a = Image.open(a_path).convert("RGB")
    b = Image.open(b_path).convert("RGB")
    w = min(a.width, b.width)
    h = min(a.height, b.height)
    height_delta = abs(a.height - b.height)
    if w == 0 or h == 0:
        return 0.0, height_delta, 0
    a_c = a.crop((0, 0, w, h))
    b_c = b.crop((0, 0, w, h))
    diff = ImageChops.difference(a_c, b_c).convert("L")
    mask = diff.point(lambda p: 255 if p > PIXEL_NOISE_THRESHOLD else 0)
    changed_px = mask.histogram()[255]
    ratio_pct = 100.0 * changed_px / float(w * h)

    boxes = _find_regions(mask) if changed_px else []
    if change_dir and boxes:
        change_dir = Path(change_dir)
        change_dir.mkdir(parents=True, exist_ok=True)
        _draw_boxes(a, boxes, change_dir / "old_boxed.png")
        _draw_boxes(b, boxes, change_dir / "new_boxed.png")
        for i, box in enumerate(boxes):
            _region_strip(a, b, box, change_dir / f"region_{i:02d}.png")
    return ratio_pct, height_delta, len(boxes)


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


def _item_id(d):
    """What makes an item itself: its name and where it points."""
    return (str(d.get("title", "")).strip().lower(),
            str(d.get("url", "")).strip())


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
            if ia and ib and ia != ib:
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

    t_changed, t_added, t_removed, _ = text_diff(old_dir, new_dir, ignore_patterns)
    # SAME LINES, NEW ORDER is not a content change. The line diff is
    # positional, so a block that merely moved reads as removals plus
    # additions - the homepage reported the same four AURIX part numbers
    # cycling by one position, and the interactive-expansion pass returns its
    # panels in whatever order they hydrate. Compare the line MULTISET: equal
    # multisets mean nothing was added, removed or edited, only rearranged.
    # Anything genuinely new, gone or reworded changes the counts and still
    # flags. Duplication changes them too, so a block rendered twice is not
    # mistaken for a move.
    t_reordered = False
    if t_changed:
        if Counter(_load_text_lines(old_dir, ignore_patterns)) == \
           Counter(_load_text_lines(new_dir, ignore_patterns)):
            t_reordered, t_changed = True, False
    i_changed, i_added, i_removed = images_diff(old_dir, new_dir,
                                                ignore_image_patterns)
    lib = library_diff(old_dir, new_dir)

    ratio_pct, height_delta, n_regions = visual_diff(old_dir, new_dir, change_dir)
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

    return {
        # the library counts too: an item can be repointed, redated or removed
        # without moving one visible word or one pixel over the threshold
        "any_changed": t_changed or i_changed or v_changed or lib["changed"],
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
