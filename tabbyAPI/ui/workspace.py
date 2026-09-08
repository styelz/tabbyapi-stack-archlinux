"""Jailed per-user, per-chat project folder for UI Code mode."""

from __future__ import annotations

import contextvars
import difflib
import fnmatch
import io
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import socket
import threading
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_PAGE_SUFFIXES = frozenset({".html", ".htm", ".css", ".js", ".mjs"})
# Editing is limited to what the console can safely round-trip as UTF-8 text.
TEXT_SUFFIXES = frozenset(
    {
        ".html",
        ".htm",
        ".css",
        ".js",
        ".mjs",
        ".json",
        ".jsx",
        ".ts",
        ".tsx",
        ".md",
        ".txt",
        ".svg",
        ".xml",
        ".yml",
        ".yaml",
        ".csv",
        ".py",
        ".sh",
        ".php",
        ".toml",
        ".ini",
        ".conf",
    }
)
PAGE_SUFFIXES = frozenset({".html", ".htm"})
# Agent Delete may not drop these, even if the user names the exact path.
CORE_KEEP_SUFFIXES = frozenset(
    {".html", ".htm", ".css", ".js", ".mjs", ".jsx", ".ts", ".tsx"}
)
MAX_FILES = 2000
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_GREP_MATCHES = 200
MAX_GREP_LINE = 400
LAYOUT_REPORT_MAX = 2048
INSTRUCTIONS_NAMES = ("AGENTS.md", ".tabby.md")
INSTRUCTIONS_MAX = 8000
CLONE_TIMEOUT_S = 120
# Match git log's window so Recent commits is populated without a follow-up Pull.
CLONE_DEPTH = 50
SKIP_LISTING_NAMES = frozenset({".gitconfig", ".git-credentials"})
HISTORY_SUFFIX = ".file-history"
MAX_HISTORY_VERSIONS = 40
DRAFTS_SUFFIX = ".drafts.json"
DRAFTS_MAX_BYTES = 4 * 1024 * 1024
MAX_DRAFTS = 40
_DRAFTS_LOCK = threading.Lock()

_WORK_DIR: Optional[Path] = None
_HISTORY_LOCK = threading.Lock()
_HISTORY_RUN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_HISTORY_RUN = contextvars.ContextVar("tabby_history_run", default="")


def _clean_history_run(raw: Any) -> str:
    text = str(raw or "").strip()
    return text if _HISTORY_RUN_RE.fullmatch(text) else ""


def push_history_run(run_id: Any) -> contextvars.Token:
    """Tag file-history snapshots for one Code-mode prompt run."""
    return _HISTORY_RUN.set(_clean_history_run(run_id))


def pop_history_run(token: contextvars.Token) -> None:
    _HISTORY_RUN.reset(token)


def _active_history_run(explicit: Any = None) -> str:
    return _clean_history_run(explicit) or str(_HISTORY_RUN.get() or "")


def workspaces_dir() -> Path:
    if _WORK_DIR is not None:
        return _WORK_DIR
    from common.gpu_mode import GENERATED_DIR

    path = GENERATED_DIR / "ui_workspaces"
    path.mkdir(parents=True, exist_ok=True)
    return path


def set_workspaces_dir(path: Optional[Path]) -> None:
    global _WORK_DIR
    _WORK_DIR = path


def safe_name(raw: str) -> str:
    # Dots survive the character filter, so a chat id of "." or ".." would
    # otherwise land on the parent folder and reach another chat's files.
    name = SAFE_NAME_RE.sub("_", str(raw or "").strip())[:80]
    if not name or set(name) <= {"."}:
        return "user"
    return name


def user_dir(username: str) -> Path:
    return workspaces_dir() / safe_name(username)


def workspace_root(
    username: str, chat_id: str, *, create: bool = False, box: bool = True
) -> Path:
    root = user_dir(username) / safe_name(chat_id)
    if create:
        root.mkdir(parents=True, exist_ok=True)
        if box and _WORK_DIR is None:
            from ui.codebox import try_ensure_container

            try_ensure_container(username, chat_id)
    return root


def rel_parts(rel: str) -> tuple[str, ...]:
    text = str(rel or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or text.startswith("~"):
        raise ValueError("Invalid path")
    path = Path(text)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError("Invalid path")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts:
        raise ValueError("Invalid path")
    return parts


def resolve_rel(root: Path, rel: str) -> Path:
    parts = rel_parts(rel)
    base = root.resolve()
    dest = (base.joinpath(*parts)).resolve()
    if dest != base and base not in dest.parents:
        raise ValueError("Invalid path")
    return dest


def _write_bytes_nofollow(root: Path, rel: str, data: bytes) -> None:
    """Write under root without following symlinks in any path component."""
    parts = rel_parts(rel)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fds: list[int] = []
    try:
        fds.append(os.open(str(root.resolve()), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        for part in parts[:-1]:
            parent = fds[-1]
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | nofollow,
                    dir_fd=parent,
                )
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=parent)
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | nofollow,
                    dir_fd=parent,
                )
            except OSError as exc:
                raise ValueError("Invalid path") from exc
            fds.append(child)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | nofollow
        try:
            fd = os.open(parts[-1], flags, 0o600, dir_fd=fds[-1])
        except OSError as exc:
            raise ValueError("Invalid path") from exc
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    finally:
        for handle in reversed(fds):
            os.close(handle)


def _is_inside(root: Path, path: Path) -> bool:
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    return resolved == base or base in resolved.parents


def skip_listing_rel(rel: str) -> bool:
    """True for git metadata that must not appear in Files, zip, grep, or caps."""
    text = str(rel or "").replace("\\", "/").strip("/")
    if not text:
        return False
    parts = Path(text).parts
    if ".git" in parts:
        return True
    return bool(parts) and parts[-1] in SKIP_LISTING_NAMES


