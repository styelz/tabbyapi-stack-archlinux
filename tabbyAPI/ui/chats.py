"""Per-user console chat history stored next to generated images."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

_LOCK = threading.Lock()
_CHATS_DIR: Optional[Path] = None
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

EMPTY_STORE = {
    "version": 1,
    "activeId": "",
    "chats": [],
    "lastByMode": {"chat": "", "code": ""},
}
PLACEHOLDER_TITLES = frozenset({"", "New chat", "New workspace"})
RECOVERED_TITLE = "Recovered workspace"


def chats_dir() -> Path:
    if _CHATS_DIR is not None:
        return _CHATS_DIR
    from common.gpu_mode import GENERATED_DIR

    path = GENERATED_DIR / "ui_chats"
    path.mkdir(parents=True, exist_ok=True)
    return path


def set_chats_dir(path: Optional[Path]) -> None:
    global _CHATS_DIR
    _CHATS_DIR = path


def _safe_name(username: str) -> str:
    name = SAFE_NAME_RE.sub("_", str(username or "").strip()) or "user"
    return name[:80]


def chat_path(username: str) -> Path:
    return chats_dir() / f"{_safe_name(username)}.json"


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


def _collapse_key(chat: dict[str, Any]) -> Optional[tuple[str, int]]:
    if str(chat.get("mode") or "") != "code":
        return None
    if str(chat.get("parentId") or "").strip():
        return None
    title = str(chat.get("title") or "").strip()
    if title in PLACEHOLDER_TITLES:
        return None
    try:
        stamp = int(chat.get("updatedAt") or 0)
    except (TypeError, ValueError):
        stamp = 0
    return (title, stamp)


def _collapse_duplicate_workspaces(
    chats: list[dict[str, Any]], protect: Optional[set[str]] = None
) -> list[dict[str, Any]]:
    """Merge clone-on-reload leftovers that share a title and timestamp.

    Distinct projects with the same name are kept. Empty unprotected
    New-workspace shells without nested chats are dropped.
    """
    guarded = {item for item in (protect or set()) if item}
    kids: dict[str, list[dict[str, Any]]] = {}
    for chat in chats:
        parent = str(chat.get("parentId") or "").strip()
        if parent:
            kids.setdefault(parent, []).append(chat)
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for chat in chats:
        key = _collapse_key(chat)
        if key is None:
            continue
        groups.setdefault(key, []).append(chat)
    remap: dict[str, str] = {}
    drop: set[str] = set()
    for roots in groups.values():
        if len(roots) < 2:
            continue
        with_kids = [root for root in roots if root["id"] in kids]
        kept = (with_kids or roots)[0]
        for root in roots:
            if root["id"] == kept["id"]:
                continue
            drop.add(root["id"])
            remap[root["id"]] = kept["id"]
    for chat in chats:
        if chat["id"] in drop:
            continue
        title = str(chat.get("title") or "").strip()
        if (
            chat.get("mode") == "code"
            and not str(chat.get("parentId") or "").strip()
            and title in PLACEHOLDER_TITLES
            and chat["id"] not in kids
            and chat["id"] not in guarded
        ):
            drop.add(chat["id"])
    for chat in chats:
        parent = str(chat.get("parentId") or "").strip()
        if parent in remap:
            chat["parentId"] = remap[parent]
    return [chat for chat in chats if chat["id"] not in drop]


def _remap_collapsed_id(raw: Any, cleaned: list[dict[str, Any]], wanted: str) -> str:
    want = str(wanted or "").strip()
    if not want:
        return ""
    ids = {chat["id"] for chat in cleaned}
    if want in ids:
        return want
    incoming = raw.get("chats") if isinstance(raw, dict) else None
    if not isinstance(incoming, list):
        return ""
    match = next(
        (
            item
            for item in incoming
            if isinstance(item, dict) and str(item.get("id") or "") == want
        ),
        None,
    )
    if not isinstance(match, dict):
        return ""
    parent = str(match.get("parentId") or "").strip()
    if parent:
        if parent in ids:
            return parent
        return _remap_collapsed_id(raw, cleaned, parent)
    title = str(match.get("title") or "").strip()
    if title in PLACEHOLDER_TITLES:
        return ""
    try:
        stamp = int(match.get("updatedAt") or 0)
    except (TypeError, ValueError):
        stamp = 0
    for chat in cleaned:
        if _collapse_key(chat) == (title, stamp):
            return chat["id"]
    for chat in cleaned:
        if chat.get("mode") == "code" and not chat.get("parentId"):
            if str(chat.get("title") or "").strip() == title:
                return chat["id"]
    return ""


def _kept_duplicate_root(old: Any, remaining: list[dict[str, Any]]) -> str:
    if not isinstance(old, dict):
        return ""
    title = str(old.get("title") or "").strip()
    if title in PLACEHOLDER_TITLES:
        return ""
    try:
        stamp = int(old.get("updatedAt") or 0)
    except (TypeError, ValueError):
        stamp = 0
    for chat in remaining:
        if _collapse_key(chat) == (title, stamp):
            return str(chat.get("id") or "")
    for chat in remaining:
        if chat.get("mode") == "code" and not chat.get("parentId"):
            if str(chat.get("title") or "").strip() == title:
                return str(chat.get("id") or "")
    return ""


def _usage_payload(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        prompt = int(raw.get("prompt_tokens") or 0)
        completion = int(raw.get("completion_tokens") or 0)
        total = int(raw.get("total_tokens") or (prompt + completion))
    except (TypeError, ValueError):
        return None
    if total <= 0:
        total = prompt + completion
    if total <= 0:
        return None
    payload = {
        "prompt_tokens": max(0, prompt),
        "completion_tokens": max(0, completion),
        "total_tokens": max(0, total),
    }
    if raw.get("estimated"):
        payload["estimated"] = True
    return payload


GENERATED_MEDIA_RE = re.compile(
    r"generated-[A-Za-z0-9._-]+\.(?:png|jpe?g|webp|gif|wav|flac|mp3|mp4|webm)",
    re.I,
)


def generated_media_names(text: str) -> set[str]:
    return {match.group(0).lower() for match in GENERATED_MEDIA_RE.finditer(str(text or ""))}


def drop_duplicate_media_replies(messages: Any) -> list:
    """Drop a persist echo that repeats the same generated file as the last reply.

    Console Chat writes \"Here's the video.\" The job persist path used to append
    a second \"Here's the picture.\" bubble with the same MP4.
    """
    if not isinstance(messages, list):
        return []
    out: list[Any] = []
    for item in messages:
        if not (isinstance(item, dict) and item.get("role") == "assistant"):
            out.append(item)
            continue
        content = str(item.get("content") or "")
        names = generated_media_names(content)
        prev = out[-1] if out else None
        prev_content = str((prev or {}).get("content") or "") if isinstance(prev, dict) else ""
        prev_names = generated_media_names(prev_content)
        if (
            names
            and isinstance(prev, dict)
            and prev.get("role") == "assistant"
            and prev_names
            and not names.isdisjoint(prev_names)
        ):
            prev_mark = "tabby-image-job:" in prev_content
            this_mark = "tabby-image-job:" in content
            if prev_mark and not this_mark:
                out[-1] = item
            continue
        out.append(item)
    return out


def normalize_store(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "version": 1,
            "activeId": "",
            "chats": [],
            "lastByMode": {"chat": "", "code": ""},
        }
    chats = raw.get("chats")
    if not isinstance(chats, list):
        chats = []
    cleaned = []
    seen: set[str] = set()
    for item in chats:
        if not isinstance(item, dict):
            continue
        chat_id = str(item.get("id") or "").strip()
        if not chat_id or chat_id in seen:
            continue
        seen.add(chat_id)
        messages = item.get("messages")
        if not isinstance(messages, list):
            messages = []
        mode = str(item.get("mode") or "chat").strip().lower()
        if mode not in ("chat", "code"):
            mode = "chat"
        parent_id = ""
        if mode == "code":
            parent_id = str(item.get("parentId") or "").strip()
            if parent_id == chat_id:
                parent_id = ""
        row = {
            "id": chat_id,
            "title": str(item.get("title") or "New chat"),
            "updatedAt": int(item.get("updatedAt") or 0),
            "pinned": bool(item.get("pinned")),
            "titleLocked": bool(item.get("titleLocked")),
            "mode": mode,
            "parentId": parent_id,
            "messages": drop_duplicate_media_replies(messages),
        }
        folder = str(item.get("folder") or "").strip()[:80]
        if mode == "chat" and folder:
            row["folder"] = folder
        usage = _usage_payload(item.get("usage"))
        if usage:
            row["usage"] = usage
        cleaned.append(row)
    roots = {
        chat["id"]
        for chat in cleaned
        if chat["mode"] == "code" and not chat["parentId"]
    }
    cleaned = [
        chat
        for chat in cleaned
        if not chat["parentId"] or chat["parentId"] in roots
    ]
    last_hint = raw.get("lastByMode") if isinstance(raw.get("lastByMode"), dict) else {}
    cleaned = _collapse_duplicate_workspaces(
        cleaned,
        {
            str(raw.get("activeId") or ""),
            str(last_hint.get("code") or ""),
        },
    )
    ids = {chat["id"] for chat in cleaned}
    active = str(raw.get("activeId") or "")
    if active not in ids:
        # A collapsed duplicate root may still be the selected chat.
        active = _remap_collapsed_id(raw, cleaned, active)
    if cleaned and active not in {c["id"] for c in cleaned}:
        active = cleaned[0]["id"]
    if not cleaned:
        active = ""
    version = raw.get("version")
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = 1
    last_raw = raw.get("lastByMode")
    last = last_raw if isinstance(last_raw, dict) else {}
    last_by_mode = {
        "chat": str(last.get("chat") or ""),
        "code": str(last.get("code") or ""),
    }
    if last_by_mode["code"] not in ids:
        last_by_mode["code"] = _remap_collapsed_id(raw, cleaned, last_by_mode["code"])
    if last_by_mode["chat"] not in ids or not any(
        c["id"] == last_by_mode["chat"] and c["mode"] == "chat" for c in cleaned
    ):
        last_by_mode["chat"] = ""
    if last_by_mode["code"] not in ids or not any(
        c["id"] == last_by_mode["code"] and c["mode"] == "code" for c in cleaned
    ):
        last_by_mode["code"] = ""
    return {
        "version": version or 1,
        "activeId": active,
        "chats": cleaned,
        "lastByMode": last_by_mode,
    }


def _read_disk(username: str) -> Any:
    path = chat_path(username)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_TITLE_RE = re.compile(r"<title>([^<]+)</title>", re.I | re.S)


def _workspace_title_from_files(username: str, chat_id: str) -> str:
    from ui.workspace import workspace_root

    page = workspace_root(username, chat_id, create=False, box=False) / "index.html"
    if not page.is_file():
        return RECOVERED_TITLE
    try:
        text = page.read_text(encoding="utf-8", errors="replace")[:8000]
    except OSError:
        return RECOVERED_TITLE
    match = _TITLE_RE.search(text)
    title = re.sub(r"\s+", " ", (match.group(1) if match else "").strip())
    return title[:80] or RECOVERED_TITLE


def _has_user_turn(chat: Any) -> bool:
    if not isinstance(chat, dict):
        return False
    for item in chat.get("messages") or []:
        if (
            isinstance(item, dict)
            and item.get("role") == "user"
            and str(item.get("content") or "").strip()
        ):
            return True
    return False


def _rehydrate_file_workspaces(username: str, store: dict[str, Any]) -> dict[str, Any]:
    """Put Code workspace rows back when the project folder still has files."""
    from ui.workspace import has_project_files, list_project_ids

    chats = list(store.get("chats") or [])
    known = {
        str(chat.get("id") or "")
        for chat in chats
        if isinstance(chat, dict) and str(chat.get("id") or "").strip()
    }
    added = False
    now = int(time.time() * 1000)
    for chat_id in list_project_ids(username):
        if chat_id in known or not has_project_files(username, chat_id):
            continue
        chats.append(
            {
                "id": chat_id,
                "title": _workspace_title_from_files(username, chat_id),
                "updatedAt": now,
                "pinned": False,
                "titleLocked": False,
                "mode": "code",
                "parentId": "",
                "messages": [],
            }
        )
        known.add(chat_id)
        added = True
    if not added:
        return store
    next_store = dict(store)
    next_store["chats"] = chats
    return normalize_store(next_store)


def _drop_image_only_recovered(username: str, store: dict[str, Any]) -> dict[str, Any]:
    """Drop leftover Chat image copies that were revived as empty Code chats."""
    from ui.workspace import delete_workspace, has_project_files

    chats = list(store.get("chats") or [])
    drop: set[str] = set()
    for chat in chats:
        if not is_workspace_root(chat):
            continue
        if str(chat.get("title") or "").strip() != RECOVERED_TITLE:
            continue
        if _has_user_turn(chat):
            continue
        chat_id = str(chat.get("id") or "").strip()
        if not chat_id or has_project_files(username, chat_id):
            continue
        drop.add(chat_id)
    if not drop:
        return store
    next_chats = []
    for chat in chats:
        chat_id = str(chat.get("id") or "").strip()
        parent = str(chat.get("parentId") or "").strip()
        if chat_id in drop or parent in drop:
            continue
        next_chats.append(chat)
    for chat_id in drop:
        delete_workspace(username, chat_id)
    next_store = dict(store)
    next_store["chats"] = next_chats
    return normalize_store(next_store)


def _finish_file_workspaces(username: str, store: dict[str, Any]) -> dict[str, Any]:
    return _drop_image_only_recovered(
        username, _rehydrate_file_workspaces(username, store)
    )


def _restore_omitted_file_workspaces(
    username: str,
    store: dict[str, Any],
    old_chats: dict[str, Any],
    dropped: list[str],
) -> dict[str, Any]:
    """A stale PUT must not erase Code workspaces whose folders are still on disk."""
    restore_roots = [
        chat_id
        for chat_id in dropped
        if _keep_omitted_workspace(username, old_chats.get(chat_id))
    ]
    if not restore_roots:
        return _finish_file_workspaces(username, store)
    chats = list(store.get("chats") or [])
    new_ids = {str(chat.get("id") or "") for chat in chats}
    restore_set = set(restore_roots)
    for chat_id in restore_roots:
        if chat_id in new_ids:
            continue
        chats.append(old_chats[chat_id])
        new_ids.add(chat_id)
    for chat_id, old in old_chats.items():
        if chat_id in new_ids or not isinstance(old, dict):
            continue
        parent = str(old.get("parentId") or "").strip()
        if parent in restore_set:
            chats.append(old)
            new_ids.add(chat_id)
    next_store = dict(store)
    next_store["chats"] = chats
    return _finish_file_workspaces(username, normalize_store(next_store))


def load_store(username: str) -> dict[str, Any]:
    with _LOCK:
        raw = _read_disk(username)
    if raw is None:
        store = dict(EMPTY_STORE)
    else:
        store = normalize_store(raw)
    return _finish_file_workspaces(username, store)


def is_workspace_root(chat: Any) -> bool:
    """True for a Code-mode chat that owns the project folder."""
    if not isinstance(chat, dict):
        return False
    if str(chat.get("mode") or "chat").strip().lower() != "code":
        return False
    return not str(chat.get("parentId") or "").strip()


def is_code_chat(username: str, chat_id: str) -> bool:
    """True when this id is already a Code-mode row on disk (no rehydrate)."""
    want = str(chat_id or "").strip()
    if not want:
        return False
    with _LOCK:
        raw = _read_disk(username)
    if not isinstance(raw, dict):
        return False
    for item in raw.get("chats") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "").strip() != want:
            continue
        return str(item.get("mode") or "chat").strip().lower() == "code"
    return False


def _keep_omitted_workspace(username: str, chat: Any) -> bool:
    """Keep a real Code project that a stale PUT dropped, not Chat image leftovers."""
    if not is_workspace_root(chat):
        return False
    chat_id = str(chat.get("id") or "").strip()
    if not chat_id:
        return False
    from ui.workspace import has_files, has_project_files

    if has_project_files(username, chat_id):
        return True
    return _has_user_turn(chat) and has_files(username, chat_id)


def forget_workspace(username: str, chat_id: str) -> dict[str, Any]:
    """Drop a Code workspace row after DELETE has already removed its folder."""
    from ui.workspace import safe_name

    raw = str(chat_id or "").strip()
    drop = {item for item in (raw, safe_name(raw) if raw else "") if item}
    store = load_store(username)
    chats = [
        chat
        for chat in store.get("chats") or []
        if isinstance(chat, dict)
        and str(chat.get("id") or "") not in drop
        and str(chat.get("parentId") or "").strip() not in drop
    ]
    if len(chats) == len(store.get("chats") or []):
        return store
    next_store = dict(store)
    next_store["chats"] = chats
    return save_store(username, next_store)


def workspace_root_chat_id(username: str, chat_id: str) -> str:
    """The Code-mode workspace id that owns this chat's project folder."""
    raw = str(chat_id or "").strip()
    if not raw:
        raise ValueError("Invalid chat id")
    for chat in load_store(username).get("chats") or []:
        if not isinstance(chat, dict):
            continue
        if str(chat.get("id") or "") != raw:
            continue
        parent = str(chat.get("parentId") or "").strip()
        return parent or raw
    return raw


