"""Chat Completions at the edge: phrase-switch, queue, images, then proxy."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from fastapi import Request
from sse_starlette import EventSourceResponse
from fastapi.responses import StreamingResponse

from sidecar.paths import ensure_import_path

ensure_import_path()

from common.agent_loop import inject_loop_break, inject_zero_change_hint
from common.gpu_mode import public_api_base
from common.networking import DisconnectHandler
from common.pasted_images import latest_turn_image, materialize_pasted_images
from common.phrase_switch import (
    comfy_idle_response,
    gpu_is_comfy,
    handle_if_requested,
    inject_clipboard_save_hint,
    is_restart_request,
    last_user_text,
    requested_profile,
    restart_reply_text,
    should_yield_comfy_to_llm,
    start_restart,
    start_switch,
    switch_reply_text,
    text_response,
    tools_without_format_response,
    yield_comfy_to_llm_response,
)
from endpoints.OAI.types.chat_completion import ChatCompletionRequest
from sidecar import proxy as proxy_mod
from sidecar.model_status import llm_is_ready


async def handle_chat_completion(
    request: Request,
    raw: Optional[dict[str, Any]] = None,
    *,
    proxy_fn: Optional[Callable[..., Awaitable[Any]]] = None,
    image_handler: Optional[Callable[..., Awaitable[Any]]] = None,
    skip_occupancy: bool = False,
) -> Any:
    """Public /v1/chat/completions path. Does not generate inside this process."""
    body = raw if raw is not None else await request.json()
    data = ChatCompletionRequest.model_validate(body)
    api_base = public_api_base(request)
    materialize_pasted_images(data)
    early = handle_if_requested(data, api_base=api_base, defer_switch=True)
    if early is not None:
        return early
    switching = is_restart_request(data) or bool(requested_profile(data))
    if not switching:
        inject_clipboard_save_hint(data, api_base=api_base)
        inject_zero_change_hint(data)
        inject_loop_break(data)

    source_image = latest_turn_image(data)
    disconnect_handler = DisconnectHandler(request, "/v1/chat/completions")
    from ui.occupancy import StackGate, stream_and_release

    gate = StackGate(
        str(getattr(data, "user", None) or "api"),
        kind="chat",
        prompt=last_user_text(data),
    )
    handed_off = False
    try:
        if not skip_occupancy:
            await gate.wait_until_acquired(disconnect_handler)
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

        handler = image_handler
        if handler is None:
            from images.chat import handle as handle_image_chat

            handler = handle_image_chat
        image_response = await handler(
            data,
            api_base,
            source_image=source_image,
            llm_ready=llm_is_ready(),
            gpu_is_comfy=gpu_is_comfy(),
            disconnect_handler=disconnect_handler,
        )
        if image_response is not None:
            return image_response
        if not llm_is_ready():
            if not gpu_is_comfy():
                from common.phrase_switch import llm_not_ready_response

                return await llm_not_ready_response(data)
            if should_yield_comfy_to_llm(data):
                return await yield_comfy_to_llm_response(data)
            return await comfy_idle_response(data, api_base=api_base)
        refused = tools_without_format_response(data)
        if refused is not None:
            return refused

        forward = proxy_fn or proxy_mod.forward
        payload = data.model_dump(mode="json", exclude_none=True)
        import json

        from sidecar.llama_adapter import adapt_chat_payload, rewrite_sse_line
        from sidecar.settings import chat_backend_url
        from sidecar.proxy import get_client

        target = chat_backend_url()
        llama = False
        try:
            from common.phrase_switch import gpu_is_llama

            llama = gpu_is_llama()
        except Exception:
            llama = target.rstrip("/").endswith(":5002")
        if llama:
            payload = adapt_chat_payload(payload)
        result = await forward(
            request,
            path="/v1/chat/completions",
            body=json.dumps(payload).encode("utf-8"),
            client=get_client(target) if llama else None,
        )
        if llama and isinstance(result, StreamingResponse):
            upstream = result.body_iterator

            async def _rewrite():
                in_think = False
                try:
                    async for chunk in upstream:
                        text = (
                            chunk.decode("utf-8", "replace")
                            if isinstance(chunk, bytes)
                            else str(chunk)
                        )
                        out_parts = []
                        for line in text.splitlines(keepends=True):
                            if line.startswith("data:"):
                                rewritten, in_think = rewrite_sse_line(
                                    line.rstrip("\n"), in_think=in_think
                                )
                                out_parts.append(
                                    rewritten + ("\n" if line.endswith("\n") else "")
                                )
                            else:
                                out_parts.append(line)
                        yield "".join(out_parts).encode("utf-8")
                finally:
                    closer = getattr(upstream, "aclose", None)
                    if closer is not None:
                        await closer()

            result = StreamingResponse(
                _rewrite(),
                status_code=result.status_code,
                headers=dict(result.headers),
                media_type=result.media_type,
            )
        if isinstance(result, EventSourceResponse):
            handed_off = True
            return EventSourceResponse(
                stream_and_release(gate, result, adopt=True),
                ping=result.ping if hasattr(result, "ping") else 15,
                sep="\n",
            )
        if isinstance(result, StreamingResponse) and not skip_occupancy:
            handed_off = True
            return StreamingResponse(
                stream_and_release(gate, result, adopt=True),
                status_code=result.status_code,
                headers=dict(result.headers),
                media_type=result.media_type,
            )
        return result
    finally:
        if not skip_occupancy and not handed_off:
            await gate.release()
