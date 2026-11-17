"""Add the silkscreen art and the GND/3V3 test pads to the routed board.

Run inside KiCad's Python after prep_art.py and make_testpads.py:
    flatpak run --command=python3 org.kicad.KiCad art/add_art.py
Edits discstation-remote.kicad_pcb in place (no re-placement or re-routing).
"""
import json
import math
from pathlib import Path

import pcbnew

HERE = Path(__file__).resolve().parent
PCB = HERE.parent / "discstation-remote.kicad_pcb"
LIB = str(HERE.parent / "discstation.pretty")
OX, OY, W, H = 100.0, 100.0, 72.0, 64.0
ART_KEEP = [(38.5, 55.5, 66.6, 63.6), (47.5, 51.5, 65.5, 63.6),  # front disc+text, back eye (mm)
            (36.0, 4.0, 65.5, 35.0)]  # under the OLED module: a front pad there is unreachable
ANTENNA = (65.3, 9.5, W, 28.5)
mm, I = pcbnew.FromMM, pcbnew.ToMM


def V(x, y):
    return pcbnew.VECTOR2I(mm(OX + x), mm(OY + y))


def copper_items(board):
    items = list(board.GetPads()) + list(board.GetTracks())
    return items


def clear_at(board, x, y, r, skip=()):
    """True if a disc of radius r (mm) at (x, y) touches no copper, hole or courtyard on either side."""
    c = V(x, y)
    circ = pcbnew.SHAPE_CIRCLE(c, mm(r))
    if not (r + 0.6 <= x <= W - r - 0.6 and r + 0.6 <= y <= H - r - 0.6):
        return False
    for x0, y0, x1, y1 in ART_KEEP + [ANTENNA]:
        if x0 - r < x < x1 + r and y0 - r < y < y1 + r:
            return False
    for it in copper_items(board):
        if it in skip:
            continue
        for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
            if it.IsOnLayer(layer) and it.GetEffectiveShape(layer).Collide(circ, mm(0.3)):
                return False
    for fp in board.GetFootprints():
        if fp in skip:
            continue
        for layer in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
            cy = fp.GetCourtyard(layer)
            if cy.OutlineCount() and cy.Collide(c, mm(r + 0.2)):
                return False
        for pad in fp.Pads():  # NPTH holes have no copper
            if pad.HasHole() and pad.GetEffectiveHoleShape().Collide(circ, mm(0.3)):
                return False
    return True


def route_clear(board, a, b, layer, width, net):
    seg = pcbnew.SHAPE_SEGMENT(a, b, mm(width))
    for it in copper_items(board):
        if it.GetNetCode() == net.GetNetCode() or not it.IsOnLayer(layer):
            continue
        if it.GetEffectiveShape(layer).Collide(seg, mm(0.25)):
            return False
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.HasHole() and pad.GetNetCode() != net.GetNetCode() and pad.GetEffectiveHoleShape().Collide(seg, mm(0.3)):
                return False
    x0, y0, x1, y1 = ANTENNA
    ka = pcbnew.SHAPE_RECT(V(x0, y0), mm(x1 - x0), mm(y1 - y0))
    return not ka.Collide(seg, 0)


def add_testpad(board, name, ref, net, x, y):
    fp = pcbnew.FootprintLoad(LIB, name)
    fp.SetReference(ref)
    fp.SetValue(net.GetNetname())
    fp.SetPosition(V(x, y))
    for pad in fp.Pads():
        pad.SetNet(net)
    board.Add(fp)
    return fp


