"""In-process decode weather for the TTY kiosk. Numbers only — never text."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_holders: set[str] = set()
_request_id: str | None = None
_tokens: int = 0
_run_tokens: int = 0
_run_started: float = 0.0
_last_active: float = 0.0
_stage: str = "idle"
_last_write = 0.0
LIVE_PATH = Path(__file__).resolve().parents[1] / "saver-live.json"
_RUN_GAP_S = 12.0


def is_generate_post(method: str, path: str) -> bool:
    """True for Chat Completions / completions / console chat POSTs."""
    if str(method or "").upper() != "POST":
        return False
    text = (path or "").split("?", 1)[0].rstrip("/") or "/"
    if text.endswith("/v1/ui/chat"):
        return True
    if text.endswith("/chat/completions"):
        return True
    if text.endswith("/v1/completions") or text == "/completions":
        return True
    return False


def _idle_locked() -> None:
    global _request_id, _tokens, _stage, _run_tokens
    if _tokens > 0:
        _run_tokens += _tokens
    _request_id = None
    _tokens = 0
    _stage = "idle"


def _touch_run_locked(now: float, *, new_step: bool) -> None:
    """Keep one token/time total across tool-call generates in the same run."""
    global _run_tokens, _run_started, _tokens, _last_active
    if _run_started > 0.0 and _last_active > 0.0 and (now - _last_active) > _RUN_GAP_S:
        _run_tokens = 0
        _run_started = now
        _tokens = 0
    elif _run_started <= 0.0:
        _run_started = now
        _run_tokens = 0
    elif new_step and _tokens > 0:
        _run_tokens += _tokens
        _tokens = 0
    _last_active = now


def _snapshot_locked() -> dict[str, Any]:
    stage = str(_stage)
    if stage == "idle" and _holders:
        stage = "prefill"
    busy = bool(_holders) or stage != "idle"
    step = int(_tokens)
    return {
        "busy": busy,
        "tokens": step,
        "run_tokens": int(_run_tokens) + step,
        "stage": stage,
    }


def _persist_locked(*, force: bool = False) -> None:
    """Sidecar for the kiosk when GET /saver/state is queued behind a generate."""
    global _last_write
    now = time.monotonic()
    if not force and _stage == "decode" and now - _last_write < 0.25:
        return
    _last_write = now
    payload = _snapshot_locked()
    path = LIVE_PATH
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def hold(key: str, *, stage: str = "prefill") -> None:
    """Mark an in-flight generate POST or GPU job. Does not wait on the model."""
    token = str(key or "").strip()
    if not token:
        return
    want = str(stage or "prefill").strip().lower()
    if want not in {"prefill", "decode"}:
        want = "prefill"
    with _lock:
        global _stage
        _holders.add(token)
        if want == "decode":
            _stage = "decode"
        elif _stage != "decode":
            _stage = "prefill"
        _persist_locked(force=True)


def release(key: str) -> None:
    token = str(key or "").strip()
    with _lock:
        if token:
            _holders.discard(token)
        if _holders:
            _persist_locked(force=True)
            return
        _idle_locked()
        _persist_locked(force=True)


def note_prefill(request_id: str) -> None:
    rid = str(request_id or "").strip()
    if not rid:
        return
    with _lock:
        global _request_id, _tokens, _stage
        _touch_run_locked(time.monotonic(), new_step=True)
        _holders.add(rid)
        _request_id = rid
        _tokens = 0
        _stage = "prefill"
        _persist_locked(force=True)


def note_decode(request_id: str, tokens: int) -> None:
    rid = str(request_id or "").strip()
    if not rid:
        return
    try:
        count = int(tokens)
    except (TypeError, ValueError):
        count = 0
    if count < 0:
        count = 0
    with _lock:
        global _request_id, _tokens, _stage
        if _request_id and _request_id != rid:
            return
        _touch_run_locked(time.monotonic(), new_step=False)
        _holders.add(rid)
        _request_id = rid
        _tokens = count
        _stage = "decode"
        _persist_locked()


def clear(request_id: str | None = None) -> None:
    rid = str(request_id or "").strip()
    with _lock:
        global _request_id, _tokens, _stage
        if rid:
            _holders.discard(rid)
            if _request_id and _request_id != rid:
                if _holders:
                    _persist_locked(force=True)
                    return
                _persist_locked(force=True)
                return
        else:
            _holders.clear()
        if _holders:
            _persist_locked(force=True)
            return
        _idle_locked()
        _persist_locked(force=True)


def snapshot() -> dict[str, Any]:
    with _lock:
        return _snapshot_locked()


def read_live_file(path: Path | None = None) -> dict[str, Any] | None:
    """Kiosk-side read. None when the API has not written a file."""
    try:
        raw = (path or LIVE_PATH).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def overlay_live_file(payload: dict[str, Any] | None, live: dict[str, Any] | None) -> dict[str, Any] | None:
    """Prefer the sidecar when a prompt is in flight and HTTP is stale or hung."""
    if not live or not live.get("busy"):
        return payload
    out = dict(payload or {})
    out["busy"] = True
    stage = str(live.get("stage") or "prefill").strip().lower()
    if stage in {"prefill", "decode", "tool"}:
        out["stage"] = stage
    if not out.get("kind"):
        out["kind"] = "chat"
    try:
        tokens = int(live.get("tokens") or 0)
    except (TypeError, ValueError):
        tokens = 0
    if tokens > 0:
        out["tokens"] = tokens
    try:
        run_tokens = int(live.get("run_tokens") or 0)
    except (TypeError, ValueError):
        run_tokens = 0
    if run_tokens > 0:
        out["run_tokens"] = run_tokens
    return out


def reset_for_tests() -> None:
    global _run_tokens, _run_started, _last_active
    with _lock:
        _holders.clear()
        _run_tokens = 0
        _run_started = 0.0
        _last_active = 0.0
        _idle_locked()
    try:
        LIVE_PATH.unlink(missing_ok=True)
    except TypeError:
        try:
            LIVE_PATH.unlink()
        except OSError:
            pass
    except OSError:
        pass