def workspace_thread_ids(username: str, root_id: str) -> list[str]:
    raw = str(root_id or "").strip()
    if not raw:
        return []
    return [
        str(chat.get("id") or "")
        for chat in load_store(username).get("chats") or []
        if isinstance(chat, dict)
        and str(chat.get("parentId") or "").strip() == raw
        and str(chat.get("id") or "").strip()
    ]


def _message_key(item: Any) -> Optional[tuple[str, str]]:
    if not isinstance(item, dict):
        return None
    role = str(item.get("role") or "")
    if role not in ("system", "user", "assistant"):
        return None
    return (role, str(item.get("content") or ""))


def _preserve_server_assistant(old_msgs: Any, new_msgs: Any) -> list:
    """Keep a reply the server wrote if a stale PUT arrives without it."""
    incoming = new_msgs if isinstance(new_msgs, list) else []
    previous = old_msgs if isinstance(old_msgs, list) else []
    if not previous:
        return incoming
    last = previous[-1]
    if not isinstance(last, dict) or last.get("role") != "assistant":
        return incoming
    if last.get("origin") != "server":
        return incoming
    if incoming:
        new_last = incoming[-1]
        if (
            isinstance(new_last, dict)
            and new_last.get("role") == "assistant"
            and str(new_last.get("content") or "") == str(last.get("content") or "")
        ):
            return incoming
    head = previous[:-1]
    if len(incoming) != len(head):
        return incoming
    if any(_message_key(a) != _message_key(b) for a, b in zip(incoming, head)):
        return incoming
    return incoming + [last]


