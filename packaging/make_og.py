"""Erzeugt site/og.png (1200x630) fuer Social-Previews.

Aufruf: python3 packaging/make_og.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WURZEL = Path(__file__).resolve().parent.parent

W, H = 1200, 630
INK = (0x16, 0x17, 0x1D)
PAPER = (0xFB, 0xF9, 0xF3)
GREEN = (0x1F, 0xA9, 0x71)
INK2 = (0x5C, 0x5F, 0x6E)

BOLD = str(WURZEL / "fonts" / "Body-Bold.ttf")
REG = str(WURZEL / "fonts" / "Body-Regular.ttf")

img = Image.new("RGB", (W, H), PAPER)
d = ImageDraw.Draw(img)

# dezentes Punktraster wie auf der Seite
for y in range(0, H, 24):
    for x in range(0, W, 24):
        d.point((x, y), fill=(0xEC, 0xE9, 0xE0))

PAD = 78

# Bildmarke: dunkles Squircle mit drei Balken (wie site/favicon + App-Icon)
S = 76
mx, my = PAD, PAD
d.rounded_rectangle([mx, my, mx + S, my + S], radius=19, fill=INK)


def bar(bx, by, bw, bh, col):
    d.rounded_rectangle(
        [mx + bx, my + by, mx + bx + bw, my + by + bh], radius=3, fill=col
    )


k = S / 89.0
bar(19.80 * k, 48.06 * k, 12.02 * k, 17.80 * k, PAPER)
bar(38.49 * k, 36.49 * k, 12.02 * k, 29.37 * k, PAPER)
bar(57.18 * k, 23.14 * k, 12.02 * k, 42.72 * k, GREEN)

d.text((mx + S + 22, my + 20), "Klar", font=ImageFont.truetype(BOLD, 40), fill=INK)

f_h1 = ImageFont.truetype(BOLD, 68)
f_sub = ImageFont.truetype(REG, 31)

y = 250
for line in ["Buchhaltung für", "Freiberufler — kostenlos."]:
    d.text((PAD, y), line, font=f_h1, fill=INK)
    y += 84

y += 26
d.text(
    (PAD, y),
    "Rechnungen, EÜR und USt-Voranmeldung.",
    font=f_sub,
    fill=INK2,
)
d.text((PAD, y + 44), "Open Source, ohne Abo, läuft auf deinem Rechner.", font=f_sub, fill=INK2)

# Akzentbalken unten
d.rectangle([0, H - 10, W, H], fill=GREEN)

img.save(WURZEL / "site" / "og.png", optimize=True)
print("ok")
