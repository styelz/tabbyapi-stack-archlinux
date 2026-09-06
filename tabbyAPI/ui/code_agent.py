"""Jailed workspace tools for the browser IDE. The browser runs the agent loop."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from endpoints.OAI.types.tools import Function, ToolSpec
from ui import workspace

MAX_BRIEF_FILES = 80
MAX_STEP_RESULT = 500
MAX_STEP_ARG = 200
MAX_DELETES_PER_TURN = 8
AGENT_STEP_MARK = "tabby-agent-step:"
AGENT_KINDS = ("agent", "ask", "plan")
CODE_SYSTEM = (
    "You are coding in a workspace project folder on this TabbyAPI Stack host. "
    "This conversation is one thread in that workspace; extra chats share the "
    "same files. The user can create, upload, and attach files; attached files "
    "are included in their message. Use the file tools (Grep, Glob, Write, "
    "StrReplace, Read, Rename, Delete, List) to search, create, and edit text "
    "files. Use OptimizeImage to "
    "compress, resize, or convert project images. List first: generated website "
    "images are often already WebP, not PNG. OptimizeImage finds the real file "
    "when the prompt still says .png, updates code references, and does not "
    "need you to delete the original. If they attach a picture and ask to "
    "remove a border or frame, wait for the new GPU PNG; do not fake it with "
    "CSS, background-size, or JavaScript. Use Shell to run project commands in "
    "this workspace's container (cwd is /work). Never use Shell to delete, move, or overwrite project files. Do not create placeholder files when an "
    "attached project image can be processed with OptimizeImage. Do not dump "
    "whole files in chat. Do not try to run the site for the user; they have "
    "preview. A later user message wins over an earlier example brief: if they "
    "say a different theme or names, do not keep the example's industry or "
    "setting. For a layout, alignment, or color fix, Read the file and use "
    "StrReplace on the few rules that are wrong. Do not Write the whole "
    "HTML/CSS/JS again and do not touch img src or regenerate pictures. "
    "If hero text sits off to the left, the usual cause is a full-size "
    "canvas in the flex row: give that canvas position:absolute; inset:0. "
    "After images exist, List and keep the on-disk paths (often .webp even "
    "when the plan said .png). Never change a working src back to .png. "
    "On first write, point img src or CSS url() at the planned local paths. "
    "Do not Write PNG/WebP dest files or text placeholders with those names. Generated "
    "assets for an HTML website are automatically converted to web-optimized "
    "files and their code references are updated after rendering. Unused cleanup means "
    "empty folders only, plus files the page does not reference. Never delete "
    "HTML, CSS, JS, or images the page still uses. When you are done, give a "
    "short summary of what you wrote or optimized. "
    "If the user asked to change files, do not stop after only Read, Grep, "
    "Glob, or List — Write or StrReplace, then summarize. "
    "When several files need edits, emit every Write and StrReplace in one "
    "response. After those tools succeed, stop and summarize. Do not Read, "
    "Grep, or List to check your own edits unless a tool returned an error. "
    "If they named files such as index.html, styles.css, and app.js, Write "
    "each of those files. If HTML links local CSS or JS, Write those files "
    "in the same coding pass — do not leave broken stylesheet or script "
    "hrefs for later, and do not inline them instead. "
    "Earlier messages in this thread are the brief, including an "
    "<approved_plan> or the last Plan reply. Implement that plan's "
    "## Checklist in order; do not skip items and do not ask for a new spec. "
    "If the plan says to canvas-draw, Node-export, Pillow, or "
    "base64 fake site PNGs, ignore those steps: point img src at the Asset "
    "dest paths. JS/CSS may still animate stars."
)
ASK_SYSTEM = (
    "You are answering questions about a workspace project folder on this "
    "TabbyAPI Stack host. This conversation is one thread in that workspace; "
    "extra chats share the same files. Use this thread and the project "
    "files together: earlier Plan or Ask turns are part of the brief. Do "
    "not ignore them. Use Grep, Glob, Read, and List to inspect files. Do not create, "
    "edit, delete, rename, or optimize files. Do not run Shell. Do not "
    "implement changes. Answer clearly from the conversation and the project."
)
PLAN_SYSTEM = (
    "You are Plan mode for a workspace project folder on this TabbyAPI Stack "
    "host. This conversation is one thread in that workspace; extra chats "
    "share the same files. A workspace file list is already in this prompt; "
    "only Grep, Glob, or Read a file if you need its contents. Do not List just to confirm "
    "the file list. If a plan is already in this thread, revise that plan; "
    "do not start from a blank page. Do not create, edit, delete, rename, "
    "or optimize files, and do not run Shell. "
    "Your assistant message is the plan the user will review, then click "
    "Build to implement. Never say you will write a plan — write it now. "
    "Use markdown with these headings:\n"
    "## Goal\n## Files\n## Steps\n## Assets\n## Checklist\n## Risks\n"
    "Files: concrete relative paths and what each one is for. "
    "Steps: numbered and specific enough to implement without asking again. "
    "Assets: dest paths the GPU will render after Build (hero/scene = Flux "
    "photo; logos/text/named ships = qwen-image:), shown with img src, or "
    "None. Do not plan canvas, toDataURL, Pillow, Node, or base64 fake "
    "PNGs, or placeholders reserved for later. JS/CSS starfield and warp "
    "animation is allowed and is not an Asset. Overlay canvases (stars, "
    "fireflies, particles) must be position:absolute covering the hero — "
    "never a flex or grid sibling, or they shove the title off-screen. "
    "OptimizeImage is after real "
    "files exist, not canvas export. "
    "Checklist: one `- [ ]` item per user request in this thread (each "
    "page file, each Asset dest with img src, JS/CSS effects, mobile/nav, "
    "optimize if they asked). When revising, keep earlier items and add "
    "new ones. Build will implement only this list. "
    "If they say the pasted brief is only an example, or they do not want "
    "the same theme or names, invent a different setting. Do not reuse that "
    "industry, ships, planets, or company type. "
    "Do not implement."
)
PLAN_CONTRACT_MARK = "<plan_mode>"
PLAN_USER_SUFFIX = (
    "\n\n<plan_mode>\n"
    "Write the full implementation plan now as markdown with headings Goal, "
    "Files, Steps, Assets, Checklist, and Risks. Name concrete relative "
    "paths. Number the steps. Assets are GPU dest paths (or None), not "
    "canvas/Node exports. Page pictures use img tags. Starfields may be JS. "
    "Checklist is `- [ ]` todos covering every user request; Build will "
    "follow only that list. Do not implement. Do not announce a plan — "
    "this reply is the plan. The user will click Build later.\n"
    "</plan_mode>"
)
PLAN_THEME_HINT = (
    "\n\n<plan_theme>\n"
    "The space-travel paragraph is an example only. Invent a completely "
    "different setting and names. Do not plan space, ships, planets, "
    "cruise liners, or light-speed travel.\n"
    "</plan_theme>"
)
_LAYOUT_WRITE_REFUSE = (
    "This turn is a small layout or alignment fix. Do not Write whole files. "
    "Read the existing CSS once if you need it, then emit every StrReplace "
    "in one response and stop. Leave image paths as they are on disk."
)
_LAYOUT_SRC_REFUSE = (
    "This turn is a layout fix. Do not change image paths. Use StrReplace "
    "only on CSS position or alignment rules."
)
_RASTER_WRITE_REFUSE = (
    "Do not Write PNG, JPEG, WebP, or GIF dest files. Point img src at the "
    "planned path; the GPU will save that file after the page is written."
)
_PAGE_EDIT_SUFFIXES = frozenset({".html", ".htm", ".css", ".js", ".mjs"})
_IMAGE_IN_TEXT = re.compile(
    r"(?i)[\w./-]+\.(?:png|jpe?g|webp|gif)\b"
)
PLAN_RETRY = (
    "That reply was not a plan. Write the complete implementation plan now "
    "as markdown with headings Goal, Files, Steps, Assets, Checklist, and "
    "Risks. Include concrete file paths, numbered steps, and `- [ ]` "
    "checklist items for every user request. Do not implement. "
    "Do not call tools unless you must Read a specific existing file. "
    "Do not say you will write a plan later."
)
PLAN_THEME_RETRY = (
    "That plan still uses the example theme from the pasted brief (space "
    "travel). Invent a completely different setting and names. Do not use "
    "space, ships, planets, cruise liners, or light-speed travel. Write the "
    "full plan again with Goal, Files, Steps, Assets, Checklist, and Risks. "
    "Do not implement."
)
LAYOUT_FIX_MARK = "<layout_fix>"
LAYOUT_REPORT_MARK = "<layout_report>"
LAYOUT_FIX_SUFFIX = (
    "\n\n<layout_fix>\n"
    "The user asked for a small layout or alignment change. Read the "
    "existing CSS once if you need it. Emit every StrReplace in a single "
    "response, then summarize and stop. Do not Read files again to verify. "
    "Do not Write whole files. Do not change "
    "image paths. Do not generate images.\n"
    "</layout_fix>"
)
LAYOUT_REPORT_FOOTER = (
    "Fix only the layout facts this report names (position, flex, canvas). "
    "Prefer StrReplace. Do not change image paths."
)
_LAYOUT_SERVER_BLOCK = re.compile(r"(?is)<(?:layout_fix|layout_report)\b")
_PLAN_HEADING = re.compile(r"(?m)^#{1,3}\s+\S")
_PLAN_STEPS = re.compile(r"(?m)^\s*(?:\d+[\.\)]\s+\S|[-*]\s+\S.{8,})")
_PLAN_CHECKS = re.compile(r"(?m)^\s*(?:[-*]|\d+[\.)])\s*\[\s*[xX ]?\s*\]\s+\S")
_PLAN_PATH = re.compile(
    r"\b[\w./-]+\.(?:html?|css|js|mjs|ts|tsx|jsx|json|md|py|svg|png|jpe?g|webp|gif)\b",
    re.I,
)
_PLAN_PREAMBLE = re.compile(
    r"(?is)\b(?:i will now|i(?:'m| am) (?:going to|about to)|"
    r"let me (?:now )?(?:design|write|create|plan)|"
    r"i have (?:read|listed|checked) (?:the )?(?:project|directory|workspace))\b"
)
_MUTATE_KINDS = frozenset(
    ("write", "replace", "delete", "rename", "optimize", "shell")
)
_READONLY_TOOLS = frozenset(("read", "list", "grep", "glob"))
_READONLY_REFUSE = (
    "This prompt mode is read-only. Use Grep, Glob, Read, or List, or switch "
    "to Agent to change files."
)
MODE_HINT_MARK = "<mode_hint>"
_HOWTO_PROMPT = re.compile(
    r"(?is)^\s*(?:how\s+(?:do|can|would)|what(?:'s| is)|why\b|explain)\b"
)
_PICTURE_PROMPT = re.compile(
    r"(?is)(?:qwen-image:|^(?:generate an image)|(?:^/\s*image\b)|"
    r"\b(?:generate|draw|paint|render|create|make|replace)\b[\s\S]{0,80}"
    r"\b(?:images?|pictures?|photos?|logos?|posters?|icons?)\b)"
)
_FILE_WORK_PROMPT = re.compile(
    r"(?is)\b(?:write|implement|scaffold)\b|"
    r"\b(?:create|make|build|add)\b[\s\S]{0,80}\b"
    r"(?:files?|pages?|sites?|websites?|apps?|html|css|javascript|components?)\b|"
    r"\b(?:edit|fix|change|update|delete|rename)\b[\s\S]{0,80}\b"
    r"(?:files?|html|css|js|code|folder)\b|"
    r"\b(?:index\.html|styles\.css|app\.js)\b|"
    r"\b(?:run|execute)\b[\s\S]{0,40}\b(?:command|shell|tests?|npm|pip)\b"
)
_REFUSES_IMAGES = re.compile(
    r"(?is)\b(?:do(?:\s+not|n't)\s+(?:generate|draw|create|render|make)\s+"
    r"(?:any\s+)?(?:new\s+)?(?:images?|pictures?|photos?|pics?)\b|"
    r"\bno\s+new\s+(?:images?|pictures?|photos?)\b|"
    r"\bwithout\s+(?:any\s+)?(?:new\s+)?(?:images?|pictures?|photos?)\b)"
)
BUILD_PROMPT = "Implement the approved plan above. Do not wait for more confirmation."
BUILD_CONTRACT_MARK = "<build_mode>"
BUILD_USER_SUFFIX = (
    "\n\n<build_mode>\n"
    "The plan is already approved. Write and edit project files with tools "
    "now. Do not reply with Goal, Files, Steps, Assets, Checklist, or Risks "
    "headings. Work the plan's ## Checklist in order (every `- [ ]` item). "
    "Do not skip items and do not start extra work that is not on the list. "
    "After you finish each checklist item, write Done: <exact item> on its own line. "
    "After the last item, stop calling tools and give a short summary. "
    "Site images listed under Assets are GPU PNGs: point img src at those "
    "dest paths; do not draw them on canvas or export Node/Pillow/base64 "
    "PNGs. JS/CSS may still animate stars and warp effects. Use "
    "OptimizeImage after real image files exist if they asked to optimize.\n"
    "</build_mode>"
)
_APPROVED_PLAN_RE = re.compile(r"(?is)<approved_plan>(.*?)</approved_plan>")
_CHECKLIST_SECTION_RE = re.compile(
    r"(?ims)^#{1,3}\s+(?:checklist|to-?dos?)\b[^\n]*\n(.*?)(?=^#{1,3}\s+|\Z)"
)
_CHECK_ITEM_RE = re.compile(
    r"(?m)^\s*(?:[-*]|\d+[\.)])\s*\[\s*[xX ]?\s*\]\s+(.+?\S)\s*$"
)


def normalize_agent(value: Any) -> str:
    kind = str(value or "").strip().lower()
    return kind if kind in AGENT_KINDS else "agent"


def _system_for_agent(agent: str) -> str:
    kind = normalize_agent(agent)
    if kind == "ask":
        return ASK_SYSTEM
    if kind == "plan":
        return PLAN_SYSTEM
    return CODE_SYSTEM


def _content_has_mark(content: Any, mark: str) -> bool:
    if isinstance(content, str):
        return mark in content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and mark in str(part.get("text") or ""):
                return True
            if isinstance(part, str) and mark in part:
                return True
    return False


def _append_text_content(content: Any, extra: str) -> Any:
    if isinstance(content, list):
        parts = [part if isinstance(part, dict) else part for part in content]
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                part["text"] = str(part.get("text") or "") + extra
                return parts
        return [{"type": "text", "text": extra.lstrip()}] + parts
    return str(content or "") + extra


def _plain_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return str(content or "")


def is_build_prompt(text: Any) -> bool:
    return _plain_content(text).strip().startswith(BUILD_PROMPT)


def prompt_wants_pictures(text: Any) -> bool:
    blob = _plain_content(text)
    if _HOWTO_PROMPT.match(blob) or _REFUSES_IMAGES.search(blob):
        return False
    return bool(_PICTURE_PROMPT.search(blob))


def prompt_wants_file_work(text: Any) -> bool:
    blob = _plain_content(text)
    if _HOWTO_PROMPT.match(blob):
        return False
    return bool(_FILE_WORK_PROMPT.search(blob))


def readonly_mode_targets(agent: str, text: Any) -> tuple[str, ...]:
    """Modes the user needs when Ask/Plan cannot do the asked work."""
    kind = normalize_agent(agent)
    if kind not in ("ask", "plan"):
        return ()
    pics = prompt_wants_pictures(text)
    files = prompt_wants_file_work(text)
    if kind == "plan":
        return ("chat",) if pics and not files else ()
    if files:
        return ("agent",)
    if pics:
        return ("agent", "chat")
    return ()


def attach_readonly_mode_hint(messages: list, agent: str) -> None:
    """Tell Ask/Plan to name Agent or Chat when the user asked for work."""
    kind = normalize_agent(agent)
    if kind not in ("ask", "plan"):
        return
    label = "Ask" if kind == "ask" else "Plan"
    names = {"agent": "Agent", "chat": "Chat"}
    for item in reversed(messages):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if is_build_prompt(content) or _content_has_mark(content, MODE_HINT_MARK):
            return
        targets = readonly_mode_targets(kind, content)
        if not targets:
            return
        listed = " or ".join(names[name] for name in targets)
        extra = (
            f"\n\n{MODE_HINT_MARK}\n"
            f"This prompt is {label} (read-only). The user wants work that "
            f"needs {listed}. Say that plainly. Name those modes. Do not say "
            "the product cannot generate images or write files. Do not "
            "implement.\n"
            "</mode_hint>"
        )
        item["content"] = _append_text_content(content, extra)
        return


def attach_plan_user_contract(messages: list) -> None:
    """Pin the plan-mode contract on the last user turn (server-only)."""
    for item in reversed(messages):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if is_build_prompt(content):
            return
        if readonly_mode_targets("plan", content) == ("chat",):
            return
        if _content_has_mark(content, PLAN_CONTRACT_MARK):
            return
        extra = PLAN_USER_SUFFIX
        if user_forbids_example_space(_plain_content(content)):
            content = _demote_example_space_brief(content)
            extra = PLAN_THEME_HINT + extra
        item["content"] = _append_text_content(content, extra)
        return


def plan_checklist_items(text: str) -> list[str]:
    """`- [ ]` todos from an approved plan or ## Checklist section."""
    blob = text or ""
    match = _APPROVED_PLAN_RE.search(blob)
    if match:
        blob = match.group(1) or ""
    section = _CHECKLIST_SECTION_RE.search(blob)
    if not section:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for raw in _CHECK_ITEM_RE.findall(section.group(1) or ""):
        line = " ".join(str(raw).split())
        key = line.lower()
        if not line or key in {"none", "n/a"} or key in seen:
            continue
        seen.add(key)
        found.append(line)
    return found


