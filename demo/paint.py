"""Made-up artworks and exhibition photos for the demo, drawn with Pillow.

Deterministic: the same style, seed and size always give the same pixels, so the demo
looks the same on every machine and every reset. Nothing here is anyone's real work.
Randomness comes only from random.Random(seed); Pillow's own noise generator is not used
because it does not take a seed.

Styles (one per demo project):
  tide     horizontal bands of sea and sky, laid on with short brush strokes
  maps     ink and coloured-pencil maps on paper: fields, a river, a road, seen from above
  weave    a woven textile: warp and weft threads with dyed stripes
  rooms    soft-edged colour fields, nested like rooms seen through doors
"""

from __future__ import annotations

import math
import random

from PIL import Image, ImageChops, ImageDraw, ImageFilter

RGB = tuple[int, int, int]

PALETTES: dict[str, list[list[RGB]]] = {
    "tide": [
        [(214, 222, 226), (176, 196, 205), (120, 150, 166), (78, 104, 122), (196, 186, 160), (160, 148, 120)],
        [(232, 214, 196), (214, 170, 150), (150, 140, 160), (92, 102, 132), (60, 70, 98), (180, 160, 140)],
        [(226, 230, 222), (190, 204, 196), (128, 160, 158), (70, 112, 118), (210, 200, 176), (140, 128, 104)],
    ],
    "maps": [   # ink first, then the coloured pencils of the fields, then the river
        [(52, 44, 38), (150, 170, 130), (205, 172, 112), (206, 152, 140), (176, 192, 150), (128, 156, 176)],
        [(34, 42, 58), (170, 186, 140), (214, 186, 128), (190, 150, 160), (150, 166, 120), (118, 146, 170)],
    ],
    "weave": [
        [(196, 92, 60), (232, 196, 140), (64, 88, 110), (226, 222, 210), (150, 58, 48)],
        [(88, 120, 92), (210, 190, 120), (60, 64, 80), (230, 226, 214), (170, 110, 70)],
        [(120, 70, 100), (220, 180, 170), (70, 100, 120), (236, 230, 220), (200, 150, 80)],
    ],
    "rooms": [
        [(214, 120, 84), (238, 196, 150), (110, 70, 64), (250, 236, 214)],
        [(70, 104, 120), (160, 190, 196), (230, 220, 200), (36, 52, 64)],
        [(150, 160, 96), (220, 214, 160), (96, 84, 60), (244, 238, 220)],
        [(170, 80, 96), (230, 170, 160), (70, 60, 80), (246, 232, 226)],
    ],
}


def _jitter(c: RGB, rng: random.Random, amount: int) -> RGB:
    return tuple(max(0, min(255, v + rng.randint(-amount, amount))) for v in c)  # type: ignore[return-value]