def place_testpads(board):
    gnd, v33 = board.FindNet("GND"), board.FindNet("+3V3")
    targets = []  # +3V3 copper we can join with a straight track
    for t in board.GetTracks():
        if t.GetNetCode() == v33.GetNetCode():
            if isinstance(t, pcbnew.PCB_VIA):
                targets.append((t.GetPosition(), [pcbnew.F_Cu, pcbnew.B_Cu]))
            else:
                targets += [(t.GetStart(), [t.GetLayer()]), (t.GetEnd(), [t.GetLayer()])]
    grid = [(x / 2, y / 2) for x in range(4, int(W * 2) - 3) for y in range(4, int(H * 2) - 3)]
    best = None
    for x, y in grid:
        if not clear_at(board, x, y, 1.9):
            continue
        p = V(x, y)
        for tp, layers in sorted(targets, key=lambda t: (t[0] - p).EuclideanNorm())[:6]:
            d = I((tp - p).EuclideanNorm())
            if best and d >= best[0]:
                break
            for layer in layers:
                if route_clear(board, p, tp, layer, 0.3, v33):
                    best = (d, x, y, tp, layer)
                    break
    assert best, "no spot for the 3V3 test pad"
    d, x, y, tp, layer = best
    tp33 = add_testpad(board, "TestPad_Round", "TP2", v33, x, y)
    tr = pcbnew.PCB_TRACK(board)
    tr.SetStart(V(x, y))
    tr.SetEnd(tp)
    tr.SetWidth(mm(0.3))
    tr.SetLayer(layer)
    tr.SetNet(v33)
    board.Add(tr)
    # heart GND pad: the free spot closest to the 3V3 pad, so a probe pair reaches both
    cands = sorted((math.hypot(gx - x, gy - y), gx, gy) for gx, gy in grid if math.hypot(gx - x, gy - y) >= 6)
    for _, gx, gy in cands:
        if clear_at(board, gx, gy, 3.4, skip=(tp33, tr)) and math.hypot(gx - x, gy - y) >= 3.4 + 1.5 + 1.0:
            add_testpad(board, "TestPad_Heart", "TP1", gnd, gx, gy)
            print(f"TP1 GND heart at ({gx}, {gy}); TP2 3V3 at ({x}, {y}), {d:.1f} mm track on {board.GetLayerName(layer)}")
            return
    raise AssertionError("no spot for the GND heart pad")


def pad_keepout(board, layer):
    out = pcbnew.SHAPE_POLY_SET()
    for pad in board.GetPads():
        if pad.IsOnLayer(layer if layer == pcbnew.F_Cu else pcbnew.B_Cu) or pad.HasHole():
            pad.TransformShapeToPolygon(out, pcbnew.F_Cu if layer == pcbnew.F_Cu else pcbnew.B_Cu, mm(0.15), mm(0.005), pcbnew.ERROR_OUTSIDE)
    return out


def add_art(board):
    shapes = json.loads((HERE / "art.json").read_text())
    for t in list(board.GetDrawings()):  # the plain-font placeholder is replaced by the Phuju text
        if isinstance(t, pcbnew.PCB_TEXT) and t.GetText() == "DISCSTATION":
            board.Remove(t)
    for name, layer_name in (("disc", "F.SilkS"), ("text", "F.SilkS"), ("eye", "B.SilkS")):
        layer = board.GetLayerID(layer_name)
        poly = pcbnew.SHAPE_POLY_SET()
        for sh in shapes:
            if sh["name"] != name:
                continue
            o = poly.NewOutline()
            for x, y in sh["outer"]:
                poly.Append(mm(OX + x), mm(OY + y), o)
            for hole in sh["holes"]:
                h = poly.NewHole(o)
                for x, y in hole:
                    poly.Append(mm(OX + x), mm(OY + y), o, h)
        poly.Simplify()
        poly.BooleanSubtract(pad_keepout(board, pcbnew.F_Cu if layer_name == "F.SilkS" else pcbnew.B_Cu))
        poly.Fracture()
        group = pcbnew.PCB_GROUP(board)
        group.SetName(f"ART_{name.upper()}")
        board.Add(group)
        n = 0
        for i in range(poly.OutlineCount()):
            one = pcbnew.SHAPE_POLY_SET()
            one.AddOutline(poly.Outline(i))
            s = pcbnew.PCB_SHAPE(board)
            s.SetShape(pcbnew.SHAPE_T_POLY)
            s.SetPolyShape(one)
            s.SetFilled(True)
            s.SetWidth(0)
            s.SetLayer(layer)
            board.Add(s)
            group.AddItem(s)
            n += 1
        print(f"{name}: {n} silkscreen polygons on {layer_name}")


def main():
    board = pcbnew.LoadBoard(str(PCB))
    assert not board.FindFootprintByReference("TP1"), "test pads already added"
    place_testpads(board)
    add_art(board)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.Save(str(PCB))
    print("saved", PCB.name)


if __name__ == "__main__":
    main()
