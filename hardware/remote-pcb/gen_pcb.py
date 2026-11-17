"""Build discstation-remote.kicad_pcb from design.py (placement only; routing is route.sh).

Run inside KiCad's Python:  flatpak run --command=python3 org.kicad.KiCad gen_pcb.py
Board coordinates below are mm from the board's top-left corner, measured from
the existing EasyEDA carrier board so the controls land in the same places.
"""
import json
import sys
from pathlib import Path

import pcbnew

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from design import PARTS, nets as design_nets  # noqa: E402
from gen_sch import sym_uuid, ROOT  # noqa: E402
from sexpr import parse, find, first  # noqa: E402

FPLIB = "/app/extensions/Library/footprints"
OUT = HERE / "discstation-remote.kicad_pcb"
NETLIST = HERE / "discstation-remote.net"  # kicad-cli sch export netlist
OX, OY = 100.0, 100.0          # board origin on the KiCad page
W, H = 72.0, 64.0
ANTENNA = (65.3, 9.5, W, 28.5)  # copper keep-out under the ESP32 antenna (x0, y0, x1, y1)

# ref: (x, y, rotation, side, anchor) - anchor "center" = pad-bbox centre, "origin" = footprint origin
FIXED = {
    "SW1": (20.0, 15.7, 0, "F", "center"),     # PLAY/PAUSE
    "SW2": (20.0, 34.1, 0, "F", "center"),     # HOME/BACK
    "SW3": (20.0, 52.4, 0, "F", "center"),     # EJECT
    "J2": (47.1, 7.6, 90, "F", "origin"),      # OLED header, pin 1 (GND) on the left
    "ENC1": (54.0, 47.5, 0, "F", "center"),
    "D2": (67.8, 39.3, 0, "F", "center"),      # power LED (where the heart LED was)
    "D3": (67.8, 45.5, 0, "F", "center"),      # charge LED
    "H1": (3.5, 4.2, 0, "F", "origin"), "H2": (68.7, 4.2, 0, "F", "origin"),
    "H3": (3.5, 59.8, 0, "F", "origin"), "H4": (68.7, 59.8, 0, "F", "origin"),
    "H5": (39.3, 8.1, 0, "F", "origin"), "H6": (62.4, 8.1, 0, "F", "origin"),     # OLED module standoffs
    "H7": (39.3, 31.8, 0, "F", "origin"), "H8": (62.4, 31.8, 0, "F", "origin"),
    "U1": (59.25, 19.0, 90, "B", "origin"),    # antenna points at the right edge
    "J1": (33.0, H - 3.65, 0, "B", "origin"),  # USB-C opening flush with the bottom edge
    "J3": (43.0, 59.5, 0, "B", "center"),      # battery JST-PH
    "SW4": (55.0, 61.4, 0, "B", "origin"),     # power slide switch next to the battery, lever ~0.5 mm past the bottom edge
}

# Free back-side parts: (anchor x, y). A spiral search finds the nearest free spot.
ANCHORS = {
    "U4": (35, 46), "R10": (33, 50), "R11": (37, 50), "R12": (38, 42), "C10": (32, 42), "C11": (34, 41),
    "Q1": (44, 24), "Q2": (44, 28), "R13": (42, 22), "R14": (42, 30),
    "U3": (42, 52), "R3": (46, 52), "C1": (39, 55), "C2": (46, 55), "R4": (40, 49),
    "Q3": (36, 37), "R5": (33, 36), "D1": (40, 40), "C3": (38, 34),
    "U2": (36, 30), "C4": (33, 30), "C5": (39, 30),
    "R6": (44, 38), "R7": (44, 41), "C6": (44, 44),
    "C7": (50, 30), "C8": (52, 31), "R8": (55, 31), "C9": (57, 31), "R9": (47, 30),
    "SW5": (51, 35), "SW6": (57, 35),
    "R15": (44, 14), "R16": (44, 16), "R17": (44, 18), "R18": (44, 20), "R19": (44, 22), "R20": (44, 12),
    "R21": (44, 10), "R22": (42, 10), "C12": (47, 40), "C13": (49, 40),
    "R1": (29, 54), "R2": (37, 54), "R23": (65, 36),
}

POWER_NETS = ["VBUS", "SYS", "VBAT", "BAT_SW"]


def mm(v):
    return pcbnew.FromMM(v)


def V(x, y):
    return pcbnew.VECTOR2I(mm(OX + x), mm(OY + y))


def rect_of(box, pad=0.0):
    return (pcbnew.ToMM(box.GetLeft()) - OX - pad, pcbnew.ToMM(box.GetTop()) - OY - pad,
            pcbnew.ToMM(box.GetRight()) - OX + pad, pcbnew.ToMM(box.GetBottom()) - OY + pad)


def overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def expand(pin):
    return pin.strip("[]").split(",") if pin.startswith("[") else [pin]