def _noise(size: tuple[int, int], rng: random.Random, cell: int) -> Image.Image:
    """Smooth grey noise: random bytes at 1/cell of the size, scaled up."""
    w, h = max(2, size[0] // cell), max(2, size[1] // cell)
    small = Image.frombytes("L", (w, h), rng.randbytes(w * h))
    return small.resize(size, Image.BICUBIC)


def _grain(img: Image.Image, rng: random.Random, strength: float, cell: int = 2) -> Image.Image:
    """Canvas or paper grain: soft-light a fine noise over the picture."""
    n = _noise(img.size, rng, cell).point(lambda v: int(128 + (v - 128) * strength))
    return ImageChops.soft_light(img, Image.merge("RGB", (n, n, n)))


def _strokes(layer: ImageDraw.ImageDraw, rng: random.Random, box: tuple[int, int, int, int], color: RGB,
             count: int, length: tuple[int, int], width: tuple[int, int], slope: float, alpha: tuple[int, int]) -> None:
    x0, y0, x1, y1 = box
    for _ in range(count):
        x, y = rng.uniform(x0, x1), rng.uniform(y0, y1)
        n = rng.uniform(*length)
        a = rng.uniform(-slope, slope)
        c = _jitter(color, rng, 18)
        layer.line([(x, y), (x + n * math.cos(a), y + n * math.sin(a))],
                   fill=(*c, rng.randint(*alpha)), width=rng.randint(*width))


# --- works ------------------------------------------------------------------------

def tide(seed: int, size: tuple[int, int]) -> Image.Image:
    rng = random.Random(seed)
    pal = PALETTES["tide"][seed % len(PALETTES["tide"])]
    w, h = size
    img = Image.new("RGB", size, pal[0])
    base = ImageDraw.Draw(img)
    bands = rng.randint(4, 6)
    edges = sorted(rng.uniform(0.12, 0.92) * h for _ in range(bands - 1))
    tops = [0.0, *edges]
    colors = [pal[min(i, len(pal) - 1)] for i in range(bands)]
    rng.shuffle(colors[2:])
    for i, top in enumerate(tops):
        amp, freq, phase = rng.uniform(2, h * 0.015), rng.uniform(1, 4), rng.uniform(0, math.tau)
        pts = [(x, top + amp * math.sin(freq * x / w * math.tau + phase)) for x in range(0, w + 40, 40)]
        base.polygon(pts + [(w, h), (0, h)], fill=colors[i])
    over = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    for i, top in enumerate(tops):
        bottom = tops[i + 1] if i + 1 < len(tops) else h
        _strokes(d, rng, (-50, int(top) - 10, w, int(bottom) + 10), colors[i], count=int(w * (bottom - top) / 900),
                 length=(w * 0.03, w * 0.14), width=(2, 9), slope=0.04, alpha=(50, 140))
    img = Image.alpha_composite(img.convert("RGBA"), over).convert("RGB").filter(ImageFilter.GaussianBlur(0.8))
    return _grain(img, rng, 0.18)


def _wobbly(d: ImageDraw.ImageDraw, rng: random.Random, pts: list[tuple[float, float]], fill, width: int) -> None:
    """A hand-drawn line through pts: each segment cut in short steps, nudged a little."""
    path = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        n = max(2, int(math.hypot(x1 - x0, y1 - y0) / 18))
        path += [(x0 + (x1 - x0) * k / n + rng.uniform(-0.8, 0.8), y0 + (y1 - y0) * k / n + rng.uniform(-0.8, 0.8))
                 for k in range(n)]
    path.append(pts[-1])
    d.line(path, fill=fill, width=width, joint="curve")


def _plot(rng: random.Random, size: tuple[int, int], pencils: list[RGB]) -> Image.Image:
    """One field of the map: a pencil wash and/or a pattern (furrows, hatching, an orchard)."""
    w, h = size
    tile = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    colour = pencils[rng.randrange(len(pencils))]
    if rng.random() < 0.75:
        d.rectangle([0, 0, w, h], fill=(*colour, rng.randint(70, 130)))
    pattern = rng.choice(["rows", "rows", "diag", "cross", "dots", "none"])
    step = rng.randint(7, 13)
    pen = (*[max(0, c - 70) for c in colour], 170)
    if pattern == "rows":
        horizontal = rng.random() < 0.5
        for k in range(step // 2, (h if horizontal else w), step):
            d.line([(0, k), (w, k)] if horizontal else [(k, 0), (k, h)], fill=pen, width=1)
    elif pattern in ("diag", "cross"):
        for k in range(-h, w, step):
            d.line([(k, 0), (k + h, h)], fill=pen, width=1)
            if pattern == "cross":
                d.line([(k + h, 0), (k, h)], fill=pen, width=1)
    elif pattern == "dots":
        r = rng.uniform(1.6, 2.8)
        for y in range(step, h - 2, step + 3):
            for x in range(step, w - 2, step + 3):
                cx, cy = x + rng.uniform(-1.5, 1.5), y + rng.uniform(-1.5, 1.5)
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*[max(0, c - 60) for c in colour], 210))
    return tile


