#!/usr/bin/env python3
"""Draw management-UI mock screenshots with sample data. No GPU / no live server.

Outputs JPGs under docs/ for the README. Re-run:

  python3 docs/make_ui_screenshots.py
"""

from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
OUT = HERE
FONT_SANS = Path("/usr/share/fonts/Adwaita/AdwaitaSans-Regular.ttf")
FONT_MONO = Path("/usr/share/fonts/Adwaita/AdwaitaMono-Regular.ttf")

W, H = 1280, 820
BG = (8, 11, 17)
ELEV = (14, 19, 29)
ELEV2 = (22, 29, 43)
LINE = (42, 53, 70)
TEXT = (238, 243, 250)
MUTED = (137, 151, 171)
ACCENT = (105, 166, 255)
ACCENT2 = (164, 112, 255)
OK = (74, 222, 154)
WARN = (250, 198, 78)
BAD = (255, 105, 128)
CHART_BG = (7, 10, 16)


def font(size: int, *, mono: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_MONO if mono else FONT_SANS
    if path.exists():
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def rr(draw: ImageDraw.ImageDraw, box, radius: int, fill, outline=None, width: int = 1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def header(img: Image.Image, active: str) -> ImageDraw.ImageDraw:
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, W, 64), fill=(10, 14, 21))
    draw.line((0, 64, W, 64), fill=LINE)

    # brand mark
    mark = Image.new("RGBA", (42, 42), (0, 0, 0, 0))
    md = ImageDraw.Draw(mark)
    md.rounded_rectangle((0, 0, 41, 41), 13, fill=(20, 28, 43), outline=LINE)
    md.polygon(((8, 9), (31, 9), (23, 20), (33, 20), (17, 34), (20, 23), (8, 23)), fill=ACCENT)
    img.paste(mark, (18, 11), mark)

    draw.text((72, 11), "TABBY STACK", fill=ACCENT, font=font(11))
    titles = {"logs": "Logs", "chat": "Chat", "status": "Status", "gallery": "Gallery"}
    draw.text((72, 28), titles[active], fill=TEXT, font=font(18))

    tabs = [("chat", "Chat"), ("status", "Status"), ("gallery", "Gallery"), ("logs", "Logs")]
    x = 280
    for key, label in tabs:
        tw = 78
        box = (x, 16, x + tw, 48)
        if key == active:
            rr(draw, box, 999, fill=ELEV2, outline=LINE)
            draw.text((x + 18, 24), label, fill=TEXT, font=font(14))
        else:
            draw.text((x + 18, 24), label, fill=MUTED, font=font(14))
        x += tw + 8

    # chips
    rr(draw, (W - 320, 18, W - 190, 46), 999, fill=(17, 48, 39), outline=(36, 98, 76))
    draw.ellipse((W - 307, 29, W - 299, 37), fill=OK)
    draw.text((W - 291, 24), "LLM · qwen", fill=OK, font=font(12))
    rr(draw, (W - 180, 18, W - 100, 46), 999, fill=None, outline=LINE)
    draw.text((W - 168, 24), "pbp", fill=MUTED, font=font(12))
    draw.text((W - 88, 24), "Log out", fill=MUTED, font=font(12))
    return draw


def card(draw: ImageDraw.ImageDraw, box, title: str, value: str, extra: str = ""):
    rr(draw, box, 14, fill=ELEV, outline=LINE)
    x0, y0, x1, y1 = box
    draw.text((x0 + 16, y0 + 14), title, fill=MUTED, font=font(13))
    draw.text((x0 + 16, y0 + 40), value, fill=TEXT, font=font(20))
    if extra:
        draw.text((x0 + 16, y0 + 72), extra[:48], fill=MUTED, font=font(12))


def spark_series(n: int, base: float, amp: float, seed: int) -> list[float]:
    rng = random.Random(seed)
    vals = []
    v = base
    for i in range(n):
        v += rng.uniform(-amp, amp) + 0.15 * math.sin(i / 7.0)
        v = max(5.0, min(98.0, v))
        vals.append(v)
    return vals


