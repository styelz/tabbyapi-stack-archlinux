"""Detect Cursor chat phrases for listing and switching TabbyAPI models."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from uuid import uuid4

from ruamel.yaml import YAML
from sse_starlette import EventSourceResponse

from common.gpu_mode import (
    GPU_ALIASES,
    public_api_base,
    public_image_url,
    read_mode,
    recent_generated_files,
)
from common.logger import xlogger
from common.networking import get_sse_ping_interval
from common.pasted_images import is_save_image_request, pasted_download_text
from common.switch_times import (
    extra_seconds,
    format_duration,
    gpu_label,
    profile_error,
    ready_seconds,
    wait_hint,
)
from endpoints.OAI.types.chat_completion import (
    ChatCompletionMessage,
    ChatCompletionMessagePart,
    ChatCompletionRequest,
    ChatCompletionRespChoice,
    ChatCompletionResponse,
    ChatCompletionStreamChoice,
    ChatCompletionStreamChunk,
)
from endpoints.OAI.types.tools import Tool, ToolCall

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
PROFILES_DIR = ROOT / "model_profiles"
PYTHON = (
    ROOT
    / "venv"
    / ("Scripts" if os.name == "nt" else "bin")
    / ("python.exe" if os.name == "nt" else "python")
)
SWITCHER = ROOT / "switch_model.py"
RESTARTER = ROOT / "restart_stack.py"
LOG = ROOT / "switch-model.log"
LOCK = ROOT / "switch-model.lock"
# Hold the HTTP request so a client cannot 1 Hz-loop while a model loads.
LLM_NOT_READY_WAIT_S = 5

SWITCH_RE = re.compile(
    r"(?is)^\s*/?(?:please\s+)?(?:switch(?:\s+to)?|use)\s+(\S+)(?:\s+now)?[\s!.]*$"
)
LIST_RE = re.compile(r"(?is)^\s*/?(?:please\s+)?(?:list|show|available)\s+models?[\s!.]*$")
HELP_RE = re.compile(r"(?is)^\s*/?(?:please\s+)?help(?:\s+please)?[\s!.?]*$")
RESTART_RE = re.compile(
    r"(?is)^\s*/?(?:please\s+)?"
    r"restart(?:\s+(?:the\s+)?(?:stack|api|tabby(?:api)?|server|service))?"
    r"(?:\s+now)?[\s!.]*$"
)
SLASH_PROFILE_RE = re.compile(r"(?is)^\s*/(?P<name>[A-Za-z][\w.-]*)\s*$")
_SLASH_RESERVED = frozenset({"help", "restart", "list", "models", "image", "generate"})
IMAGE_GEN_RE = re.compile(
    r"(?is)^\s*(?:please\s+)?(?:can you\s+|could you\s+)?"
    r"(?:generate|draw|imagine|create|make|render)"
    r"(?:\s+me)?(?:\s+an?)?\s+(?:image|picture|photo|pic)"
    r"(?:\s+of|\s+showing)?\s+(.+?)\s*$"
)
REFUSE_IMAGES_RE = re.compile(
    r"(?is)\b(?:"
    r"do(?:\s+not|n't)\s+(?:generate|draw|create|render|make)\s+"
    r"(?:any\s+)?(?:new\s+)?(?:images?|pictures?|photos?|pics?)|"
    r"no\s+new\s+(?:images?|pictures?|photos?)|"
    r"without\s+(?:any\s+)?(?:new\s+)?(?:images?|pictures?|photos?)"
    r")\b"
)
IMAGE_COUNT_RE = re.compile(
    r"(?is)^\s*(?:please\s+)?(?:can you\s+|could you\s+)?"
    r"(?:generate|draw|imagine|create|make|render|give\s+me)?"
    r"\s*(?P<num>\d+|two|three|four|five)"
    r"\s+(?:different\s+)?(?:images?|pictures?|photos?|pics?)"
    r"(?:\s+of|\s+showing)?\s*(?P<rest>.*)$"
)
_IMAGE_COUNT_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5}
MAX_CHAT_IMAGES = 5
# After unwrap, a real image prompt can be a few thousand chars (Qwen-Image
# posters/UI). Agent dumps are typically 10k+. Keep a cap as a backstop.
MAX_IMAGE_PROMPT_CHARS = 4000
META_IMAGE_RE = re.compile(
    r"(?is)\b("
    r"when i asked|it worked|not what i asked|stuck in a loop|"
    r"showed a preview|over and over|the image is not"
    r")\b"
)
# IDEs (GitHub Copilot, Cursor, ...) send a separate, low-stakes completion
# asking the model to name the conversation/PR, and they typically echo the
# user's own request verbatim inside it. "delete the current logo.png and
# create a new logo png image..." inside that wrapper matches IMAGE_REDO_RE
# just as well as the real ask, so a title request must not be treated as a
# fresh image/redo turn — that queued a second, unwanted Comfy render on top
# of the real one (two items, one job) and briefly wedged the mixed-image
# helpers, since none of them expect the "last user message" to be a title
# prompt rather than the user's actual line.
META_WRAPPER_RE = re.compile(
    r"(?is)^\s*(?:please\s+)?"
    r"(?:write|generate|give|suggest|create)\s+(?:me\s+)?(?:a\s+)?"
    r"(?:brief|short|concise|one[- ]word|few[- ]word|catchy)?\s*"
    r"(?:title|summary|name)\s+for\s+(?:the\s+|this\s+)?(?:following\s+)?"
    r"(?:request|conversation|chat|task|message|prompt)?"
    r"|^\s*(?:please\s+)?summariz\w*\s+(?:the\s+)?(?:following|this)\b"
)


def _is_meta_wrapper_text(text: str) -> bool:
    """True for an IDE's own title/summary request, not a user ask."""
    return bool(text) and bool(META_WRAPPER_RE.match(text.strip()))
