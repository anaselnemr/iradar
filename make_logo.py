"""Draw the iRadar mark: the header logo, the tab icon, the phone icon.

Run it when the mark changes; the output is committed, so nothing generates
at request time.

    python make_logo.py

The letters are optically sized rather than one drawing scaled down. At 16px
the version that looks right at 256 goes to mush - the R closes up and the i
merges into it - so the small sizes are drawn with a bigger glyph in a tighter
tile. Every size is drawn at 8x and resampled, which is what keeps the corner
radius and the letter edges clean.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "static"

# the app's own accent pair, so the mark is the same colour as the interface
A = (0x6d, 0x78, 0xe0)
B = (0x8b, 0x5c, 0xf6)
TEXT = "iR"
FONTS = ("segoeuib.ttf", "arialbd.ttf", "calibrib.ttf", "DejaVuSans-Bold.ttf")


def _font(px):
    for name in FONTS:
        for root in (r"C:\Windows\Fonts", "/usr/share/fonts/truetype/dejavu"):
            try:
                return ImageFont.truetype(str(Path(root) / name), px)
            except OSError:
                continue
    return ImageFont.load_default()


def mark(size, text_frac, radius_frac, supersample=8):
    """The tile at `size` px, drawn big and resampled down."""
    n = size * supersample
    grad = Image.new("RGB", (n, n))
    px = grad.load()
    for y in range(n):
        # 135 degrees means the colour is constant along the anti-diagonal, so
        # the whole row can be walked with one interpolation per pixel
        for x in range(n):
            t = (x + y) / (2.0 * (n - 1))
            px[x, y] = (round(A[0] + (B[0] - A[0]) * t),
                        round(A[1] + (B[1] - A[1]) * t),
                        round(A[2] + (B[2] - A[2]) * t))
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, n - 1, n - 1], radius=int(n * radius_frac), fill=255)
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    f = _font(int(n * text_frac))
    box = d.textbbox((0, 0), TEXT, font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    # centred on the ink, not on the line box, then nudged up a touch: the dot
    # of the i sits above the cap line and drags the optical centre down
    d.text(((n - w) / 2 - box[0], (n - h) / 2 - box[1] - n * 0.012),
           TEXT, font=f, fill="white")
    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(exist_ok=True)
    # big: room to breathe. small: bigger letters in a tighter tile, or the
    # R fills in and it reads as a smudge
    big = dict(text_frac=0.55, radius_frac=0.235)
    small = dict(text_frac=0.62, radius_frac=0.20)

    mark(256, **big).save(OUT / "logo.png")
    mark(180, **big).save(OUT / "apple-touch-icon.png")
    icons = [mark(s, **small) for s in (16, 32, 48, 64)]
    icons[-1].save(OUT / "favicon.ico",
                   sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    print("wrote logo.png, apple-touch-icon.png, favicon.ico")


if __name__ == "__main__":
    main()
