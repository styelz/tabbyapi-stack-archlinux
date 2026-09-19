"""Project PNG dests and curl commands built only from files that exist."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

DEFAULT_PNG_PATH = "images/generated.png"
MEDIA_PATH_SUFFIXES = {".png", ".wav", ".flac", ".mp3", ".mp4", ".webm"}
_DRIVE_ABS_RE = re.compile(r"^[A-Za-z]:/")
_MACHINE_PARENTS = frozenset(
    {
        "home",
        "users",
        "user",
        "tmp",
        "var",
        "private",
        "mnt",
        "media",
        "root",
        "opt",
        "usr",
        "etc",
        "windows",
        "program files",
        "programdata",
        "appdata",
        "volumes",
        "cursor",
        "vscode",
        "documents",
        "desktop",
        "downloads",
        "onedrive",
    }
)
_PROJECT_PARENTS = frozenset(
    {
        "projects",
        "project",
        "repos",
        "repo",
        "src",
        "workspace",
        "work",
        "code",
        "dev",
    }
)
_API_IMAGES_PARENTS = frozenset({"v1", "openai", "api"})
_WORKSPACE_PARENTS = _MACHINE_PARENTS | _PROJECT_PARENTS | _API_IMAGES_PARENTS
JOB_ID_RE = re.compile(r"tabby-image-job:\s*([0-9a-fA-F-]{4,})", re.I)


def _looks_absolute(text: str, path: Path) -> bool:
    return bool(
        path.is_absolute() or _DRIVE_ABS_RE.match(text) or text.startswith("//")
    )


def _keep_images_parent(parts: list[str], images_idx: int) -> bool:
    if images_idx <= 0:
        return False
    parent = parts[images_idx - 1]
    if parent.lower() in _WORKSPACE_PARENTS or parent.startswith("."):
        return False
    if images_idx >= 2 and parts[images_idx - 2].lower() in _MACHINE_PARENTS:
        return False
    return True


def _abs_path_parts(text: str) -> list[str]:
    parts: list[str] = []
    for part in Path(text).parts:
        if part in ("/", "\\"):
            continue
        if len(part) == 2 and part[1] == ":":
            continue
        parts.append(part)
    return parts


def project_png_from_abs(raw: str) -> Optional[str]:
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        return None
    parts = _abs_path_parts(text)
    if not parts or any(part == ".." for part in parts):
        return None
    if "images" not in parts:
        return None
    idx = len(parts) - 1 - parts[::-1].index("images")
    start = idx
    if _keep_images_parent(parts, idx):
        start = idx - 1
    rel = Path(*parts[start:])
    if rel.suffix.lower() not in MEDIA_PATH_SUFFIXES:
        rel = rel.with_suffix(".png")
    cleaned = [part for part in rel.parts if part not in ("", ".")]
    if not cleaned:
        return None
    return Path(*cleaned).as_posix()


def _strip_api_images_prefix(parts: list[str]) -> list[str]:
    lower = [part.lower() for part in parts]
    if lower[:3] == ["openai", "v1", "images"]:
        return ["images", *parts[3:]]
    if lower[:2] == ["v1", "images"]:
        return ["images", *parts[2:]]
    return parts


def safe_rel_png_path(raw: str, default: str = DEFAULT_PNG_PATH) -> str:
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        text = default
    path = Path(text)
    if any(part == ".." for part in path.parts):
        path = Path(default)
    elif _looks_absolute(text, path):
        recovered = project_png_from_abs(text)
        path = Path(recovered) if recovered else Path(default)
    if path.suffix.lower() not in MEDIA_PATH_SUFFIXES:
        path = path.with_suffix(".png")
    parts = [part for part in path.parts if part not in ("", ".")]
    parts = _strip_api_images_prefix(parts)
    if not parts:
        parts = list(Path(default).parts)
    return Path(*parts).as_posix()


def uniquify_rel_png_paths(
    paths: Iterable[str], reserved: Iterable[str] = ()
) -> list[str]:
    used = {safe_rel_png_path(path) for path in reserved if path}
    out: list[str] = []
    for raw in paths:
        path = safe_rel_png_path(raw)
        if path not in used:
            used.add(path)
            out.append(path)
            continue
        stem = Path(path)
        index = 2
        while True:
            candidate = safe_rel_png_path(
                stem.with_name(f"{stem.stem}-{index}{stem.suffix}").as_posix()
            )
            if candidate not in used:
                used.add(candidate)
                out.append(candidate)
                break
            index += 1
    return out


def resolve_output_paths(items: Iterable, reserved: Iterable[str] = ()) -> list[str]:
    planned: list[str] = []
    rows = list(items)
    for item in rows:
        if isinstance(item, dict):
            dest = safe_rel_png_path(str(item.get("output_path") or ""))
        else:
            dest = safe_rel_png_path(getattr(item, "output_path", "") or "")
        planned.append(dest)
    unique = uniquify_rel_png_paths(planned, reserved=reserved)
    for item, dest in zip(rows, unique):
        if isinstance(item, dict):
            item["output_path"] = dest
        else:
            item.output_path = dest
    return unique


def is_http_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def generated_png_name_from_url(url: str) -> str:
    path = urlparse(str(url or "")).path
    return Path(path).name


def gpu_generated_file_missing(url: str) -> bool:
    from common.gpu_mode import generated_image_path, is_public_generated_png

    name = generated_png_name_from_url(url)
    if not is_public_generated_png(name):
        return False
    path = generated_image_path(name)
    return path is None or not path.is_file()


def _posix_quote(text: str) -> str:
    return "'" + str(text).replace("'", "'\\''") + "'"


def _safe_curl_url(url: str) -> Optional[str]:
    text = str(url or "").strip()
    if not is_http_url(text):
        return None
    if any(ch in text for ch in ("'", '"', "\n", "\r", " ", "$", "`", "\\")):
        return None
    return text


def living_download_pairs(job) -> list[tuple[str, str]]:
    """(url, dest) only for timestamped PNGs that exist on this host."""
    out: list[tuple[str, str]] = []
    for item in getattr(job, "items", None) or []:
        dest = safe_rel_png_path(getattr(item, "output_path", "") or "")
        for url in getattr(item, "urls", None) or []:
            safe_url = _safe_curl_url(str(url))
            if not safe_url or gpu_generated_file_missing(safe_url):
                continue
            out.append((safe_url, dest))
            break
    if out:
        return out
    dests = [
        safe_rel_png_path(getattr(item, "output_path", "") or "")
        for item in (getattr(job, "items", None) or [])
    ]
    urls = [str(url) for url in (getattr(job, "urls", None) or [])]
    for url, dest in zip(urls, dests):
        safe_url = _safe_curl_url(url)
        if not safe_url or gpu_generated_file_missing(safe_url):
            continue
        out.append((safe_url, dest))
    return out


def image_download_command(pairs: Iterable[tuple[str, str]]) -> str:
    rows = [(url, dest) for url, dest in pairs if _safe_curl_url(url)]
    if not rows:
        return ""
    dirs: list[str] = []
    auth = '${TABBY_API_KEY:+-H "Authorization: Bearer $TABBY_API_KEY"}'
    for _url, dest in rows:
        parent = Path(dest).parent.as_posix()
        if parent not in (".", "") and parent not in dirs:
            dirs.append(parent)
    # One curl --parallel. Do not put `--` between pairs: that ends option
    # parsing, so later -o flags are treated as URLs and the batch fails.
    fetches = " ".join(
        f"-o {_posix_quote(dest)} --url {_posix_quote(url)}" for url, dest in rows
    )
    curl = (
        f"curl -fsSL --connect-timeout 15 --max-time 120 --parallel {auth} {fetches}"
    )
    listed = " ".join(_posix_quote(dest) for _, dest in rows)
    prefix = ""
    if dirs:
        prefix = "mkdir -p -- " + " ".join(_posix_quote(item) for item in dirs) + " && "
    return prefix + curl + " && ls -l -- " + listed


_LS_FAIL_RE = re.compile(
    r"no such file|existing none|cannot access|not found|curl:",
    re.I,
)


def tool_result_has_pngs(text: str, dests: Iterable[str]) -> bool:
    """True when a Shell ls/curl result lists every dest.

    A stray 'not found' for some other path must not fail the whole batch —
    VS Code Copilot often ls extra files. Only a missing needed dest fails.
    """
    blob = str(text or "")
    if not blob:
        return False
    needed = [safe_rel_png_path(dest) for dest in dests if dest]
    if not needed:
        return False
    for dest in needed:
        name = Path(dest).name
        listed = False
        missing = False
        for line in blob.splitlines():
            if dest not in line and name not in line:
                continue
            listed = True
            if _LS_FAIL_RE.search(line):
                missing = True
        if not listed or missing:
            return False
    return True


def image_download_note(pairs: Iterable[tuple[str, str]]) -> str:
    rows = list(pairs)
    if not rows:
        return ""
    lines = ["Images are ready. Save each URL into the project:"]
    lines.extend(f"- {dest}  {url}" for url, dest in rows)
    lines.append(
        "Do not overwrite these files with Pillow, SVG, or a Python drawing script."
    )
    return "\n".join(lines)


def job_ids_from_text(text: str) -> list[str]:
    found: list[str] = []
    for match in JOB_ID_RE.finditer(text or ""):
        job_id = (match.group(1) or "").strip()
        if job_id:
            found.append(job_id)
    return found


def job_id_from_text(text: str) -> str:
    """Latest stamp wins. An older done job must not hide a replace/redo."""
    ids = job_ids_from_text(text)
    return ids[-1] if ids else ""


def dest_fact_list(pairs: Iterable[tuple[str, str]]) -> str:
    rows = list(pairs)
    if not rows:
        return ""
    names = ", ".join(dest for _, dest in rows)
    return (
        f"These image files exist at: {names}. "
        "Write HTML/CSS/JS that points at those local paths "
        "(use the extensions listed — they may be .webp). "
        "Do not generate images. Do not write Python drawing scripts."
    )


def planned_dest_fact_list(items: Iterable[dict[str, str]]) -> str:
    """Dests the page should use before Comfy has written the PNGs."""
    names = [str(row.get("output_path") or "").strip() for row in items]
    names = [name for name in names if name]
    if not names:
        return ""
    listed = ", ".join(names)
    return (
        f"Write or update every HTML/CSS/JS file for this page now. "
        f"Point img src, video src, audio src, or CSS url() at these exact "
        f"local paths: {listed}. "
        "Do not Write PNG, WebP, GIF, WAV, MP4, or placeholder media files. "
        "Do not generate images. Do not write Python drawing scripts. "
        "Do not dump the page in chat; use file tools. "
        "The GPU will save those media files after you finish the page."
    )
