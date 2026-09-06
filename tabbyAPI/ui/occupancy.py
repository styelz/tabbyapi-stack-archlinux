"""Serialize UI Chat, Code, and GPU actions when the stack is already in use."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional
from uuid import uuid4

QUEUE_MARK = "tabbyapi-stack-queue:"
QUEUE_HINT = "The stack is being used. You are in a queue."
MINE_HINT = "Your session is running."
SELF_QUEUED_HINT = "Your previous request is still running."

KIND_ACTIONS = {
    "chat": "chatting",
    "code": "writing code",
    "image": "generating images",
    "gpu": "switching the GPU",
}

# A lease is normally released by the task that took it. These bound the damage
# when that task dies without unwinding: an aborted Steer or Stop used to leave
# the slot held and wedge every chat behind "your previous request is running".
_LEASE_MAX_S = 30 * 60

_cond = asyncio.Condition()
_occupant: Optional["Occupant"] = None
_waiters: list["Waiter"] = []


@dataclass
class Occupant:
    id: str
    username: str
    started_at: float
    kind: str = "chat"
    chat_id: str = ""
    prompt: str = ""
    task: Optional[asyncio.Task] = None


@dataclass
class Waiter:
    id: str
    username: str
    queued_at: float
    kind: str = "chat"
    chat_id: str = ""
    prompt: str = ""


def _image_job():
    try:
        from images.jobs import active_mcp_image_job

        job = active_mcp_image_job()
    except Exception:
        return None
    if job and job.status in ("queued", "running"):
        return job
    return None


def _switch_busy() -> bool:
    try:
        from common.phrase_switch import switch_lock_held

        return bool(switch_lock_held())
    except Exception:
        return False


def _llm_jobs_active() -> bool:
    try:
        from common import model as tabby_model

        container = tabby_model.container
        jobs = getattr(container, "active_job_ids", None) if container is not None else None
        return bool(jobs)
    except Exception:
        return False


def _externally_busy() -> bool:
    if _image_job() is not None:
        return True
    if _switch_busy():
        return True
    return _llm_jobs_active()


def _reclaim_stale() -> bool:
    """Drop a lease whose owning task is gone, or that has run absurdly long."""
    global _occupant
    occupant = _occupant
    if occupant is None:
        return False
    if time.time() - occupant.started_at > _LEASE_MAX_S:
        _occupant = None
        return True
    task = occupant.task
    if task is None or not task.done():
        return False
    # Streaming hands the HTTP task off to an SSE generator. The acquire task
    # looks done while the GPU job is still running — do not drop that lease.
    if _llm_jobs_active():
        return False
    try:
        from ui.flight import iter_live_flights

        if iter_live_flights():
            return False
    except Exception:
        pass
    _occupant = None
    return True


def _holder(occupant: Optional[Occupant]) -> tuple[Optional[str], str]:
    """kind, occupant username for the current GPU holder."""
    if occupant is not None:
        return occupant.kind, occupant.username or ""
    job = _image_job()
    if job is not None:
        return "image", str(getattr(job, "owner", "") or "")
    if _switch_busy():
        return "gpu", ""
    if _llm_jobs_active():
        return "chat", ""
    return None, ""


def snapshot(username: str = "") -> dict[str, Any]:
    _reclaim_stale()
    occupant = _occupant
    waiters = list(_waiters)
    who = (username or "").strip()
    position = None
    queued_at = None
    for index, waiter in enumerate(waiters, start=1):
        if who and waiter.username == who:
            position = index
            queued_at = waiter.queued_at
            break
    kind, occupant_name = _holder(occupant)
    now = time.time()
    mine = bool(who and occupant_name and occupant_name == who)
    queued = position is not None
    busy = occupant is not None or bool(waiters) or _externally_busy()
    try:
        from ui.flight import iter_live_flights

        live_flights = iter_live_flights()
    except Exception:
        live_flights = []
    if live_flights:
        busy = True
        if not kind:
            kind = str(live_flights[0].kind or "chat") or "chat"
    chat_id = ""
    prompt = ""
    live = bool(live_flights)
    if occupant and who and occupant.username == who:
        chat_id = occupant.chat_id or ""
    elif position is not None:
        for waiter in waiters:
            if who and waiter.username == who:
                chat_id = waiter.chat_id or ""
                break
    if not chat_id and who:
        job = _image_job()
        if job and str(getattr(job, "owner", "") or "") == who:
            chat_id = str(getattr(job, "chat_id", "") or "")
    try:
        from ui.flight import snapshot_for

        info = snapshot_for(who)
    except Exception:
        info = None
    if info:
        live = live or bool(info.get("live"))
        chat_id = str(info.get("chat_id") or chat_id or "")
        prompt = str(info.get("prompt") or "")
        kind = str(info.get("kind") or kind or "") or kind
    if not prompt:
        mine_holder = bool(occupant and who and occupant.username == who)
        wall = not who
        if occupant is not None and (mine_holder or wall):
            prompt = str(occupant.prompt or "")
        if not prompt and wall:
            for flight in live_flights:
                text = str(getattr(flight, "prompt", "") or "").strip()
                if text:
                    prompt = text
                    break
    return {
        "busy": busy,
        "queued": queued,
        "position": position,
        "waiters": len(waiters),
        "kind": kind,
        "occupant": occupant_name or None,
        "mine": mine,
        "chat_id": chat_id,
        "prompt": prompt,
        "live": live,
        "elapsed_s": int(now - occupant.started_at) if occupant else 0,
        "queued_elapsed_s": int(now - queued_at) if queued_at else 0,
        "hint": queue_text(
            {
                "position": position or 0,
                "queued": queued,
                "busy": busy,
                "kind": kind,
                "occupant": occupant_name,
                "who": who,
                "mine": mine,
            }
        ),
    }


def _holder_head(occupant: str, who: str, kind: str) -> str:
    action = KIND_ACTIONS.get(kind, "")
    if occupant and occupant != who:
        return f"{occupant} is {action}." if action else f"{occupant} is using the stack."
    if occupant and occupant == who:
        return MINE_HINT
    if action:
        return f"The stack is {action}."
    return "The stack is being used."


def queue_text(info: Optional[dict[str, Any]] = None) -> str:
    info = info or {}
    position = int(info.get("position") or 0)
    occupant = str(info.get("occupant") or "").strip()
    kind = str(info.get("kind") or "").strip()
    who = str(info.get("who") or "").strip()
    queued = bool(info.get("queued")) or position > 0
    mine = bool(info.get("mine")) or bool(who and occupant and occupant == who)
    head = _holder_head(occupant, who, kind)
    if queued:
        if mine:
            line = SELF_QUEUED_HINT
        elif head == "The stack is being used.":
            line = QUEUE_HINT
        else:
            line = f"{head} You are in a queue."
        if position > 1:
            return f"{line} You are number {position}."
        return line
    if mine:
        return MINE_HINT
    if info.get("busy") or occupant or kind:
        return head
    return "The stack is being used."


def queue_comment(info: Optional[dict[str, Any]] = None) -> str:
    return f"{QUEUE_MARK} {queue_text(info)}"


async def try_acquire(
    username: str, *, kind: str = "chat", chat_id: str = "", prompt: str = ""
) -> Optional[str]:
    """Take the slot if nothing else is waiting or running."""
    global _occupant
    async with _cond:
        _reclaim_stale()
        if _occupant is not None or _waiters or _externally_busy():
            return None
        occupant = Occupant(
            id=uuid4().hex,
            username=username or "",
            started_at=time.time(),
            kind=kind,
            chat_id=chat_id or "",
            prompt=str(prompt or "").strip(),
            task=asyncio.current_task(),
        )
        _occupant = occupant
        return occupant.id


async def enqueue(
    username: str, *, kind: str = "chat", chat_id: str = "", prompt: str = ""
) -> Waiter:
    async with _cond:
        waiter = Waiter(
            id=uuid4().hex,
            username=username or "",
            queued_at=time.time(),
            kind=kind,
            chat_id=chat_id or "",
            prompt=str(prompt or "").strip(),
        )
        _waiters.append(waiter)
        return waiter


async def promote(waiter: Waiter) -> Optional[str]:
    global _occupant
    async with _cond:
        _reclaim_stale()
        if _occupant is not None:
            return None
        if not _waiters or _waiters[0].id != waiter.id:
            return None
        if _externally_busy():
            return None
        _waiters.pop(0)
        occupant = Occupant(
            id=waiter.id,
            username=waiter.username,
            started_at=time.time(),
            kind=waiter.kind,
            chat_id=waiter.chat_id,
            prompt=str(waiter.prompt or ""),
            task=asyncio.current_task(),
        )
        _occupant = occupant
        return occupant.id


async def drop_waiter(waiter: Waiter) -> None:
    async with _cond:
        _waiters[:] = [item for item in _waiters if item.id != waiter.id]
        _cond.notify_all()


async def release(occupant_id: Optional[str]) -> None:
    global _occupant
    if not occupant_id:
        return
    async with _cond:
        if _occupant and _occupant.id == occupant_id:
            _occupant = None
            _cond.notify_all()


async def wait_tick(timeout: float = 1.0) -> None:
    async with _cond:
        try:
            await asyncio.wait_for(_cond.wait(), timeout)
        except asyncio.TimeoutError:
            return


def reset_for_tests() -> None:
    global _occupant
    _occupant = None
    _waiters.clear()
    from ui.flight import reset_for_tests as reset_flights

    reset_flights()
    try:
        from common.live_decode import reset_for_tests as reset_decode

        reset_decode()
    except Exception:
        pass


class StackGate:
    """One UI chat or GPU request: take the GPU slot or wait in line."""

    def __init__(
        self, username: str, *, kind: str = "chat", chat_id: str = "", prompt: str = ""
    ):
        self.username = username or ""
        self.kind = kind
        self.chat_id = chat_id or ""
        self.prompt = str(prompt or "").strip()
        self.occupant_id: Optional[str] = None
        self.waiter: Optional[Waiter] = None

    async def step(self, disconnect_handler) -> Optional[dict[str, Any]]:
        """None once this request owns the stack; otherwise a queue snapshot."""
        if self.occupant_id:
            return None
        if self.waiter is None:
            self.occupant_id = await try_acquire(
                self.username, kind=self.kind, chat_id=self.chat_id, prompt=self.prompt
            )
            if self.occupant_id:
                return None
            self.waiter = await enqueue(
                self.username, kind=self.kind, chat_id=self.chat_id, prompt=self.prompt
            )
        await disconnect_handler.poll()
        occupant_id = await promote(self.waiter)
        if occupant_id:
            self.occupant_id = occupant_id
            self.waiter = None
            return None
        return snapshot(self.username)

    async def wait_until_acquired(self, disconnect_handler) -> None:
        while True:
            info = await self.step(disconnect_handler)
            if info is None:
                return
            await wait_tick(1.0)

    async def adopt_task(self) -> None:
        """Point the lease at the current task when ownership hands off."""
        async with _cond:
            if _occupant is not None and _occupant.id == self.occupant_id:
                _occupant.task = asyncio.current_task()

    async def release(self) -> None:
        waiter = self.waiter
        occupant_id = self.occupant_id
        self.waiter = None
        self.occupant_id = None
        if waiter is not None:
            await drop_waiter(waiter)
        await release(occupant_id)


async def drain_sse(response) -> AsyncIterator[Any]:
    """Yield a streaming response's items verbatim, then close its iterator.

    Byte-transparent on purpose: third-party IDEs read the same SSE stream as
    the browser UI, so nothing here may add, drop, reorder, or buffer items.
    """
    iterator = getattr(response, "body_iterator", None)
    if iterator is None:
        return
    try:
        async for item in iterator:
            yield item
    finally:
        closer = getattr(iterator, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception:
                pass


async def stream_and_release(
    gate: StackGate, response, *, adopt: bool = False
) -> AsyncIterator[Any]:
    """drain_sse, releasing the gate lease once the stream is done.

    Every streaming path that holds a lease must go through this. Forgetting the
    release strands the lease and the stack looks busy until it ages out.
    Pass adopt=True when the lease was taken by a different task than the one
    that will drain the stream.
    """
    if adopt:
        await gate.adopt_task()
    try:
        async for item in drain_sse(response):
            yield item
    finally:
        await gate.release()
