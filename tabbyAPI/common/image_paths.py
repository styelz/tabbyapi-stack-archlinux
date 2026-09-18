"""Legacy PNG dest helpers (regex guess, sleep/ls poll).

Live chat/MCP dests and curl commands live in ``images.paths`` (no
``guess_output_path``). Keep this module for tests and leftover poll
detection in ``agent_loop``. Do not wire it back into the control plane.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse


DEFAULT_PNG_PATH = "images/generated.png"
_DRIVE_ABS_RE = re.compile(r"^[A-Za-z]:/")
# Parents of images/ that are the machine/workspace, not a site folder.
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


def _looks_absolute(text: str, path: Path) -> bool:
    return bool(
        path.is_absolute() or _DRIVE_ABS_RE.match(text) or text.startswith("//")
    )


def _keep_images_parent(parts: list[str], images_idx: int) -> bool:
    """True when the folder above images/ is a site dir (pbptours), not the clone."""
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
    """Turn /home/.../pbptours/images/logo.png into pbptours/images/logo.png.

    VS Code Copilot often sends workspace-absolute output_path values. Dropping
    those on the floor made every dest images/generated.png (then uniquified),
    so Shell saved the wrong files and skipped the site folder.
    """
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
    if rel.suffix.lower() != ".png":
        rel = rel.with_suffix(".png")
    cleaned = [part for part in rel.parts if part not in ("", ".")]
    if not cleaned:
        return None
    return Path(*cleaned).as_posix()


def _strip_api_images_prefix(parts: list[str]) -> list[str]:
    """v1/images/logo.png is the API URL path, not a project folder."""
    lower = [part.lower() for part in parts]
    if lower[:3] == ["openai", "v1", "images"]:
        return ["images", *parts[3:]]
    if lower[:2] == ["v1", "images"]:
        return ["images", *parts[2:]]
    return parts


_MEDIA_SUFFIXES = {".png", ".wav", ".flac", ".mp3", ".mp4", ".webm"}


def safe_rel_png_path(raw: str, default: str = DEFAULT_PNG_PATH) -> str:
    """Keep generated media inside the project. Reject escapes and unknown abs paths."""
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        text = default
    path = Path(text)
    if any(part == ".." for part in path.parts):
        path = Path(default)
    elif _looks_absolute(text, path):
        recovered = project_png_from_abs(text)
        path = Path(recovered) if recovered else Path(default)
    if path.suffix.lower() not in _MEDIA_SUFFIXES:
        path = path.with_suffix(".png")
    parts = [part for part in path.parts if part not in ("", ".")]
    parts = _strip_api_images_prefix(parts)
    if not parts:
        parts = list(Path(default).parts)
    return Path(*parts).as_posix()


_PATH_HINTS = (
    (("logo", "wordmark", "favicon", "icon"), "images/logo.png"),
    (("header", "banner", "hero", "masthead"), "images/header.png"),
    (("mercury",), "images/mercury.png"),
    (("venus",), "images/venus.png"),
    (("earth",), "images/earth.png"),
    (("mars",), "images/mars.png"),
    (("jupiter",), "images/jupiter.png"),
    (("saturn",), "images/saturn.png"),
    (("uranus",), "images/uranus.png"),
    (("neptune",), "images/neptune.png"),
)


_NO_HINT_RE = re.compile(
    r"\bno\s+(?:logo|wordmark|word-?mark|favicon|icon|header|banner|hero|"
    r"masthead|mercury|venus|earth|mars|jupiter|saturn|uranus|neptune|"
    r"text|letters?)\b"
)


def guess_output_path(prompt: str, default: str = DEFAULT_PNG_PATH) -> str:
    """Pick a stable dest when the agent omitted output_path.

    Ignore 'no logo' / 'no text' tails from rewrite_comfy_prompt so a Mercury
    scene does not collapse to images/logo.png.
    """
    text = _NO_HINT_RE.sub(" ", " ".join((prompt or "").lower().split()))
    for words, path in _PATH_HINTS:
        if any(re.search(rf"\b{re.escape(word)}\b", text) for word in words):
            return path
    return default


def uniquify_rel_png_paths(
    paths: Iterable[str], reserved: Iterable[str] = ()
) -> list[str]:
    """Keep colliding dests from overwriting: logo.png, logo-2.png, …"""
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
    """Guess omitted dests (logo/header) and uniquify collisions in place."""
    planned: list[str] = []
    rows = list(items)
    for item in rows:
        if isinstance(item, dict):
            prompt = str(item.get("prompt") or "")
            dest = safe_rel_png_path(str(item.get("output_path") or ""))
        else:
            prompt = str(getattr(item, "prompt", "") or "")
            dest = safe_rel_png_path(getattr(item, "output_path", "") or "")
        if dest == DEFAULT_PNG_PATH:
            dest = guess_output_path(prompt)
        planned.append(dest)
    unique = uniquify_rel_png_paths(planned, reserved=reserved)
    for item, dest in zip(rows, unique):
        if isinstance(item, dict):
            item["output_path"] = dest
        else:
            item.output_path = dest
    return unique


_COLLAPSED_DEST_RE = re.compile(r"^(logo|generated)(-\d+)?\.png$", re.I)


def dests_look_collapsed(items: Iterable) -> bool:
    """True when a batch was uniquified to images/logo.png, images/logo-2.png, …"""
    names: list[str] = []
    for item in items or []:
        if isinstance(item, dict):
            dest = str(item.get("output_path") or "")
        else:
            dest = str(getattr(item, "output_path", "") or "")
        names.append(Path(safe_rel_png_path(dest)).name)
    return len(names) > 1 and all(_COLLAPSED_DEST_RE.match(name) for name in names)


def ensure_site_prefix(dest: str, site_folder: str) -> str:
    """images/logo.png + pbptours → pbptours/images/logo.png."""
    path = safe_rel_png_path(dest)
    folder = str(site_folder or "").strip().strip("/")
    if not folder or "/" in folder or folder == "images":
        return path
    parts = Path(path).parts
    if not parts:
        return path
    if parts[0] == folder:
        return path
    if parts[0] == "images":
        return f"{folder}/{path}"
    return path


def align_item_dests(items: Iterable, site_folder: str = "") -> list[str]:
    """Fix dests before inventing curl.

    A collapsed logo-N batch is renamed from each prompt (Mercury stays
    mercury.png). A site folder is prefixed so files land in pbptours/images/
    instead of workspace-root images/.
    """
    rows = list(items or [])
    collapsed = dests_look_collapsed(rows)
    planned: list[str] = []
    for item in rows:
        if isinstance(item, dict):
            prompt = str(item.get("prompt") or "")
            dest = safe_rel_png_path(str(item.get("output_path") or ""))
        else:
            prompt = str(getattr(item, "prompt", "") or "")
            dest = safe_rel_png_path(getattr(item, "output_path", "") or "")
        if collapsed or dest == DEFAULT_PNG_PATH:
            dest = guess_output_path(prompt)
        dest = ensure_site_prefix(dest, site_folder)
        planned.append(dest)
    unique = uniquify_rel_png_paths(planned)
    for item, dest in zip(rows, unique):
        if isinstance(item, dict):
            item["output_path"] = dest
        else:
            item.output_path = dest
    return unique


def _url_list(raw) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(url) for url in raw if url]


def download_pairs_from_job(job) -> list[tuple[str, str]]:
    """(url, relative output_path) for every finished item on an MCP job."""
    planned: list[tuple[str, str]] = []
    items = getattr(job, "items", None)
    if not isinstance(items, (list, tuple)):
        items = []
    for item in items:
        dest = safe_rel_png_path(getattr(item, "output_path", "") or "")
        for url in _url_list(getattr(item, "urls", None)):
            planned.append((url, dest))
    if not planned:
        dest = safe_rel_png_path(getattr(job, "output_path", "") or "")
        for url in _url_list(getattr(job, "urls", None)):
            planned.append((url, dest))
    dests = uniquify_rel_png_paths(path for _, path in planned)
    return [(url, dest) for (url, _), dest in zip(planned, dests)]


def generated_png_name_from_url(url: str) -> str:
    """Filename from a /v1/images/generated-*.png URL (query stripped)."""
    try:
        path = urlparse(str(url or "")).path
    except ValueError:
        return ""
    name = path.rsplit("/", 1)[-1]
    return name.split("?")[0]


def gpu_generated_file_missing(url: str) -> bool:
    """True when this URL is a timestamped generated PNG that is not on disk."""
    from common.gpu_mode import generated_image_path, is_public_generated_png

    name = generated_png_name_from_url(url)
    if not is_public_generated_png(name):
        return False
    return generated_image_path(name) is None


def living_download_pairs(job) -> list[tuple[str, str]]:
    """download_pairs_from_job minus timestamped URLs whose GPU files are gone."""
    return [
        (url, dest)
        for url, dest in download_pairs_from_job(job)
        if not gpu_generated_file_missing(url)
    ]


def is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


IMAGE_POLL_WAIT_S = 20


def dest_project_folder(path: str) -> str:
    """First folder when dest is site/images/x.png. Empty for images/x.png."""
    parts = [part for part in Path(safe_rel_png_path(path)).parts if part not in ("", ".")]
    if len(parts) >= 2 and parts[0] != "images":
        return parts[0]
    return ""


def job_project_folders(job) -> set[str]:
    return {
        folder
        for folder in (dest_project_folder(path) for path in job_output_paths(job))
        if folder
    }


def job_output_paths(job) -> list[str]:
    """Relative PNG paths this job asked the coding machine to write."""
    raw_items = getattr(job, "items", None)
    if not isinstance(raw_items, (list, tuple)):
        raw_items = []
    planned: list[str] = []
    for item in raw_items:
        dest = safe_rel_png_path(getattr(item, "output_path", "") or "")
        try:
            copies = max(1, int(getattr(item, "count", 1) or 1))
        except (TypeError, ValueError):
            copies = 1
        planned.extend([dest] * copies)
    if not planned:
        planned.append(safe_rel_png_path(getattr(job, "output_path", "") or ""))
    return uniquify_rel_png_paths(planned)


def _posix_quote(text: str) -> str:
    """Single-quote a token for bash / Cursor Shell."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def _safe_curl_url(url: str) -> Optional[str]:
    text = str(url or "").strip()
    if not is_http_url(text):
        return None
    if any(ch in text for ch in ("'", '"', "\n", "\r", " ", "$", "`", "\\")):
        return None
    return text


