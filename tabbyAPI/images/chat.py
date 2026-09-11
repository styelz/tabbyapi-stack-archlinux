"""Chat intercept: write mixed-site code first, then hold for GPU PNGs."""

from __future__ import annotations

import asyncio
import json
import posixpath
import re
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4

from common.logger import xlogger
from common.networking import get_sse_ping_interval
from endpoints.OAI.types.chat_completion import ChatCompletionRequest
from sse_starlette import EventSourceResponse, ServerSentEvent

from images.jobs import (
    abandon_foreign_coding_job,
    active_mcp_image_job,
    copy_job_to_workspace,
    get_mcp_image_job,
    launch_mcp_image_job,
    note_coding_progress,
    start_mcp_image_job,
    wait_mcp_job_progress,
    wait_until_done,
)
from images.paths import (
    dest_fact_list,
    image_download_command,
    image_download_note,
    job_id_from_text,
    job_ids_from_text,
    living_download_pairs,
    planned_dest_fact_list,
    tool_result_has_pngs,
)
from images.plan import ImageTurnPlan, classify_image_turn, plan_from_extracted

JOB_MARK = "tabby-image-job:"
STATUS_MARK = "tabby-image-status:"
HOLD_KEEPALIVE_S = 12.0
NO_MODEL_WRITE = "No model is loaded, so files were not written."
NO_TEMPLATE_WRITE = "Chat is disabled because no prompt template is set."
MAX_CODE_TURNS = 16
_ATTACHED_PROJECT_IMAGE_RE = re.compile(r"Attached project image:\s+`([^`]+)`")
FILE_WRITE_NAMES = (
    "write",
    "write_file",
    "write_to_file",
    "create_file",
    "strreplace",
    "search_replace",
    "replace_in_file",
    "apply_patch",
    "applypatch",
    "apply_diff",
    "edit_notebook",
    "edit_file",
)
READONLY_AGENTS = frozenset({"ask", "plan"})
_HERO_STEMS = frozenset(
    {"header", "hero", "banner", "hero-background", "hero_background"}
)
_EXPLICIT_NEW_RE = re.compile(
    r"(?is)(?:"
    r"qwen-image:|"
    r"\b(?:generate|draw|render|create)\s+"
    r"(?:(?:real|gpu|new)\s+)*"
    r"(?:an?\s+)?"
    r"(?:images?|pictures?|photos?|pics?|logo|hero(?:\s+photo)?|header\s+photo)\b|"
    r"\b(?:redo|recreate)\b|"
    r"\breplace\s+the\s+(?:logo|hero(?:\s+(?:image|photo))?|header\s+(?:image|photo)|image|photo)\b|"
    r"\bnew\s+(?:logo|hero(?:\s+photo)?|header\s+photo)\b"
    r")"
)
_RASTER_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_NAMED_DEST_RE = re.compile(r"(?i)\bimages/([A-Za-z][A-Za-z0-9_-]*)")
_CREATE_SITE_RE = re.compile(
    r"(?is)\b(?:create|write|make|build)\b.{0,160}?\b"
    r"(?:landing(?:\s+page)?|website|web\s+page|webpage)\b"
)
_PAGE_LOGO_RE = re.compile(r"(?is)\b(?:a\s+)?logo\b")
_PAGE_HERO_RE = re.compile(r"(?is)\b(?:header|hero)\s+(?:photo|image|picture)s?\b")
_PAGE_WIRE_RE = re.compile(
    r"(?is)\b(?:opaci|opaque|blend|on (?:each |the )?(?:home page|panels?|cards?|sections?))\b"
)
_NAMED_HTML_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9._/-])((?:[A-Za-z0-9._/-]+/)?[A-Za-z0-9._-]+\.html?)\b"
)
_SKIP_DEST_STEMS = frozenset({"image", "images", "generated", "generated.png"})
_GALLERY_OUTPUT_STEM_RE = re.compile(r"^generated(?:-\d+){3}$")
_APPROVED_PLAN_RE = re.compile(r"(?is)<approved_plan>(.*?)</approved_plan>")
_ASSETS_SECTION_RE = re.compile(
    r"(?im)^#{1,3}\s+assets\b[^\n]*\n(.*?)(?=^#{1,3}\s+|\Z)"
)
_ASSET_NONE_RE = re.compile(r"(?is)^\s*(?:[-*]\s*)?none\b")
_ASSET_FILE_RE = re.compile(r"(?i)\b[\w./-]+\.(?:png|jpe?g|webp|gif)\b")
_SKIP_ASSET_STEMS = frozenset(
    {"etc", "css", "css3", "html", "html5", "js", "javascript", "react", "vue"}
)
def _readonly_agent(agent: str | None) -> bool:
    """UI Ask/Plan: inspect only. Do not start Comfy or write files."""
    return str(agent or "").strip().lower() in READONLY_AGENTS