AGENT_MARKERS = (
    "<user_query>",
    "<userRequest>",
    "You are Cursor",
    "You are an AI coding assistant",
    "PRIORITY: refuse",
    "Available Tools",
)
COMFY_IDLE = (
    "GPU is on ComfyUI. Describe the image in this chat "
    "(for example: a red bicycle in the rain). "
    "The reply will include a PNG URL on this same API host. "
    "Send switch to qwen when you want the LLM back."
)
SWITCH_LLM_MARK = "tabby-switch-llm"
CHAT_OPENER_RE = re.compile(
    r"(?is)^\s*(?:"
    r"(?:hi|hello|hey|yo|sup|thanks|thank you|thx|"
    r"good (?:morning|afternoon|evening)|"
    r"ok(?:ay)?|sure|yes|no|yep|nope|got it|cool|great"
    r")(?:\s|[!.]|$)"
    r"|(?:please\s+)?(?:tell me|explain|help(?:\s+me)?)\b"
    r"|(?:i(?:'m|m)?\s+(?:just\s+)?(?:have|need|want|think|wonder)|i have a question)\b"
    r"|(?:what(?:'s|s)?|why|who|when|where|which)\b"
    r"|(?:is|are|do|does|did|am)\s+(?:the|this|that|it|there|you|we|they|i|these|those)\b"
    r"|(?:can|could|would|should|will)\s+you\s+(?:explain|tell|help|show me how)\b"
    r"|how\s+(?:are|do|does|did|can|to|is|come)\b"
    r")"
)
CHAT_QUESTION_RE = re.compile(
    r"(?is)^\s*(?:"
    r"(?:what(?:'s|s)?|why|who|when|where|which)\b"
    r"|how\s+(?:are|do|does|did|can|to|is|come)\b"
    r"|(?:can|could|would|should|will)\s+you\s+(?:explain|tell|help|show me how)\b"
    r")"
)


def llm_not_ready_text(*, console: bool = False) -> str:
    if console:
        return (
            "The coding model is not loaded. Switch to an LLM profile from Status, then try again."
        )
    qwen = wait_hint("qwen").lower()
    qwen35 = format_duration(ready_seconds("qwen35"))
    return (
        "No LLM is loaded. This is not an image request and ComfyUI is not involved. "
        f"Send switch to qwen and {qwen} "
        f"(qwen35 can take about {qwen35} on {gpu_label()}). "
        "Send switch to comfy only if you want Flux images."
    )


def llm_loading_text(name: str = "", *, console: bool = False) -> str:
    key = (name or "").strip().lower() or "qwen"
    if key == "restart":
        return restart_reply_text()
    if key in GPU_ALIASES or key == "comfy":
        return comfy_starting_text()
    hint = wait_hint(key)
    extra = ""
    if key in ("qwen35", "qwen36"):
        extra = f" ({key} on {gpu_label()})"
    if console:
        return f"The model is still loading. {hint}{extra}, then send your message again."
    return f"A model is still loading. {hint}{extra}, then keep using gpt-4o."


def comfy_starting_text() -> str:
    return (
        f"ComfyUI is still starting. {wait_hint('comfy')}, then send a short "
        "image description (for example: a red bicycle in the rain)."
    )


def comfy_not_running_text() -> str:
    return (
        "ComfyUI is not running. Send switch to comfy, "
        f"{wait_hint('comfy').lower()}, then try again."
    )
# Cursor uses <user_query>; VS Code custom-endpoint uses <userRequest>.
QUERY_TAG_RE = re.compile(
    r"<(user_query|userRequest|UserRequest|userPrompt|user_prompt)>\s*(.*?)\s*</\1>",
    re.S | re.I,
)
_PASTE_STUB_RE = re.compile(
    r"(?is)^\s*#?\s*attachment:\s*pasted text(?:\s*#\s*\d+)?\s*$"
)
_ATTACHMENT_INNER_RE = re.compile(
    r"(?is)<attachment\b[^>]*>(.*?)</attachment>"
)
SAVE_IMAGE_RE = re.compile(
    r"(?is)\b(save|write|export|download)\b.*\b(image|screenshot|png|jpe?g|photo|picture)\b"
    r"|\b(image|screenshot|png|jpe?g|photo|picture)\b.*\b(save|write|export|download)\b"
)
CLIPBOARD_HINT_MARK = "The pasted image lives on the TabbyAPI host, not this workspace."


def clipboard_save_hint(api_base: Optional[str] = None) -> str:
    """Point a remote client at the paste URL on this API."""
    base = (api_base or public_api_base()).rstrip("/")
    url = f"{base}/images/pasted/latest.png"
    return (
        f"{CLIPBOARD_HINT_MARK} "
        "It is at this API URL (same host as this chat):\n"
        f"{url}"
    )


# Older tests / callers
CLIPBOARD_HINT = clipboard_save_hint()


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


def last_user_raw(data: ChatCompletionRequest) -> str:
    for message in reversed(data.messages or []):
        if message.role != "user":
            continue
        return _content_text(message.content)
    return ""


def _unwrap_query(text: str) -> str:
    matches = list(QUERY_TAG_RE.finditer(text))
    if matches:
        return matches[-1].group(2).strip()
    return text.strip()


_ATTACHMENT_BLOCK_RE = re.compile(
    r"(?is)<attachment\b[^>]*>.*?</attachment>"
)
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_CSS_OR_CODE_LINE_RE = re.compile(
    r"(?is)^\s*("
    r"[.#@][\w-]*\s*[,{]"
    r"|[\w-]+\s*\{"
    r"|[\w-]+\s*:\s*[^;\n]+;?"
    r"|grid-template|linear-gradient|radial-gradient|"
    r"rgba?\s*\(|var\s*\(--|repeat\s*\(|minmax\s*\("
    r"|img\s*\[|url\s*\("
    r")"
)


def _plain_user_ask(text: str) -> str:
    """User sentence without IDE attachments or pasted CSS/JS."""
    raw = _ATTACHMENT_BLOCK_RE.sub(" ", text or "")
    raw = _CODE_FENCE_RE.sub(" ", raw)
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.fullmatch(r"[\w./-]+\.(css|js|html|jsx|tsx|vue|py)", stripped, re.I):
            continue
        if _CSS_OR_CODE_LINE_RE.match(stripped):
            continue
        lines.append(stripped)
    return "\n".join(lines).strip()


def _is_paste_stub(text: str) -> bool:
    return bool(_PASTE_STUB_RE.match((text or "").strip()))


def user_task_text(data: ChatCompletionRequest) -> str:
    """Website/image spec from the last user turn, including VS Code pastes.

    The editor often wraps a paste as ``#attachment:Pasted text #1``. Mixed
    chat must read the real spec from the same message, not that stub.
    """
    raw = last_user_raw(data)
    unwrapped = _unwrap_query(raw)
    inners = [
        inner.strip()
        for inner in _ATTACHMENT_INNER_RE.findall(raw)
        if inner and inner.strip()
    ]
    if not _is_paste_stub(unwrapped):
        # A short layout follow-up often has styles.css attached. That CSS
        # is not a new website+images spec — do not merge it in.
        return unwrapped
    leftover_lines = [
        line.strip()
        for line in unwrapped.splitlines()
        if line.strip() and not _is_paste_stub(line)
    ]
    leftover = "\n".join(leftover_lines).strip()
    parts = [part for part in [*inners, leftover] if part]
    if parts:
        return "\n".join(parts)
    stripped = QUERY_TAG_RE.sub(" ", raw)
    stripped = "\n".join(
        line.strip()
        for line in stripped.splitlines()
        if line.strip() and not _is_paste_stub(line)
    )
    return stripped.strip()


