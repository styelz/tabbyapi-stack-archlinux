"""Console chat: LLM replies plus inline images. Code mode writes a jailed project."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request
from sse_starlette import EventSourceResponse, ServerSentEvent

from common import model
from common.gpu_mode import public_api_base
from common.networking import DisconnectHandler, get_sse_ping_interval
from common.phrase_switch import (
    comfy_chat_suggest_text,
    gpu_is_comfy,
    handle_if_requested,
    is_restart_request,
    last_user_text,
    looks_like_chat_not_image,
    requested_profile,
    restart_reply_text,
    start_restart,
    start_switch,
    stream_text,
    switch_reply_text,
    text_response,
)
from endpoints.OAI.types.chat_completion import ChatCompletionRequest
from endpoints.OAI.utils.pipeline import run_chat_completion_turn
from common.pasted_images import latest_turn_image, materialize_pasted_images
from ui.flight import (
    ConsoleFlight,
    abort_flight,
    close_flight,
    get_flight,
    register_flight,
    stream_response,
)
from ui.manager import sanitize_chat_payload, sanitize_code_payload
from ui.occupancy import (
    StackGate,
    drain_sse,
    queue_comment,
    stream_and_release,
    wait_tick,
)


USAGE_MARK = "tabby-context-usage:"


def usage_dict(usage: Any) -> dict[str, int] | None:
    if usage is None:
        return None
    raw = usage.model_dump(mode="json") if hasattr(usage, "model_dump") else usage
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
    return {
        "prompt_tokens": max(0, prompt),
        "completion_tokens": max(0, completion),
        "total_tokens": max(0, total),
    }


def usage_sse_data(usage: Any) -> str:
    payload = usage_dict(usage)
    if not payload:
        return ""
    return json.dumps(
        {
            "id": f"chatcmpl-{uuid4().hex}",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": payload,
        }
    )


def usage_comment(usage: Any) -> str:
    payload = usage_dict(usage)
    if not payload:
        return ""
    return f"{USAGE_MARK} {json.dumps(payload, separators=(',', ':'))}"


CODE_DEFAULT_MAX_TOKENS = 16384
_SAMPLER_KEYS = (
    "temperature",
    "top_p",
    "min_p",
    "frequency_penalty",
    "presence_penalty",
    "max_tokens",
)


def _apply_sampler_fields(payload: dict[str, Any], fields: dict[str, Any]) -> None:
    for key in _SAMPLER_KEYS:
        if payload.get(key) is not None:
            fields[key] = payload[key]


def completion_request_from_payload(payload: dict[str, Any]) -> ChatCompletionRequest:
    fields: dict[str, Any] = {
        "messages": payload["messages"],
        "stream": payload.get("stream", True),
        "tools": payload.get("tools"),
        "stream_options": {"include_usage": True},
    }
    _apply_sampler_fields(payload, fields)
    if fields.get("max_tokens") is None and payload.get("mode") == "code":
        fields["max_tokens"] = CODE_DEFAULT_MAX_TOKENS
    if payload.get("tool_choice") is not None:
        fields["tool_choice"] = payload["tool_choice"]
    return ChatCompletionRequest(**fields)


def is_code_request(body: dict[str, Any]) -> bool:
    return str(body.get("mode") or "").strip().lower() == "code"


def _completion_text(result: Any) -> str:
    choices = getattr(result, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    return str(getattr(message, "content", None) or "")


def _iter_sse(response) -> Any:
    """Drain a streaming response without touching any gate lease.

    Used where the items go into a flight rather than out to a client, so there
    is no lease to release here.
    """
    return drain_sse(response)


async def _run_console_work(
    request: Request,
    data: ChatCompletionRequest,
    username: str,
    chat_id: str,
    code: bool,
    saved_images: list,
    api_base: str,
    disconnect_handler,
    agent: str = "agent",
):
    await disconnect_handler.poll()
    if is_restart_request(data):
        if not start_restart():
            return text_response(
                data,
                "Restart is not available on this host. Send help for the chat phrases.",
            )
        return text_response(data, restart_reply_text())
    name = requested_profile(data)
    if name:
        start_switch(name)
        return text_response(data, switch_reply_text(name))
    if not model.container or not getattr(model.container, "loaded", False):
        if gpu_is_comfy() and looks_like_chat_not_image(last_user_text(data)):
            return text_response(data, comfy_chat_suggest_text())
    try:
        return await run_chat_completion_turn(
            request,
            data,
            disconnect_handler,
            api_base=api_base,
            source_image=saved_images[-1] if saved_images else None,
            console=True,
            owner=username or None,
            chat_id=chat_id or None,
            agent=agent,
            code=code,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc


def _proxy_request(request: Request):
    from common.networking import generation_request

    req_id = getattr(getattr(request, "state", None), "id", None) or uuid4().hex
    return generation_request(SimpleNamespace(state=SimpleNamespace(id=req_id)))


def _stream_held_result(gate: StackGate, result):
    return stream_and_release(gate, result, adopt=True)


def _sse(content):
    return EventSourceResponse(
        content,
        ping=get_sse_ping_interval(),
        sep="\n",
    )


async def _pump_console_result(
    flight: ConsoleFlight, result, data: ChatCompletionRequest
) -> None:
    if isinstance(result, EventSourceResponse):
        async for item in _iter_sse(result):
            await flight.publish(item)
        return
    text = _completion_text(result)
    if text:
        async for chunk in stream_text(data, text):
            await flight.publish(chunk)
    payload = usage_sse_data(getattr(result, "usage", None))
    if payload:
        await flight.publish(payload)


async def _run_console_job(
    flight: ConsoleFlight,
    request: Request,
    data: ChatCompletionRequest,
    username: str,
    workspace_id: str,
    code: bool,
    saved_images: list,
    api_base: str,
    agent: str,
    gate: StackGate,
) -> None:
    proxy = _proxy_request(request)
    handler = DisconnectHandler(proxy, "/v1/ui/chat", abort_event=flight.abort_event)
    try:
        info = await gate.step(handler)
        while info is not None:
            try:
                event = ServerSentEvent(comment=queue_comment(info), sep="\n")
            except TypeError:
                event = ServerSentEvent(comment=queue_comment(info))
            await flight.publish(event)
            await wait_tick(1.0)
            info = await gate.step(handler)
        result = await _run_console_work(
            proxy,
            data,
            username,
            workspace_id,
            code,
            saved_images,
            api_base,
            handler,
            agent,
        )
        await _pump_console_result(flight, result, data)
    except asyncio.CancelledError:
        pass
    except HTTPException as exc:
        await flight.publish(json.dumps({"error": {"message": str(exc.detail)}}))
    except Exception as exc:
        from common.logger import xlogger

        xlogger.error("Console chat job failed", str(exc))
        await flight.publish(json.dumps({"error": {"message": str(exc)}}))
    finally:
        await gate.release()
        await close_flight(flight)


async def run_console_chat(request: Request, body: dict[str, Any], username: str = ""):
    if body.get("cancel"):
        abort_flight(
            username,
            str(body.get("conversation_id") or body.get("chat_id") or ""),
        )
        return {"ok": True}
    if body.get("resume"):
        return stream_response(
            get_flight(
                username,
                str(body.get("conversation_id") or body.get("chat_id") or ""),
            )
        )

    code = is_code_request(body)
    try:
        payload = (
            sanitize_code_payload(body, username) if code else sanitize_chat_payload(body)
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    workspace_id = str(payload.get("chat_id") or "") if code else ""
    conversation_id = str(payload.get("conversation_id") or "").strip()
    handle_chat_id = workspace_id or conversation_id
    agent = str(payload.get("agent") or "agent") if code else "agent"
    data = completion_request_from_payload(payload)
    # Every pasted image is written to disk, but only one attached on this turn
    # may seed img2img: an older one made unrelated image prompts re-render it.
    materialize_pasted_images(data)
    turn_image = latest_turn_image(data)
    saved_images = [turn_image] if turn_image else []
    api_base = public_api_base(request)
    switched = handle_if_requested(data, api_base=api_base, defer_switch=True)
    if switched is not None:
        return switched

    kind = "code" if code else "chat"
    gate = StackGate(
        username, kind=kind, chat_id=conversation_id, prompt=last_user_text(data)
    )
    if data.stream:
        flight = ConsoleFlight(
            username, conversation_id, kind, last_user_text(data), agent=agent
        )
        register_flight(flight)
        flight.task = asyncio.create_task(
            _run_console_job(
                flight,
                request,
                data,
                username,
                handle_chat_id,
                code,
                saved_images,
                api_base,
                agent,
                gate,
            )
        )
        return stream_response(flight)

    disconnect_handler = DisconnectHandler(request, "/v1/ui/chat")
    # An image hold can answer a non-streaming request with a stream. When it
    # does, the returned generator owns the lease and we must not release here.
    handed_off = False
    try:
        while True:
            info = await gate.step(disconnect_handler)
            if info is None:
                break
            await wait_tick(1.0)
        result = await _run_console_work(
            request,
            data,
            username,
            handle_chat_id,
            code,
            saved_images,
            api_base,
            disconnect_handler,
            agent,
        )
        if isinstance(result, EventSourceResponse):
            handed_off = True
            return _sse(_stream_held_result(gate, result))
        return result
    finally:
        if not handed_off:
            await gate.release()
