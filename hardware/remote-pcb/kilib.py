"""Read symbols (with pins) from the installed KiCad libraries."""
import os
from pathlib import Path

from sexpr import parse, find, first

FLATPAK = Path.home() / ".local/share/flatpak/runtime"
SYM_DIR = Path(os.environ.get("KICAD_SYMBOL_DIR", FLATPAK / "org.kicad.KiCad.Library.Symbols/x86_64/stable/active/files/symbols"))
FP_DIR = Path(os.environ.get("KICAD_FOOTPRINT_DIR", FLATPAK / "org.kicad.KiCad.Library.Footprints/x86_64/stable/active/files/footprints"))

_libs = {}


def _lib(name):
    if name not in _libs:
        tree = parse((SYM_DIR / f"{name}.kicad_sym").read_text())
        _libs[name] = {s[1]: s for s in find(tree, "symbol")}
    return _libs[name]


def symbol(lib_id):
    """The symbol node with any `extends` resolved (pins and graphics from the parent)."""
    lib, name = lib_id.split(":")
    sym = _lib(lib)[name]
    ext = first(sym, "extends")
    if not ext:
        return sym
    parent = symbol(f"{lib}:{ext[1]}")
    merged = [sym[0], name] + [n for n in sym[2:] if not (isinstance(n, list) and n[0] in ("extends",))]
    have = {n[0] for n in merged if isinstance(n, list) and n[0] != "property"}
    props = {n[1] for n in merged if isinstance(n, list) and n[0] == "property"}
    for n in parent[2:]:
        if isinstance(n, list) and n[0] == "symbol":  # sub-units carry graphics + pins
            merged.append([n[0], n[1].replace(ext[1], name, 1)] + n[2:])
        elif isinstance(n, list) and n[0] == "property" and n[1] not in props:
            merged.append(n)
        elif isinstance(n, list) and n[0] not in have and n[0] != "property":
            merged.append(n)
    return merged


def pins(lib_id):
    """[(number, name, etype, x, y, rot, unit)] in symbol coordinates (y up)."""
    out = []
    for unit in find(symbol(lib_id), "symbol"):
        unit_no = int(unit[1].rsplit("_", 2)[-2])
        for p in find(unit, "pin"):
            at = first(p, "at")
            out.append((first(p, "number")[1], first(p, "name")[1], str(p[1]),
                        float(at[1]), float(at[2]), float(at[3]) if len(at) > 3 else 0.0, unit_no))
    return out


if __name__ == "__main__":
    import sys
    for lid in sys.argv[1:]:
        print(f"== {lid}")
        for num, name, et, x, y, r, u in sorted(pins(lid), key=lambda p: (p[6], p[0].zfill(4))):
            print(f"  {num:>4} {name:<18} {et:<14} u{u}")