def maps(seed: int, size: tuple[int, int]) -> Image.Image:
    rng = random.Random(seed)
    pal = PALETTES["maps"][seed % len(PALETTES["maps"])]
    ink, pencils, water = pal[0], pal[1:5], pal[5]
    w, h = size
    img = Image.new("RGBA", size, (238, 232, 218, 255))
    m = int(min(w, h) * 0.08)
    plots: list[tuple[float, float, float, float]] = []

    def split(x0: float, y0: float, x1: float, y1: float, depth: int) -> None:
        pw, ph = x1 - x0, y1 - y0
        if depth == 0 or (pw < w * 0.14 and ph < h * 0.12) or (depth < 4 and rng.random() < 0.2):
            plots.append((x0, y0, x1, y1))
            return
        t = rng.uniform(0.3, 0.7)
        if pw > ph * rng.uniform(0.7, 1.4):
            split(x0, y0, x0 + pw * t, y1, depth - 1); split(x0 + pw * t, y0, x1, y1, depth - 1)
        else:
            split(x0, y0, x1, y0 + ph * t, depth - 1); split(x0, y0 + ph * t, x1, y1, depth - 1)

    split(m, m, w - m, h - m, 6)
    d = ImageDraw.Draw(img)
    gap = 3
    for x0, y0, x1, y1 in plots:
        box = (int(x0 + gap), int(y0 + gap), int(x1 - gap), int(y1 - gap))
        if box[2] - box[0] > 6 and box[3] - box[1] > 6:
            tile = _plot(rng, (box[2] - box[0], box[3] - box[1]), pencils)
            img.alpha_composite(tile, (box[0], box[1]))
            _wobbly(d, rng, [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]), (box[0], box[1])],
                    (*ink, 150), 1)

    over = Image.new("RGBA", size, (0, 0, 0, 0))                # the river, across the sheet
    o = ImageDraw.Draw(over)
    y0, amp, freq, phase = rng.uniform(0.3, 0.7) * h, rng.uniform(0.04, 0.09) * h, rng.uniform(0.8, 1.6), rng.uniform(0, math.tau)
    freq2, phase2 = rng.uniform(2.2, 3.4), rng.uniform(0, math.tau)   # a second bend, so it meanders
    half = max(6.0, w * 0.018)
    xs = range(m, w - m + 1, 12)
    mid = [(x, y0 + amp * math.sin(freq * x / w * math.tau + phase) + amp * 0.35 * math.sin(freq2 * x / w * math.tau + phase2))
           for x in xs]
    o.polygon([(x, y - half) for x, y in mid] + [(x, y + half) for x, y in reversed(mid)], fill=(*water, 200))
    img.alpha_composite(over)
    for off in (-half, half):
        _wobbly(d, rng, [(x, y + off) for x, y in mid], (*ink, 220), 2)

    x = rng.uniform(0.3, 0.7) * w                                # a road, top to bottom, crossing the river
    road = [(x + rng.uniform(-w * 0.03, w * 0.03), y) for y in range(m, h - m + 1, int(h / 7))]
    for off in (-3, 3):
        _wobbly(d, rng, [(px + off, py) for px, py in road], (*ink, 230), 1)

    d.rectangle([m - 10, m - 10, w - m + 10, h - m + 10], outline=(*ink, 230), width=2)   # the map's frame
    cx, cy, r = w - m - 34, m + 40, 18                            # north mark
    d.line([(cx, cy + r), (cx, cy - r)], fill=(*ink, 230), width=2)
    d.polygon([(cx, cy - r - 8), (cx - 6, cy - r + 4), (cx + 6, cy - r + 4)], fill=(*ink, 230))
    d.line([(cx - r * 0.6, cy), (cx + r * 0.6, cy)], fill=(*ink, 200), width=1)
    return _grain(img.convert("RGB"), rng, 0.15, cell=1)


