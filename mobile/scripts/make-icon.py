#!/usr/bin/env python3
"""Generate the DiscStation app icons from the website palette (src/static/style.css).

Flat print style: a terracotta optical disc on warm paper, ink grooves, a paper
light-glint. Run from anywhere:  python3 mobile/scripts/make-icon.py
"""
import math
import os

from PIL import Image, ImageDraw

PAPER = (240, 237, 228, 255)   # --paper  #f0ede4
INK = (26, 26, 26, 255)        # --ink    #1a1a1a
TERRA = (188, 115, 85, 255)    # --accent #bc7355
CLEAR = (0, 0, 0, 0)

SS = 4  # supersample factor for antialiasing
OUT = os.path.join(os.path.dirname(__file__), "..", "assets")


def disc(size, *, bg, scale=0.92, mono=False):
    """Return an RGBA icon `size`x`size`. `bg=None` -> transparent."""
    S = size * SS
    img = Image.new("RGBA", (S, S), bg if bg else CLEAR)
    d = ImageDraw.Draw(img)
    c = S / 2
    R = c * scale                      # outer disc radius
    ink = INK if not mono else (255, 255, 255, 255)
    face = ink if mono else TERRA
    hub_fill = CLEAR if mono else (bg or PAPER)

    def circle(r, **kw):
        d.ellipse([c - r, c - r, c + r, c + r], **kw)

    lw = max(2, int(S * 0.012))
    circle(R, fill=face, outline=ink, width=lw)              # disc body
    if not mono:
        for gr in (0.86, 0.72, 0.58):                       # grooves
            circle(R * gr, outline=ink, width=max(1, lw // 2))

    if not mono:                                            # paper light-glint
        glint = Image.new("RGBA", (S, S), CLEAR)
        gd = ImageDraw.Draw(glint)
        gd.pieslice([c - R, c - R, c + R, c + R], 208, 236, fill=PAPER)
        gd.ellipse([c - R * 0.34, c - R * 0.34, c + R * 0.34, c + R * 0.34], fill=CLEAR)
        band = Image.new("L", (S, S), 0)
        ImageDraw.Draw(band).ellipse(
            [c - R * 0.97, c - R * 0.97, c + R * 0.97, c + R * 0.97], fill=105)
        img.paste(glint, (0, 0), Image.composite(glint.split()[3], band, band))

    circle(R * 0.30, fill=hub_fill, outline=ink, width=lw)  # center hub
    circle(R * 0.13, fill=hub_fill, outline=ink, width=max(1, lw // 2))  # spindle hole

    return img.resize((size, size), Image.LANCZOS)


def save(img, name):
    path = os.path.normpath(os.path.join(OUT, name))
    img.save(path)
    print("wrote", os.path.relpath(path, os.path.join(OUT, "..", "..")))


# App / store icon — full bleed on paper.
save(disc(1024, bg=PAPER, scale=0.80), "icon.png")
# iOS/web favicon.
save(disc(48, bg=PAPER, scale=0.82), "favicon.png")
# Android adaptive layers — foreground disc sits inside the ~66% safe circle.
save(disc(1024, bg=None, scale=0.62), "android-icon-foreground.png")
save(Image.new("RGBA", (1024, 1024), PAPER), "android-icon-background.png")
save(disc(1024, bg=None, scale=0.62, mono=True), "android-icon-monochrome.png")
# Splash — transparent disc, small; splash bg color is set in app.json.
save(disc(1024, bg=None, scale=0.42), "splash-icon.png")
