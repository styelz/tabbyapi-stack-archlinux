"""Helpers for the /v1/ui management console."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from loguru import logger

from common.logger import is_hidden_journal_line, is_ui_access_line

ROOT = Path(__file__).resolve().parent.parent
STACK_ROOT = ROOT.parent
JOURNAL_UNITS = ("tabbyapi", "comfyui")
UPDATE_UNIT = "tabbyapi-stack-update"
CONSOLE_SYSTEM = (
    "You are chatting in the TabbyAPI Stack web console. Answer in this conversation "
    "only. Do not write project files, HTML, CSS, or scripts to disk. "
    "If the user asks for an image, describe or generate it; the UI will show PNGs. "
    "If they attach a picture and ask to remove a border or crop a frame, the GPU "
    "regenerates a new PNG. Do not claim CSS or JavaScript changes."
)
PROCESS_LOGS: deque[str] = deque(maxlen=4000)
_SINK_ID: Optional[int] = None
_STARTED_AT = time.time()


def visible_log_lines(lines, limit: Optional[int] = None) -> list[str]:
    out = [line for line in lines if line and not is_hidden_journal_line(line)]
    if limit is None:
        return out
    return out[-max(1, int(limit)) :]


def install_log_sink() -> None:
    global _SINK_ID
    if _SINK_ID is not None:
        return

    def _sink(message):
        text = str(message).rstrip("\n")
        if not text or is_hidden_journal_line(text):
            return
        PROCESS_LOGS.append(text)

    _SINK_ID = logger.add(
        _sink,
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {message}",
        colorize=False,
        enqueue=False,
    )


def journalctl_cmd(*, follow: bool = False, lines: int = 300) -> list[str]:
    cmd = [
        "journalctl",
        "--user",
        "--no-pager",
        "-o",
        "short-iso",
    ]
    for unit in JOURNAL_UNITS:
        cmd.extend(["-u", unit])
    if follow:
        # `-n 0 -f` can sit open without emitting new lines on some systemd
        # versions. `--since now` follows from this moment; the UI catches up
        # any gap from /logs/history.
        cmd.extend(["--since", "now", "-f"])
        return cmd
    count = max(1, min(int(lines), 5000))
    cmd.extend(["-n", str(count)])
    return cmd


def journalctl_history(lines: int = 300) -> list[str]:
    wanted = max(1, min(int(lines), 5000))
    fetch = min(5000, max(wanted * 6, 800))
    if shutil.which("journalctl") is None:
        return visible_log_lines(PROCESS_LOGS, wanted)
    try:
        completed = subprocess.run(
            journalctl_cmd(follow=False, lines=fetch),
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return visible_log_lines(PROCESS_LOGS, wanted)
    text = completed.stdout or ""
    if completed.returncode != 0 and not text.strip():
        return visible_log_lines(PROCESS_LOGS, wanted)
    return visible_log_lines(text.splitlines(), wanted)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except (asyncio.TimeoutError, ProcessLookupError):
        process.kill()


async def stream_journal_lines() -> AsyncIterator[str]:
    if shutil.which("journalctl") is None:
        async for line in _stream_process_logs():
            yield line
        return
    while True:
        process = await asyncio.create_subprocess_exec(
            *journalctl_cmd(follow=True),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            assert process.stdout is not None
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line and not is_hidden_journal_line(line):
                    yield line
        except asyncio.CancelledError:
            await _stop_process(process)
            raise
        await _stop_process(process)
        await asyncio.sleep(0.4)


async def _stream_process_logs() -> AsyncIterator[str]:
    index = 0
    for line in list(PROCESS_LOGS):
        index += 1
        if line and not is_hidden_journal_line(line):
            yield line
    while True:
        await asyncio.sleep(0.25)
        current = list(PROCESS_LOGS)
        if len(current) < index:
            index = 0
        while index < len(current):
            line = current[index]
            index += 1
            if line and not is_hidden_journal_line(line):
                yield line


def nvidia_stats() -> dict[str, Any]:
    if shutil.which("nvidia-smi") is None:
        return {}
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}
    rows = (out or "").strip().splitlines()
    if not rows:
        return {}
    parts = [part.strip() for part in rows[0].split(",")]
    if len(parts) < 5:
        return {"name": rows[0]}
    try:
        used = int(float(parts[1]))
        total = int(float(parts[2]))
        util = int(float(parts[3]))
        temp = int(float(parts[4]))
    except ValueError:
        return {"name": parts[0]}
    return {
        "name": parts[0],
        "memory_used_mib": used,
        "memory_total_mib": total,
        "utilization_pct": util,
        "temperature_c": temp,
    }


_GPU_CACHE_INTERVAL_S = 1.0
_gpu_cache_lock = threading.Lock()
_gpu_cache: dict[str, Any] = {}
_gpu_cache_stop = threading.Event()
_gpu_cache_thread: Optional[threading.Thread] = None


def cached_nvidia_stats() -> dict[str, Any]:
    """Last background nvidia-smi sample. Never blocks on the driver."""
    with _gpu_cache_lock:
        return dict(_gpu_cache)


def ensure_gpu_cache() -> None:
    """Start a 1s nvidia-smi sampler so the kiosk HUD is not zeros or a stall."""
    global _gpu_cache_thread
    with _gpu_cache_lock:
        if _gpu_cache_thread is not None and _gpu_cache_thread.is_alive():
            return
        _gpu_cache_stop.clear()
        _gpu_cache_thread = threading.Thread(
            target=_gpu_cache_loop, name="saver-gpu-cache", daemon=True
        )
        _gpu_cache_thread.start()


def _gpu_cache_loop() -> None:
    while True:
        sample = nvidia_stats()
        with _gpu_cache_lock:
            _gpu_cache.clear()
            _gpu_cache.update(sample)
        if _gpu_cache_stop.wait(_GPU_CACHE_INTERVAL_S):
            break


def reset_gpu_cache_for_tests() -> None:
    global _gpu_cache_thread
    _gpu_cache_stop.set()
    thread = _gpu_cache_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=0.2)
    with _gpu_cache_lock:
        _gpu_cache.clear()
        _gpu_cache_thread = None
    _gpu_cache_stop.clear()


def unit_active(name: str) -> Optional[bool]:
    if shutil.which("systemctl") is None:
        return None
    try:
        from common.gpu_mode import user_systemd_env

        completed = subprocess.run(
            ["systemctl", "--user", "is-active", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            env=user_systemd_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() == "active"


def _model_card() -> dict[str, Any]:
    from common import model

    container = getattr(model, "container", None)
    if not container or not getattr(container, "loaded", False):
        return {}
    try:
        card = container.model_info()
        payload = card.model_dump() if hasattr(card, "model_dump") else dict(card)
    except Exception:
        payload = {"id": getattr(getattr(container, "model_dir", None), "name", None)}
    params = payload.get("parameters") or {}
    return {
        "id": payload.get("id"),
        "max_seq_len": params.get("max_seq_len"),
        "cache_size": params.get("cache_size"),
        "cache_mode": params.get("cache_mode"),
        "use_vision": params.get("use_vision"),
    }


async def stack_status(request=None, username: str = "") -> dict[str, Any]:
    from common.gpu_mode import comfy_up, public_api_base, read_mode
    from common.health import HealthManager
    from common.phrase_switch import (
        last_llm_profile_name,
        profile_alias_for_model,
        profile_ui_labels,
        switch_lock_held,
        switch_lock_name,
    )
    from images.jobs import active_mcp_image_job, loaded_tabby_name
    from select_model import available_profiles, last_profile
    from ui.occupancy import snapshot as stack_queue_snapshot

    mode = read_mode()
    tabby = loaded_tabby_name()
    gpu_mode = "llm" if tabby else (mode.get("mode") or "llm")
    try:
        healthy, issues = await HealthManager.is_service_healthy()
        issue_text = [
            issue.description if hasattr(issue, "description") else str(issue) for issue in issues
        ]
    except Exception:
        healthy, issue_text = True, []
    lock_name = switch_lock_name()
    lock_held = switch_lock_held()
    restarting = lock_held and lock_name == "restart"
    switching = lock_held and not restarting
    job = active_mcp_image_job()
    job_info = None
    if job:
        job_info = {
            "id": getattr(job, "id", None),
            "status": getattr(job, "status", None),
            "phase": getattr(job, "phase", None),
            "count": getattr(job, "count", None),
            "current_index": getattr(job, "current_index", 0),
            "done_count": getattr(job, "done_count", 0),
            "wait_s": getattr(job, "wait_s", None),
            "wait_text": getattr(job, "wait_text", None),
            "prompt": getattr(job, "prompt", None),
            "started_at": getattr(job, "started_at", None),
        }
    http_up = comfy_up()
    comfy_unit = unit_active("comfyui")
    job_phase = (job_info or {}).get("phase")
    comfy_booting = (not http_up) and (bool(comfy_unit) or job_phase == "starting_comfy")
    if comfy_booting and not restarting:
        switching = True
    names = available_profiles()
    # Prefer the folder actually in VRAM over last.json (VRAM fallback can desync them).
    profile = profile_alias_for_model(tabby) or last_llm_profile_name() or last_profile()
    return {
        "ok": True,
        "gpu_mode": gpu_mode,
        "comfy_up": http_up,
        "tabby_model": tabby,
        "profile": profile,
        "profiles": names,
        "profile_labels": profile_ui_labels(names),
        "model": _model_card(),
        "health": {"healthy": healthy, "issues": issue_text},
        "units": {
            "tabbyapi": unit_active("tabbyapi"),
            "comfyui": comfy_unit,
        },
        "gpu": await asyncio.to_thread(nvidia_stats),
        "host": _host_live(),
        "uptime_s": int(time.time() - _STARTED_AT),
        "api_base": public_api_base(request),
        "job": job_info,
        "switching": switching,
        "restarting": restarting,
        "busy": lock_held or comfy_booting,
        "switch_target": lock_name or ("comfy" if comfy_booting else None),
        "user": os.environ.get("USER") or "",
        "now": datetime.now(timezone.utc).isoformat(),
        "stack_queue": stack_queue_snapshot(username),
    }


def _host_live() -> dict[str, Any]:
    """One-shot CPU / RAM / load for the status cards (not the chart history)."""
    cpu = None
    ram = None
    load1 = None
    try:
        import psutil

        cpu = float(psutil.cpu_percent(interval=None))
        ram = float(psutil.virtual_memory().percent)
    except Exception:
        pass
    try:
        load1 = float(os.getloadavg()[0])
    except (OSError, AttributeError):
        pass
    return {
        "cpu_pct": None if cpu is None else round(cpu, 1),
        "ram_pct": None if ram is None else round(ram, 1),
        "load1": None if load1 is None else round(load1, 2),
    }


def gallery_listing(
    page: int = 1,
    per_page: int = 24,
    *,
    username: str = "",
    is_admin: bool = False,
) -> dict[str, Any]:
    from common.gallery_owners import filter_files, owner_of
    from common.gpu_mode import gallery_page, gallery_thumb_href, list_generated_files

    files = filter_files(list_generated_files(), username, is_admin)
    shown, page, pages, per_page = gallery_page(files, page, per_page)
    items = []
    for path in shown:
        try:
            stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            when = stamp.strftime("%Y-%m-%d %H:%M UTC")
            size = path.stat().st_size
        except OSError:
            when = ""
            size = 0
        items.append(
            {
                "name": path.name,
                "mtime": when,
                "size": size,
                "url": f"/v1/ui/gallery/file/{path.name}",
                "thumb": f"/v1/ui/gallery/thumb/{path.name}",
                "public_thumb": gallery_thumb_href(path.name),
                "owner": owner_of(path.name) or "",
            }
        )
    return {
        "items": items,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "total": len(files),
    }


def gallery_upload(raw: bytes, username: str) -> dict[str, Any]:
    from common.gpu_mode import gallery_thumb_href, png_bytes_from_upload, save_generated_image

    png = png_bytes_from_upload(raw)
    dest = save_generated_image(png, owner=username, as_latest=False)
    return {
        "ok": True,
        "name": dest.name,
        "url": f"/v1/ui/gallery/file/{dest.name}",
        "thumb": f"/v1/ui/gallery/thumb/{dest.name}",
        "public_thumb": gallery_thumb_href(dest.name),
    }


UPDATE_PROMPT_NAME = "tabby-update-prompt.json"


def start_stack_restart() -> dict[str, Any]:
    from common.phrase_switch import restart_reply_text, start_restart

    ok = start_restart()
    return {
        "ok": ok,
        "message": restart_reply_text() if ok else "Could not start a restart (systemctl missing?).",
    }


def update_log_lines(limit: int = 400) -> list[str]:
    path = STACK_ROOT / "tabby-update.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    count = max(1, min(int(limit or 400), 5000))
    return lines[-count:]


def update_job_running() -> bool:
    return unit_active(UPDATE_UNIT) is True


def update_log_state(limit: int = 400) -> dict[str, Any]:
    return {"lines": update_log_lines(limit), "running": update_job_running()}


def _update_log_tail(limit: int = 40) -> str:
    return "\n".join(update_log_lines(limit))


def load_update_prompt(path: Path | None = None) -> dict[str, Any] | None:
    target = path if path is not None else STACK_ROOT / UPDATE_PROMPT_NAME
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _spawn_stack_update(script: Path, args: list[str], message: str) -> dict[str, Any]:
    """Run update.sh outside the tabbyapi cgroup so systemctl restart can finish.

    A child of tabbyapi.service (even with start_new_session) stays in that
    cgroup. install.sh then deadlocks on `systemctl --user restart tabbyapi`:
    systemd waits for the update process, the update process waits for systemd.
    The API never bounces and the Status modal waits forever.
    """
    started = {
        "ok": True,
        "message": message,
        "restarting": True,
        "log": _update_log_tail(400),
    }
    if update_job_running():
        return {
            "ok": False,
            "message": "An update is already running.",
            "log": _update_log_tail(400),
        }
    systemd_run = shutil.which("systemd-run")
    if systemd_run:
        from common.gpu_mode import user_systemd_env

        env = user_systemd_env()
        for extra in (
            ["systemctl", "--user", "reset-failed", UPDATE_UNIT],
            ["systemctl", "--user", "stop", UPDATE_UNIT],
        ):
            subprocess.run(
                extra,
                check=False,
                capture_output=True,
                timeout=5,
                env=env,
            )
        cmd = [
            systemd_run,
            "--user",
            "--collect",
            f"--unit={UPDATE_UNIT}",
            f"--working-directory={STACK_ROOT}",
            f"--setenv=XDG_RUNTIME_DIR={env['XDG_RUNTIME_DIR']}",
        ]
        dbus = env.get("DBUS_SESSION_BUS_ADDRESS")
        if dbus:
            cmd.append(f"--setenv=DBUS_SESSION_BUS_ADDRESS={dbus}")
        cmd.extend(["--", *args])
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(STACK_ROOT),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "message": str(exc), "log": _update_log_tail(400)}
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            return {
                "ok": False,
                "message": detail[:1500] if detail else "systemd-run failed to start the update.",
                "log": _update_log_tail(400),
            }
        started["log"] = _update_log_tail(400)
        return started
    try:
        subprocess.Popen(
            args,
            cwd=str(STACK_ROOT),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return {"ok": False, "message": str(exc), "log": _update_log_tail(400)}
    started["log"] = _update_log_tail(400)
    return started


def start_stack_update(*, full: bool = False) -> dict[str, Any]:
    script = STACK_ROOT / "update.sh"
    if not script.is_file():
        return {"ok": False, "message": f"update.sh not found at {script}"}
    if full:
        return _spawn_stack_update(
            script,
            ["bash", str(script), "--all", "--restart"],
            "Started full (git + deps) update. TabbyAPI will bounce when it finishes.",
        )
    return _spawn_stack_update(
        script,
        ["bash", str(script), "--git"],
        "Started git update. TabbyAPI restarts on its own if API code changed.",
    )


MAX_CODE_TOOL_ROUNDS = 32
_INSPECT_TOOLS = frozenset({"read", "grep", "glob", "list", "list_files"})


def _tool_call_names(item: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for call in item.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        raw = ""
        if isinstance(fn, dict):
            raw = str(fn.get("name") or "")
        if not raw:
            raw = str(call.get("name") or "")
        names.append(raw.strip().lower())
    return names


def _is_inspect_tool_round(item: dict[str, Any]) -> bool:
    names = _tool_call_names(item)
    if not names:
        return False
    return all(name in _INSPECT_TOOLS for name in names)


def _cap_tool_rounds(messages: list[dict[str, Any]], limit: int = MAX_CODE_TOOL_ROUNDS) -> list:
    starts = [
        index
        for index, item in enumerate(messages)
        if item.get("role") == "assistant" and item.get("tool_calls")
    ]
    extra = len(starts) - limit
    if extra <= 0:
        return messages
    inspect_starts = [index for index in starts if _is_inspect_tool_round(messages[index])]
    mutate_starts = [index for index in starts if index not in inspect_starts]
    drop_starts = inspect_starts[:extra]
    still = extra - len(drop_starts)
    if still > 0:
        drop_starts = drop_starts + mutate_starts[:still]
    drop: set[int] = set()
    for start in drop_starts:
        drop.add(start)
        index = start + 1
        while index < len(messages) and messages[index].get("role") == "tool":
            drop.add(index)
            index += 1
    out = []
    for index, item in enumerate(messages):
        if index not in drop:
            out.append(item)
            continue
        if item.get("role") == "assistant" and item.get("content"):
            kept = {key: value for key, value in item.items() if key != "tool_calls"}
            out.append(kept)
    return out


def sanitize_chat_payload(body: dict[str, Any], *, keep_tools: bool = False) -> dict[str, Any]:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages is required")
    allowed = ("system", "user", "assistant", "tool") if keep_tools else ("system", "user", "assistant")
    clean_messages = []
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "user")
        if role not in allowed:
            continue
        content = raw.get("content")
        if isinstance(content, list):
            texts = []
            images = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    image = part.get("image_url")
                    url = ""
                    if isinstance(image, dict):
                        url = str(image.get("url") or "")
                    elif isinstance(image, str):
                        url = image
                    if url.startswith("data:image") and 32 < len(url) < 12_000_000:
                        images.append({"type": "image_url", "image_url": {"url": url}})
                elif isinstance(part, dict) and part.get("type") == "text":
                    texts.append(str(part.get("text") or ""))
                elif isinstance(part, str):
                    texts.append(part)
            text = "\n".join(texts)
            if images:
                parts: list[dict[str, Any]] = []
                if text:
                    parts.append({"type": "text", "text": text})
                parts.extend(images)
                content = parts
            else:
                content = text
        if content is None:
            content = ""
        if not isinstance(content, list):
            content = str(content)
        item = {"role": role, "content": content}
        if keep_tools and role == "assistant" and raw.get("tool_calls"):
            item["tool_calls"] = raw["tool_calls"]
        if keep_tools and role == "tool":
            if raw.get("tool_call_id"):
                item["tool_call_id"] = str(raw.get("tool_call_id") or "")
            if raw.get("name"):
                item["name"] = str(raw.get("name") or "")
        clean_messages.append(item)
    if not any(item["role"] == "system" for item in clean_messages):
        clean_messages.insert(0, {"role": "system", "content": CONSOLE_SYSTEM})
    payload = {
        "messages": clean_messages,
        "stream": bool(body.get("stream", True)),
    }
    conv = str(body.get("conversation_id") or "").strip()[:80]
    if conv:
        payload["conversation_id"] = conv
    for key in (
        "temperature",
        "top_p",
        "min_p",
        "frequency_penalty",
        "presence_penalty",
        "max_tokens",
    ):
        if body.get(key) is not None:
            payload[key] = body[key]
    return payload


def sanitize_code_payload(body: dict[str, Any], username: str = "") -> dict[str, Any]:
    """Chat sanitizer, but force the Code-mode system prompt and keep chat_id."""
    from ui.code_agent import (
        attach_build_user_contract,
        attach_layout_fix_contract,
        attach_plan_user_contract,
        attach_readonly_mode_hint,
        code_system_for,
        code_tool_specs,
        normalize_agent,
        workspace_file_brief,
    )
    from ui.workspace import safe_name

    payload = sanitize_chat_payload(body, keep_tools=True)
    raw_id = str(body.get("chat_id") or "").strip()
    if not raw_id:
        raise ValueError("chat_id is required in Code mode")
    chat_id = safe_name(raw_id)
    agent = normalize_agent(body.get("agent"))
    messages = [item for item in payload["messages"] if item.get("role") != "system"]
    messages = _cap_tool_rounds(messages)
    messages.insert(0, {"role": "system", "content": code_system_for(username, chat_id, agent)})
    if agent == "plan":
        attach_plan_user_contract(messages)
    elif agent != "ask":
        attach_build_user_contract(messages)
        attach_layout_fix_contract(messages, username, chat_id)
    if agent in ("ask", "plan"):
        attach_readonly_mode_hint(messages, agent)
    payload["messages"] = messages
    payload["chat_id"] = chat_id
    payload["mode"] = "code"
    payload["agent"] = agent
    payload["tools"] = code_tool_specs(agent)
    if agent == "plan" and "empty project" in workspace_file_brief(username, chat_id):
        payload["tool_choice"] = "none"
    return payload
