"""Write the test-pad footprints into ../discstation.pretty (a project footprint library).

TestPad_Heart: heart-shaped exposed copper pad, 6 mm wide, with a 1 mm plated hole for a
scope hook or a soldered wire loop. TestPad_Round: 3 mm round pad, same hole.
Both are through-hole, so the copper is identical on the front and the back.
"""
import math
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "discstation.pretty"


def heart(width=6.0, n=72):
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((x, -y))  # KiCad y points down
    s = width / 32.0
    ys = [p[1] for p in pts]
    cy = (min(ys) + max(ys)) / 2
    return [(round(x * s, 4), round((y - cy) * s, 4)) for x, y in pts]


def footprint(name, descr, primitive, label, label_dy):
    pts = " ".join(f"(xy {x} {y})" for x, y in primitive) if primitive else ""
    prim = f"\n\t\t(primitives\n\t\t\t(gr_poly (pts {pts}) (width 0) (fill yes))\n\t\t)" if primitive else ""
    shape = "custom" if primitive else "circle"
    size = "1.8 1.8" if primitive else "3 3"
    opts = "\n\t\t(options (clearance outline) (anchor circle))" if primitive else ""
    return f'''(footprint "{name}"
\t(version 20241229)
\t(generator "discstation-make_testpads")
\t(layer "F.Cu")
\t(descr "{descr}")
\t(tags "test point")
\t(property "Reference" "TP**" (at 0 {-label_dy} 0) (layer "F.SilkS") (hide yes)
\t\t(effects (font (size 0.8 0.8) (thickness 0.12))))
\t(property "Value" "{name}" (at 0 {label_dy + 1.2} 0) (layer "F.Fab")
\t\t(effects (font (size 0.8 0.8) (thickness 0.12))))
\t(attr through_hole board_only exclude_from_pos_files exclude_from_bom)
\t(fp_text user "{label}" (at 0 {label_dy} 0) (layer "F.SilkS")
\t\t(effects (font (size 0.9 0.9) (thickness 0.15))))
\t(fp_text user "{label}" (at 0 {label_dy} 0) (layer "B.SilkS")
\t\t(effects (font (size 0.9 0.9) (thickness 0.15)) (justify mirror)))
\t(fp_circle (center 0 0) (end {4.0 if primitive else 2.2} 0) (stroke (width 0.05) (type solid)) (fill no) (layer "F.CrtYd"))
\t(pad "1" thru_hole {shape} (at 0 0) (size {size}) (drill 1) (layers "*.Cu" "*.Mask"){opts}{prim}
\t\t(zone_connect 2))
)
'''


def main():
    LIB.mkdir(exist_ok=True)
    (LIB / "TestPad_Heart.kicad_mod").write_text(
        footprint("TestPad_Heart", "Heart-shaped test pad, 6 mm, 1 mm plated hole", heart(), "GND", 3.9))
    (LIB / "TestPad_Round.kicad_mod").write_text(
        footprint("TestPad_Round", "Round test pad, 3 mm, 1 mm plated hole", None, "3V3", 2.6))
    print("wrote", sorted(p.name for p in LIB.iterdir()))


if __name__ == "__main__":
    main()
