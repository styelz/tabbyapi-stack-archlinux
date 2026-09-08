"""Warm-switch wait copy from model_profiles/switch_times.json.

Two files: the tracked bench baseline (switch_times.json) and an untracked
overlay (switch_times.local.json) that real loads EMA into. The updater
resets tracked files to origin on every pull, so live learning lives in the
overlay and is merged on read. A fresh bench deletes the overlay.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
TIMES_PATH = ROOT / "model_profiles" / "switch_times.json"
LOCAL_TIMES_PATH = ROOT / "model_profiles" / "switch_times.local.json"

# Live loads blend into the stored typical: new = ALPHA * measured + (1 - ALPHA) * old.
# One slow load moves the HUD a third of the way; three in a row settle it.
EMA_ALPHA = 1.0 / 3.0
# Under this many seconds nothing was really loaded (already-loaded path).
RECORD_MIN_S = 5.0
# A sample far above the stored typical (cold Triton compile, weights evicted
# from page cache) is clamped to this multiple before blending: one bad load
# nudges the HUD, and a slower reality still converges over a few loads.
RECORD_MAX_RATIO = 4.0

# Used when the file is missing. Bench overwrites the JSON with measured values;
# real loads then EMA into it. Measured 2026-09-09 on an RTX 4070 Ti 12 GB.
DEFAULT_READY_S = {
    "qwen": 75,
    "qwen35": 65,
    "qwen36": 110,
    "gemma": 84,
    "gemma26": 113,
    "glm": 40,
    "comfy": 4,
    "flux": 4,
    "llm": 52,
}


def detect_gpu() -> dict[str, Any]:
    """nvidia-smi name + total MiB. Empty strings if the tool is missing."""
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=10,
        )
        line = out.strip().splitlines()[0]
        name, _, mem = line.partition(",")
        name = name.strip()
        try:
            vram_mib = int(float(mem.strip()))
        except ValueError:
            vram_mib = 0
        gb = max(1, int(round(vram_mib / 1024.0))) if vram_mib else 0
        short = name.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")
        label = f"{short} {gb} GB" if gb else short or "unknown GPU"
        return {"name": name or "unknown", "vram_mib": vram_mib, "label": label}
    except (OSError, subprocess.CalledProcessError, IndexError):
        return {"name": "unknown", "vram_mib": 0, "label": "unknown GPU"}


def _read_json_dict(target: Path) -> dict[str, Any]:
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def merge_times(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Overlay per-alias fields onto the baseline (live ready_s over bench)."""
    out: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        prev = out.get(key)
        if isinstance(value, dict) and isinstance(prev, dict):
            out[key] = {**prev, **value}
        else:
            out[key] = value
    return out


def load_switch_times(
    path: Optional[Path] = None, local_path: Optional[Path] = None
) -> dict[str, Any]:
    """Bench baseline merged with the live overlay.

    Passing `path` alone (bench / calibrate --out) reads only that file.
    """
    target = path or TIMES_PATH
    base = _read_json_dict(target)
    if path is not None and local_path is None:
        return base
    overlay = _read_json_dict(local_path or LOCAL_TIMES_PATH)
    return merge_times(base, overlay) if overlay else base


def gpu_label(times: Optional[dict[str, Any]] = None) -> str:
    """Label from switch_times.json, else nvidia-smi (e.g. 'RTX 4070 Ti 12 GB')."""
    data = times if times is not None else load_switch_times()
    label = data.get("gpu") if isinstance(data, dict) else None
    if isinstance(label, str) and label.strip():
        return label.strip()
    return str(detect_gpu().get("label") or "this GPU")


def ready_seconds(name: str, times: Optional[dict[str, Any]] = None) -> int:
    """Typical seconds until the GPU is ready for this switch alias."""
    key = (name or "").strip().lower()
    if key in ("flux", "image", "comfyui"):
        key = "comfy"
    data = times if times is not None else load_switch_times()
    entry = data.get(key)
    if isinstance(entry, dict) and entry.get("ready_s") is not None:
        try:
            return max(1, int(round(float(entry["ready_s"]))))
        except (TypeError, ValueError):
            pass
    if isinstance(entry, (int, float)):
        return max(1, int(round(float(entry))))
    return DEFAULT_READY_S.get(key, DEFAULT_READY_S["qwen"])


