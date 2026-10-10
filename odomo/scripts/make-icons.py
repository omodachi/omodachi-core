#!/usr/bin/env python3
"""Draw Odomo's app icons from the Omodachi symbol, with no dependencies.

The symbol (omodachi-brand/concepts/v6-omodachi-symbol.svg, as omodachi-web's
`Icon.jsx` carries it) is made only of horizontal and vertical edges, so an
even-odd scanline fill at the pixel centres draws it exactly. The PNGs are
committed under public/ and are inputs to the build, not outputs of it; this
script is how they were made, and running it again writes the same bytes.

    python3 scripts/make-icons.py
"""
from pathlib import Path
import re
import struct
import zlib

SYMBOL = ("M0 20H4V12H8V8H12V4H20V0H68V4H76V8H80V12H84V20H88V68H84V76H80V80H76V84H68V88H20V84H12V80H8V76H4V68H0Z "
          "M24 32V56H28V60H32V64H56V60H60V56H64V32H60V28H56V24H32V28H28V32Z "
          "M32 36H40V48H32Z M48 36H56V48H48Z")
BACKGROUND = (0x1A, 0x1B, 0x26)   # Tokyo Night background, the theme Odomo opens in
INK = (0xC0, 0xCA, 0xF5)          # its bright foreground
OUT = Path(__file__).resolve().parents[1] / "public"


def polygons(path):
    shapes, current, x, y = [], [], 0.0, 0.0
    tokens = re.findall(r"[MHVZ]|-?[0-9.]+", path)
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "M":
            x, y = float(tokens[i + 1]), float(tokens[i + 2]); i += 3
            current = [(x, y)]
        elif token == "H":
            x = float(tokens[i + 1]); i += 2; current.append((x, y))
        elif token == "V":
            y = float(tokens[i + 1]); i += 2; current.append((x, y))
        elif token == "Z":
            shapes.append(current); current = []; i += 1
        else:
            raise ValueError(token)
    return shapes


def inside(shapes, px, py):
    crossings = 0
    for shape in shapes:
        for (x1, y1), (x2, y2) in zip(shape, shape[1:] + shape[:1]):
            if x1 == x2 and min(y1, y2) <= py < max(y1, y2) and px < x1:
                crossings += 1
    return crossings % 2 == 1


def draw(size, fraction):
    shapes = polygons(SYMBOL)
    side = size * fraction           # the symbol's 88 units, in pixels
    origin = (size - side) / 2
    rows = []
    for row in range(size):
        line = bytearray([0])
        for column in range(size):
            u = (column + 0.5 - origin) / side * 88
            v = (row + 0.5 - origin) / side * 88
            line += bytes(INK if 0 <= u < 88 and 0 <= v < 88 and inside(shapes, u, v) else BACKGROUND)
        rows.append(bytes(line))
    raw = zlib.compress(b"".join(rows), 9)
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def main():
    # A full icon carries the symbol at 62% of its edge; a maskable one keeps
    # it inside the 80% safe circle, so a little smaller.
    for name, size, fraction in (("icon-192.png", 192, 0.62), ("icon-512.png", 512, 0.62),
                                 ("icon-512-maskable.png", 512, 0.5), ("apple-touch-icon.png", 180, 0.62)):
        (OUT / name).write_bytes(draw(size, fraction))
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" shape-rendering="crispEdges">'
           '<rect width="96" height="96" fill="#1a1b26"/>'
           '<g transform="translate(16.7 16.7) scale(0.7)" fill="#c0caf5">'
           f'<path fill-rule="evenodd" d="{SYMBOL.rsplit(" M32", 1)[0]}"/>'
           '<path d="M32 36H40V48H32Z M48 36H56V48H48Z"/></g></svg>\n')
    (OUT / "icon.svg").write_text(svg)


if __name__ == "__main__":
    main()
