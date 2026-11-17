"""Turn the traced DXF sketches and the Phuju font into board-space polygons (art.json).

Runs on the host (needs fontTools); add_art.py then puts the polygons on the board.
Each shape is {"layer", "outer": [[x, y], ...], "holes": [[[x, y], ...], ...]} in board mm
(origin = board top-left, y down).
"""
import json
import sys
from pathlib import Path

from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from dxfpoly import polylines  # noqa: E402

DISC_DXF = HERE / "disc.dxf"
EYE_DXF = HERE / "eye.dxf"
FONT = HERE / "Phuju-Regular.ttf"

# Placement (board mm)
DISC_CENTRE, DISC_DIAMETER = (42.6, 59.6), 6.8          # front, bottom-right strip
TEXT_LEFT, TEXT_RIGHT, TEXT_MID_Y, TEXT_CAP_MAX = 46.8, 65.6, 59.6, 2.6
EYE_WIDTH = 16.0
EYE_PUPIL_DXF = (972.6, 386.1)                          # pupil centre in the eye DXF's units
EYE_PUPIL_BOARD = (55.6, 58.4)                          # = D4, the eye LED


def area(r):
    return sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(r, r[1:] + r[:1])) / 2


def inside(pt, ring):
    x, y = pt
    c = False
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
            c = not c
    return c


def nest(rings):
    """Even-odd nesting: even-depth rings are outlines, odd-depth rings are holes of their parent."""
    rings = [r for r in rings if len(r) >= 3 and abs(area(r)) > 1e-9]
    rings.sort(key=lambda r: abs(area(r)), reverse=True)
    bbox = [(min(x for x, _ in r), min(y for _, y in r), max(x for x, _ in r), max(y for _, y in r)) for r in rings]
    parent, depth = [None] * len(rings), [0] * len(rings)
    for i, r in enumerate(rings):
        p = r[0]
        for j in range(i - 1, -1, -1):  # smallest enclosing ring = last larger one that contains it
            b = bbox[j]
            if b[0] <= p[0] <= b[2] and b[1] <= p[1] <= b[3] and inside(p, rings[j]):
                parent[i], depth[i] = j, depth[j] + 1
                break
    shapes = {}
    for i, r in enumerate(rings):
        if depth[i] % 2 == 0:
            shapes[i] = {"outer": r, "holes": []}
    for i, r in enumerate(rings):
        if depth[i] % 2 == 1 and parent[i] in shapes:
            shapes[parent[i]]["holes"].append(r)
    return list(shapes.values())


def dxf_rings(path, scale, offset, flip_y=True):
    ox, oy = offset
    return [[(ox + x * scale, oy + (-y if flip_y else y) * scale) for x, y in pts] for _, pts in polylines(path)]


class FlattenPen(BasePen):
    def __init__(self, glyphset, steps=8):
        super().__init__(glyphset)
        self.rings, self.cur, self.steps = [], [], steps

    def _moveTo(self, p):
        self.cur = [p]

    def _lineTo(self, p):
        self.cur.append(p)

    def _curveToOne(self, p1, p2, p3):
        p0 = self.cur[-1]
        for i in range(1, self.steps + 1):
            t = i / self.steps
            a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
            self.cur.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))

    def _qCurveToOne(self, p1, p2):
        p0 = self.cur[-1]
        for i in range(1, self.steps + 1):
            t = i / self.steps
            self.cur.append(((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
                             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]))

    def _closePath(self):
        if len(self.cur) > 2:
            self.rings.append(self.cur)
        self.cur = []

    _endPath = _closePath


def text_rings(text):
    font = TTFont(FONT)
    gs, cmap, hmtx = font.getGlyphSet(), font.getBestCmap(), font["hmtx"]
    rings, x = [], 0.0
    for ch in text:
        name = cmap.get(ord(ch))
        assert name, f"font has no glyph for {ch!r}"
        pen = FlattenPen(gs)
        gs[name].draw(pen)
        rings += [[(px + x, py) for px, py in r] for r in pen.rings]
        x += hmtx[name][0]
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return rings, (min(xs), min(ys), max(xs), max(ys))


def main():
    out = []
    # disc sketch -> front, scaled to DISC_DIAMETER
    raw = [pts for _, pts in polylines(DISC_DXF)]
    xs = [x for r in raw for x, _ in r]
    ys = [y for r in raw for _, y in r]
    s = DISC_DIAMETER / max(max(xs) - min(xs), max(ys) - min(ys))
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    rings = [[(DISC_CENTRE[0] + (x - cx) * s, DISC_CENTRE[1] - (y - cy) * s) for x, y in r] for r in raw]
    out += [dict(layer="F.SilkS", name="disc", **sh) for sh in nest(rings)]

    # "DISCSTATION" in the Phuju font -> front, fitted into the strip right of the disc
    rings, (x0, y0, x1, y1) = text_rings("DISCSTATION")
    s = min((TEXT_RIGHT - TEXT_LEFT) / (x1 - x0), TEXT_CAP_MAX / (y1 - y0))
    rings = [[(TEXT_LEFT + (x - x0) * s, TEXT_MID_Y + ((y0 + y1) / 2 - y) * s) for x, y in r] for r in rings]
    out += [dict(layer="F.SilkS", name="text", **sh) for sh in nest(rings)]
    text_w, text_h = (x1 - x0) * s, (y1 - y0) * s

    # eye sketch -> back, pupil on the LED; drawn as seen from the back, so mirror x
    raw = [pts for _, pts in polylines(EYE_DXF)]
    xs = [x for r in raw for x, _ in r]
    s = EYE_WIDTH / (max(xs) - min(xs))
    px, py = EYE_PUPIL_DXF
    bx, by = EYE_PUPIL_BOARD
    rings = [[(bx - (x - px) * s, by - (y - py) * s) for x, y in r] for r in raw]
    out += [dict(layer="B.SilkS", name="eye", **sh) for sh in nest(rings)]

    (HERE / "art.json").write_text(json.dumps(out))
    for name in ("disc", "text", "eye"):
        pts = [p for sh in out if sh["name"] == name for p in sh["outer"]]
        print(f"{name:5} shapes={sum(1 for sh in out if sh['name'] == name):4}  x {min(p[0] for p in pts):6.2f}..{max(p[0] for p in pts):6.2f}"
              f"  y {min(p[1] for p in pts):6.2f}..{max(p[1] for p in pts):6.2f}")
    print(f"text size {text_w:.2f} x {text_h:.2f} mm")


if __name__ == "__main__":
    main()
