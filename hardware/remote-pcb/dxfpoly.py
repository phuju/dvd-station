"""Read POLYLINE/VERTEX outlines from a traced-image DXF (e.g. potrace output)."""


def polylines(path):
    """[(closed, [(x, y), ...]), ...]"""
    lines = open(path, errors="replace").read().splitlines()
    pairs = [(c.strip(), v.strip()) for c, v in zip(lines[0::2], lines[1::2])]
    out, cur, closed, i = [], None, False, 0
    while i < len(pairs):
        code, val = pairs[i]
        if code == "0" and val == "POLYLINE":
            cur, closed = [], False
            i += 1
            while i < len(pairs) and pairs[i][0] != "0":
                if pairs[i][0] == "70":
                    closed = bool(int(pairs[i][1]) & 1)
                i += 1
            continue
        if code == "0" and val == "VERTEX" and cur is not None:
            x = y = None
            i += 1
            while i < len(pairs) and pairs[i][0] != "0":
                if pairs[i][0] == "10":
                    x = float(pairs[i][1])
                elif pairs[i][0] == "20":
                    y = float(pairs[i][1])
                i += 1
            cur.append((x, y))
            continue
        if code == "0" and val == "SEQEND" and cur is not None:
            out.append((closed, cur))
            cur = None
        i += 1
    return out


def render_png(path, png, size=900):
    from PIL import Image, ImageDraw
    polys = polylines(path)
    xs = [x for _, p in polys for x, _ in p]
    ys = [y for _, p in polys for _, y in p]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    s = (size - 20) / max(x1 - x0, y1 - y0)
    im = Image.new("1", (int((x1 - x0) * s) + 20, int((y1 - y0) * s) + 20), 0)
    for closed, p in polys:   # even-odd fill: XOR each ring
        ring = Image.new("1", im.size, 0)
        ImageDraw.Draw(ring).polygon([((x - x0) * s + 10, (y1 - y) * s + 10) for x, y in p], fill=1)
        im = Image.frombytes("1", im.size, bytes(a ^ b for a, b in zip(im.tobytes(), ring.tobytes())))
    im.convert("L").point(lambda v: 255 - v).save(png)
    return (x0, y0, x1, y1), len(polys), sum(1 for c, _ in polys if c)