def weave(seed: int, size: tuple[int, int]) -> Image.Image:
    rng = random.Random(seed)
    pal = PALETTES["weave"][seed % len(PALETTES["weave"])]
    w, h = size
    img = Image.new("RGB", size, pal[3])
    d = ImageDraw.Draw(img)
    stripes, y = [], 0
    while y < h:                                                # weft: dyed stripes of varying height
        band = rng.randint(h // 30, h // 7)
        stripes.append((y, y + band, pal[rng.randrange(len(pal))]))
        y += band
    thread = max(3, w // 160)
    for y0, y1, color in stripes:
        for y in range(y0, y1, thread):
            d.line([(0, y), (w, y + rng.randint(-1, 1))], fill=_jitter(color, rng, 14), width=thread - 1)
    over = Image.new("RGBA", size, (0, 0, 0, 0))
    o = ImageDraw.Draw(over)
    for x in range(0, w, thread * 2):                           # warp showing through every other pass
        for y in range(rng.randint(0, thread * 2), h, thread * 2):
            o.rectangle([x, y, x + thread - 1, y + thread - 1], fill=(*_jitter(pal[3], rng, 20), 90))
    img = Image.alpha_composite(img.convert("RGBA"), over).convert("RGB").filter(ImageFilter.GaussianBlur(0.6))
    return _grain(img, rng, 0.22, cell=1)


def rooms(seed: int, size: tuple[int, int]) -> Image.Image:
    rng = random.Random(seed)
    pal = PALETTES["rooms"][seed % len(PALETTES["rooms"])]
    w, h = size
    img = Image.new("RGB", size, pal[3])
    for depth in range(rng.randint(2, 4)):
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        m = 0.08 + depth * rng.uniform(0.07, 0.11)
        x0, y0 = w * (m + rng.uniform(-0.03, 0.03)), h * (m + rng.uniform(-0.03, 0.05))
        x1, y1 = w * (1 - m + rng.uniform(-0.03, 0.03)), h * (1 - m * 0.6)
        ImageDraw.Draw(layer).rectangle([x0, y0, x1, y1], fill=(*_jitter(pal[depth % 3], rng, 12), 235))
        img = Image.alpha_composite(img.convert("RGBA"), layer.filter(ImageFilter.GaussianBlur(w / rng.uniform(60, 120))))
    over = Image.new("RGBA", size, (0, 0, 0, 0))
    _strokes(ImageDraw.Draw(over), rng, (0, 0, w, h), pal[2], count=w * h // 2500, length=(w * 0.02, w * 0.08),
             width=(3, 10), slope=math.pi, alpha=(10, 35))
    img = Image.alpha_composite(img, over).convert("RGB")
    return _grain(img, rng, 0.18)


STYLES = {"tide": tide, "maps": maps, "weave": weave, "rooms": rooms}


def work(style: str, seed: int, size: tuple[int, int]) -> Image.Image:
    return STYLES[style](seed, size)


# --- exhibition photos ------------------------------------------------------------

def _framed(art: Image.Image, frame: str) -> Image.Image:
    """A paper work gets a white mat and a thin dark frame; paintings and textiles hang bare."""
    if frame != "mat":
        return art
    w, h = art.size
    mat = max(8, w // 8)
    out = Image.new("RGB", (w + 2 * mat + 8, h + 2 * mat + 8), (40, 38, 36))
    out.paste(Image.new("RGB", (w + 2 * mat, h + 2 * mat), (246, 244, 240)), (4, 4))
    out.paste(art, (4 + mat, 4 + mat))
    return out


def exhibition(seed: int, works: list[Image.Image], frame: str = "none",
               size: tuple[int, int] = (1600, 1067)) -> Image.Image:
    """A gallery wall with the given works hung at eye height, a floor, spotlights and soft
    shadows: a plausible installation view, frontal and a little idealised."""
    rng = random.Random(seed)
    W, H = size
    horizon = int(H * rng.uniform(0.70, 0.76))
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    wall_top, wall_bottom = (240, 238, 234), (222, 219, 213)
    for y in range(horizon):
        t = y / horizon
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(wall_top, wall_bottom)))
    wood = rng.random() < 0.5
    floor_a, floor_b = ((150, 118, 88), (120, 92, 66)) if wood else ((178, 175, 170), (150, 147, 142))
    for y in range(horizon, H):
        t = (y - horizon) / (H - horizon)
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(floor_a, floor_b)))
    if wood:
        for k in range(1, 9):
            y = horizon + (H - horizon) * (k / 9) ** 1.6
            d.line([(0, y), (W, y)], fill=(104, 80, 58), width=1)
    d.rectangle([0, horizon - 10, W, horizon], fill=(250, 250, 248))           # skirting board
    img = _grain(img, rng, 0.15, cell=3)

    hung = [_framed(a, frame) for a in works]
    target = horizon * rng.uniform(0.34, 0.46) if len(hung) > 1 else horizon * rng.uniform(0.48, 0.58)
    if frame == "mat":                                          # small paper works hang closer to life size
        target *= 1.3
    scaled = [a.resize((max(1, int(a.width * target / a.height)), int(target)), Image.LANCZOS) for a in hung]
    while sum(a.width for a in scaled) > W * 0.82:              # many or wide works: hang them smaller
        scaled = [a.resize((int(a.width * 0.9), int(a.height * 0.9)), Image.LANCZOS) for a in scaled]
    gap = (W - sum(a.width for a in scaled)) / (len(scaled) + 1)
    centre = horizon * rng.uniform(0.44, 0.50)
    light = Image.new("L", size, 0)
    shadow = Image.new("L", size, 0)
    x = gap
    places = []
    for a in scaled:
        top = int(centre - a.height / 2)
        places.append((int(x), top))
        ImageDraw.Draw(light).ellipse([x - a.width * 0.3, top - a.height * 0.5, x + a.width * 1.3, top + a.height * 1.2],
                                      fill=70)
        ImageDraw.Draw(shadow).rectangle([x + 6, top + 10, x + a.width + 6, top + a.height + 10], fill=110)
        x += a.width + gap
    light = light.filter(ImageFilter.GaussianBlur(W / 18))
    img = Image.composite(Image.new("RGB", size, (255, 252, 244)), img, light)
    img = Image.composite(Image.new("RGB", size, (90, 86, 80)), img, shadow.filter(ImageFilter.GaussianBlur(12)))
    for a, pos in zip(scaled, places):
        img.paste(a, pos)
    vignette = Image.new("L", size, 0)
    ImageDraw.Draw(vignette).ellipse([-W * 0.25, -H * 0.3, W * 1.25, H * 1.3], fill=255)
    vignette = vignette.filter(ImageFilter.GaussianBlur(W / 10))
    return Image.composite(img, Image.new("RGB", size, (60, 58, 54)), vignette.point(lambda v: 150 + v * 105 // 255))


def detail(seed: int, art: Image.Image, size: tuple[int, int] = (1600, 1067)) -> Image.Image:
    """A close-up of part of a work, the way a detail photo crops it."""
    rng = random.Random(seed)
    cw, ch = art.width // 3, int(art.width // 3 * size[1] / size[0])
    x, y = rng.randint(0, art.width - cw), rng.randint(0, max(0, art.height - ch))
    return art.crop((x, y, x + cw, y + ch)).resize(size, Image.LANCZOS).filter(ImageFilter.SHARPEN)