def _build_user_suffix(content: Any) -> str:
    items = plan_checklist_items(_plain_content(content))
    extra = ""
    if items:
        extra = (
            "Checklist to finish:\n"
            + "\n".join(f"- [ ] {item}" for item in items)
            + "\n"
        )
    return BUILD_USER_SUFFIX.replace("</build_mode>", f"{extra}</build_mode>")


def attach_build_user_contract(messages: list) -> None:
    """Pin the Build contract on the last user turn (server-only)."""
    for item in reversed(messages):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if not is_build_prompt(content):
            return
        if _content_has_mark(content, BUILD_CONTRACT_MARK):
            return
        item["content"] = _append_text_content(content, _build_user_suffix(content))
        return


_FORBID_EXAMPLE_THEME = re.compile(
    r"(?is)(?:don'?t|do not|not)\s+(?:use\s+)?(?:the\s+)?same theme"
    r"|totally new website"
    r"|totally different"
    r"|not\s+\w+\s+themed"
    r"|i don'?t want it\s+\w+\s+themed"
)
_EXAMPLE_SPACE_BRIEF = re.compile(
    r"(?i)\b(?:space travel|solar system|cruise liner|light[- ]speed|"
    r"trips to any planet)\b"
)
_PLAN_SPACE_THEME = re.compile(
    r"(?i)\b(?:space travel|solar system|cruise liner|light[- ]speed|"
    r"luxury (?:space )?(?:liner|cruise)|warp drive|starship|spaceship|"
    r"planet(?:s)? (?:mars|neptune|jupiter)|observation deck)\b"
)
_LAYOUT_ASK = re.compile(
    r"(?i)(?:"
    r"\b(?:align(?:ment|ed)?|off (?:to the )?left|off[- ]screen|"
    r"center(?:ed)?|pushed off|too (?:far )?(?:left|right)|layout)\b"
    r"|(?<![/\w.])hero(?![\w.-])"
    r")"
)
_IMAGE_GEN_ASK = re.compile(
    r"(?is)\b(?:generate|draw|render)\s+"
    r"(?:(?:a|an|the|new|real|gpu)\s+)*"
    r"(?:images?|pictures?|photos?|pics?|logo)\b"
    r"|\bcreate\s+(?:(?:a|an|the|new)\s+)+(?:logo|hero(?:\s+photo)?)\b"
    r"|\bmissing (?:its |the )?(?:pictures?|images?|photos?|logo)"
    r"|qwen-image:"
)


