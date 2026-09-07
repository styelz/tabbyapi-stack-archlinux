#!/usr/bin/env python3
"""Write TSOS boot art (stdlib only): syslinux splash, Plymouth logo, spinner."""
from __future__ import annotations

import argparse
import math
import struct
import sys
import zlib
from pathlib import Path

FONT = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01110"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00001", "00001", "00001", "10001", "10001", "01110"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10001", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10001", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
}

BG = (11, 28, 58)
FG = (255, 255, 255)
ACCENT = (51, 204, 255)
BAR = (20, 90, 160)
STACK = ((36, 168, 220), (51, 204, 255), (180, 236, 255))


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


def fill_round_rect(pixels, x0, y0, x1, y1, radius, color):
    for y in range(y0, y1):
        for x in range(x0, x1):
            cx = x0 + radius if x < x0 + radius else (x1 - 1 - radius if x >= x1 - radius else x)
            cy = y0 + radius if y < y0 + radius else (y1 - 1 - radius if y >= y1 - radius else y)
            if x != cx or y != cy:
                dx = x - cx
                dy = y - cy
                if dx * dx + dy * dy > radius * radius:
                    continue
            if 0 <= y < len(pixels) and 0 <= x < len(pixels[0]):
                pixels[y][x] = color


def draw_ring_arc(pixels, cx, cy, outer, inner, start, end, color):
    h = len(pixels)
    w = len(pixels[0])
    pad = outer + 1
    for y in range(cy - pad, cy + pad + 1):
        for x in range(cx - pad, cx + pad + 1):
            dx = x - cx
            dy = y - cy
            dist = math.sqrt(dx * dx + dy * dy)
            if dist < inner or dist > outer:
                continue
            ang = math.atan2(dy, dx)
            if ang < 0:
                ang += 2 * math.pi
            if start <= end:
                ok = start <= ang <= end
            else:
                ok = ang >= start or ang <= end
            if ok and 0 <= y < h and 0 <= x < w:
                pixels[y][x] = color


def canvas(width: int, height: int, color=BG):
    return [[color for _ in range(width)] for _ in range(height)]


def write_png(path: Path, pixels) -> None:
    data = png_rgb(len(pixels[0]), len(pixels), pixels)
    path.write_bytes(data)
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"did not write a PNG: {path}")


def stack_mark(pixels, x, y, w=72, h=72):
    gap = 8
    bar_h = (h - 2 * gap) // 3
    for i, color in enumerate(STACK):
        top = y + i * (bar_h + gap)
        inset = i * 4
        fill_round_rect(pixels, x + inset, top, x + w - inset, top + bar_h, 8, color)


def build_splash() -> list[list[tuple[int, int, int]]]:
    w, h = 640, 480
    pixels = canvas(w, h)
    fill_rect(pixels, 0, 0, w, 8, BAR)
    fill_rect(pixels, 0, h - 8, w, h, BAR)
    stack_mark(pixels, 148, 168, 78, 78)
    blit_text(pixels, 246, 178, "TSOS", FG, 8)
    blit_text(pixels, 214, 280, "INSTALLER", ACCENT, 3)
    blit_text(pixels, 96, 340, "ARCH LINUX + TABBYAPI-STACK", FG, 2)
    return pixels


def build_logo() -> list[list[tuple[int, int, int]]]:
    w, h = 560, 180
    pixels = canvas(w, h)
    stack_mark(pixels, 24, 54, 88, 88)
    blit_text(pixels, 136, 58, "TSOS", FG, 8)
    blit_text(pixels, 140, 130, "TABBYAPI-STACK", ACCENT, 2)
    return pixels


def build_spinner() -> list[list[tuple[int, int, int]]]:
    size = 96
    pixels = canvas(size, size)
    cx = cy = size // 2
    draw_ring_arc(pixels, cx, cy, 36, 26, 0.35, 5.2, ACCENT)
    draw_ring_arc(pixels, cx, cy, 36, 26, 5.2, 5.9, (180, 236, 255))
    return pixels


def build_caption(text: str, scale: int, color) -> list[list[tuple[int, int, int]]]:
    width = len(text) * 6 * scale
    height = 7 * scale
    pixels = canvas(width, height)
    blit_text(pixels, 0, 0, text, color, scale)
    return pixels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        help="syslinux splash.png, or a directory that receives splash.png, logo.png, spinner.png",
    )
    args = parser.parse_args(argv)
    target = Path(args.target)
    if target.suffix.lower() == ".png":
        out_dir = target.parent
        splash_path = target
    else:
        out_dir = target
        splash_path = out_dir / "splash.png"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_png(splash_path, build_splash())
    write_png(out_dir / "logo.png", build_logo())
    write_png(out_dir / "spinner.png", build_spinner())
    write_png(out_dir / "loading.png", build_caption("LOADING", 4, FG))
    write_png(out_dir / "please-wait.png", build_caption("PLEASE WAIT", 3, ACCENT))
    if not splash_path.stat().st_size:
        print("boot splash PNG was not written", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
