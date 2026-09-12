"""One Chat Completions turn for both /v1/chat/completions and /v1/ui/chat."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException, Request
from sse_starlette import EventSourceResponse

from common import model
from common.assistant_text import strip_apology_sse, strip_response_apologies
from common.debug_requests import write_chat_completion_prompt_log
from common.model import check_model_container
from common.networking import get_sse_ping_interval, handle_request_error
from common.phrase_switch import (
    comfy_idle_response,
    gpu_is_comfy,
    llm_not_ready_response,
    should_yield_comfy_to_llm,
    tools_without_format_response,
    yield_comfy_to_llm_response,
)
from common.tabby_config import config
from endpoints.OAI.types.chat_completion import ChatCompletionRequest
from endpoints.OAI.utils.chat_completion import (
    apply_chat_template,
    generate_chat_completion,
    stream_generate_chat_completion,
)
from endpoints.OAI.utils.common_ import load_inline_model
from images.chat import handle as handle_image_chat

load_lock: asyncio.Lock = asyncio.Lock()


async def run_chat_completion_turn(
    request: Request,
    data: ChatCompletionRequest,
    disconnect_handler,
    *,
    api_base: str,
    source_image=None,
    console: bool = False,
    owner: str | None = None,
    chat_id: str | None = None,
    agent: str = "agent",
    code: bool = False,
    generate_only: bool = False,
):
    """Image intercept, then one generate. Callers own phrase-switch and StackGate."""
    sidecar = False
    try:
        from sidecar.settings import is_sidecar_process

        sidecar = is_sidecar_process()
    except Exception:
        sidecar = False
    if sidecar:
        from sidecar.model_status import llm_is_ready as remote_ready

        llm_ready = remote_ready()
    else:
        llm_ready = bool(model.container and getattr(model.container, "loaded", False))
    await disconnect_handler.poll()
    if generate_only:
        sidecar = False
        image_response = None
    else:
        image_response = await handle_image_chat(
        data,
        api_base,
        source_image=source_image,
        llm_ready=llm_ready,
        gpu_is_comfy=gpu_is_comfy(),
        disconnect_handler=disconnect_handler,
        console=console,
        owner=owner,
        chat_id=chat_id,
        agent=agent,
        code=code,
    )
    if image_response is not None:
        return image_response
    if not llm_ready:
        if not gpu_is_comfy():
            return await llm_not_ready_response(data, console=console)
        if should_yield_comfy_to_llm(data):
            return await yield_comfy_to_llm_response(data, console=console)
        return await comfy_idle_response(data, api_base=api_base)

    if sidecar:
        from sidecar.proxy import forward_llm_chat

        refused = tools_without_format_response(data)
        if refused is not None:
            return refused
        payload = data.model_dump(mode="json", exclude_none=True)
        return await forward_llm_chat(payload, request=request)

    async with load_lock:
        if data.model:
            await load_inline_model(data.model, request)
        else:
            await check_model_container()
        if not (model.container and getattr(model.container, "model_dir", None)):
            if gpu_is_comfy():
                if should_yield_comfy_to_llm(data):
                    return await yield_comfy_to_llm_response(data, console=console)
                return await comfy_idle_response(data, api_base=api_base)
            return await llm_not_ready_response(data, console=console)
        model_path = model.container.model_dir

    refused = tools_without_format_response(data)
    if refused is not None:
        return refused

    if model.container.prompt_template is None:
        error_message = handle_request_error(
            "Chat completions are disabled because a prompt template is not set.",
            exc_info=False,
        ).error.message
        raise HTTPException(422, error_message)
    prompt, mm_embeddings = await apply_chat_template(data)
    await write_chat_completion_prompt_log(request, prompt)

    if data.response_format.type == "json":
        data.json_schema = {"type": "object"}
    if data.response_format.type == "json_schema":
        data.json_schema = data.response_format.json_schema

    await disconnect_handler.poll()
    if data.stream and not config.developer.disable_request_streaming:
        model.check_context_length(prompt, data, mm_embeddings)
        return EventSourceResponse(
            strip_apology_sse(
                stream_generate_chat_completion(
                    prompt, mm_embeddings, data, request, model_path, disconnect_handler
                )
            ),
            ping=get_sse_ping_interval(),
            sep="\n",
        )
    response = await generate_chat_completion(
        prompt, mm_embeddings, data, request, model_path, disconnect_handler
    )
    return strip_response_apologies(response)