def user_forbids_example_space(text: str) -> bool:
    user = text or ""
    if re.search(r"(?i)(?:don'?t|do not|not).{0,40}space\s+themed", user):
        return True
    return bool(
        _FORBID_EXAMPLE_THEME.search(user) and _EXAMPLE_SPACE_BRIEF.search(user)
    )


def plan_copies_forbidden_example(user_text: str, plan_text: str) -> bool:
    """True when they forbade the pasted space brief but the plan still uses it."""
    if not user_forbids_example_space(user_text or ""):
        return False
    return bool(_PLAN_SPACE_THEME.search(_strip_think(plan_text or "")))


_SPACE_EXAMPLE_PARA = re.compile(
    r"(?is)(you are designing a website for a space travel company\..+?)(?=\n{2,}|\Z)"
)


def _demote_example_space_brief(content: Any) -> Any:
    """Label the pasted space brief so it is not read as the site to build."""

    def wrap(text: str) -> str:
        if "EXAMPLE ONLY" in (text or ""):
            return text
        return _SPACE_EXAMPLE_PARA.sub(
            r"EXAMPLE ONLY — do not use this theme or names:\n\1",
            text or "",
            count=1,
        )

    if isinstance(content, str):
        return wrap(content)
    if isinstance(content, list):
        parts = [part if isinstance(part, dict) else part for part in content]
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                part["text"] = wrap(str(part.get("text") or ""))
                return parts
        return parts
    return content


