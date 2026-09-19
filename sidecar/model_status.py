"""LLM loaded? Ask the backend over HTTP when we are the sidecar."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from sidecar.backend_key import ensure_backend_key
from sidecar.paths import ensure_import_path
from sidecar.settings import backend_url

ensure_import_path()

# Status + screensaver used to GET /v1/model several times a second. The card
# only changes on load/unload; TTL plus mode/lock epoch covers switches.
# A dummy /v1/model card (cache_mode=comfy) is not an LLM. Do not skip the
# HTTP just because gpu_mode.json still says comfy after an LLM load.
_CACHE_TTL_S = 5.0
_FAIL_TTL_S = 5.0
_cache_lock = threading.Lock()
_model_cache: dict[str, Any] | None = None


def _backend_configured() -> bool:
    return bool(os.environ.get("TABBY_BACKEND_URL") or os.environ.get("TABBY_BACKEND_KEY"))


def _gpu_mode() -> str:
    try:
        from common.gpu_mode import read_mode

        return (read_mode().get("mode") or "").lower()
    except Exception:
        return ""


def _get_json(path: str, timeout: float = 2.0) -> tuple[int, Any]:
    url = backend_url().rstrip("/") + path
    key = ensure_backend_key()
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}", "X-API-Key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        return int(exc.code), None
    except (OSError, ValueError):
        return 0, None
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except ValueError:
        return status, None


def _admin_headers() -> dict[str, str]:
    key = ensure_backend_key()
    return {
        "Authorization": f"Bearer {key}",
        "X-API-Key": key,
        "X-Admin-Key": key,
    }


def invalidate_model_cache() -> None:
    global _model_cache
    with _cache_lock:
        _model_cache = None


def _model_epoch() -> tuple[float, float]:
    mode_m = 0.0
    lock_m = 0.0
    try:
        from common.gpu_mode import STATUS_PATH

        mode_m = STATUS_PATH.stat().st_mtime
    except OSError:
        pass
    try:
        from common.phrase_switch import LOCK

        if LOCK.exists():
            lock_m = LOCK.stat().st_mtime
    except OSError:
        pass
    return (mode_m, lock_m)


def _cached_backend_model() -> tuple[int, Any]:
    global _model_cache
    epoch = _model_epoch()
    now = time.monotonic()
    with _cache_lock:
        cached = _model_cache
        if cached and cached.get("epoch") == epoch and now < float(cached.get("until") or 0):
            return int(cached["status"]), cached.get("payload")
    status, payload = _get_json("/v1/model")
    ttl = _CACHE_TTL_S if status == 200 else _FAIL_TTL_S
    with _cache_lock:
        _model_cache = {
            "epoch": epoch,
            "until": now + ttl,
            "status": status,
            "payload": payload,
        }
    return status, payload


def _is_comfy_placeholder(card: dict[str, Any] | None) -> bool:
    """GET /v1/model returns a dummy card while Comfy owns the GPU."""
    if not card:
        return False
    name = str(card.get("id") or "").strip().lower()
    if name in {"comfy", "flux", "image", "comfyui"}:
        return True
    return str(card.get("cache_mode") or "").strip().lower() == "comfy"


def loaded_model_id() -> str | None:
    try:
        from common.gpu_mode import llama_loaded_id

        if _gpu_mode() == "llama":
            return llama_loaded_id()
    except Exception:
        pass
    card = model_card()
    if _is_comfy_placeholder(card):
        return None
    name = str(card.get("id") or "").strip()
    return name or None


def unload_backend() -> None:
    """POST /v1/model/unload on Tabby. 503 means already empty."""
    invalidate_model_cache()
    if not loaded_model_id():
        return
    url = backend_url().rstrip("/") + "/v1/model/unload"
    req = urllib.request.Request(url, method="POST", headers=_admin_headers())
    try:
        urllib.request.urlopen(req, timeout=180).read()
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            return
        raise RuntimeError(exc.read().decode("utf-8", "replace") or str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc
    finally:
        invalidate_model_cache()


def load_backend_model(payload: dict[str, Any]) -> None:
    """POST /v1/model/load on Tabby and wait for the SSE finished event."""
    invalidate_model_cache()
    wanted = str((payload or {}).get("model_name") or "").strip()
    if not wanted:
        raise RuntimeError("A model name was not provided for load.")
    current = loaded_model_id()
    if current == wanted:
        return
    if current:
        unload_backend()
    url = backend_url().rstrip("/") + "/v1/model/load"
    body = json.dumps(payload).encode("utf-8")
    headers = _admin_headers()
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "text/event-stream"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    finished = False
    error = None
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("error"):
                    error = event["error"]
                    break
                if event.get("status") == "finished":
                    finished = True
    except urllib.error.HTTPError as exc:
        raise RuntimeError(exc.read().decode("utf-8", "replace") or str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc
    finally:
        invalidate_model_cache()
    if error:
        raise RuntimeError(str(error))
    if not finished:
        raise RuntimeError("Load stream ended before the model finished loading.")


def llm_is_ready() -> bool:
    try:
        from common.gpu_mode import llama_up

        mode = _gpu_mode()
        if mode == "llama":
            return llama_up()
    except Exception:
        pass
    if _backend_configured():
        status, payload = _cached_backend_model()
        if status != 200 or not isinstance(payload, dict):
            return False
        params = payload.get("parameters") or {}
        if isinstance(params, dict) and str(params.get("cache_mode") or "").lower() == "comfy":
            return False
        name = str(payload.get("id") or "").strip().lower()
        if name in {"comfy", "flux", "image", "comfyui"}:
            return False
        return bool(payload.get("id") or payload.get("parameters"))
    from common import model

    container = getattr(model, "container", None)
    return bool(container and getattr(container, "loaded", False))


def model_card() -> dict[str, Any]:
    try:
        from common.gpu_mode import llama_loaded_id, llama_up
        from common.llama_runtime import read_llama_runtime

        mode = _gpu_mode()
        if mode == "llama" and llama_up():
            runtime = read_llama_runtime()
            return {
                "id": llama_loaded_id() or runtime.get("profile") or "gpt-4o",
                "max_seq_len": runtime.get("max_seq_len"),
                "cache_size": runtime.get("max_seq_len"),
                "cache_mode": "gguf",
                "use_vision": bool(runtime.get("mmproj")),
            }
    except Exception:
        pass
    if _backend_configured():
        status, payload = _cached_backend_model()
        if status != 200 or not isinstance(payload, dict):
            return {}
        params = payload.get("parameters") or {}
        if not isinstance(params, dict):
            params = {}
        return {
            "id": payload.get("id"),
            "max_seq_len": params.get("max_seq_len"),
            "cache_size": params.get("cache_size"),
            "cache_mode": params.get("cache_mode"),
            "use_vision": params.get("use_vision"),
        }
    from common import model

    container = getattr(model, "container", None)
    if not container or not getattr(container, "loaded", False):
        return {}
    try:
        card = container.model_info()
        payload = card.model_dump() if hasattr(card, "model_dump") else dict(card)
    except Exception:
        payload = {"id": getattr(getattr(container, "model_dir", None), "name", None)}
    params = payload.get("parameters") or {}
    return {
        "id": payload.get("id"),
        "max_seq_len": params.get("max_seq_len"),
        "cache_size": params.get("cache_size"),
        "cache_mode": params.get("cache_mode"),
        "use_vision": params.get("use_vision"),
    }


def llm_jobs_active() -> bool:
    """Sidecar cannot see in-process ExLlama jobs; occupancy owns the queue."""
    if _backend_configured():
        return False
    try:
        from common import model as tabby_model

        container = tabby_model.container
        jobs = getattr(container, "active_job_ids", None) if container is not None else None
        return bool(jobs)
    except Exception:
        return False
