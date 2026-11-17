"""Minimal KiCad s-expression reader/writer (lists of str/Sym, quoted strings kept as str)."""
import re


class Sym(str):
    """An unquoted atom (keyword or number), as opposed to a quoted string."""


_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')


def parse(text):
    stack, cur, pos = [], [], 0
    while True:
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            break
        pos = m.end()
        if m.group(1):
            stack.append(cur)
            cur = []
        elif m.group(2):
            done, cur = cur, stack.pop()
            cur.append(done)
        elif m.group(3) is not None:
            cur.append(m.group(3).replace('\\"', '"').replace("\\\\", "\\"))
        else:
            cur.append(Sym(m.group(4)))
    assert not stack, "unbalanced s-expression"
    return cur[0] if len(cur) == 1 else cur


def dump(node, indent=0):
    if isinstance(node, Sym):
        return str(node)
    if isinstance(node, str):
        return '"' + node.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(node, (int, float)):
        return f"{node:g}" if isinstance(node, float) else str(node)
    inner = [dump(n, indent + 1) for n in node]
    flat = "(" + " ".join(inner) + ")"
    if len(flat) < 100 and "\n" not in flat:
        return flat
    pad = "\n" + "  " * (indent + 1)
    return "(" + inner[0] + "".join(pad + i for i in inner[1:]) + "\n" + "  " * indent + ")"


def find(node, key):
    return [n for n in node if isinstance(n, list) and n and n[0] == key]


def first(node, key):
    hits = find(node, key)
    return hits[0] if hits else None


if __name__ == "__main__":
    t = '(a (b "x \\"q\\"" 1.5) (c d) e)'
    p = parse(t)
    assert p == ["a", ["b", 'x "q"', "1.5"], ["c", "d"], "e"] and isinstance(p[1][2], Sym)
    assert parse(dump(p)) == p
    print("sexpr ok")