def workspace_raster_paths(owner: str | None, chat_id: str | None) -> list[str]:
    """Relative image paths already in this UI Code workspace."""
    if not owner or not chat_id:
        return []
    try:
        from ui.workspace import IMAGE_SUFFIXES, list_files
    except Exception:
        return []
    try:
        rows = list_files(owner, chat_id)
    except Exception:
        return []
    suffixes = IMAGE_SUFFIXES or _RASTER_SUFFIXES
    found: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").strip().replace("\\", "/")
        if not path or _is_scratch_raster(path):
            continue
        try:
            size = int(row.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size and size < 1024:
            continue
        kind = str(row.get("kind") or "")
        if kind == "image" or Path(path).suffix.lower() in suffixes:
            found.append(path)
    return found


def _is_scratch_raster(path: str) -> bool:
    """Scratch leftovers are not dests for a new images/logo or header."""
    clean = str(path or "").replace("\\", "/").lstrip("/")
    return clean == "scratch" or clean.startswith("scratch/")


def _skip_dest_stem(stem: str) -> bool:
    """Generic and gallery stamps are not workspace dests to generate."""
    return stem in _SKIP_DEST_STEMS or bool(_GALLERY_OUTPUT_STEM_RE.match(stem))


def _named_image_dests(text: str) -> list[str]:
    """Project dests the user named, such as images/logo or images/header."""
    found: list[str] = []
    seen: set[str] = set()
    blob = _ATTACHED_PROJECT_IMAGE_RE.sub("", text or "")

    def add(dest: str) -> None:
        if dest in seen:
            return
        seen.add(dest)
        found.append(dest)

    for match in _NAMED_DEST_RE.finditer(blob):
        raw = str(match.group(1) or "").strip(".-")
        stem = Path(raw).stem.lower().replace("_", "-").strip(".-")
        if not stem or _skip_dest_stem(stem):
            continue
        if Path(raw).suffix:
            name = Path(raw).name
        else:
            name = f"{stem}.png"
        add(f"images/{name}")
    if _CREATE_SITE_RE.search(blob):
        if _PAGE_LOGO_RE.search(blob):
            add("images/logo.png")
        if _PAGE_HERO_RE.search(blob):
            have_hero = any(
                Path(dest).stem.lower().replace("_", "-") in _HERO_STEMS
                for dest in found
            )
            if not have_hero:
                add("images/header.png")
    return found


def _workspace_gpu_raster(owner: str, chat_id: str, dest: str) -> bool:
    """True when this dest is a real GPU raster, not a stub or converted leftover."""
    wanted = str(dest or "").strip().replace("\\", "/").lstrip("/")
    if not wanted or _is_scratch_raster(wanted):
        return False
    try:
        from ui.workspace import resolve_file

        path = resolve_file(owner, chat_id, wanted)
    except (OSError, ValueError, FileNotFoundError):
        return False
    try:
        from ui.workspace import raster_file_ok

        return raster_file_ok(path)
    except OSError:
        return False


def _gpu_dest_ready(
    dest: str,
    existing: list[str],
    owner: str | None = None,
    chat_id: str | None = None,
) -> bool:
    """Skip Comfy only when the named dest is already a real GPU file."""
    if owner and chat_id:
        return _workspace_gpu_raster(owner, chat_id, dest)
    return _dest_exists(dest, existing)


def _upgrade_missing_named_dests(
    plan: ImageTurnPlan,
    ask: str,
    existing: list[str],
    owner: str | None = None,
    chat_id: str | None = None,
) -> ImageTurnPlan:
    """Scratch leftovers are not reuse when images/logo or header is still missing."""
    if plan.action == "generate" and plan.items:
        return plan
    named = list(dict.fromkeys(_named_image_dests(ask) + _plan_asset_dests(ask)))
    if not named:
        return plan
    missing = [
        dest
        for dest in named
        if not _gpu_dest_ready(dest, existing, owner, chat_id)
    ]
    if not missing:
        return plan
    items = plan_from_extracted(ask, [{"filename": dest} for dest in missing])
    if not items:
        return plan
    return ImageTurnPlan(action="generate", items=items, from_model=False)


def workspace_raster_facts(paths: list[str]) -> str:
    if not paths:
        return ""
    return "Already in the project: " + ", ".join(paths) + "."


def _raster_stem(path: str) -> str:
    return Path(str(path or "").replace("\\", "/")).stem.lower().replace("_", "-")


def _dest_exists(dest: str, existing: list[str]) -> bool:
    wanted = str(dest or "").strip().replace("\\", "/").lstrip("/")
    if not wanted or not existing:
        return False
    dest_name = Path(wanted).name.lower()
    dest_stem = _raster_stem(wanted)
    dest_stems = {dest_stem}
    if dest_stem in _HERO_STEMS:
        dest_stems |= _HERO_STEMS
    for path in existing:
        have = str(path or "").strip().replace("\\", "/").lstrip("/")
        if not have:
            continue
        if have == wanted or have.endswith("/" + wanted) or wanted.endswith("/" + have):
            return True
        name = Path(have).name.lower()
        if name == dest_name:
            return True
        stem = _raster_stem(have)
        if stem == dest_stem:
            return True
        if dest_stem in dest_stems and stem in dest_stems:
            return True
    return False


def _existing_raster_paths(
    job,
    workspace_paths: list[str],
    owner: str | None = None,
    chat_id: str | None = None,
) -> list[str]:
    found = list(workspace_paths)
    if job is None:
        return found
    for _url, dest in living_download_pairs(job):
        if not dest:
            continue
        if owner and chat_id:
            if _workspace_gpu_raster(owner, chat_id, dest):
                found.append(dest)
            continue
        found.append(dest)
    return found


def _all_dests_exist(
    items: list[dict[str, str]],
    existing: list[str],
    owner: str | None = None,
    chat_id: str | None = None,
) -> bool:
    dests = [str(row.get("output_path") or "").strip() for row in items or []]
    dests = [dest for dest in dests if dest]
    if not dests:
        return False
    if owner and chat_id:
        return all(_workspace_gpu_raster(owner, chat_id, dest) for dest in dests)
    if not existing:
        return False
    return all(_dest_exists(dest, existing) for dest in dests)


def _last_assistant_text(data: ChatCompletionRequest) -> str:
    for message in reversed(data.messages or []):
        if (message.role or "").lower() == "assistant":
            return _content_text(message.content)
    return ""


def _asset_section_blob(text: str) -> str:
    blob = _ATTACHED_PROJECT_IMAGE_RE.sub("", text or "")
    match = _APPROVED_PLAN_RE.search(blob)
    if match:
        blob = match.group(1) or ""
    section = _ASSETS_SECTION_RE.search(blob)
    if section:
        body = section.group(1) or ""
        if _ASSET_NONE_RE.search(body.strip()) and not _ASSET_FILE_RE.search(body):
            return ""
        return body
    return blob


def _plan_asset_rasters(text: str) -> list[str]:
    """PNG/WebP dest basenames from an approved plan or ## Assets section."""
    found: list[str] = []
    seen: set[str] = set()
    for raw in _ASSET_FILE_RE.findall(_asset_section_blob(text)):
        name = Path(str(raw).replace("\\", "/")).name
        stem = Path(name).stem.lower().replace("_", "-")
        if not stem or _skip_dest_stem(stem) or stem in _SKIP_ASSET_STEMS:
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        found.append(name)
    return found


def _plan_asset_dests(text: str) -> list[str]:
    """Full relative dests from ## Assets, keeping assets/ or images/."""
    found: list[str] = []
    seen: set[str] = set()
    for raw in _ASSET_FILE_RE.findall(_asset_section_blob(text)):
        path = str(raw).replace("\\", "/").lstrip("./")
        if not path or ".." in path.split("/"):
            continue
        name = Path(path).name
        stem = Path(name).stem.lower().replace("_", "-")
        if not stem or _skip_dest_stem(stem) or stem in _SKIP_ASSET_STEMS:
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        if "/" not in path:
            path = f"images/{name}"
        found.append(path)
    return found


def _stem_tokens(path: str) -> frozenset[str]:
    stem = Path(str(path or "").replace("\\", "/")).stem.lower().replace("_", "-")
    return frozenset(
        part
        for part in stem.split("-")
        if part and part not in {"img", "image", "pic", "photo", "png", "webp", "gif"}
    )


def _best_dest_match(wanted: str, dests: list[str], used: set[str]) -> str:
    want = _stem_tokens(wanted)
    if not want or not dests:
        return ""
    best = ""
    best_score = 0
    for dest in dests:
        if dest in used:
            continue
        have = _stem_tokens(dest)
        score = len(want & have)
        if score > best_score:
            best = dest
            best_score = score
    return best if best_score else ""


def _apply_approved_asset_dests(plan: ImageTurnPlan, ask: str) -> ImageTurnPlan:
    """Use ## Assets dests (assets/foo.png) instead of invented images/ names."""
    dests = _plan_asset_dests(ask)
    if not dests or plan.action != "generate" or not plan.items:
        return plan
    used: set[str] = set()
    items: list[dict[str, str]] = []
    for row in plan.items:
        dest = str(row.get("output_path") or "")
        prompt = str(row.get("prompt") or "")
        match = _best_dest_match(dest, dests, used) or _best_dest_match(
            prompt.replace(" ", "-"), dests, used
        )
        updated = dict(row)
        if match:
            updated["output_path"] = match
            used.add(match)
        items.append(updated)
    return ImageTurnPlan(action="generate", items=items, from_model=plan.from_model)


def _explicit_new_rasters(data: ChatCompletionRequest) -> bool:
    from common.phrase_switch import last_user_text, requested_image_prompt

    if requested_image_prompt(data, explicit_only=True):
        return True
    from ui.code_agent import is_build_prompt

    text = (last_user_text(data) or "").strip()
    if _EXPLICIT_NEW_RE.search(text):
        return True
    if _CREATE_SITE_RE.search(text) and (
        _PAGE_LOGO_RE.search(text) or _PAGE_HERO_RE.search(text)
    ):
        return True
    if not is_build_prompt(text):
        return False
    if _plan_asset_rasters(text):
        return True
    assistant = _last_assistant_text(data) or ""
    if _EXPLICIT_NEW_RE.search(assistant):
        return True
    return bool(_plan_asset_rasters(assistant))


def _workspace_dest_facts(job) -> str:
    """Prefer on-disk workspace copies (.webp) over the job's original .png dests."""
    names = [
        str(path).strip()
        for path in (getattr(job, "workspace_files", None) or [])
        if str(path).strip()
    ]
    if names:
        listed = ", ".join(names)
        return (
            f"These image files exist at: {listed}. "
            "Write HTML/CSS/JS that points at those local paths. "
            "Do not generate images. Do not write Python drawing scripts."
        )
    return dest_fact_list(living_download_pairs(job))


def _classify_prior_facts(job, workspace_paths: list[str]) -> str:
    parts: list[str] = []
    if job and job.status in ("done", "error"):
        dests = _workspace_dest_facts(job)
        if dests:
            parts.append(dests)
    raster = workspace_raster_facts(workspace_paths)
    if raster:
        parts.append(raster)
    return "\n".join(parts)


def _inject_existing_image_facts(
    data: ChatCompletionRequest, job, workspace_paths: list[str]
) -> None:
    if job:
        _inject_dest_facts(data, job)
    facts = workspace_raster_facts(workspace_paths)
    if facts:
        _append_user_facts(
            data,
            facts + " Use those local paths. Do not generate images.",
        )


def _content_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        text = getattr(part, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def last_role(data: ChatCompletionRequest) -> str:
    messages = data.messages or []
    if not messages:
        return ""
    return (messages[-1].role or "").lower()


def _history_blob(data: ChatCompletionRequest) -> str:
    parts: list[str] = []
    for message in data.messages or []:
        parts.append(_content_text(message.content))
        for call in message.tool_calls or []:
            args = getattr(getattr(call, "function", None), "arguments", "") or ""
            parts.append(str(args))
    return "\n".join(parts)


def job_id_from_history(data: ChatCompletionRequest) -> str:
    """Prefer a still-running stamp. The first done job in a long thread is stale."""
    ids = job_ids_from_text(_history_blob(data))
    last = ""
    in_flight = ""
    for job_id in ids:
        last = job_id
        job = get_mcp_image_job(job_id)
        if job and str(getattr(job, "status", "") or "") in (
            "coding",
            "queued",
            "running",
        ):
            in_flight = job_id
    return in_flight or last or job_id_from_text(_history_blob(data))


def _job_uses_curl(job) -> bool:
    dests = [getattr(item, "output_path", "") for item in (getattr(job, "items", None) or [])]
    return any(dest and dest != "images/generated.png" for dest in dests)


def _shell_name(data: ChatCompletionRequest) -> str:
    from common.image_paths import match_tool_name

    names: list[str] = []
    for spec in data.tools or []:
        func = getattr(spec, "function", None)
        name = getattr(func, "name", None) if func is not None else getattr(spec, "name", None)
        if name:
            names.append(str(name))
    return match_tool_name(names, ("shell", "bash", "run_in_terminal", "terminal")) or "Shell"


def _append_user_facts(data: ChatCompletionRequest, facts: str) -> None:
    if not facts:
        return
    for message in reversed(data.messages or []):
        if message.role != "user":
            continue
        content = message.content
        if isinstance(content, str):
            if facts in content:
                return
            message.content = content.rstrip() + "\n" + facts
            return
        return


def _inject_dest_facts(data: ChatCompletionRequest, job) -> None:
    _append_user_facts(data, _workspace_dest_facts(job))


def _inject_planned_dests(data: ChatCompletionRequest, items: list[dict[str, str]]) -> None:
    _append_user_facts(data, planned_dest_fact_list(items))


def _assistant_message(response):
    choices = getattr(response, "choices", None) or []
    if not choices:
        return None
    return getattr(choices[0], "message", None)


def _file_write_pairs(message) -> list[tuple[str, dict]]:
    from common.image_paths import match_tool_name

    return [
        (name, args)
        for name, args in _tool_call_pairs(message)
        if match_tool_name([name], FILE_WRITE_NAMES)
    ]


def _job_plan_items(job) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for item in getattr(job, "items", None) or []:
        dest = str(getattr(item, "output_path", "") or "").strip()
        if dest:
            items.append(
                {
                    "prompt": str(getattr(item, "prompt", "") or ""),
                    "output_path": dest,
                }
            )
    return items


def _tool_write_path(args: dict) -> str:
    raw = args.get("path") or args.get("filename") or args.get("file") or ""
    return str(raw).replace("\\", "/").lstrip("/")


def _page_write_paths(message) -> list[str]:
    paths: list[str] = []
    for _name, args in _file_write_pairs(message):
        path = _tool_write_path(args)
        if Path(path).suffix.lower() in {".html", ".htm", ".css", ".js", ".mjs"}:
            paths.append(path)
    return paths


def _workspace_has_page_files(job) -> bool:
    owner = str(getattr(job, "owner", "") or "")
    chat_id = str(getattr(job, "chat_id", "") or "")
    return _pages_on_disk(owner, chat_id)


def _pages_on_disk(owner: str | None, chat_id: str | None) -> bool:
    if not owner or not chat_id:
        return False
    try:
        from ui.workspace import list_files

        rows = list_files(owner, chat_id)
    except Exception:
        return False
    for row in rows or []:
        path = str(row.get("path") or "")
        if Path(path).suffix.lower() in {".html", ".htm", ".css", ".js", ".mjs"}:
            return True
    return False


def _requested_page_paths(data) -> list[str]:
    from common.phrase_switch import last_user_text

    text = last_user_text(data) or ""
    return [m.group(1).replace("\\", "/") for m in _NAMED_HTML_RE.finditer(text)]


def _requested_pages_on_disk(owner: str | None, chat_id: str | None, data) -> bool:
    """True when named HTML dests exist; if none named, any page file counts."""
    required = _requested_page_paths(data)
    if not required:
        return _pages_on_disk(owner, chat_id)
    return not _missing_requested_pages(owner, chat_id, data)


def _dests_already_on_page(
    owner: str | None, chat_id: str | None, items: list[dict[str, str]]
) -> bool:
    """True when every planned dest is already named in the page files."""
    dests = [str(row.get("output_path") or "").strip() for row in items or []]
    dests = [dest for dest in dests if dest]
    if not dests or not owner or not chat_id:
        return False
    try:
        from ui.workspace import page_references_dests

        return page_references_dests(owner, chat_id, dests)
    except Exception:
        return False


def _ask_needs_page_wire(data: ChatCompletionRequest) -> bool:
    from common.phrase_switch import last_user_text

    return bool(_PAGE_WIRE_RE.search(last_user_text(data) or ""))


def _missing_requested_pages(
    owner: str | None, chat_id: str | None, data
) -> list[str]:
    """Named HTML dests from the user prompt that are not on disk yet."""
    required = _requested_page_paths(data)
    if not required:
        return []
    if not owner or not chat_id:
        return list(required)
    try:
        from ui.workspace import list_files

        rows = list_files(owner, chat_id)
    except Exception:
        return list(required)
    have = {str(row.get("path") or "").replace("\\", "/") for row in rows or []}
    have_names = {Path(p).name.lower() for p in have}
    missing: list[str] = []
    for rel in required:
        name = Path(rel).name.lower()
        if rel not in have and name not in have_names:
            missing.append(rel)
    return missing


def _inject_missing_requested_pages(data: ChatCompletionRequest, job) -> None:
    owner = str(getattr(job, "owner", "") or "")
    chat_id = str(getattr(job, "chat_id", "") or "")
    missing = _missing_requested_pages(owner, chat_id, data)
    if not missing:
        return
    listed = ", ".join(f"`{path}`" for path in missing)
    _append_user_facts(
        data,
        "These page files are still missing: "
        f"{listed}. Write each of them now with Write. Do not stop for "
        "pictures until they exist.",
    )


_LINKED_PAGE_RE = re.compile(
    r"""(?i)(?:href|src)\s*=\s*["']([^"']+\.(?:css|js|mjs))["']"""
)


def _normalize_page_ref(html_path: str, ref: str) -> str:
    raw = str(ref or "").strip().split("#", 1)[0].split("?", 1)[0]
    if not raw or raw.startswith(("http://", "https://", "//", "data:", "blob:")):
        return ""
    raw = raw.lstrip("/")
    base = posixpath.dirname(str(html_path or "").replace("\\", "/"))
    joined = posixpath.normpath(posixpath.join(base, raw) if base else raw)
    return joined.lstrip("./")


def _missing_linked_page_files(job) -> list[str]:
    """Local CSS/JS that HTML already links but the workspace does not have."""
    owner = str(getattr(job, "owner", "") or "")
    chat_id = str(getattr(job, "chat_id", "") or "")
    if not owner or not chat_id:
        return []
    try:
        from ui.workspace import list_files, read_text

        rows = list_files(owner, chat_id) or []
    except Exception:
        return []
    existing = {str(row.get("path") or "") for row in rows}
    missing: list[str] = []
    for row in rows:
        path = str(row.get("path") or "")
        if Path(path).suffix.lower() not in {".html", ".htm"}:
            continue
        try:
            html = read_text(owner, chat_id, path)
        except Exception:
            continue
        if not html or html.startswith("[binary "):
            continue
        for match in _LINKED_PAGE_RE.finditer(html):
            rel = _normalize_page_ref(path, match.group(1))
            if rel and rel not in existing and rel not in missing:
                missing.append(rel)
    return missing


def _inject_missing_page_files(data: ChatCompletionRequest, job) -> None:
    missing = _missing_linked_page_files(job)
    if not missing:
        return
    listed = ", ".join(f"`{path}`" for path in missing)
    _append_user_facts(
        data,
        "The page links these files that are not on disk yet: "
        f"{listed}. Write each of them now with Write. Do not stop for "
        "pictures until they exist.",
    )


def _inject_unwired_dests(data: ChatCompletionRequest, job) -> None:
    owner = str(getattr(job, "owner", "") or "")
    chat_id = str(getattr(job, "chat_id", "") or "")
    dests = [
        str(row.get("output_path") or "").strip()
        for row in _job_plan_items(job)
        if str(row.get("output_path") or "").strip()
    ]
    if not dests or not owner or not chat_id:
        return
    try:
        from ui.workspace import unreferenced_dests

        missing = unreferenced_dests(owner, chat_id, dests)
    except Exception:
        missing = dests
    if not missing:
        return
    listed = ", ".join(f"`{path}`" for path in missing)
    _append_user_facts(
        data,
        "These image dests are not used on the page yet: "
        f"{listed}. Update the existing HTML/CSS now (img src or CSS url(), "
        "including opacity/size they asked for) with Write or StrReplace. "
        "Do not stop for pictures until the page references them.",
    )


def _job_workspace_ids(job) -> tuple[str, str]:
    return (
        str(getattr(job, "owner", "") or ""),
        str(getattr(job, "chat_id", "") or ""),
    )


def _bound_workspace(job) -> bool:
    owner, chat_id = _job_workspace_ids(job)
    return bool(owner and chat_id)


def _history_needs_page(data) -> bool:
    """True when an earlier user line asked for a page, not only pictures."""
    from common.phrase_switch import is_coding_task

    for message in getattr(data, "messages", None) or []:
        if (getattr(message, "role", "") or "").lower() != "user":
            continue
        text = _content_text(getattr(message, "content", None))
        if is_coding_task(text) or _CREATE_SITE_RE.search(text or ""):
            return True
    return False


def _workspace_page_ready(job, data=None) -> bool:
    owner, chat_id = _job_workspace_ids(job)
    if not owner or not chat_id:
        return False
    if _pages_on_disk(owner, chat_id):
        return True
    return _dests_already_on_page(owner, chat_id, _job_plan_items(job))


def _page_blocking_comfy(job, data=None) -> bool:
    """Hold Comfy while a Code workspace still has no HTML for a page ask."""
    from common.phrase_switch import IMAGE_GEN_RE, is_coding_task, last_user_text

    if not _bound_workspace(job):
        return False
    if _workspace_page_ready(job, data):
        return False
    if data is not None and _history_needs_page(data):
        return True
    ask = last_user_text(data) or "" if data is not None else ""
    if IMAGE_GEN_RE.match(ask) and not is_coding_task(ask):
        return False
    return True


def _should_launch_mixed_render(code_response, job, data=None) -> bool:
    """Comfy only after a real code pass, and after a page workspace has files."""
    if code_response is None:
        return False
    return not _page_blocking_comfy(job, data)


def _profile_writes_files() -> bool:
    from common.phrase_switch import container_parses_tools, profile_is_thinking_only

    if profile_is_thinking_only():
        return False
    return container_parses_tools()


def _keep_writing_page(code_response, job, data=None) -> bool:
    if int(getattr(job, "code_turns", 0) or 0) >= MAX_CODE_TURNS:
        return False
    missing_css = _missing_linked_page_files(job)
    owner = str(getattr(job, "owner", "") or "")
    chat_id = str(getattr(job, "chat_id", "") or "")
    missing_html = _missing_requested_pages(owner, chat_id, data) if data is not None else []
    unwired = not _dests_already_on_page(owner, chat_id, _job_plan_items(job))
    if not code_response:
        return False
    message = _assistant_message(code_response)
    if not _file_write_pairs(message):
        return bool(unwired and _tool_call_pairs(message))
    pages = _page_write_paths(message)
    if not pages:
        return not _workspace_has_page_files(job) or unwired
    written = list(getattr(job, "written_pages", None) or [])
    new_pages = [path for path in pages if path not in written]
    if new_pages:
        job.written_pages = written + new_pages
        return True
    return bool(missing_css or missing_html or unwired)


async def _write_page_then_maybe_launch(data, job, disconnect_handler):
    """Another coding completion. Hold while named HTML or linked CSS/JS are missing."""
    _inject_missing_page_files(data, job)
    _inject_missing_requested_pages(data, job)
    note_coding_progress(job)
    code_response = await _write_site_code(data, disconnect_handler)
    if code_response is None:
        return None, False
    if _keep_writing_page(code_response, job, data):
        return code_response, False
    can_retry = int(getattr(job, "code_turns", 0) or 0) < MAX_CODE_TURNS
    if _missing_linked_page_files(job) and can_retry:
        _inject_missing_page_files(data, job)
        extra = await _write_site_code(data, disconnect_handler)
        if extra is None:
            return code_response, False
        code_response = extra
        if _keep_writing_page(code_response, job, data):
            return code_response, False
    owner = str(getattr(job, "owner", "") or "")
    chat_id = str(getattr(job, "chat_id", "") or "")
    if _missing_requested_pages(owner, chat_id, data) and can_retry:
        _inject_missing_requested_pages(data, job)
        extra = await _write_site_code(data, disconnect_handler)
        if extra is None:
            return code_response, False
        code_response = extra
        if _keep_writing_page(code_response, job, data):
            return code_response, False
    if not _dests_already_on_page(owner, chat_id, _job_plan_items(job)) and can_retry:
        _inject_unwired_dests(data, job)
        extra = await _write_site_code(data, disconnect_handler)
        if extra is None:
            return code_response, False
        code_response = extra
        if _keep_writing_page(code_response, job, data):
            return code_response, False
        message = _assistant_message(code_response)
        if _tool_call_pairs(message):
            return code_response, False
    return code_response, _should_launch_mixed_render(code_response, job, data)


def _first_code_pass_holds_llm(code_response, *, page_ready: bool = False) -> bool:
    """Hold Comfy for page tools. Skip only for a pure dest replace."""
    return not page_ready


def _tool_call_pairs(message) -> list[tuple[str, dict]]:
    pairs: list[tuple[str, dict]] = []
    for call in getattr(message, "tool_calls", None) or []:
        func = getattr(call, "function", None)
        name = getattr(func, "name", None) if func is not None else None
        if not name:
            continue
        raw = getattr(func, "arguments", "") or ""
        if isinstance(raw, dict):
            args = raw
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"arguments": raw}
            args = parsed if isinstance(parsed, dict) else {"value": parsed}
        pairs.append((str(name), args))
    return pairs


def _stamp_job_content(content: Optional[str], job) -> str:
    mark = f"{JOB_MARK} {job.id}"
    body = (content or "").strip()
    if JOB_MARK in body:
        return body
    if body:
        return f"{mark}\n{body}"
    return mark


def _items_finished(job) -> bool:
    items = list(getattr(job, "items", None) or [])
    if not items:
        return str(getattr(job, "status", "") or "") in ("done", "error")
    return all(
        str(getattr(item, "status", "") or "") in ("done", "error") for item in items
    )


def _editor_files_ready(job) -> bool:
    status = str(getattr(job, "status", "") or "")
    if status in ("done", "error"):
        return True
    if not _items_finished(job):
        return False
    items = list(getattr(job, "items", None) or [])
    if items and all(
        str(getattr(item, "status", "") or "") == "error" for item in items
    ):
        return True
    return bool(living_download_pairs(job))


def _hold_is_ready(job, *, files_only: bool) -> bool:
    status = str(getattr(job, "status", "") or "")
    if status in ("done", "error"):
        return True
    if files_only:
        return _editor_files_ready(job)
    return False


async def _wait_for_hold(job, *, files_only: bool) -> None:
    while not _hold_is_ready(job, files_only=files_only):
        await wait_mcp_job_progress(job, HOLD_KEEPALIVE_S)


def _job_dests(job) -> list[str]:
    from images.paths import safe_rel_png_path

    pairs = living_download_pairs(job)
    if pairs:
        return [dest for _url, dest in pairs]
    dests: list[str] = []
    for item in getattr(job, "items", None) or []:
        dest = safe_rel_png_path(getattr(item, "output_path", "") or "")
        if dest:
            dests.append(dest)
    return dests


def _message_has_curl(message) -> bool:
    if "curl " in _content_text(getattr(message, "content", None)):
        return True
    for _name, args in _tool_call_pairs(message):
        blob = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
        if "curl " in blob:
            return True
    return False


def _last_assistant_message(data: ChatCompletionRequest):
    for message in reversed(data.messages or []):
        if (message.role or "").lower() == "assistant":
            return message
    return None


def _last_assistant_was_curl(data: ChatCompletionRequest) -> bool:
    message = _last_assistant_message(data)
    return bool(message and _message_has_curl(message))


def _curl_assistant_count(data: ChatCompletionRequest) -> int:
    count = 0
    for message in reversed(data.messages or []):
        role = (message.role or "").lower()
        if role in ("tool", "function"):
            continue
        if role == "assistant":
            if _message_has_curl(message):
                count += 1
                continue
            break
        break
    return count


def _trailing_tool_blob(data: ChatCompletionRequest) -> str:
    parts: list[str] = []
    for message in reversed(data.messages or []):
        if (message.role or "").lower() not in ("tool", "function"):
            break
        parts.append(_content_text(message.content))
    return "\n".join(reversed(parts))


def _curl_tool_followup(data: ChatCompletionRequest, job, *, console: bool):
    from common.phrase_switch import text_response

    if console or job is None:
        return None
    if not _last_assistant_was_curl(data):
        return None
    status = str(getattr(job, "status", "") or "")
    if not _editor_files_ready(job) and status not in ("done", "error"):
        return None
    dests = _job_dests(job)
    mark = f"{JOB_MARK} {job.id}"
    if not dests:
        err = getattr(job, "error", None) or (
            "Image generation finished with no files on this host."
        )
        return text_response(data, f"{mark}\n{err}")
    blob = _trailing_tool_blob(data)
    if tool_result_has_pngs(blob, dests):
        return text_response(data, f"{mark}\nImages saved to {', '.join(dests)}.")
    if _curl_assistant_count(data) >= 2:
        return text_response(
            data,
            f"{mark}\nCould not download {', '.join(dests)}. "
            "The PNGs are on the GPU host; download those URLs by hand.",
        )
    return _curl_response(data, job)


def _response_from_stream_payloads(payloads: list[dict], model_name: str):
    from endpoints.OAI.types.chat_completion import (
        ChatCompletionMessage,
        ChatCompletionRespChoice,
        ChatCompletionResponse,
    )

    content = ""
    reasoning = ""
    tool_calls = None
    finish = "stop"
    name = model_name
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if payload.get("model"):
            name = str(payload.get("model") or name)
        choice = (payload.get("choices") or [{}])[0] or {}
        delta = choice.get("delta") or {}
        message = choice.get("message") or {}
        piece = delta.get("content") or message.get("content") or ""
        think = delta.get("reasoning_content") or message.get("reasoning_content") or ""
        tools = delta.get("tool_calls") or message.get("tool_calls")
        if piece:
            content += str(piece)
        if think:
            reasoning += str(think)
        if tools:
            tool_calls = tools
        reason = choice.get("finish_reason")
        if reason:
            finish = str(reason)
    return ChatCompletionResponse(
        model=name or "gpt-4o",
        choices=[
            ChatCompletionRespChoice(
                finish_reason=finish,
                message=ChatCompletionMessage(
                    role="assistant",
                    content=content or None,
                    reasoning_content=reasoning or None,
                    tool_calls=tool_calls,
                ),
            )
        ],
    )


async def _write_site_code(data: ChatCompletionRequest, disconnect_handler):
    """One coding completion while the LLM is still loaded. None if it cannot run."""
    from common import model
    from common.assistant_text import strip_response_apologies
    from common.networking import DisconnectHandler, generation_request
    from endpoints.OAI.utils.chat_completion import (
        apply_chat_template,
        generate_chat_completion,
        stream_generate_chat_completion,
    )
    from ui.flight import current_console_flight, publish_console_status

    container = getattr(model, "container", None)
    if not container or not getattr(container, "loaded", False):
        xlogger.info("Mixed chat code pass skipped: no loaded model")
        return None
    if getattr(container, "prompt_template", None) is None:
        return None
    model_path = getattr(container, "model_dir", None)
    if model_path is None:
        return None
    request = generation_request(
        getattr(disconnect_handler, "request", None) if disconnect_handler else None
    )
    nested = DisconnectHandler(
        request=request,
        description="mixed site code",
        abort_event=getattr(disconnect_handler, "abort_event", None),
    )
    await publish_console_status("Writing the page")
    flight = current_console_flight()
    try:
        if flight is None:
            sync = data.model_copy(update={"stream": False, "n": 1})
            prompt, embeddings = await apply_chat_template(sync)
            response = await generate_chat_completion(
                prompt, embeddings, sync, request, model_path, nested
            )
            return strip_response_apologies(response)
        streamed = data.model_copy(update={"stream": True, "n": 1})
        prompt, embeddings = await apply_chat_template(streamed)
        payloads: list[dict] = []
        async for chunk in stream_generate_chat_completion(
            prompt, embeddings, streamed, request, model_path, nested
        ):
            if chunk == "[DONE]" or not isinstance(chunk, str):
                continue
            text = chunk.strip()
            if not text.startswith("{"):
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            await flight.publish(text)
            flight.streamed_live = True
            payloads.append(payload)
        response = _response_from_stream_payloads(payloads, getattr(model_path, "name", "") or "")
        return strip_response_apologies(response)
    except Exception as exc:
        xlogger.warning(f"Mixed chat code pass failed: {exc}")
        return None


async def _start_mixed_job(
    items: list[dict[str, str]],
    api_base: str,
    *,
    start: bool = True,
    owner: str | None = None,
    chat_id: str | None = None,
):
    job, kind = await start_mcp_image_job(
        items=items,
        seed=None,
        restore=True,
        api_base=api_base or "",
        delay=0.0,
        start=start,
        owner=owner,
        chat_id=chat_id,
    )
    xlogger.info(f"Mixed chat queued image job {job.id} ({kind}, {len(items)} dests)")
    return job


async def _launch_mixed_job(job):
    launched = await launch_mcp_image_job(job)
    xlogger.info(f"Mixed chat started Comfy for job {job.id}")
    return launched


async def _start_prompt_job(
    prompt: str,
    api_base: str,
    *,
    restore: bool,
    source_image=None,
    denoise=None,
    owner: str | None = None,
    chat_id: str | None = None,
):
    item: dict = {"prompt": prompt, "output_path": "images/generated.png"}
    from common.image_prompts import infer_image_size

    found_size = infer_image_size(prompt)
    if found_size:
        item["size"] = found_size
    if source_image is not None:
        item["source_image"] = str(Path(source_image))
    if denoise is not None:
        item["denoise"] = denoise
    items = [item]
    job, kind = await start_mcp_image_job(
        items=items,
        seed=None,
        restore=restore,
        api_base=api_base or "",
        delay=0.0,
        owner=owner,
        chat_id=chat_id,
    )
    xlogger.info(f"Chat image job {job.id} ({kind})")
    return job


def _assistant_already_has_dest_hint(data: ChatCompletionRequest) -> bool:
    """True when a prior assistant turn already listed the planned PNG dests."""
    for message in data.messages or []:
        if (getattr(message, "role", "") or "").lower() != "assistant":
            continue
        text = _content_text(getattr(message, "content", None))
        if "Do not Write PNG" in text or "Point img src" in text:
            return True
    return False


def _code_reply(data: ChatCompletionRequest, job, code_response):
    from common.phrase_switch import text_response, tool_call_response

    message = _assistant_message(code_response)
    if message is None:
        return text_response(data, f"{JOB_MARK} {job.id}")
    content = _stamp_job_content(getattr(message, "content", None), job)
    dests = _job_dests(job)
    hinted = _assistant_already_has_dest_hint(data)
    turns = int(getattr(job, "code_turns", 0) or 0)
    hint = ""
    if dests and not hinted and turns <= 1:
        hint = (
            f" Point img src or CSS url() at these exact paths: {', '.join(dests)}. "
            "Write HTML, CSS, and JS only — if the HTML links styles.css or "
            "app.js, Write those files before pictures. Do not Write PNG, WebP, "
            "or placeholder image files."
        )
    if content == f"{JOB_MARK} {job.id}":
        if hint:
            content = (
                f"{content}\nWrite the page now. The GPU will render the images "
                f"after these files are written.{hint}"
            )
    elif hint and "Do not Write PNG" not in content:
        content = content.rstrip() + "\n" + hint.strip()
    calls = _tool_call_pairs(message)
    payload = _console_live_reply_data(data)
    if calls:
        return tool_call_response(payload, calls, content=content)
    return text_response(payload, content)


def _console_live_reply_data(data: ChatCompletionRequest) -> ChatCompletionRequest:
    """After a live mixed-code stream, return an object the console pump can split.

    Re-sending the whole SSE would repeat the page text and then immediately
    POST the Write body on a connection that is still tearing down.
    """
    from ui.flight import current_console_flight

    flight = current_console_flight()
    if flight and getattr(flight, "streamed_live", False) and data.stream:
        return data.model_copy(update={"stream": False})
    return data


def _curl_response(data: ChatCompletionRequest, job, code_response=None):
    from common.phrase_switch import text_response, tool_call_response

    pairs = living_download_pairs(job)
    mark = f"{JOB_MARK} {job.id}"
    parts = [mark]
    code_message = _assistant_message(code_response) if code_response else None
    code_content = getattr(code_message, "content", None) if code_message else None
    if isinstance(code_content, str) and code_content.strip():
        if JOB_MARK not in code_content:
            parts.append(code_content.strip())
        elif code_content.strip() != mark:
            parts = [code_content.strip()]
    if not pairs:
        err = job.error or "Image generation finished with no files on this host."
        parts.append(err)
        calls = _tool_call_pairs(code_message) if code_message else []
        body = "\n".join(parts)
        if calls:
            return tool_call_response(data, calls, content=body)
        return text_response(data, body)
    parts.append(image_download_note(pairs))
    command = image_download_command(pairs)
    calls = _tool_call_pairs(code_message) if code_message else []
    if command:
        calls.append((_shell_name(data), {"command": command}))
    body = "\n".join(parts)
    if calls:
        return tool_call_response(data, calls, content=body)
    return text_response(data, body)


def job_progress_line(job) -> str:
    """Short live status for the management UI (and SSE comments)."""
    phase = str(getattr(job, "phase", "") or getattr(job, "status", "") or "")
    count = int(getattr(job, "count", 0) or 0)
    index = int(getattr(job, "current_index", 0) or 0) + 1
    if phase == "queued":
        return "Queued"
    if phase in ("writing_code", "coding"):
        return "Planning the picture"
    if phase == "starting_comfy":
        return "Starting Comfy"
    if phase in ("generating", "running"):
        if count > 1:
            return f"Rendering image {min(index, count)} of {count}"
        return "Rendering in Comfy"
    if phase == "restoring_llm":
        return "Reloading the coding model"
    if str(getattr(job, "status", "") or "") == "error":
        return "Image generation failed"
    return ""


def _console_ready_text(
    job,
    api_base: Optional[str],
    *,
    code: bool = False,
    extra_files: Optional[list[str]] = None,
) -> str:
    from common.phrase_switch import image_job_done_text

    pairs = living_download_pairs(job)
    n = len(pairs)
    lead = "Here's the picture." if n == 1 else f"Here are the {n} pictures."
    lines = [lead, ""]
    for url, dest in pairs:
        label = dest if code and dest else ""
        lines.append(f"![{label}]({url})")
        lines.append("")
    lines.append(image_job_done_text(job=job, count=max(1, n)))
    if code:
        dests = [dest for _url, dest in pairs]
        names = []
        workspace_files = list(extra_files) if extra_files is not None else dests
        for name in workspace_files:
            if name and name not in names:
                names.append(name)
        if names:
            lines.append("They're also in this chat's Files: " + ", ".join(names) + ".")
        else:
            lines.append("It's also in Gallery and in this chat's Files.")
    else:
        lines.append(
            "It's also in Gallery. Describe another picture to generate it, "
            "or switch models from Status."
        )
    return "\n".join(lines).strip()


def _copy_workspace_pngs(job, workspace) -> list[str]:
    copied = copy_job_to_workspace(job)
    if copied:
        return copied
    if not workspace:
        return []
    owner, chat_id = workspace
    if not owner or not chat_id:
        return []
    if not str(getattr(job, "chat_id", "") or "").strip():
        job.chat_id = chat_id
    if not str(getattr(job, "owner", "") or "").strip():
        job.owner = owner
    return copy_job_to_workspace(job)


def _url_response(
    data: ChatCompletionRequest,
    job,
    api_base: Optional[str],
    *,
    console: bool = False,
    code: bool = False,
    extra_files: Optional[list[str]] = None,
):
    from common.phrase_switch import image_ready_response, text_response

    pairs = living_download_pairs(job)
    mark = f"{JOB_MARK} {job.id}"
    if not pairs:
        err = job.error or "Image generation finished with no files on this host."
        if console:
            return text_response(data, err)
        return text_response(data, f"{mark}\n{err}")
    if console:
        return text_response(
            data,
            _console_ready_text(job, api_base, code=code, extra_files=extra_files),
        )
    names = [url.rsplit("/", 1)[-1].split("?", 1)[0] for url, _dest in pairs]
    response = image_ready_response(
        data,
        names[-1],
        api_base=api_base,
        restore=bool(job.restore),
        count=len(names),
        filenames=names,
    )
    message = response.choices[0].message
    body = message.content or ""
    if JOB_MARK not in body:
        message.content = f"{mark}\n{body}"
    return response


async def _hold_then_reply(
    data: ChatCompletionRequest,
    job,
    *,
    mixed: bool,
    api_base: Optional[str],
    code_response=None,
    console: bool = False,
    workspace=None,
    extra_files: Optional[list[str]] = None,
):
    from common.phrase_switch import stream_chat_delta, stream_text, stream_tool_calls

    sync = data.model_copy(update={"stream": False})
    mixed = False if console else mixed
    code = bool(workspace)
    files_only = not console

    def _reply():
        copied = _copy_workspace_pngs(job, workspace)
        names = list(extra_files or []) + copied
        if mixed:
            return _curl_response(sync, job, code_response)
        return _url_response(
            sync, job, api_base, console=console, code=code, extra_files=names
        )

    async def _body():
        chunk_id = f"chatcmpl-{uuid4().hex}"
        created = int(time.time())
        yield ServerSentEvent(comment=f"{JOB_MARK} {job.id}")
        last_line = job_progress_line(job)
        if last_line:
            yield ServerSentEvent(comment=f"{STATUS_MARK} {last_line}")
        if not console:
            first = (last_line + "\n") if last_line else " "
            yield stream_chat_delta(
                data,
                {"role": "assistant", "content": first},
                chunk_id=chunk_id,
                created=created,
            )
        async def _emit_reply():
            reply = _reply()
            message = reply.choices[0].message
            if message.tool_calls:
                async for chunk in stream_tool_calls(
                    data, message, chunk_id=chunk_id, created=created
                ):
                    yield chunk
            else:
                async for chunk in stream_text(
                    data, message.content or "", chunk_id=chunk_id, created=created
                ):
                    yield chunk

        emitted = False
        try:
            while not _hold_is_ready(job, files_only=files_only):
                await wait_mcp_job_progress(job, HOLD_KEEPALIVE_S)
                line = job_progress_line(job)
                if line and line != last_line:
                    last_line = line
                    yield ServerSentEvent(comment=f"{STATUS_MARK} {line}")
                    if not console:
                        yield stream_chat_delta(
                            data,
                            {"content": line + "\n"},
                            chunk_id=chunk_id,
                            created=created,
                        )
                elif not console:
                    yield stream_chat_delta(
                        data, {"content": " "}, chunk_id=chunk_id, created=created
                    )
            emitted = True
            async for chunk in _emit_reply():
                yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            if not emitted and _hold_is_ready(job, files_only=files_only):
                async for chunk in _emit_reply():
                    yield chunk
            raise

    if data.stream:
        return EventSourceResponse(
            _body(),
            ping=get_sse_ping_interval(),
            sep="\n",
        )
    if console:
        await wait_until_done(job)
    else:
        await _wait_for_hold(job, files_only=True)
    return _reply()




def _attached_project_image_rel(text: str) -> str:
    matches = _ATTACHED_PROJECT_IMAGE_RE.findall(text or "")
    return str(matches[-1]).strip() if matches else ""


def _resolve_edit_source(source_image, owner: str | None, chat_id: str | None, text: str):
    if source_image is not None:
        path = Path(source_image)
        if path.is_file():
            return path
    rel = _attached_project_image_rel(text)
    if rel and owner and chat_id:
        from ui.workspace import resolve_file

        try:
            return resolve_file(owner, chat_id, rel)
        except ValueError:
            return None
    return None


async def handle(
    data: ChatCompletionRequest,
    api_base: Optional[str] = None,
    *,
    source_image=None,
    llm_ready: bool = True,
    gpu_is_comfy: bool = False,
    disconnect_handler=None,
    console: bool = False,
    owner: str | None = None,
    code: bool = False,
    chat_id: str | None = None,
    agent: str = "agent",
):
    """Mixed generate writes the page first, then holds until PNGs exist.

    File-write tool calls go out while the LLM stays loaded. Comfy starts
    only after a coding turn has no more file tools, and only when this
    turn explicitly asks for new rasters. Existing workspace or job dests
    are reuse. Always wins over the 9B while this conversation's job is
    still running. Resume only via ``tabby-image-job: <uuid>`` in this
    conversation — never by attaching a global coding job. A leftover
    coding job from another chat is abandoned so a new turn can start.

    console=True (management UI) still generates images but never emits
    a download curl. code=True plus a workspace copies finished PNGs into
    the per-chat host folder after the browser writes the page.

    agent=ask|plan (UI Code mode) does not start a new Comfy job. In-flight
    jobs still hold until they finish.
    """
    from common.phrase_switch import (
        IMAGE_GEN_RE,
        border_edit_prompt,
        last_user_text,
        refuses_new_images,
        requested_image_prompt,
        text_response,
        turn_needs_image_classify,
        wants_border_trim,
    )

    workspace = (owner, chat_id) if code and owner and chat_id else None
    job_id = job_id_from_history(data)
    job = get_mcp_image_job(job_id) if job_id else None
    if workspace and (job is None or str(getattr(job, "status", "") or "") in ("done", "error")):
        busy = active_mcp_image_job()
        if (
            busy
            and str(getattr(busy, "status", "") or "") in ("coding", "queued", "running")
            and str(getattr(busy, "owner", "") or "") == owner
            and str(getattr(busy, "chat_id", "") or "") == chat_id
        ):
            job = busy
            job_id = busy.id
    role = last_role(data)

    if job and role in ("tool", "function"):
        follow = _curl_tool_followup(data, job, console=console)
        if follow is not None:
            return follow

    if job and job.status in ("queued", "running"):
        if workspace:
            bound_owner, bound_chat = workspace
            if bound_owner and (not job.owner or job.owner == bound_owner):
                job.owner = bound_owner
        if chat_id and (not job.chat_id or job.chat_id == chat_id):
            job.chat_id = chat_id
        return await _hold_then_reply(
            data,
            job,
            mixed=False if console else _job_uses_curl(job),
            api_base=api_base,
            console=console,
            workspace=workspace,
        )

    if job and job.status == "coding" and llm_ready:
        if workspace:
            bound_owner, bound_chat = workspace
            if bound_owner and (not job.owner or job.owner == bound_owner):
                job.owner = bound_owner
            if bound_chat and (not job.chat_id or job.chat_id == bound_chat):
                job.chat_id = bound_chat
            if _page_blocking_comfy(job, data) and not _profile_writes_files():
                return None
            _inject_planned_dests(data, _job_plan_items(job))
            code_response, launch = await _write_page_then_maybe_launch(
                data, job, disconnect_handler
            )
            if not launch:
                if code_response is None:
                    return None
                return _code_reply(data, job, code_response)
            await _launch_mixed_job(job)
            if code_response and _file_write_pairs(_assistant_message(code_response)):
                return _code_reply(data, job, code_response)
            return await _hold_then_reply(
                data,
                job,
                mixed=True,
                api_base=api_base,
                code_response=code_response,
                console=True,
                workspace=workspace,
            )
        if console:
            if _page_blocking_comfy(job, data):
                return None
            await _launch_mixed_job(job)
            return await _hold_then_reply(
                data, job, mixed=False, api_base=api_base, console=True
            )
        _inject_planned_dests(data, _job_plan_items(job))
        code_response, launch = await _write_page_then_maybe_launch(
            data, job, disconnect_handler
        )
        if not launch:
            if code_response is None:
                return None
            return _code_reply(data, job, code_response)
        await _launch_mixed_job(job)
        if code_response and _file_write_pairs(_assistant_message(code_response)):
            return _code_reply(data, job, code_response)
        return await _hold_then_reply(
            data,
            job,
            mixed=True,
            api_base=api_base,
            code_response=code_response,
            console=console,
        )

    if role in ("tool", "function"):
        if job and job.status in ("done", "error"):
            _inject_dest_facts(data, job)
        return None

    if _readonly_agent(agent):
        return None

    ask = last_user_text(data) or ""
    if wants_border_trim(ask):
        source = _resolve_edit_source(source_image, owner, chat_id, ask)
        if source is not None:
            started = await _start_prompt_job(
                border_edit_prompt(ask),
                api_base or "",
                restore=bool(llm_ready),
                source_image=source,
                denoise=0.85,
                owner=owner,
                chat_id=chat_id,
            )
            return await _hold_then_reply(
                data,
                started,
                mixed=False,
                api_base=api_base,
                console=console,
                workspace=workspace,
            )

    if refuses_new_images(ask) and not IMAGE_GEN_RE.match(ask):
        return None

    if llm_ready and not turn_needs_image_classify(ask):
        return None

    if llm_ready:
        rasters = workspace_raster_paths(owner, chat_id) if workspace else []
        prior = _classify_prior_facts(job, rasters)
        from ui.flight import publish_console_status

        await publish_console_status("Planning the picture")
        plan = await classify_image_turn(
            data, disconnect_handler=disconnect_handler, prior_facts=prior
        )
        existing = _existing_raster_paths(job, rasters, owner, chat_id)
        plan = _upgrade_missing_named_dests(plan, ask, existing, owner, chat_id)
        plan = _apply_approved_asset_dests(plan, ask)
        if plan.action == "reuse":
            _inject_existing_image_facts(data, job, rasters)
            return None
        if (
            plan.action == "generate"
            and plan.items
            and _all_dests_exist(plan.items, existing, owner, chat_id)
            and not _explicit_new_rasters(data)
        ):
            _inject_existing_image_facts(data, job, rasters)
            return None
        if plan.action == "generate" and plan.items:
            busy = active_mcp_image_job()
            if busy and not job_id:
                if busy.status == "coding":
                    if not abandon_foreign_coding_job(
                        busy, owner=owner or "", chat_id=chat_id or ""
                    ):
                        return text_response(
                            data,
                            f"The stack is already writing a page for job {busy.id}. "
                            "Wait until that chat finishes, then ask again.",
                        )
                else:
                    return text_response(
                        data,
                        f"The GPU is already generating job {busy.id}. "
                        "Wait until that batch finishes, then ask again.",
                    )
            if workspace:
                from common.phrase_switch import is_coding_task

                if is_coding_task(ask) and not _profile_writes_files():
                    return None
                _inject_planned_dests(data, plan.items)
                code_response = await _write_site_code(data, disconnect_handler)
                if code_response is None:
                    return None
                keep = _first_code_pass_holds_llm(
                    code_response,
                    page_ready=_dests_already_on_page(owner, chat_id, plan.items)
                    and _explicit_new_rasters(data)
                    and not _ask_needs_page_wire(data),
                )
                started = await _start_mixed_job(
                    plan.items,
                    api_base or "",
                    start=not keep,
                    owner=owner,
                    chat_id=chat_id,
                )
                if keep:
                    return _code_reply(data, started, code_response)
                return await _hold_then_reply(
                    data,
                    started,
                    mixed=True,
                    api_base=api_base,
                    code_response=code_response,
                    console=True,
                    workspace=workspace,
                )
            if console:
                started = await _start_mixed_job(
                    plan.items,
                    api_base or "",
                    start=True,
                    owner=owner,
                    chat_id=chat_id,
                )
                return await _hold_then_reply(
                    data, started, mixed=False, api_base=api_base, console=True
                )
            _inject_planned_dests(data, plan.items)
            code_response = await _write_site_code(data, disconnect_handler)
            if code_response is None:
                return None
            keep = _first_code_pass_holds_llm(code_response)
            started = await _start_mixed_job(
                plan.items,
                api_base or "",
                start=not keep,
                owner=owner,
                chat_id=chat_id,
            )
            if keep:
                return _code_reply(data, started, code_response)
            return await _hold_then_reply(
                data,
                started,
                mixed=True,
                api_base=api_base,
                code_response=code_response,
                console=console,
            )

    explicit = requested_image_prompt(data, explicit_only=True)
    if llm_ready and explicit:
        started = await _start_prompt_job(
            explicit,
            api_base or "",
            restore=True,
            source_image=source_image,
            owner=owner,
            chat_id=chat_id,
        )
        return await _hold_then_reply(
            data,
            started,
            mixed=False,
            api_base=api_base,
            console=console,
            workspace=workspace,
        )

    if not llm_ready and gpu_is_comfy:
        prompt = requested_image_prompt(data)
        if not prompt and source_image is not None:
            from common.phrase_switch import last_user_text, looks_like_chat_not_image

            text = last_user_text(data).strip()
            if not looks_like_chat_not_image(text):
                prompt = text or "cartoon style"
        if prompt:
            started = await _start_prompt_job(
                prompt,
                api_base or "",
                restore=False,
                source_image=source_image,
                owner=owner,
                chat_id=chat_id,
            )
            return await _hold_then_reply(
                data,
                started,
                mixed=False,
                api_base=api_base,
                console=console,
                workspace=workspace,
            )

    if not llm_ready:
        if job and job.status == "coding":
            if _page_blocking_comfy(job, data):
                return None
            await _launch_mixed_job(job)
            return await _hold_then_reply(
                data, job, mixed=True, api_base=api_base, console=console
            )
        busy = active_mcp_image_job()
        if busy and busy.status in ("queued", "running"):
            return text_response(
                data,
                f"Images are still rendering (job {busy.id}). "
                "Wait for the download curl in the chat that started that job.",
            )

    return None
