"""Install-scoped id so a new ISO at the same LAN URL is not the old machine."""

from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Optional

_LOCK = threading.Lock()
_EPOCH_PATH: Optional[Path] = None
EPOCH_BOOT_MARK = "window.TABBY_UI_EPOCH = null;"


def epoch_path() -> Path:
    if _EPOCH_PATH is not None:
        return _EPOCH_PATH
    from common.gpu_mode import GENERATED_DIR

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    return GENERATED_DIR / "ui_epoch"


def set_epoch_path(path: Optional[Path]) -> None:
    global _EPOCH_PATH
    _EPOCH_PATH = path


def load_epoch() -> str:
    """Stable id for this GPU install. Recreated when pasted-images is wiped."""
    path = epoch_path()
    with _LOCK:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            return text[:80]
        token = secrets.token_hex(16)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".epoch.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (token + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        return token


def inject_index_epoch(html: str, epoch: str | None = None) -> str:
    token = str(epoch if epoch is not None else load_epoch()).strip()[:80]
    payload = f"window.TABBY_UI_EPOCH = {json.dumps(token, ensure_ascii=True)};"
    if EPOCH_BOOT_MARK not in html:
        return html
    return html.replace(EPOCH_BOOT_MARK, payload, 1)