def format_duration(seconds: float) -> str:
    """'15 seconds' or '2 minutes' — no leading 'about'."""
    secs = max(1, float(seconds))
    if secs < 90:
        rounded = int(5 * round(secs / 5.0)) if secs >= 5 else int(round(secs))
        rounded = max(1, rounded)
        unit = "second" if rounded == 1 else "seconds"
        return f"{rounded} {unit}"
    minutes = max(1, int(round(secs / 60.0)))
    unit = "minute" if minutes == 1 else "minutes"
    return f"{minutes} {unit}"


def wait_hint(name: str, times: Optional[dict[str, Any]] = None) -> str:
    """'Wait about 15 seconds' / 'Wait about 2 minutes'."""
    return f"Wait about {format_duration(ready_seconds(name, times))}"


def profile_error(name: str, times: Optional[dict[str, Any]] = None) -> Optional[str]:
    key = (name or "").strip().lower()
    data = times if times is not None else load_switch_times()
    entry = data.get(key)
    if isinstance(entry, dict):
        err = entry.get("error")
        if err:
            return str(err)
    return None


def _times_key(name: str) -> str:
    key = (name or "").strip().lower()
    if key in ("flux", "image", "comfyui"):
        key = "comfy"
    return key


def blend_ready(old: Optional[float], measured: float, alpha: float = EMA_ALPHA) -> float:
    """EMA of one new sample into the stored typical. No prior: the sample."""
    if old is None:
        return float(measured)
    return alpha * float(measured) + (1.0 - alpha) * float(old)


def should_record(measured: float, old: Optional[float] = None) -> bool:
    """False for no-op loads (already in VRAM) and junk values."""
    try:
        secs = float(measured)
    except (TypeError, ValueError):
        return False
    return secs >= RECORD_MIN_S


def clamp_sample(measured: float, old: Optional[float]) -> float:
    """Cap an outlier at RECORD_MAX_RATIO x the stored typical."""
    secs = float(measured)
    if old is not None and old > 0:
        return min(secs, RECORD_MAX_RATIO * float(old))
    return secs


def record_ready(
    name: str,
    seconds: float,
    field: str = "ready_s",
    path: Optional[Path] = None,
    local_path: Optional[Path] = None,
) -> Optional[float]:
    """Blend one real load into the live overlay. Returns the new typical.

    Called after a successful warm switch (LLM profile, `comfy`, `llm` restore)
    or a finished single render (`comfy` with field flux_s / qwen_image_s).
    The prior comes from baseline + overlay (`path`, `local_path`); only the
    overlay file is written, so a stack update cannot reset what was learned.
    Never raises: a read-only disk or a torn JSON must not fail a switch.
    Returns None when the sample was skipped or the write failed.
    """
    key = _times_key(name)
    if not key:
        return None
    base_path = path or TIMES_PATH
    overlay_path = local_path or LOCAL_TIMES_PATH
    merged = load_switch_times(base_path, overlay_path)
    entry = merged.get(key)
    if isinstance(entry, (int, float)):
        entry = {"ready_s": float(entry)}
    if not isinstance(entry, dict):
        entry = {}
    old: Optional[float]
    try:
        old = float(entry[field]) if entry.get(field) is not None else None
    except (TypeError, ValueError):
        old = None
    if old is None and field == "ready_s":
        default = DEFAULT_READY_S.get(key)
        old = float(default) if default is not None else None
    if not should_record(seconds):
        return None
    new = round(blend_ready(old, clamp_sample(seconds, old)), 1)
    overlay = _read_json_dict(overlay_path)
    local_entry = overlay.get(key)
    local_entry = dict(local_entry) if isinstance(local_entry, dict) else {}
    local_entry[field] = new
    if field == "ready_s":
        # A load just succeeded; a stale bench failure note is wrong now.
        local_entry["error"] = None
    overlay[key] = local_entry
    overlay["live_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = overlay_path.with_name(f".{overlay_path.name}.{os.getpid()}.tmp")
    try:
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(overlay, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, overlay_path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return new


def extra_seconds(name: str, field: str, times: Optional[dict[str, Any]] = None) -> Optional[int]:
    key = (name or "").strip().lower()
    if key in ("flux", "image", "comfyui"):
        key = "comfy"
    data = times if times is not None else load_switch_times()
    entry = data.get(key)
    if not isinstance(entry, dict) or entry.get(field) is None:
        return None
    try:
        return max(1, int(round(float(entry[field]))))
    except (TypeError, ValueError):
        return None
