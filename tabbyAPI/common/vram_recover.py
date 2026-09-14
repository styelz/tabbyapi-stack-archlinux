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


def _short_exc(exc: object) -> str:
    text = str(exc or "").strip().splitlines()[0].strip()
    if isinstance(exc, str):
        return text[:180]
    name = type(exc).__name__ if exc is not None else "Error"
    if not text:
        return name
    if name.lower() in text.lower():
        return text[:180]
    return f"{name}: {text}"[:180]


def _gpu_memory_line() -> str:
    """Used/total VRAM without blocking on nvidia-smi."""
    try:
        import torch

        if getattr(torch, "cuda", None) and torch.cuda.is_available():
            free_b, total_b = torch.cuda.mem_get_info()
            used_mib = int((int(total_b) - int(free_b)) / (1024 * 1024))
            total_mib = int(int(total_b) / (1024 * 1024))
            free_mib = int(int(free_b) / (1024 * 1024))
            name = str(torch.cuda.get_device_name(0) or "")
            short = name.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "").strip()
            gpu = short or "GPU"
            return f"{gpu} ({used_mib} / {total_mib} MiB used, {free_mib} MiB free)"
    except Exception:
        pass
    try:
        from ui.manager import cached_nvidia_stats

        gpu = cached_nvidia_stats() or {}
        name = str(gpu.get("name") or "").replace("NVIDIA GeForce ", "").replace("NVIDIA ", "").strip()
        used = gpu.get("memory_used_mib")
        total = gpu.get("memory_total_mib")
        if used is None or total is None:
            return name
        free = max(0, int(total) - int(used))
        label = name or "GPU"
        return f"{label} ({int(used)} / {int(total)} MiB used, {free} MiB free)"
    except Exception:
        return ""


def _loaded_model_bits() -> tuple[str, str]:
    folder = ""
    try:
        from common import model as model_mod

        container = getattr(model_mod, "container", None)
        model_dir = getattr(container, "model_dir", None) if container is not None else None
        folder = str(getattr(model_dir, "name", "") or "")
    except Exception:
        folder = ""
    if not folder:
        try:
            from common.tabby_config import config

            folder = str(getattr(getattr(config, "model", None), "model_name", "") or "")
        except Exception:
            folder = ""
    alias = ""
    if folder:
        try:
            from common.phrase_switch import profile_alias_for_model

            alias = str(profile_alias_for_model(folder) or "")
        except Exception:
            alias = ""
    return folder, alias


def generation_abort_message(exc: object, kind: str = "chat") -> str:
    """User-facing abort copy: cause, model/VRAM, and what to do next."""
    if kind == "completion":
        title = "Completion aborted"
    elif kind == "kobold":
        title = "Kobold generation aborted"
    else:
        title = "Chat completion aborted"
    cause = _short_exc(exc)
    text = str(exc or "")
    if is_vram_error(exc):
        if "no available slots" in text or "Cannot create new state" in text:
            lead = "the GPU cache ran out of slots"
        else:
            lead = "the GPU ran out of memory during generation"
        lines = [f"{title}: {lead} ({cause})."]
        folder, alias = _loaded_model_bits()
        gpu = _gpu_memory_line()
        model_bit = folder or alias
        if alias and folder and alias.lower() not in folder.lower():
            model_bit = f"{folder} ({alias})"
        where = []
        if model_bit:
            where.append(f"Loaded model: {model_bit}")
        if gpu:
            where.append(f"on {gpu}" if model_bit else gpu)
        if where:
            extra = " ".join(where) + "."
            extra += (
                " The first turn can fit while a follow-up with tool results "
                "or extra context needs more VRAM than was free."
            )
            lines.append(extra)
        if (alias or "").lower() == FALLBACK_PROFILE:
            lines.append(
                "The generator was reset, so the API is still up. A retry of "
                "the same turn can run out of memory again. Start a new chat "
                "or send less context, or send restart if it keeps failing."
            )
        else:
            lines.append(
                "The generator was reset, so the API is still up. A retry of "
                "the same turn can run out of memory again on this load. For "
                "coding on this GPU, switch to qwen (the 9B). This model "
                "sits on almost no VRAM headroom."
            )
        return "\n\n".join(lines)
    return (
        f"{title}: {cause}.\n\n"
        "The generator was reset after a crash. Retry the message, or send "
        "restart if it keeps failing."
    )


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
