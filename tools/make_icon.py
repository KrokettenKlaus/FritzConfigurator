"""Erzeugt FritzConfigurator.ico (nur Standardbibliothek).

Motiv: Router mit leuchtender grüner Info-LED und Haken ("fertig konfiguriert") auf blauem Grund.
Gezeichnet über Distanzfelder mit Kantenglättung, Größen 16-256 px.

    py -3 tools\\make_icon.py            -> FritzConfigurator.ico neben dem Programm
    py -3 tools\\make_icon.py --png X    -> zusätzlich Vorschau (256 px) als PNG
"""
from __future__ import annotations

import math
import os
import struct
import sys
import zlib

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def hexrgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


# ------------------------------------------------------------ Distanzfelder
def sd_round_box(x, y, cx, cy, hw, hh, r):
    qx, qy = abs(x - cx) - hw + r, abs(y - cy) - hh + r
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r


def sd_circle(x, y, cx, cy, r):
    return math.hypot(x - cx, y - cy) - r


def sd_capsule(x, y, ax, ay, bx, by, r):
    pax, pay, bax, bay = x - ax, y - ay, bx - ax, by - ay
    h = max(0.0, min(1.0, (pax * bax + pay * bay) / (bax * bax + bay * bay)))
    return math.hypot(pax - bax * h, pay - bay * h) - r


# ------------------------------------------------------------------ Motiv
BG_A, BG_B = hexrgb("#3b8cf0"), hexrgb("#0b3f94")
WHITE = (1.0, 1.0, 1.0)
LED_ON, LED_GLOW, LED_OFF = hexrgb("#22c55e"), hexrgb("#86efac"), hexrgb("#a9bfdf")
BADGE = hexrgb("#16a34a")


def shapes(px: int):
    """Liste (sdf, farbe) von hinten nach vorne. px = Kantenlänge (kleine Größen: dickere Linien)."""
    small = px <= 24
    t = 0.05 if small else 0.034          # Antennenstärke
    bg = (lambda x, y: sd_round_box(x, y, .5, .5, .47, .47, .2), None)  # None = Verlauf
    ant = [(lambda x, y, a=a: sd_capsule(x, y, *a, t), WHITE) for a in
           (((.33, .52, .25, .2) if not small else (.32, .5, .24, .2)),
            ((.67, .52, .75, .2) if not small else (.68, .5, .76, .2)))]
    body = (lambda x, y: sd_round_box(x, y, .5, .615, .35, .13, .075), WHITE)
    out = [bg] + ant + [body]
    if small:
        out.append((lambda x, y: sd_circle(x, y, .32, .615, .075), LED_ON))
    else:
        out.append((lambda x, y: sd_circle(x, y, .29, .615, .072), LED_GLOW + (0.45,)))
        out.append((lambda x, y: sd_circle(x, y, .29, .615, .043), LED_ON))
        out.append((lambda x, y: sd_circle(x, y, .41, .615, .03), LED_OFF))
    # Haken-Abzeichen unten rechts
    bx, by, br = (.75, .75, .21) if small else (.755, .755, .195)
    out.append((lambda x, y: sd_circle(x, y, bx, by, br + (.05 if small else .035)), WHITE))
    out.append((lambda x, y: sd_circle(x, y, bx, by, br), BADGE))
    ck = .045 if small else .029
    p1, p2, p3 = (bx - .085, by + .005), (bx - .025, by + .065), (bx + .085, by - .065)
    out.append((lambda x, y: min(sd_capsule(x, y, *p1, *p2, ck), sd_capsule(x, y, *p2, *p3, ck)), WHITE))
    return out


def render(px: int):
    """RGBA-Bytes (zeilenweise von oben), straight alpha."""
    sh = shapes(px)
    buf = bytearray(px * px * 4)
    for j in range(px):
        y = (j + .5) / px
        for i in range(px):
            x = (i + .5) / px
            r = g = b = a = 0.0
            for fn, col in sh:
                cov = max(0.0, min(1.0, .5 - fn(x, y) * px))
                if cov <= 0:
                    continue
                if col is None:  # Hintergrund: diagonaler Verlauf + leichter Glanz oben
                    tt = max(0.0, min(1.0, (x * .45 + y * .75) / 1.2))
                    c = tuple(BG_A[k] + (BG_B[k] - BG_A[k]) * tt for k in range(3))
                    ca = 1.0
                else:
                    c, ca = col[:3], (col[3] if len(col) > 3 else 1.0)
                sa = cov * ca
                na = sa + a * (1 - sa)
                if na > 0:
                    r = (c[0] * sa + r * a * (1 - sa)) / na
                    g = (c[1] * sa + g * a * (1 - sa)) / na
                    b = (c[2] * sa + b * a * (1 - sa)) / na
                a = na
            o = (j * px + i) * 4
            buf[o:o + 4] = bytes((round(r * 255), round(g * 255), round(b * 255), round(a * 255)))
    return bytes(buf)


# ------------------------------------------------------------- Dateiformate
def png_bytes(px: int, rgba: bytes) -> bytes:
    raw = b"".join(b"\x00" + rgba[j * px * 4:(j + 1) * px * 4] for j in range(px))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", px, px, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def dib_bytes(px: int, rgba: bytes) -> bytes:
    """Klassischer ICO-Eintrag (BITMAPINFOHEADER, 32 bit BGRA von unten + AND-Maske)."""
    head = struct.pack("<IiiHHIIiiII", 40, px, px * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rows = []
    for j in range(px - 1, -1, -1):
        row = rgba[j * px * 4:(j + 1) * px * 4]
        rows.append(b"".join(bytes((row[i + 2], row[i + 1], row[i], row[i + 3])) for i in range(0, len(row), 4)))
    mask_row = ((px + 31) // 32) * 4
    mask = bytearray()
    for j in range(px - 1, -1, -1):
        bits = bytearray(mask_row)
        for i in range(px):
            if rgba[(j * px + i) * 4 + 3] == 0:
                bits[i // 8] |= 0x80 >> (i % 8)
        mask += bits
    return head + b"".join(rows) + bytes(mask)


def write_ico(path: str, images) -> None:
    entries, blobs = [], []
    offset = 6 + 16 * len(images)
    for px, rgba in images:
        blob = png_bytes(px, rgba) if px >= 256 else dib_bytes(px, rgba)
        dim = 0 if px >= 256 else px
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset))
        blobs.append(blob)
        offset += len(blob)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries) + b"".join(blobs))


def main(argv) -> int:
    images = [(px, render(px)) for px in SIZES]
    out = os.path.join(ROOT, "FritzConfigurator.ico")
    write_ico(out, images)
    print(out)
    if "--png" in argv:
        target = argv[argv.index("--png") + 1]
        os.makedirs(target, exist_ok=True)
        for px, rgba in images:
            with open(os.path.join(target, f"icon_{px}.png"), "wb") as fh:
                fh.write(png_bytes(px, rgba))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
