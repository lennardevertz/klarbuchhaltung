"""Erzeugt die App-Icons im Klar-Look: Papier + grüner Balken-Trend.

Schreibt .icns (macOS), .ico (Windows) und .png (Linux/README). Nur nötig,
wenn das Icon geändert werden soll:
    .venv/bin/python packaging/make_icon.py
"""
import os
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw

HIER = os.path.dirname(os.path.abspath(__file__))
PAPER = (251, 249, 243, 255)   # --paper
INK = (22, 23, 29, 255)        # --ink
GREEN = (31, 169, 113, 255)    # --green


def zeichne(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = size * 0.055                 # macOS-Rand
    r = size * 0.225                 # Squircle-Radius
    d.rounded_rectangle([m, m, size - m, size - m], radius=r, fill=PAPER)

    inner = size - 2 * m
    base = size - m - inner * 0.26   # Balken: klein, mittel, groß
    bw, gap = inner * 0.135, inner * 0.075
    x = (size - (3 * bw + 2 * gap)) / 2
    for i, h in enumerate((inner * 0.20, inner * 0.33, inner * 0.48)):
        d.rounded_rectangle([x, base - h, x + bw, base],
                            radius=bw * 0.28, fill=GREEN if i == 2 else INK)
        x += bw + gap
    return img


def main():
    tmp = tempfile.mkdtemp()
    iconset = os.path.join(tmp, "icon.iconset")
    os.makedirs(iconset)
    for px in (16, 32, 128, 256, 512):
        zeichne(px).save(f"{iconset}/icon_{px}x{px}.png")
        zeichne(px * 2).save(f"{iconset}/icon_{px}x{px}@2x.png")
    if shutil.which("iconutil"):
        subprocess.run(["iconutil", "-c", "icns", iconset,
                        "-o", os.path.join(HIER, "Buchhaltung.icns")], check=True)
        print("packaging/Buchhaltung.icns geschrieben")
    else:
        print("iconutil fehlt (kein macOS) – .icns übersprungen")

    # Windows-Icon: mehrere Größen in einer .ico
    zeichne(256).save(os.path.join(HIER, "Buchhaltung.ico"), format="ICO",
                      sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("packaging/Buchhaltung.ico geschrieben")

    # Linux/AppImage und README mögen ein schlichtes PNG
    zeichne(512).save(os.path.join(HIER, "Buchhaltung.png"))
    print("packaging/Buchhaltung.png geschrieben")

    shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
