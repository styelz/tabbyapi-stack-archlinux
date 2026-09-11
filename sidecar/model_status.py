"""LLM loaded? Ask the backend over HTTP when we are the sidecar."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from sidecar.backend_key import ensure_backend_key
from sidecar.paths import ensure_import_path
from sidecar.settings import backend_url

ensure_import_path()


def _backend_configured() -> bool:
    return bool(os.environ.get("TABBY_BACKEND_URL") or os.environ.get("TABBY_BACKEND_KEY"))


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


def llm_is_ready() -> bool:
    if _backend_configured():
        status, payload = _get_json("/v1/model")
        if status != 200 or not isinstance(payload, dict):
            return False
        return bool(payload.get("id") or payload.get("parameters"))
    from common import model

    container = getattr(model, "container", None)
    return bool(container and getattr(container, "loaded", False))


def model_card() -> dict[str, Any]:
    if _backend_configured():
        status, payload = _get_json("/v1/model")
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
