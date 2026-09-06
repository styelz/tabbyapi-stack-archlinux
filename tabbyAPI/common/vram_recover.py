"""Guards for VRAM-fail recovery: one bounce, then fall back to qwen."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from common.switch_times import ready_seconds

ROOT = Path(__file__).resolve().parent.parent
RECOVER_PATH = ROOT / "model_profiles" / "vram_recover.json"
FALLBACK_PROFILE = "qwen"
BOUNCE_COOLDOWN_S = 10 * 60
VRAM_MARKERS = (
    "Insufficient VRAM",
    "out of memory",
    "OutOfMemory",
    "CUDA out of memory",
    "Allocation on device",
    "no available slots",
    "Cannot create new state",
)

# Short wall-monitor copy. No paths, no usernames.
_NOTICE: dict[str, str] = {}


def is_vram_error(exc: object) -> bool:
    text = str(exc)
    return any(marker in text for marker in VRAM_MARKERS)


def needs_generator_rebuild(exc: object) -> bool:
    """True when the ExLlama generator/cache is unsafe to keep using."""
    return is_vram_error(exc)


def set_notice(phase: str, detail: str = "") -> None:
    """What the kiosk should show while we unstick the GPU."""
    _NOTICE.clear()
    _NOTICE["phase"] = str(phase or "").strip()
    _NOTICE["detail"] = str(detail or "").strip()


def clear_notice() -> None:
    _NOTICE.clear()


def current_notice() -> dict[str, str]:
    return dict(_NOTICE)


def reset_recurrent_slots(cache: object) -> None:
    """Return every GDN/SWA slot after a crashed job that never released one."""
    from collections import deque

    n = int(getattr(cache, "num_slots", 0) or 0)
    if n <= 0 or not hasattr(cache, "free_list"):
        return
    cache.free_list = deque(range(n))


def reset_cuda_memory() -> None:
    """Lift ExLlama's per-process VRAM cap and return cached blocks to the driver.

    Autosplit sets torch.cuda.set_per_process_memory_fraction from free VRAM at
    load start, and only resets it after a successful load. A mid-load OOM
    leaves the cap in place so the next attempt still sees a shrunken budget.
    """
    try:
        import gc

        import torch
    except ImportError:
        return
    if not getattr(torch, "cuda", None) or not torch.cuda.is_available():
        return
    try:
        device_count = int(torch.cuda.device_count() or 0)
    except Exception:
        device_count = 0
    for index in range(device_count):
        try:
            torch.cuda.set_per_process_memory_fraction(1.0, device=index)
        except Exception:
            break
    gc.collect()
    try:
        torch.cuda.empty_cache()
    except Exception:
        pass
    ipc_collect = getattr(torch.cuda, "ipc_collect", None)
    if callable(ipc_collect):
        try:
            ipc_collect()
        except Exception:
            pass


def health_timeout_s(profile: str) -> float:
    """Wait long enough for a cold load after a bounce, with a hard cap."""
    return min(360.0, max(180.0, float(ready_seconds(profile)) + 90.0))


def read_state() -> dict[str, Any]:
    if not RECOVER_PATH.is_file():
        return {}
    try:
        data = json.loads(RECOVER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(state: dict[str, Any]) -> None:
    try:
        RECOVER_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def bounce_is_cooling(now: Optional[float] = None) -> bool:
    bounced = read_state().get("bounced_at")
    if not isinstance(bounced, (int, float)):
        return False
    return float(now if now is not None else time.time()) - float(bounced) < BOUNCE_COOLDOWN_S


def mark_bounce(profile: str) -> None:
    write_state(
        {
            "bounced_at": time.time(),
            "profile": profile,
            "action": "bounce",
        }
    )


def mark_fallback(failed: str, fallback: str) -> None:
    write_state(
        {
            "bounced_at": read_state().get("bounced_at"),
            "profile": failed,
            "action": "fallback",
            "fallback": fallback,
            "fallback_at": time.time(),
        }
    )
