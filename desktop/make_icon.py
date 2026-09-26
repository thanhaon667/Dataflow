"""
Generate the ERP Desk app icon (desktop/static/app.ico + app-192.png) in code -
no binary asset is hand-made. The mark is the project's brand square: a coral ->
violet gradient tile, here carrying three rising "report" bars and a live dot.

Uses Pillow when it is importable (it ships with matplotlib, so it is already in
the venv). If Pillow is missing, falls back to a stdlib-only writer that still
produces a valid multi-size .ico (plain gradient tile + bars, no anti-aliasing
beyond a 4x supersample).

Run:
    venv\\Scripts\\python.exe -m desktop.make_icon
"""
from __future__ import annotations

import logging
import struct
import sys
import zlib
from pathlib import Path

logger = logging.getLogger("erp_desk.icon")

STATIC_DIR = Path(__file__).resolve().parent / "static"
ICO_PATH = STATIC_DIR / "app.ico"
PNG_PATH = STATIC_DIR / "app-192.png"

CORAL = (255, 90, 54)
VIOLET = (124, 92, 255)
AMBER = (242, 183, 5)
SIZES = [16, 24, 32, 48, 64, 128, 256]

# Geometry in a 0..1 unit square: (x0, y0, x1, y1) of the three bars, then the dot.
BARS = [(0.22, 0.52, 0.36, 0.76), (0.43, 0.38, 0.57, 0.76), (0.64, 0.24, 0.78, 0.76)]
DOT = (0.71, 0.155, 0.055)  # cx, cy, r
CORNER = 0.23


def _mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


# ---------------------------------------------------------------------------
# Pillow path
# ---------------------------------------------------------------------------
def _render_pillow(size: int):
    from PIL import Image, ImageDraw

    ss = 4
    n = size * ss
    grad = Image.new("RGBA", (n, n))
    px = grad.load()
    for y in range(n):
        for x in range(n):
            px[x, y] = _mix(CORAL, VIOLET, (x + y) / (2 * n - 2)) + (255,)

    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, n - 1, n - 1], radius=int(n * CORNER), fill=255)
    tile = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    tile.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(tile)
    for x0, y0, x1, y1 in BARS:
        d.rounded_rectangle([x0 * n, y0 * n, x1 * n, y1 * n], radius=int(0.035 * n), fill=(255, 255, 255, 240))
    cx, cy, r = DOT
    d.ellipse([(cx - r) * n, (cy - r) * n, (cx + r) * n, (cy + r) * n], fill=AMBER + (255,))
    return tile.resize((size, size), Image.LANCZOS)


def _write_pillow() -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    big = _render_pillow(256)
    big.save(ICO_PATH, format="ICO", sizes=[(s, s) for s in SIZES])
    _render_pillow(192).save(PNG_PATH, format="PNG")


# ---------------------------------------------------------------------------
# Stdlib-only fallback
# ---------------------------------------------------------------------------
def _pixel(u: float, v: float):
    """RGBA for a point in the unit square (analytic shapes, no image lib)."""
    # rounded-rect coverage
    cx = min(max(u, CORNER), 1 - CORNER)
    cy = min(max(v, CORNER), 1 - CORNER)
    if (u - cx) ** 2 + (v - cy) ** 2 > CORNER ** 2:
        return (0, 0, 0, 0)
    dcx, dcy, dr = DOT
    if (u - dcx) ** 2 + (v - dcy) ** 2 <= dr ** 2:
        return AMBER + (255,)
    for x0, y0, x1, y1 in BARS:
        if x0 <= u <= x1 and y0 <= v <= y1:
            return (255, 255, 255, 240)
    return _mix(CORAL, VIOLET, (u + v) / 2) + (255,)


def _render_raw(size: int) -> bytes:
    """size x size RGBA, top-down, 3x3 supersampled."""
    out = bytearray()
    ss = 3
    for y in range(size):
        for x in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    r, g, b, a = _pixel((x + (sx + 0.5) / ss) / size, (y + (sy + 0.5) / ss) / size)
                    acc[0] += r * a
                    acc[1] += g * a
                    acc[2] += b * a
                    acc[3] += a
            total = ss * ss
            alpha = acc[3] / total
            if acc[3]:
                out += bytes((int(acc[0] / acc[3]), int(acc[1] / acc[3]), int(acc[2] / acc[3]), int(alpha)))
            else:
                out += bytes(4)
    return bytes(out)


def _png_bytes(size: int, raw_rgba: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + raw_rgba[y * size * 4:(y + 1) * size * 4] for y in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def _write_stdlib() -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    images = [(s, _png_bytes(s, _render_raw(s))) for s in SIZES]  # PNG-compressed ICO entries (Vista+)
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    ICO_PATH.write_bytes(header + entries + blobs)
    PNG_PATH.write_bytes(_png_bytes(192, _render_raw(192)))


def generate(force_stdlib: bool = False) -> Path:
    """Write app.ico + app-192.png, return the .ico path."""
    if not force_stdlib:
        try:
            _write_pillow()
            logger.info("Icon written with Pillow: %s", ICO_PATH)
            return ICO_PATH
        except ImportError:
            logger.warning("Pillow not available - using the stdlib icon writer")
    _write_stdlib()
    logger.info("Icon written (stdlib): %s", ICO_PATH)
    return ICO_PATH


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    path = generate(force_stdlib="--stdlib" in sys.argv)
    print(f"Icon saved to: {path}")