def draw_chart(
    draw: ImageDraw.ImageDraw,
    box,
    title: str,
    legend: list[tuple[str, tuple[int, int, int]]],
    series: list[tuple[tuple[int, int, int], list[float]]],
    y_max: float = 100.0,
):
    rr(draw, box, 12, fill=CHART_BG, outline=LINE)
    x0, y0, x1, y1 = box
    draw.text((x0 + 14, y0 + 10), title, fill=TEXT, font=font(13))
    lx = x0 + 90
    for label, color in legend:
        draw.ellipse((lx, y0 + 14, lx + 8, y0 + 22), fill=color)
        draw.text((lx + 12, y0 + 10), label, fill=MUTED, font=font(11))
        lx += 14 + draw.textlength(label, font=font(11)) + 14

    plot = (x0 + 44, y0 + 40, x1 - 14, y1 - 28)
    px0, py0, px1, py1 = plot
    draw.rectangle(plot, fill=(12, 14, 18))
    for i in range(5):
        y = py0 + (py1 - py0) * i / 4
        draw.line((px0, y, px1, y), fill=LINE)
        val = int(y_max * (1 - i / 4))
        draw.text((px0 - 36, y - 6), str(val), fill=MUTED, font=font(10, mono=True))

    for color, vals in series:
        if len(vals) < 2:
            continue
        pts = []
        for i, v in enumerate(vals):
            x = px0 + (px1 - px0) * i / (len(vals) - 1)
            y = py1 - (py1 - py0) * min(y_max, max(0, v)) / y_max
            pts.append((x, y))
        draw.line(pts, fill=color, width=2)

    # time labels
    for i, label in enumerate(("14:00", "16:00", "18:00", "20:00", "now")):
        x = px0 + (px1 - px0) * i / 4
        draw.text((x - 14, py1 + 8), label, fill=MUTED, font=font(10, mono=True))


