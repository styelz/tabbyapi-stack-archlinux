"""Ports, backend URL, and keys for the public sidecar."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from sidecar.paths import STACK_ROOT, TABBY_DIR, VENDOR_DIR, ensure_import_path

ensure_import_path()


def sidecar_host() -> str:
    return os.environ.get("SIDECAR_HOST") or os.environ.get("TABBY_NETWORK_HOST") or "0.0.0.0"


def sidecar_port() -> int:
    raw = os.environ.get("SIDECAR_PORT") or os.environ.get("TABBY_NETWORK_PORT") or "5000"
    return int(raw)


def backend_host() -> str:
    return os.environ.get("TABBY_BACKEND_HOST") or "127.0.0.1"


def backend_port() -> int:
    return int(os.environ.get("TABBY_BACKEND_PORT") or "5001")


def backend_url() -> str:
    explicit = os.environ.get("TABBY_BACKEND_URL")
    if explicit:
        return explicit.rstrip("/")
    return f"http://{backend_host()}:{backend_port()}"


def backend_dir() -> Path:
    override = os.environ.get("TABBY_BACKEND_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if (VENDOR_DIR / "main.py").is_file():
        return VENDOR_DIR.resolve()
    return TABBY_DIR.resolve()


def uses_vanilla_backend() -> bool:
    return backend_dir().resolve() != TABBY_DIR.resolve()


def sidecar_enabled() -> bool:
    return os.environ.get("TABBY_SIDECAR", "1") != "0"


def is_sidecar_process() -> bool:
    """True in the public 5000 process. Tabby on 5001 must stay local."""
    role = (os.environ.get("TABBY_PROCESS") or "").strip().lower()
    if role == "sidecar":
        return True
    if role == "tabby":
        return False
    cmd = " ".join(sys.argv).replace("\\", "/").lower()
    if "-m sidecar" in cmd or "/sidecar/__main__.py" in cmd:
        return True
    return "sidecar" in cmd and "__main__.py" in cmd


def key_path() -> Path:
    override = os.environ.get("SIDECAR_BACKEND_KEY_FILE")
    if override:
        return Path(override)
    return STACK_ROOT / "sidecar" / ".backend-key"
