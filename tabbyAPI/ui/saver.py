"""Localhost-only snapshot for the TTY kiosk screensaver.

No usernames or chat ids. The current Comfy prompt or LLM ask is included
as image_what for the idle-field HUD.
"""

from __future__ import annotations

import ipaddress
import time
from typing import Any

from fastapi import HTTPException, Request

SAFE_KINDS = frozenset({"chat", "code", "image", "gpu"})
SAFE_STAGES = frozenset({"prefill", "decode", "tool", "image", "switch", "recover", "idle"})
_COMFY_LOCKS = frozenset({"comfy", "flux"})
_LEAK_KEYS = frozenset(
    {
        "occupant",
        "prompt",
        "chat_id",
        "user",
        "hint",
        "job",
        "stack_queue",
        "profiles",
        "profile_labels",
        "api_base",
    }
)


def _ip_is_loopback(host: str) -> bool:
    text = (host or "").strip().strip("[]")
    if not text:
        return False
    if text.lower() in {"localhost", "127.0.0.1", "::1"}:
        return True
    if text.lower().startswith("::ffff:"):
        text = text[7:]
    try:
        return bool(ipaddress.ip_address(text).is_loopback)
    except ValueError:
        return False


def peer_is_loopback(request: Request) -> bool:
    """True only when the TCP peer (and any forwarded client) is loopback.

    A local reverse proxy that forwards a public client is rejected: the
    screensaver feed is for the GPU host's own kiosk, not the LAN.
    """
    peer = ""
    client = getattr(request, "client", None)
    if client is not None:
        peer = str(getattr(client, "host", "") or "")
    if not _ip_is_loopback(peer):
        return False
    headers = getattr(request, "headers", None) or {}
    forwarded = ""
    getter = getattr(headers, "get", None)
    if callable(getter):
        forwarded = (getter("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded and not _ip_is_loopback(forwarded):
        return False
    return True


def require_loopback(request: Request) -> None:
    if not peer_is_loopback(request):
        raise HTTPException(403, "Saver state is localhost only.")


def _kind(raw: Any) -> str | None:
    text = str(raw or "").strip()
    return text if text in SAFE_KINDS else None


def _stage(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    return text if text in SAFE_STAGES else "idle"


def _switch_target(raw: Any) -> str | None:
    text = str(raw or "").strip().lower()
    if text in {"comfy", "flux"}:
        return "comfy"
    if text == "llm":
        return "llm"
    return None


def switch_load_what(lock_name: str, image_phase: str = "") -> str | None:
    """llm or comfy for the kiosk HUD. None when this is not a model load."""
    name = str(lock_name or "").strip().lower()
    if name == "restart":
        return None
    if name in _COMFY_LOCKS:
        return "comfy"
    if name:
        return "llm"
    phase = str(image_phase or "").strip().lower()
    if phase == "starting_comfy":
        return "comfy"
    if phase == "restoring_llm":
        return "llm"
    return None


def _int_ge0(value: Any, default: int = 0) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return 0 if number < 0 else number


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return 0 if number < 0 else number


def _vram_pct(gpu: dict[str, Any]) -> int | None:
    used = gpu.get("memory_used_mib")
    total = gpu.get("memory_total_mib")
    try:
        used_n = float(used)
        total_n = float(total)
    except (TypeError, ValueError):
        return None
    if total_n <= 0:
        return None
    return int(round(100.0 * used_n / total_n))


def sanitize_status(raw: dict[str, Any]) -> dict[str, Any]:
    """Whitelist the fields a wall monitor may show."""
    gpu = raw.get("gpu") if isinstance(raw.get("gpu"), dict) else {}
    host = raw.get("host") if isinstance(raw.get("host"), dict) else {}
    queue = raw.get("stack_queue") if isinstance(raw.get("stack_queue"), dict) else {}
    profile = str(raw.get("profile") or "").strip() or None
    gpu_mode = str(raw.get("gpu_mode") or "").strip() or None
    payload = {
        "ok": True,
        "gpu_mode": gpu_mode,
        "profile": profile,
        "busy": bool(raw.get("busy") or queue.get("busy") or queue.get("live")),
        "switching": bool(raw.get("switching")),
        "restarting": bool(raw.get("restarting")),
        "recovering": bool(raw.get("recovering")),
        "switch_target": _switch_target(raw.get("switch_target")),
        "kind": _kind(queue.get("kind") if queue.get("kind") is not None else raw.get("kind")),
        "stage": _stage(raw.get("stage")),
        "tokens": _int_ge0(raw.get("tokens")),
        "run_tokens": _int_ge0(
            raw.get("run_tokens") if raw.get("run_tokens") is not None else raw.get("tokens")
        ),
        "image_n": _optional_int(raw.get("image_n")),
        "image_of": _optional_int(raw.get("image_of")),
        "image_file": _safe_image_file(raw.get("image_file")),
        "image_what": _safe_image_what(raw.get("image_what"))
        or _safe_image_what(queue.get("prompt")),
        "waiters": _int_ge0(
            raw.get("waiters") if raw.get("waiters") is not None else queue.get("waiters")
        ),
        "elapsed_s": _int_ge0(
            raw.get("elapsed_s")
            if raw.get("elapsed_s") is not None
            else queue.get("elapsed_s")
        ),
        "typical_s": _optional_int(raw.get("typical_s")),
        "gpu": {
            "utilization_pct": gpu.get("utilization_pct"),
            "vram_pct": _vram_pct(gpu) if gpu.get("vram_pct") is None else gpu.get("vram_pct"),
            "temperature_c": gpu.get("temperature_c"),
        },
        "host": {
            "cpu_pct": host.get("cpu_pct"),
        },
    }
    leaked = _LEAK_KEYS.intersection(payload)
    if leaked:
        raise RuntimeError(f"saver payload leaked {sorted(leaked)}")
    return payload


def _flight_prompt(flights: list[Any]) -> str:
    for flight in flights:
        if getattr(flight, "done", False):
            continue
        text = str(getattr(flight, "prompt", "") or "").strip()
        if text:
            return _safe_image_what(text)
    return ""


def _flight_weather(flights: list[Any]) -> tuple[int, str]:
    """Character counts from assembled output — not the text itself."""
    for flight in flights:
        if getattr(flight, "done", False):
            continue
        chars = len(str(getattr(flight, "assembled", "") or "")) + len(
            str(getattr(flight, "reasoning", "") or "")
        )
        kind = str(getattr(flight, "kind", "") or "")
        steps = getattr(flight, "steps", None) or []
        last = steps[-1] if steps else None
        if kind == "image" and not chars:
            return chars, "image"
        if isinstance(last, dict) and last.get("type") == "tool" and not last.get("result"):
            return chars, "tool"
        if chars:
            return chars, "decode"
        return 0, "prefill"
    return 0, "idle"


def _safe_image_file(raw: Any) -> str:
    """Dest basename or images/foo.png — never a home/user path."""
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        return ""
    text = text.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in text.split("/") if p and p not in {".", ".."}]
    if not parts:
        return ""
    name = parts[-1]
    if len(parts) >= 2 and parts[-2].lower() in {"images", "img", "assets", "static"}:
        shown = f"{parts[-2]}/{name}"
    else:
        shown = name
    if len(shown) > 48:
        shown = shown[:45] + "..."
    return shown


def _safe_image_what(raw: Any) -> str:
    """Task / prompt for the wall. No newlines; cap length."""
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    if len(text) > 360:
        text = text[:357].rstrip() + "..."
    return text


def _job_attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _current_job_item(job: Any) -> Any:
    items = _job_attr(job, "items")
    if not items:
        return None
    try:
        idx = int(_job_attr(job, "current_index") or 0)
    except (TypeError, ValueError):
        idx = 0
    if idx < 0:
        idx = 0
    if idx >= len(items):
        idx = len(items) - 1
    return items[idx]


def _image_caption(job: Any) -> tuple[str, str]:
    if job is None:
        return "", ""
    item = _current_job_item(job)
    path = _job_attr(item, "output_path") if item is not None else None
    prompt = _job_attr(item, "prompt") if item is not None else None
    path = path or _job_attr(job, "output_path") or ""
    if not prompt:
        fallback = str(_job_attr(job, "prompt") or "").strip()
        bits = fallback.split()
        if not (len(bits) == 2 and bits[0].isdigit() and bits[1] == "images"):
            prompt = fallback
    return _safe_image_file(path), _safe_image_what(prompt or "")


def _image_progress(job: Any) -> tuple[int | None, int | None, str, str, str]:
    if job is None:
        return None, None, "", "", ""
    status = str(getattr(job, "status", "") or "")
    if status not in ("queued", "running", "coding"):
        return None, None, "", "", ""
    phase = str(getattr(job, "phase", "") or status)
    try:
        count = max(1, int(getattr(job, "count", 0) or 1))
    except (TypeError, ValueError):
        count = 1
    try:
        index = int(getattr(job, "current_index", 0) or 0)
    except (TypeError, ValueError):
        index = 0
    n = index + 1
    if n > count:
        n = count
    image_file, image_what = _image_caption(job)
    return n, count, phase, image_file, image_what


def _compose_weather(
    *,
    switching: bool,
    restarting: bool,
    queue: dict[str, Any],
    decode: dict[str, Any],
    job: Any,
    flights: list[Any],
) -> dict[str, Any]:
    image_n, image_of, image_phase, image_file, image_what = _image_progress(job)
    if not image_what:
        image_what = _safe_image_what(queue.get("prompt")) or _flight_prompt(flights)
    tokens = 0
    run_tokens = 0
    stage = "idle"
    kind = queue.get("kind")
    busy = bool(queue.get("busy") or queue.get("live"))

    if restarting or switching:
        stage = "switch"
    elif image_phase == "restoring_llm":
        stage = "switch"
    elif image_phase in ("writing_code", "coding"):
        kind = kind or "code"
    elif image_phase:
        stage = "image"
        kind = kind or "image"

    if stage == "idle":
        decode_stage = str(decode.get("stage") or "idle")
        decode_tokens = _int_ge0(decode.get("tokens"))
        decode_run = _int_ge0(
            decode.get("run_tokens") if decode.get("run_tokens") is not None else decode_tokens
        )
        if decode_stage in ("prefill", "decode"):
            stage = decode_stage
            tokens = decode_tokens
            run_tokens = max(decode_run, decode_tokens)
            kind = kind or "chat"
        else:
            flight_tokens, flight_stage = _flight_weather(flights)
            if flight_stage != "idle":
                stage = flight_stage
                tokens = flight_tokens
                run_tokens = flight_tokens
            elif busy and str(kind or "") in {"chat", "code"}:
                stage = "prefill"

    return {
        "tokens": tokens,
        "run_tokens": max(run_tokens, tokens),
        "stage": stage,
        "image_n": image_n,
        "image_of": image_of,
        "image_file": image_file,
        "image_what": image_what,
        "kind": kind,
        "waiters": _int_ge0(queue.get("waiters")),
        "elapsed_s": _int_ge0(queue.get("elapsed_s")),
    }


def _typical_switch_s(
    lock_name: str, switching: bool, restarting: bool, stage: str
) -> int | None:
    if not (switching or restarting or stage == "switch"):
        return None
    name = str(lock_name or "").strip().lower()
    if restarting or name == "restart":
        name = "llm"
    elif name in {"flux", "comfy"}:
        name = "comfy"
    elif not name:
        name = "llm"
    from common.switch_times import ready_seconds

    return ready_seconds(name)


def _lock_age_s() -> int:
    from common.phrase_switch import LOCK

    try:
        if LOCK.exists():
            return max(0, int(time.time() - LOCK.stat().st_mtime))
    except OSError:
        pass
    return 0


async def saver_state() -> dict[str, Any]:
    """Occupancy weather only — never wait on nvidia-smi or HealthManager.

    The kiosk needs to see thinking the moment StackGate is taken. Full
    stack_status blocks the event loop on nvidia-smi (up to 5s), which is why
    the field used to sit idle for several seconds after a chat started.
    """
    from common.gpu_mode import read_mode
    from common.live_decode import snapshot as decode_snapshot
    from common.phrase_switch import (
        last_llm_profile_name,
        profile_alias_for_model,
        switch_lock_held,
        switch_lock_name,
    )
    from images.jobs import active_mcp_image_job, loaded_tabby_name
    from select_model import last_profile
    from ui.flight import iter_live_flights
    from ui.manager import cached_nvidia_stats, ensure_gpu_cache
    from ui.occupancy import snapshot as stack_queue_snapshot

    mode = read_mode()
    tabby = loaded_tabby_name()
    gpu_mode = "llm" if tabby else (mode.get("mode") or "llm")
    lock_name = switch_lock_name()
    lock_held = switch_lock_held()
    restarting = lock_held and lock_name == "restart"
    switching = lock_held and not restarting
    profile = profile_alias_for_model(tabby) or last_llm_profile_name() or last_profile()
    queue = stack_queue_snapshot("")
    ensure_gpu_cache()
    job = active_mcp_image_job()
    weather = _compose_weather(
        switching=switching,
        restarting=restarting,
        queue=queue if isinstance(queue, dict) else {},
        decode=decode_snapshot(),
        job=job,
        flights=iter_live_flights(),
    )
    job_phase = str(getattr(job, "phase", "") or "") if job is not None else ""
    switch_target = switch_load_what(lock_name, job_phase)
    if switch_target is None and (switching or weather.get("stage") == "switch"):
        switch_target = "comfy" if gpu_mode == "comfy" and not tabby else "llm"
    if not (switching or weather.get("stage") == "switch"):
        switch_target = None
    if weather.get("kind"):
        queue = dict(queue)
        queue["kind"] = weather["kind"]
    thinking = weather["stage"] in {"prefill", "decode", "tool"}
    from common.vram_recover import current_notice

    notice = current_notice()
    recovering = bool(notice.get("phase"))
    if recovering:
        weather["stage"] = "recover"
        detail = str(notice.get("detail") or notice.get("phase") or "").strip()
        if detail:
            weather["image_what"] = _safe_image_what(detail)
    typical_s = _typical_switch_s(lock_name, switching, restarting, str(weather.get("stage") or ""))
    elapsed_s = weather["elapsed_s"]
    if typical_s is not None:
        # Occupancy elapsed is the chat/job that holds StackGate. The HUD
        # says Loading LLM/Comfy here, so the clock is the switch lock
        # (when that load started), not the run that triggered it.
        elapsed_s = _lock_age_s()
    return sanitize_status(
        {
            "gpu_mode": gpu_mode,
            "profile": profile,
            "busy": bool(lock_held) or bool(queue.get("busy")) or thinking or recovering,
            "switching": switching,
            "restarting": restarting,
            "recovering": recovering,
            "switch_target": switch_target,
            "stack_queue": queue,
            "tokens": weather["tokens"],
            "run_tokens": weather.get("run_tokens") or weather["tokens"],
            "stage": weather["stage"],
            "image_n": weather["image_n"],
            "image_of": weather["image_of"],
            "image_file": weather["image_file"],
            "image_what": weather["image_what"],
            "waiters": weather["waiters"],
            "elapsed_s": elapsed_s,
            "typical_s": typical_s,
            "gpu": cached_nvidia_stats(),
        }
    )