def _skip_listing_path(root: Path, path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return True
    return skip_listing_rel(rel)


def _iter_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if _skip_listing_path(root, path):
            continue
        if _is_inside(root, path):
            out.append(path)
    return out


def _iter_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    try:
        base = root.resolve()
    except OSError:
        return []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_dir():
            continue
        if _skip_listing_path(root, path):
            continue
        if _is_inside(root, path):
            try:
                if path.resolve() == base:
                    continue
            except OSError:
                continue
            out.append(path)
    return out


def _prune_empty_parents(root: Path, start: Path) -> None:
    parent = start
    try:
        base = root.resolve()
    except OSError:
        return
    while parent != base and parent.is_dir():
        try:
            if any(parent.iterdir()):
                break
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _stats(root: Path) -> tuple[int, int]:
    files = _iter_files(root)
    return len(files), sum(path.stat().st_size for path in files)


def has_files(username: str, chat_id: str) -> bool:
    """True as soon as a listed file turns up, so a badge check stays cheap."""
    root = workspace_root(username, chat_id, create=False)
    if not root.is_dir():
        return False
    pending = [root]
    while pending:
        try:
            entries = list(os.scandir(pending.pop()))
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            name = str(entry.name or "")
            if name == ".git" or name in SKIP_LISTING_NAMES:
                continue
            try:
                if entry.is_file():
                    return True
                if entry.is_dir():
                    pending.append(entry.path)
            except OSError:
                continue
    return False


def chats_with_files(username: str, chat_ids: list[str]) -> list[str]:
    """Which of these chats own a project folder that is not empty."""
    return [str(cid) for cid in chat_ids if str(cid).strip() and has_files(username, str(cid))]


_SKIP_PROJECT_SUFFIXES = (".codebox", HISTORY_SUFFIX, ".drafts.json")


def list_project_ids(username: str) -> list[str]:
    """Workspace folder names under this account that still have files."""
    root = user_dir(username)
    if not root.is_dir():
        return []
    out: list[str] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    for entry in entries:
        name = str(entry.name or "")
        if not name or name.startswith("."):
            continue
        if any(name.endswith(suffix) for suffix in _SKIP_PROJECT_SUFFIXES):
            continue
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
        except OSError:
            continue
        if has_files(username, name):
            out.append(name)
    return out


def _merge_tree(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.rglob("*")):
        if path.is_symlink():
            continue
        try:
            rel = path.relative_to(src)
        except ValueError:
            continue
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file() or target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.replace(target)
        except OSError:
            try:
                target.write_bytes(path.read_bytes())
            except OSError:
                continue


def merge_workspace_dirs(username: str, dest_id: str, source_ids: list[str]) -> None:
    """Move project files saved under nested chat ids into the workspace folder."""
    dest_name = safe_name(dest_id)
    dest = workspace_root(username, dest_name, create=False, box=False)
    try:
        dest_resolved = dest.resolve() if dest.exists() else dest
    except OSError:
        dest_resolved = dest
    for raw in source_ids:
        src_name = safe_name(raw)
        if not src_name or src_name == dest_name:
            continue
        src = workspace_root(username, src_name, create=False, box=False)
        if not src.is_dir():
            continue
        try:
            if src.resolve() == dest_resolved:
                continue
        except OSError:
            continue
        if not dest.is_dir():
            dest = workspace_root(username, dest_name, create=True, box=False)
            try:
                dest_resolved = dest.resolve()
            except OSError:
                dest_resolved = dest
        _merge_tree(src, dest)
        shutil.rmtree(src, ignore_errors=True)


def _check_caps(root: Path, *, extra_bytes: int = 0, extra_files: int = 0) -> None:
    count, total = _stats(root)
    if count + extra_files > MAX_FILES:
        raise ValueError("This chat's project has too many files.")
    if total + extra_bytes > MAX_TOTAL_BYTES:
        raise ValueError("This chat's project is too large.")


def is_text_path(rel: str) -> bool:
    return Path(str(rel or "")).suffix.lower() in TEXT_SUFFIXES


def is_image_path(rel: str) -> bool:
    return Path(str(rel or "")).suffix.lower() in IMAGE_SUFFIXES


def upload_name(rel: str, filename: str = "") -> str:
    """Keep a user-supplied relative path, or fall back to the upload's name."""
    text = str(rel or "").strip().replace("\\", "/")
    if not text:
        text = Path(str(filename or "").strip().replace("\\", "/")).name
    if not text or text.startswith("/") or text.startswith("~"):
        raise ValueError("Invalid path")
    path = Path(text)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError("Invalid path")
    # Drive-prefixed names from a desktop picker, e.g. C:/Users/a.html
    if path.parts and ":" in path.parts[0]:
        text = path.name
    if not text or text in (".", ".."):
        raise ValueError("Invalid path")
    return text


def add_upload(username: str, chat_id: str, rel: str, data: bytes, filename: str = "") -> str:
    """Write a user-picked text or image file into this chat's project."""
    raw = data if isinstance(data, (bytes, bytearray)) else bytes(data or b"")
    name = upload_name(rel, filename)
    if is_image_path(name):
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("Image must be under 8 MB.")
        return copy_bytes(username, chat_id, name, raw)
    if is_text_path(name):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("File is not UTF-8 text.") from exc
        return write_text(username, chat_id, name, text)
    raise ValueError("Only text and image files can be added.")


def raster_head_ok(head: bytes) -> bool:
    """True for a real PNG/JPEG/WebP/GIF header, not a text dest stub."""
    raw = bytes(head or b"")
    if len(raw) < 12:
        return False
    if raw.startswith(_PNG_MAGIC) or raw.startswith(_JPEG_MAGIC) or raw.startswith(b"GIF8"):
        return True
    return raw.startswith(b"RIFF") and b"WEBP" in raw[:12]


def raster_file_ok(path: Path) -> bool:
    """True when path is a real raster. Dest stubs are tiny text with an image suffix."""
    try:
        with path.open("rb") as fh:
            return raster_head_ok(fh.read(16))
    except OSError:
        return False


def list_files(username: str, chat_id: str) -> list[dict[str, Any]]:
    root = workspace_root(username, chat_id, create=False)
    rows: list[dict[str, Any]] = []
    for path in _iter_files(root):
        rel = path.relative_to(root.resolve()).as_posix()
        suffix = path.suffix.lower()
        image = suffix in IMAGE_SUFFIXES
        info = path.stat()
        rows.append(
            {
                "path": rel,
                "size": info.st_size,
                "mtime": info.st_mtime_ns,
                "kind": "image" if image else "text",
                "raster": raster_file_ok(path) if image else False,
                "editable": suffix in TEXT_SUFFIXES,
                "page": suffix in PAGE_SUFFIXES,
            }
        )
    return rows


def site_entry(username: str, chat_id: str, wanted: str = "") -> str:
    """Pick the page a preview link should land on. Empty when there is none."""
    pages = [row["path"] for row in list_files(username, chat_id) if row["page"]]
    if not pages:
        return ""
    ask = str(wanted or "").strip().lstrip("/")
    if ask in pages:
        return ask
    if "index.html" in pages:
        return "index.html"
    return sorted(pages, key=lambda page: (page.count("/"), page))[0]


_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_CSS_URL = re.compile(r"""(?i)url\(\s*['"]?([^'")\s]+)['"]?\s*\)""")
_JS_CANVAS_WIDTH = re.compile(r"(?i)\.width\s*=\s*([^;\n]{1,80})")
_JS_CANVAS_HEIGHT = re.compile(r"(?i)\.height\s*=\s*([^;\n]{1,80})")


def _el_label(tag: str, el_id: str, classes: list[str]) -> str:
    label = tag or "element"
    if el_id:
        label += f"#{el_id}"
    for name in classes:
        label += f".{name}"
    return label


def _is_hero(el_id: str, classes: list[str]) -> bool:
    if (el_id or "").lower() == "hero":
        return True
    return any(name.lower() == "hero" for name in classes)


class _LayoutWalker(HTMLParser):
    """Collect hero nodes, canvases, assets, and linked CSS/JS."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.heroes: list[dict[str, Any]] = []
        self.canvases: list[dict[str, Any]] = []
        self.imgs: list[str] = []
        self.css_hrefs: list[str] = []
        self.js_hrefs: list[str] = []
        self.style_blocks: list[str] = []
        self.script_blocks: list[str] = []
        self._capture = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        ad = {str(key).lower(): (val or "") for key, val in attrs}
        classes = [name for name in ad.get("class", "").split() if name]
        el_id = ad.get("id") or ""
        depth = len(self.stack)
        rel = (ad.get("rel") or "").lower()
        if tag == "link" and "stylesheet" in rel and ad.get("href"):
            self.css_hrefs.append(ad["href"].strip())
        if tag == "script" and ad.get("src"):
            self.js_hrefs.append(ad["src"].strip())
        if tag == "img" and ad.get("src"):
            self.imgs.append(ad["src"].strip())
        inline = ad.get("style") or ""
        if _is_hero(el_id, classes):
            self.heroes.append(
                {
                    "tag": tag,
                    "id": el_id,
                    "classes": classes,
                    "depth": depth,
                    "children": [],
                    "style": inline,
                    "open": True,
                }
            )
        for hero in self.heroes:
            if hero.get("open") and hero["depth"] + 1 == depth:
                hero["children"].append(_el_label(tag, el_id, classes))
        if tag == "canvas":
            self.canvases.append(
                {
                    "tag": tag,
                    "id": el_id,
                    "classes": classes,
                    "depth": depth,
                    "in_hero": any(
                        hero.get("open") and hero["depth"] < depth
                        for hero in self.heroes
                    ),
                    "style": inline,
                }
            )
        if tag in ("style", "script") and not ad.get("src"):
            self._capture = tag
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._capture == tag:
            self._capture = ""
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index] == tag:
                del self.stack[index:]
                break
        for hero in self.heroes:
            if hero.get("open") and hero["depth"] >= len(self.stack):
                hero["open"] = False

    def handle_data(self, data: str) -> None:
        if self._capture == "style":
            self.style_blocks.append(data)
        elif self._capture == "script":
            self.script_blocks.append(data)


def _try_read_text(username: str, chat_id: str, rel: str) -> str:
    try:
        text = read_text(username, chat_id, rel)
    except (OSError, ValueError, FileNotFoundError):
        return ""
    if str(text).startswith("[binary "):
        return ""
    return text


def _workspace_file_exists(username: str, chat_id: str, rel: str) -> bool:
    try:
        root = workspace_root(username, chat_id, create=False)
        return resolve_rel(root, rel).is_file()
    except (OSError, ValueError):
        return False


def _local_ref(base_file: str, ref: str) -> str:
    raw = str(ref or "").strip().split("?", 1)[0].split("#", 1)[0]
    if not raw or raw.startswith(("http://", "https://", "data:", "/", "//")):
        return ""
    try:
        path = Path(base_file).parent / raw
        parts: list[str] = []
        for part in path.parts:
            if part == "..":
                if parts:
                    parts.pop()
            elif part not in ("", "."):
                parts.append(part)
        return "/".join(parts)
    except (OSError, ValueError):
        return ""


def _parse_decls(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in str(body or "").split(";"):
        if ":" not in part:
            continue
        prop, val = part.split(":", 1)
        key = prop.strip().lower()
        value = re.sub(r"\s*!important\s*$", "", val.strip(), flags=re.I)
        if key:
            out[key] = value
    return out


def _iter_css_rules(css: str) -> list[tuple[str, str]]:
    text = re.sub(r"/\*.*?\*/", "", css or "", flags=re.S)
    rules: list[tuple[str, str]] = []
    index = 0
    length = len(text)
    while index < length:
        start = text.find("{", index)
        if start < 0:
            break
        selector = text[index:start].strip()
        depth = 1
        cursor = start + 1
        while cursor < length and depth:
            if text[cursor] == "{":
                depth += 1
            elif text[cursor] == "}":
                depth -= 1
            cursor += 1
        body = text[start + 1 : cursor - 1]
        if "{" in body:
            rules.extend(_iter_css_rules(body))
        elif selector and not selector.lstrip().startswith("@"):
            rules.append((selector, body))
        index = cursor
    return rules


def _selector_matches(selector: str, tag: str, el_id: str, classes: list[str]) -> bool:
    compound = re.split(r"[\s>+~]+", (selector or "").strip())[-1]
    compound = re.sub(r"::?[\w-]+", "", compound)
    compound = re.sub(r"\[[^\]]*\]", "", compound).strip()
    if not compound or compound == "*":
        return False
    rest = compound
    tag_m = ""
    match = re.match(r"(?i)^([a-z][a-z0-9-]*)", rest)
    if match:
        tag_m = match.group(1).lower()
        rest = rest[match.end() :]
    id_m = ""
    class_m: list[str] = []
    while rest:
        if rest.startswith("#"):
            match = re.match(r"#([\w-]+)", rest)
            if not match:
                return False
            id_m = match.group(1)
            rest = rest[match.end() :]
            continue
        if rest.startswith("."):
            match = re.match(r"\.([\w-]+)", rest)
            if not match:
                return False
            class_m.append(match.group(1).lower())
            rest = rest[match.end() :]
            continue
        return False
    if tag_m and tag_m != (tag or "").lower():
        return False
    if id_m and id_m != el_id:
        return False
    have = {name.lower() for name in classes}
    if any(name not in have for name in class_m):
        return False
    return bool(tag_m or id_m or class_m)


def _rules_for(
    rules: list[tuple[str, str]], tag: str, el_id: str, classes: list[str]
) -> dict[str, str]:
    decls: dict[str, str] = {}
    for selector, body in rules:
        for piece in selector.split(","):
            if _selector_matches(piece, tag, el_id, classes):
                decls.update(_parse_decls(body))
                break
    return decls


def _merged_decls(rules: dict[str, str], inline: str) -> dict[str, str]:
    merged = dict(rules)
    merged.update(_parse_decls(inline))
    return merged


def _css_position(merged: dict[str, str]) -> str:
    pos = (merged.get("position") or "").strip()
    return f"position:{pos}" if pos else "no CSS rule"


def _js_canvas_notes(js: str) -> list[str]:
    notes: list[str] = []
    for raw in _JS_CANVAS_WIDTH.findall(js or ""):
        value = str(raw).strip()
        if "innerWidth" in value:
            notes.append("JS sets canvas.width = window.innerWidth")
        elif re.search(r"(?:clientWidth|offsetWidth)", value):
            notes.append("JS sets canvas.width from parent")
    for raw in _JS_CANVAS_HEIGHT.findall(js or ""):
        value = str(raw).strip()
        if "innerHeight" in value:
            notes.append("JS sets canvas.height = window.innerHeight")
    return list(dict.fromkeys(notes))


def _image_ref_notes(
    username: str,
    chat_id: str,
    refs: list[tuple[str, str]],
) -> list[str]:
    disk: list[str] = []
    for row in list_files(username, chat_id):
        rel = str(row.get("path") or "")
        if is_image_path(rel):
            disk.append(rel)
    on_disk = set(disk)
    notes: list[str] = []
    seen: set[str] = set()
    matched = 0
    for base, ref in refs:
        rel = _local_ref(base, ref)
        if not rel or rel in seen or not is_image_path(rel):
            continue
        seen.add(rel)
        if rel in on_disk:
            matched += 1
            continue
        parent = Path(rel).parent.as_posix()
        stem = Path(rel).stem
        alts = [
            path
            for path in disk
            if Path(path).stem == stem
            and Path(path).parent.as_posix() == parent
        ]
        if alts:
            notes.append(
                f"{rel} missing, {Path(alts[0]).name} exists — leave src"
            )
        else:
            notes.append(f"{rel} missing — leave src")
    if not seen:
        return []
    if not notes:
        return ["Image src paths match files on disk."]
    if matched:
        notes.append(f"{matched} other image src path(s) match disk.")
    return notes


def layout_report(username: str, chat_id: str) -> str:
    """Static hero/canvas/image facts for a layout-fix turn. No browser."""
    if not username or not chat_id:
        return "No HTML page in this workspace."
    try:
        entry = site_entry(username, chat_id)
    except (OSError, ValueError):
        return "No HTML page in this workspace."
    if not entry:
        return "No HTML page in this workspace."
    html = _try_read_text(username, chat_id, entry)
    if not html:
        return f"Could not read {entry}."
    walker = _LayoutWalker()
    try:
        walker.feed(html)
        walker.close()
    except Exception:
        return f"Could not parse {entry}."

    css_rels = [
        rel
        for rel in (_local_ref(entry, href) for href in walker.css_hrefs)
        if rel
    ]
    if _workspace_file_exists(username, chat_id, "styles.css") and "styles.css" not in css_rels:
        css_rels.append("styles.css")
    js_rels = [
        rel
        for rel in (_local_ref(entry, href) for href in walker.js_hrefs)
        if rel
    ]
    if _workspace_file_exists(username, chat_id, "script.js") and "script.js" not in js_rels:
        js_rels.append("script.js")

    css_chunks = list(walker.style_blocks)
    css_urls: list[tuple[str, str]] = []
    for rel in css_rels:
        text = _try_read_text(username, chat_id, rel)
        if text:
            css_chunks.append(text)
            for url in _CSS_URL.findall(text):
                css_urls.append((rel, url))
    rules = []
    for chunk in css_chunks:
        rules.extend(_iter_css_rules(chunk))
    js_text = "\n".join(walker.script_blocks)
    for rel in js_rels:
        text = _try_read_text(username, chat_id, rel)
        if text:
            js_text += "\n" + text

    lines = [f"Page: {entry}"]
    if css_rels:
        lines.append("CSS: " + ", ".join(css_rels))
    if js_rels:
        lines.append("JS: " + ", ".join(js_rels))

    hero_flex = False
    if not walker.heroes:
        lines.append("No .hero / header.hero element.")
    for hero in walker.heroes[:4]:
        label = _el_label(hero["tag"], hero["id"], hero["classes"])
        merged = _merged_decls(
            _rules_for(rules, hero["tag"], hero["id"], hero["classes"]),
            hero["style"],
        )
        display = (merged.get("display") or "").strip() or "no CSS rule"
        line = f"{label} display:{display}"
        direction = (merged.get("flex-direction") or "").strip()
        if direction:
            line += f" flex-direction:{direction}"
        kids = hero["children"] or ["(no children)"]
        line += "; children: " + ", ".join(kids[:12])
        lines.append(line)
        if display.split()[0] in ("flex", "grid"):
            hero_flex = True

    js_notes = _js_canvas_notes(js_text)
    flagged = False
    for canvas in walker.canvases[:6]:
        label = _el_label(canvas["tag"], canvas["id"], canvas["classes"])
        merged = _merged_decls(
            _rules_for(rules, canvas["tag"], canvas["id"], canvas["classes"]),
            canvas["style"],
        )
        pos = _css_position(merged)
        where = " inside hero" if canvas["in_hero"] else ""
        extra = f"; {js_notes[0]}" if js_notes else ""
        lines.append(f"{label}{where}: {pos}{extra}")
        in_flow = (merged.get("position") or "").strip() not in (
            "absolute",
            "fixed",
            "sticky",
        )
        if hero_flex and canvas["in_hero"] and in_flow and not flagged:
            sel = f"#{canvas['id']}" if canvas.get("id") else "canvas"
            lines.append(
                "Flag: this canvas takes layout space and can shove hero text. "
                f"StrReplace styles.css to add {sel} "
                "{ position: absolute; inset: 0; pointer-events: none; }"
            )
            flagged = True
    if walker.canvases and js_notes[1:]:
        lines.extend(js_notes[1:])

    refs = [(entry, src) for src in walker.imgs] + css_urls
    img_notes = _image_ref_notes(username, chat_id, refs)
    missing = [note for note in img_notes if "missing" in note]
    if missing:
        lines.append(
            "Image srcs use extensions that are not on disk (files are .webp). "
            "Do not change src on this layout turn."
        )
    elif img_notes:
        lines.extend(img_notes)

    text = "\n".join(lines)
    if len(text) > LAYOUT_REPORT_MAX:
        text = text[: LAYOUT_REPORT_MAX - 1].rstrip() + "…"
    return text


def listing(username: str, chat_id: str) -> dict[str, Any]:
    files = list_files(username, chat_id)
    root = workspace_root(username, chat_id, create=False)
    dir_rows: list[dict[str, Any]] = []
    for path in _iter_dirs(root):
        try:
            rel = path.relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            continue
        dir_rows.append(
            {
                "path": rel,
                "size": 0,
                "kind": "dir",
                "editable": False,
                "page": False,
            }
        )
    return {
        "files": files + dir_rows,
        "deleted": missing_history_rows(username, chat_id),
        "bytes": sum(int(row["size"]) for row in files),
        "count": len(files),
    }


def _path_mentioned(text: str, rel: str) -> bool:
    """True when a project file still names this path or a sibling image ext."""
    body = str(text or "")
    if not body:
        return False
    path = str(rel or "").replace("\\", "/")
    if path and path in body:
        return True
    name = Path(path).name
    if name and name in body:
        return True
    if not is_image_path(path):
        return False
    stem = Path(path).stem
    parent = Path(path).parent.as_posix()
    for ext in IMAGE_SUFFIXES:
        alt = f"{stem}{ext}"
        if alt in body:
            return True
        if parent and parent != "." and f"{parent}/{alt}" in body:
            return True
    return False


def referenced_project_paths(username: str, chat_id: str) -> set[str]:
    """Existing files named by HTML/CSS/JS (image stems count across .png/.webp)."""
    rows = list_files(username, chat_id)
    paths = [str(row["path"]) for row in rows if row.get("path")]
    chunks: list[str] = []
    for row in rows:
        if not row.get("editable"):
            continue
        try:
            chunks.append(read_text(username, chat_id, str(row["path"])))
        except (OSError, FileNotFoundError, ValueError):
            continue
    blob = "\n".join(chunks)
    return {rel for rel in paths if _path_mentioned(blob, rel)}


def page_references_dests(username: str, chat_id: str, dests: list[str]) -> bool:
    """True when every dest is already named in HTML/CSS/JS."""
    wanted = [str(dest or "").strip() for dest in dests or []]
    wanted = [dest for dest in wanted if dest]
    if not wanted or not username or not chat_id:
        return False
    blob = _project_blob(username, chat_id)
    if not blob.strip():
        return False
    return all(_path_mentioned(blob, dest) for dest in wanted)


def unreferenced_dests(username: str, chat_id: str, dests: list[str]) -> list[str]:
    """Planned dests that HTML/CSS/JS do not name yet."""
    wanted = [str(dest or "").strip() for dest in dests or []]
    wanted = [dest for dest in wanted if dest]
    if not wanted or not username or not chat_id:
        return wanted
    blob = _project_blob(username, chat_id)
    if not blob.strip():
        return wanted
    return [dest for dest in wanted if not _path_mentioned(blob, dest)]


_HERO_ALIAS_STEMS = frozenset(
    {"header", "hero", "banner", "hero-background", "hero_background"}
)


def _preview_raster_ok(path: Path) -> bool:
    """Skip dest stubs so preview can fall through to the GPU file."""
    try:
        return path.is_file() and raster_file_ok(path)
    except OSError:
        return False


def resolve_image_rel(username: str, chat_id: str, rel: str) -> str:
    """Resolve an image path, preferring a same-stem sibling when the ext is stale."""
    root = workspace_root(username, chat_id, create=False)
    path = resolve_rel(root, rel)
    if _preview_raster_ok(path) and is_image_path(rel):
        return path.relative_to(root.resolve()).as_posix()
    stem = path.stem
    stems = [stem]
    if stem.lower().replace("_", "-") in _HERO_ALIAS_STEMS:
        stems.extend(
            alias
            for alias in ("hero", "header", "banner")
            if alias != stem.lower()
        )
    found: list[str] = []
    parent = path.parent
    if parent.is_dir():
        for alias in stems:
            for ext in sorted(IMAGE_SUFFIXES):
                candidate = parent / f"{alias}{ext}"
                if _preview_raster_ok(candidate):
                    found.append(candidate.relative_to(root.resolve()).as_posix())
            if found:
                break
    if not found:
        wanted = {item.lower().replace("_", "-") for item in stems}
        found = [
            str(row["path"])
            for row in list_files(username, chat_id)
            if is_image_path(str(row["path"]))
            and Path(str(row["path"])).stem.lower().replace("_", "-") in wanted
            and row.get("raster")
        ]
    if found:
        return found[0]
    raise FileNotFoundError(rel)


def missing_history_rows(username: str, chat_id: str) -> list[dict[str, Any]]:
    """Text files that still have History after they were deleted from the tree."""
    existing = {str(row["path"]) for row in list_files(username, chat_id)}
    folder = history_dir(username, chat_id)
    with _HISTORY_LOCK:
        index = _load_history_index(folder)
    rows: list[dict[str, Any]] = []
    for key, versions in index.items():
        rel = str(key or "").strip().replace("\\", "/")
        if not rel or rel in existing or not versions:
            continue
        content = [
            row
            for row in versions
            if isinstance(row, dict) and not row.get("created")
        ]
        if not content:
            continue
        latest = content[0]
        suffix = Path(rel).suffix.lower()
        rows.append(
            {
                "path": rel,
                "size": int(latest.get("bytes") or 0),
                "kind": "text",
                "editable": suffix in TEXT_SUFFIXES,
                "page": suffix in PAGE_SUFFIXES,
                "missing": True,
                "rev": str(latest.get("id") or ""),
            }
        )
    rows.sort(key=lambda row: str(row["path"]))
    return rows


def delete_empty_dir(username: str, chat_id: str, rel: str) -> str:
    """Remove one empty folder. Refuses if it still contains files or folders."""
    text = _folder_prefix(rel)
    root = workspace_root(username, chat_id, create=False)
    path = resolve_rel(root, text)
    if not path.is_dir():
        raise FileNotFoundError(text)
    try:
        next(path.iterdir())
    except StopIteration:
        path.rmdir()
        _prune_empty_parents(root, path.parent)
        return path.relative_to(root.resolve()).as_posix()
    raise ValueError(f"{text} is not empty.")


def history_dir(username: str, chat_id: str) -> Path:
    return user_dir(username) / f"{safe_name(chat_id)}{HISTORY_SUFFIX}"


def drop_history(username: str, chat_id: str) -> None:
    folder = history_dir(username, chat_id)
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


def _file_key(username: str, chat_id: str, rel: str) -> str:
    root = workspace_root(username, chat_id, create=False)
    path = resolve_rel(root, rel)
    return path.relative_to(root.resolve()).as_posix()


def _history_index_path(folder: Path) -> Path:
    return folder / "index.json"


def _history_blob(folder: Path, rev_id: str) -> Path:
    return folder / "blobs" / rev_id


def _load_history_index(folder: Path) -> dict[str, list[dict[str, Any]]]:
    try:
        raw = json.loads(_history_index_path(folder).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for key, rows in raw.items():
        if not isinstance(key, str) or not isinstance(rows, list):
            continue
        clean: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            rev_id = str(row.get("id") or "").strip()
            if not rev_id or any(part in rev_id for part in ("/", "\\", "..")):
                continue
            try:
                ts = int(row.get("ts") or 0)
                nbytes = int(row.get("bytes") or 0)
            except (TypeError, ValueError):
                continue
            item = {"id": rev_id, "ts": ts, "bytes": max(0, nbytes)}
            run_id = _clean_history_run(row.get("run"))
            if run_id:
                item["run"] = run_id
            if row.get("created"):
                item["created"] = True
            clean.append(item)
        if clean:
            out[key] = clean
    return out


def _save_history_index(folder: Path, index: dict[str, list[dict[str, Any]]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    dest = _history_index_path(folder)
    tmp = dest.with_name("index.json.tmp")
    tmp.write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(dest)
    os.chmod(dest, 0o600)


def _record_history(
    username: str,
    chat_id: str,
    rel: str,
    data: bytes,
    *,
    created: bool = False,
) -> None:
    if is_image_path(rel):
        return
    try:
        if not created:
            data.decode("utf-8")
        key = _file_key(username, chat_id, rel)
    except (UnicodeDecodeError, ValueError):
        return
    folder = history_dir(username, chat_id)
    run_id = _active_history_run()
    with _HISTORY_LOCK:
        index = _load_history_index(folder)
        rows = list(index.get(key) or [])
        if rows:
            if not created:
                last = _history_blob(folder, str(rows[0].get("id") or ""))
                try:
                    if last.is_file() and last.read_bytes() == data:
                        return
                except OSError:
                    pass
            # One snapshot per file for a prompt run: keep the pre-run bytes.
            if run_id and str(rows[0].get("run") or "") == run_id:
                return
        rev_id = secrets.token_hex(8)
        row = {
            "id": rev_id,
            "ts": int(time.time() * 1000),
            "bytes": 0 if created else len(data),
        }
        if created:
            row["created"] = True
        else:
            blob = _history_blob(folder, rev_id)
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(data)
            os.chmod(blob, 0o600)
        if run_id:
            row["run"] = run_id
        rows.insert(0, row)
        extra = rows[MAX_HISTORY_VERSIONS:]
        rows = rows[:MAX_HISTORY_VERSIONS]
        for old in extra:
            try:
                _history_blob(folder, str(old.get("id") or "")).unlink()
            except OSError:
                pass
        index[key] = rows
        _save_history_index(folder, index)


def _move_history(username: str, chat_id: str, src: str, dest: str) -> None:
    try:
        src_key = _file_key(username, chat_id, src)
        dest_key = _file_key(username, chat_id, dest)
    except ValueError:
        return
    if src_key == dest_key:
        return
    folder = history_dir(username, chat_id)
    with _HISTORY_LOCK:
        index = _load_history_index(folder)
        rows = index.pop(src_key, None)
        if not rows:
            return
        index[dest_key] = rows
        _save_history_index(folder, index)


def list_history(username: str, chat_id: str, rel: str) -> list[dict[str, Any]]:
    key = _file_key(username, chat_id, rel)
    folder = history_dir(username, chat_id)
    with _HISTORY_LOCK:
        rows = list(_load_history_index(folder).get(key) or [])
    return [row for row in rows if not row.get("created")]


def _line_diff(latest: str, revision: str) -> list[dict[str, str]]:
    old_lines = latest.splitlines()
    new_lines = revision.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    out: list[dict[str, str]] = []
    for tag, i1, j1, i2, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend({"kind": "eq", "text": line} for line in old_lines[i1:j1])
        elif tag == "delete":
            out.extend({"kind": "del", "text": line} for line in old_lines[i1:j1])
        elif tag == "insert":
            out.extend({"kind": "add", "text": line} for line in new_lines[i2:j2])
        else:
            out.extend({"kind": "del", "text": line} for line in old_lines[i1:j1])
            out.extend({"kind": "add", "text": line} for line in new_lines[i2:j2])
    return out


def history_revision(
    username: str, chat_id: str, rel: str, rev_id: str
) -> dict[str, Any]:
    key = _file_key(username, chat_id, rel)
    wanted = str(rev_id or "").strip()
    if not wanted or any(part in wanted for part in ("/", "\\", "..")):
        raise ValueError("Invalid revision")
    folder = history_dir(username, chat_id)
    with _HISTORY_LOCK:
        rows = _load_history_index(folder).get(key) or []
        meta = next((row for row in rows if row.get("id") == wanted), None)
        if not meta:
            raise FileNotFoundError(wanted)
        blob = _history_blob(folder, wanted)
        try:
            contents = blob.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise FileNotFoundError(wanted) from exc
        except UnicodeDecodeError as exc:
            raise ValueError("Revision is not UTF-8 text.") from exc
    latest = ""
    try:
        latest = read_text(username, chat_id, rel)
    except FileNotFoundError:
        latest = ""
    return {
        "path": key,
        "id": wanted,
        "ts": int(meta.get("ts") or 0),
        "bytes": int(meta.get("bytes") or len(contents.encode("utf-8"))),
        "contents": contents,
        "latest": latest,
        "diff": _line_diff(latest, contents),
    }


def restore_revision(
    username: str,
    chat_id: str,
    rel: str,
    rev_id: str,
    *,
    record_history: bool = True,
) -> str:
    if is_image_path(rel):
        raise ValueError("Only text files can be restored.")
    data = history_revision(username, chat_id, rel, rev_id)
    return write_text(
        username, chat_id, rel, data["contents"], record_history=record_history
    )


def _history_row_ts(row: dict[str, Any]) -> int:
    try:
        return int(row.get("ts") or 0)
    except (TypeError, ValueError):
        return 0


def _history_row_matches(
    row: dict[str, Any], wanted: set[str], since_ts: int
) -> bool:
    run_id = str(row.get("run") or "")
    if wanted and run_id in wanted:
        return True
    return bool(since_ts and _history_row_ts(row) >= since_ts)


def restore_run(
    username: str,
    chat_id: str,
    run_id: str = "",
    created: Optional[list[str]] = None,
    run_ids: Optional[list[str]] = None,
    since_ts: int = 0,
) -> dict[str, list[str]]:
    """Undo Code-mode prompt runs: restore the oldest matching snapshot, delete created files."""
    wanted: set[str] = set()
    for raw in list(run_ids or []) + [run_id]:
        clean = _clean_history_run(raw)
        if clean:
            wanted.add(clean)
    try:
        since = int(since_ts or 0)
    except (TypeError, ValueError):
        since = 0
    if since < 0:
        since = 0
    if not wanted and not since:
        raise ValueError("Invalid run")
    folder = history_dir(username, chat_id)
    targets: list[tuple[str, dict[str, Any]]] = []
    with _HISTORY_LOCK:
        index = _load_history_index(folder)
        for key, rows in index.items():
            if not isinstance(rows, list):
                continue
            matched = [
                row
                for row in rows
                if isinstance(row, dict) and _history_row_matches(row, wanted, since)
            ]
            if not matched:
                continue
            oldest = min(matched, key=_history_row_ts)
            targets.append((key, oldest))
    created_set: set[str] = set()
    for raw in created or []:
        rel = str(raw or "").strip().replace("\\", "/")
        if rel:
            created_set.add(rel)
    restored: list[str] = []
    deleted: list[str] = []
    for rel, row in targets:
        drop = bool(row.get("created")) or rel in created_set
        try:
            if drop:
                delete_file(username, chat_id, rel, record_history=False)
                deleted.append(rel)
            else:
                written = restore_revision(
                    username, chat_id, rel, str(row.get("id") or ""), record_history=False
                )
                restored.append(written or rel)
        except (ValueError, FileNotFoundError, OSError):
            continue
    handled = set(restored) | set(deleted)
    for rel in created_set:
        if rel in handled:
            continue
        try:
            delete_file(username, chat_id, rel, record_history=False)
            deleted.append(rel)
        except (ValueError, FileNotFoundError, OSError):
            continue
    return {"restored": restored, "deleted": deleted}


def workspace_instructions(username: str, chat_id: str) -> str:
    """Capped AGENTS.md / .tabby.md excerpt for the Code-mode system prompt."""
    for name in INSTRUCTIONS_NAMES:
        if not _workspace_file_exists(username, chat_id, name):
            continue
        try:
            text = read_text(username, chat_id, name)
        except (OSError, ValueError, FileNotFoundError):
            continue
        if not text or text.startswith("[binary"):
            continue
        body = text.strip()
        if not body:
            continue
        if len(body) > INSTRUCTIONS_MAX:
            body = body[: INSTRUCTIONS_MAX - 1].rstrip() + "…"
        return body
    return ""


def _assert_public_clone_host(host: str) -> None:
    text = str(host or "").strip().lower().rstrip(".")
    if not text or text in {"localhost", "localhost.localdomain"}:
        raise ValueError("Clone URL host is not allowed.")
    try:
        parsed_ip = ipaddress.ip_address(text)
    except ValueError:
        parsed_ip = None
    if parsed_ip is not None and not parsed_ip.is_global:
        raise ValueError("Clone URL host is not allowed.")
    try:
        infos = socket.getaddrinfo(text, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("Clone URL host could not be resolved.") from exc
    if not infos:
        raise ValueError("Clone URL host could not be resolved.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("Clone URL host is not allowed.")


def _https_clone_url(raw: str) -> tuple[str, str]:
    parsed = urlparse(str(raw or "").strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Clone URL must be https.")
    if parsed.username or parsed.password:
        raise ValueError("Clone URL must not include credentials.")
    host = str(parsed.hostname or "").strip().lower()
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Clone URL host is not allowed.")
    _assert_public_clone_host(host)
    path = parsed.path or ""
    clean = urlunparse(("https", parsed.netloc, path, "", "", ""))
    name = Path(path.rstrip("/")).name or "repo"
    if name.endswith(".git"):
        name = name[:-4]
    dest = safe_name(name) or "repo"
    return clean, dest


def clone_git(username: str, chat_id: str, url: str) -> dict[str, str]:
    """Clone an HTTPS git repo into this workspace via the jail container."""
    clean, dest = _https_clone_url(url)
    files = [
        row
        for row in list_files(username, chat_id)
        if row.get("path") and row.get("kind") != "dir"
    ]
    empty = not files
    target = "." if empty else dest
    if not empty:
        root = workspace_root(username, chat_id, create=True)
        try:
            dest_path = resolve_rel(root, dest)
        except ValueError as exc:
            raise ValueError("Invalid repository name.") from exc
        if dest_path.exists():
            raise ValueError(f"{dest} already exists.")
    from ui import codebox

    cmd = (
        f"git clone --depth {CLONE_DEPTH} -- "
        + shlex.quote(clean)
        + " "
        + shlex.quote(target)
    )
    try:
        code, output = codebox.run_shell(
            username, chat_id, cmd, timeout=CLONE_TIMEOUT_S, network=True
        )
    except codebox.CodeboxError as exc:
        raise ValueError(str(exc)) from exc
    if code:
        detail = (output or "git clone failed").strip()
        raise ValueError(detail[:1500] or "git clone failed")
    _check_caps(workspace_root(username, chat_id, create=False))
    return {"path": "" if target == "." else dest, "url": clean}


def resolve_file(username: str, chat_id: str, rel: str) -> Path:
    root = workspace_root(username, chat_id, create=False)
    path = resolve_rel(root, rel)
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(rel)
    return path


def resolve_preview_file(username: str, chat_id: str, rel: str) -> Path:
    """Site-preview path. A .png URL may land on the same-stem .webp (or reverse)."""
    try:
        return resolve_file(username, chat_id, rel)
    except FileNotFoundError:
        if not is_image_path(rel):
            raise
        return resolve_file(username, chat_id, resolve_image_rel(username, chat_id, rel))


def read_bytes(username: str, chat_id: str, rel: str) -> tuple[Path, bytes]:
    path = resolve_file(username, chat_id, rel)
    return path, path.read_bytes()


def read_text(username: str, chat_id: str, rel: str) -> str:
    _path, data = read_bytes(username, chat_id, rel)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return f"[binary {len(data)} bytes]"


def write_text(
    username: str,
    chat_id: str,
    rel: str,
    contents: str,
    *,
    sync_images: bool = True,
    record_history: bool = True,
) -> str:
    text = contents if isinstance(contents, str) else str(contents or "")
    data = text.encode("utf-8")
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError(f"File is larger than {MAX_TEXT_BYTES // (1024 * 1024)} MB.")
    root = workspace_root(username, chat_id, create=True)
    path = resolve_rel(root, rel)
    extra_files = 0 if path.is_file() else 1
    extra_bytes = len(data) - (path.stat().st_size if path.is_file() else 0)
    _check_caps(root, extra_bytes=max(0, extra_bytes), extra_files=extra_files)
    if record_history:
        if path.is_file():
            try:
                previous = path.read_bytes()
            except OSError:
                previous = None
            if previous is not None and previous != data:
                _record_history(username, chat_id, rel, previous)
        else:
            _record_history(username, chat_id, rel, b"", created=True)
    _write_bytes_nofollow(root, rel, data)
    written = path.relative_to(root.resolve()).as_posix()
    if sync_images and Path(written).suffix.lower() in _PAGE_SUFFIXES:
        _resync_existing_image_refs(username, chat_id)
    return written


def read_text_window(
    username: str,
    chat_id: str,
    rel: str,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
) -> str:
    text = read_text(username, chat_id, rel)
    if offset is None and limit is None:
        return text
    lines = text.splitlines()
    start = max(int(offset or 1), 1)
    span = len(lines) if limit is None else max(int(limit), 0)
    chunk = lines[start - 1 : start - 1 + span]
    if not chunk:
        return f"# {rel} has {len(lines)} lines; offset {start} is past the end."
    end = start + len(chunk) - 1
    numbered = [f"{start + index}|{line}" for index, line in enumerate(chunk)]
    return f"# {rel} lines {start}-{end} of {len(lines)}\n" + "\n".join(numbered)


def _glob_match(rel: str, pattern: str) -> bool:
    pat = (pattern or "").strip()
    if not pat:
        return True
    name = Path(rel).name
    if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
        return True
    if "**" in pat:
        tail = pat.split("**", 1)[-1].lstrip("/")
        if tail and (fnmatch.fnmatch(rel, tail) or fnmatch.fnmatch(name, tail)):
            return True
    return False


def grep_hits(
    username: str,
    chat_id: str,
    pattern: str,
    path: str = "",
    glob_pat: str = "",
    max_matches: int = MAX_GREP_MATCHES,
    literal: bool = False,
) -> list[dict[str, Any]]:
    try:
        regex = re.compile(re.escape(pattern) if literal else pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regex: {exc}") from exc
    prefix = str(path or "").strip().strip("/")
    hits: list[dict[str, Any]] = []
    for row in list_files(username, chat_id):
        rel = str(row.get("path") or "")
        if not rel or row.get("kind") == "image" or not row.get("editable"):
            continue
        if prefix and rel != prefix and not rel.startswith(prefix + "/"):
            continue
        if glob_pat and not _glob_match(rel, glob_pat):
            continue
        try:
            text = read_text(username, chat_id, rel)
        except (ValueError, FileNotFoundError, OSError):
            continue
        if text.startswith("[binary"):
            continue
        for index, line in enumerate(text.splitlines(), 1):
            if not regex.search(line):
                continue
            snippet = line if len(line) <= MAX_GREP_LINE else line[:MAX_GREP_LINE] + "…"
            hits.append({"path": rel, "line": index, "text": snippet})
            if len(hits) >= max_matches:
                return hits
    return hits


def grep_text(
    username: str,
    chat_id: str,
    pattern: str,
    path: str = "",
    glob_pat: str = "",
    max_matches: int = MAX_GREP_MATCHES,
) -> str:
    hits = grep_hits(
        username,
        chat_id,
        pattern,
        path=path,
        glob_pat=glob_pat,
        max_matches=max_matches,
    )
    if not hits:
        return "No matches."
    lines = [f"{row['path']}:{row['line']}:{row['text']}" for row in hits]
    if len(hits) >= max_matches:
        lines.append(f"…stopped after {max_matches} matches")
    return "\n".join(lines)


def replace_all_text(
    username: str, chat_id: str, rel: str, old: str, new: str
) -> int:
    """Replace every occurrence of old in one text file. Returns the count."""
    if not str(old):
        raise ValueError("Find text is required.")
    text = read_text(username, chat_id, rel)
    if text.startswith("[binary"):
        raise ValueError(f"{rel} is not text.")
    count = text.count(old)
    if not count:
        return 0
    write_text(username, chat_id, rel, text.replace(old, new))
    return count


def glob_paths(username: str, chat_id: str, pattern: str) -> str:
    pat = str(pattern or "").strip() or "**/*"
    matches = [
        str(row.get("path") or "")
        for row in list_files(username, chat_id)
        if row.get("path") and _glob_match(str(row.get("path") or ""), pat)
    ]
    if not matches:
        return "No files matched."
    return "\n".join(sorted(matches))


def str_replace(username: str, chat_id: str, rel: str, old: str, new: str) -> str:
    text = read_text(username, chat_id, rel)
    if old not in text:
        raise ValueError(f"{rel}: old_string was not found.")
    count = text.count(old)
    if count != 1:
        raise ValueError(f"{rel}: old_string must match exactly once ({count} found).")
    write_text(username, chat_id, rel, text.replace(old, new, 1))
    return rel


def delete_file(
    username: str, chat_id: str, rel: str, *, record_history: bool = True
) -> None:
    root = workspace_root(username, chat_id, create=False)
    path = resolve_rel(root, rel)
    if not path.is_file():
        raise FileNotFoundError(rel)
    if record_history and not is_image_path(rel):
        try:
            _record_history(username, chat_id, rel, path.read_bytes())
        except OSError:
            pass
    path.unlink()
    _prune_empty_parents(root, path.parent)


def rename_file(username: str, chat_id: str, src: str, dest: str) -> str:
    root = workspace_root(username, chat_id, create=False)
    src_path = resolve_rel(root, src)
    dest_path = resolve_rel(root, dest)
    if not src_path.is_file():
        raise FileNotFoundError(src)
    if dest_path == src_path:
        return src_path.relative_to(root.resolve()).as_posix()
    if dest_path.exists():
        raise ValueError(f"{dest} already exists.")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    src_path.rename(dest_path)
    _prune_empty_parents(root, src_path.parent)
    written = dest_path.relative_to(root.resolve()).as_posix()
    _move_history(username, chat_id, src, dest)
    return written


def _folder_prefix(prefix: str) -> str:
    text = str(prefix or "").strip().replace("\\", "/").strip("/")
    if not text or text.startswith("~"):
        raise ValueError("Invalid path")
    path = Path(text)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError("Invalid path")
    return text


def files_with_prefix(username: str, chat_id: str, prefix: str) -> list[str]:
    text = _folder_prefix(prefix)
    return [
        row["path"]
        for row in list_files(username, chat_id)
        if row["path"] == text or row["path"].startswith(text + "/")
    ]


def mkdir(username: str, chat_id: str, rel: str) -> str:
    text = _folder_prefix(rel)
    root = workspace_root(username, chat_id, create=True)
    path = resolve_rel(root, text)
    if path.exists() and not path.is_dir():
        raise ValueError(f"{text} already exists.")
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path.relative_to(root.resolve()).as_posix()


def _copy_empty_dirs(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.rglob("*")):
        if path.is_symlink() or not path.is_dir():
            continue
        (dest / path.relative_to(src)).mkdir(parents=True, exist_ok=True)


def delete_prefix(username: str, chat_id: str, prefix: str) -> list[str]:
    text = _folder_prefix(prefix)
    root = workspace_root(username, chat_id, create=False)
    folder = resolve_rel(root, text)
    paths = files_with_prefix(username, chat_id, text)
    if not folder.is_dir() and not paths:
        raise FileNotFoundError(text)
    for rel in paths:
        delete_file(username, chat_id, rel)
    if folder.is_dir():
        shutil.rmtree(folder)
        _prune_empty_parents(root, folder.parent)
    return paths


def rename_prefix(username: str, chat_id: str, src: str, dest: str) -> list[tuple[str, str]]:
    source = _folder_prefix(src)
    target = _folder_prefix(dest)
    root = workspace_root(username, chat_id, create=False)
    src_path = resolve_rel(root, source)
    dest_path = resolve_rel(root, target)
    paths = files_with_prefix(username, chat_id, source)
    if not src_path.is_dir() and not paths:
        raise FileNotFoundError(source)
    if target == source:
        return [(path, path) for path in paths]
    if target.startswith(source + "/") or source.startswith(target + "/"):
        raise ValueError("Cannot move a folder into itself.")
    if dest_path.exists():
        raise ValueError(f"{target} already exists.")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[str, str]] = []
    for rel in sorted(paths, key=len, reverse=True):
        suffix = rel[len(source) :].lstrip("/")
        new = target if not suffix else f"{target}/{suffix}"
        written = rename_file(username, chat_id, rel, new)
        moved.append((rel, written))
    if src_path.is_dir():
        if dest_path.exists():
            _copy_empty_dirs(src_path, dest_path)
            shutil.rmtree(src_path)
        else:
            src_path.rename(dest_path)
        _prune_empty_parents(root, src_path.parent)
    return moved


def copy_bytes(username: str, chat_id: str, rel: str, data: bytes) -> str:
    raw = data if isinstance(data, (bytes, bytearray)) else bytes(data or b"")
    root = workspace_root(username, chat_id, create=True)
    path = resolve_rel(root, rel)
    extra_files = 0 if path.is_file() else 1
    extra_bytes = len(raw) - (path.stat().st_size if path.is_file() else 0)
    _check_caps(root, extra_bytes=max(0, extra_bytes), extra_files=extra_files)
    _write_bytes_nofollow(root, rel, raw)
    return path.relative_to(root.resolve()).as_posix()


def optimize_image(
    username: str,
    chat_id: str,
    rel: str,
    *,
    output_path: str = "",
    max_width: Optional[int] = None,
    max_height: Optional[int] = None,
    quality: int = 82,
    output_format: str = "original",
    lossless: bool = False,
    trim_border: bool = False,
) -> dict[str, Any]:
    """Compress, resize, convert, or crop a uniform border from one raster."""
    from PIL import Image, ImageOps

    resolved = resolve_image_rel(username, chat_id, rel)
    if not is_image_path(resolved):
        raise ValueError("OptimizeImage only supports PNG, JPEG, WebP, and GIF files.")
    source = resolve_file(username, chat_id, resolved)

    formats = {
        ".png": "PNG",
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".webp": "WEBP",
        ".gif": "GIF",
    }
    requested = str(output_format or "original").strip().lower()
    if requested == "jpg":
        requested = "jpeg"
    if requested not in ("original", "png", "jpeg", "webp", "gif"):
        raise ValueError("format must be original, png, jpeg, webp, or gif")
    image_format = formats[source.suffix.lower()] if requested == "original" else requested.upper()

    destination = str(output_path or "").strip()
    wanted_ext = ".jpg" if image_format == "JPEG" else f".{image_format.lower()}"
    if not destination:
        if requested == "original" or source.suffix.lower() == wanted_ext:
            destination = resolved
        else:
            destination = str(Path(resolved).with_suffix(wanted_ext))
    destination_suffix = Path(destination).suffix.lower()
    if destination_suffix not in formats:
        raise ValueError("output_path must end in .png, .jpg, .jpeg, .webp, or .gif")
    if formats[destination_suffix] != image_format:
        raise ValueError("output_path extension does not match format")

    try:
        width_limit = int(max_width) if max_width is not None else None
        height_limit = int(max_height) if max_height is not None else None
        image_quality = int(quality)
    except (TypeError, ValueError) as exc:
        raise ValueError("Dimensions and quality must be integers.") from exc
    if width_limit is not None and not 1 <= width_limit <= 8192:
        raise ValueError("max_width must be between 1 and 8192")
    if height_limit is not None and not 1 <= height_limit <= 8192:
        raise ValueError("max_height must be between 1 and 8192")
    if not 1 <= image_quality <= 100:
        raise ValueError("quality must be between 1 and 100")

    original_bytes = source.stat().st_size
    try:
        with Image.open(source) as opened:
            if int(getattr(opened, "n_frames", 1) or 1) > 1:
                raise ValueError("Animated images are not supported by OptimizeImage.")
            if opened.width * opened.height > 40_000_000:
                raise ValueError("Image dimensions are too large to optimize safely.")
            image = ImageOps.exif_transpose(opened)
            image.load()
            original_size = (image.width, image.height)
            trimmed = False
            if trim_border:
                from images.trim import trim_image

                image, box = trim_image(image)
                trimmed = box is not None
            if width_limit is not None or height_limit is not None:
                bounds = (
                    width_limit or image.width,
                    height_limit or image.height,
                )
                image.thumbnail(bounds, Image.Resampling.LANCZOS)

            if image_format == "JPEG":
                if image.mode in ("RGBA", "LA") or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    rgba = image.convert("RGBA")
                    flattened = Image.new("RGB", rgba.size, "white")
                    flattened.paste(rgba, mask=rgba.getchannel("A"))
                    image = flattened
                elif image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")

            buffer = io.BytesIO()
            save_options: dict[str, Any] = {"format": image_format}
            if image_format == "PNG":
                save_options.update(optimize=True, compress_level=9)
            elif image_format == "JPEG":
                save_options.update(quality=image_quality, optimize=True, progressive=True)
            elif image_format == "WEBP":
                save_options.update(quality=image_quality, method=6, lossless=bool(lossless))
            elif image_format == "GIF":
                save_options.update(optimize=True)
            image.save(buffer, **save_options)
    except ValueError:
        raise
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Could not optimize {rel}: {exc}") from exc

    encoded = buffer.getvalue()
    written = copy_bytes(username, chat_id, destination, encoded)
    rewritten: list[str] = []
    if written != resolved:
        rewritten = _rewrite_project_image_path(username, chat_id, resolved, written)
        try:
            delete_file(username, chat_id, resolved)
        except (FileNotFoundError, OSError, ValueError):
            pass
    rewritten.extend(_sync_image_references(username, chat_id, written))
    # Keep the first occurrence of each rewritten path.
    rewritten = list(dict.fromkeys(rewritten))
    return {
        "path": written,
        "source": resolved,
        "format": image_format.lower(),
        "original_dimensions": f"{original_size[0]}x{original_size[1]}",
        "dimensions": f"{image.width}x{image.height}",
        "original_bytes": original_bytes,
        "bytes": len(encoded),
        "saved_bytes": original_bytes - len(encoded),
        "trimmed": trimmed,
        "rewritten": rewritten,
    }


_IMAGE_FORMATS = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
    ".gif": "GIF",
}


def crop_image(
    username: str,
    chat_id: str,
    rel: str,
    x: Any,
    y: Any,
    width: Any,
    height: Any,
) -> dict[str, Any]:
    """Crop a project raster in place to an axis-aligned pixel box."""
    from PIL import Image, ImageOps

    if not is_image_path(rel):
        raise ValueError("Crop only supports PNG, JPEG, WebP, and GIF files.")
    try:
        left = int(x)
        top = int(y)
        box_w = int(width)
        box_h = int(height)
    except (TypeError, ValueError) as exc:
        raise ValueError("Crop box must be integers.") from exc

    source = resolve_file(username, chat_id, rel)
    image_format = _IMAGE_FORMATS[source.suffix.lower()]
    original_bytes = source.stat().st_size
    try:
        with Image.open(source) as opened:
            if int(getattr(opened, "n_frames", 1) or 1) > 1:
                raise ValueError("Animated images are not supported by crop.")
            if opened.width * opened.height > 40_000_000:
                raise ValueError("Image dimensions are too large to crop safely.")
            image = ImageOps.exif_transpose(opened)
            image.load()
            original_size = (image.width, image.height)
            right = max(0, min(left + box_w, image.width))
            bottom = max(0, min(top + box_h, image.height))
            left = max(0, min(left, image.width))
            top = max(0, min(top, image.height))
            if right <= left or bottom <= top:
                raise ValueError("Crop box is empty.")
            image = image.crop((left, top, right, bottom))
            if image_format == "JPEG":
                if image.mode in ("RGBA", "LA") or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    rgba = image.convert("RGBA")
                    flattened = Image.new("RGB", rgba.size, "white")
                    flattened.paste(rgba, mask=rgba.getchannel("A"))
                    image = flattened
                elif image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
            buffer = io.BytesIO()
            save_options: dict[str, Any] = {"format": image_format}
            if image_format == "PNG":
                save_options.update(optimize=True, compress_level=9)
            elif image_format == "JPEG":
                save_options.update(quality=92, optimize=True, progressive=True)
            elif image_format == "WEBP":
                save_options.update(quality=82, method=6)
            elif image_format == "GIF":
                save_options.update(optimize=True)
            image.save(buffer, **save_options)
    except ValueError:
        raise
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Could not crop {rel}: {exc}") from exc

    encoded = buffer.getvalue()
    written = copy_bytes(username, chat_id, rel, encoded)
    return {
        "path": written,
        "format": image_format.lower(),
        "original_dimensions": f"{original_size[0]}x{original_size[1]}",
        "dimensions": f"{image.width}x{image.height}",
        "original_bytes": original_bytes,
        "bytes": len(encoded),
    }


def resize_image(
    username: str,
    chat_id: str,
    rel: str,
    width: Any,
    height: Any,
) -> dict[str, Any]:
    """Resize a project raster in place to exact pixel dimensions."""
    from PIL import Image, ImageOps

    if not is_image_path(rel):
        raise ValueError("Resize only supports PNG, JPEG, WebP, and GIF files.")
    try:
        box_w = int(width)
        box_h = int(height)
    except (TypeError, ValueError) as exc:
        raise ValueError("Width and height must be integers.") from exc
    if not 1 <= box_w <= 8192 or not 1 <= box_h <= 8192:
        raise ValueError("Width and height must be between 1 and 8192.")
    if box_w * box_h > 40_000_000:
        raise ValueError("Image dimensions are too large to resize safely.")

    source = resolve_file(username, chat_id, rel)
    image_format = _IMAGE_FORMATS[source.suffix.lower()]
    original_bytes = source.stat().st_size
    try:
        with Image.open(source) as opened:
            if int(getattr(opened, "n_frames", 1) or 1) > 1:
                raise ValueError("Animated images are not supported by resize.")
            if opened.width * opened.height > 40_000_000:
                raise ValueError("Image dimensions are too large to resize safely.")
            image = ImageOps.exif_transpose(opened)
            image.load()
            original_size = (image.width, image.height)
            if image_format == "JPEG":
                if image.mode in ("RGBA", "LA") or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    rgba = image.convert("RGBA")
                    flattened = Image.new("RGB", rgba.size, "white")
                    flattened.paste(rgba, mask=rgba.getchannel("A"))
                    image = flattened
                elif image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
            elif image.mode == "P":
                image = image.convert(
                    "RGBA" if "transparency" in image.info else "RGB"
                )
            image = image.resize((box_w, box_h), Image.Resampling.LANCZOS)
            has_alpha = image.mode in ("RGBA", "LA")
            buffer = io.BytesIO()
            save_options: dict[str, Any] = {"format": image_format}
            if image_format == "PNG":
                save_options.update(optimize=True, compress_level=9)
            elif image_format == "JPEG":
                save_options.update(quality=92, optimize=True, progressive=True)
            elif image_format == "WEBP":
                save_options.update(quality=82, method=6, lossless=has_alpha)
            elif image_format == "GIF":
                save_options.update(optimize=True)
            image.save(buffer, **save_options)
    except ValueError:
        raise
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Could not resize {rel}: {exc}") from exc

    encoded = buffer.getvalue()
    written = copy_bytes(username, chat_id, rel, encoded)
    return {
        "path": written,
        "format": image_format.lower(),
        "original_dimensions": f"{original_size[0]}x{original_size[1]}",
        "dimensions": f"{image.width}x{image.height}",
        "original_bytes": original_bytes,
        "bytes": len(encoded),
    }


def _parse_punch_seeds(raw: Any) -> list[tuple[int, int]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("seeds must be a list.")
    if len(raw) > 64:
        raise ValueError("Too many sample points.")
    out: list[tuple[int, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each seed must be an object with x and y.")
        try:
            out.append((int(item.get("x")), int(item.get("y"))))
        except (TypeError, ValueError) as exc:
            raise ValueError("Seed coordinates must be integers.") from exc
    return out


def _parse_punch_boxes(raw: Any) -> list[tuple[int, int, int, int]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("boxes must be a list.")
    if len(raw) > 32:
        raise ValueError("Too many boxes.")
    out: list[tuple[int, int, int, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each box must be an object with x, y, width, and height.")
        try:
            out.append(
                (
                    int(item.get("x")),
                    int(item.get("y")),
                    int(item.get("width")),
                    int(item.get("height")),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Box values must be integers.") from exc
    return out


def _dilate_mask(mask: bytearray, width: int, height: int) -> bytearray:
    out = bytearray(mask)
    for y in range(height):
        row = y * width
        for x in range(width):
            if not mask[row + x]:
                continue
            if x:
                out[row + x - 1] = 1
            if x + 1 < width:
                out[row + x + 1] = 1
            if y:
                out[row - width + x] = 1
            if y + 1 < height:
                out[row + width + x] = 1
    return out


def _unique_rel(username: str, chat_id: str, rel: str) -> str:
    root = workspace_root(username, chat_id, create=True)
    if not resolve_rel(root, rel).exists():
        return rel
    parent = Path(rel).parent
    prefix = "" if str(parent) in (".", "") else f"{parent.as_posix()}/"
    stem = Path(rel).stem
    suffix = Path(rel).suffix
    for index in range(2, 100):
        candidate = f"{prefix}{stem}-{index}{suffix}"
        if not resolve_rel(root, candidate).exists():
            return candidate
    raise ValueError("Could not pick a free filename.")


def punch_image(
    username: str,
    chat_id: str,
    rel: str,
    seeds: Any = None,
    boxes: Any = None,
    tolerance: Any = 28,
    contiguous: bool = True,
) -> dict[str, Any]:
    """Punch selected colors or boxes to real alpha in a project raster."""
    from collections import deque

    from PIL import Image, ImageOps

    if not is_image_path(rel):
        raise ValueError("Make transparent only supports PNG, JPEG, WebP, and GIF files.")
    points = _parse_punch_seeds(seeds)
    rects = _parse_punch_boxes(boxes)
    if not points and not rects:
        raise ValueError("Select at least one area.")
    try:
        tol = int(tolerance)
    except (TypeError, ValueError) as exc:
        raise ValueError("Tolerance must be an integer.") from exc
    if not 0 <= tol <= 96:
        raise ValueError("Tolerance must be between 0 and 96.")

    source = resolve_file(username, chat_id, rel)
    source_format = _IMAGE_FORMATS[source.suffix.lower()]
    original_bytes = source.stat().st_size
    dest = rel
    image_format = source_format
    if source_format in ("JPEG", "GIF"):
        dest = _unique_rel(username, chat_id, str(Path(rel).with_suffix(".png")))
        image_format = "PNG"

    try:
        with Image.open(source) as opened:
            if int(getattr(opened, "n_frames", 1) or 1) > 1:
                raise ValueError("Animated images are not supported by make transparent.")
            if opened.width * opened.height > 40_000_000:
                raise ValueError("Image dimensions are too large to edit safely.")
            image = ImageOps.exif_transpose(opened)
            image.load()
            original_size = (image.width, image.height)
            rgba = image.convert("RGBA")
            pixels = rgba.load()
            width, height = rgba.size
            mask = bytearray(width * height)
            tol2 = tol * tol

            def matches(x: int, y: int, target: tuple[int, int, int]) -> bool:
                red, green, blue, alpha = pixels[x, y]
                if alpha < 8:
                    return False
                return (
                    (red - target[0]) ** 2
                    + (green - target[1]) ** 2
                    + (blue - target[2]) ** 2
                    <= tol2
                )

            def flood(start_x: int, start_y: int) -> None:
                if not (0 <= start_x < width and 0 <= start_y < height):
                    return
                if pixels[start_x, start_y][3] < 8:
                    return
                target = pixels[start_x, start_y][:3]
                if contiguous:
                    queue = deque([(start_x, start_y)])
                    while queue:
                        x, y = queue.popleft()
                        index = y * width + x
                        if mask[index] or not matches(x, y, target):
                            continue
                        mask[index] = 1
                        if x:
                            queue.append((x - 1, y))
                        if x + 1 < width:
                            queue.append((x + 1, y))
                        if y:
                            queue.append((x, y - 1))
                        if y + 1 < height:
                            queue.append((x, y + 1))
                    return
                for y in range(height):
                    row = y * width
                    for x in range(width):
                        if matches(x, y, target):
                            mask[row + x] = 1

            for start_x, start_y in points:
                flood(start_x, start_y)
            for left, top, box_w, box_h in rects:
                x0 = max(0, left)
                y0 = max(0, top)
                x1 = min(width, left + box_w)
                y1 = min(height, top + box_h)
                if x1 <= x0 or y1 <= y0:
                    continue
                for y in range(y0, y1):
                    row = y * width
                    for x in range(x0, x1):
                        if pixels[x, y][3] >= 8:
                            mask[row + x] = 1

            punched = sum(mask)
            if not punched:
                raise ValueError("Nothing to make transparent.")
            mask = _dilate_mask(mask, width, height)
            punched = sum(mask)
            for y in range(height):
                row = y * width
                for x in range(width):
                    if not mask[row + x]:
                        continue
                    red, green, blue, _alpha = pixels[x, y]
                    pixels[x, y] = (red, green, blue, 0)

            buffer = io.BytesIO()
            save_options: dict[str, Any] = {"format": image_format}
            if image_format == "PNG":
                save_options.update(optimize=True, compress_level=9)
            elif image_format == "WEBP":
                save_options.update(lossless=True, method=6)
            rgba.save(buffer, **save_options)
    except ValueError:
        raise
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Could not make {rel} transparent: {exc}") from exc

    encoded = buffer.getvalue()
    written = copy_bytes(username, chat_id, dest, encoded)
    rewritten: list[str] = []
    if written != rel:
        rewritten = _rewrite_project_image_path(username, chat_id, rel, written)
        try:
            delete_file(username, chat_id, rel)
        except (FileNotFoundError, OSError, ValueError):
            pass
    return {
        "path": written,
        "format": image_format.lower(),
        "original_dimensions": f"{original_size[0]}x{original_size[1]}",
        "dimensions": f"{rgba.width}x{rgba.height}",
        "original_bytes": original_bytes,
        "bytes": len(encoded),
        "punched": punched,
        "rewritten": rewritten,
    }


def _generated_item_prompt(job, dest: str) -> str:
    for item in getattr(job, "items", None) or []:
        if str(getattr(item, "output_path", "") or "").strip() == dest:
            return str(getattr(item, "prompt", "") or "")
    return ""


def _has_transparency(path: Path) -> bool:
    from PIL import Image

    try:
        with Image.open(path) as image:
            if image.mode in ("RGBA", "LA"):
                low, _high = image.getchannel("A").getextrema()
                return low < 255
            return image.mode == "P" and "transparency" in image.info
    except OSError:
        return False


def _rewrite_project_image_path(
    username: str, chat_id: str, old_path: str, new_path: str
) -> list[str]:
    changed: list[str] = []
    replacements = [(old_path, new_path)]
    old_name = Path(old_path).name
    new_name = Path(new_path).name
    if old_name != old_path:
        replacements.append((old_name, new_name))
    for row in list_files(username, chat_id):
        rel = str(row["path"])
        if not row["editable"] or rel in (old_path, new_path):
            continue
        text = read_text(username, chat_id, rel)
        updated = text
        for old, new in replacements:
            updated = updated.replace(old, new)
        if updated == text:
            continue
        write_text(username, chat_id, rel, updated, sync_images=False)
        changed.append(rel)
    return changed


def _resync_existing_image_refs(username: str, chat_id: str) -> None:
    """After a page rewrite, keep img/css urls on the files that exist (.webp)."""
    for row in list_files(username, chat_id):
        rel = str(row.get("path") or "")
        if is_image_path(rel):
            _sync_image_references(username, chat_id, rel)


def _sync_image_references(username: str, chat_id: str, actual_rel: str) -> list[str]:
    """Point stale .png/.jpg/… refs at the file that is actually on disk."""
    path = Path(str(actual_rel or "").replace("\\", "/"))
    if not is_image_path(str(path)):
        return []
    changed: list[str] = []
    for ext in IMAGE_SUFFIXES:
        if ext == path.suffix.lower():
            continue
        changed.extend(
            _rewrite_project_image_path(
                username, chat_id, str(path.with_suffix(ext)), str(path.as_posix())
            )
        )
    return list(dict.fromkeys(changed))


def _publish_dest_aliases(username: str, chat_id: str, dests: list[str]) -> list[str]:
    """Write hero/header aliases the page already references (hero.webp vs header.webp)."""
    extra: list[str] = []
    blob = ""
    for row in list_files(username, chat_id):
        if not row.get("editable"):
            continue
        try:
            blob += read_text(username, chat_id, str(row["path"])) + "\n"
        except (OSError, FileNotFoundError, ValueError):
            continue
    for dest in dests:
        stem = Path(dest).stem.lower().replace("_", "-")
        if stem not in _HERO_ALIAS_STEMS:
            continue
        try:
            src = resolve_file(username, chat_id, dest)
            raw = src.read_bytes()
        except (OSError, FileNotFoundError, ValueError):
            continue
        if not raster_head_ok(raw[:16]):
            continue
        suffix = Path(dest).suffix or ".webp"
        for alias in ("hero", "header", "banner"):
            if alias == Path(dest).stem.lower():
                continue
            mentioned = any(
                f"{alias}{ext}" in blob for ext in IMAGE_SUFFIXES
            )
            if not mentioned:
                continue
            written = copy_bytes(username, chat_id, f"images/{alias}{suffix}", raw)
            extra.append(written)
    return extra


def _ensure_logo_img(username: str, chat_id: str, dests: list[str]) -> None:
    """If a GPU logo exists but the page only has a text .logo, insert the img."""
    logo = next(
        (dest for dest in dests if Path(dest).stem.lower() == "logo"),
        "",
    )
    if not logo:
        return
    for row in list_files(username, chat_id):
        rel = str(row.get("path") or "")
        if not row.get("editable") or Path(rel).suffix.lower() not in {".html", ".htm"}:
            continue
        try:
            text = read_text(username, chat_id, rel)
        except (OSError, FileNotFoundError, ValueError):
            continue
        if re.search(r'(?is)<img\b[^>]+src=["\'][^"\']*logo', text):
            continue
        updated, n = re.subn(
            r'(<([a-zA-Z0-9]+)\b[^>]*\bclass="[^"]*\blogo\b[^"]*"[^>]*>)([^<]*)(</\2>)',
            rf'\1<img src="{logo}" alt="Logo">\4',
            text,
            count=1,
        )
        if n:
            if ".logo img" not in updated and "</style>" in updated:
                updated = updated.replace(
                    "</style>",
                    "        .logo img { height: 48px; width: auto; display: block; }\n"
                    "    </style>",
                    1,
                )
            write_text(username, chat_id, rel, updated, sync_images=False)
            return


_IMAGE_REF_RE = re.compile(
    r"""(?i)((?:src|href)\s*=\s*["']|url\(\s*["']?)([^"')\s]+)"""
)
_HERO_RULE_RE = re.compile(r"(\.hero\b[^{]*\{)([^}]*)\}", re.I)
_HERO_DEST_HINTS = frozenset({"hero", "header", "banner"})


def _page_file_texts(username: str, chat_id: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for row in list_files(username, chat_id):
        rel = str(row.get("path") or "")
        if not row.get("editable") or Path(rel).suffix.lower() not in _PAGE_SUFFIXES:
            continue
        try:
            rows.append((rel, read_text(username, chat_id, rel)))
        except (OSError, FileNotFoundError, ValueError):
            continue
    return rows


def _stem_tokens(path: str) -> set[str]:
    stem = Path(str(path or "").replace("\\", "/")).stem.lower().replace("_", "-")
    return {
        part
        for part in stem.split("-")
        if part and part not in {"img", "image", "pic", "photo", "png", "webp", "gif"}
    }


def _best_dest_for_ref(ref: str, dests: list[str]) -> str:
    want = _stem_tokens(ref)
    if not want:
        return ""
    best = ""
    best_score = 0
    for dest in dests:
        score = len(want & _stem_tokens(dest))
        if score > best_score:
            best = dest
            best_score = score
    return best if best_score else ""


def _project_blob(username: str, chat_id: str) -> str:
    return "\n".join(text for _rel, text in _page_file_texts(username, chat_id))


def _rewrite_stub_image_refs(username: str, chat_id: str, dests: list[str]) -> None:
    """Point leftover assets/ or stub refs at the GPU dest with the same stem tokens."""
    for rel, text in _page_file_texts(username, chat_id):
        updated = text
        for match in _IMAGE_REF_RE.finditer(text):
            ref = match.group(2)
            if not is_image_path(ref):
                continue
            if any(_path_mentioned(ref, dest) or Path(ref).name == Path(dest).name for dest in dests):
                continue
            target = _best_dest_for_ref(ref, dests)
            if target and target not in ref and ref not in target:
                updated = updated.replace(ref, target)
        if updated != text:
            write_text(username, chat_id, rel, updated, sync_images=False)


def _ensure_hero_background(username: str, chat_id: str, dests: list[str]) -> None:
    """If a hero dest exists but .hero has no photo, set background-image."""
    hero = next(
        (
            dest
            for dest in dests
            if _HERO_DEST_HINTS & _stem_tokens(dest)
        ),
        "",
    )
    if not hero:
        return
    blob = _project_blob(username, chat_id)
    if _path_mentioned(blob, hero):
        return
    for rel, text in _page_file_texts(username, chat_id):
        if Path(rel).suffix.lower() != ".css":
            continue
        if not _HERO_RULE_RE.search(text):
            continue

        def _inject(match: re.Match[str]) -> str:
            body = match.group(2)
            if "url(" in body:
                return match.group(0)
            return (
                f'{match.group(1)}{body.rstrip()}\n'
                f'    background-image: url("{hero}");\n'
                "}"
            )

        updated, n = _HERO_RULE_RE.subn(_inject, text, count=1)
        if n and updated != text:
            write_text(username, chat_id, rel, updated, sync_images=False)
            return


def _wire_job_dests(username: str, chat_id: str, dests: list[str]) -> None:
    """After GPU rasters land, make the page actually use them."""
    if not dests:
        return
    _rewrite_stub_image_refs(username, chat_id, dests)
    _ensure_hero_background(username, chat_id, dests)


def copy_job_pngs(username: str, chat_id: str, job) -> list[str]:
    from common.gpu_mode import generated_image_path
    from images.paths import generated_png_name_from_url, living_download_pairs

    copied: list[str] = []
    for url, dest in living_download_pairs(job):
        name = generated_png_name_from_url(url)
        src = generated_image_path(name)
        if src is None:
            continue
        copied.append(copy_bytes(username, chat_id, dest, src.read_bytes()))

    # A Code-mode request that produced an HTML page is a website build. Convert
    # its generated PNG assets to WebP and update the page/CSS/JS references as
    # one server-side operation, so the model cannot leave stale .png paths.
    if not site_entry(username, chat_id):
        return copied

    optimized: list[str] = []
    for dest in copied:
        source = resolve_file(username, chat_id, dest)
        prompt = f"{dest} {_generated_item_prompt(job, dest)}".lower()
        graphic = any(
            word in prompt
            for word in ("logo", "icon", "badge", "wordmark", "emblem", "transparent")
        )
        transparent = _has_transparency(source)
        output = str(Path(dest).with_suffix(".webp"))
        try:
            result = optimize_image(
                username,
                chat_id,
                dest,
                output_path=output,
                max_width=1024 if graphic else 1920,
                max_height=1024 if graphic else 1920,
                quality=88 if graphic else 82,
                output_format="webp",
                lossless=graphic or transparent,
            )
            written = str(result["path"])
            optimized.append(written)
        except (ValueError, FileNotFoundError, OSError):
            # Keep the original PNG and its already-written code reference if a
            # particular asset cannot be converted.
            optimized.append(dest)
    dests = optimized or copied
    dests.extend(_publish_dest_aliases(username, chat_id, dests))
    _ensure_logo_img(username, chat_id, dests)
    _wire_job_dests(username, chat_id, dests)
    return list(dict.fromkeys(dests))


def drafts_path(username: str, chat_id: str) -> Path:
    return user_dir(username) / f"{safe_name(chat_id)}{DRAFTS_SUFFIX}"


def load_drafts(username: str, chat_id: str) -> list[dict[str, Any]]:
    path = drafts_path(username, chat_id)
    with _DRAFTS_LOCK:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return []
    rows = raw.get("drafts") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "").strip()
        text = item.get("text")
        if not rel or not isinstance(text, str) or is_image_path(rel):
            continue
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            continue
        row: dict[str, Any] = {"path": rel, "text": text}
        caret = item.get("caret")
        if isinstance(caret, list) and len(caret) == 2:
            try:
                row["caret"] = [int(caret[0]), int(caret[1])]
            except (TypeError, ValueError):
                pass
        out.append(row)
        if len(out) >= MAX_DRAFTS:
            break
    return out


def save_drafts(username: str, chat_id: str, drafts: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in drafts if isinstance(drafts, list) else []:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "").strip().replace("\\", "/")
        text = item.get("text")
        if not rel or not isinstance(text, str) or is_image_path(rel):
            continue
        root = workspace_root(username, chat_id, create=False)
        try:
            resolve_rel(root, rel)
        except ValueError:
            continue
        if rel in seen:
            continue
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("Draft is too large.")
        seen.add(rel)
        row: dict[str, Any] = {"path": rel, "text": text}
        caret = item.get("caret")
        if isinstance(caret, list) and len(caret) == 2:
            try:
                row["caret"] = [int(caret[0]), int(caret[1])]
            except (TypeError, ValueError):
                pass
        rows.append(row)
        if len(rows) >= MAX_DRAFTS:
            break
    payload = json.dumps({"drafts": rows}, ensure_ascii=False, separators=(",", ":")) + "\n"
    data = payload.encode("utf-8")
    if len(data) > DRAFTS_MAX_BYTES:
        raise ValueError("Drafts are too large.")
    path = drafts_path(username, chat_id)
    with _DRAFTS_LOCK:
        if not rows:
            try:
                path.unlink()
            except OSError:
                pass
            return []
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, 0o600)
    return rows


def drop_draft(username: str, chat_id: str, rel: str) -> None:
    current = [row for row in load_drafts(username, chat_id) if row.get("path") != rel]
    save_drafts(username, chat_id, current)


def drop_drafts(username: str, chat_id: str) -> None:
    try:
        drafts_path(username, chat_id).unlink()
    except OSError:
        pass


def _rmtree(path: Path) -> None:
    """Remove a folder even when some files are not writable."""
    if not path.exists() and not path.is_symlink():
        return

    def _retry(func, item, _exc):
        try:
            os.chmod(item, 0o700)
            func(item)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onexc=_retry)
    except TypeError:
        shutil.rmtree(path, onerror=_retry)
    if path.exists() or path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)


def delete_workspace(username: str, chat_id: str) -> None:
    from ui.codebox import drop_container

    drop_container(username, chat_id)
    root = workspace_root(username, chat_id, create=False, box=False)
    _rmtree(root)
    drop_history(username, chat_id)
    drop_drafts(username, chat_id)
    from ui.preview import drop_storage

    drop_storage(username, chat_id)


def delete_user_workspaces(username: str) -> None:
    from ui.codebox import drop_user_containers

    drop_user_containers(username)
    folder = user_dir(username)
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


def zip_bytes(
    username: str, chat_id: str, paths: Optional[list[str]] = None
) -> bytes:
    root = workspace_root(username, chat_id, create=False)
    files = _iter_files(root)
    wanted: Optional[set[str]] = None
    if paths:
        wanted = set()
        for raw in paths:
            rel = str(raw or "").strip().replace("\\", "/").lstrip("/")
            if not rel or rel.startswith("/") or ".." in Path(rel).parts:
                continue
            wanted.add(rel)
        if not wanted:
            wanted = None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        file_arcs: list[str] = []
        for path in files:
            arc = path.relative_to(root.resolve()).as_posix()
            if wanted is not None:
                keep = arc in wanted or any(
                    arc.startswith(item.rstrip("/") + "/") for item in wanted
                )
                if not keep:
                    continue
            zf.write(path, arcname=arc)
            file_arcs.append(arc)
        for path in _iter_dirs(root):
            rel = path.relative_to(root.resolve()).as_posix()
            if wanted is not None and rel not in wanted:
                continue
            prefix = rel.rstrip("/") + "/"
            if any(arc == rel or arc.startswith(prefix) for arc in file_arcs):
                continue
            zf.writestr(zipfile.ZipInfo(prefix), "")
    return buf.getvalue()


def guess_media_type(path: Path) -> str:
    guessed, _enc = mimetypes.guess_type(path.name)
    if guessed:
        # Files here are written as UTF-8; say so or a preview mangles accents.
        if guessed.startswith("text/") or guessed in (
            "application/javascript",
            "application/json",
            "image/svg+xml",
        ):
            return f"{guessed}; charset=utf-8"
        return guessed
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return "application/octet-stream"
    return "text/plain; charset=utf-8"