def schematic_nets():
    """{(ref, pad): net} from the schematic's own netlist, so the board matches it exactly."""
    out = {}
    for net in find(first(parse(NETLIST.read_text()), "nets"), "net"):
        name = first(net, "name")[1].lstrip("/")
        for node in find(net, "node"):
            for pad in expand(first(node, "pin")[1]):
                out[(first(node, "ref")[1], pad)] = name
    # cross-check against design.py for every named net
    for name, members in design_nets().items():
        for ref, pin in members:
            for pad in expand(pin):
                assert out.get((ref, pad)) == name, (ref, pad, name, out.get((ref, pad)))
    return out


def fix_esp32(fp):
    """Called with the footprint at the origin, unrotated.

    Espressif's 48 x 21 mm placement keep-out (zones and courtyard) is bigger than this
    board, so keep copper out from under the antenna instead (ANTENNA rule area) and use
    the module body as the courtyard. The 0.2 mm thermal vias in the ground pad are below
    JLCPCB's standard 0.3 mm drill; the module doesn't need them at this power.
    """
    for z in list(fp.Zones()):
        fp.Remove(z)
    for item in list(fp.GraphicalItems()):
        if item.GetLayer() in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
            fp.Remove(item)
    for pad in list(fp.Pads()):
        if pad.GetNumber() == "39" and pad.HasHole():
            fp.Remove(pad)
    box = pcbnew.PCB_SHAPE(fp)
    box.SetShape(pcbnew.SHAPE_T_RECT)
    box.SetStart(pcbnew.VECTOR2I(mm(-9.5), mm(-13.25)))
    box.SetEnd(pcbnew.VECTOR2I(mm(9.5), mm(13.25)))
    box.SetLayer(pcbnew.F_CrtYd)
    box.SetWidth(mm(0.05))
    fp.Add(box)


def courtyard(fp):
    layer = pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd
    cy = fp.GetCourtyard(layer)
    return rect_of(cy.BBox()) if cy.OutlineCount() else rect_of(fp.GetBoundingBox(False))


def orient(fp, rot, side):
    fp.SetOrientationDegrees(rot)
    if side == "B":
        fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)