def make_status() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    draw = header(img, "status")

    rr(draw, (18, 80, 100, 112), 10, fill=ELEV2, outline=LINE)
    draw.text((36, 88), "Refresh", fill=TEXT, font=font(13))
    draw.text((W - 220, 88), "2026-08-24T04:12:08Z", fill=MUTED, font=font(12, mono=True))

    # Left stacked panels
    side_x0, side_x1 = 18, 310
    cards = [
        ("GPU mode", "llm", "Comfy idle"),
        ("Profile", "qwen", "Qwen3.5-9B-exl3-4.00bpw"),
        ("Context", "262144", "cache FP8"),
        ("Health", "healthy", "no issues"),
        ("Uptime", "3h 42m", "http://gpu-host:5000/v1"),
        ("NVIDIA", "RTX 4070 Ti", "8142 / 12282 MiB · 62% · 61°C"),
        ("CPU / load", "28%", "load 1.84"),
        ("RAM", "47%", ""),
    ]
    y = 124
    for t, v, e in cards:
        box = (side_x0, y, side_x1, y + 62)
        rr(draw, box, 12, fill=ELEV, outline=LINE)
        draw.text((side_x0 + 12, y + 8), t, fill=MUTED, font=font(11))
        draw.text((side_x0 + 12, y + 26), v, fill=TEXT, font=font(16))
        if e:
            draw.text((side_x0 + 12, y + 46), e[:42], fill=MUTED, font=font(11))
        y += 68

    rr(draw, (side_x0, y, side_x1, min(H - 18, y + 170)), 14, fill=ELEV, outline=LINE)
    draw.text((side_x0 + 14, y + 12), "Actions", fill=MUTED, font=font(13))
    for i, label in enumerate(("qwen ▾", "Load LLM", "Hand GPU to Comfy", "Restart stack")):
        by = y + 38 + i * 32
        if by + 26 > H - 28:
            break
        fill = (58, 24, 30) if "Restart" in label else ELEV2
        outline = (120, 50, 60) if "Restart" in label else LINE
        rr(draw, (side_x0 + 12, by, side_x1 - 12, by + 26), 8, fill=fill, outline=outline)
        draw.text((side_x0 + 22, by + 5), label, fill=TEXT, font=font(12))

    # Right graphs panel
    panel = (322, 124, W - 18, H - 18)
    rr(draw, panel, 14, fill=ELEV, outline=LINE)
    draw.text((338, 138), "Graphs", fill=TEXT, font=font(13))

    # compact segmented range
    seg_x = 400
    segs = (("1h", False), ("6h", False), ("24h", True), ("7d", False), ("30d", False))
    rr(draw, (seg_x, 134, seg_x + 5 * 42, 160), 8, fill=BG, outline=LINE)
    for i, (label, on) in enumerate(segs):
        x0 = seg_x + i * 42
        if on:
            draw.rectangle((x0 + 1, 135, x0 + 41, 159), fill=(40, 55, 90))
            draw.text((x0 + 10, 140), label, fill=TEXT, font=font(12))
        else:
            draw.text((x0 + 10, 140), label, fill=MUTED, font=font(12))
        if i < 4:
            draw.line((x0 + 42, 136, x0 + 42, 158), fill=LINE)

    # custom amount + unit + Go
    cx = seg_x + 5 * 42 + 10
    rr(draw, (cx, 134, cx + 118, 160), 8, fill=BG, outline=LINE)
    draw.text((cx + 8, 140), "24", fill=TEXT, font=font(12, mono=True))
    draw.line((cx + 40, 136, cx + 40, 158), fill=LINE)
    draw.text((cx + 48, 140), "h ▾", fill=MUTED, font=font(12))
    draw.line((cx + 78, 136, cx + 78, 158), fill=LINE)
    draw.text((cx + 88, 140), "Go", fill=ACCENT, font=font(12))
    draw.text((W - 200, 140), "240 pts · 24h · ~30s", fill=MUTED, font=font(11))

    n = 48
    gpu = spark_series(n, 55, 12, 1)
    vram = spark_series(n, 68, 6, 2)
    temp = [min(85, 45 + v * 0.35) for v in gpu]
    cpu = spark_series(n, 25, 10, 3)
    ram = spark_series(n, 45, 4, 4)
    load = [v / 10 for v in spark_series(n, 18, 8, 5)]

    mid = (124 + H - 18) // 2 + 20
    draw_chart(
        draw,
        (338, 172, W - 34, mid - 6),
        "GPU",
        [("util %", ACCENT), ("VRAM %", ACCENT2), ("°C", WARN)],
        [(ACCENT, gpu), (ACCENT2, vram), (WARN, temp)],
    )
    draw_chart(
        draw,
        (338, mid + 6, W - 34, H - 34),
        "Host",
        [("CPU %", OK), ("RAM %", BAD), ("load×10", MUTED)],
        [(OK, cpu), (BAD, ram), (MUTED, [v * 10 for v in load])],
    )
    return img


def make_logs() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    draw = header(img, "logs")
    rr(draw, (18, 80, 100, 112), 10, fill=ELEV2, outline=LINE)
    draw.text((40, 88), "Pause", fill=TEXT, font=font(13))
    rr(draw, (110, 80, 420, 112), 10, fill=BG, outline=LINE)
    draw.text((122, 88), "Filter logs…", fill=MUTED, font=font(13))

    rr(draw, (18, 124, W - 18, H - 18), 14, fill=CHART_BG, outline=LINE)
    lines = [
        ("info", "2026-08-24 04:10:01 | INFO     | Management UI: http://127.0.0.1:5000/v1/ui"),
        ("info", "2026-08-24 04:10:02 | INFO     | Starting OAI API"),
        ("debug", "2026-08-24 04:10:33 | DEBUG    | Metrics sample recorded (cpu=22.1 gpu=58)"),
        ("info", "2026-08-24 04:11:02 | INFO     | [comfy] idle"),
        ("warn", "2026-08-24 04:11:18 | WARNING  | Client disconnected during stream"),
        ("info", "2026-08-24 04:11:40 | INFO     | Chat completion finished tokens=842"),
        ("info", "2026-08-24 04:12:01 | INFO     | GPU mode=llm profile=qwen"),
        ("error", "2026-08-24 04:12:14 | ERROR    | (example) image job cancelled by restart"),
        ("info", "2026-08-24 04:12:40 | INFO     | Health check ok"),
        ("info", "2026-08-24 04:13:02 | INFO     | Switch to qwen complete (~65s)"),
    ]
    colors = {
        "info": (183, 196, 255),
        "debug": (138, 147, 166),
        "warn": WARN,
        "error": (255, 139, 150),
    }
    y = 140
    for kind, line in lines:
        draw.text((34, y), line, fill=colors[kind], font=font(12, mono=True))
        y += 22
    return img


