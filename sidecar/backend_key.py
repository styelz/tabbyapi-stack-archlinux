"""Internal key the sidecar injects when talking to Tabby."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from sidecar.settings import key_path, uses_vanilla_backend, backend_dir


def ensure_backend_key(path: Path | None = None) -> str:
    env_key = os.environ.get("TABBY_BACKEND_KEY")
    if env_key:
        return env_key
    dest = path or key_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        text = dest.read_text(encoding="utf-8").strip()
        if text:
            os.environ["TABBY_BACKEND_KEY"] = text
            return text
    key = secrets.token_hex(16)
    dest.write_text(key + "\n", encoding="utf-8")
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    os.environ["TABBY_BACKEND_KEY"] = key
    return key


def write_backend_tokens(tabby_root: Path | None = None, key: str | None = None) -> Path | None:
    """Write a backend-only api_tokens.yml when the LLM tree is vanilla."""
    root = tabby_root or backend_dir()
    if not uses_vanilla_backend() and tabby_root is None:
        return None
    token = key or ensure_backend_key()
    dest = root / "api_tokens.yml"
    dest.write_text(
        f"api_key: {token}\nadmin_key: {token}\n",
        encoding="utf-8",
    )
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    return dest
