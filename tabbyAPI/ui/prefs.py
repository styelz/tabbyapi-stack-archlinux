"""Per-user console UI prefs stored next to chat history."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

_LOCK = threading.Lock()
_PREFS_DIR: Optional[Path] = None
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
PREFS_BOOT_MARK = "window.TABBY_UI_PREFS = null;"

THEMES = frozenset({"midnight", "ember", "glacier", "moss", "contrast"})
MODES = frozenset({"dark", "light", "system"})
AGENTS = frozenset({"agent", "ask", "plan"})
SAMPLER_KEYS = (
    "temperature",
    "top_p",
    "min_p",
    "frequency_penalty",
    "presence_penalty",
    "max_tokens",
)
SAMPLER_RANGE = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "min_p": (0.0, 1.0),
    "frequency_penalty": (-2.0, 2.0),
    "presence_penalty": (-2.0, 2.0),
    "max_tokens": (16.0, 32768.0),
}
ZOOM_MIN = 75
ZOOM_MAX = 150
ZOOM_STEP = 5
FILES_FR_MIN = 0.15
FILES_FR_MAX = 20.0
SIDEBAR_W_MIN, SIDEBAR_W_MAX, SIDEBAR_W_DEFAULT = 180, 520, 268
FILES_W_MIN, FILES_W_MAX, FILES_W_DEFAULT = 160, 560, 250
PREVIEW_W_MIN, PREVIEW_W_MAX, PREVIEW_W_DEFAULT = 22, 78, 42
SPLIT_W_MIN, SPLIT_W_MAX, SPLIT_W_DEFAULT = 22, 78, 50
TERM_H_MIN, TERM_H_DEFAULT = 80, 220
COMPOSE_H_MIN = 56
MAX_WS_OPEN = 200
MAX_FOLDERS = 50
FOLDER_LEN = 80

EMPTY_LAYOUT = {
    "sidebarHidden": False,
    "sidebarW": SIDEBAR_W_DEFAULT,
    "filesOpen": True,
    "filesW": FILES_W_DEFAULT,
    "previewW": PREVIEW_W_DEFAULT,
    "splitW": SPLIT_W_DEFAULT,
    "termH": TERM_H_DEFAULT,
    "composeH": 0,
    "filesFr": [2.0, 1.0, 1.0, 1.0],
    "historyOpen": True,
    "changesOpen": True,
    "gitOpen": False,
}

EMPTY_PREFS = {
    "version": 1,
    "theme": "midnight",
    "mode": "dark",
    "zoom": 100,
    "codeAgent": "agent",
    "samplers": {key: None for key in SAMPLER_KEYS},
    "layout": dict(EMPTY_LAYOUT),
    "wsOpen": {},
    "extraFolders": [],
}


def prefs_dir() -> Path:
    if _PREFS_DIR is not None:
        return _PREFS_DIR
    from common.gpu_mode import GENERATED_DIR

    path = GENERATED_DIR / "ui_prefs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def set_prefs_dir(path: Optional[Path]) -> None:
    global _PREFS_DIR
    _PREFS_DIR = path


def _safe_name(username: str) -> str:
    name = SAFE_NAME_RE.sub("_", str(username or "").strip()) or "user"
    return name[:80]


def prefs_path(username: str) -> Path:
    return prefs_dir() / f"{_safe_name(username)}.json"


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _clamp_zoom(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 100
    stepped = int(round(n / ZOOM_STEP) * ZOOM_STEP)
    return max(ZOOM_MIN, min(ZOOM_MAX, stepped))


def _clamp_float(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if n != n:  # NaN
        return default
    return max(lo, min(hi, n))


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1, "0", "1", "true", "false", "True", "False"):
        return value in (True, 1, "1", "true", "True")
    return default


def _sampler_value(key: str, raw: Any) -> Optional[float]:
    if raw is None:
        return None
    lo, hi = SAMPLER_RANGE[key]
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if n != n:
        return None
    if key == "max_tokens":
        return float(_clamp_int(n, int(lo), int(hi), int(lo)))
    return max(lo, min(hi, n))


def _normalize_layout(raw: Any) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    files_fr = src.get("filesFr")
    if isinstance(files_fr, str):
        parts = [item.strip() for item in files_fr.split(",") if item.strip()]
        try:
            files_fr = [float(item) for item in parts]
        except (TypeError, ValueError):
            files_fr = None
    if not isinstance(files_fr, list) or len(files_fr) not in (3, 4):
        ratios = list(EMPTY_LAYOUT["filesFr"])
    else:
        nums = [
            _clamp_float(item, FILES_FR_MIN, FILES_FR_MAX, EMPTY_LAYOUT["filesFr"][i if i < 4 else 3])
            for i, item in enumerate(files_fr[:4])
        ]
        if len(nums) == 3:
            nums = [nums[0], 1.0, nums[1], nums[2]]
        ratios = nums
    compose = src.get("composeH")
    try:
        compose_n = int(compose)
    except (TypeError, ValueError):
        compose_n = 0
    if compose_n < 0:
        compose_n = 0
    elif compose_n > 0:
        compose_n = max(COMPOSE_H_MIN, min(800, compose_n))
    return {
        "sidebarHidden": _bool(src.get("sidebarHidden"), False),
        "sidebarW": _clamp_int(src.get("sidebarW"), SIDEBAR_W_MIN, SIDEBAR_W_MAX, SIDEBAR_W_DEFAULT),
        "filesOpen": _bool(src.get("filesOpen"), True),
        "filesW": _clamp_int(src.get("filesW"), FILES_W_MIN, FILES_W_MAX, FILES_W_DEFAULT),
        "previewW": _clamp_int(src.get("previewW"), PREVIEW_W_MIN, PREVIEW_W_MAX, PREVIEW_W_DEFAULT),
        "splitW": _clamp_int(src.get("splitW"), SPLIT_W_MIN, SPLIT_W_MAX, SPLIT_W_DEFAULT),
        "termH": _clamp_int(src.get("termH"), TERM_H_MIN, 800, TERM_H_DEFAULT),
        "composeH": compose_n,
        "filesFr": ratios,
        "historyOpen": _bool(src.get("historyOpen"), True),
        "changesOpen": _bool(src.get("changesOpen"), True),
        "gitOpen": _bool(src.get("gitOpen"), False),
    }


def _normalize_samplers(raw: Any) -> dict[str, Optional[float]]:
    src = raw if isinstance(raw, dict) else {}
    return {key: _sampler_value(key, src.get(key)) for key in SAMPLER_KEYS}


def _normalize_ws_open(raw: Any) -> dict[str, bool]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, bool] = {}
    for key, value in raw.items():
        name = str(key or "").strip()[:80]
        if not name:
            continue
        out[name] = bool(value)
        if len(out) >= MAX_WS_OPEN:
            break
    return out


def _normalize_folders(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        name = str(item or "").strip()[:FOLDER_LEN]
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= MAX_FOLDERS:
            break
    return out


def normalize_prefs(raw: Any) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    theme = str(src.get("theme") or "").strip().lower()
    if theme not in THEMES:
        theme = "midnight"
    mode = str(src.get("mode") or "").strip().lower()
    if mode not in MODES:
        mode = "dark"
    agent = str(src.get("codeAgent") or "").strip().lower()
    if agent not in AGENTS:
        agent = "agent"
    try:
        version = int(src.get("version") or 1)
    except (TypeError, ValueError):
        version = 1
    return {
        "version": version or 1,
        "theme": theme,
        "mode": mode,
        "zoom": _clamp_zoom(src.get("zoom")),
        "codeAgent": agent,
        "samplers": _normalize_samplers(src.get("samplers")),
        "layout": _normalize_layout(src.get("layout")),
        "wsOpen": _normalize_ws_open(src.get("wsOpen")),
        "extraFolders": _normalize_folders(src.get("extraFolders")),
    }


def _read_disk(username: str) -> Any:
    path = prefs_path(username)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_prefs(username: str) -> dict[str, Any]:
    with _LOCK:
        raw = _read_disk(username)
    if raw is None:
        return normalize_prefs(None)
    return normalize_prefs(raw)


def save_prefs(username: str, raw: Any) -> dict[str, Any]:
    prefs = normalize_prefs(raw)
    payload = json.dumps(prefs, ensure_ascii=False) + "\n"
    with _LOCK:
        _atomic_write(prefs_path(username), payload)
    return prefs


def delete_prefs(username: str) -> None:
    path = prefs_path(username)
    with _LOCK:
        try:
            path.unlink()
        except OSError:
            pass


def prefs_js_literal(prefs: dict[str, Any]) -> str:
    return (
        json.dumps(normalize_prefs(prefs), ensure_ascii=True, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def inject_index_prefs(html: str, prefs: dict[str, Any]) -> str:
    payload = f"window.TABBY_UI_PREFS = {prefs_js_literal(prefs)};"
    if PREFS_BOOT_MARK not in html:
        return html
    return html.replace(PREFS_BOOT_MARK, payload, 1)


def index_page_html(username: str) -> str:
    from ui.epoch import inject_index_epoch

    html = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    html = inject_index_prefs(html, load_prefs(username))
    return inject_index_epoch(html)