def _layout_fix_user_words(text: str) -> str:
    """User's own words, excluding server <layout_fix> / <layout_report> suffixes."""
    return _LAYOUT_SERVER_BLOCK.split(str(text or ""), maxsplit=1)[0].strip()


def is_layout_fix_prompt(text: Any) -> bool:
    raw = _plain_content(text).strip()
    if not raw or is_build_prompt(raw):
        return False
    user = _layout_fix_user_words(raw)
    if _IMAGE_GEN_ASK.search(user):
        return False
    if LAYOUT_FIX_MARK in raw:
        return True
    if not user or len(user) > 240:
        return False
    return bool(_LAYOUT_ASK.search(user))


def _layout_report_suffix(username: str, chat_id: str) -> str:
    facts = ""
    if username and chat_id:
        try:
            facts = workspace.layout_report(username, chat_id)
        except Exception:
            facts = "Could not read workspace layout."
    facts = (facts or "No HTML page in this workspace.").strip()
    return (
        f"\n\n{LAYOUT_REPORT_MARK}\n"
        f"{facts}\n"
        f"{LAYOUT_REPORT_FOOTER}\n"
        "</layout_report>"
    )


def attach_layout_fix_contract(
    messages: list, username: str = "", chat_id: str = ""
) -> None:
    """Pin a surgical-edit contract on a short hero/alignment follow-up."""
    for item in reversed(messages):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if is_build_prompt(content):
            return
        if _content_has_mark(content, LAYOUT_FIX_MARK):
            return
        if not is_layout_fix_prompt(content):
            return
        extra = LAYOUT_FIX_SUFFIX + _layout_report_suffix(username, chat_id)
        item["content"] = _append_text_content(content, extra)
        return


