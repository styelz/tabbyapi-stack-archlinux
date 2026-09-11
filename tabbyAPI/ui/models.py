"""Administrator model library: local weights, catalog picks, Hugging Face search."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

TABBY_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = TABBY_ROOT / "deploy" / "arch" / "models.json"
PROFILES_DIR = TABBY_ROOT / "model_profiles"
MODELS_DIR = TABBY_ROOT / "models"
JOB_PATH = PROFILES_DIR / "download_job.json"
FETCH_MODELS_PATH = TABBY_ROOT / "deploy" / "arch" / "fetch_models.py"

DISK_HEADROOM_BYTES = 2 * 1024 * 1024 * 1024
MAX_SEQ_CAP = 32768
SEARCH_LIMIT = 40
REVISION_SIZE_CAP = 10
BUSY_STATUSES = frozenset({"queued", "running", "cancelling"})
TERMINAL_STATUSES = frozenset({"done", "error", "cancelled"})
# Finished banners stay on the Models page until this TTL; after that the API
# omits the job so a leftover download_job.json cannot stick around all day.
JOB_DONE_TTL_SEC = 90
HF_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?huggingface\.co/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
REPO_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
QUANT_REV_RE = re.compile(r"(bpw|exl\d|exllama)", re.IGNORECASE)
ALIAS_RE = re.compile(r"^[a-z][a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
SHIPPED_PROFILE_ALIASES = frozenset(
    {"qwen", "qwen35", "qwen36", "gemma", "gemma26", "glm"}
)
RESERVED_ALIASES = frozenset(
    {
        "comfy",
        "flux",
        "image",
        "comfyui",
        "llm",
        "llama",
        "llamacpp",
        "gguf",
        "help",
        "restart",
        "list",
        "models",
        "generate",
        "embed",
        "qwen-image",
    }
) | SHIPPED_PROFILE_ALIASES
FORMAT_NEEDLES = {
    "exl3": ("exl3", "exllamav3"),
    "exl2": ("exl2", "exllamav2"),
    "gguf": ("gguf",),
}
ALL_EXL_NEEDLES = FORMAT_NEEDLES["exl3"] + FORMAT_NEEDLES["exl2"]

_JOB_LOCK = threading.Lock()
_CANCEL = threading.Event()
_THREAD: threading.Thread | None = None
_ACTIVE_PATHS: "ModelPaths | None" = None
_FETCH_MOD = None


class ModelsError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class DownloadCancelled(Exception):
    """Raised from tqdm when the administrator cancels a download."""


@dataclass
class ModelPaths:
    root: Path = TABBY_ROOT
    models_dir: Path = MODELS_DIR
    profiles_dir: Path = PROFILES_DIR
    catalog_path: Path = CATALOG_PATH
    job_path: Path = JOB_PATH
    comfy_dir: Path | None = None

    def resolved_comfy(self) -> Path:
        if self.comfy_dir is not None:
            return Path(self.comfy_dir)
        from common.gpu_mode import COMFY_DIR

        return Path(COMFY_DIR)


def default_paths() -> ModelPaths:
    return ModelPaths()


def fetch_models_mod():
    global _FETCH_MOD
    if _FETCH_MOD is None:
        spec = importlib.util.spec_from_file_location("tabby_fetch_models", FETCH_MODELS_PATH)
        if spec is None or spec.loader is None:
            raise ModelsError("fetch_models.py is missing", 500)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _FETCH_MOD = mod
    return _FETCH_MOD


def hf_token() -> str | None:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    token = (token or "").strip()
    return token or None


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return int(path.stat().st_size)
        except OSError:
            return 0
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                try:
                    total += int(child.stat().st_size)
                except OSError:
                    continue
    except OSError:
        return total
    return total


def disk_usage_for(path: Path) -> dict[str, int]:
    target = path if path.exists() else path.parent
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return {"free_bytes": 0, "total_bytes": 0, "used_bytes": 0}
    return {
        "free_bytes": int(usage.free),
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
    }


def sanitize_folder_name(raw: str) -> str:
    name = str(raw or "").strip()
    if not name or name in (".", "..") or any(ch in name for ch in "/\\\0"):
        raise ModelsError(f"Invalid folder name: {raw!r}")
    cleaned = []
    prev_dash = False
    for ch in name:
        if ch.isalnum() or ch in "._-":
            cleaned.append(ch)
            prev_dash = ch == "-"
        elif not prev_dash:
            cleaned.append("-")
            prev_dash = True
    out = "".join(cleaned).strip(".-")
    if not out or out in (".", ".."):
        raise ModelsError(f"Invalid folder name: {raw!r}")
    return out[:180]


def normalize_alias(raw: str) -> str:
    alias = str(raw or "").strip().lower()
    if alias.endswith(".yml"):
        alias = alias[:-4]
    if not ALIAS_RE.fullmatch(alias) or "--" in alias:
        raise ModelsError(
            "Short name must be 2–32 characters: start with a letter, then letters, "
            "digits, or hyphens (like qwen38)."
        )
    if alias in RESERVED_ALIASES:
        raise ModelsError(f"{alias} is reserved. Pick another short name.")
    return alias


def optional_alias(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    return normalize_alias(text)


def is_local_profile(alias: str, data: dict | None = None) -> bool:
    if isinstance(data, dict) and data.get("local"):
        return True
    return str(alias or "").strip().lower().startswith("hf-")


def hf_folder_name(repo_id: str, revision: str | None) -> str:
    name = sanitize_folder_name(str(repo_id).rsplit("/", 1)[-1])
    rev = str(revision or "").strip()
    if rev and rev.lower() not in ("main", "master"):
        name = f"{name}-{sanitize_folder_name(rev)}"
    return name


def parse_repo_id(raw: str) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    match = HF_URL_RE.search(text)
    if match:
        return match.group(1)
    trimmed = text.strip().strip("/")
    if trimmed.lower().startswith("huggingface.co/"):
        trimmed = trimmed.split("/", 1)[1]
    if REPO_ID_RE.match(trimmed):
        return trimmed
    return None


def _tag_blob(repo_id: str, tags: list[str] | tuple[str, ...] | None) -> str:
    parts = [str(repo_id or "")]
    parts.extend(str(tag) for tag in (tags or []))
    return " ".join(parts).lower()


def matches_format(repo_id: str, tags: list[str] | None, fmt: str) -> bool:
    needles = FORMAT_NEEDLES.get((fmt or "exl3").lower()) or FORMAT_NEEDLES["exl3"]
    blob = _tag_blob(repo_id, tags)
    return any(needle in blob for needle in needles)


def is_gguf_only(repo_id: str, tags: list[str] | None) -> bool:
    blob = _tag_blob(repo_id, tags)
    has_gguf = "gguf" in blob
    has_exl = any(needle in blob for needle in ALL_EXL_NEEDLES)
    return has_gguf and not has_exl


def is_exllama_compatible(repo_id: str, tags: list[str] | None) -> bool:
    blob = _tag_blob(repo_id, tags)
    if is_gguf_only(repo_id, tags):
        return False
    return any(needle in blob for needle in ALL_EXL_NEEDLES)


def is_gguf_compatible(repo_id: str, tags: list[str] | None) -> bool:
    return is_gguf_only(repo_id, tags) or "gguf" in _tag_blob(repo_id, tags)


def normalize_format(fmt: str | None) -> str:
    value = str(fmt or "exl3").strip().lower()
    return value if value in FORMAT_NEEDLES else "exl3"


def load_catalog(path: Path | None = None) -> dict:
    target = path or CATALOG_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _loaded_folder() -> str | None:
    try:
        from images.jobs import loaded_tabby_name

        name = loaded_tabby_name()
        return str(name) if name else None
    except Exception:
        return None


def read_job(paths: ModelPaths | None = None) -> dict | None:
    target = (paths or default_paths()).job_path
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_job(data: dict, paths: ModelPaths | None = None) -> dict:
    target = (paths or default_paths()).job_path
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["updated_at"] = iso_now()
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return payload


def patch_job(paths: ModelPaths | None = None, **fields: Any) -> dict | None:
    with _JOB_LOCK:
        current = read_job(paths) or {}
        if not current:
            return None
        current.update(fields)
        return write_job(current, paths)


def job_is_busy(job: dict | None) -> bool:
    return bool(job) and str(job.get("status") or "") in BUSY_STATUSES


def parse_job_time(job: dict | None) -> datetime | None:
    if not job:
        return None
    raw = str(job.get("updated_at") or job.get("started_at") or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def job_is_visible(job: dict | None, now: datetime | None = None) -> bool:
    if not job:
        return False
    if job_is_busy(job):
        return True
    if str(job.get("status") or "") not in TERMINAL_STATUSES:
        return True
    when = parse_job_time(job)
    if when is None:
        return False
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return (clock - when).total_seconds() < JOB_DONE_TTL_SEC


def public_job(job: dict | None, now: datetime | None = None) -> dict | None:
    return job if job_is_visible(job, now) else None


def recover_stale_job(paths: ModelPaths | None = None) -> dict | None:
    """Mark a running job as interrupted if this process is no longer downloading it."""
    job = read_job(paths)
    if not job_is_busy(job):
        return job
    alive = _THREAD is not None and _THREAD.is_alive()
    if alive:
        return job
    job["status"] = "error"
    job["error"] = (
        "Download interrupted (API restarted). Partial files were kept; you can try again."
    )
    job["message"] = job["error"]
    return write_job(job, paths)


def _hf_http_error_message(exc: BaseException) -> tuple[str, int, bool]:
    text = str(exc)
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    gated = status in (401, 403) or "401" in text or "403" in text or "gated" in text.lower()
    if gated:
        return (
            "This repo is gated. Set a Hugging Face token in Settings.",
            403,
            True,
        )
    if status == 404 or "404" in text:
        return ("Hugging Face repo not found.", 404, False)
    return (f"Hugging Face request failed: {text}", 502, False)


def _hf_api(token: str | None = None):
    from huggingface_hub import HfApi

    return HfApi(token=token if token is not None else hf_token())


def _list_models(api, query: str, fmt: str, limit: int) -> tuple[Any, bool]:
    """Search the Hub for EXL2/EXL3 repos. `direction` is gone in recent huggingface_hub."""
    kwargs: dict[str, Any] = {
        "search": query,
        "sort": "downloads",
        "limit": limit,
        "filter": fmt,
    }
    try:
        return api.list_models(**kwargs), True
    except TypeError:
        kwargs.pop("filter", None)
        try:
            return api.list_models(**kwargs), False
        except TypeError:
            kwargs.pop("limit", None)
            return api.list_models(**kwargs), False


def search_models(query: str, fmt: str = "exl3", hf_api=None, limit: int = SEARCH_LIMIT) -> dict:
    fmt = normalize_format(fmt)
    q = str(query or "").strip()
    exact = parse_repo_id(q)
    api = hf_api or _hf_api()
    results: list[dict] = []
    if exact:
        try:
            info = api.model_info(exact)
        except Exception as exc:
            message, status, gated = _hf_http_error_message(exc)
            raise ModelsError(message, status) from exc
        repo_id = str(getattr(info, "id", None) or exact)
        tags = list(getattr(info, "tags", None) or [])
        compatible = (
            is_gguf_compatible(repo_id, tags)
            if fmt == "gguf"
            else (is_exllama_compatible(repo_id, tags) and not is_gguf_only(repo_id, tags))
        )
        results.append(_model_card(info, repo_id, tags, compatible))
        return {
            "ok": True,
            "query": q,
            "format": fmt,
            "results": results,
            "has_token": bool(hf_token()),
        }

    if len(q) < 2:
        return {"ok": True, "query": q, "format": fmt, "results": [], "has_token": bool(hf_token())}

    try:
        listing, used_filter = _list_models(api, q, fmt, limit)
    except Exception as exc:
        message, status, _gated = _hf_http_error_message(exc)
        raise ModelsError(message, status) from exc

    for info in listing:
        repo_id = str(getattr(info, "id", None) or getattr(info, "modelId", None) or "")
        if not repo_id:
            continue
        tags = list(getattr(info, "tags", None) or [])
        if fmt != "gguf" and is_gguf_only(repo_id, tags):
            continue
        if fmt == "gguf" and not is_gguf_compatible(repo_id, tags):
            continue
        if not used_filter and not matches_format(repo_id, tags, fmt):
            continue
        results.append(_model_card(info, repo_id, tags, True))
        if len(results) >= 24:
            break
    return {
        "ok": True,
        "query": q,
        "format": fmt,
        "results": results,
        "has_token": bool(hf_token()),
    }


def _model_card(info: Any, repo_id: str, tags: list[str], compatible: bool) -> dict:
    likes = getattr(info, "likes", None)
    downloads = getattr(info, "downloads", None)
    modified = getattr(info, "last_modified", None) or getattr(info, "lastModified", None)
    if hasattr(modified, "isoformat"):
        modified = modified.isoformat()
    return {
        "id": repo_id,
        "tags": tags,
        "likes": int(likes or 0),
        "downloads": int(downloads or 0),
        "last_modified": str(modified or ""),
        "pipeline_tag": str(getattr(info, "pipeline_tag", None) or ""),
        "compatible": bool(compatible),
        "gated": bool(getattr(info, "gated", False)),
        "private": bool(getattr(info, "private", False)),
    }


def _is_quant_revision(name: str) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    if text.lower() in ("main", "master"):
        return True
    return bool(QUANT_REV_RE.search(text))


def _revision_size(api, repo_id: str, revision: str) -> tuple[int | None, int]:
    info = api.model_info(repo_id, revision=revision, files_metadata=True)
    siblings = list(getattr(info, "siblings", None) or [])
    total = 0
    known = False
    files = 0
    for sibling in siblings:
        name = str(getattr(sibling, "rfilename", None) or getattr(sibling, "path", None) or "")
        if not name or name.endswith("/"):
            continue
        files += 1
        size = getattr(sibling, "size", None)
        if size is not None:
            known = True
            total += int(size)
    return (total if known else None, files)


def gpu_vram_gb(vram_mib: int) -> int:
    return max(1, int(round(int(vram_mib) / 1024.0))) if vram_mib else 0


def revision_vram_fit(size_bytes: int | None, vram_mib: int, *, offload: bool = False) -> dict:
    """On-disk snapshot vs this GPU. File size stands in for resident weights."""
    need_mib = 0
    if size_bytes is not None and int(size_bytes) > 0:
        need_mib = int(round(int(size_bytes) / (1024 * 1024)))
    if vram_mib <= 0 or need_mib <= 0:
        return {"fits": None, "need_mib": need_mib, "vram_badge": ""}
    if need_mib > int(vram_mib):
        if offload:
            return {
                "fits": True,
                "need_mib": need_mib,
                "vram_badge": f"CPU offload on {gpu_vram_gb(vram_mib)} GB",
            }
        return {
            "fits": False,
            "need_mib": need_mib,
            "vram_badge": f"won't fit {gpu_vram_gb(vram_mib)} GB",
        }
    return {"fits": True, "need_mib": need_mib, "vram_badge": ""}


def inspect_repo(
    repo_id: str,
    revision: str | None = None,
    hf_api=None,
    gpu: dict | None = None,
) -> dict:
    parsed = parse_repo_id(repo_id)
    if not parsed:
        text = str(repo_id or "").strip()
        parsed = text if REPO_ID_RE.match(text) else None
    if not parsed:
        raise ModelsError("repo id is required")
    api = hf_api or _hf_api()
    if gpu is None:
        from common.switch_times import detect_gpu

        gpu = detect_gpu()
    vram_mib = int((gpu or {}).get("vram_mib") or 0)
    try:
        info = api.model_info(parsed, revision=revision or None)
        refs = api.list_repo_refs(parsed)
    except Exception as exc:
        message, status, _gated = _hf_http_error_message(exc)
        raise ModelsError(message, status) from exc

    tags = list(getattr(info, "tags", None) or [])
    branches = []
    for ref in list(getattr(refs, "branches", None) or []):
        name = str(getattr(ref, "name", None) or "")
        if name:
            branches.append(name)
    quant = [name for name in branches if _is_quant_revision(name)]
    chosen = list(quant)
    if "main" in branches and "main" not in chosen:
        chosen.insert(0, "main")
    if not chosen:
        chosen = branches[:REVISION_SIZE_CAP]
    if revision and revision not in chosen:
        chosen.insert(0, revision)
    chosen = chosen[:REVISION_SIZE_CAP]

    revisions = []
    for name in chosen:
        size_bytes = None
        files = 0
        try:
            size_bytes, files = _revision_size(api, parsed, name)
        except Exception:
            size_bytes, files = None, 0
        revisions.append(
            {
                "name": name,
                "size_bytes": size_bytes,
                "files": files,
                **revision_vram_fit(size_bytes, vram_mib, offload=is_gguf_only(parsed, tags)),
            }
        )

    gguf = is_gguf_only(parsed, tags)
    compatible = is_gguf_compatible(parsed, tags) if gguf else (
        is_exllama_compatible(parsed, tags) and not gguf
    )
    return {
        "ok": True,
        "id": parsed,
        "tags": tags,
        "compatible": compatible,
        "gguf_only": gguf,
        "gated": bool(getattr(info, "gated", False)),
        "private": bool(getattr(info, "private", False)),
        "revisions": revisions,
        "vram_mib": vram_mib,
        "vram_gb": gpu_vram_gb(vram_mib),
        "folder_name": hf_folder_name(parsed, revision or (chosen[0] if chosen else "main")),
        "has_token": bool(hf_token()),
    }


def catalog_pick_rows(paths: ModelPaths | None = None) -> list[dict]:
    p = paths or default_paths()
    fm = fetch_models_mod()
    catalog = load_catalog(p.catalog_path)
    tabby = p.root
    comfy = p.resolved_comfy()
    rows = []
    for pick in fm.catalog_picks(catalog):
        pick_id = str(pick.get("id") or "")
        if not pick_id:
            continue
        items = []
        ready_all = True
        any_present = False
        size = 0
        for item_id in [str(x) for x in (pick.get("items") or []) if x]:
            item = (catalog.get("items") or {}).get(item_id) or {}
            dest = fm.dest_path(item, tabby, comfy)
            ready = fm.is_ready(dest, item)
            present = dest.exists()
            ready_all = ready_all and ready
            any_present = any_present or present
            size += path_size(dest)
            items.append(
                {
                    "id": item_id,
                    "dest": str(dest),
                    "ready": ready,
                    "kind": item.get("kind") or "snapshot",
                }
            )
        kind = "image" if pick_id in ("flux", "qwen-image") else ("embed" if pick_id == "embed" else "llm")
        rows.append(
            {
                "id": pick_id,
                "label": pick.get("label") or pick_id,
                "kind": kind,
                "disk_gib": int(pick.get("disk_gib") or 0),
                "min_vram_mib": int(pick.get("min_vram_mib") or 0),
                "installed": ready_all and bool(items),
                "partial": any_present and not ready_all,
                "size_bytes": size,
                "items": items,
            }
        )
    return rows


def _profile_map(profiles_dir: Path) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    if not profiles_dir.is_dir():
        return mapping
    from ruamel.yaml import YAML

    yaml = YAML(typ="safe")
    for path in sorted(profiles_dir.glob("*.yml")):
        try:
            data = yaml.load(path.read_text(encoding="utf-8")) or {}
        except (OSError, Exception):
            continue
        if not isinstance(data, dict):
            continue
        alias = path.stem
        folder = str(((data.get("model") or {}) or {}).get("model_name") or "")
        pretty = str(data.get("pretty") or folder or alias)
        entry = {
            "alias": alias,
            "folder": folder,
            "pretty": pretty,
            "local": is_local_profile(alias, data),
            "path": str(path),
        }
        mapping[alias.lower()] = entry
        if folder:
            mapping[folder.lower()] = entry
    return mapping


def library_llms(paths: ModelPaths | None = None, loaded: str | None = None) -> list[dict]:
    p = paths or default_paths()
    from select_model import is_embedding_folder

    profiles = _profile_map(p.profiles_dir)
    catalog_folders: dict[str, str] = {}
    catalog = load_catalog(p.catalog_path)
    for item_id, item in (catalog.get("items") or {}).items():
        dest = str(item.get("dest") or "")
        if dest.startswith("tabby/models/"):
            catalog_folders[Path(dest).name] = str(item_id)
    rows = []
    if not p.models_dir.is_dir():
        return rows
    try:
        children = list(p.models_dir.iterdir())
    except OSError:
        return rows
    for child in sorted(children, key=lambda path: path.name.lower()):
        if not child.is_dir():
            continue
        gguf_files = list(child.glob("*.gguf"))
        if not (child / "config.json").is_file() and not gguf_files:
            continue
        entry = profiles.get(child.name.lower()) or {}
        embed = is_embedding_folder(child.name)
        rows.append(
            {
                "id": child.name,
                "kind": "embed" if embed else "llm",
                "label": entry.get("pretty") or child.name,
                "folder": child.name,
                "size_bytes": path_size(child),
                "profile": None if embed else (entry.get("alias") or None),
                "pretty": entry.get("pretty") or child.name,
                "loaded": (not embed) and loaded == child.name,
                "catalog_id": catalog_folders.get(child.name),
                "local_profile": bool(entry.get("local")),
            }
        )
    return rows


def library_images(paths: ModelPaths | None = None) -> list[dict]:
    rows = []
    for pick in catalog_pick_rows(paths):
        if pick["kind"] != "image":
            continue
        if not pick["installed"] and not pick["partial"]:
            continue
        rows.append(
            {
                "id": pick["id"],
                "kind": "image",
                "label": pick["label"],
                "folder": None,
                "size_bytes": pick["size_bytes"],
                "profile": None,
                "pretty": pick["label"],
                "loaded": False,
                "catalog_id": pick["id"],
                "installed": pick["installed"],
                "partial": pick["partial"],
                "items": pick["items"],
            }
        )
    return rows


def library_state(
    paths: ModelPaths | None = None,
    loaded: str | None = None,
) -> dict:
    p = paths or default_paths()
    recover_stale_job(p)
    loaded_name = loaded if loaded is not None else _loaded_folder()
    return {
        "ok": True,
        "llms": library_llms(p, loaded=loaded_name),
        "images": library_images(p),
        "catalog": catalog_pick_rows(p),
        "disk": disk_usage_for(p.models_dir),
        "loaded": loaded_name,
        "job": public_job(read_job(p)),
        "has_token": bool(hf_token()),
    }


def job_state(paths: ModelPaths | None = None) -> dict:
    p = paths or default_paths()
    recover_stale_job(p)
    return {"ok": True, "job": public_job(read_job(p))}


def _need_bytes(reported: int | None, disk_gib: int = 0) -> int:
    size = int(reported or 0)
    if size <= 0 and disk_gib > 0:
        size = int(disk_gib) * 1024 * 1024 * 1024
    return size + DISK_HEADROOM_BYTES


def _assert_disk(paths: ModelPaths, need: int) -> None:
    free = disk_usage_for(paths.models_dir)["free_bytes"]
    if need > DISK_HEADROOM_BYTES and free < need:
        raise ModelsError(
            f"Not enough disk space (need about {need / (1024 ** 3):.1f} GiB free, "
            f"have {free / (1024 ** 3):.1f} GiB).",
            409,
        )
    if free < DISK_HEADROOM_BYTES:
        raise ModelsError(
            f"Not enough disk space (need at least 2 GiB free, have {free / (1024 ** 3):.1f} GiB).",
            409,
        )


def start_download(
    body: dict,
    paths: ModelPaths | None = None,
    spawn: bool = True,
    hf_api=None,
) -> dict:
    p = paths or default_paths()
    kind = str((body or {}).get("kind") or "").strip().lower()
    if kind not in ("catalog", "hf"):
        raise ModelsError("kind must be catalog or hf")

    recover_stale_job(p)
    with _JOB_LOCK:
        if job_is_busy(read_job(p)):
            raise ModelsError("A download is already running", 409)

        if kind == "catalog":
            job = _prepare_catalog_job(body, p)
        else:
            job = _prepare_hf_job(body, p, hf_api=hf_api)
        write_job(job, p)

    global _ACTIVE_PATHS, _THREAD
    _ACTIVE_PATHS = p
    _CANCEL.clear()
    if spawn:
        thread = threading.Thread(
            target=_run_job, args=(job["id"],), daemon=True, name="tabby-model-dl"
        )
        _THREAD = thread
        thread.start()
    return {"ok": True, "job": job}


def _prepare_catalog_job(body: dict, paths: ModelPaths) -> dict:
    pick_id = str(body.get("pick_id") or body.get("id") or "").strip()
    if not pick_id:
        raise ModelsError("pick_id is required")
    rows = {row["id"]: row for row in catalog_pick_rows(paths)}
    pick = rows.get(pick_id)
    if not pick:
        raise ModelsError(f"Unknown catalog pick {pick_id!r}")
    if pick["installed"]:
        raise ModelsError(f"{pick['label']} is already installed", 409)
    _assert_disk(paths, _need_bytes(pick.get("size_bytes"), pick.get("disk_gib") or 0))
    dests = [item["dest"] for item in pick.get("items") or []]
    return {
        "id": str(uuid.uuid4()),
        "status": "queued",
        "kind": "catalog",
        "pick_id": pick_id,
        "label": pick["label"],
        "repo_id": None,
        "revision": None,
        "folder": None,
        "dests": dests,
        "percent": 0,
        "bytes_done": 0,
        "bytes_total": int(pick.get("size_bytes") or 0)
        or int(pick.get("disk_gib") or 0) * 1024 * 1024 * 1024,
        "file": "",
        "message": f"Queued {pick['label']}",
        "error": None,
        "started_at": iso_now(),
    }


def _prepare_hf_job(body: dict, paths: ModelPaths, hf_api=None) -> dict:
    repo_id = parse_repo_id(str(body.get("repo_id") or body.get("id") or ""))
    if not repo_id:
        raise ModelsError("repo_id is required")
    revision = str(body.get("revision") or "main").strip() or "main"
    folder = hf_folder_name(repo_id, revision)
    dest = (paths.models_dir / folder).resolve()
    models_root = paths.models_dir.resolve()
    if dest != models_root and models_root not in dest.parents:
        raise ModelsError("Download path is outside the models directory")
    fm = fetch_models_mod()
    fmt = str(body.get("format") or "").strip().lower()
    if not fmt:
        fmt = "gguf" if "gguf" in repo_id.lower() else "exl3"
    has_cfg = dest.exists() and (dest / "config.json").is_file()
    has_gguf = dest.exists() and any(dest.glob("*.gguf"))
    if dest.exists() and (has_cfg or has_gguf) and not fm.has_incomplete_downloads(dest):
        raise ModelsError(f"{folder} is already installed", 409)

    size_bytes = body.get("size_bytes")
    try:
        size_bytes = int(size_bytes) if size_bytes is not None else None
    except (TypeError, ValueError):
        size_bytes = None
    if size_bytes is None:
        try:
            api = hf_api or _hf_api()
            size_bytes, _files = _revision_size(api, repo_id, revision)
        except Exception:
            size_bytes = None
    _assert_disk(paths, _need_bytes(size_bytes))
    return {
        "id": str(uuid.uuid4()),
        "status": "queued",
        "kind": "hf",
        "pick_id": None,
        "label": f"{repo_id}@{revision}",
        "repo_id": repo_id,
        "revision": revision,
        "folder": folder,
        "alias": optional_alias(body.get("alias") or body.get("name") or ""),
        "format": fmt,
        "dests": [str(dest)],
        "percent": 0,
        "bytes_done": 0,
        "bytes_total": int(size_bytes or 0),
        "file": "",
        "message": f"Queued {repo_id}",
        "error": None,
        "started_at": iso_now(),
    }


def cancel_download(paths: ModelPaths | None = None) -> dict:
    p = paths or default_paths()
    job = read_job(p)
    if not job_is_busy(job):
        raise ModelsError("No download is running", 409)
    _CANCEL.set()
    patched = patch_job(p, status="cancelling", message="Cancelling…")
    return {"ok": True, "job": patched}


def _job_tqdm_class(paths: ModelPaths) -> Callable:
    from tqdm.auto import tqdm

    class JobTqdm(tqdm):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("disable", False)
            kwargs.setdefault("mininterval", 0.5)
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            if _CANCEL.is_set():
                raise DownloadCancelled("cancelled")
            result = super().update(n)
            total = int(self.total or 0)
            done = int(self.n or 0)
            percent = min(100, int(done * 100 / total)) if total else 0
            desc = str(self.desc or "").strip()
            patch_job(
                paths,
                percent=percent,
                bytes_done=done,
                bytes_total=total or (read_job(paths) or {}).get("bytes_total") or 0,
                file=desc,
                status="running",
            )
            return result

    return JobTqdm


def _run_job(job_id: str) -> None:
    paths = _ACTIVE_PATHS or default_paths()
    job = read_job(paths) or {}
    if str(job.get("id") or "") != job_id:
        return
    patch_job(paths, status="running", message="Starting download")
    try:
        if _CANCEL.is_set():
            raise DownloadCancelled("cancelled")
        if job.get("kind") == "catalog":
            _run_catalog_job(job, paths)
        else:
            _run_hf_job(job, paths)
        if _CANCEL.is_set():
            raise DownloadCancelled("cancelled")
        final = {
            "status": "done",
            "percent": 100,
            "message": "Download finished",
            "error": None,
            "file": "",
        }
        if job.get("kind") == "hf" and job.get("folder"):
            maybe_write_hf_profile(
                job["folder"],
                repo_id=job.get("repo_id") or "",
                revision=job.get("revision") or "",
                alias=job.get("alias") or None,
                paths=paths,
            )
            final["profile"] = profile_alias_for_folder(job["folder"], paths)
        patch_job(paths, **final)
    except DownloadCancelled:
        patch_job(
            paths,
            status="cancelled",
            message="Cancelled. Partial files were kept so you can resume.",
            error=None,
        )
    except ModelsError as exc:
        patch_job(paths, status="error", error=str(exc), message=str(exc))
    except SystemExit as exc:
        if _CANCEL.is_set() or "cancelled" in str(exc).lower():
            patch_job(
                paths,
                status="cancelled",
                message="Cancelled. Partial files were kept so you can resume.",
                error=None,
            )
        else:
            patch_job(paths, status="error", error=str(exc), message=str(exc))
    except Exception as exc:
        message, _status, _gated = _hf_http_error_message(exc)
        if "gated" in str(exc).lower() or "401" in str(exc) or "403" in str(exc):
            patch_job(paths, status="error", error=message, message=message)
        else:
            patch_job(paths, status="error", error=str(exc) or "Download failed", message=str(exc))


def _run_catalog_job(job: dict, paths: ModelPaths) -> None:
    fm = fetch_models_mod()
    catalog = load_catalog(paths.catalog_path)
    pick_id = str(job.get("pick_id") or "")
    picks = {str(row["id"]): row for row in fm.catalog_picks(catalog)}
    pick = picks.get(pick_id) or {}
    item_ids = [str(x) for x in (pick.get("items") or []) if x]
    items = catalog.get("items") or {}
    progress_cls = _job_tqdm_class(paths)
    for index, item_id in enumerate(item_ids, start=1):
        if _CANCEL.is_set():
            raise DownloadCancelled("cancelled")
        item = items.get(item_id)
        if not item:
            raise ModelsError(f"Catalog item {item_id!r} is missing")
        patch_job(
            paths,
            message=f"Downloading {item_id} ({index}/{len(item_ids)})",
            file=item_id,
        )
        fm.ensure_item(
            item_id,
            item,
            paths.root,
            paths.resolved_comfy(),
            None,
            tqdm_class=progress_cls,
        )


def _run_hf_job(job: dict, paths: ModelPaths) -> None:
    fm = fetch_models_mod()
    repo_id = str(job.get("repo_id") or "")
    revision = str(job.get("revision") or "main")
    folder = str(job.get("folder") or hf_folder_name(repo_id, revision))
    dest = paths.models_dir / folder
    dest.parent.mkdir(parents=True, exist_ok=True)
    item = {
        "kind": "snapshot",
        "repo": repo_id,
        "revision": revision,
        "dest": f"tabby/models/{folder}",
        "ready": ["config.json"] if job.get("format") != "gguf" else [],
    }
    patch_job(paths, message=f"Downloading {repo_id}", file=repo_id)
    fm.download_item(item, dest, tqdm_class=_job_tqdm_class(paths))
    has_cfg = (dest / "config.json").is_file()
    has_gguf = any(dest.glob("*.gguf"))
    if not has_cfg and not has_gguf:
        raise ModelsError(
            "Download finished but the folder has no config.json or .gguf file."
        )


def profile_slug(folder_name: str) -> str:
    raw = str(folder_name or "").lower()
    out: list[str] = []
    prev_dash = False
    for ch in raw:
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")[:40] or "model"
    return slug


def profile_alias_for_folder(folder: str, paths: ModelPaths | None = None) -> str | None:
    mapping = _profile_map((paths or default_paths()).profiles_dir)
    entry = mapping.get(str(folder).lower())
    return entry.get("alias") if entry else None


def profile_defaults_from_config(
    folder: Path,
    pretty: str | None = None,
    *,
    vram_mib: int | None = None,
    gpu: dict | None = None,
) -> dict:
    from common.switch_times import detect_gpu
    from common.vision_defaults import (
        decide_vision,
        gpu_size_label,
        parse_param_billions,
        pretty_with_vision_note,
        weight_mib,
    )

    max_seq = MAX_SEQ_CAP
    capable = False
    ggufs = list(folder.glob("*.gguf")) if folder.is_dir() else []
    cfg_path = folder / "config.json"
    if not cfg_path.is_file() and ggufs:
        pretty_name = pretty or folder.name
        mmproj = next((p for p in ggufs if "mmproj" in p.name.lower()), None)
        weights = [p for p in ggufs if p != mmproj]
        model_file = weights[0].name if len(weights) == 1 else folder.name
        model = {
            "backend": "llamacpp",
            "model_name": model_file,
            "n_gpu_layers": -1,
            "max_seq_len": max_seq,
            "vision": bool(mmproj),
        }
        if mmproj:
            model["mmproj"] = mmproj.name
        return {
            "pretty": pretty_name,
            "model": model,
            "sampling": {"override_preset": "safe_defaults"},
        }
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            raw = (
                data.get("max_position_embeddings")
                or data.get("max_seq_len")
                or (data.get("text_config") or {}).get("max_position_embeddings")
            )
            try:
                parsed = int(raw)
            except (TypeError, ValueError):
                parsed = 0
            if parsed > 0:
                max_seq = min(parsed, MAX_SEQ_CAP)
            model_type = str(data.get("model_type") or "").lower()
            if data.get("vision_config") or "vision" in model_type or "vl" in model_type:
                capable = True
    resolved_gpu = gpu
    if vram_mib is None:
        resolved_gpu = detect_gpu()
        vram_mib = int(resolved_gpu.get("vram_mib") or 0)
    choice = decide_vision(
        capable=capable,
        vram_mib=int(vram_mib or 0),
        params_b=parse_param_billions(folder.name),
        weight_mib=weight_mib(folder),
    )
    pretty_name = pretty or folder.name
    if capable and not choice["vision"]:
        pretty_name = pretty_with_vision_note(
            pretty_name, False, gpu_size_label(int(vram_mib or 0), resolved_gpu)
        )
    model = {
        "model_name": folder.name,
        "max_seq_len": max_seq,
        "cache_size": max_seq,
        "cache_mode": "Q4",
        "chunk_size": 4096,
        "max_batch_size": 1,
        "autosplit_reserve": [384],
        "vision": choice["vision"],
    }
    if choice["vision_offload"]:
        model["vision_offload"] = True
    _apply_family_model_defaults(folder.name, model)
    data = {
        "pretty": pretty_name,
        "model": model,
        "sampling": {"override_preset": "safe_defaults"},
    }
    if _thinking_only_folder(folder.name):
        data["thinking_only"] = True
    return data


def _thinking_only_folder(folder_name: str) -> bool:
    name = str(folder_name or "").lower()
    if "glm" not in name:
        return False
    return "thinking" in name or "4.1" in name or "41v" in name


def _apply_family_model_defaults(folder_name: str, model: dict) -> None:
    """Fill shipped-profile tokens that a Hugging Face download would omit."""
    if not _thinking_only_folder(folder_name):
        return
    model.setdefault("reasoning", True)
    model.setdefault("reasoning_start_token", "<think>")
    model.setdefault("reasoning_end_token", "</think>")
    model.setdefault("answer_start_token", "<answer>")
    model.setdefault("answer_end_token", "</answer>")
    model.setdefault("start_in_reasoning", "always")


def _write_profile_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle)


def _load_profile_yaml(path: Path) -> dict:
    from ruamel.yaml import YAML

    yaml = YAML(typ="safe")
    try:
        data = yaml.load(path.read_text(encoding="utf-8")) or {}
    except (OSError, Exception):
        return {}
    return data if isinstance(data, dict) else {}


def _alias_taken_by_other(alias: str, folder: str, paths: ModelPaths) -> str | None:
    dest = paths.profiles_dir / f"{alias}.yml"
    if not dest.is_file():
        return None
    data = _load_profile_yaml(dest)
    other = str(((data.get("model") or {}) or {}).get("model_name") or "")
    if other and other.lower() != str(folder).lower():
        return other
    return None


def maybe_write_hf_profile(
    folder: str,
    repo_id: str = "",
    revision: str = "",
    alias: str | None = None,
    paths: ModelPaths | None = None,
) -> str | None:
    p = paths or default_paths()
    wanted = optional_alias(alias) if alias else None
    existing = profile_alias_for_folder(folder, p)
    if wanted:
        taken = _alias_taken_by_other(wanted, folder, p)
        if taken:
            raise ModelsError(f"{wanted} is already used by {taken}")
        if existing == wanted:
            return wanted
    elif existing:
        return existing
    if wanted:
        stem = wanted
        dest = p.profiles_dir / f"{stem}.yml"
    else:
        slug = profile_slug(folder)
        stem = f"hf-{slug}"
        dest = p.profiles_dir / f"{stem}.yml"
        n = 2
        while dest.exists():
            stem = f"hf-{slug}-{n}"
            dest = p.profiles_dir / f"{stem}.yml"
            n += 1
            if n > 50:
                raise ModelsError("Could not allocate a profile name")
    pretty = " ".join(part for part in (repo_id, revision) if part).strip() or folder
    data = profile_defaults_from_config(p.models_dir / folder, pretty=pretty)
    data["local"] = True
    _write_profile_yaml(dest, data)
    return stem


def _local_profiles_for_folder(folder: str, paths: ModelPaths) -> list[Path]:
    found = []
    mapping = _profile_map(paths.profiles_dir)
    entry = mapping.get(str(folder).lower()) or {}
    alias = str(entry.get("alias") or "")
    if alias and is_local_profile(alias, _load_profile_yaml(paths.profiles_dir / f"{alias}.yml")):
        path = paths.profiles_dir / f"{alias}.yml"
        if path.is_file():
            found.append(path)
    return found


def _patch_json_profile(path: Path, old: str, new: str, key: str = "profile") -> None:
    if not path.is_file() or not old or old == new:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    if str(data.get(key) or "") != old:
        return
    data[key] = new
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def retarget_profile_refs(old: str, new: str, paths: ModelPaths | None = None) -> None:
    p = paths or default_paths()
    if not old or old == new:
        return
    _patch_json_profile(p.profiles_dir / "last.json", old, new)
    _patch_json_profile(p.profiles_dir / "gpu_mode.json", old, new)
    times_path = p.profiles_dir / "switch_times.local.json"
    if not times_path.is_file():
        return
    try:
        data = json.loads(times_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict) or old not in data:
        return
    if new not in data:
        data[new] = data[old]
    del data[old]
    tmp = times_path.with_name(times_path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(times_path)


def set_profile_alias(body: dict, paths: ModelPaths | None = None) -> dict:
    p = paths or default_paths()
    wanted = normalize_alias((body or {}).get("alias") or (body or {}).get("name") or "")
    folder = str((body or {}).get("folder") or (body or {}).get("id") or "").strip()
    current = str((body or {}).get("profile") or "").strip()
    if folder:
        folder = sanitize_folder_name(folder)
    elif current:
        entry = _profile_map(p.profiles_dir).get(current.lower()) or {}
        folder = str(entry.get("folder") or "")
        if not folder:
            raise ModelsError(f"Unknown profile {current!r}", 404)
    else:
        raise ModelsError("folder or profile is required")

    dest_folder = (p.models_dir / folder).resolve()
    models_root = p.models_dir.resolve()
    if dest_folder != models_root and models_root not in dest_folder.parents:
        raise ModelsError("Path is outside the models directory")
    if not dest_folder.is_dir():
        raise ModelsError(f"{folder} is not installed", 404)

    existing = profile_alias_for_folder(folder, p)
    if existing == wanted:
        return {"ok": True, "alias": wanted, "folder": folder, "unchanged": True}
    if existing:
        existing_data = _load_profile_yaml(p.profiles_dir / f"{existing}.yml")
        if not is_local_profile(existing, existing_data):
            raise ModelsError(
                "Catalog models keep their built-in names (qwen, qwen35, qwen36, …)."
            )
    taken = _alias_taken_by_other(wanted, folder, p)
    if taken:
        raise ModelsError(f"{wanted} is already used by {taken}")

    dest = p.profiles_dir / f"{wanted}.yml"
    if existing:
        src = p.profiles_dir / f"{existing}.yml"
        data = _load_profile_yaml(src) if src.is_file() else {}
        if not data:
            data = profile_defaults_from_config(dest_folder, pretty=folder)
        data["local"] = True
        model_cfg = data.get("model")
        if not isinstance(model_cfg, dict):
            model_cfg = {}
            data["model"] = model_cfg
        model_cfg["model_name"] = folder
        _write_profile_yaml(dest, data)
        if src.is_file() and src.resolve() != dest.resolve():
            src.unlink(missing_ok=True)
        retarget_profile_refs(existing, wanted, p)
    else:
        maybe_write_hf_profile(folder, alias=wanted, paths=p)
    return {"ok": True, "alias": wanted, "folder": folder}


def delete_model(
    body: dict,
    paths: ModelPaths | None = None,
    loaded: str | None = None,
) -> dict:
    p = paths or default_paths()
    kind = str((body or {}).get("kind") or "").strip().lower()
    ident = str((body or {}).get("id") or body.get("folder") or "").strip()
    if kind not in ("llm", "image", "embed"):
        raise ModelsError("kind must be llm or image")
    if not ident:
        raise ModelsError("id is required")

    recover_stale_job(p)
    job = read_job(p)
    if job_is_busy(job):
        dests = [Path(x).resolve() for x in (job.get("dests") or []) if x]
        if kind in ("llm", "embed"):
            target = (p.models_dir / ident).resolve()
            if any(
                dest == target or target in dest.parents or dest in target.parents
                for dest in dests
            ):
                raise ModelsError("Cannot delete a model while it is downloading", 409)
        else:
            if ident == job.get("pick_id"):
                raise ModelsError("Cannot delete a model while it is downloading", 409)

    loaded_name = loaded if loaded is not None else _loaded_folder()
    if kind in ("llm", "embed"):
        folder = sanitize_folder_name(ident)
        dest = (p.models_dir / folder).resolve()
        models_root = p.models_dir.resolve()
        if dest != models_root and models_root not in dest.parents:
            raise ModelsError("Path is outside the models directory")
        if not dest.exists():
            raise ModelsError(f"{folder} is not installed", 404)
        if loaded_name == folder:
            raise ModelsError("Unload this model before deleting it", 409)
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
        removed = []
        for profile in _local_profiles_for_folder(folder, p):
            profile.unlink(missing_ok=True)
            removed.append(profile.stem)
        return {"ok": True, "deleted": folder, "profiles": removed}

    rows = {row["id"]: row for row in catalog_pick_rows(p)}
    pick = rows.get(ident)
    if not pick or pick["kind"] != "image":
        raise ModelsError(f"Unknown image pick {ident!r}")
    deleted = []
    for item in pick.get("items") or []:
        dest = Path(item["dest"])
        if dest.is_file():
            dest.unlink()
            deleted.append(str(dest))
        elif dest.is_dir():
            shutil.rmtree(dest)
            deleted.append(str(dest))
    return {"ok": True, "deleted": ident, "files": deleted}