def make_gallery() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    draw = header(img, "gallery")
    rr(draw, (18, 80, 110, 112), 10, fill=ELEV2, outline=LINE)
    draw.text((36, 88), "Refresh", fill=TEXT, font=font(13))
    rr(draw, (120, 80, 230, 112), 10, fill=(58, 24, 30), outline=(120, 50, 60))
    draw.text((138, 88), "Delete…", fill=BAD, font=font(13))
    draw.text((W - 160, 88), "Page 1 / 3", fill=MUTED, font=font(13))

    thumbs = [
        ("rainy-neon-diner.png", "diner"),
        ("alpine-lake.png", "lake"),
        ("harbor-cafe-logo.png", "logo"),
        ("desert-observatory.png", "desert"),
        ("night-train.png", "train"),
        ("paper-fox.png", "fox"),
        ("mars-outpost.png", "mars"),
        ("botanical-study.png", "plant"),
    ]
    gap, tw, th = 14, 290, 210
    for i, (name, scene) in enumerate(thumbs):
        col, row = i % 4, i // 4
        x = 18 + col * (tw + gap)
        y = 128 + row * (th + gap)
        rr(draw, (x, y, x + tw, y + th), 12, fill=ELEV, outline=LINE)
        draw_thumbnail(draw, (x + 1, y + 1, x + tw - 1, y + th - 44), scene)
        draw.rectangle((x + 1, y + th - 44, x + tw - 1, y + th - 1), fill=ELEV)
        draw.text((x + 12, y + th - 32), name[:34], fill=MUTED, font=font(11))
        # checkbox
        rr(draw, (x + tw - 36, y + 10, x + tw - 12, y + 34), 6, fill=(0, 0, 0), outline=LINE)
    return img