def _strip_think(text: str) -> str:
    return re.sub(r"(?is)<think>.*?</think>", " ", text or "").strip()


def plan_looks_complete(text: str) -> bool:
    """True when the reply is a reviewable plan, not a promise to write one."""
    body = _strip_think(text)
    if not body:
        return False
    headings = len(_PLAN_HEADING.findall(body))
    steps = len(_PLAN_STEPS.findall(body)) + len(_PLAN_CHECKS.findall(body))
    paths = len(_PLAN_PATH.findall(body))
    if _PLAN_PREAMBLE.search(body) and headings < 2 and steps < 3:
        return False
    if headings >= 3 and len(body) >= 160:
        return True
    if headings >= 2 and steps >= 3 and len(body) >= 160:
        return True
    if steps >= 5 and paths >= 1 and len(body) >= 240:
        return True
    return False


def workspace_file_brief(username: str, chat_id: str) -> str:
    """Short path list so a fresh workspace thread is not blind."""
    if not username or not chat_id:
        return ""
    try:
        data = workspace.listing(username, chat_id)
    except Exception:
        return ""
    files = [
        str(row.get("path") or "")
        for row in data.get("files") or []
        if isinstance(row, dict) and row.get("kind") != "dir" and row.get("path")
    ]
    if not files:
        return "Workspace files: (empty project)."
    files.sort()
    rasters = [
        Path(path).suffix.lower()
        for path in files
        if Path(path).suffix.lower() in workspace.IMAGE_SUFFIXES
    ]
    extra = 0
    if len(files) > MAX_BRIEF_FILES:
        extra = len(files) - MAX_BRIEF_FILES
        files = files[:MAX_BRIEF_FILES]
    count = int(data.get("count") or len(files) + extra)
    text = ", ".join(files)
    if extra:
        text += f", …and {extra} more"
    line = f"Workspace files ({count}): {text}."
    if any(suffix != ".png" for suffix in rasters):
        line += (
            " On disk, generated pictures are .webp; do not change src to .png."
        )
    return line


def code_system_for(username: str, chat_id: str, agent: str = "agent") -> str:
    base = _system_for_agent(agent)
    parts = [base]
    try:
        notes = workspace.workspace_instructions(username, chat_id)
    except Exception:
        notes = ""
    if notes:
        parts.append("Workspace instructions (AGENTS.md):\n" + notes)
    brief = workspace_file_brief(username, chat_id)
    if brief:
        parts.append(brief)
    return "\n\n".join(parts)


_WRITE_NAMES = (
    "write",
    "write_file",
    "write_to_file",
    "create_file",
)
_REPLACE_NAMES = (
    "strreplace",
    "search_replace",
    "replace_in_file",
    "edit_file",
)
_READ_NAMES = ("read", "read_file", "readfile")
_GREP_NAMES = ("grep", "search", "rg")
_GLOB_NAMES = ("glob", "find_files", "find")
_DELETE_NAMES = ("delete", "delete_file", "remove_file")
_RENAME_NAMES = ("rename", "rename_file", "move_file", "mv")
_LIST_NAMES = ("list", "list_dir", "listdir", "list_files")
_OPTIMIZE_NAMES = ("optimizeimage", "optimize_image", "compress_image", "resize_image")
_SHELL_NAMES = ("shell", "bash", "run_command", "run_terminal_cmd")


def code_tool_specs(agent: str = "agent") -> list[ToolSpec]:
    specs = [
        ToolSpec(
            type="function",
            function=Function(
                name="Write",
                    description="Create or overwrite a whole text file. For a small layout, alignment, or path edit, use StrReplace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path, e.g. index.html"},
                        "contents": {"type": "string", "description": "Full file contents"},
                    },
                    "required": ["path", "contents"],
                },
            ),
        ),
        ToolSpec(
            type="function",
            function=Function(
                name="StrReplace",
                description="Replace one exact string in a project file.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            ),
        ),
        ToolSpec(
            type="function",
            function=Function(
                name="Rename",
                description="Rename or move a project file.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Current relative path"},
                        "to": {"type": "string", "description": "New relative path"},
                    },
                    "required": ["path", "to"],
                },
            ),
        ),
        ToolSpec(
            type="function",
            function=Function(
                name="Read",
                description="Read a project file. Use offset and limit for large files.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "offset": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "1-based start line. Omit to read the whole file.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Maximum number of lines to return.",
                        },
                    },
                    "required": ["path"],
                },
            ),
        ),
        ToolSpec(
            type="function",
            function=Function(
                name="Delete",
                description=(
                    "Delete one unused file or an empty folder. Always refuses HTML, CSS, "
                    "and JS. Refuses files the page still uses unless the user named that "
                    "exact path. Do not use this to clear a project."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative file path, or an empty folder.",
                        }
                    },
                    "required": ["path"],
                },
            ),
        ),
        ToolSpec(
            type="function",
            function=Function(
                name="List",
                description="List files in the project (optional subdirectory).",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory to list, or omit for the project root.",
                        }
                    },
                },
            ),
        ),
        ToolSpec(
            type="function",
            function=Function(
                name="Grep",
                description="Search project text files with a regular expression.",
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Regular expression to search for.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Optional subdirectory or file to search.",
                        },
                        "glob": {
                            "type": "string",
                            "description": "Optional filename glob, e.g. **/*.css",
                        },
                    },
                    "required": ["pattern"],
                },
            ),
        ),
        ToolSpec(
            type="function",
            function=Function(
                name="Glob",
                description="List project files matching a glob pattern.",
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Glob, e.g. **/*.js or src/**/*.css",
                        }
                    },
                    "required": ["pattern"],
                },
            ),
        ),
        ToolSpec(
            type="function",
            function=Function(
                name="OptimizeImage",
                description=(
                    "Optimize, resize, convert, or crop a uniform border from one "
                    "existing project image. If path is .png but only .webp exists, "
                    "that file is used. Omit output_path and format to optimize in "
                    "place. Converting updates code references and removes the old "
                    "file. Set trim_border to crop a white or black frame; do not "
                    "use CSS for that."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path of the existing project image.",
                        },
                        "output_path": {
                            "type": "string",
                            "description": (
                                "Optional destination. Omit to overwrite in place, or when "
                                "converting to create name.optimized.ext."
                            ),
                        },
                        "max_width": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 8192,
                            "description": "Optional maximum width while preserving aspect ratio.",
                        },
                        "max_height": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 8192,
                            "description": "Optional maximum height while preserving aspect ratio.",
                        },
                        "quality": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 82,
                            "description": "JPEG or WebP quality.",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["original", "png", "jpeg", "webp", "gif"],
                            "default": "original",
                        },
                        "lossless": {
                            "type": "boolean",
                            "default": False,
                            "description": "Use lossless encoding when writing WebP.",
                        },
                        "trim_border": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Crop a uniform edge color (white card frame, letterbox) "
                                "before optimizing."
                            ),
                        },
                    },
                    "required": ["path"],
                },
            ),
        ),
        ToolSpec(
            type="function",
            function=Function(
                name="Shell",
                description=(
                    "Run a command in this workspace's project container. cwd is /work. "
                    "Use for installs, builds, and checks. Prefer file tools for edits. "
                    "Do not delete, move, or overwrite project files from Shell."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Shell command to run, e.g. python3 -m http.server --help",
                        }
                    },
                    "required": ["command"],
                },
            ),
        ),
    ]
    if normalize_agent(agent) == "agent":
        return specs
    return [spec for spec in specs if _kind(spec.function.name) in _READONLY_TOOLS]


