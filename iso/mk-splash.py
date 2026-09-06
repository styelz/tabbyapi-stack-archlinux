#!/usr/bin/env python3
"""Write a 640x480 syslinux vesamenu splash (stdlib only)."""
from __future__ import annotations

import struct
import sys
import zlib

# 5x7 caps used on the boot menu background.
FONT = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
}

BG = (11, 28, 58)
FG = (255, 255, 255)
ACCENT = (51, 204, 255)
BAR = (20, 90, 160)


def png_rgb(width: int, height: int, pixels: list[list[tuple[int, int, int]]]) -> bytes:
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for r, g, b in pixels[y]:
            raw.extend((r, g, b))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def blit_text(pixels, x, y, text, color, scale):
    cursor = x
    for ch in text.upper():
        glyph = FONT.get(ch, FONT[" "])
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        px = cursor + col * scale + dx
                        py = y + row * scale + dy
                        if 0 <= py < len(pixels) and 0 <= px < len(pixels[0]):
                            pixels[py][px] = color
        cursor += 6 * scale


def fill_rect(pixels, x0, y0, x1, y1, color):
    h = len(pixels)
    w = len(pixels[0])
    for y in range(max(0, y0), min(h, y1)):
        row = pixels[y]
        for x in range(max(0, x0), min(w, x1)):
            row[x] = color


def build() -> bytes:
    w, h = 640, 480
    pixels = [[BG for _ in range(w)] for _ in range(h)]
    fill_rect(pixels, 0, 0, w, 8, BAR)
    fill_rect(pixels, 0, h - 8, w, h, BAR)
    fill_rect(pixels, 40, 214, w - 40, 216, ACCENT)
    blit_text(pixels, 188, 150, "TSOS", FG, 8)
    blit_text(pixels, 214, 236, "INSTALLER", ACCENT, 3)
    blit_text(pixels, 96, 320, "ARCH LINUX + TABBYAPI-STACK", FG, 2)
    return png_rgb(w, h, pixels)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: mk-splash.py OUT.png", file=sys.stderr)
        return 2
    path = sys.argv[1]
    data = build()
    with open(path, "wb") as fh:
        fh.write(data)
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        print("mk-splash.py: did not write a PNG", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