def image_poll_wait_command(job, wait_s: int = IMAGE_POLL_WAIT_S) -> str:
    """Sleep, then list which job PNGs already exist."""
    try:
        hold = max(1, min(int(wait_s), 45))
    except (TypeError, ValueError):
        hold = IMAGE_POLL_WAIT_S
    paths = job_output_paths(job)
    job_id = str(getattr(job, "id", "") or "")
    listed = " ".join(_posix_quote(path) for path in paths)
    return (
        f"sleep {hold}; "
        f"echo job {_posix_quote(job_id)} still running; "
        f"ls -l -- {listed} 2>/dev/null || echo existing none"
    )


def image_running_shell_command(job, wait_s: int = IMAGE_POLL_WAIT_S) -> str:
    """Sleep and list dests. Do not curl until the job is done.

    Mid-batch curls taught GitHub Copilot the generated-*.png URL pattern.
    It then invented timestamps for planets that were still rendering and
    404'd the whole && chain.
    """
    return image_poll_wait_command(job, wait_s=wait_s)


def image_running_note(job) -> str:
    """Tell the agent the batch is still on the GPU — no URL list to copy."""
    dests = job_output_paths(job)
    done = len(download_pairs_from_job(job))
    total = max(len(dests), done, 1)
    return (
        f"Still generating ({done}/{total} PNG(s) rendered on the GPU). "
        "Do not download yet. Do not invent /v1/images/generated-*.png URLs "
        "or guess timestamps. When this job is done the next turn curls "
        "the real URLs into each output_path."
    )