def move_to(fp, x, y, anchor):
    if anchor == "origin":
        fp.SetPosition(V(x, y))
        return
    pads = list(fp.Pads())
    xs = [p.GetPosition().x for p in pads]
    ys = [p.GetPosition().y for p in pads]
    cx, cy = (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2
    pos = fp.GetPosition()
    fp.SetPosition(pcbnew.VECTOR2I(pos.x + V(x, y).x - cx, pos.y + V(x, y).y - cy))


def main():
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    ds = board.GetDesignSettings()
    ds.SetCopperLayerCount(2)

    padnets = schematic_nets()
    netinfo = {}
    for net in sorted(set(padnets.values())):
        netinfo[net] = pcbnew.NETINFO_ITEM(board, net)
        board.Add(netinfo[net])

    fps = {}
    for ref, (lib_id, value, fpid, pins, _) in PARTS.items():
        lib, name = fpid.split(":")
        fp = pcbnew.FootprintLoad(f"{FPLIB}/{lib}.pretty", name)
        assert fp, fpid
        fp.SetReference(ref)
        fp.SetValue(value)
        fp.SetFPID(pcbnew.LIB_ID(lib, name))
        fp.SetPath(pcbnew.KIID_PATH(f"/{ROOT}/{sym_uuid(ref)}"))
        if ref == "U1":
            fix_esp32(fp)
        for pad in fp.Pads():
            net = padnets.get((ref, pad.GetNumber())) if pad.GetNumber() else None
            if net:
                pad.SetNet(netinfo[net])
            if net == "GND" and pad.HasHole() and (ref, pad.GetNumber()) in (("J2", "1"), ("J1", "SH")):
                pad.SetLocalZoneConnection(pcbnew.ZONE_CONNECTION_FULL)  # one thermal spoke only fits here
            if ref[0] in "RCQD" and not pad.HasHole():
                fp.Reference().SetVisible(False)  # dense back side: refs stay on the fab layer
        board.Add(fp)
        fps[ref] = fp

    placed = []        # obstacle rectangles in board mm
    for ref, (x, y, rot, side, anchor) in FIXED.items():
        fp = fps[ref]
        orient(fp, rot, side)
        move_to(fp, x, y, anchor)
        if ref == "U1":
            placed.append((x - 12.75 - 0.3, y - 9 - 0.3, x + 12.75 + 0.3, y + 9 + 0.3))
        elif side == "B":
            placed.append(courtyard(fp))
        for pad in fp.Pads():
            if pad.HasHole():  # through-hole pins and holes block the back side too
                placed.append(rect_of(pad.GetBoundingBox(), 0.4))
        if ref.startswith("H"):
            placed.append(rect_of(fp.GetBoundingBox(False), 0.5))
    placed.append(ANTENNA)

    for ref, (ax, ay) in ANCHORS.items():
        fp = fps[ref]
        best = None
        for rot in (0, 90):
            orient(fp, rot, "B") if not fp.IsFlipped() else fp.SetOrientationDegrees(rot)
            for r in [i * 0.25 for i in range(0, 120)]:
                steps = max(1, int(r * 8))
                for k in range(steps):
                    import math
                    x = ax + r * math.cos(2 * math.pi * k / steps)
                    y = ay + r * math.sin(2 * math.pi * k / steps)
                    move_to(fp, x, y, "center")
                    box = courtyard(fp)
                    box = (box[0] - 0.15, box[1] - 0.15, box[2] + 0.15, box[3] + 0.15)
                    if box[0] < 0.6 or box[1] < 0.6 or box[2] > W - 0.6 or box[3] > H - 0.6:
                        continue
                    if any(overlaps(box, o) for o in placed):
                        continue
                    if best is None or r < best[0]:
                        best = (r, rot, x, y, box)
                    break
                if best and best[1] == rot:
                    break
        assert best, f"no room for {ref}"
        _, rot, x, y, box = best
        fp.SetOrientationDegrees(rot)
        move_to(fp, x, y, "center")
        placed.append(box)

    # board outline
    corners = [(0, 0), (W, 0), (W, H), (0, H)]
    for (x0, y0), (x1, y1) in zip(corners, corners[1:] + corners[:1]):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(V(x0, y0))
        seg.SetEnd(V(x1, y1))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(mm(0.1))
        board.Add(seg)

    # antenna keep-out on both copper layers
    ka = pcbnew.ZONE(board)
    ka.SetIsRuleArea(True)
    ka.SetDoNotAllowTracks(True)
    ka.SetDoNotAllowVias(True)
    ka.SetDoNotAllowZoneFills(True)
    ka.SetDoNotAllowPads(False)
    ka.SetDoNotAllowFootprints(False)
    ls = pcbnew.LSET()
    ls.AddLayer(pcbnew.F_Cu)
    ls.AddLayer(pcbnew.B_Cu)
    ka.SetLayerSet(ls)
    x0, y0, x1, y1 = ANTENNA
    ol = ka.Outline()
    ol.NewOutline()
    for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        ol.Append(V(px, py))
    ka.SetZoneName("ANTENNA_KEEPOUT")
    board.Add(ka)

    # silkscreen
    for text, x, y, layer, size in (("DISCSTATION", 34, 3.0, pcbnew.F_SilkS, 1.5),
                                    ("PLAY", 20, 23.2, pcbnew.F_SilkS, 1.0), ("HOME", 20, 41.6, pcbnew.F_SilkS, 1.0),
                                    ("EJECT", 20, 59.9, pcbnew.F_SilkS, 1.0),
                                    ("DiscStation remote r1", 50, 5.0, pcbnew.B_SilkS, 1.0),
                                    ("BAT +  -", 43, 55.8, pcbnew.B_SilkS, 0.8)):
        t = pcbnew.PCB_TEXT(board)
        t.SetText(text)
        t.SetPosition(V(x, y))
        t.SetLayer(layer)
        t.SetTextSize(pcbnew.VECTOR2I(mm(size), mm(size)))
        t.SetTextThickness(mm(size * 0.15))
        if layer == pcbnew.B_SilkS:
            t.SetMirrored(True)
        board.Add(t)

    board.Save(str(OUT))

    # Net classes live in the project file; KiCad reads them when the board is loaded.
    pro = {"board": {"design_settings": {"rules": {"min_clearance": 0.2, "min_track_width": 0.2,
                                                    "min_via_diameter": 0.6, "min_through_hole_diameter": 0.3}}},
           "net_settings": {"classes": [
               {"name": "Default", "clearance": 0.2, "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3,
                "diff_pair_width": 0.2, "diff_pair_gap": 0.25, "microvia_diameter": 0.3, "microvia_drill": 0.1,
                "wire_width": 6, "bus_width": 12, "line_style": 0, "schematic_color": "rgba(0, 0, 0, 0.000)",
                "pcb_color": "rgba(0, 0, 0, 0.000)", "priority": 2147483647},
               {"name": "Power", "clearance": 0.2, "track_width": 0.5, "via_diameter": 0.8, "via_drill": 0.4,
                "diff_pair_width": 0.2, "diff_pair_gap": 0.25, "microvia_diameter": 0.3, "microvia_drill": 0.1,
                "wire_width": 6, "bus_width": 12, "line_style": 0, "schematic_color": "rgba(0, 0, 0, 0.000)",
                "pcb_color": "rgba(0, 0, 0, 0.000)", "priority": 0}],
               "meta": {"version": 4},
               "netclass_patterns": [{"netclass": "Power", "pattern": n} for n in POWER_NETS]},
           "meta": {"filename": "discstation-remote.kicad_pro", "version": 3}}
    (HERE / "discstation-remote.kicad_pro").write_text(json.dumps(pro, indent=2))

    for ref in ("J2", "SW4", "U1", "J1"):
        fp = fps[ref]
        print(ref, [(p.GetNumber(), round(pcbnew.ToMM(p.GetPosition().x) - OX, 2), round(pcbnew.ToMM(p.GetPosition().y) - OY, 2))
                    for p in fp.Pads()][:4])
    print(f"placed {len(fps)} footprints -> {OUT.name}")


if __name__ == "__main__":
    main()
