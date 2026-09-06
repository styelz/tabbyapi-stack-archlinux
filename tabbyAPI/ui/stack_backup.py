"""Host-level backup and restore for model weights and optional stack data."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

FORMAT = "tabbyapi-stack-backup"
VERSION = 1
ProgressFn = Optional[Callable[[str], None]]

TABBY_ROOT = Path(__file__).resolve().parent.parent
STACK_ROOT = TABBY_ROOT.parent
COMFY_ROOT = STACK_ROOT / "ComfyUI"
CATALOG_PATH = TABBY_ROOT / "deploy" / "arch" / "models.json"

INCOMPLETE_SUFFIXES = (".part", ".partial", ".incomplete", ".tmp")
EXTRA_GROUPS = ("config", "users", "chats")
ALL_GROUPS = ("models", *EXTRA_GROUPS)


class StackBackupError(ValueError):
    pass


def format_bytes(value: int) -> str:
    size = float(max(0, int(value or 0)))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{int(value)} B"


def _emit(on_progress: ProgressFn, message: str) -> None:
    if on_progress:
        on_progress(str(message))


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except OSError as exc:
        raise StackBackupError(f"Cannot resolve path: {path}") from exc


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_external(path: Path, *, label: str) -> Path:
    resolved = _resolved(path)
    install = _resolved(STACK_ROOT)
    if resolved == install or _inside(resolved, install):
        raise StackBackupError(f"{label} must be outside the stack install: {install}")
    return resolved


def _catalog() -> dict[str, Any]:
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StackBackupError(f"Cannot read model catalog: {CATALOG_PATH}") from exc
    return data if isinstance(data, dict) else {}


def _is_incomplete(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(INCOMPLETE_SUFFIXES) or ".part." in name


def _regular_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [
            name
            for name in dirs
            if not (Path(base) / name).is_symlink()
            and name not in {".cache", "__pycache__"}
        ]
        for name in sorted(files):
            path = Path(base) / name
            if path.is_file() and not path.is_symlink() and not _is_incomplete(path):
                yield path


def _installed_llm_dirs() -> list[Path]:
    root = TABBY_ROOT / "models"
    if not root.is_dir():
        return []
    installed: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        ready = (child / "config.json").is_file() or any(child.glob("*.safetensors"))
        if ready:
            installed.append(child)
    return installed


def _model_roots() -> list[tuple[Path, Path]]:
    """Return (source, backup-relative destination) for installed model roots."""
    roots = [(path, Path("tabbyAPI/models") / path.name) for path in _installed_llm_dirs()]
    seen: set[Path] = set()
    for item in (_catalog().get("items") or {}).values():
        if not isinstance(item, dict):
            continue
        rel = str(item.get("dest") or "")
        if not rel.startswith("comfy/"):
            continue
        source = COMFY_ROOT / rel[len("comfy/") :]
        if not source.exists() or source.is_symlink():
            continue
        source_resolved = _resolved(source)
        if source_resolved in seen:
            continue
        seen.add(source_resolved)
        roots.append((source, Path("ComfyUI") / rel[len("comfy/") :]))
    return roots


def _extra_roots(groups: set[str]) -> list[tuple[Path, Path, str]]:
    roots: list[tuple[Path, Path, str]] = []
    if "config" in groups:
        for rel in (
            Path("config.yml"),
            Path("deploy/arch/tabby.env"),
            Path("model_profiles/last.json"),
        ):
            roots.append((TABBY_ROOT / rel, Path("extras/tabbyAPI") / rel, "config"))
    if "users" in groups:
        for rel in (Path("ui_users.json"), Path("api_tokens.yml")):
            roots.append((TABBY_ROOT / rel, Path("extras/tabbyAPI") / rel, "users"))
    if "chats" in groups:
        pasted = TABBY_ROOT / "pasted-images"
        for rel in (
            Path("ui_chats"),
            Path("ui_prefs"),
            Path("ui_workspaces"),
            Path("thumbs"),
            Path("gallery_owners.json"),
        ):
            roots.append((pasted / rel, Path("extras/tabbyAPI/pasted-images") / rel, "chats"))
        if pasted.is_dir():
            for image in sorted(pasted.glob("generated-*.png")):
                roots.append(
                    (image, Path("extras/tabbyAPI/pasted-images") / image.name, "chats")
                )
    return roots


def _add_root(
    jobs: list[dict[str, Any]],
    source: Path,
    relative: Path,
    group: str,
) -> None:
    if source.is_file() and not source.is_symlink() and not _is_incomplete(source):
        jobs.append(
            {"source": source, "relative": relative, "group": group, "bytes": source.stat().st_size}
        )
        return
    if source.is_dir() and not source.is_symlink():
        for path in _regular_files(source):
            jobs.append(
                {
                    "source": path,
                    "relative": relative / path.relative_to(source),
                    "group": group,
                    "bytes": path.stat().st_size,
                }
            )


def _same_file(source: Path, destination: Path) -> bool:
    if not destination.is_file():
        return False
    try:
        src = source.stat()
        dst = destination.stat()
        return src.st_size == dst.st_size and int(src.st_mtime) == int(dst.st_mtime)
    except OSError:
        return False


def _disk_free(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return int(shutil.disk_usage(probe).free)
    except OSError as exc:
        raise StackBackupError(f"Cannot check free space for {path}") from exc


def _require_writable_destination(path: Path) -> None:
    if path.exists() and not path.is_dir():
        raise StackBackupError(f"Backup destination is not a directory: {path}")
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.is_dir() or not os.access(probe, os.W_OK | os.X_OK):
        raise StackBackupError(f"Backup destination is not writable: {path}")


def plan_backup(
    destination: str | Path,
    *,
    include_config: bool = False,
    include_users: bool = False,
    include_chats: bool = False,
) -> dict[str, Any]:
    dest = _validate_external(Path(destination), label="Backup destination")
    _require_writable_destination(dest)
    groups = {
        name
        for name, enabled in (
            ("config", include_config),
            ("users", include_users),
            ("chats", include_chats),
        )
        if enabled
    }
    jobs: list[dict[str, Any]] = []
    for source, relative in _model_roots():
        _add_root(jobs, source, relative, "models")
    for source, relative, group in _extra_roots(groups):
        _add_root(jobs, source, relative, group)
    jobs.sort(key=lambda item: str(item["relative"]))

    totals = {group: 0 for group in ALL_GROUPS}
    existing = {group: 0 for group in ALL_GROUPS}
    for job in jobs:
        group = job["group"]
        totals[group] += int(job["bytes"])
        if _same_file(job["source"], dest / job["relative"]):
            existing[group] += int(job["bytes"])
    total = sum(totals.values())
    already = sum(existing.values())
    needed = total - already
    free = _disk_free(dest)
    return {
        "format": FORMAT,
        "version": VERSION,
        "destination": str(dest),
        "groups": ["models", *sorted(groups)],
        "totals": totals,
        "existing": existing,
        "bytes": total,
        "already_bytes": already,
        "needed_bytes": needed,
        "free_bytes": free,
        "enough_space": free >= needed,
        "files": len(jobs),
        "_jobs": jobs,
    }


def public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if not key.startswith("_")}


def summary_lines(plan: dict[str, Any]) -> list[str]:
    lines = [
        f"Destination: {plan['destination']}",
        f"Files: {plan['files']}",
    ]
    for group in ALL_GROUPS:
        size = int((plan.get("totals") or {}).get(group) or 0)
        if size or group in (plan.get("groups") or []):
            lines.append(f"{group.capitalize()}: {format_bytes(size)}")
    lines.extend(
        (
            f"To copy: {format_bytes(plan['needed_bytes'])}",
            f"Free space: {format_bytes(plan['free_bytes'])}",
        )
    )
    return lines


def _manifest(plan: dict[str, Any], *, complete: bool) -> dict[str, Any]:
    return {
        **public_plan(plan),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "complete": complete,
        "items": [
            {
                "path": str(job["relative"]),
                "group": job["group"],
                "bytes": job["bytes"],
            }
            for job in plan["_jobs"]
        ],
    }


def _write_manifest(destination: Path, data: dict[str, Any]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "manifest.json"
    tmp = destination / ".manifest.json.part"
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _copy_one(source: Path, destination: Path, on_progress: ProgressFn = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    rsync = shutil.which("rsync")
    if rsync:
        process = subprocess.Popen(
            [
                rsync,
                "-a",
                "--partial",
                "--no-links",
                "--human-readable",
                "--info=progress2",
                "--",
                str(source),
                str(destination),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        pending = ""
        assert process.stdout is not None
        while True:
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            pending += chunk.decode("utf-8", errors="replace")
            parts = pending.replace("\r", "\n").split("\n")
            pending = parts.pop()
            for line in parts:
                if line.strip():
                    _emit(on_progress, line.strip())
        if pending.strip():
            _emit(on_progress, pending.strip())
        returncode = process.wait()
        if returncode:
            raise StackBackupError(f"rsync failed ({returncode}): {source}")
    else:
        tmp = destination.with_name(f".{destination.name}.part")
        shutil.copy2(source, tmp)
        if tmp.stat().st_size != source.stat().st_size:
            raise StackBackupError(f"Incomplete copy: {source}")
        tmp.replace(destination)
    if not destination.is_file() or destination.stat().st_size != source.stat().st_size:
        raise StackBackupError(f"Incomplete copy: {source} -> {destination}")


def run_backup(
    destination: str | Path,
    *,
    include_config: bool = False,
    include_users: bool = False,
    include_chats: bool = False,
    on_progress: ProgressFn = None,
) -> dict[str, Any]:
    plan = plan_backup(
        destination,
        include_config=include_config,
        include_users=include_users,
        include_chats=include_chats,
    )
    if not plan["enough_space"]:
        raise StackBackupError(
            f"Not enough free space: need {format_bytes(plan['needed_bytes'])}, "
            f"have {format_bytes(plan['free_bytes'])}"
        )
    dest = Path(plan["destination"])
    _write_manifest(dest, _manifest(plan, complete=False))
    copied = 0
    skipped = 0
    total = max(1, int(plan["needed_bytes"]))
    for job in plan["_jobs"]:
        target = dest / job["relative"]
        if _same_file(job["source"], target):
            skipped += 1
            continue
        _emit(
            on_progress,
            f"Copying {job['relative']} ({format_bytes(job['bytes'])}) "
            f"[{int(copied * 100 / total)}%]",
        )
        _copy_one(job["source"], target, on_progress)
        copied += int(job["bytes"])
    _write_manifest(dest, _manifest(plan, complete=True))
    _emit(on_progress, f"Backup complete: {format_bytes(plan['bytes'])}")
    return {
        **public_plan(plan),
        "copied_bytes": copied,
        "skipped_files": skipped,
        "manifest": str(dest / "manifest.json"),
    }


def _load_manifest(source: Path) -> dict[str, Any]:
    path = source / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StackBackupError(f"Invalid or missing backup manifest: {path}") from exc
    if data.get("format") != FORMAT or int(data.get("version") or 0) != VERSION:
        raise StackBackupError("Unsupported stack backup format or version")
    return data


def plan_restore(
    source: str | Path,
    *,
    include_models: bool = True,
    include_config: bool = False,
    include_users: bool = False,
    include_chats: bool = False,
) -> dict[str, Any]:
    src = _validate_external(Path(source), label="Backup source")
    if not src.is_dir():
        raise StackBackupError(f"Backup source is not a directory: {src}")
    manifest = _load_manifest(src)
    selected = {
        name
        for name, enabled in (
            ("models", include_models),
            ("config", include_config),
            ("users", include_users),
            ("chats", include_chats),
        )
        if enabled
    }
    available = set(manifest.get("groups") or [])
    missing = selected - available
    if missing:
        raise StackBackupError(f"Backup does not contain: {', '.join(sorted(missing))}")

    jobs: list[dict[str, Any]] = []
    for item in manifest.get("items") or []:
        if not isinstance(item, dict) or item.get("group") not in selected:
            continue
        relative = Path(str(item.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise StackBackupError("Backup manifest contains an unsafe path")
        source_path = src / relative
        if not source_path.is_file() or source_path.is_symlink():
            raise StackBackupError(f"Backup file is missing: {relative}")
        if relative.parts[:2] == ("tabbyAPI", "models"):
            target = STACK_ROOT / relative
        elif relative.parts[:2] == ("ComfyUI", "models"):
            target = STACK_ROOT / relative
        elif relative.parts[:2] == ("extras", "tabbyAPI"):
            target = TABBY_ROOT / Path(*relative.parts[2:])
        else:
            raise StackBackupError(f"Backup manifest path is not restorable: {relative}")
        jobs.append(
            {
                "source": source_path,
                "target": target,
                "relative": relative,
                "group": item["group"],
                "bytes": source_path.stat().st_size,
            }
        )
    totals = {group: 0 for group in ALL_GROUPS}
    existing = {group: 0 for group in ALL_GROUPS}
    for job in jobs:
        totals[job["group"]] += job["bytes"]
        if _same_file(job["source"], job["target"]):
            existing[job["group"]] += job["bytes"]
    return {
        "format": FORMAT,
        "version": VERSION,
        "source": str(src),
        "groups": sorted(selected),
        "totals": totals,
        "existing": existing,
        "bytes": sum(totals.values()),
        "already_bytes": sum(existing.values()),
        "needed_bytes": sum(totals.values()) - sum(existing.values()),
        "files": len(jobs),
        "_jobs": jobs,
    }


def run_restore(
    source: str | Path,
    *,
    include_models: bool = True,
    include_config: bool = False,
    include_users: bool = False,
    include_chats: bool = False,
    on_progress: ProgressFn = None,
) -> dict[str, Any]:
    plan = plan_restore(
        source,
        include_models=include_models,
        include_config=include_config,
        include_users=include_users,
        include_chats=include_chats,
    )
    copied = 0
    skipped = 0
    total = max(1, int(plan["needed_bytes"]))
    for job in plan["_jobs"]:
        if _same_file(job["source"], job["target"]):
            skipped += 1
            continue
        _emit(
            on_progress,
            f"Restoring {job['relative']} ({format_bytes(job['bytes'])}) "
            f"[{int(copied * 100 / total)}%]",
        )
        _copy_one(job["source"], job["target"], on_progress)
        copied += int(job["bytes"])
    _emit(on_progress, f"Restore complete: {format_bytes(plan['bytes'])}")
    return {
        **public_plan(plan),
        "copied_bytes": copied,
        "skipped_files": skipped,
        "restart_recommended": "config" in plan["groups"],
    }
