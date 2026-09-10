"""VRAM-aware vision defaults for Hugging Face auto-profiles."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

# Same 16 GB bar as calibrate.py's GLM vision probe.
VISION_PROBE_VRAM_MIB = 16000
VISION_OFFLOAD_VRAM_MIB = 24000
# 14B+ text towers do not get a vision tower on 12 GB cards.
LARGE_TEXT_B = 14
# Without a "27B" name, this many MiB of weights counts as large on an unknown GPU.
UNKNOWN_VRAM_LARGE_WEIGHT_MIB = 8192

# Match "-27B-" / "_9B_" / "12B" at a boundary, not the "8" in Qwen3.8.
_PARAM_B_RE = re.compile(r"(?<![0-9.])(\d{1,3})B(?:-|_|\b)", re.IGNORECASE)


def parse_param_billions(name: str) -> Optional[float]:
    """Largest N from an `NB` token in a folder or repo name, or None."""
    found = [float(match.group(1)) for match in _PARAM_B_RE.finditer(name or "")]
    return max(found) if found else None


def weight_mib(folder: Path) -> int:
    """Sum of safetensor file sizes in the model folder, in MiB."""
    if not folder.is_dir():
        return 0
    total = 0
    for path in folder.rglob("*"):
        if path.suffix.lower() in {".safetensors", ".safetensor"} and path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return int(total / (1024 * 1024))


def gpu_size_label(vram_mib: int, gpu: Optional[dict[str, Any]] = None) -> str:
    label = (gpu or {}).get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    if vram_mib > 0:
        return f"{max(1, int(round(vram_mib / 1024.0)))} GB"
    return "this GPU"


def is_large_text_tower(
    *,
    vram_mib: int = 0,
    params_b: Optional[float] = None,
    weight_mib: int = 0,
) -> bool:
    if params_b is not None and params_b >= LARGE_TEXT_B:
        return True
    if vram_mib > 0 and weight_mib > 0 and weight_mib > 0.5 * vram_mib:
        return True
    if vram_mib <= 0 and weight_mib >= UNKNOWN_VRAM_LARGE_WEIGHT_MIB:
        return True
    return False


def decide_vision(
    *,
    capable: bool,
    vram_mib: int = 0,
    params_b: Optional[float] = None,
    weight_mib: int = 0,
) -> dict[str, bool]:
    """Choose vision / vision_offload for an auto-created HF profile."""
    if not capable:
        return {"vision": False, "vision_offload": False}
    tight = vram_mib < VISION_PROBE_VRAM_MIB
    if tight and is_large_text_tower(
        vram_mib=vram_mib, params_b=params_b, weight_mib=weight_mib
    ):
        return {"vision": False, "vision_offload": False}
    if tight and params_b is None and weight_mib <= 0:
        return {"vision": False, "vision_offload": False}
    offload = vram_mib < VISION_OFFLOAD_VRAM_MIB
    return {"vision": True, "vision_offload": offload}


def pretty_with_vision_note(pretty: str, vision: bool, vram_label: str) -> str:
    text = (pretty or "").strip() or "model"
    if vision or "vision off" in text.lower():
        return text
    return f"{text} (vision off on {vram_label})"
