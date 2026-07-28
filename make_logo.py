"""Draw the iRadar mark: the header logo, the tab icon, the phone icon.

Run it when the mark changes; the output is committed, so nothing generates
at request time.

    python make_logo.py

The mark is "iR" with a radar sweep behind it. Letters, because a pure radar
dial is lovely at 200px and an unreadable smudge at the 16px a browser tab
actually gives you - and the tab is where a favicon does its job. The sweep
carries the idea; the letters carry the identity.

Two versions, because the tile has to sit on whatever is behind it: the
gradient one on light backgrounds, a near-black one for dark mode.

Everything is optically sized rather than one drawing scaled down. At 16px the
version that looks right at 256 goes to mush - the R's counter closes and the i
merges into it - so the small sizes get a bigger glyph in a tighter tile. Every
size is drawn at 6x and resampled, which is what keeps the corner radius, the
sweep edge and the letter edges clean.
"""
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

OUT = Path(__file__).parent / "static"
SS = 6                       # supersample factor

# the app's own accent pair, so the mark is the same colour as the interface
INDIGO = (0x6d, 0x78, 0xe0)
VIOLET = (0x8b, 0x5c, 0xf6)
WHITE = (255, 255, 255)
BLACK = (0x0b, 0x0b, 0x10)

TEXT = "iR"
FONTS = ("segoeuib.ttf", "arialbd.ttf", "calibrib.ttf", "DejaVuSans-Bold.ttf")
FONT_DIRS = (r"C:\Windows\Fonts", "/usr/share/fonts/truetype/dejavu")


def _font(px):
    for name in FONTS:
        for root in FONT_DIRS:
            try:
                return ImageFont.truetype(str(Path(root) / name), px)
            except OSError:
                continue
    return ImageFont.load_default()


def _tile(n, top, bottom, radius_frac):
    """The rounded square, filled with a 135-degree gradient."""
    if top == bottom:
        grad = Image.new("RGB", (n, n), top)
    else:
        grad = Image.new("RGB", (n, n))
        px = grad.load()
        for y in range(n):
            # 135 degrees means the colour is constant along the anti-diagonal
            for x in range(n):
                t = (x + y) / (2.0 * (n - 1))
                px[x, y] = tuple(round(top[i] + (bottom[i] - top[i]) * t)
                                 for i in range(3))
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, n - 1, n - 1], radius=int(n * radius_frac), fill=255)
    out = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def _sweep(im, ink, alpha, start=-96, span=74, steps=26):
    """The radar wedge, fading out behind its leading edge.

    Built from a stack of thin pie slices rather than one shape: a wedge that
    is solid all the way round reads as a slice of pie, and it is the fade
    that makes it read as something moving.
    """
    n = im.size[0]
    c, r = n / 2, 0.435 * n
    over = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    for i in range(steps):
        layer = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        a = int(alpha * (i / (steps - 1)) ** 1.9)
        ImageDraw.Draw(layer).pieslice(
            [c - r, c - r, c + r, c + r],
            start + span * i / steps,
            # the +0.6 overlap hides the hairline seams between slices
            start + span * (i + 1) / steps + 0.6,
            fill=ink + (a,))
        over = Image.alpha_composite(over, layer)
    return Image.alpha_composite(im, over)


def _ink_rows(mask, x0, x1):
    """Which rows carry ink between two columns, as (start, end) bands."""
    px = mask.load()
    rows, bands, run = [], [], None
    for y in range(mask.size[1]):
        has = any(px[x, y] > 40 for x in range(x0, x1))
        rows.append(has)
    for y, has in enumerate(rows):
        if has and run is None:
            run = y
        elif not has and run is not None:
            bands.append((run, y))
            run = None
    if run is not None:
        bands.append((run, mask.size[1]))
    return bands


