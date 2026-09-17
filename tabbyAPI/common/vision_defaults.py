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
# 2.00bpw 27B still leaves KV room on 12 GB; 3.00bpw does not.
_LIGHT_BPW_RE = re.compile(r"(?<![0-9])([12](?:\.\d+)?)bpw", re.IGNORECASE)
TIGHT_KV_TOKENS = 16384
DEFAULT_AUTOSPLIT_RESERVE_MIB = 384
TIGHT_AUTOSPLIT_RESERVE_MIB = 768
TIGHT_WEIGHT_FRACTION = 0.65
TIGHT_PARAMS_B = 20.0


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


_VISION_OFF_NOTE_RE = re.compile(r"\s*\(vision off on [^)]+\)\s*$", re.I)


def strip_vision_note(pretty: str) -> str:
    return _VISION_OFF_NOTE_RE.sub("", pretty or "").strip()


def pretty_with_vision_note(pretty: str, vision: bool, vram_label: str) -> str:
    text = strip_vision_note(pretty) or "model"
    if vision:
        return text
    return f"{text} (vision off on {vram_label})"


def looks_light_quant(name: str) -> bool:
    """True for 2.x bpw names that still leave KV room on a 12 GB card."""
    match = _LIGHT_BPW_RE.search(name or "")
    if not match:
        return False
    try:
        return float(match.group(1)) < 2.5
    except (TypeError, ValueError):
        return False


def needs_tight_kv(
    *,
    vram_mib: int = 0,
    params_b: Optional[float] = None,
    weight_mib: int = 0,
    folder_name: str = "",
) -> bool:
    """True when a 12 GB card cannot keep a 32k Q4 cache after the weights."""
    vram = int(vram_mib or 0)
    if vram <= 0 or vram >= VISION_PROBE_VRAM_MIB:
        return False
    weights = int(weight_mib or 0)
    if weights > 0:
        return weights > TIGHT_WEIGHT_FRACTION * vram
    if looks_light_quant(folder_name):
        return False
    return params_b is not None and params_b >= TIGHT_PARAMS_B


def decide_kv_cache(
    *,
    max_seq: int,
    vram_mib: int = 0,
    params_b: Optional[float] = None,
    weight_mib: int = 0,
    folder_name: str = "",
) -> dict[str, Any]:
    """Cache size and autosplit reserve for an auto profile or a clamp."""
    seq = max(256, int(max_seq or 0) or TIGHT_KV_TOKENS)
    reserve = DEFAULT_AUTOSPLIT_RESERVE_MIB
    if needs_tight_kv(
        vram_mib=vram_mib,
        params_b=params_b,
        weight_mib=weight_mib,
        folder_name=folder_name,
    ):
        reserve = TIGHT_AUTOSPLIT_RESERVE_MIB
    return {
        "max_seq_len": seq,
        "cache_size": seq,
        "autosplit_reserve": [reserve],
    }


def clamp_model_kv(
    model: dict[str, Any],
    *,
    vram_mib: int = 0,
    params_b: Optional[float] = None,
    weight_mib: int = 0,
    folder_name: str = "",
) -> bool:
    """Do not shrink cache_size or max_seq_len.

    Bumps autosplit_reserve when the current reserve is below the tight
    budget on a packed 12 GB load.
    """
    if not isinstance(model, dict):
        return False
    if not needs_tight_kv(
        vram_mib=vram_mib,
        params_b=params_b,
        weight_mib=weight_mib,
        folder_name=folder_name or str(model.get("model_name") or ""),
    ):
        return False
    try:
        current_seq = int(model.get("cache_size") or model.get("max_seq_len") or 0)
    except (TypeError, ValueError):
        current_seq = 0
    decided = decide_kv_cache(
        max_seq=current_seq or TIGHT_KV_TOKENS,
        vram_mib=vram_mib,
        params_b=params_b,
        weight_mib=weight_mib,
        folder_name=folder_name or str(model.get("model_name") or ""),
    )
    changed = False
    reserve = model.get("autosplit_reserve")
    wanted_reserve = decided["autosplit_reserve"]
    current_reserve = 0.0
    if isinstance(reserve, list) and reserve:
        try:
            current_reserve = float(reserve[0])
        except (TypeError, ValueError):
            current_reserve = 0.0
    if current_reserve < TIGHT_AUTOSPLIT_RESERVE_MIB:
        model["autosplit_reserve"] = wanted_reserve
        changed = True
    return changed
