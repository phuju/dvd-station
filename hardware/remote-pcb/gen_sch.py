"""Write discstation-remote.kicad_sch from design.py.

Every pin gets a global label with its net name (or a no-connect flag), so the
sheet is fully connected without drawn wires.
"""
import uuid
from pathlib import Path

from sexpr import Sym, dump, find, first
import kilib
from design import PARTS, PWR_FLAGS

OUT = Path(__file__).with_name("discstation-remote.kicad_sch")
PROJECT = "discstation-remote"
ROOT = str(uuid.uuid5(uuid.NAMESPACE_URL, "discstation-remote/root"))
G = 2.54
BLOCKS = [("usb", "USB-C input"), ("power", "Charger, power path, 3.3 V"), ("mcu", "ESP32"),
          ("usbserial", "USB-serial + auto-program"), ("controls", "Pull-ups"), ("front", "Front panel"),
          ("mech", "Mounting holes"), ("flags", "Power flags")]

_n = 0


def uid():
    global _n
    _n += 1
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"discstation-remote/{_n}"))


def sym_uuid(ref):
    """Stable per-reference UUID; gen_pcb.py uses it to link footprints to symbols."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"discstation-remote/sym/{ref}"))


def S(*a):
    return [Sym(a[0]), *a[1:]]


def num(v):
    return Sym(f"{round(v, 4):g}")


def font(size=1.27, hide=False, justify=None):
    e = S("effects", S("font", S("size", num(size), num(size))))
    if justify:
        e.append(S("justify", Sym(justify)))
    if hide:
        e.append(S("hide", Sym("yes")))
    return e


def prop(name, value, x, y, hide=False):
    return S("property", name, value, S("at", num(x), num(y), num(0)), font(hide=hide))


def expand(pin):
    return pin.strip("[]").split(",") if pin.startswith("[") else [pin]


ROTATED = {"Device:R", "Device:C"}  # drawn sideways so their labels run horizontally


def placed_pins(lib_id):
    """Pins in sheet orientation: (num, x, y_down, angle). Symbol rotation is CCW."""
    r = 90 if lib_id in ROTATED else 0
    out = []
    for pnum, _, _, px, py, prot, _ in kilib.pins(lib_id):
        if r == 90:
            px, py = -py, px
        out.append((pnum, px, -py, (prot + r) % 360))
    return r, out


def bbox(lib_id):
    _, ps = placed_pins(lib_id)
    xs = [p[1] for p in ps] + [-2.54, 2.54]
    ys = [p[2] for p in ps] + [-2.54, 2.54]
    return min(xs), min(ys), max(xs), max(ys)


def label_len(net):
    return 0.8 * len(net) * 1.27 + 4


def main():
    lib_syms, items = {}, []
    col_w = 185
    columns = [[15 + i * (col_w + 6), 25.0] for i in range(3)]  # [x, next free y]
    instances = [(ref, *PARTS[ref]) for ref in PARTS]
    flags = [(f"#FLG{i:02d}", "power:PWR_FLAG", "PWR_FLAG", "", {"1": net}, "flags") for i, net in enumerate(PWR_FLAGS, 1)]
    for block, title in BLOCKS:
        members = [p for p in instances + flags if p[5] == block]
        if not members:
            continue
        col = min(columns, key=lambda c: c[1])
        x0, y = col
        sheet_w = x0 + col_w
        items.append(S("text", title, S("at", num(x0), num(y), num(0)), font(2.54, justify="left")))
        y += 8
        x, row_h = x0, 0
        for ref, lib_id, value, fp, pins, _ in members:
            lib_syms.setdefault(lib_id, kilib.symbol(lib_id))
            minx, miny, maxx, maxy = bbox(lib_id)
            rot, ppins = placed_pins(lib_id)
            left = max((label_len(pins.get(p[0], "")) for p in ppins if p[3] == 0), default=0)
            right = max((label_len(pins.get(p[0], "")) for p in ppins if p[3] == 180), default=0)
            up = max((label_len(pins.get(p[0], "")) for p in ppins if p[3] == 270), default=0)
            down = max((label_len(pins.get(p[0], "")) for p in ppins if p[3] == 90), default=0)
            w = (maxx - minx) + left + right + 8
            h = (maxy - miny) + up + down + 12
            if x + w > sheet_w and x > x0:
                x, y, row_h = x0, y + row_h, 0
            sx = round((x + left - minx + 4) / G) * G
            sy = round((y + up - miny + 6) / G) * G
            sym = S("symbol", S("lib_id", lib_id), S("at", num(sx), num(sy), num(rot)), S("unit", Sym("1")),
                    S("exclude_from_sim", Sym("no")), S("in_bom", Sym("no" if ref.startswith(("#", "H")) else "yes")),
                    S("on_board", Sym("no" if ref.startswith("#") else "yes")), S("dnp", Sym("no")), S("uuid", sym_uuid(ref)),
                    prop("Reference", ref, sx, sy + miny - (1.5 if rot == 0 else 0.5), hide=ref.startswith("#")),
                    prop("Value", value, sx, sy + maxy + (1.5 if rot == 0 else 0.5)),
                    prop("Footprint", fp, sx, sy, hide=True),
                    prop("Datasheet", "", sx, sy, hide=True))
            used = set()
            for pnum, px, py, prot in ppins:
                sym.append(S("pin", pnum, S("uuid", uid())))
                ex, ey = sx + px, sy + py
                net = pins.get(pnum)
                if net is None:
                    items.append(S("no_connect", S("at", num(ex), num(ey)), S("uuid", uid())))
                    continue
                used.add(pnum)
                ang = (prot + 180) % 360
                items.append(S("global_label", net, S("shape", Sym("passive")), S("at", num(ex), num(ey), num(ang)),
                               S("fields_autoplaced", Sym("yes")), font(justify="left" if ang in (0, 90) else "right"),
                               S("uuid", uid())))
            missing = set(pins) - used
            assert not missing, (ref, missing)
            sym.append(S("instances", S("project", PROJECT, S("path", f"/{ROOT}", S("reference", ref), S("unit", Sym("1"))))))
            items.append(sym)
            x += w
            row_h = max(row_h, h)
        y += row_h + 6
        col[1] = y

    libs = S("lib_symbols")
    for lib_id, node in lib_syms.items():
        libs.append([node[0], lib_id] + node[2:])
    doc = S("kicad_sch", S("version", Sym("20250114")), S("generator", "eeschema"), S("generator_version", "9.0"),
            S("uuid", ROOT), S("paper", "A2"),
            S("title_block", S("title", "DiscStation remote"), S("rev", "1"),
              S("comment", Sym("1"), "Generated by hardware/remote-pcb/gen_sch.py from design.py - edit design.py, not this file")),
            libs, *items, S("sheet_instances", S("path", "/", S("page", "1"))))
    y = max(c[1] for c in columns)
    assert y < 410, f"sheet overflow: {y:.0f} mm"
    OUT.write_text(dump(doc) + "\n")
    print(f"wrote {OUT.name}: {len(PARTS)} parts, {len(lib_syms)} symbols, height {y:.0f} mm")


if __name__ == "__main__":
    main()
