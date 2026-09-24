"""Draws the app icon (build/icon.ico, build/public/icon.png) — run once after a design change."""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024
HERE = Path(__file__).resolve().parent.parent
STOPS = [(0.0, (94, 234, 212)), (0.55, (96, 165, 250)), (1.0, (192, 132, 252))]


def grad(t: float) -> tuple[int, int, int]:
    for (a, ca), (b, cb) in zip(STOPS, STOPS[1:]):
        if t <= b:
            k = (t - a) / (b - a)
            return tuple(round(x + (y - x) * k) for x, y in zip(ca, cb))
    return STOPS[-1][1]


def gradient_layer() -> Image.Image:
    img = Image.new("RGB", (S, S))
    px = img.load()
    for y in range(S):
        for x in range(S):
            px[x, y] = grad((x + y) / (2 * S))
    return img


# background: dark rounded square
bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(bg).rounded_rectangle((40, 40, S - 40, S - 40), radius=220, fill=(16, 19, 26, 255))

# the "G": an open arc plus a bar, drawn as a mask and filled with the gradient
mask = Image.new("L", (S, S), 0)
d = ImageDraw.Draw(mask)
c, r, w = S / 2, 290, 96
d.arc((c - r, c - r, c + r, c + r), start=20, end=330, fill=255, width=w)
d.rounded_rectangle((c + 10, c - w / 2 + 40, c + r + w / 2 - 4, c + w / 2 + 40), radius=w / 2, fill=255)
cap = 58
ang = math.radians(330)
cx, cy = c + (r - w / 2) * math.cos(ang), c + (r - w / 2) * math.sin(ang)
d.ellipse((cx - cap, cy - cap, cx + cap, cy + cap), fill=255)
glow = mask.filter(ImageFilter.GaussianBlur(40))

g = gradient_layer()
halo = Image.new("RGBA", (S, S), (0, 0, 0, 0))
halo.paste(g, (0, 0), glow.point(lambda v: v * 0.55))
shape = Image.new("RGBA", (S, S), (0, 0, 0, 0))
shape.paste(g, (0, 0), mask)
out = Image.alpha_composite(Image.alpha_composite(bg, halo), shape)

(HERE / "build" / "public").mkdir(parents=True, exist_ok=True)
out.resize((512, 512), Image.LANCZOS).save(HERE / "build" / "public" / "icon.png")
out.resize((256, 256), Image.LANCZOS).save(
    HERE / "build" / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("ok")
