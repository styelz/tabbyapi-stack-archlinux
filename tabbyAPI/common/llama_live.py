"""Decode weather from llama-server /slots for the TTY kiosk."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_CACHE_TTL_S = 0.2
_FETCH_TIMEOUT_S = 0.15
_RUN_GAP_S = 12.0
_cache: tuple[float, list[Any] | None] = (0.0, None)
_run_tokens = 0
_run_step = 0
_run_task: Any = None
_run_active = 0.0
_IDLE = {"busy": False, "tokens": 0, "run_tokens": 0, "stage": "idle"}


def reset_for_tests() -> None:
    global _cache, _run_tokens, _run_step, _run_task, _run_active
    _cache = (0.0, None)
    _run_tokens = 0
    _run_step = 0
    _run_task = None
    _run_active = 0.0


def _int_ge0(value: Any) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    return 0 if number < 0 else number


def slot_n_decoded(slot: dict[str, Any]) -> int:
    nxt = slot.get("next_token")
    if isinstance(nxt, list) and nxt:
        first = nxt[0] if isinstance(nxt[0], dict) else {}
        if first.get("n_decoded") is not None:
            return _int_ge0(first.get("n_decoded"))
    for key in ("n_decoded", "n_gen", "generated_tokens"):
        if slot.get(key) is not None:
            return _int_ge0(slot.get(key))
    return 0


def slot_is_processing(slot: dict[str, Any]) -> bool:
    if slot.get("is_processing") is True:
        return True
    nxt = slot.get("next_token")
    if isinstance(nxt, list) and nxt and isinstance(nxt[0], dict):
        return bool(nxt[0].get("has_next_token"))
    return False


def weather_from_slots(payload: Any) -> dict[str, Any]:
    """Map llama-server /slots JSON onto live_decode's snapshot shape."""
    rows = payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        rows = [payload]
    slot = None
    for item in rows:
        if isinstance(item, dict) and slot_is_processing(item):
            slot = item
            break
    if slot is None and rows and isinstance(rows[0], dict):
        slot = rows[0]
    if not isinstance(slot, dict) or not slot_is_processing(slot):
        return dict(_IDLE)
    tokens = slot_n_decoded(slot)
    prompt = _int_ge0(slot.get("n_prompt_tokens"))
    processed = _int_ge0(slot.get("n_prompt_tokens_processed"))
    if tokens > 0:
        stage = "decode"
    elif prompt > 0 and processed < prompt:
        stage = "prefill"
    else:
        stage = "prefill"
    return {
        "busy": True,
        "tokens": tokens,
        "run_tokens": tokens,
        "stage": stage,
        "task_id": slot.get("id_task"),
    }


def _fetch_slots() -> list[Any] | None:
    global _cache
    now = time.monotonic()
    cached_at, cached = _cache
    if cached is not None and now - cached_at < _CACHE_TTL_S:
        return cached
    try:
        from common.llama_runtime import llama_url

        req = Request(str(llama_url()).rstrip("/") + "/slots", method="GET")
        with urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "[]")
    except (URLError, HTTPError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        _cache = (now, None)
        return None
    if not isinstance(payload, list):
        payload = [payload] if isinstance(payload, dict) else []
    _cache = (now, payload)
    return payload


def snapshot() -> dict[str, Any]:
    """Current llama decode step, with a run total across nearby tool calls."""
    global _run_tokens, _run_step, _run_task, _run_active
    rows = _fetch_slots()
    weather = weather_from_slots(rows)
    now = time.monotonic()
    if not weather.get("busy"):
        if _run_active > 0.0 and now - _run_active > _RUN_GAP_S:
            _run_tokens = 0
            _run_step = 0
            _run_task = None
            _run_active = 0.0
        return dict(_IDLE)
    tokens = _int_ge0(weather.get("tokens"))
    task = weather.get("task_id")
    if _run_active > 0.0 and now - _run_active > _RUN_GAP_S:
        _run_tokens = 0
        _run_step = 0
        _run_task = None
    if _run_task is not None and task != _run_task and _run_step > 0:
        _run_tokens += _run_step
        _run_step = 0
    _run_task = task
    _run_step = tokens
    _run_active = now
    weather["run_tokens"] = _run_tokens + tokens
    return weather