def draw_thumbnail(draw: ImageDraw.ImageDraw, box, scene: str) -> None:
    """Small illustrated samples make the gallery read like a real image library."""
    x0, y0, x1, y1 = box
    palettes = {
        "diner": ((18, 27, 58), (238, 66, 140), (62, 202, 232)),
        "lake": ((53, 95, 137), (139, 195, 210), (28, 73, 78)),
        "logo": ((244, 226, 183), (24, 54, 62), (206, 104, 57)),
        "desert": ((64, 35, 57), (239, 140, 79), (252, 210, 135)),
        "train": ((15, 29, 51), (62, 126, 163), (246, 190, 83)),
        "fox": ((228, 218, 200), (222, 105, 58), (46, 55, 65)),
        "mars": ((74, 31, 29), (194, 74, 47), (239, 173, 102)),
        "plant": ((24, 52, 44), (84, 151, 105), (219, 196, 135)),
    }
    sky, mid, light = palettes[scene]
    h = y1 - y0
    for i in range(h):
        t = i / max(1, h - 1)
        c = tuple(int(sky[j] * (1 - t) + mid[j] * t) for j in range(3))
        draw.line((x0, y0 + i, x1, y0 + i), fill=c)

    if scene in {"diner", "train"}:
        draw.rectangle((x0 + 34, y0 + 58, x1 - 32, y1 - 18), fill=(13, 19, 28))
        for wx in range(x0 + 52, x1 - 45, 44):
            draw.rectangle((wx, y0 + 72, wx + 24, y0 + 100), fill=light)
        draw.line((x0, y1 - 15, x1, y1 - 15), fill=light, width=3)
    elif scene in {"lake", "desert", "mars"}:
        draw.polygon(((x0, y1), (x0 + 70, y0 + 52), (x0 + 126, y1), (x0 + 190, y0 + 35), (x1, y1)), fill=mid)
        draw.polygon(((x0 + 138, y0 + 77), (x0 + 190, y0 + 35), (x0 + 221, y0 + 80)), fill=light)
        draw.ellipse((x1 - 66, y0 + 18, x1 - 30, y0 + 54), fill=light)
    elif scene == "logo":
        draw.ellipse((x0 + 78, y0 + 27, x0 + 210, y0 + 135), fill=(246, 232, 199), outline=mid, width=4)
        draw.arc((x0 + 112, y0 + 51, x0 + 174, y0 + 107), 0, 180, fill=mid, width=5)
        draw.text((x0 + 77, y1 - 33), "HARBOR CAFE", fill=mid, font=font(18))
    elif scene == "fox":
        draw.polygon(((x0 + 92, y0 + 42), (x0 + 140, y0 + 18), (x0 + 190, y0 + 45), (x0 + 174, y0 + 124), (x0 + 115, y0 + 124)), fill=mid)
        draw.polygon(((x0 + 96, y0 + 43), (x0 + 104, y0 + 8), (x0 + 136, y0 + 28)), fill=mid)
        draw.polygon(((x0 + 185, y0 + 43), (x0 + 179, y0 + 8), (x0 + 148, y0 + 27)), fill=mid)
        draw.ellipse((x0 + 126, y0 + 68, x0 + 134, y0 + 76), fill=sky)
        draw.ellipse((x0 + 158, y0 + 68, x0 + 166, y0 + 76), fill=sky)
    else:
        for ox, oy, r in ((80, 84, 35), (132, 61, 42), (194, 88, 37)):
            draw.ellipse((x0 + ox - r, y0 + oy - r, x0 + ox + r, y0 + oy + r), fill=mid)
            draw.line((x0 + ox, y0 + oy, x0 + 145, y1), fill=light, width=4)