def _arg_path(args: dict) -> str:
    for key in ("path", "file_path", "target_file", "output_path"):
        value = args.get(key)
        if value:
            return str(value).strip()
    return ""


def _arg_contents(args: dict) -> str:
    for key in ("contents", "content", "text"):
        if key in args and args[key] is not None:
            return args[key] if isinstance(args[key], str) else str(args[key])
    return ""


def _arg_dest(args: dict) -> str:
    for key in ("to", "dest", "new_path", "destination"):
        value = args.get(key)
        if value:
            return str(value).strip()
    return ""


def _arg_bool(args: dict, key: str, default: bool = False) -> bool:
    if key not in args or args.get(key) is None:
        return default
    value = args.get(key)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _tool_pairs(message) -> list[tuple[str, dict, str]]:
    pairs: list[tuple[str, dict, str]] = []
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
        call_id = str(getattr(call, "id", "") or "")
        pairs.append((str(name), args, call_id))
    return pairs


def _kind(name: str) -> str:
    from common.image_paths import match_tool_name

    key = (name or "").strip()
    if match_tool_name([key], _WRITE_NAMES):
        return "write"
    if match_tool_name([key], _REPLACE_NAMES):
        return "replace"
    if match_tool_name([key], _READ_NAMES):
        return "read"
    if match_tool_name([key], _GREP_NAMES):
        return "grep"
    if match_tool_name([key], _GLOB_NAMES):
        return "glob"
    if match_tool_name([key], _DELETE_NAMES):
        return "delete"
    if match_tool_name([key], _RENAME_NAMES):
        return "rename"
    if match_tool_name([key], _LIST_NAMES):
        return "list"
    if match_tool_name([key], _OPTIMIZE_NAMES):
        return "optimize"
    if match_tool_name([key], _SHELL_NAMES):
        return "shell"
    return ""


def _existing_page_file(username: str, chat_id: str, rel: str) -> bool:
    try:
        root = workspace.workspace_root(username, chat_id, create=False)
        dest = workspace.resolve_rel(root, rel)
    except (OSError, ValueError):
        return False
    return dest.is_file()


def _latest_user_text(messages: Any) -> str:
    for item in reversed(list(messages or [])):
        if isinstance(item, dict):
            role = item.get("role")
            content = item.get("content")
        else:
            role = getattr(item, "role", None)
            content = getattr(item, "content", None)
        if role == "user":
            return _plain_content(content)
    return ""


def _all_user_text(messages: Any) -> str:
    parts: list[str] = []
    for item in list(messages or []):
        if isinstance(item, dict):
            role = item.get("role")
            content = item.get("content")
        else:
            role = getattr(item, "role", None)
            content = getattr(item, "content", None)
        if role == "user":
            parts.append(_plain_content(content))
    return "\n".join(parts)


def _user_named_path(user_text: str, rel: str) -> bool:
    text = str(user_text or "")
    path = str(rel or "").strip().replace("\\", "/")
    if not text.strip() or not path:
        return False
    name = Path(path).name
    if re.search(rf"(?i)(?<![A-Za-z0-9._/-]){re.escape(path)}(?![A-Za-z0-9._-])", text):
        return True
    if name != path and re.search(
        rf"(?i)(?<![A-Za-z0-9._-]){re.escape(name)}(?![A-Za-z0-9._-])", text
    ):
        return True
    return False


_SHELL_DESTROY = re.compile(
    r"(?is)(?:^|[\n;&|`]|\$\(|\bthen\b|\bdo\b)\s*(?:"
    r"rm\b|rmdir\b|unlink\b|shred\b|"
    r"find\b.{0,200}\s-(?:delete|exec)\b|"
    r"truncate\b|"
    r"dd\b|"
    r"python(?:3)?\b.{0,160}\b(?:remove|unlink|rmtree)\b|"
    r"(?:os|pathlib|shutil)\.(?:remove|unlink|rmtree)\b"
    r")"
)