def save_store(username: str, raw: Any) -> dict[str, Any]:
    with _LOCK:
        disk = _read_disk(username)
    previous = normalize_store(disk) if disk is not None else dict(EMPTY_STORE)
    store = normalize_store(raw)
    old_chats: dict[str, Any] = {}
    if isinstance(disk, dict) and isinstance(disk.get("chats"), list):
        for item in disk["chats"]:
            if isinstance(item, dict) and str(item.get("id") or "").strip():
                old_chats[str(item.get("id")).strip()] = item
    for chat in previous.get("chats") or []:
        chat_id = str(chat.get("id") or "")
        if chat_id and chat_id not in old_chats:
            old_chats[chat_id] = chat
    for chat in store.get("chats") or []:
        chat_id = str(chat.get("id") or "")
        old = old_chats.get(chat_id)
        if not old:
            continue
        chat["messages"] = _preserve_server_assistant(
            old.get("messages"), chat.get("messages")
        )
    new_ids = {str(chat.get("id") or "") for chat in store.get("chats") or []}
    dropped = [chat_id for chat_id in old_chats if chat_id not in new_ids]
    store = _restore_omitted_file_workspaces(username, store, old_chats, dropped)
    new_ids = {str(chat.get("id") or "") for chat in store.get("chats") or []}
    remaining_roots = [
        chat for chat in store.get("chats") or [] if is_workspace_root(chat)
    ]
    dropped = [chat_id for chat_id in old_chats if chat_id not in new_ids]
    payload = json.dumps(store, ensure_ascii=False) + "\n"
    with _LOCK:
        _atomic_write(chat_path(username), payload)
    if dropped:
        from ui.workspace import drop_drafts, drop_history, merge_workspace_dirs

        for chat_id in dropped:
            # Nested chats share the parent folder; never wipe it with the thread.
            if not is_workspace_root(old_chats.get(chat_id)):
                continue
            kept = _kept_duplicate_root(old_chats.get(chat_id), remaining_roots)
            if kept:
                merge_workspace_dirs(username, kept, [chat_id])
                drop_history(username, chat_id)
                drop_drafts(username, chat_id)
                continue
            # A PUT can omit a workspace after a proxy blip or another tab
            # saving an older list. Never wipe the project folder for that.
            # Explicit delete is DELETE /workspace/{id}.
    return store