_SERVER_LAYOUT_BLOCK_RE = re.compile(r"(?is)<(?:layout_fix|layout_report)\b.*")


def _strip_server_layout_blocks(text: str) -> str:
    """Drop Code-mode <layout_fix> / <layout_report> suffixes from the user line.

    Those blocks tell the coding model not to start pictures. They are not the
    user's ask — leaving them in last_user_text made refuses_new_images skip
    Comfy on a generate-logo follow-up that mentioned images/hero.
    """
    return _SERVER_LAYOUT_BLOCK_RE.sub("", text or "").strip()


def last_user_text(data: ChatCompletionRequest) -> str:
    task = user_task_text(data)
    plain = _plain_user_ask(task) if task else ""
    if plain:
        return _strip_server_layout_blocks(plain)
    return _strip_server_layout_blocks(_unwrap_query(last_user_raw(data)))


def _command_candidates(data: ChatCompletionRequest) -> list[str]:
    """Possible user-typed commands, including VS Code/Cursor wrappers.

    Agent prompts include workspace rules like ``switch to qwen35``. Only scan
    the tagged user line for those, never the whole system prompt.
    """
    raw = last_user_raw(data)
    unwrapped = _unwrap_query(raw)
    seen: set[str] = set()
    out: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip().strip("`")
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)

    add(unwrapped)
    if any(marker.lower() in raw.lower() for marker in AGENT_MARKERS):
        for line in unwrapped.splitlines():
            add(line)
        return out
    add(raw)
    for line in raw.splitlines():
        add(line)
    return out


def _match_any(pattern: re.Pattern, data: ChatCompletionRequest):
    for candidate in _command_candidates(data):
        match = pattern.match(candidate)
        if match:
            return match
    return None


def _load_yaml(path: Path):
    yaml = YAML(typ="safe")
    with path.open(encoding="utf-8") as handle:
        return yaml.load(handle) or {}


def profile_map() -> dict[str, dict]:
    """alias / folder name -> {alias, folder, pretty} from model_profiles/*.yml"""
    mapping = {}
    if not PROFILES_DIR.exists():
        return mapping
    for path in PROFILES_DIR.glob("*.yml"):
        data = _load_yaml(path)
        alias = path.stem.lower()
        model_cfg = data.get("model") or {}
        folder = model_cfg.get("model_name")
        pretty = data.get("pretty") or folder or alias
        entry = {
            "alias": alias,
            "folder": folder,
            "pretty": pretty,
            "max_seq_len": model_cfg.get("max_seq_len"),
            "cache_size": model_cfg.get("cache_size"),
            "vision": bool(model_cfg.get("vision")),
        }
        mapping[alias] = entry
        if folder:
            mapping[folder.lower()] = entry
    return mapping


def profile_alias_for_model(folder: Optional[str]) -> Optional[str]:
    """Map a loaded models/ folder name to a profile alias."""
    if not folder:
        return None
    entry = profile_map().get(str(folder).lower())
    return entry["alias"] if entry else None


def profile_ui_labels(names: Optional[list[str]] = None) -> dict[str, str]:
    """alias -> short pretty name for the GPU picker (text before ' - ')."""
    if names is None:
        from select_model import available_profiles

        names = available_profiles()
    mapping = profile_map()
    labels: dict[str, str] = {}
    for name in names:
        entry = mapping.get(str(name).lower()) or {}
        pretty = str(entry.get("pretty") or name)
        labels[name] = pretty.split(" - ", 1)[0].strip() or name
    return labels