def make_ide_preview() -> Image.Image:
    """Polished editor + live preview hero for the top of the README."""
    pw, ph = 1440, 900
    img = Image.new("RGB", (pw, ph), (7, 10, 16))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, pw, 48), fill=(13, 18, 27))
    draw.ellipse((18, 17, 30, 29), fill=BAD)
    draw.ellipse((38, 17, 50, 29), fill=WARN)
    draw.ellipse((58, 17, 70, 29), fill=OK)
    draw.text((94, 15), "tabbyapi-stack  —  Cursor", fill=MUTED, font=font(13))

    # Explorer.
    draw.rectangle((0, 48, 228, ph), fill=(10, 14, 21))
    draw.text((22, 74), "EXPLORER", fill=MUTED, font=font(11))
    draw.text((22, 105), "▾  HARBOR", fill=TEXT, font=font(13, mono=True))
    files = ("▾  images", "   header.png", "   logo.png", "index.html", "styles.css")
    for i, name in enumerate(files):
        color = ACCENT if name == "index.html" else MUTED
        draw.text((34, 140 + i * 31), name, fill=color, font=font(13, mono=True))

    # Browser preview.
    bx0, by0, bx1, by1 = 228, 48, 1000, ph
    draw.rectangle((bx0, by0, bx1, by1), fill=(245, 239, 226))
    draw.rectangle((bx0, by0, bx1, 96), fill=(22, 29, 39))
    rr(draw, (bx0 + 110, 61, bx1 - 110, 84), 8, fill=(9, 13, 20), outline=LINE)
    draw.text((bx0 + 132, 66), "http://127.0.0.1:5173", fill=MUTED, font=font(11, mono=True))
    draw.rectangle((bx0, 96, bx1, 158), fill=(252, 248, 239))
    draw.text((bx0 + 34, 115), "HARBOR", fill=(24, 55, 64), font=font(22))
    draw.text((bx1 - 240, 119), "Menu     Story     Visit", fill=(76, 78, 74), font=font(12))

    # Hero scene.
    for i in range(340):
        t = i / 339
        c = (int(19 + 49 * t), int(43 + 61 * t), int(62 + 63 * t))
        draw.line((bx0, 158 + i, bx1, 158 + i), fill=c)
    draw.ellipse((bx1 - 210, 202, bx1 - 125, 287), fill=(246, 194, 109))
    draw.polygon(((bx0, 498), (bx0 + 165, 300), (bx0 + 335, 498), (bx0 + 530, 250), (bx1, 498)), fill=(20, 57, 66))
    draw.rectangle((bx0 + 56, 318, bx0 + 432, 458), fill=(12, 26, 34))
    draw.rectangle((bx0 + 76, 338, bx0 + 412, 438), outline=(219, 155, 78), width=2)
    draw.text((bx0 + 91, 350), "COFFEE BY THE WATER", fill=(251, 239, 205), font=font(29))
    draw.text((bx0 + 92, 395), "Small-batch coffee, baked daily.", fill=(195, 207, 202), font=font(15))
    rr(draw, (bx0 + 92, 421, bx0 + 226, 453), 999, fill=(219, 155, 78))
    draw.text((bx0 + 119, 429), "View the menu", fill=(21, 38, 43), font=font(12))

    draw.text((bx0 + 55, 548), "Made slowly. Served warmly.", fill=(24, 55, 64), font=font(28))
    for i, label in enumerate(("Morning roast", "Fresh pastry", "Harbor views")):
        cx = bx0 + 55 + i * 226
        rr(draw, (cx, 604, cx + 194, 734), 14, fill=(255, 252, 245), outline=(219, 211, 195))
        draw.ellipse((cx + 18, 624, cx + 54, 660), fill=(219, 155, 78))
        draw.text((cx + 18, 680), label, fill=(24, 55, 64), font=font(15))
        draw.text((cx + 18, 709), "Local · seasonal", fill=(112, 108, 98), font=font(11))

    # Agent pane.
    ax0 = 1000
    draw.rectangle((ax0, 48, pw, ph), fill=(12, 16, 24))
    draw.text((ax0 + 22, 72), "AGENT", fill=MUTED, font=font(11))
    rr(draw, (ax0 + 18, 105, pw - 18, 177), 12, fill=(26, 35, 51))
    draw.text((ax0 + 34, 121), "You", fill=ACCENT, font=font(12))
    draw.text((ax0 + 34, 145), "Build a warm cafe landing page", fill=TEXT, font=font(14))
    draw.text((ax0 + 34, 164), "with a header image and logo.", fill=TEXT, font=font(14))
    steps = (
        ("✓", "Created index.html"),
        ("✓", "Created styles.css"),
        ("✓", "Generated header.png"),
        ("✓", "Generated logo.png"),
    )
    y = 211
    for mark, label in steps:
        draw.ellipse((ax0 + 24, y, ax0 + 44, y + 20), fill=(24, 67, 53))
        draw.text((ax0 + 29, y + 1), mark, fill=OK, font=font(11))
        draw.text((ax0 + 56, y + 1), label, fill=TEXT, font=font(13, mono=True))
        y += 42
    rr(draw, (ax0 + 18, 400, pw - 18, 520), 12, fill=ELEV, outline=LINE)
    draw.text((ax0 + 34, 420), "The page is ready.", fill=TEXT, font=font(15))
    draw.text((ax0 + 34, 450), "Responsive layout, generated assets,", fill=MUTED, font=font(12))
    draw.text((ax0 + 34, 470), "and a live local preview.", fill=MUTED, font=font(12))
    rr(draw, (ax0 + 18, ph - 76, pw - 18, ph - 22), 12, fill=(8, 11, 17), outline=LINE)
    draw.text((ax0 + 34, ph - 57), "Ask for a change…", fill=MUTED, font=font(13))
    return img


