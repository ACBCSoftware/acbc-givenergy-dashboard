#!/usr/bin/env python3
"""
Generate PWA icons for GivEnergy dashboard.
Uses the same lightning-bolt shape as the inline SVG favicon.
Run once:  python generate_icons.py
Requires Pillow:  pip install Pillow
"""

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("ERROR: Pillow not installed.")
    print("Install it into this project's virtual environment, e.g.:")
    print("  Windows:  venv\\Scripts\\pip.exe install Pillow")
    print("  Linux:    venv/bin/pip install Pillow")
    raise SystemExit(1)

from pathlib import Path

BG   = (13,  27,  53, 255)   # #0d1b35
BOLT = (245, 158, 11, 255)   # #f59e0b

# Bolt polygon derived from the inline SVG path  M18 2L9 18h6l-3 12 11-16h-6z
# in a 32×32 viewBox — normalised to 0-1 fractions
BOLT_NORM = [
    (18/32, 2/32),
    ( 9/32,18/32),
    (15/32,18/32),
    (12/32,30/32),
    (23/32,14/32),
    (17/32,14/32),
]

def bolt_poly(size, pad=0.12):
    """Scale normalised bolt to pixel coords with padding."""
    p   = size * pad
    s   = size - 2*p
    return [(p + rx*s, p + ry*s) for rx, ry in BOLT_NORM]

def make_icon(size, pad=0.12):
    img  = Image.new("RGBA", (size, size), BG)
    draw = ImageDraw.Draw(img)
    draw.polygon(bolt_poly(size, pad), fill=BOLT)
    return img

out = Path(__file__).parent / "icons"
out.mkdir(exist_ok=True)

specs = [
    ("icon-192.png",          192, 0.12),
    ("icon-512.png",          512, 0.12),
    ("icon-maskable-192.png", 192, 0.22),  # extra padding for safe-zone
    ("icon-maskable-512.png", 512, 0.22),
    ("apple-touch-icon.png",  180, 0.12),
]

for name, size, pad in specs:
    make_icon(size, pad).save(out / name)
    print(f"  {name}")

print(f"\nDone — icons saved to {out}")