def installed_models() -> list[str]:
    """Folder names under models/ that look like real EXL3 downloads."""
    if not MODELS_DIR.exists():
        return []
    names = []
    for path in sorted(MODELS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if path.is_dir() and (path / "config.json").exists():
            names.append(path.name)
    return names


def current_folder() -> Optional[str]:
    try:
        from common import model as model_mod

        if model_mod.container and getattr(model_mod.container, "model_dir", None):
            return model_mod.container.model_dir.name
    except Exception:
        return None
    return None


def resolve_switch_target(token: str) -> Optional[str]:
    """Return a switch_model.py profile alias, or None if unknown."""
    key = token.strip().lower()
    if key in GPU_ALIASES or key == "llm":
        return GPU_ALIASES.get(key, "llm")
    profiles = profile_map()
    if key in profiles:
        return profiles[key]["alias"]
    return None


def switch_token(data: ChatCompletionRequest) -> Optional[str]:
    match = _match_any(SWITCH_RE, data)
    if match:
        return match.group(1)
    match = _match_any(SLASH_PROFILE_RE, data)
    if match:
        name = match.group("name")
        if name.lower() in _SLASH_RESERVED:
            return None
        return name
    return None


def requested_profile(data: ChatCompletionRequest) -> Optional[str]:
    token = switch_token(data)
    if not token:
        return None
    return resolve_switch_target(token)


def is_list_request(data: ChatCompletionRequest) -> bool:
    return bool(_match_any(LIST_RE, data))


def is_help_request(data: ChatCompletionRequest) -> bool:
    return bool(_match_any(HELP_RE, data))


def is_restart_request(data: ChatCompletionRequest) -> bool:
    return bool(_match_any(RESTART_RE, data))


def _ctx_label(entry: dict) -> str:
    raw = entry.get("max_seq_len") or entry.get("cache_size")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return ""
    if n >= 1000:
        return f"{n // 1000}k"
    return str(n)


def _is_loopback_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def help_api_urls(
    api_base: Optional[str] = None, request=None
) -> tuple[str, str]:
    """Client-facing (base, origin). Empty when only a loopback URL is known."""
    base = (api_base or public_api_base(request) or "").rstrip("/")
    if not base or _is_loopback_url(base):
        return "", ""
    origin = base[:-3] if base.endswith("/v1") else base
    return base, origin


def help_text(api_base: Optional[str] = None, request=None) -> str:
    """Markdown user guide returned for a chat line that is only ``help``."""
    profiles = profile_map()
    loaded = current_folder()
    aliases = []
    seen = set()
    for path in sorted(PROFILES_DIR.glob("*.yml")) if PROFILES_DIR.exists() else []:
        alias = path.stem.lower()
        if alias in seen:
            continue
        seen.add(alias)
        aliases.append(alias)

    base, origin = help_api_urls(api_base, request)
    api_url = base or "your configured /v1 URL"
    ui_url = f"{origin}/v1/ui" if origin else "/v1/ui on the same host"
    health_url = f"{origin}/health" if origin else "/health on the same host"
    embed = f"{base}/embeddings" if base else "/v1/embeddings"
    images_post = f"{base}/images/generations" if base else "/v1/images/generations"

    lines = [
        "# TabbyAPI Stack help",
        "",
        "Use **`gpt-4o`** as the model name in your editor and leave it selected. "
        "It is a compatibility label; the local profile shown by `list models` "
        "still performs the inference.",
        "",
        "## Choose where to work",
        "",
        "- **Editor:** Chat Completions at this `/v1`. Keep model name `gpt-4o`. "
        "Your project stays on your computer and the editor supplies its own tools.",
        "- **Browser Chat:** same API, no file tools. Conversations, vision, "
        "model commands, and image generation.",
        "- **Browser Code:** a self-contained IDE on this host. Same Chat Completions "
        "pipeline; the browser runs the tool loop against a jailed workspace "
        "(Grep, Glob, Read, Write, Shell, …), plus Monaco, preview, and a container terminal.",
        "- **Status:** model switching, GPU occupancy, restart, updates, health, and resource graphs.",
        "- **Gallery:** generated output images only.",
        "- **Logs:** live and historical server output.",
        "- **Users:** administrator-only Tabby accounts (not Linux users).",
        "- **Settings:** administrator-only Tabby `config.yml`, system `tabby.env`, screensaver, and GPU fan/power (`tsctl`).",
        "- **Account menu:** Download backup / Restore backup for this account's chats, Code files, prefs, and gallery. Other accounts are not in the zip.",
        "",
        "## Connection",
        "",
        f"- API: `{api_url}`",
        f"- Health: `GET {health_url}`",
        f"- Browser UI: `{ui_url}`",
        f"- CPU embeddings: `POST {embed}`",
        "",
        "The editor **API key** is your UI login password: the Linux account "
        "password for the stack admin, or the password set on the Users page "
        "for a Tabby-only account.",
        "",
        "The NVIDIA GPU runs either a language model or ComfyUI, never both at once. "
        "CPU embeddings remain available in either mode. Browser and editor requests "
        "share one GPU slot.",
        "",
        "## Chat commands",
        "",
        "Send a command as the **whole message**:",
        "",
        "```tabby",
        "help",
        "list models",
        "restart",
    ]
    for alias in aliases:
        entry = profiles.get(alias) or {}
        mark = "  # loaded" if entry.get("folder") == loaded else ""
        lines.append(f"switch to {alias}{mark}")
    lines.extend(
        [
            "switch to comfy",
            "switch to flux",
            "switch to llm",
            "```",
            "",
            "- `help` shows this guide.",
            "- `list models` shows only installed profiles and marks the loaded one.",
            "- `restart` restarts the API and restores the last language model.",
            "- `switch to comfy` and `switch to flux` start image generation.",
            "- `switch to llm` stops ComfyUI and restores the last language model.",
            "",
            f"### Model profiles on this {gpu_label()}",
            "",
        ]
    )
    for alias in aliases:
        entry = profiles.get(alias) or {}
        pretty = entry.get("pretty") or alias
        ctx = _ctx_label(entry)
        wait = format_duration(ready_seconds(alias))
        mark = " — **loaded**" if entry.get("folder") == loaded else ""
        ctx_bit = f", {ctx} context" if ctx else ""
        lines.append(
            f"- **`{alias}`** — {pretty}{ctx_bit}; about {wait}{mark}"
        )
    flux = extra_seconds("comfy", "flux_s")
    qwen_img = extra_seconds("comfy", "qwen_image_s")
    comfy_ready = format_duration(ready_seconds("comfy"))
    llm_ready = format_duration(ready_seconds("llm"))
    lines.extend(
        [
            "",
            "Warm estimates:",
            "",
            f"- ComfyUI ready: about {comfy_ready}",
            f"- First Flux image: about {format_duration(flux) if flux else 'a few minutes'}",
            f"- First Qwen-Image: about {format_duration(qwen_img) if qwen_img else 'a few minutes'}",
            f"- Language model restored: about {llm_ready}",
            "",
            "A first boot may take longer while Triton compiles.",
            "",
            "## Generate a new image",
            "",
            "For one image, ask directly while a language model is loaded:",
            "",
            "```tabby",
            "generate an image of a red bicycle on a city street",
            "qwen-image: a poster with the heading SALE",
            "```",
            "",
            "TabbyAPI Stack hands the GPU to ComfyUI, returns the finished image, then "
            "restores the previous language model. For several images, send "
            "`switch to comfy`, submit each prompt, then send `switch to qwen`.",
            "",
            "- **Flux Schnell:** scenes, photos, drafts, and img2img.",
            "- **Qwen-Image:** prefix `qwen-image:` for logos, posters, UI mockups, "
            "and readable text.",
            "- Describe hero and header art as a scene, not as a screenshot of the whole website.",
            "",
            "## Use an existing image",
            "",
            "- Attach or paste an image with a question to inspect it with a vision-capable profile.",
            "- Attach a source image with an image-generation prompt to use Flux img2img.",
            "- Ask to remove a white border or crop a frame on an attached picture; "
            "Flux img2img regenerates a new PNG.",
            "- An attached source or reference image is **not** a generated result and is not "
            "added to the Gallery.",
            "",
            "## Image library",
            "",
            "The **Gallery** is the library of images generated by TabbyAPI Stack. Open it to "
            "preview, download, or delete generated outputs. Regular users see their own "
            "results; the administrator can see all users' generated images.",
            "",
            "## Build code with images",
            "",
            "- **Browser Code:** ask for the files and named PNGs together. The browser "
            "writes the project with workspace tools; the API then holds for images and "
            "copies them into the Files pane.",
            "- **Editor:** ask for the page and named image paths together. Apply the editor's "
            "file tools; the API waits for the image batch and returns one download command.",
            "",
            "## Image API",
            "",
            f"`POST {images_post}`",
            "",
            "```json",
            '{"prompt": "qwen-image: a logo that says Cafe"}',
            "```",
            "",
            "The response includes `b64_json` and a URL on this server.",
            "",
            "## Recommended workflow",
            "",
            "Use **qwen** for everyday coding. Switch to **qwen35** or **qwen36** "
            "before a long, difficult agent task. Send `list models` to see what is "
            "installed on this server.",
        ]
    )
    return "\n".join(lines)


def list_text() -> str:
    profiles = profile_map()
    loaded = current_folder()
    lines = [
        "Stay on gpt-4o. To switch, type switch to <model>. "
        "Send restart to bounce the API. Send help for the full guide.",
        "Daily chat: qwen. Long Agent tasks: switch to qwen35 or qwen36 first. "
        "glm is thinking chat only — it does not parse coding tools.",
        "Image gen: switch to comfy (unloads the LLM, Flux Schnell). "
        "Switch back with switch to qwen.",
        "",
    ]
    found = False
    for folder in installed_models():
        found = True
        entry = profiles.get(folder.lower())
        if entry:
            ctx = entry.get("max_seq_len") or entry.get("cache_size")
            bits = []
            if folder == loaded:
                bits.append("loaded")
            if ctx:
                bits.append(f"{ctx} ctx")
            extra = f" ({', '.join(bits)})" if bits else ""
            lines.append(f"- {entry['pretty']}{extra} | switch to {entry['alias']}")
        else:
            extra = " (loaded)" if folder == loaded else ""
            lines.append(f"- {folder}{extra} | switch to {folder}")
    if not found:
        lines.append("No models installed.")
    lines.append("- Flux Schnell (ComfyUI) | switch to comfy")
    return "\n".join(lines)


def restart_wait_name() -> str:
    if gpu_is_comfy():
        return "comfy"
    try:
        from select_model import last_profile

        name = last_profile()
    except Exception:
        name = None
    return name or "qwen"


def restart_reply_text() -> str:
    name = restart_wait_name()
    hint = wait_hint(name)
    if name == "comfy":
        return (
            f"Restarting the stack. {hint}, then describe an image "
            "or send switch to qwen."
        )
    return f"Restarting the stack. {hint}, then keep using gpt-4o."


def _abandon_jobs_for_restart() -> None:
    """Drop in-flight image jobs before the process is killed."""
    try:
        from images.jobs import abandon_inflight_jobs

        abandon_inflight_jobs("TabbyAPI is restarting.")
    except Exception as exc:
        xlogger.warning(f"Could not clear image jobs before restart: {exc}")


def start_restart(*, abandon: bool = True) -> bool:
    """Detach a delayed systemd bounce so this chat reply can flush."""
    if shutil.which("systemctl") is None:
        return False
    if abandon:
        _abandon_jobs_for_restart()
    LOG.touch(exist_ok=True)
    LOCK.write_text("restart", encoding="utf-8")
    mode = "comfy" if gpu_is_comfy() else "llm"
    with LOG.open("a", encoding="utf-8") as log:
        log.write("\n--- restart (from chat) ---\n")
        log.flush()
        kwargs: dict = {
            "cwd": str(ROOT),
            "stdout": log,
            "stderr": log,
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000 | 0x00000008
        else:
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen(
                [
                    str(PYTHON),
                    str(RESTARTER),
                    "--delay",
                    "1.5",
                    "--mode",
                    mode,
                    "--lock",
                    str(LOCK),
                ],
                **kwargs,
            )
        except OSError:
            LOCK.unlink(missing_ok=True)
            return False
    xlogger.info("Phrase restart started")
    return True


def set_switch_lock(name: str) -> None:
    LOCK.write_text((name or "qwen").strip().lower() or "qwen", encoding="utf-8")


def clear_switch_lock() -> None:
    LOCK.unlink(missing_ok=True)


def start_switch(name: str) -> None:
    LOG.touch(exist_ok=True)
    # Write the lock before spawning: switch_model.py removes it when it exits,
    # and a fast failure (unknown profile, server down) would otherwise finish
    # first and leave a lock nobody clears for 180s.
    set_switch_lock(name)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(f"\n--- switch {name} (from Cursor chat) ---\n")
        log.flush()
        kwargs: dict = {
            "cwd": str(ROOT),
            "stdout": log,
            "stderr": log,
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen([str(PYTHON), str(SWITCHER), name], **kwargs)
        except OSError:
            LOCK.unlink(missing_ok=True)
            raise
    xlogger.info(f"Phrase switch started: {name}")


def switch_reply_text(name: str) -> str:
    hint = wait_hint(name)
    if name == "comfy":
        return (
            f"Switching the GPU to ComfyUI / Flux. {hint}. "
            "TabbyAPI stays up; the LLM is unloaded. "
            "Next message: a short image description, or POST /v1/images/generations. "
            "The reply includes a download URL on this API host. "
            "Send switch to qwen when you want the LLM back."
        )
    if name == "llm":
        return (
            "Stopping ComfyUI and reloading the last TabbyAPI model. "
            f"{hint}, then keep using gpt-4o."
        )
    entry = profile_map().get(name, {})
    pretty = entry.get("pretty") or name
    err = profile_error(name)
    extra = ""
    if err:
        extra = (
            f" On {gpu_label()} this profile previously failed ({err}). "
            "If the load fails, switch to qwen."
        )
    return (
        f"Switching TabbyAPI to {pretty} (ComfyUI will be stopped). "
        f"{hint}, then keep using gpt-4o. "
        f"The next message will use the new model.{extra}"
    )


NO_TOOL_FORMAT_HINT = (
    "The loaded profile does not parse tool calls. "
    "Send `switch to qwen` (or gemma) for coding with tools. "
    "glm is a thinking chat profile, not a coding-agent profile."
)


def request_has_tools(data: ChatCompletionRequest) -> bool:
    return bool(getattr(data, "tools", None) or getattr(data, "functions", None))


def container_parses_tools() -> bool:
    from common import model as tabby_model

    container = getattr(tabby_model, "container", None)
    if container is None:
        return False
    return bool(
        getattr(container, "tool_format", None)
        or getattr(container, "harmony", False)
        or getattr(container, "muse_glimmer", False)
    )


def tools_without_format_response(data: ChatCompletionRequest):
    """Honest reply when the client sent tools but this profile cannot parse them."""
    if not request_has_tools(data) or container_parses_tools():
        return None
    return text_response(data, NO_TOOL_FORMAT_HINT)


def text_response(data: ChatCompletionRequest, text: str):
    if data.stream:
        return EventSourceResponse(
            stream_text(data, text),
            ping=get_sse_ping_interval(),
            sep="\n",
        )
    return ChatCompletionResponse(
        model=data.model or "gpt-4o",
        choices=[
            ChatCompletionRespChoice(
                finish_reason="stop",
                message=ChatCompletionMessage(role="assistant", content=text),
            )
        ],
    )


def tool_call_response(
    data: ChatCompletionRequest,
    calls: list[tuple[str, dict]],
    *,
    content: Optional[str] = None,
):
    """Drive Cursor tools while the coding model is off the GPU."""
    tool_calls = [
        ToolCall(
            function=Tool(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
            type="function",
            index=index,
        )
        for index, (name, arguments) in enumerate(calls)
    ]
    message = ChatCompletionMessage(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
    )
    if data.stream:
        return EventSourceResponse(
            stream_tool_calls(data, message),
            ping=get_sse_ping_interval(),
            sep="\n",
        )
    return ChatCompletionResponse(
        model=data.model or "gpt-4o",
        choices=[
            ChatCompletionRespChoice(
                finish_reason="tool_calls",
                message=message,
            )
        ],
    )


def stream_chat_delta(
    data: ChatCompletionRequest,
    delta: dict,
    *,
    chunk_id: str,
    created: int,
    finish: Optional[str] = None,
) -> str:
    """One OpenAI chat.completion.chunk JSON object (EventSourceResponse data)."""
    return json.dumps(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": data.model or "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish,
                }
            ],
        },
        ensure_ascii=False,
    )


async def stream_text(
    data: ChatCompletionRequest,
    text: str,
    *,
    chunk_id: Optional[str] = None,
    created: Optional[int] = None,
):
    chunk_id = chunk_id or f"chatcmpl-{uuid4().hex}"
    model_name = data.model or "gpt-4o"
    first = ChatCompletionStreamChunk(
        id=chunk_id,
        model=model_name,
        choices=[
            ChatCompletionStreamChoice(delta=ChatCompletionMessage(role="assistant", content=text))
        ],
    )
    last = ChatCompletionStreamChunk(
        id=chunk_id,
        model=model_name,
        choices=[ChatCompletionStreamChoice(delta={}, finish_reason="stop")],
    )
    yield first.model_dump_json()
    yield last.model_dump_json()


async def stream_tool_calls(
    data: ChatCompletionRequest,
    message: ChatCompletionMessage,
    *,
    chunk_id: Optional[str] = None,
    created: Optional[int] = None,
):
    """OpenAI-shaped SSE deltas. Cursor ignores a whole ChatCompletionMessage dump."""
    chunk_id = chunk_id or f"chatcmpl-{uuid4().hex}"
    created = int(time.time()) if created is None else created

    def dump(delta: dict, finish: Optional[str] = None) -> str:
        return stream_chat_delta(
            data, delta, chunk_id=chunk_id, created=created, finish=finish
        )

    yield dump({"role": "assistant"})
    content = getattr(message, "content", None) or ""
    if content:
        yield dump({"content": content})
    tool_payload = []
    for index, call in enumerate(message.tool_calls or []):
        item = call.model_dump(mode="json", exclude_none=True)
        item["index"] = index
        tool_payload.append(item)
    if tool_payload:
        yield dump({"tool_calls": tool_payload})
    yield dump({}, finish="tool_calls")


def inject_clipboard_save_hint(
    data: ChatCompletionRequest, api_base: Optional[str] = None
) -> None:
    """Tell the model to download the paste from this API when the user asked to save."""
    if not SAVE_IMAGE_RE.search(last_user_text(data)):
        return
    hint = clipboard_save_hint(api_base)
    for message in reversed(data.messages or []):
        if message.role != "user":
            continue
        content = message.content
        if isinstance(content, str):
            if CLIPBOARD_HINT_MARK in content:
                return
            message.content = content + "\n" + hint
            return
        if isinstance(content, list):
            for part in content:
                if CLIPBOARD_HINT_MARK in (getattr(part, "text", None) or ""):
                    return
            message.content = list(content) + [
                ChatCompletionMessagePart(type="text", text=hint)
            ]
        return


def last_role(data: ChatCompletionRequest) -> str:
    messages = data.messages or []
    if not messages:
        return ""
    return (messages[-1].role or "").lower()


def already_made_image(data: ChatCompletionRequest) -> bool:
    """True after we already generated or asked Cursor to save this turn."""
    for message in data.messages or []:
        if message.role == "assistant":
            content = _content_text(message.content)
            if "Image is ready" in content or "/images/generated-" in content:
                return True
            for call in message.tool_calls or []:
                args = getattr(getattr(call, "function", None), "arguments", "") or ""
                if "generated-latest.png" in args or "generated.png" in args:
                    return True
        if message.role in ("tool", "function"):
            content = _content_text(message.content)
            if "generated.png" in content or "generated-latest" in content:
                return True
    return False


def has_new_user_after_image(data: ChatCompletionRequest) -> bool:
    """True when the user sent a new line after the last generated preview."""
    last_image = -1
    last_user = -1
    for index, message in enumerate(data.messages or []):
        content = _content_text(message.content)
        if message.role == "assistant" and (
            "Image is ready" in content or "/images/generated-" in content
        ):
            last_image = index
        if message.role == "user":
            last_user = index
    return last_user > last_image


IMAGE_NOUN_RE = re.compile(
    r"(?is)\b("
    r"images?|pictures?|photos?|pics?|posters?|mockups?|"
    r"icons?|logos?|banners?|pngs?|qwen-images?"
    r")\b"
)
BORDER_TRIM_RE = re.compile(
    r"(?is)\b(?:"
    r"(?:remove|strip|crop(?:\s+off)?|trim(?:\s+off)?|cut(?:\s+off)?|"
    r"delete|erase|drop|get rid of)\b"
    r".{0,60}?\b(?:border|frame|letterbox|mat|bezel)s?(?!-)"
    r"|\b(?:crop|trim)\b.{0,24}\b(?:image|picture|photo|png)\b"
    r")"
)
CODING_TASK_RE = re.compile(
    r"(?is)\b("
    r"web\s*page|website|web\s*site|html|css|javascript|typescript|"
    r"homepage|landing\s*page|component|implement|source\s*code|"
    r"react|vue|jsx|tsx"
    r")\b"
)
USE_IN_UI_RE = re.compile(
    r"(?is)\buse\s+(?:them|it|these|those)\s+(?:on|in|for)\b"
    r"|\b(?:on|in)\s+the\s+(?:menu|page|site|website|header|hero|card)s?\b"
    r"|\bmenu\s+sections?\b"
)
MIXED_IMAGE_HINT_MARK = "This turn is a coding task that also needs images."


def refuses_new_images(text: str) -> bool:
    """True when this line asks not to start another picture job."""
    return bool(REFUSE_IMAGES_RE.search(text or ""))


def is_coding_task(text: str) -> bool:
    return bool(CODING_TASK_RE.search(text or ""))


def is_page_layout_ask(text: str) -> bool:
    """True when this line is page/HTML work, not a standalone image prompt.

    Used only when the coding model is not loaded (Comfy owns the GPU). The
    mixed-chat gate is LLM classify, not this helper.
    """
    raw = text or ""
    return bool(CODING_TASK_RE.search(raw) or USE_IN_UI_RE.search(raw))


def image_job_wait_text(
    prompt: str = "",
    restore: bool = True,
    count: int = 1,
    prompts: Optional[list[str]] = None,
) -> str:
    """Measured wait for one Comfy batch, from switch_times.json."""
    from common.gpu_mode import wants_qwen_image

    texts = list(prompts) if prompts else [prompt or ""] * max(1, int(count))
    n = len(texts)
    llm_s = format_duration(ready_seconds("llm"))
    if n == 1:
        qwen = wants_qwen_image(texts[0]) if texts[0] else False
        backend = "Qwen-Image" if qwen else "Flux"
        extra = extra_seconds("comfy", "qwen_image_s" if qwen else "flux_s")
        render_s = format_duration(int(extra) if extra is not None else (240 if qwen else 180))
        bits = [f"about {render_s} to render ({backend})"]
        if restore:
            bits.append(f"about {llm_s} to reload the coding model")
        one = ", then ".join(bits)
        return one[0].upper() + one[1:] + "."
    total_render = 0
    for text in texts:
        qwen = wants_qwen_image(text) if text else False
        extra = extra_seconds("comfy", "qwen_image_s" if qwen else "flux_s")
        total_render += int(extra) if extra is not None else (240 if qwen else 180)
    bits = [
        f"{n} images in one Comfy session",
        f"about {format_duration(total_render)} to render",
    ]
    if restore:
        bits.append(f"about {llm_s} to reload the coding model once at the end")
    return ", ".join(bits) + "."


def _compact_elapsed(seconds: float) -> str:
    total = max(0, int(round(float(seconds) or 0)))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if minutes:
        return f"{hours}h {minutes}m"
    return f"{hours}h"


def _image_backends(texts: list[str]) -> list[str]:
    from common.gpu_mode import wants_qwen_image

    names: list[str] = []
    for text in texts:
        label = "Qwen-Image" if (text and wants_qwen_image(text)) else "Flux"
        if label not in names:
            names.append(label)
    return names or ["Flux"]


def image_job_done_text(
    prompt: str = "",
    restore: bool = True,
    count: int = 1,
    prompts: Optional[list[str]] = None,
    elapsed_s: Optional[float] = None,
    job=None,
) -> str:
    """Past-tense summary after a Comfy batch finished."""
    if job is not None:
        restore = bool(getattr(job, "restore", restore))
        items = getattr(job, "items", None) or []
        prompts = [
            str(getattr(item, "prompt", "") or "") for item in items
        ] or prompts
        started = float(getattr(job, "started_at", 0) or 0)
        if elapsed_s is None and started > 0:
            elapsed_s = max(0.0, time.time() - started)
        if not prompts:
            count = max(1, int(getattr(job, "count", 0) or count))
    texts = list(prompts) if prompts else [prompt or ""] * max(1, int(count))
    n = len(texts)
    backends = _image_backends(texts)
    named = " and ".join(backends) if len(backends) <= 2 else ", ".join(backends[:-1]) + f", and {backends[-1]}"
    if n == 1:
        lead = f"Rendered with {named}"
    else:
        lead = f"Rendered {n} pictures in one Comfy session ({named})"
    if elapsed_s is not None and elapsed_s >= 1:
        lead += f" in {_compact_elapsed(elapsed_s)}"
    lead += "."
    if restore:
        lead += " The coding model is loaded again."
    return lead


def image_job_wait_seconds(
    prompt: str = "",
    restore: bool = True,
    count: int = 1,
    prompts: Optional[list[str]] = None,
) -> int:
    from common.gpu_mode import wants_qwen_image

    texts = list(prompts) if prompts else [prompt or ""] * max(1, int(count))
    total = 0
    for text in texts:
        qwen = wants_qwen_image(text) if text else False
        extra = extra_seconds("comfy", "qwen_image_s" if qwen else "flux_s")
        total += int(extra) if extra is not None else (240 if qwen else 180)
    if restore:
        total += ready_seconds("llm")
    return max(30, total)


async def gpu_busy_image_response(data: ChatCompletionRequest):
    from images.chat import handle

    return await handle(data)


async def await_gpu_busy_image_response(data: ChatCompletionRequest):
    return await gpu_busy_image_response(data)


async def prepare_mixed_image_turn(
    data: ChatCompletionRequest, api_base: Optional[str] = None
):
    from images.chat import handle

    return await handle(data, api_base)


def inject_mixed_image_hint(
    data: ChatCompletionRequest, api_base: Optional[str] = None
) -> None:
    return


def requested_image_prompt(
    data: ChatCompletionRequest, explicit_only: bool = False
) -> Optional[str]:
    """Image prompt from the user's actual line, never the Agent wrapper.

    When the LLM is loaded, only explicit “generate an image …” lines run
    Comfy. Mixed coding + image requests stay with the agent.
    In Comfy mode any short description is a prompt, except mixed tasks.
    """
    if last_role(data) in ("tool", "function"):
        return None
    if already_made_image(data) and not has_new_user_after_image(data):
        return None
    raw = last_user_raw(data)
    text = user_task_text(data) or _unwrap_query(raw)
    if not text or _is_paste_stub(text):
        return None
    if any(marker.lower() in text.lower() for marker in AGENT_MARKERS):
        return None
    if len(text) > MAX_IMAGE_PROMPT_CHARS or META_IMAGE_RE.search(text):
        return None
    if _is_meta_wrapper_text(text):
        return None
    if is_page_layout_ask(text):
        return None
    if looks_like_chat_not_image(text):
        return None
    if refuses_new_images(text) and not IMAGE_GEN_RE.match(text):
        return None
    if explicit_only and is_coding_task(text):
        return None
    match = IMAGE_GEN_RE.match(text)
    if match:
        prompt = (match.group(1) or "").strip()
        if explicit_only and not IMAGE_NOUN_RE.search(text):
            return None
        return prompt or None
    if explicit_only:
        return None
    return text


def wants_border_trim(text: str) -> bool:
    """True when they asked to take a frame off an existing picture."""
    return bool(BORDER_TRIM_RE.search(text or ""))


def border_edit_prompt(text: str) -> str:
    """Flux img2img prompt: same picture, full bleed, no card frame."""
    color = "black" if re.search(r"(?i)\bblack\b", text or "") else "white"
    return (
        "flux: the same photograph filling the entire frame edge to edge, "
        f"no {color} border, no {color} frame, no rounded rectangle, "
        "no mat, no padding, no letterbox, no card, full bleed, "
        "no UI, no website"
    )


def looks_like_chat_not_image(text: str) -> bool:
    """True when this line is conversation, not a Comfy picture prompt."""
    raw = (text or "").strip()
    if not raw:
        return False
    if IMAGE_GEN_RE.match(raw) or IMAGE_COUNT_RE.match(raw):
        return False
    if raw.lower().startswith("qwen-image:"):
        return False
    if IMAGE_NOUN_RE.search(raw) and not CHAT_QUESTION_RE.match(raw):
        return False
    return bool(CHAT_OPENER_RE.match(raw))


def comfy_chat_suggest_text() -> str:
    return (
        f"{SWITCH_LLM_MARK}\n"
        "ComfyUI is loaded, so this would generate a picture. "
        "That looks like a chat for the coding model. "
        "Switch to the last LLM to talk, or send a short image description instead."
    )


def should_yield_comfy_to_llm(data: ChatCompletionRequest) -> bool:
    """True when Comfy owns the GPU but this turn needs the coding model."""
    try:
        from images.jobs import active_mcp_image_job

        busy = active_mcp_image_job()
    except Exception:
        busy = None
    if busy and busy.status in ("queued", "running"):
        return False
    if last_role(data) in ("tool", "function"):
        return True
    return requested_image_prompt(data) is None


def last_llm_profile_name() -> str:
    """Last LLM profile for a Comfy→LLM handoff (never 'comfy')."""
    alias = profile_alias_for_model(current_folder())
    if alias:
        return alias
    try:
        from select_model import last_profile

        name = last_profile()
    except Exception:
        name = None
    if name and name.lower() not in GPU_ALIASES and name.lower() != "comfy":
        return name
    return "qwen"


async def yield_comfy_to_llm_response(
    data: ChatCompletionRequest, *, console: bool = False
):
    """Reload the last LLM so mixed Agent / tool turns can keep coding."""
    if switch_in_progress():
        return await llm_not_ready_response(data, console=console)
    name = last_llm_profile_name()
    start_switch(name)
    if not console:
        await asyncio.sleep(LLM_NOT_READY_WAIT_S)
    return text_response(data, llm_loading_text(name, console=console))


def requested_image_count(prompt: str) -> tuple[int, str]:
    """How many separate Flux jobs a chat line asked for, and the cleaned prompt."""

    text = (prompt or "").strip()
    match = IMAGE_COUNT_RE.match(text)
    if not match:
        return 1, text
    raw = (match.group("num") or "1").lower()
    if raw in _IMAGE_COUNT_WORDS:
        count = _IMAGE_COUNT_WORDS[raw]
    else:
        try:
            count = int(raw)
        except ValueError:
            return 1, text
    count = max(1, min(count, MAX_CHAT_IMAGES))
    rest = (match.group("rest") or "").strip()
    return count, rest or text


IMAGE_DOWNLOAD_HINT = (
    "These URLs are on this API host. The markdown preview above is the picture."
)


def _image_url_block(filenames: list[str], api_base: Optional[str] = None) -> str:
    names = [name for name in (filenames or []) if name]
    if not names:
        return "No generated images for this turn yet."
    lines = [f"{len(names)} image(s) from this turn:"]
    for index, name in enumerate(names, start=1):
        url = public_image_url(name, api_base=api_base)
        lines.append(f"\n{index}. {name}\n![]({url})\n{url}")
    lines.append("\n" + IMAGE_DOWNLOAD_HINT)
    return "\n".join(lines)


def turn_image_names(extra: Optional[str] = None) -> list[str]:
    names = [path.name for path in recent_generated_files()]
    if extra and extra not in names:
        names.append(extra)
    return names


def gpu_is_comfy() -> bool:
    return (read_mode().get("mode") or "llm").lower() == "comfy"


def switch_lock_name() -> str:
    try:
        return LOCK.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""


def switch_lock_held() -> bool:
    """True while a detached switch/restart still owns the GPU."""
    try:
        if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 180:
            return True
    except OSError:
        pass
    try:
        from common import model as tabby_model

        if tabby_model.load_lock.locked():
            return True
    except Exception:
        pass
    return False


def switch_in_progress() -> bool:
    if switch_lock_held():
        return True
    try:
        from common import model as tabby_model

        container = tabby_model.container
        if container is not None and not getattr(container, "loaded", False):
            return True
    except Exception:
        pass
    return False


async def llm_not_ready_response(
    data: ChatCompletionRequest, *, console: bool = False
):
    if not console:
        await asyncio.sleep(LLM_NOT_READY_WAIT_S)
    if switch_in_progress():
        name = switch_lock_name() or last_llm_profile_name()
        if name in GPU_ALIASES or name == "comfy" or name == "flux":
            return text_response(data, comfy_starting_text())
        return text_response(data, llm_loading_text(name, console=console))
    try:
        from images.jobs import active_mcp_image_job

        busy = active_mcp_image_job()
    except Exception:
        busy = None
    if busy and busy.status in ("queued", "running"):
        return text_response(
            data,
            f"Images are still rendering (job {busy.id}). "
            "Wait for the download curl in the chat that started that job.",
        )
    if busy and busy.status == "coding":
        return text_response(
            data, llm_loading_text(last_llm_profile_name(), console=console)
        )
    return text_response(data, llm_not_ready_text(console=console))


async def comfy_idle_response(data: ChatCompletionRequest, api_base: Optional[str] = None):
    await asyncio.sleep(LLM_NOT_READY_WAIT_S)
    if already_made_image(data):
        return text_response(
            data,
            "That request already has previews (same chat turn). "
            "Send a new short description for another picture, or switch to qwen.\n\n"
            + _image_url_block(turn_image_names(), api_base=api_base),
        )
    return text_response(data, COMFY_IDLE)


def image_ready_response(
    data: ChatCompletionRequest,
    filename: str,
    api_base: Optional[str] = None,
    *,
    restore: bool = False,
    count: int = 1,
    filenames: Optional[list[str]] = None,
):
    this = image_job_done_text(last_user_text(data), restore=restore, count=count)
    names = [name for name in (filenames or []) if name]
    if not names:
        names = [filename] if filename else []
    text = (
        _image_url_block(names, api_base=api_base)
        + f"\n\n{this}"
        + "\nSend another short description for a different picture, or switch to qwen."
    )
    return text_response(data, text)


def handle_if_requested(
    data: ChatCompletionRequest,
    api_base: Optional[str] = None,
    *,
    defer_switch: bool = False,
):
    if is_help_request(data):
        return text_response(data, help_text(api_base=api_base))
    if is_list_request(data):
        return text_response(data, list_text())
    if is_restart_request(data):
        if not start_restart():
            return text_response(
                data,
                "Restart is not available on this host. Send help for the chat phrases.",
            )
        return text_response(data, restart_reply_text())

    query = last_user_text(data)
    if is_save_image_request(query):
        return text_response(data, pasted_download_text(query, api_base or public_api_base()))

    name = requested_profile(data)
    if not name:
        token = switch_token(data)
        if token:
            return text_response(
                data,
                f"Unknown model {token!r}. Send 'list models' or 'help'.",
            )
        return None
    if defer_switch:
        return None
    start_switch(name)
    return text_response(data, switch_reply_text(name))
