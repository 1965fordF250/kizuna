"""Draw the app icon and write icon.png / icon.ico / icon.icns into the given folder.

    python packaging/make_icon.py build
Needs Pillow (in requirements-build.txt).

The icon is a bow: two cords (white and red, the two releases) tied into one knot on a
charcoal tile. The same drawing is in src/kizuna/web/index.html (header logo and tab icon)
as SVG; keep the two in step if it changes.
"""
import os
import sys

from PIL import Image, ImageDraw

S = 1024
K = 4                                   # drawn 4x larger, then scaled down for smooth edges
TOP, BOTTOM = (51, 53, 59), (18, 19, 22)
WHITE, RED, KNOT_LINE = (255, 255, 255, 255), (232, 65, 58, 255), (18, 19, 22, 255)
CORD = 58

# cubic Bezier paths in a 1024 x 1024 box: (start, (c1, c2, end), (c1, c2, end), ...)
LEFT_LOOP = ((512, 505), ((430, 380), (250, 300), (214, 420)), ((182, 530), (360, 590), (512, 525)))
RIGHT_LOOP = ((512, 505), ((594, 380), (774, 300), (810, 420)), ((842, 530), (664, 590), (512, 525)))
LEFT_TAIL = ((488, 540), ((455, 640), (410, 735), (350, 835)),)
RIGHT_TAIL = ((536, 540), ((569, 640), (614, 735), (674, 835)),)


def _points(path, steps=80):
    (x, y), segs = path[0], path[1:]
    pts = [(x, y)]
    for (x1, y1), (x2, y2), (x3, y3) in segs:
        x0, y0 = pts[-1]
        for i in range(1, steps + 1):
            t = i / steps
            a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
            pts.append((a * x0 + b * x1 + c * x2 + d * x3, a * y0 + b * y1 + c * y2 + d * y3))
    return [(px * K, py * K) for px, py in pts]


def _cord(d, path, colour):
    pts, w = _points(path), CORD * K
    d.line(pts, fill=colour, width=w, joint="curve")
    for x, y in pts:                    # round every joint and both ends
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=colour)


def draw():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([40, 40, S - 40, S - 40], radius=220, fill=255)
    img.paste(grad, (0, 0), mask)

    big = Image.new("RGBA", (S * K, S * K), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    _cord(d, LEFT_TAIL, WHITE)
    _cord(d, RIGHT_TAIL, RED)
    _cord(d, LEFT_LOOP, WHITE)
    _cord(d, RIGHT_LOOP, RED)
    # the knot: a rounded block with two red bands
    d.rounded_rectangle([445 * K, 445 * K, 579 * K, 591 * K], radius=51 * K, fill=KNOT_LINE)
    d.rounded_rectangle([459 * K, 459 * K, 565 * K, 577 * K], radius=37 * K, fill=WHITE)
    for y in (500, 538):
        d.rounded_rectangle([472 * K, (y - 6) * K, 552 * K, (y + 6) * K], radius=6 * K, fill=RED)
    img.alpha_composite(big.resize((S, S), Image.LANCZOS))
    return img


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    img = draw()
    img.save(os.path.join(out_dir, "icon.png"))
    img.save(os.path.join(out_dir, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.save(os.path.join(out_dir, "icon.icns"))
    print("icons written to", out_dir)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "build")