def make_chat() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    draw = header(img, "chat")
    rr(draw, (18, 80, 118, 112), 10, fill=ELEV2, outline=LINE)
    draw.text((36, 88), "New chat", fill=TEXT, font=font(13))
    rr(draw, (128, 80, 268, 112), 10, fill=(58, 24, 30), outline=(120, 50, 60))
    draw.text((142, 88), "Clear history", fill=BAD, font=font(13))
    draw.text((286, 88), "1/3 · What model is loaded, and is the GPU…", fill=MUTED, font=font(13))
    draw.text((W - 280, 88), "Tab previous chats · ↑↓ recall", fill=MUTED, font=font(12))
    rr(draw, (18, 124, W - 18, H - 90), 14, fill=ELEV, outline=LINE)

    bubbles = [
        ("user", "What model is loaded, and is the GPU free for images?"),
        (
            "assistant",
            "You're on qwen (Qwen3.5-9B). GPU mode is llm — Comfy is idle.\n"
            "Send switch to comfy when you want Flux / Qwen-Image, or generate an image of …\n"
            "from your editor and the API will hand the card over.",
        ),
        ("user", "Show GPU memory roughly."),
        (
            "assistant",
            "About 8.1 / 12.3 GiB in use at 61°C, util ~60%. Status → Host graphs has the last 24h.",
        ),
    ]
    y = 144
    for role, text in bubbles:
        if role == "user":
            box_w = 520
            x0 = W - 40 - box_w
            rr(draw, (x0, y, x0 + box_w, y + 70), 12, fill=(40, 55, 90))
            draw.multiline_text((x0 + 14, y + 14), text, fill=TEXT, font=font(14), spacing=4)
            y += 90
        else:
            box_w = 640
            x0 = 40
            lines = text.count("\n") + 1
            bh = 28 + lines * 22
            rr(draw, (x0, y, x0 + box_w, y + bh), 12, fill=ELEV2, outline=LINE)
            draw.multiline_text((x0 + 14, y + 12), text, fill=TEXT, font=font(14), spacing=4)
            y += bh + 20

    rr(draw, (18, H - 74, W - 120, H - 18), 12, fill=BG, outline=LINE)
    draw.text((34, H - 54), "Message the console…", fill=MUTED, font=font(14))
    rr(draw, (W - 108, H - 74, W - 18, H - 18), 12, fill=ACCENT)
    draw.text((W - 88, H - 54), "Send", fill=BG, font=font(14))
    return img