def _shell_is_destructive(command: str) -> bool:
    text = str(command or "").strip()
    if not text:
        return False
    if _SHELL_DESTROY.search(text):
        return True
    lowered = text.lower()
    return any(
        token in lowered
        for token in ("shutil.rmtree", "os.remove", "os.unlink", "path.unlink")
    )


def _delete_refusal(
    username: str,
    chat_id: str,
    rel: str,
    *,
    user_text: str,
    protected: set[str],
    deletes_used: int,
) -> str:
    if deletes_used >= MAX_DELETES_PER_TURN:
        return (
            f"Stopped deleting after {MAX_DELETES_PER_TURN} files this turn. "
            "Unused cleanup only removes empty folders and files the page does not use."
        )
    suffix = Path(rel).suffix.lower()
    if suffix in workspace.CORE_KEEP_SUFFIXES:
        return (
            f"{rel} is part of the site. Agent will not delete HTML, CSS, or JS files."
        )
    if _user_named_path(user_text, rel):
        return ""
    if rel in protected:
        return (
            f"{rel} is still used by the project (or was when this turn started). "
            "OptimizeImage updates references; do not delete used assets."
        )
    if rel in workspace.referenced_project_paths(username, chat_id):
        return f"{rel} is still referenced by the project."
    remaining = [
        row["path"]
        for row in workspace.list_files(username, chat_id)
        if row.get("path") and row["path"] != rel
    ]
    if not remaining and (
        suffix in workspace.CORE_KEEP_SUFFIXES or workspace.is_image_path(rel)
    ):
        return f"{rel} is the last site file. Refusing to empty the workspace."
    return ""


def _note_change(sink: Optional[dict], kind: str, path: str, **extra: str) -> None:
    """Record what a mutating tool actually changed, for the browser to act on.

    The status label is prose meant for humans and is free to be reworded; this
    is the machine-readable channel, so nothing has to parse the label.
    """
    if sink is None or not path:
        return
    sink.clear()
    sink.update({"kind": kind, "path": path})
    for key, value in extra.items():
        if value:
            sink[key] = value


def execute_tool(
    username: str,
    chat_id: str,
    name: str,
    args: dict,
    agent: str = "agent",
    *,
    user_text: str = "",
    protected: Optional[set[str]] = None,
    deletes_used: int = 0,
    history_run: str = "",
) -> tuple[str, str, dict]:
    """Run one tool. Returns (status_label, result_text, change).

    change is {} for read-only tools and for any failure, otherwise
    {"kind": write|edit|delete|rename|optimize, "path": <resolved path>}.
    """
    token = workspace.push_history_run(history_run)
    change: dict[str, str] = {}
    try:
        label, result = _execute_tool(
            username,
            chat_id,
            name,
            args,
            agent,
            user_text=user_text,
            protected=protected,
            deletes_used=deletes_used,
            change=change,
        )
        return label, result, change
    finally:
        workspace.pop_history_run(token)