def image_download_pairs(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Validated (url, relative dest) rows a curl command can fetch."""
    out: list[tuple[str, str]] = []
    for url, path in pairs:
        safe_url = _safe_curl_url(url)
        if not safe_url:
            continue
        out.append((safe_url, safe_rel_png_path(path)))
    return out


def image_download_note(pairs: Iterable[tuple[str, str]]) -> str:
    """Visible URL list so the agent/user can open the files without the blob."""
    rows = image_download_pairs(pairs)
    if not rows:
        return ""
    lines = [
        "Images are ready. Downloading each URL into the project:",
    ]
    lines.extend(f"- {dest}  {url}" for url, dest in rows)
    lines.append(
        "Do not overwrite these files with Pillow, SVG, or a Python drawing script."
    )
    return "\n".join(lines)


def image_download_command(pairs: Iterable[tuple[str, str]]) -> str:
    """curl each public /v1/images/... URL into its project path.

    The GPU host already serves the PNG. The coding machine is a different
    computer, so Shell just GETs the URL. Keep the command readable — do not
    wrap it in python -c / base64.
    """
    rows = image_download_pairs(pairs)
    if not rows:
        return ""
    dirs = []
    fetches = []
    auth = '${TABBY_API_KEY:+-H "Authorization: Bearer $TABBY_API_KEY"}'
    for url, dest in rows:
        parent = Path(dest).parent.as_posix()
        if parent not in (".", "") and parent not in dirs:
            dirs.append(parent)
        fetches.append(
            f"curl -fsSL --max-time 120 {auth} -o {_posix_quote(dest)} -- {_posix_quote(url)}"
        )
    prefix = ""
    if dirs:
        prefix = "mkdir -p -- " + " ".join(_posix_quote(item) for item in dirs) + " && "
    listed = " ".join(_posix_quote(dest) for _, dest in rows)
    return prefix + " && ".join(fetches) + " && ls -l -- " + listed


def _fold_tool_name(name: str) -> str:
    return str(name or "").strip().lower().replace("-", "_")


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


# VS Code Copilot lists kill_terminal; that is not a place to run curl.
_NOT_RUNNER_PREFIXES = ("kill_", "stop_", "close_", "cancel_")


def match_tool_name(names: Iterable[str], wanted: Iterable[str]) -> Optional[str]:
    """Return the original tool name that matches one of wanted (case-insensitive).

    Cursor MCP names vary: get_image_job, mcp_tabby-images_get_image_job,
    mcp_tabby-images_get-image-job. Hyphens and the mcp_ prefix all match.
    Do not treat kill_terminal as a shell just because it ends with terminal.
    """
    wanted_set = {_fold_tool_name(item) for item in wanted if item}
    for original in names:
        key = _fold_tool_name(original)
        if not key:
            continue
        if key in wanted_set:
            return original
        if any(key.startswith(prefix) for prefix in _NOT_RUNNER_PREFIXES):
            continue
        tail = key.rsplit("_", 1)[-1]
        if tail in wanted_set:
            return original
        for want in wanted_set:
            if key.endswith(want):
                return original
    return None