def make_code() -> Image.Image:
    """Show the current three-pane Code workspace."""
    img = Image.new("RGB", (W, H), BG)
    draw = header(img, "chat")

    top, bottom = 80, H - 18
    left = (18, top, 250, bottom)
    center = (262, top, 914, bottom)
    right = (926, top, W - 18, bottom)
    for box in (left, center, right):
        rr(draw, box, 14, fill=ELEV, outline=LINE)

    # Chat history.
    rr(draw, (32, 94, 236, 128), 9, fill=ACCENT)
    draw.text((81, 103), "New code chat", fill=BG, font=font(13))
    rr(draw, (32, 140, 236, 174), 9, fill=BG, outline=LINE)
    draw.text((44, 149), "Search code chats", fill=MUTED, font=font(12))
    chats = [
        ("Harbor Cafe landing page", "now"),
        ("Product card component", "1h"),
        ("Portfolio refresh", "Tue"),
    ]
    y = 190
    for i, (title, when) in enumerate(chats):
        if i == 0:
            rr(draw, (30, y - 6, 238, y + 38), 9, fill=ELEV2, outline=LINE)
        draw.text((42, y), title[:26], fill=TEXT if i == 0 else MUTED, font=font(12))
        draw.text((202, y + 20), when, fill=MUTED, font=font(10, mono=True))
        y += 54

    # Code toolbar and mode toggle.
    draw.text((280, 99), "Harbor Cafe landing page", fill=TEXT, font=font(14))
    rr(draw, (570, 92, 700, 124), 999, fill=BG, outline=LINE)
    draw.text((587, 101), "Chat", fill=MUTED, font=font(12))
    rr(draw, (632, 94, 697, 122), 999, fill=(40, 55, 90))
    draw.text((648, 101), "Code", fill=TEXT, font=font(12))
    draw.text((848, 101), "More", fill=MUTED, font=font(12))
    draw.line((262, 138, 914, 138), fill=LINE)

    # Open file tabs.
    rr(draw, (278, 150, 394, 180), 8, fill=ELEV2, outline=LINE)
    draw.text((292, 158), "index.html", fill=TEXT, font=font(12, mono=True))
    rr(draw, (402, 150, 504, 180), 8, fill=BG, outline=LINE)
    draw.text((416, 158), "styles.css", fill=MUTED, font=font(12, mono=True))

    # Conversation.
    rr(draw, (500, 198, 892, 254), 12, fill=(40, 55, 90))
    draw.text((516, 212), "Build a responsive cafe landing page with a", fill=TEXT, font=font(13))
    draw.text((516, 232), "header photo and a logo that says Harbor Cafe.", fill=TEXT, font=font(13))
    rr(draw, (280, 274, 760, 382), 12, fill=ELEV2, outline=LINE)
    draw.text((296, 288), "Writing index.html", fill=ACCENT, font=font(12, mono=True))
    draw.text((296, 312), "Writing styles.css", fill=ACCENT, font=font(12, mono=True))
    draw.text((296, 340), "Created the page and generated both image assets.", fill=TEXT, font=font(13))
    draw.text((296, 362), "Use Open site to preview it or Zip to download.", fill=MUTED, font=font(12))

    # Composer.
    rr(draw, (278, bottom - 112, 898, bottom - 18), 12, fill=BG, outline=LINE)
    draw.text((294, bottom - 94), "Ask for another change or attach project files…", fill=MUTED, font=font(13))
    draw.text((294, bottom - 48), "📎", fill=MUTED, font=font(15))
    draw.text((665, bottom - 45), "Enter send · Shift+Enter line", fill=MUTED, font=font(10))
    rr(draw, (830, bottom - 60, 884, bottom - 28), 9, fill=ACCENT)
    draw.text((842, bottom - 51), "Send", fill=BG, font=font(11))

    # Files pane.
    draw.text((942, 98), "Files", fill=TEXT, font=font(14))
    draw.text((982, 100), "4", fill=MUTED, font=font(11))
    controls = [("New", 1036), ("Upload", 1082), ("Open site", 1144)]
    for label, x in controls:
        width = 42 if label == "New" else 56 if label == "Upload" else 78
        rr(draw, (x, 91, x + width, 122), 8, fill=ELEV2, outline=LINE)
        draw.text((x + 8, 100), label, fill=TEXT, font=font(11))
    draw.line((926, 138, W - 18, 138), fill=LINE)
    files = [
        ("▾  images", MUTED),
        ("    header.png", TEXT),
        ("    logo.png", TEXT),
        ("index.html", ACCENT),
        ("styles.css", TEXT),
    ]
    y = 158
    for name, color in files:
        if name == "index.html":
            rr(draw, (938, y - 5, W - 30, y + 24), 7, fill=(34, 42, 62))
        draw.text((950, y), name, fill=color, font=font(12, mono=True))
        y += 34
    draw.text((946, bottom - 42), "Open site   Zip   Clear", fill=MUTED, font=font(11))
    return img


def save_jpg(img: Image.Image, name: str) -> Path:
    path = OUT / name
    img = img.convert("RGB")
    img.save(path, "JPEG", quality=88, optimize=True)
    print(f"wrote {path} ({path.stat().st_size // 1024} KiB)")
    return path


def main() -> None:
    save_jpg(make_ide_preview(), "ide-preview.jpg")
    save_jpg(make_status(), "ui-status.jpg")
    save_jpg(make_logs(), "ui-logs.jpg")
    save_jpg(make_gallery(), "ui-gallery.jpg")
    save_jpg(make_chat(), "ui-chat.jpg")
    save_jpg(make_code(), "ui-code.jpg")


if __name__ == "__main__":
    main()