def _execute_tool(
    username: str,
    chat_id: str,
    name: str,
    args: dict,
    agent: str = "agent",
    *,
    user_text: str = "",
    protected: Optional[set[str]] = None,
    deletes_used: int = 0,
    change: Optional[dict] = None,
) -> tuple[str, str]:
    kind = _kind(name)
    if normalize_agent(agent) != "agent" and kind in _MUTATE_KINDS:
        return "Tool error", _READONLY_REFUSE
    rel = _arg_path(args)
    if kind == "shell":
        command = str(args.get("command") or args.get("cmd") or "").strip()
        if not command:
            return "Tool error", "command is required"
        if _shell_is_destructive(command):
            return (
                "Tool error",
                "Shell cannot delete or replace project files. Use OptimizeImage "
                "for images, and Delete only for an empty folder or a file the "
                "user named.",
            )
        from ui import codebox

        try:
            code, output = codebox.run_shell(username, chat_id, command)
        except codebox.CodeboxError as exc:
            return "Tool error", str(exc)
        text = output if output.strip() else "(no output)"
        if code:
            text = f"exit {code}\n{text}"
        return "Running command", text
    if kind == "list":
        prefix = rel.rstrip("/")
        if prefix in (".",):
            prefix = ""
        data = workspace.listing(username, chat_id)
        rows = [row for row in data.get("files") or [] if isinstance(row, dict)]
        if prefix:
            rows = [
                row
                for row in rows
                if row.get("path") == prefix
                or str(row.get("path") or "").startswith(prefix + "/")
            ]
        if not rows:
            return "Listing files", "(empty project)" if not prefix else f"{prefix}: no files"
        lines: list[str] = []
        for row in rows:
            path = str(row.get("path") or "")
            if row.get("kind") == "dir":
                lines.append(f"{path}/ (folder)")
            else:
                lines.append(f"{path} ({row.get('size', 0)} bytes)")
        return "Listing files", "\n".join(lines)
    if kind == "grep":
        pattern = str(args.get("pattern") or args.get("query") or "").strip()
        if not pattern:
            return "Tool error", "pattern is required"
        return (
            "Searching project",
            workspace.grep_text(
                username,
                chat_id,
                pattern,
                path=rel,
                glob_pat=str(args.get("glob") or args.get("include") or ""),
            ),
        )
    if kind == "glob":
        pattern = str(args.get("pattern") or args.get("glob") or rel or "").strip()
        if not pattern:
            return "Tool error", "pattern is required"
        return "Finding files", workspace.glob_paths(username, chat_id, pattern)
    if not rel:
        return "Tool error", "path is required"
    if kind == "write":
        if Path(rel).suffix.lower() in workspace.IMAGE_SUFFIXES:
            return "Tool error", _RASTER_WRITE_REFUSE
        if (
            is_layout_fix_prompt(user_text)
            and Path(rel).suffix.lower() in _PAGE_EDIT_SUFFIXES
            and _existing_page_file(username, chat_id, rel)
        ):
            return "Tool error", _LAYOUT_WRITE_REFUSE
        created = not workspace._workspace_file_exists(username, chat_id, rel)
        written = workspace.write_text(username, chat_id, rel, _arg_contents(args))
        extra = {"created": "1"} if created else {}
        _note_change(change, "write", written, **extra)
        return f"Writing {written}", f"Wrote {written}"
    if kind == "replace":
        old = str(args.get("old_string") or args.get("oldStr") or "")
        new = str(args.get("new_string") or args.get("newStr") or "")
        if is_layout_fix_prompt(user_text):
            old_imgs = set(_IMAGE_IN_TEXT.findall(old))
            new_imgs = set(_IMAGE_IN_TEXT.findall(new))
            if old_imgs != new_imgs:
                return "Tool error", _LAYOUT_SRC_REFUSE
        workspace.str_replace(username, chat_id, rel, old, new)
        _note_change(change, "edit", rel)
        return f"Editing {rel}", f"Updated {rel}"
    if kind == "read":
        offset = args.get("offset")
        limit = args.get("limit")
        try:
            offset_n = int(offset) if offset is not None else None
        except (TypeError, ValueError):
            offset_n = None
        try:
            limit_n = int(limit) if limit is not None else None
        except (TypeError, ValueError):
            limit_n = None
        return (
            f"Reading {rel}",
            workspace.read_text_window(username, chat_id, rel, offset_n, limit_n),
        )
    if kind == "delete":
        root = workspace.workspace_root(username, chat_id, create=False)
        dest = workspace.resolve_rel(root, rel)
        if dest.is_dir():
            written = workspace.delete_empty_dir(username, chat_id, rel)
            _note_change(change, "delete", written)
            return f"Deleting {written}", f"Removed empty folder {written}"
        reason = _delete_refusal(
            username,
            chat_id,
            dest.relative_to(root.resolve()).as_posix() if dest.exists() else rel,
            user_text=user_text,
            protected=set(protected or ()),
            deletes_used=deletes_used,
        )
        if reason:
            return "Tool error", reason
        workspace.delete_file(username, chat_id, rel)
        _note_change(change, "delete", rel)
        return f"Deleting {rel}", f"Deleted {rel}"
    if kind == "rename":
        dest = _arg_dest(args)
        if not dest:
            return "Tool error", "to is required"
        written = workspace.rename_file(username, chat_id, rel, dest)
        _note_change(change, "rename", written, previous=rel)
        return f"Renaming {written}", f"Renamed {rel} to {written}"
    if kind == "optimize":
        result = workspace.optimize_image(
            username,
            chat_id,
            rel,
            output_path=str(args.get("output_path") or ""),
            max_width=args.get("max_width"),
            max_height=args.get("max_height"),
            quality=args.get("quality", 82),
            output_format=str(args.get("format") or "original"),
            lossless=_arg_bool(args, "lossless", False),
            trim_border=_arg_bool(args, "trim_border", False),
        )
        _note_change(change, "optimize", str(result.get("path") or ""))
        return f"Optimizing {result['path']}", json.dumps(result, separators=(",", ":"))
    return (
        "Tool error",
        f"Unknown tool {name!r}. Use Grep, Glob, Write, StrReplace, Read, Rename, Delete, List, OptimizeImage, or Shell.",
    )


def _assistant_message(response):
    choices = getattr(response, "choices", None) or []
    if not choices:
        return None
    return getattr(choices[0], "message", None)


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


def summarize_writes(paths: list[str]) -> str:
    names = [name for name in paths if name]
    if not names:
        return "No files were written."
    if len(names) == 1:
        return f"Wrote {names[0]}. Download the zip from Files when you want a copy."
    return (
        "Wrote " + ", ".join(names) + ". Download the zip from Files when you want a copy."
    )


def truncate_step_text(text: Any, limit: int = MAX_STEP_RESULT) -> str:
    body = str(text or "")
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + "…"


def tool_step_args(args: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(args, dict):
        return out
    path = _arg_path(args)
    if path:
        out["path"] = path
    dest = _arg_dest(args)
    if dest:
        out["to"] = dest
    cmd = str(args.get("command") or args.get("cmd") or "").strip()
    if cmd:
        out["command"] = truncate_step_text(cmd, MAX_STEP_ARG)
    return out


def tool_step_payload(name: str, args: dict, label: str, result: str) -> dict[str, Any]:
    return {
        "type": "tool",
        "name": str(name or ""),
        "label": str(label or ""),
        "args": tool_step_args(args if isinstance(args, dict) else {}),
        "result": truncate_step_text(result),
    }


def format_agent_step_comment(step: dict[str, Any]) -> str:
    return f"{AGENT_STEP_MARK} {json.dumps(step, separators=(',', ':'), ensure_ascii=False)}"


def remaining_stream_text(final: str, streamed: str) -> str:
    """Text still needed after live content deltas. Empty means do not dump again.

    The final text arrives stripped while the deltas are raw, so both sides are
    compared stripped. A lone leading newline in the deltas used to defeat the
    prefix test and replay the whole answer under it.
    """
    final = str(final or "")
    lean_final = final.strip()
    lean_streamed = str(streamed or "").strip()
    if not lean_final or lean_final == lean_streamed:
        return ""
    if not lean_streamed:
        return final
    if lean_streamed.startswith(lean_final):
        return ""
    if lean_final.startswith(lean_streamed):
        return lean_final[len(lean_streamed) :]
    return final


def parse_completion_chunk(raw: Any) -> Optional[dict[str, Any]]:
    if raw is None or raw == "[DONE]":
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    text = str(raw).strip()
    if text.startswith("data:"):
        text = text[5:].strip()
    if not text or text == "[DONE]":
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def final_code_text(text: str, written: list[str]) -> str:
    body = (text or "").strip()
    if body:
        return body
    return summarize_writes(written)