def append_flight_assistant(
    username: str,
    chat_id: str,
    *,
    content: str,
    reasoning: str = "",
    elapsed_s: Optional[int] = None,
    status_label: str = "",
    steps: Optional[list] = None,
    agent: str = "",
) -> None:
    """Write a finished console reply so a reload can show it."""
    cid = str(chat_id or "").strip()
    if not cid:
        return
    text = str(content or "")
    thought = str(reasoning or "")
    stored_steps = [step for step in (steps or []) if isinstance(step, dict)]
    if not text.strip() and not thought.strip() and not stored_steps:
        return
    store = load_store(username)
    for chat in store.get("chats") or []:
        if str(chat.get("id") or "") != cid:
            continue
        messages = chat.get("messages")
        if not isinstance(messages, list):
            messages = []
            chat["messages"] = messages
        if messages:
            last = messages[-1]
            if (
                isinstance(last, dict)
                and last.get("role") == "assistant"
                and str(last.get("content") or "") == text
                and str(last.get("reasoning") or "") == thought
            ):
                return
        item: dict[str, Any] = {
            "role": "assistant",
            "content": text,
            "createdAt": int(time.time() * 1000),
            "origin": "server",
        }
        if thought.strip():
            item["reasoning"] = thought
        if elapsed_s:
            item["elapsed_s"] = int(elapsed_s)
        label = str(status_label or "").strip()
        if label:
            item["status_label"] = label
        if stored_steps:
            item["steps"] = stored_steps
        kind = str(agent or "").strip().lower()
        if kind in ("ask", "plan"):
            item["agent"] = kind
        messages.append(item)
        chat["updatedAt"] = item["createdAt"]
        save_store(username, store)
        return


def chat_count(username: str) -> int:
    """Conversations that have at least one user message."""
    n = 0
    for chat in load_store(username).get("chats") or []:
        messages = chat.get("messages") if isinstance(chat, dict) else None
        if not isinstance(messages, list):
            continue
        if any(
            isinstance(item, dict)
            and item.get("role") == "user"
            and str(item.get("content") or "").strip()
            for item in messages
        ):
            n += 1
    return n


def delete_store(username: str) -> None:
    path = chat_path(username)
    with _LOCK:
        try:
            path.unlink()
        except OSError:
            pass
    from ui import lsp, shell
    from ui.preview import drop_user
    from ui.workspace import delete_user_workspaces

    shell.drop_user(username)
    lsp.drop_user(username)
    delete_user_workspaces(username)
    drop_user(username)
