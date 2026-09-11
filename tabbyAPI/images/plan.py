"""LLM intent + dest planner for mixed coding+images.

The loaded coding model decides whether this turn needs new PNGs, reuse of
files that already exist, or ordinary coding. Regex does not own that gate.
rewrite_comfy_prompt is only a safety net on the model's prompts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from common.image_prompts import (
    MAX_PLANNED_IMAGES,
    resolved_image_size,
    rewrite_comfy_prompt,
)

LLM_PLAN_TIMEOUT_S = 25
VALID_ACTIONS = frozenset({"generate", "reuse", "none"})
MIXED_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["generate", "reuse", "none"],
        },
        "images": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "prompt": {"type": "string"},
                    "subject": {"type": "string"},
                    "size": {"type": "string"},
                },
            },
        },
    },
    "required": ["action", "images"],
}

CLASSIFY_SYSTEM = (
    "You organize image work for a local GPU. JSON only, no markdown. "
    "Read the whole conversation, especially This turn, the last assistant "
    "plan, and any files already in the project or generated in this chat.\n"
    "action:\n"
    "- generate: this turn (or the approved plan, if This turn is Implement "
    "the approved plan) explicitly asks to render NEW rasters now: generate/"
    "draw/render/recreate/replace a picture, qwen-image:, a new logo, a "
    "new hero photo, or ## Assets PNG/WebP dests that are not already in "
    "the project. Creating a landing page, website, or HTML page that should "
    "include a logo and/or a hero/header photo is generate for those dests "
    "when they are not already in the project. Putting NEW pictures onto an "
    "existing home page, panel, card, or section is generate for those dests "
    "even if they also ask for size, opacity, or blend. Fill images with one "
    "object per new file. Do not infer generate from a CSS/layout follow-up "
    "that only mentions an existing logo or header. Mentions of pictures that "
    "already exist are reuse. Removing a border or "
    "frame on an attached picture is generate (img2img), not CSS.\n"
    "- reuse: files already exist (Already in the project / Already generated) "
    "and they only want them used in HTML/CSS. images must be []. Do not render.\n"
    "- none: ordinary coding, CSS/layout/overlay/opacity/z-index on pictures "
    "that already exist, an image "
    "that is not showing because of CSS, Build of a CSS-only plan whose "
    "Assets are None, questions, or "
    "a standalone generate-an-image line with no website/page. images must be [].\n"
    "images (generate only): filename is a basename like logo.png or mars.png, "
    "or a project path like pbptours/images/logo.png. prompt is the full Comfy "
    "prompt. Optional size is WIDTHxHEIGHT. Honor requested dimensions "
    "(1920x1080, 16:9, landscape, portrait). Banners/headers default to "
    "1536x768; portraits/9:16 to 768x1344. Logos, wordmarks, posters, "
    "buttons, and readable text start with qwen-image:. Hero/header/banner "
    "images are a scene, not a website screenshot. Keep the user's "
    "imaginary or fantasy description; do not substitute a stock real-world "
    "photograph unless they asked for one. One file per named subject they "
    "asked to create. Do not invent a category. Skip CSS, HTML, JavaScript, "
    "React, Vue, pricing tiers, and etc."
)
_PLAN_CACHE: OrderedDict[str, "ImageTurnPlan"] = OrderedDict()
_PLAN_CACHE_MAX = 24
_SKIP_SLUGS = frozenset(
    {"etc", "css", "css3", "html", "html5", "js", "javascript", "react", "vue"}
)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CSS_SLUG_RE = re.compile(
    r"(?is)^("
    r"[\d.-]+(?:px|em|rem|vh|vw|vmin|vmax|pct|deg|turn|fr|s|ms|ch|ex)?"
    r"|auto-fit|auto-fill|minmax|inherit|unset|initial|revert|none|auto"
    r"|linear-gradient|radial-gradient|repeat|rgba?|hsla?"
    r"|var-.+"
    r")$"
)
_SITE_FOLDER_RE = re.compile(
    r"(?is)\b(?:under|in|into|inside)\s+(?:the\s+)?"
    r"(?:folder|directory|dir)\s+[\"`'“”]?([A-Za-z][A-Za-z0-9._-]*)"
)


@dataclass
class ImageTurnPlan:
    action: str = "none"
    items: list[dict[str, str]] = field(default_factory=list)
    from_model: bool = False


def _spec_key(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode()).hexdigest()


def remember_plan(text: str, plan: ImageTurnPlan) -> None:
    key = _spec_key(text)
    if key in _PLAN_CACHE:
        _PLAN_CACHE.move_to_end(key)
    _PLAN_CACHE[key] = ImageTurnPlan(
        action=plan.action,
        items=[dict(row) for row in plan.items],
        from_model=plan.from_model,
    )
    while len(_PLAN_CACHE) > _PLAN_CACHE_MAX:
        _PLAN_CACHE.popitem(last=False)


def recalled_plan(text: str) -> ImageTurnPlan | None:
    key = _spec_key(text)
    plan = _PLAN_CACHE.get(key)
    if not plan:
        return None
    _PLAN_CACHE.move_to_end(key)
    return ImageTurnPlan(
        action=plan.action,
        items=[dict(row) for row in plan.items],
        from_model=plan.from_model,
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


def classify_blob(data, prior_facts: str = "") -> str:
    """Conversation the classifier sees: history + this turn + known dests."""
    from common.phrase_switch import last_user_text

    chunks: list[str] = []
    facts = (prior_facts or "").strip()
    if facts:
        if not facts.lower().startswith("already"):
            facts = f"Already generated in this chat:\n{facts}"
        chunks.append(facts)
    history: list[str] = []
    last_assistant = ""
    for message in getattr(data, "messages", None) or []:
        role = (getattr(message, "role", None) or "").lower()
        text = _content_text(getattr(message, "content", None)).strip()
        if not text:
            continue
        if role == "user":
            history.append(f"user: {text[:2000]}")
        elif role == "assistant":
            last_assistant = text
            if "tabby-image-job:" in text:
                history.append(f"assistant: {text[:500]}")
    if last_assistant and "tabby-image-job:" not in last_assistant:
        history.append(f"assistant: {last_assistant[:2000]}")
    if history:
        chunks.append("Conversation:\n" + "\n".join(history[-8:]))
    this_turn = (last_user_text(data) or "").strip()
    if this_turn:
        chunks.append(f"This turn:\n{this_turn[:2000]}")
    return "\n\n".join(chunks)[:6000]


def parse_plan_json(raw: str) -> list[dict[str, str]]:
    text = re.sub(r"(?is)<think>.*?</think>", " ", raw or "")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    blob = fenced.group(1) if fenced else ""
    if not blob:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            blob = text[start : end + 1]
    if not blob:
        return []
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return []
    rows = data.get("images") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    found: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        filename = str(row.get("filename") or row.get("output_path") or "").strip()
        subject = str(row.get("subject") or row.get("prompt") or "").strip()
        if filename or subject:
            row_out = {"filename": filename, "subject": subject}
            size = str(row.get("size") or "").strip()
            if size:
                row_out["size"] = size
            found.append(row_out)
    return found


def parse_turn_plan(raw: str) -> ImageTurnPlan | None:
    """Parse classifier JSON. None if there is no usable object."""
    text = re.sub(r"(?is)<think>.*?</think>", " ", raw or "")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    blob = fenced.group(1) if fenced else ""
    if not blob:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            blob = text[start : end + 1]
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    action = str(data.get("action") or "").strip().lower()
    rows = parse_plan_json(raw)
    if action not in VALID_ACTIONS:
        action = "generate" if rows else "none"
    if action != "generate":
        return ImageTurnPlan(action=action, items=[], from_model=True)
    return ImageTurnPlan(action="generate", items=[], from_model=True)


def _subject_slug(name: str) -> str:
    slug = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return slug[:40]


def site_images_dir(text: str) -> str:
    match = _SITE_FOLDER_RE.search(text or "")
    folder = (match.group(1) if match else "").strip().strip("/")
    if folder and folder.lower() not in {"images", "image", "v1", "openai"}:
        return f"{folder}/images"
    return "images"


def plan_from_extracted(text: str, rows: list[dict[str, str]]) -> list[dict[str, str]]:
    dest_dir = site_images_dir(text)
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows or []:
        if len(items) >= MAX_PLANNED_IMAGES:
            break
        raw_path = str(row.get("filename") or row.get("output_path") or "")
        raw_path = raw_path.replace("\\", "/").lstrip("/")
        filename = raw_path.split("/")[-1].strip()
        slug = _subject_slug(filename.rsplit(".", 1)[0] if filename else "")
        if not slug:
            slug = _subject_slug(str(row.get("subject") or ""))
        if (
            not slug
            or slug in seen
            or slug in _SKIP_SLUGS
            or _CSS_SLUG_RE.match(slug)
        ):
            continue
        name = "logo.png" if slug == "logo" else f"{slug}.png"
        subject = str(row.get("subject") or slug).strip() or slug
        seen.add(slug)
        if "/" in raw_path and Path(raw_path).suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".gif",
        }:
            dest = raw_path
            if Path(dest).suffix.lower() != ".png":
                dest = str(Path(dest).with_suffix(".png"))
        else:
            dest = f"{dest_dir}/{name}".replace("//", "/")
        items.append(
            {
                "prompt": rewrite_comfy_prompt(subject),
                "output_path": dest,
                "size": resolved_image_size(
                    prompt=subject,
                    filename=filename or name,
                    size=row.get("size"),
                    spec=text,
                ),
            }
        )
    return items


def fallback_item(text: str = "") -> list[dict[str, str]]:
    """Unused by the live gate. Kept for older unit tests."""
    dest_dir = site_images_dir(text)
    return [
        {
            "prompt": rewrite_comfy_prompt("wide photographic scene"),
            "output_path": f"{dest_dir}/generated.png",
        }
    ]


def _plan_from_model_text(spec: str, raw: str) -> ImageTurnPlan | None:
    parsed = parse_turn_plan(raw)
    if parsed is None:
        rows = parse_plan_json(raw)
        if not rows:
            return None
        parsed = ImageTurnPlan(action="generate", items=[], from_model=True)
        items = plan_from_extracted(spec, rows)
        return ImageTurnPlan(
            action="generate" if items else "none",
            items=items,
            from_model=True,
        )
    if parsed.action != "generate":
        return parsed
    items = plan_from_extracted(spec, parse_plan_json(raw))
    if not items:
        return ImageTurnPlan(action="none", items=[], from_model=True)
    return ImageTurnPlan(action="generate", items=items, from_model=True)


async def _llm_json_via_backend(system: str, user: str) -> str:
    try:
        from sidecar.settings import is_sidecar_process

        if not is_sidecar_process():
            return ""
        from sidecar.proxy import generate_chat
        from endpoints.OAI.types.chat_completion import (
            ChatCompletionMessage,
            ChatCompletionRequest,
        )
    except Exception:
        return ""
    request = ChatCompletionRequest(
        messages=[
            ChatCompletionMessage(role="system", content=system),
            ChatCompletionMessage(role="user", content=(user or "")[:6000]),
        ],
        max_tokens=800,
        temperature=0.1,
        json_schema=MIXED_PLAN_SCHEMA,
        stream=False,
    )
    try:
        raw = await asyncio.wait_for(
            generate_chat(request.model_dump(mode="json", exclude_none=True)),
            timeout=LLM_PLAN_TIMEOUT_S,
        )
    except Exception as exc:
        from common.logger import xlogger

        xlogger.warning(f"Mixed image classify via backend failed: {exc}")
        return ""
    choices = raw.get("choices") if isinstance(raw, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or message.get("text") or "")


async def _llm_json(system: str, user: str, disconnect_handler=None) -> str:
    try:
        from common import model as model_mod
        from endpoints.OAI.types.chat_completion import (
            ChatCompletionMessage,
            ChatCompletionRequest,
        )
        from endpoints.OAI.utils.chat_completion import apply_chat_template
    except Exception:
        return ""
    container = getattr(model_mod, "container", None)
    if not container or not getattr(container, "loaded", False):
        return await _llm_json_via_backend(system, user)
    if getattr(container, "prompt_template", None) is None:
        return ""
    request = ChatCompletionRequest(
        messages=[
            ChatCompletionMessage(role="system", content=system),
            ChatCompletionMessage(role="user", content=(user or "")[:6000]),
        ],
        max_tokens=800,
        temperature=0.1,
        json_schema=MIXED_PLAN_SCHEMA,
    )
    try:
        prompt, embeddings = await apply_chat_template(request)
        result = await asyncio.wait_for(
            container.generate(
                f"mixed-plan-{uuid.uuid4().hex[:12]}",
                prompt,
                request,
                mm_embeddings=embeddings,
                disconnect_handler=disconnect_handler,
            ),
            timeout=LLM_PLAN_TIMEOUT_S,
        )
    except Exception as exc:
        from common.logger import xlogger

        xlogger.warning(f"Mixed image classify failed: {exc}")
        return ""
    if isinstance(result, dict):
        return str(result.get("text") or result.get("content") or "")
    return ""


async def llm_classify_turn(
    data,
    disconnect_handler=None,
    prior_facts: str = "",
) -> ImageTurnPlan:
    """Ask the loaded coding model what this turn needs. none if it cannot run."""
    from common.phrase_switch import last_user_text

    blob = classify_blob(data, prior_facts=prior_facts)
    if not blob.strip():
        return ImageTurnPlan()
    raw = await _llm_json(CLASSIFY_SYSTEM, blob, disconnect_handler=disconnect_handler)
    if not raw.strip():
        return ImageTurnPlan()
    spec = (last_user_text(data) or blob).strip()
    plan = _plan_from_model_text(spec, raw)
    if plan is None:
        plan = ImageTurnPlan()
    from common.gen_logging import log_image_translator

    log_image_translator(
        plan.action,
        plan.items,
        source="classify",
        user_text=spec,
    )
    return plan


async def classify_image_turn(
    data,
    disconnect_handler=None,
    prior_facts: str = "",
) -> ImageTurnPlan:
    """Cached classify for this conversation blob."""
    key = classify_blob(data, prior_facts=prior_facts)
    remembered = recalled_plan(key)
    if remembered is not None:
        from common.gen_logging import log_image_translator
        from common.phrase_switch import last_user_text

        log_image_translator(
            remembered.action,
            remembered.items,
            source="classify cache",
            user_text=last_user_text(data) or "",
        )
        return remembered
    plan = await llm_classify_turn(
        data, disconnect_handler=disconnect_handler, prior_facts=prior_facts
    )
    if plan.from_model:
        remember_plan(key, plan)
    return plan


async def llm_plan_images(text: str, disconnect_handler=None) -> list[dict[str, str]]:
    """Ask the loaded coding model for dests. Empty if it cannot run."""
    from endpoints.OAI.types.chat_completion import (
        ChatCompletionMessage,
        ChatCompletionRequest,
    )

    raw = (text or "").strip()
    if not raw:
        return []
    data = ChatCompletionRequest(
        messages=[ChatCompletionMessage(role="user", content=raw)]
    )
    plan = await llm_classify_turn(data, disconnect_handler=disconnect_handler)
    return list(plan.items) if plan.action == "generate" else []


async def plan_mixed_dests(text: str, disconnect_handler=None) -> list[dict[str, str]]:
    """Dests when the model chooses generate. Empty means do not render."""
    from endpoints.OAI.types.chat_completion import (
        ChatCompletionMessage,
        ChatCompletionRequest,
    )

    raw = text or ""
    data = ChatCompletionRequest(
        messages=[ChatCompletionMessage(role="user", content=raw)]
    )
    plan = await classify_image_turn(data, disconnect_handler=disconnect_handler)
    if plan.action == "generate":
        return list(plan.items)
    return []
