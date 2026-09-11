"""In-process GPU handoff for image generation.

Clients talk to this API over HTTP. Never call switch_model.py or
/v1/gpu/mode from inside a request handler — that deadlocks on this
same server. Unload, start Comfy, generate, then optionally reload.

Completion means the PNG files exist on this host. Chat holds the HTTP
request until then; do not invent wait/curl tool calls mid-batch.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import uuid4

from common import model
from common.gpu_mode import (
    JOBS_PERSIST_NAME,
    comfy_up,
    generate_image,
    save_generated_image,
    start_comfy_if_needed,
    stop_comfy,
    wait_gpu_vram_drain,
    write_mode,
)
from common.logger import xlogger
from common.vram_recover import is_vram_error, reset_cuda_memory
from common.tabby_config import config
from endpoints.core.types.model import ModelLoadRequest
from endpoints.core.utils.model import stream_model_load
from common.load_fields import LOAD_FIELDS
from select_model import apply_profile, available_profiles, last_profile

# Chat holds the HTTP request, so Comfy can start immediately.
MCP_HANDOFF_DELAY_S = 0.0
MCP_MAX_BATCH = 12
MCP_POLL_WAIT_S = 20
MCP_POLL_WAIT_MAX_S = 45
_MCP_JOBS: dict[str, "McpImageJob"] = {}
_MCP_ORDER: list[str] = []
_MCP_TASK: Optional[asyncio.Task] = None
_MCP_JOB_ID: Optional[str] = None
_GENERATE_LOCK: Optional[asyncio.Lock] = None
_GENERATE_LOCK_LOOP: Optional[asyncio.AbstractEventLoop] = None
_PERSIST_LOADED = False
RESTART_ABANDON_REASON = "TabbyAPI restarted before this job finished."
CODING_ABANDON_REASON = (
    "Left unused while another chat started writing."
)
STALE_ABANDON_REASON = (
    "Left unused after the API restarted. Ask again if you still want that picture."
)
STALE_JOB_S = 20 * 60


@dataclass
class McpImageItem:
    prompt: str
    output_path: str
    size: str = "1024x1024"
    count: int = 1
    seed: Optional[int] = None
    source_image: str = ""
    denoise: Optional[float] = None
    status: str = "queued"
    urls: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class McpImageJob:
    id: str
    items: list[McpImageItem]
    restore: bool
    api_base: str
    wait_text: str
    wait_s: int
    status: str = "queued"
    phase: str = "queued"
    restore_name: Optional[str] = None
    urls: list[str] = field(default_factory=list)
    error: str = ""
    started_at: float = field(default_factory=time.time)
    progress_seq: int = 0
    current_index: int = 0
    client_saved: bool = False
    download_attempts: int = 0
    pillow_redownload: bool = False
    dead_requeued: bool = False
    is_requeue: bool = False
    download_stopped: bool = False
    code_turns: int = 0
    owner: str = ""
    chat_id: str = ""
    workspace_copied: bool = False
    workspace_files: list[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    progress: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def prompt(self) -> str:
        if not self.items:
            return ""
        if len(self.items) == 1:
            return self.items[0].prompt
        return f"{len(self.items)} images"

    @property
    def output_path(self) -> str:
        if len(self.items) == 1:
            return self.items[0].output_path
        return "images/"

    @property
    def count(self) -> int:
        return sum(max(1, int(item.count or 1)) for item in self.items)

    @property
    def done_count(self) -> int:
        return sum(1 for item in self.items if item.status == "done")

    @property
    def accepting(self) -> bool:
        return self.status in ("queued", "running") and self.phase not in (
            "restoring_llm",
            "done",
            "error",
        )


def loaded_tabby_name() -> Optional[str]:
    try:
        from sidecar.settings import is_sidecar_process
        from sidecar.model_status import loaded_model_id

        if is_sidecar_process():
            return loaded_model_id()
    except Exception:
        pass
    if model.container and getattr(model.container, "model_dir", None):
        if getattr(model.container, "loaded", False):
            return model.container.model_dir.name
    return None


def restore_llm_profile() -> Optional[str]:
    """Profile actually in VRAM, else last.json. Never 'comfy'."""
    from common.phrase_switch import GPU_ALIASES, profile_alias_for_model

    alias = profile_alias_for_model(loaded_tabby_name())
    if alias:
        return alias
    name = last_profile()
    if name and str(name).lower() not in GPU_ALIASES and str(name).lower() != "comfy":
        return name
    return None


def _is_load_error(event) -> bool:
    if not isinstance(event, str):
        return False
    try:
        payload = json.loads(event)
    except ValueError:
        return False
    return isinstance(payload, dict) and bool(payload.get("error"))


async def ensure_comfy() -> None:
    """Unload any LLM and make sure ComfyUI owns the GPU."""
    from common.phrase_switch import clear_switch_lock, set_switch_lock
    from sidecar.settings import is_sidecar_process

    if is_sidecar_process():
        from sidecar.model_status import unload_backend

        if comfy_up() and not loaded_tabby_name():
            write_mode("comfy")
            return
        set_switch_lock("comfy")
        started = time.time()
        was_up = comfy_up()
        try:
            await asyncio.to_thread(unload_backend)
            write_mode("comfy")
            await asyncio.to_thread(start_comfy_if_needed)
            if not comfy_up():
                raise RuntimeError("ComfyUI did not start")
        finally:
            clear_switch_lock()
        if not was_up:
            from common.switch_times import record_ready

            record_ready("comfy", time.time() - started)
        return

    if comfy_up() and not loaded_tabby_name():
        write_mode("comfy")
        return

    set_switch_lock("comfy")
    started = time.time()
    was_up = comfy_up()
    try:
        if loaded_tabby_name():
            await model.unload_model(skip_wait=True)
        write_mode("comfy")
        await asyncio.to_thread(start_comfy_if_needed)
        if not comfy_up():
            raise RuntimeError("ComfyUI did not start")
    finally:
        clear_switch_lock()
    if not was_up:
        from common.switch_times import record_ready

        record_ready("comfy", time.time() - started)


async def _load_profile(profile_name: str) -> None:
    try:
        profile = apply_profile(profile_name)
    except SystemExit as exc:
        raise RuntimeError(str(exc)) from exc

    model_cfg = profile.get("model") or {}
    model_name = model_cfg.get("model_name")
    if not model_name:
        raise RuntimeError(f"Profile {profile_name} has no model_name")

    tabby_root = Path(__file__).resolve().parent.parent
    model_dir = Path(config.model.model_dir)
    if not model_dir.is_absolute():
        model_dir = tabby_root / model_dir
    model_path = model_dir / model_name
    if not model_path.exists():
        raise RuntimeError(f"Model folder missing: {model_path}")

    load_data = ModelLoadRequest(model_name=model_name)
    for key in LOAD_FIELDS:
        if key in model_cfg and model_cfg[key] is not None:
            setattr(load_data, key, model_cfg[key])
    sidecar = False
    try:
        from sidecar.settings import is_sidecar_process

        sidecar = is_sidecar_process()
    except Exception:
        sidecar = False
    if sidecar:
        from sidecar.model_status import load_backend_model

        await asyncio.to_thread(
            load_backend_model, load_data.model_dump(exclude_none=True)
        )
        return
    async for event in stream_model_load(load_data, model_path):
        if _is_load_error(event):
            raise RuntimeError(event)


async def wait_out_generating_image_jobs(*, from_job: bool = False, interval: float = 0.5) -> None:
    """Block until no Comfy batch still owns the GPU.

    The image worker passes from_job=True so its own restore is not waiting
    on itself.
    """
    if from_job:
        return
    while True:
        job = active_mcp_image_job()
        if not job or job.status not in ("queued", "running"):
            return
        await asyncio.sleep(interval)


async def _unload_tabby_leftovers() -> None:
    """Drop a half-loaded container even when `loaded` is still false."""
    if getattr(model, "container", None):
        try:
            await model.unload_model(skip_wait=True)
        except Exception:
            pass
    reset_cuda_memory()


def _bounce_after_vram_fail(profile_name: str) -> None:
    """Fresh CUDA context via systemd, keeping config.yml on the intended LLM.

    Do not load the 9B daily profile here — that is what mixed restore was
    doing after Comfy, so qwen36/qwen35 came back as Qwen3.5-9B.
    """
    write_mode("llm", profile=profile_name)
    from common.phrase_switch import start_restart

    if start_restart(abandon=False):
        xlogger.warning(
            f"VRAM still short for {profile_name}; bouncing TabbyAPI to reload it"
        )
        return
    raise RuntimeError(
        f"Insufficient VRAM reloading {profile_name} and could not bounce the API"
    )


async def reload_last_llm(name: Optional[str] = None, *, from_job: bool = False) -> str:
    """Free Comfy and load a TabbyAPI profile. Returns the profile alias."""
    await wait_out_generating_image_jobs(from_job=from_job)
    names = available_profiles()
    chosen = name or restore_llm_profile()
    if chosen in names:
        profile_name = chosen
    elif last_profile() in names:
        profile_name = last_profile()
    else:
        profile_name = names[0] if names else None
    if not profile_name:
        raise RuntimeError("No TabbyAPI profile is installed")

    from common.phrase_switch import clear_switch_lock, set_switch_lock

    # Lock before stop_comfy so /status reports switching during the wait
    # (systemd stop can take tens of seconds). The UI loading banner keys off it.
    set_switch_lock(profile_name)
    bounce = False
    started = time.time()
    from_comfy = comfy_up()
    try:
        await asyncio.to_thread(stop_comfy)
        await asyncio.to_thread(wait_gpu_vram_drain)
        reset_cuda_memory()
        load_exc: Optional[BaseException] = None
        try:
            await _load_profile(profile_name)
        except RuntimeError as exc:
            if not is_vram_error(exc):
                raise
            load_exc = exc

        if load_exc is not None:
            # A retry in this process never wins: after a Comfy batch the
            # allocator and CUDA context here still hold 1-1.8 GiB that
            # empty_cache cannot hand back, and the tight 12 GiB profiles need
            # exactly that margin. Only a fresh process frees it, so bounce
            # instead of burning another full load attempt.
            xlogger.warning(
                "LLM load hit leftover VRAM; bouncing for a clean CUDA context"
            )
            await asyncio.to_thread(stop_comfy)
            # Keep last.json / config.yml on the intended profile. A 9B
            # fallback is the wrong model, not a recovery.
            write_mode("llm", profile=profile_name)
            await _unload_tabby_leftovers()
            if from_job:
                raise RuntimeError(
                    f"Insufficient VRAM reloading {profile_name}"
                ) from load_exc
            bounce = True

        if not bounce:
            write_mode("llm", profile=profile_name)
    finally:
        clear_switch_lock()
    if bounce:
        _bounce_after_vram_fail(profile_name)
        return profile_name
    # Clean warm load: teach the screensaver / chat "typical" what this box
    # really does. A bounce is recovery, not a switch time.
    from common.switch_times import record_ready

    elapsed = time.time() - started
    record_ready(profile_name, elapsed)
    if from_comfy:
        record_ready("llm", elapsed)
    return profile_name


def _generate_lock() -> asyncio.Lock:
    global _GENERATE_LOCK, _GENERATE_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _GENERATE_LOCK is None or _GENERATE_LOCK_LOOP is not loop:
        _GENERATE_LOCK = asyncio.Lock()
        _GENERATE_LOCK_LOOP = loop
    return _GENERATE_LOCK


def _signal(job: McpImageJob) -> None:
    job.progress_seq += 1
    job.progress.set()
    _persist_jobs()


def _persist_path() -> Path:
    """JSON of the recent job queue, next to the PNGs it points at.

    Living beside the gallery (not model_profiles/) keeps job state and the
    images it references on the same disk, so a copy/backup of one carries
    the other.
    """
    from common import gpu_mode

    return gpu_mode.GENERATED_DIR / JOBS_PERSIST_NAME


def _item_to_persist(item: McpImageItem) -> dict:
    data = {
        "prompt": item.prompt,
        "output_path": item.output_path,
        "size": item.size,
        "count": item.count,
        "seed": item.seed,
        "status": item.status,
        "urls": list(item.urls),
        "error": item.error,
    }
    if item.source_image:
        data["source_image"] = item.source_image
    if item.denoise is not None:
        data["denoise"] = item.denoise
    return data


def _item_from_persist(data: dict) -> McpImageItem:
    seed = data.get("seed")
    try:
        seed = int(seed) if seed is not None else None
    except (TypeError, ValueError):
        seed = None
    return McpImageItem(
        prompt=str(data.get("prompt") or ""),
        output_path=str(data.get("output_path") or "images/generated.png"),
        size=str(data.get("size") or "1024x1024"),
        count=max(1, int(data.get("count") or 1)),
        seed=seed,
        source_image=_item_source_path(data),
        denoise=_item_denoise(data),
        status=str(data.get("status") or "queued"),
        urls=[str(u) for u in (data.get("urls") or []) if u],
        error=str(data.get("error") or ""),
    )


def _job_to_persist(job: McpImageJob) -> dict:
    return {
        "id": job.id,
        "items": [_item_to_persist(item) for item in job.items],
        "restore": job.restore,
        "restore_name": job.restore_name,
        "api_base": job.api_base,
        "wait_text": job.wait_text,
        "wait_s": job.wait_s,
        "status": job.status,
        "phase": job.phase,
        "urls": list(job.urls),
        "error": job.error,
        "started_at": job.started_at,
        "current_index": job.current_index,
        "client_saved": bool(job.client_saved),
        "code_turns": int(job.code_turns or 0),
        "owner": str(job.owner or ""),
        "chat_id": str(job.chat_id or ""),
    }


def _job_from_persist(data: dict) -> Optional[McpImageJob]:
    job_id = str(data.get("id") or "").strip()
    raw_items = data.get("items")
    if not job_id or not isinstance(raw_items, list) or not raw_items:
        return None
    items = [_item_from_persist(raw) for raw in raw_items if isinstance(raw, dict)]
    if not items:
        return None
    try:
        started_at = float(data.get("started_at") or 0)
    except (TypeError, ValueError):
        started_at = 0.0
    job = McpImageJob(
        id=job_id,
        items=items,
        restore=bool(data.get("restore")),
        restore_name=str(data.get("restore_name") or "") or None,
        api_base=str(data.get("api_base") or ""),
        wait_text=str(data.get("wait_text") or ""),
        wait_s=int(data.get("wait_s") or 0),
        status=str(data.get("status") or "error"),
        phase=str(data.get("phase") or "error"),
        urls=[str(u) for u in (data.get("urls") or []) if u],
        error=str(data.get("error") or ""),
        started_at=started_at,
        current_index=int(data.get("current_index") or 0),
        client_saved=bool(data.get("client_saved")),
        code_turns=int(data.get("code_turns") or 0),
        owner=str(data.get("owner") or ""),
        chat_id=str(data.get("chat_id") or ""),
    )
    return _maybe_revive_restarted_job(job)


def _persist_jobs() -> None:
    """Best-effort snapshot so a restart does not lose finished renders."""
    try:
        path = _persist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            _job_to_persist(_MCP_JOBS[job_id])
            for job_id in _MCP_ORDER
            if job_id in _MCP_JOBS
        ]
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _load_persisted_jobs() -> None:
    global _PERSIST_LOADED
    if _PERSIST_LOADED:
        return
    _PERSIST_LOADED = True
    try:
        path = _persist_path()
        if not path.exists():
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, list):
        return
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        job = _job_from_persist(entry)
        if job and job.id not in _MCP_JOBS:
            _MCP_JOBS[job.id] = job
            _MCP_ORDER.append(job.id)
    _drop_dead_jobs()
    _persist_jobs()


def _worker_is_alive() -> bool:
    return _MCP_TASK is not None and not _MCP_TASK.done()


def _mark_job_abandoned(job: McpImageJob, reason: str) -> None:
    job.status = "error"
    job.phase = "error"
    job.client_saved = False
    if not job.error:
        job.error = reason
    for item in job.items:
        if item.status in ("queued", "running"):
            item.status = "error"
            if not item.error:
                item.error = reason


def abandon_foreign_coding_job(
    job: Optional[McpImageJob],
    *,
    owner: str = "",
    chat_id: str = "",
    job_id: str = "",
) -> bool:
    """Drop a leftover coding job this conversation cannot resume.

    Resume is ``tabby-image-job`` in history, or the same owner+chat
    workspace. A ghost coding job must not block a new landing-page turn.
    """
    if job is None or str(getattr(job, "status", "") or "") != "coding":
        return False
    if job_id and str(getattr(job, "id", "") or "") == job_id:
        return False
    job_owner = str(getattr(job, "owner", "") or "").strip()
    job_chat = str(getattr(job, "chat_id", "") or "").strip()
    owner_name = str(owner or "").strip()
    chat_name = str(chat_id or "").strip()
    if (
        job_owner
        and job_chat
        and owner_name
        and chat_name
        and job_owner == owner_name
        and job_chat == chat_name
    ):
        return False
    _mark_job_abandoned(job, CODING_ABANDON_REASON)
    _persist_jobs()
    return True


def bind_job_workspace(job: Optional[McpImageJob], owner: str = "", chat_id: str = "") -> bool:
    """Attach a leftover coding job to the Code workspace that owns this turn."""
    if job is None:
        return False
    owner_name = str(owner or "").strip()
    chat_name = str(chat_id or "").strip()
    changed = False
    if owner_name and not str(getattr(job, "owner", "") or "").strip():
        job.owner = owner_name
        changed = True
    if chat_name and not str(getattr(job, "chat_id", "") or "").strip():
        job.chat_id = chat_name
        changed = True
    if changed:
        _persist_jobs()
        _signal(job)
    return changed


def _is_restart_abandon(job: McpImageJob) -> bool:
    return (job.error or "").startswith(RESTART_ABANDON_REASON)


def _requeue_unfinished_items(job: McpImageJob) -> None:
    """Reset unfinished items so a new worker can finish the batch."""
    for item in job.items:
        if item.status == "done" and item.urls:
            continue
        item.status = "queued"
        item.error = ""
    job.status = "queued"
    job.phase = "queued"
    job.error = ""
    job.client_saved = False


def job_is_stale(job, *, now: Optional[float] = None) -> bool:
    """True when a leftover job is too old to steal the GPU on the next boot."""
    started = float(getattr(job, "started_at", 0) or 0)
    if started <= 0:
        return True
    return (time.time() if now is None else now) - started > STALE_JOB_S


def abandon_stale_job(
    job: Optional[McpImageJob],
    reason: str = STALE_ABANDON_REASON,
) -> bool:
    """Drop a leftover queued/running job so a new LLM turn can keep the GPU."""
    global _MCP_TASK, _MCP_JOB_ID
    if job is None or not job_is_stale(job):
        return False
    if str(getattr(job, "status", "") or "") not in ("queued", "running", "coding"):
        return False
    _mark_job_abandoned(job, reason)
    if _MCP_JOB_ID == job.id:
        task = _MCP_TASK
        _MCP_TASK = None
        _MCP_JOB_ID = None
        if task is not None and not task.done():
            task.cancel()
    _persist_jobs()
    return True


def _maybe_revive_restarted_job(job: McpImageJob) -> McpImageJob:
    """Keep unfinished Comfy work after an API bounce; honor explicit cancels.

    A coding job cannot resume the LLM write after this process died. Leave
    it abandoned so the next Code message is a new turn, not a ghost generate.
    Stale leftovers stay abandoned so switching to Gemma does not start Comfy.
    """
    if job.status == "done":
        return job
    if job.status == "coding":
        _mark_job_abandoned(job, RESTART_ABANDON_REASON)
        return job
    if job_is_stale(job) and job.status in ("queued", "running"):
        _mark_job_abandoned(job, STALE_ABANDON_REASON)
        return job
    if job.status == "error":
        if not _is_restart_abandon(job):
            for item in job.items:
                if item.status in ("queued", "running"):
                    item.status = "error"
                    if not item.error:
                        item.error = job.error or RESTART_ABANDON_REASON
            return job
        if all(item.status == "done" and item.urls for item in job.items):
            job.status = "done"
            job.phase = "done"
            job.error = ""
            return job
        if job_is_stale(job):
            _mark_job_abandoned(job, STALE_ABANDON_REASON)
            return job
        _requeue_unfinished_items(job)
        return job
    if job_is_stale(job):
        _mark_job_abandoned(job, STALE_ABANDON_REASON)
        return job
    _requeue_unfinished_items(job)
    return job


def _drop_dead_jobs() -> None:
    """A queued/running job with no worker is leftover from a crash or reboot.

    Requeue unfinished items. Startup resumes the worker; leaving the id as
    error dropped rasters Comfy had already finished.
    """
    live_id = _MCP_JOB_ID if _worker_is_alive() else None
    changed = False
    for job in _MCP_JOBS.values():
        if job.status not in ("queued", "running"):
            continue
        if live_id and job.id == live_id:
            continue
        if job_is_stale(job):
            _mark_job_abandoned(job, STALE_ABANDON_REASON)
            changed = True
            continue
        _requeue_unfinished_items(job)
        changed = True
    if changed:
        _persist_jobs()


def _persist_job_reply(job: McpImageJob) -> None:
    """Write the finished reply so a dropped Code hold can unlock."""
    owner = str(getattr(job, "owner", "") or "").strip()
    chat_id = str(getattr(job, "chat_id", "") or "").strip()
    if not owner or not chat_id:
        return
    urls = [str(url) for url in (job.urls or []) if url]
    mark = f"tabby-image-job: {job.id}"
    if job.status == "done" and urls:
        n = len(urls)
        lead = "Here's the picture." if n == 1 else f"Here are the {n} pictures."
        lines = [mark, "", lead, ""]
        lines.extend(f"![]({url})" for url in urls)
        text = "\n".join(lines)
    elif job.status == "error":
        text = f"{mark}\nError: {job.error or 'Image generation failed.'}"
    else:
        return
    try:
        from ui.chats import append_flight_assistant, load_store

        store = load_store(owner)
        for chat in store.get("chats") or []:
            if str(chat.get("id") or "") != chat_id:
                continue
            messages = chat.get("messages") or []
            last = messages[-1] if messages else None
            content = str((last or {}).get("content") or "")
            if (
                isinstance(last, dict)
                and last.get("role") == "assistant"
                and job.id in content
                and ("/v1/images/generated-" in content or content.lstrip().startswith("Error:"))
            ):
                return
            break
        append_flight_assistant(owner, chat_id, content=text)
    except Exception as exc:
        xlogger.warning(f"Could not persist image reply for job {job.id}: {exc}")


def abandon_inflight_jobs(reason: str = RESTART_ABANDON_REASON) -> int:
    """Mark every queued/running job as error and cancel the render worker.

    Call this before an explicit systemd bounce (chat ``restart``) and on
    process shutdown. Unexpected kills resume leftover items on the next boot.
    """
    global _MCP_TASK, _MCP_JOB_ID
    _load_persisted_jobs()
    count = 0
    for job in _MCP_JOBS.values():
        if job.status not in ("queued", "running"):
            continue
        _mark_job_abandoned(job, reason)
        count += 1
    task = _MCP_TASK
    _MCP_TASK = None
    _MCP_JOB_ID = None
    if count:
        _persist_jobs()
    if task is not None and not task.done():
        task.cancel()
    return count


async def resume_persisted_jobs() -> int:
    """Finish Comfy batches that were still open when this process started."""
    _load_persisted_jobs()
    _drop_dead_jobs()
    ready = 0
    first: Optional[McpImageJob] = None
    for job_id in _MCP_ORDER:
        job = _MCP_JOBS.get(job_id)
        if not job:
            continue
        if job.status == "done":
            copy_job_to_workspace(job)
            _persist_job_reply(job)
            continue
        if job.status not in ("queued", "running"):
            continue
        if job_is_stale(job):
            _mark_job_abandoned(job, STALE_ABANDON_REASON)
            continue
        ready += 1
        if first is None:
            first = job
    if ready == 0:
        _persist_jobs()
    if first is not None and not _worker_is_alive():
        await launch_mcp_image_job(first)
    return ready


def refresh_job_wait(job: McpImageJob) -> None:
    from common.phrase_switch import image_job_wait_seconds, image_job_wait_text

    prompts = [item.prompt for item in job.items for _ in range(max(1, item.count))]
    job.wait_text = image_job_wait_text(prompts=prompts, restore=job.restore)
    job.wait_s = image_job_wait_seconds(prompts=prompts, restore=job.restore)


def _normalize_image_specs(
    prompt: str = "",
    *,
    size: str = "1024x1024",
    seed: Optional[int] = None,
    count: int = 1,
    source_image: Optional[Path] = None,
    items: Optional[list[dict]] = None,
) -> list[dict]:
    if items:
        specs = []
        for raw in items:
            specs.append(
                {
                    "prompt": str(raw.get("prompt") or prompt or "").strip(),
                    "size": str(raw.get("size") or size or "1024x1024"),
                    "seed": raw.get("seed", seed),
                    "count": max(1, min(int(raw.get("count") or raw.get("n") or 1), 4)),
                    "source_image": raw.get("source_image", source_image),
                }
            )
        specs = [spec for spec in specs if spec["prompt"]]
        if not specs:
            raise ValueError("prompt is required")
        return specs
    if not str(prompt or "").strip():
        raise ValueError("prompt is required")
    return [
        {
            "prompt": prompt,
            "size": size,
            "seed": seed,
            "count": max(1, min(int(count), 4)),
            "source_image": source_image,
        }
    ]


async def _render_specs(
    specs: list[dict],
    *,
    timeout: float = 300,
    owner: str | None = None,
) -> list[Path]:
    saved: list[Path] = []
    for spec in specs:
        n = max(1, min(int(spec.get("count") or 1), 4))
        spec_seed = spec.get("seed")
        base_seed = spec_seed if spec_seed is not None else random.randint(0, 2**31 - 1)
        spec_source = spec.get("source_image")
        if spec_source:
            spec_source = Path(spec_source)
        denoise = spec.get("denoise")
        for index in range(n):
            raw = await asyncio.to_thread(
                generate_image,
                spec["prompt"],
                spec.get("size") or "1024x1024",
                base_seed + index,
                timeout,
                spec_source if index == 0 else None,
                denoise,
            )
            saved.append(save_generated_image(raw, owner=owner))
    return saved


async def generate_images_job(
    prompt: str = "",
    *,
    size: str = "1024x1024",
    seed: Optional[int] = None,
    count: int = 1,
    source_image: Optional[Path] = None,
    restore: bool = False,
    timeout: float = 300,
    items: Optional[list[dict]] = None,
) -> list[Path]:
    """Generate one or more PNGs. Optionally reload the last LLM afterwards.

    `items` is a list of {prompt, size, seed, count} for different prompts in
    one Comfy session. Without it, `prompt` is repeated `count` times.
    """
    specs = _normalize_image_specs(
        prompt,
        size=size,
        seed=seed,
        count=count,
        source_image=source_image,
        items=items,
    )
    async with _generate_lock():
        restore_name = restore_llm_profile() if restore else None
        was_llm = bool(loaded_tabby_name())
        await ensure_comfy()
        try:
            return await _render_specs(specs, timeout=timeout)
        finally:
            if restore and (was_llm or restore_name):
                await reload_last_llm(restore_name)


def active_mcp_image_job() -> Optional[McpImageJob]:
    _load_persisted_jobs()
    _drop_dead_jobs()
    for job_id in reversed(_MCP_ORDER):
        job = _MCP_JOBS.get(job_id)
        if job and job.status in ("queued", "running", "coding"):
            return job
    return None


def get_mcp_image_job(job_id: Optional[str] = None) -> Optional[McpImageJob]:
    _load_persisted_jobs()
    _drop_dead_jobs()
    if job_id:
        return _MCP_JOBS.get(job_id)
    if not _MCP_ORDER:
        return None
    return _MCP_JOBS.get(_MCP_ORDER[-1])


def recent_mcp_image_jobs() -> list[McpImageJob]:
    """Newest MCP/Comfy jobs first (in-process queue + persisted)."""
    _load_persisted_jobs()
    _drop_dead_jobs()
    return [_MCP_JOBS[job_id] for job_id in reversed(_MCP_ORDER) if job_id in _MCP_JOBS]


def mcp_job_to_dict(job: McpImageJob) -> dict:
    """JSON for GET /v1/images/jobs and the local stdio saver."""
    return {
        "id": job.id,
        "status": job.status,
        "phase": job.phase,
        "prompt": job.prompt,
        "output_path": job.output_path,
        "wait_s": int(job.wait_s),
        "wait_text": job.wait_text,
        "error": job.error,
        "done_count": int(job.done_count),
        "count": int(job.count),
        "urls": list(job.urls),
        "client_saved": bool(job.client_saved),
        "items": [
            {
                "prompt": item.prompt,
                "output_path": item.output_path,
                "status": item.status,
                "urls": list(item.urls),
                "error": item.error,
            }
            for item in job.items
        ],
    }


def _remember_mcp_job(job: McpImageJob) -> None:
    _MCP_JOBS[job.id] = job
    _MCP_ORDER.append(job.id)
    while len(_MCP_ORDER) > 8:
        old = _MCP_ORDER.pop(0)
        if old != job.id:
            _MCP_JOBS.pop(old, None)


def _item_source_path(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, dict):
        raw = raw.get("source_image")
    if not raw:
        return ""
    return str(Path(raw))


def _item_denoise(raw) -> Optional[float]:
    if not isinstance(raw, dict):
        return None
    value = raw.get("denoise")
    if value is None or value == "":
        return None
    try:
        strength = float(value)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= strength <= 1.0:
        return None
    return strength


def _new_items(
    *,
    prompt: str = "",
    output_path: str = "",
    size: str = "1024x1024",
    count: int = 1,
    seed: Optional[int] = None,
    items: Optional[list[dict]] = None,
) -> list[McpImageItem]:
    parsed: list[McpImageItem] = []
    if items:
        for raw in items:
            text = str(raw.get("prompt") or "").strip()
            if not text:
                continue
            from common.image_prompts import resolved_image_size
            from images.paths import safe_rel_png_path

            rel = safe_rel_png_path(str(raw.get("output_path") or "").strip())
            parsed.append(
                McpImageItem(
                    prompt=text,
                    output_path=rel,
                    size=resolved_image_size(
                        prompt=text,
                        filename=rel,
                        size=raw.get("size"),
                        fallback=size or "1024x1024",
                    ),
                    count=max(1, min(int(raw.get("count") or raw.get("n") or 1), 4)),
                    seed=raw.get("seed", seed),
                    source_image=_item_source_path(raw),
                    denoise=_item_denoise(raw),
                )
            )
    if prompt.strip():
        from common.image_prompts import resolved_image_size
        from images.paths import safe_rel_png_path

        rel = safe_rel_png_path(output_path)
        explicit = size if size and str(size) != "1024x1024" else None
        parsed.insert(
            0,
            McpImageItem(
                prompt=prompt.strip(),
                output_path=rel,
                size=resolved_image_size(
                    prompt=prompt.strip(),
                    filename=rel,
                    size=explicit,
                    fallback=size or "1024x1024",
                ),
                count=max(1, min(int(count), 4)),
                seed=seed,
            ),
        )
    from common.image_prompts import rewrite_comfy_prompt
    from images.paths import resolve_output_paths

    resolve_output_paths(parsed)
    for item in parsed:
        item.prompt = rewrite_comfy_prompt(item.prompt)
    from common.gen_logging import log_image_translator

    log_image_translator("generate", parsed, source="comfy handoff")
    return parsed


async def start_mcp_image_job(
    *,
    prompt: str = "",
    output_path: str = "",
    size: str = "1024x1024",
    count: int = 1,
    seed: Optional[int],
    restore: bool,
    api_base: str,
    wait_text: str = "",
    wait_s: int = 0,
    delay: Optional[float] = None,
    items: Optional[list[dict]] = None,
    start: bool = True,
    owner: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> tuple[McpImageJob, str]:
    """Queue a Comfy job that survives the MCP HTTP client disconnecting.

    Extra generate_image calls while a batch is queued or generating are
    appended so Comfy stays up and the LLM reloads once at the end.
    Append only when both sides have a non-empty owner and they match.
    Empty-identity MCP calls do not batch into each other.
    Returns (job, "started"|"appended"|"busy"|"coding").

    `start=False` remembers dests in a coding-phase job and does not
    unload the LLM. Call launch_mcp_image_job when the page is written.
    """
    new_items = _new_items(
        prompt=prompt,
        output_path=output_path,
        size=size,
        count=count,
        seed=seed,
        items=items,
    )
    if not new_items:
        raise ValueError("prompt is required")

    owner_name = str(owner or "").strip()
    chat_name = str(chat_id or "").strip()
    busy = active_mcp_image_job()
    if busy and abandon_foreign_coding_job(
        busy, owner=owner_name, chat_id=chat_name
    ):
        busy = None
    if busy:
        busy_owner = str(busy.owner or "").strip()
        busy_chat = str(busy.chat_id or "").strip()
        if not busy_owner or not owner_name or busy_owner != owner_name:
            return busy, "busy"
        if busy_chat and chat_name and busy_chat != chat_name:
            return busy, "busy"
        if chat_name and not busy_chat:
            busy.chat_id = chat_name
        async with busy.lock:
            if busy.accepting and busy.count + sum(
                max(1, item.count) for item in new_items
            ) <= MCP_MAX_BATCH:
                from images.paths import resolve_output_paths

                resolve_output_paths(
                    new_items,
                    reserved=[item.output_path for item in busy.items],
                )
                busy.items.extend(new_items)
                if restore:
                    busy.restore = True
                    if not busy.restore_name:
                        busy.restore_name = restore_llm_profile()
                refresh_job_wait(busy)
                _signal(busy)
                return busy, "appended"
        return busy, "busy"

    job = McpImageJob(
        id=str(uuid4()),
        items=new_items,
        restore=restore,
        restore_name=restore_llm_profile() if restore else None,
        api_base=api_base,
        wait_text=wait_text,
        wait_s=wait_s,
        started_at=time.time(),
        owner=owner_name,
        chat_id=chat_name,
    )
    refresh_job_wait(job)
    _remember_mcp_job(job)
    if not start:
        job.status = "coding"
        job.phase = "writing_code"
        job.code_turns = 1
        _signal(job)
        return job, "coding"
    await launch_mcp_image_job(job, delay=delay)
    return job, "started"


async def launch_mcp_image_job(
    job: McpImageJob, delay: Optional[float] = None
) -> McpImageJob:
    """Hand a remembered coding job to Comfy. No-op if already rendering."""
    global _MCP_TASK, _MCP_JOB_ID
    if job.status in ("queued", "running") and _MCP_JOB_ID == job.id and _worker_is_alive():
        return job
    if delay is None:
        delay = MCP_HANDOFF_DELAY_S if loaded_tabby_name() else 0.0
    job.status = "queued"
    job.phase = "queued"
    refresh_job_wait(job)
    _signal(job)
    loop = asyncio.get_running_loop()
    _MCP_TASK = loop.create_task(_run_mcp_image_job(job, float(delay)))
    _MCP_JOB_ID = job.id
    return job


def note_coding_progress(job: McpImageJob) -> int:
    """Count another file-write turn. Comfy still must not start."""
    job.status = "coding"
    job.phase = "writing_code"
    job.code_turns = int(job.code_turns or 0) + 1
    _signal(job)
    return job.code_turns


async def wait_until_done(job: McpImageJob) -> McpImageJob:
    """Block until this job is done or errored. SSE pings keep the client alive."""
    while job.status in ("queued", "running"):
        await wait_mcp_job_progress(job, MCP_POLL_WAIT_MAX_S)
    copy_job_to_workspace(job)
    return job


def copy_job_to_workspace(job: McpImageJob) -> list[str]:
    """Copy finished PNGs into the Code-mode workspace even if the chat stream dropped."""
    existing = list(getattr(job, "workspace_files", None) or [])
    if existing or bool(getattr(job, "workspace_copied", False)):
        return existing
    owner = str(getattr(job, "owner", "") or "").strip()
    chat_id = str(getattr(job, "chat_id", "") or "").strip()
    if not owner or not chat_id:
        return []
    if str(getattr(job, "status", "") or "") not in ("done", "error"):
        return []
    job.workspace_copied = True
    try:
        from ui.workspace import copy_job_pngs

        copied = copy_job_pngs(owner, chat_id, job)
    except Exception as exc:
        job.workspace_copied = False
        xlogger.warning(f"Could not copy job {job.id} into workspace {chat_id}: {exc}")
        return []
    job.workspace_files = list(copied)
    return job.workspace_files


async def wait_mcp_job_progress(job: McpImageJob, wait_s: float) -> None:
    """Block until the job reports progress, finishes, or wait_s elapses."""
    timeout = max(0.0, min(float(wait_s), MCP_POLL_WAIT_MAX_S))
    if timeout <= 0 or str(getattr(job, "status", "") or "") in ("done", "error"):
        return
    seen = int(getattr(job, "progress_seq", 0) or 0)
    progress = getattr(job, "progress", None)
    waiter = getattr(progress, "wait", None)
    clearer = getattr(progress, "clear", None)
    if not callable(waiter):
        return
    deadline = time.monotonic() + timeout
    while int(getattr(job, "progress_seq", 0) or 0) <= seen and str(
        getattr(job, "status", "") or ""
    ) not in ("done", "error"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if callable(clearer):
            clearer()
        if int(getattr(job, "progress_seq", 0) or 0) > seen or str(
            getattr(job, "status", "") or ""
        ) in ("done", "error"):
            break
        try:
            await asyncio.wait_for(waiter(), remaining)
        except asyncio.TimeoutError:
            break


def _record_first_render(item: McpImageItem, seconds: float) -> None:
    """First text-to-image picture after a Comfy start: the bench's flux_s /
    qwen_image_s (checkpoint load plus one render). Cached renders and
    img2img batches are not that number, so callers only pass the first."""
    if item.source_image or max(1, int(item.count or 1)) != 1:
        return
    from common.gpu_mode import uses_qwen_image
    from common.switch_times import record_ready

    field = "qwen_image_s" if uses_qwen_image(item.prompt or "") else "flux_s"
    record_ready("comfy", seconds, field=field)


def _mark_job_items_failed(job: McpImageJob, reason: str) -> None:
    for item in job.items:
        if item.status in ("queued", "running"):
            item.status = "error"
            if not item.error:
                item.error = reason


async def _run_mcp_image_job(job: McpImageJob, delay: float) -> None:
    global _MCP_JOB_ID
    from common.gpu_mode import public_image_url

    restore = job.restore
    restore_name = job.restore_name
    was_llm = False
    started_comfy = False
    bounce_llm = False
    try:
        if delay > 0:
            await asyncio.sleep(delay)
        job.status = "running"
        job.phase = "starting_comfy"
        _signal(job)

        async with _generate_lock():
            if restore:
                restore_name = restore_name or restore_llm_profile()
                job.restore_name = restore_name
            was_llm = bool(loaded_tabby_name())
            comfy_was_up = comfy_up()
            await ensure_comfy()
            started_comfy = True
            render_failed = False
            try:
                index = 0
                while True:
                    async with job.lock:
                        if index >= len(job.items):
                            job.phase = "restoring_llm" if restore else "done"
                            break
                        item = job.items[index]
                    job.phase = "generating"
                    job.current_index = index
                    if item.status == "done" and item.urls:
                        index += 1
                        continue
                    item.status = "running"
                    _signal(job)
                    render_started = time.time()
                    paths = await _render_specs(
                        [
                            {
                                "prompt": item.prompt,
                                "size": item.size,
                                "seed": item.seed,
                                "count": item.count,
                                "source_image": item.source_image or None,
                                "denoise": item.denoise,
                            }
                        ],
                        owner=job.owner or None,
                    )
                    if index == 0 and not comfy_was_up:
                        _record_first_render(item, time.time() - render_started)
                    item.urls = [
                        public_image_url(path.name, api_base=job.api_base, bust=False)
                        for path in paths
                    ]
                    job.urls.extend(item.urls)
                    item.status = "done"
                    index += 1
                    _signal(job)
            except Exception as exc:
                render_failed = True
                _mark_job_items_failed(job, str(exc))
                _signal(job)
                raise
            finally:
                if restore and started_comfy and (was_llm or restore_name):
                    job.phase = "restoring_llm"
                    _signal(job)
                    try:
                        await reload_last_llm(restore_name, from_job=True)
                    except Exception as restore_exc:
                        xlogger.warning(
                            f"Could not restore LLM after image job {job.id}: {restore_exc}"
                        )
                        if is_vram_error(restore_exc):
                            bounce_llm = True
                        elif not render_failed:
                            raise
        job.status = "done"
        job.phase = "done"
        copy_job_to_workspace(job)
        _persist_job_reply(job)
        _signal(job)
    except asyncio.CancelledError:
        if job.status not in ("done", "error"):
            job.status = "error"
            job.phase = "error"
            job.error = job.error or "Image job was cancelled"
            _mark_job_items_failed(job, job.error)
            _signal(job)
        copy_job_to_workspace(job)
        _persist_job_reply(job)
        raise
    except Exception as exc:
        job.status = "error"
        job.phase = "error"
        job.error = str(exc)
        _mark_job_items_failed(job, str(exc))
        copy_job_to_workspace(job)
        _persist_job_reply(job)
        _signal(job)
    finally:
        if _MCP_JOB_ID == job.id:
            _MCP_JOB_ID = None
        # Render failures used to skip the bounce below job.status = "done",
        # so leftover VRAM left the UI on "Loading qwen36" with no model.
        if bounce_llm:
            _bounce_after_vram_fail(restore_name or last_profile() or "qwen")


async def reset_mcp_image_jobs_for_tests() -> None:
    global _MCP_TASK, _MCP_JOB_ID, _GENERATE_LOCK, _GENERATE_LOCK_LOOP, _PERSIST_LOADED
    if _MCP_TASK is not None and not _MCP_TASK.done():
        _MCP_TASK.cancel()
        try:
            await _MCP_TASK
        except (asyncio.CancelledError, Exception):
            pass
    _MCP_TASK = None
    _MCP_JOB_ID = None
    _MCP_JOBS.clear()
    _MCP_ORDER.clear()
    _GENERATE_LOCK = None
    _GENERATE_LOCK_LOOP = None
    _PERSIST_LOADED = False
