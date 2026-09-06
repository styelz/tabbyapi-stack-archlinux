#!/usr/bin/env python3
"""Draw Cursor-style chat frames from the replies a user actually sees. No GPU.

Phrase slides use help_text / list_text / switch_reply_text / image_ready copy.
Mixed slides show the server-owned wait and download — not generate_image.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
TABBY = HERE.parent / "tabbyAPI"
sys.path.insert(0, str(TABBY))

from common.image_paths import (  # noqa: E402
    image_download_command,
    image_download_note,
    image_poll_wait_command,
    image_running_note,
)
from common.phrase_switch import (  # noqa: E402
    _image_url_block,
    help_text,
    image_job_wait_text,
    list_text,
    switch_reply_text,
)

OUT_GIF = HERE / "ide-chat.gif"
FRAMES_DIR = HERE / "ide-chat-frames"
FONT_MONO = Path("/usr/share/fonts/Adwaita/AdwaitaMono-Regular.ttf")
FONT_SANS = Path("/usr/share/fonts/Adwaita/AdwaitaSans-Regular.ttf")
FONT_SANS_B = Path("/usr/share/fonts/Adwaita/AdwaitaSans-Regular.ttf")

API = "http://<gpu-host>:5000/v1"
QWEN_FOLDER = "Qwen3.5-9B-exl3-4.00bpw"
DURATION_MS = 4200

W, H = 1080, 720
MARGIN = 22
TITLE_H = 44
INPUT_H = 56
LINE = 19
PAD = 8
AV = 20

BG = (8, 11, 17)
BAR = (11, 16, 24)
COMPOSER = (12, 17, 26)
YOU_BG = (24, 38, 61)
CARD = (17, 24, 36)
PREVIEW = (12, 18, 28)
TEXT = (238, 243, 250)
DIM = (137, 151, 171)
TOOL = (170, 196, 255)
SEP = (42, 53, 70)
DOT = (105, 166, 255)
YOU_AV = (105, 166, 255)
AGENT_AV = (164, 112, 255)
ACCENT = (105, 166, 255)

BODY_TOP = TITLE_H + 12
BODY_FLOOR = H - INPUT_H - 26
TEXT_X = MARGIN + AV + 12
TEXT_W = W - TEXT_X - MARGIN


def font(size: int, *, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    if mono:
        return ImageFont.truetype(str(FONT_MONO), size)
    path = FONT_SANS_B if bold and FONT_SANS_B.exists() else FONT_SANS
    if path.exists():
        return ImageFont.truetype(str(path), size)
    return ImageFont.truetype(str(FONT_MONO), size)


def measure() -> ImageDraw.ImageDraw:
    return ImageDraw.Draw(Image.new("RGB", (W, H), BG))


def wrap_line(draw: ImageDraw.ImageDraw, text: str, face, max_w: int) -> list[str]:
    text = text.replace("\t", "    ")
    if text == "":
        return [""]
    out: list[str] = []
    while text:
        cut = len(text)
        while cut > 1 and draw.textlength(text[:cut], font=face) > max_w:
            space = text.rfind(" ", 0, cut)
            cut = space if space > 0 else cut - 1
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return out or [""]


def wrap_block(draw: ImageDraw.ImageDraw, text: str, face, max_w: int) -> list[str]:
    lines: list[str] = []
    for raw in text.split("\n"):
        lines.extend(wrap_line(draw, raw, face, max_w))
    return lines


def new_window(slide: int, total: int, caption: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, W, TITLE_H), fill=BAR)
    d.line((0, TITLE_H, W, TITLE_H), fill=SEP, width=1)
    d.rounded_rectangle((17, 10, 39, 34), radius=7, fill=(20, 29, 44), outline=SEP)
    d.polygon(((23, 15), (34, 15), (30, 21), (35, 21), (26, 30), (28, 23), (22, 23)), fill=ACCENT)
    d.text((48, 12), "TabbyAPI Stack", font=font(15, bold=True), fill=TEXT)
    chip = "gpt-4o"
    chip_w = int(d.textlength(chip, font=font(12))) + 18
    d.rounded_rectangle((W - MARGIN - chip_w, 10, W - MARGIN, 34), radius=9, fill=(42, 42, 46), outline=SEP)
    d.text((W - MARGIN - chip_w + 9, 13), chip, font=font(12), fill=DIM)

    d.rectangle((0, H - INPUT_H, W, H), fill=COMPOSER)
    d.line((0, H - INPUT_H, W, H - INPUT_H), fill=SEP, width=1)
    d.rounded_rectangle((MARGIN, H - 44, W - MARGIN, H - 12), radius=12, fill=(7, 10, 16), outline=SEP, width=1)
    d.text((MARGIN + 14, H - 36), "Ask TabbyAPI Stack…", font=font(13), fill=DIM)

    cy = H - INPUT_H - 12
    gap = 12
    start = (W - (total - 1) * gap) // 2
    for i in range(total):
        r = 3
        x = start + i * gap
        d.ellipse((x - r, cy - r, x + r, cy + r), fill=DOT if i == slide else SEP)
    d.text((MARGIN, H - INPUT_H - 18), f"{slide + 1}/{total}  {caption}", font=font(11), fill=DIM)
    return im, d


def draw_role(d: ImageDraw.ImageDraw, y: int, who: str, color) -> int:
    d.ellipse((MARGIN, y + 1, MARGIN + AV, y + 1 + AV), fill=color)
    d.text((TEXT_X, y + 2), who, font=font(14, bold=True), fill=TEXT)
    return y + 26


def draw_separator(d: ImageDraw.ImageDraw, y: int) -> int:
    d.line((MARGIN, y + 6, W - MARGIN, y + 6), fill=SEP, width=1)
    return y + 16


def draw_user_bubble(d: ImageDraw.ImageDraw, y: int, lines: list[str]) -> int:
    face = font(14)
    h = len(lines) * LINE + PAD * 2
    d.rounded_rectangle((TEXT_X - 4, y, W - MARGIN, y + h), radius=8, fill=YOU_BG)
    ty = y + PAD
    for line in lines:
        d.text((TEXT_X + 4, ty), line, font=face, fill=TEXT)
        ty += LINE
    return y + h + 8


def draw_text_block(d: ImageDraw.ImageDraw, y: int, lines: list[str], *, mono: bool = False) -> int:
    face = font(13, mono=mono)
    for line in lines:
        d.text((TEXT_X, y), line, font=face, fill=TEXT)
        y += LINE
    return y + 4


def draw_tool_card(d: ImageDraw.ImageDraw, y: int, name: str, detail: str) -> int:
    draw = d
    face = font(12, mono=True)
    detail_lines = wrap_block(draw, detail, face, TEXT_W - 20)
    h = 28 + len(detail_lines) * 17 + 10
    d.rounded_rectangle((TEXT_X - 4, y, W - MARGIN, y + h), radius=8, fill=CARD, outline=SEP)
    d.rectangle((TEXT_X - 4, y, TEXT_X, y + h), fill=ACCENT)
    d.text((TEXT_X + 12, y + 6), name, font=font(13, bold=True), fill=TEXT)
    ty = y + 28
    for line in detail_lines:
        d.text((TEXT_X + 12, ty), line, font=face, fill=TOOL)
        ty += 17
    return y + h + 10


def draw_bike_preview(im: Image.Image, d: ImageDraw.ImageDraw, y: int) -> int:
    """Stand-in for an editor-rendered generated image — docs art, not Comfy."""
    box = (TEXT_X, y, TEXT_X + 420, y + 164)
    d.rounded_rectangle(box, radius=8, fill=PREVIEW, outline=SEP)
    x0, y0, x1, y1 = box
    # Rainy neon street scene with a bicycle silhouette.
    for row in range(y1 - y0):
        t = row / max(1, y1 - y0 - 1)
        color = (int(18 + 22 * t), int(24 + 10 * t), int(48 + 28 * t))
        d.line((x0 + 1, y0 + row, x1 - 1, y0 + row), fill=color)
    d.rectangle((x0 + 42, y0 + 35, x0 + 170, y1 - 30), fill=(14, 18, 28))
    d.rectangle((x0 + 58, y0 + 52, x0 + 154, y0 + 92), fill=(221, 54, 135))
    d.text((x0 + 76, y0 + 61), "OPEN", font=font(18, bold=True), fill=(255, 215, 235))
    d.rectangle((x0 + 270, y0 + 18, x0 + 375, y1 - 34), fill=(12, 19, 30))
    for wy in (38, 76, 114):
        d.rectangle((x0 + 286, y0 + wy, x0 + 310, y0 + wy + 18), fill=(62, 202, 232))
        d.rectangle((x0 + 330, y0 + wy, x0 + 354, y0 + wy + 18), fill=(250, 185, 79))
    d.rectangle((x0 + 1, y1 - 34, x1 - 1, y1 - 1), fill=(15, 19, 28))
    d.line((x0 + 35, y1 - 10, x1 - 28, y1 - 22), fill=(87, 58, 109), width=3)
    wheel = (225, 62, 118)
    d.ellipse((x0 + 156, y1 - 68, x0 + 204, y1 - 20), outline=wheel, width=4)
    d.ellipse((x0 + 235, y1 - 68, x0 + 283, y1 - 20), outline=wheel, width=4)
    d.line((x0 + 180, y1 - 44, x0 + 220, y1 - 77), fill=wheel, width=4)
    d.line((x0 + 220, y1 - 77, x0 + 259, y1 - 44), fill=wheel, width=4)
    d.line((x0 + 180, y1 - 44, x0 + 259, y1 - 44), fill=wheel, width=4)
    d.text((x0 + 12, y0 + 8), "generated-20260821-105900.png", font=font(10), fill=DIM)
    return y + 172


def paint_page(im: Image.Image, d: ImageDraw.ImageDraw, page: dict) -> None:
    y = BODY_TOP
    user_lines = page.get("user")
    if user_lines is not None:
        y = draw_role(d, y, "You", YOU_AV)
        y = draw_user_bubble(d, y, user_lines)
        y = draw_separator(d, y)
        y = draw_role(d, y, "gpt-4o", AGENT_AV)
    else:
        y = draw_role(d, y, "gpt-4o", AGENT_AV)
        d.text((TEXT_X + 56, y - 24), "· continued", font=font(12), fill=DIM)

    for block in page["blocks"]:
        kind = block[0]
        if kind == "text":
            y = draw_text_block(d, y, block[1], mono=block[2] if len(block) > 2 else False)
        elif kind == "tool":
            y = draw_tool_card(d, y, block[1], block[2])
        elif kind == "image":
            y = draw_bike_preview(im, d, y)


def header_h(user_lines: list[str] | None) -> int:
    if user_lines is None:
        return 26
    return 26 + (len(user_lines) * LINE + PAD * 2 + 8) + 16 + 26


def fit_lines(lines: list[str], used: int) -> int:
    usable = BODY_FLOOR - BODY_TOP - used
    return max(3, usable // LINE)


def paginate_text(user: str, caption: str, body: str, *, mono: bool = True) -> list[dict]:
    draw = measure()
    uface = font(14)
    bface = font(13, mono=mono)
    user_lines = wrap_block(draw, user, uface, TEXT_W - 16)
    body_lines = wrap_block(draw, body, bface, TEXT_W)
    first = fit_lines(body_lines, header_h(user_lines))
    cont = fit_lines(body_lines, header_h(None))
    chunks: list[list[str]] = []
    if body_lines:
        chunks.append(body_lines[:first])
        rest = body_lines[first:]
        while rest:
            chunks.append(rest[:cont])
            rest = rest[cont:]
    else:
        chunks.append([])
    pages = []
    for i, chunk in enumerate(chunks):
        part = f" ({i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
        pages.append(
            {
                "caption": caption + part,
                "user": user_lines if i == 0 else None,
                "blocks": [("text", chunk, mono)],
            }
        )
    return pages


def session_pages() -> list[dict]:
    with patch("common.phrase_switch.current_folder", return_value=QWEN_FOLDER):
        help_body = help_text(api_base=API)
        listed = list_text()
    switch_qwen = switch_reply_text("qwen")

    flux_name = "generated-20260821-105900.png"
    with patch("common.gpu_mode.time.time", return_value=1_755_737_940):
        url_block = _image_url_block([flux_name], api_base=API)
    # Cursor renders ![](url) as the picture. Drop that markdown line from the text.
    visible_ready = "\n".join(
        line for line in url_block.split("\n") if not line.startswith("![](")
    )
    this = image_job_wait_text("generate an image of a red bicycle", restore=True)
    another = image_job_wait_text("", restore=True)
    image_caption = (
        f"{visible_ready}\n\nThis picture: {this}\n"
        "Send another short description for a different picture, or switch to qwen.\n"
        f"Another picture: {another}"
    )

    header_url = f"{API}/images/generated-20260821-110001.png?t=1755737940"
    logo_url = f"{API}/images/generated-20260821-110002.png?t=1755737940"
    pairs = [(header_url, "harbor/images/header.png"), (logo_url, "harbor/images/logo.png")]
    download_note = image_download_note(pairs)
    download_cmd = image_download_command(pairs)
    job = SimpleNamespace(
        id="7f3a2c1e",
        status="running",
        urls=[],
        items=[
            SimpleNamespace(output_path="harbor/images/header.png", count=1, urls=[]),
            SimpleNamespace(output_path="harbor/images/logo.png", count=1, urls=[]),
        ],
    )
    wait_cmd = image_poll_wait_command(job)
    running_note = image_running_note(job)
    batch_wait = image_job_wait_text(
        prompts=[
            "a neon diner street at night",
            "qwen-image: a logo that says Harbor Cafe",
        ],
        restore=True,
    )

    mixed_user = (
        "Create a cafe landing page under harbor/. Write the HTML and CSS.\n"
        "Generate harbor/images/header.png of a neon diner street at night,\n"
        "and qwen-image: harbor/images/logo.png that says Harbor Cafe.\n"
        "Point the page at those files."
    )
    draw = measure()
    mixed_user_lines = wrap_block(draw, mixed_user, font(14), TEXT_W - 16)

    pages: list[dict] = []
    pages.extend(paginate_text("help", "help", help_body, mono=True))
    pages.extend(paginate_text("list models", "list models", listed, mono=True))
    pages.extend(paginate_text("switch to qwen", "switch to qwen", switch_qwen, mono=False))

    ready_pages = paginate_text(
        "generate an image of a red bicycle",
        "generate an image",
        image_caption,
        mono=False,
    )
    # First image-ready page: insert the rendered preview after the filename line.
    first = ready_pages[0]
    text_lines = first["blocks"][0][1]
    split_at = 0
    for i, line in enumerate(text_lines):
        if line.startswith("1. generated-") or "generated-20260821-105900.png" in line:
            split_at = i + 1
            break
    first["blocks"] = [
        ("text", text_lines[:split_at], False),
        ("image",),
        ("text", text_lines[split_at:], False),
    ]
    pages.extend(ready_pages)

    pages.append(
        {
            "caption": "webpage + header + logo",
            "user": mixed_user_lines,
            "blocks": [
                (
                    "text",
                    wrap_block(
                        draw,
                        "The API queued the PNG job. " + batch_wait,
                        font(13),
                        TEXT_W,
                    ),
                    False,
                ),
                ("tool", "Shell", wait_cmd),
            ],
        }
    )
    pages.append(
        {
            "caption": "job running",
            "user": mixed_user_lines,
            "blocks": [
                ("text", wrap_block(draw, running_note, font(13), TEXT_W), False),
                ("tool", "Shell", wait_cmd),
            ],
        }
    )
    pages.append(
        {
            "caption": "images ready",
            "user": mixed_user_lines,
            "blocks": [
                ("text", wrap_block(draw, download_note, font(13), TEXT_W), False),
                ("tool", "Shell", download_cmd),
                (
                    "tool",
                    "Write",
                    "harbor/index.html  ·  img src harbor/images/header.png, harbor/images/logo.png",
                ),
            ],
        }
    )
    return pages


def main() -> None:
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    for old in FRAMES_DIR.glob("*.png"):
        old.unlink()

    pages = session_pages()
    images = []
    total = len(pages)
    for i, page in enumerate(pages):
        im, d = new_window(i, total, page["caption"])
        paint_page(im, d, page)
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in page["caption"])
        slug = "-".join(part for part in slug.split("-") if part)[:60]
        png = FRAMES_DIR / f"{i + 1:02d}-{slug}.png"
        im.save(png)
        images.append(im)
        print(f"wrote {png}")

    first, rest = images[0], images[1:]
    first.save(
        OUT_GIF,
        save_all=True,
        append_images=rest,
        duration=DURATION_MS,
        loop=0,
        optimize=True,
    )
    print(f"wrote {OUT_GIF} ({OUT_GIF.stat().st_size} bytes, {total} frames × {DURATION_MS} ms)")


if __name__ == "__main__":
    main()
