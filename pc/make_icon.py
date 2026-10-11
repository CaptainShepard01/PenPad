"""
Draws penpad.ico (Windows app icon). Same design as android/res/drawable/ic_launcher_foreground.xml.

Run:  uv run --no-project --with pillow make_icon.py
"""
import math
import os

from PIL import Image, ImageDraw

BG, ACCENT, BODY, CONE = "#1E2230", "#4FC3F7", "#F2F4F8", "#C7CDD6"
SS = 1024             # render size; downscaled for each icon size
ZOOM = 1.35           # the Android art sits in a 66/108 safe zone; fill more of the tile here
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def to_px(x, y, rotate=False):
    """108-unit Android canvas -> render pixels, optionally rotated 45° clockwise about the center."""
    dx, dy = x - 54, y - 54
    if rotate:
        a = math.radians(45)
        dx, dy = dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)
    scale = SS / 108 * ZOOM
    return SS / 2 + dx * scale, SS / 2 + dy * scale


def bezier(p0, p1, p2, p3, steps=40):
    return [tuple((1 - t) ** 3 * a + 3 * (1 - t) ** 2 * t * b + 3 * (1 - t) * t ** 2 * c + t ** 3 * d
                  for a, b, c, d in zip(p0, p1, p2, p3))
            for t in (i / steps for i in range(steps + 1))]


def render():
    img = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = SS * 0.04
    d.rounded_rectangle([m, m, SS - m, SS - m], radius=SS * 0.22, fill=BG)

    wave = bezier((37, 71), (42, 65), (48, 65), (53, 71)) + bezier((53, 71), (58, 77), (64, 77), (69, 71))
    pts = [to_px(x, y) for x, y in wave]
    width = round(3.5 * SS / 108 * ZOOM)
    d.line(pts, fill=ACCENT, width=width, joint="curve")
    for p in (pts[0], pts[-1]):  # round caps
        d.ellipse([p[0] - width / 2, p[1] - width / 2, p[0] + width / 2, p[1] + width / 2], fill=ACCENT)

    for color, shape in [(ACCENT, [(48, 26), (60, 26), (60, 34), (48, 34)]),
                         (BODY, [(48, 34), (60, 34), (60, 64), (48, 64)]),
                         (CONE, [(48, 64), (60, 64), (54, 78)]),
                         (ACCENT, [(51.4, 72), (56.6, 72), (54, 78)])]:
        d.polygon([to_px(x, y, rotate=True) for x, y in shape], fill=color)
    return img


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "penpad.ico")
    big = render().resize((256, 256), Image.LANCZOS)
    big.save(out, sizes=[(s, s) for s in SIZES])
    print("wrote", out)