def _letters(im, colour, fade_to, text_frac):
    """Draw "iR", with the dot of the i fading into the tile behind it.

    The dot is the one piece of the wordmark that is pure decoration - it
    carries no shape you need to read - so it is where a gradient can go
    without costing legibility. It is isolated by rendering the text to a mask
    and taking the topmost separate band of ink inside the i's columns; the
    gap between the dot and the stem is what makes that reliable, in any font.
    """
    n = im.size[0]
    f = _font(int(n * text_frac))
    scratch = Image.new("L", (n, n), 0)
    sd = ImageDraw.Draw(scratch)
    box = sd.textbbox((0, 0), TEXT, font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    # centred on the ink, not the line box, then nudged up a touch: the dot of
    # the i sits above the cap line and drags the optical centre down
    x0 = (n - w) / 2 - box[0]
    y0 = (n - h) / 2 - box[1] - n * 0.012
    sd.text((x0, y0), TEXT, font=f, fill=255)

    i_left = max(0, int(x0))
    i_right = min(n, int(x0 + sd.textlength("i", font=f)))
    bands = _ink_rows(scratch, i_left, i_right)

    dot = Image.new("L", (n, n), 0)
    if len(bands) >= 2:
        top, bottom = bands[0]
        crop = scratch.crop((i_left, top, i_right, bottom))
        dot.paste(crop, (i_left, top))
    body = ImageChops.subtract(scratch, dot)

    solid = Image.new("RGBA", (n, n), colour + (255,))
    im = Image.composite(solid, im, body)

    if dot.getbbox():
        grad = Image.new("RGBA", (n, n))
        gp = grad.load()
        bb = dot.getbbox()
        span = max(1, (bb[2] - bb[0]) + (bb[3] - bb[1]))
        for y in range(bb[1], bb[3]):
            for x in range(bb[0], bb[2]):
                t = ((x - bb[0]) + (y - bb[1])) / span
                gp[x, y] = (round(colour[0] + (fade_to[0] - colour[0]) * t),
                            round(colour[1] + (fade_to[1] - colour[1]) * t),
                            round(colour[2] + (fade_to[2] - colour[2]) * t), 255)
        im = Image.composite(grad, im, dot)
    return im


# The same tile, a touch deeper, so dark mode is the same mark rather than a
# different one - only the ink flips.
#
# Not much deeper, though: the ink that goes on it is BLACK, so darkening the
# tile takes contrast away rather than adding it. At 0.72 the letters sat at
# 2.8:1 and went soft at tab size; 0.88 keeps the tile clearly the deeper of
# the two while the letters read.
def _darker(c, k=0.88):
    return tuple(round(v * k) for v in c)


def mark(size, dark=False, text_frac=0.55, radius_frac=0.235):
    n = size * SS
    if dark:
        top, bottom = _darker(INDIGO), _darker(VIOLET)
        ink, sweep_ink, sweep_a = BLACK, BLACK, 52
    else:
        top, bottom, ink, sweep_ink, sweep_a = INDIGO, VIOLET, WHITE, WHITE, 62
    im = _tile(n, top, bottom, radius_frac)
    im = _sweep(im, sweep_ink, sweep_a)
    # the dot fades from the letter colour into the tile under it
    im = _letters(im, ink, bottom, text_frac)
    return im.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(exist_ok=True)
    # big: room to breathe. small: bigger letters in a tighter tile, or the R
    # fills in and it reads as a smudge
    big = dict(text_frac=0.55, radius_frac=0.235)
    small = dict(text_frac=0.62, radius_frac=0.20)

    mark(256, **big).save(OUT / "logo.png")
    mark(256, dark=True, **big).save(OUT / "logo-dark.png")
    # home screens sit on the user's own wallpaper, so the gradient one - the
    # near-black tile disappears on a dark wallpaper
    mark(180, **big).save(OUT / "apple-touch-icon.png")

    sizes = [(16, 16), (32, 32), (48, 48), (64, 64)]
    mark(64, **small).save(OUT / "favicon.ico", sizes=sizes)
    mark(64, dark=True, **small).save(OUT / "favicon-dark.ico", sizes=sizes)
    print("wrote logo.png, logo-dark.png, apple-touch-icon.png, "
          "favicon.ico, favicon-dark.ico")


if __name__ == "__main__":
    main()
