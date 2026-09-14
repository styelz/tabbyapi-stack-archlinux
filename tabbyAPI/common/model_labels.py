"""Display names for the model dropdown and library."""

from __future__ import annotations

import json
import re
from pathlib import Path

NAMES_LOCAL = "names.local.json"
_VISION_OFF_RE = re.compile(r"\s*\(vision off on [^)]+\)\s*$", re.I)
_REV_TOKEN_RE = re.compile(
    r"(?ix)^(?:main|master|[\d.]+bpw|sc[_-]?[\d.]+bpw[\w-]*|"
    r"iq\d[\w-]*|q\d(?:[_-][\w]+)+|fp(?:16|8)|bf16)$"
)
_TRAILING_JUNK_RE = re.compile(
    r"(?ix)[-_](?:exl[23]|gguf|ggml|gptq|awq|hqq|mtp|xs|"
    r"\d+gb(?:-vram)?|vram|iq\d[\w-]*|q\d(?:[_-][a-z0-9]+)+|"
    r"\d+(?:\.\d+)?bpw(?:[-_][\w]+)*|"
    r"sc[-_]?\d+(?:\.\d+)?bpw(?:[-_][\w]+)*)$"
)


def pretty_model_label(*parts: str) -> str:
    """Human list/dropdown name: drop HF org, quant/revision, vision-off notes."""
    raw = " ".join(str(part or "").strip() for part in parts if str(part or "").strip())
    text = " ".join(raw.split())
    if not text:
        return ""
    text = _VISION_OFF_RE.sub("", text).strip()
    slash = text.find("/")
    if 0 < slash <= 32 and " " not in text[:slash]:
        rest = text[slash + 1 :]
        nxt = rest.split("/", 1)[0]
        if nxt and " " not in nxt.split()[0]:
            text = rest.strip()
    bits = text.split()
    while len(bits) > 1 and _REV_TOKEN_RE.match(bits[-1]):
        bits.pop()
    text = " ".join(bits)
    head, sep, tail = text.partition(" - ")
    prev = None
    while prev != head:
        prev = head
        head = _TRAILING_JUNK_RE.sub("", head).strip(" .-_")
    if sep and tail and "/" not in raw:
        return f"{head} - {tail}".strip() if head else text
    return head or text or raw


def load_name_overrides(profiles_dir: Path | None) -> dict[str, dict]:
    if profiles_dir is None:
        return {}
    path = Path(profiles_dir) / NAMES_LOCAL
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for key, value in data.items():
        stem = str(key or "").strip().lower()
        if not stem:
            continue
        if isinstance(value, str) and value.strip():
            out[stem] = {"pretty": value.strip()[:80]}
        elif isinstance(value, dict):
            pretty = str(value.get("pretty") or "").strip()
            if pretty:
                out[stem] = {"pretty": pretty[:80]}
    return out
