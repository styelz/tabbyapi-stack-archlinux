import asyncio
from asyncio import CancelledError, InvalidStateError

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette import EventSourceResponse

from common import model
from common.auth import check_api_key
from common.debug_requests import log_chat_completion_request
from common.model import check_embeddings_container, check_model_container
from common.networking import (
    get_sse_ping_interval,
    DisconnectHandler,
    run_with_request_disconnect,
)
from common.tabby_config import config
from common.logger import xlogger
from endpoints.OAI.types.completion import CompletionRequest, CompletionResponse
from endpoints.OAI.types.chat_completion import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from endpoints.OAI.types.embedding import EmbeddingsRequest, EmbeddingsResponse
from common.agent_loop import inject_loop_break, inject_zero_change_hint
from common.gpu_mode import public_api_base
from common.pasted_images import latest_turn_image, materialize_pasted_images
from common.phrase_switch import (
    handle_if_requested,
    inject_clipboard_save_hint,
    is_restart_request,
    last_user_text,
    requested_profile,
    restart_reply_text,
    start_restart,
    start_switch,
    switch_reply_text,
    text_response,
)
from endpoints.OAI.utils.common_ import load_inline_model
from endpoints.OAI.utils.pipeline import load_lock, run_chat_completion_turn
from endpoints.OAI.utils.completion import (
    generate_completion,
    stream_generate_completion,
)
from endpoints.OAI.utils.embeddings import get_embeddings


api_name = "OAI"
router = APIRouter()
urls = {
    "Completions": "http://{host}:{port}/v1/completions",
    "Chat completions": "http://{host}:{port}/v1/chat/completions",
}

def setup():
    return router


# Completions endpoint
@router.post(
    "/v1/completions",
    dependencies=[Depends(check_api_key)],
)
async def completion_request(request: Request, data: CompletionRequest) -> CompletionResponse:
    """
    Generates a completion from a prompt.

    If stream = true, this returns an SSE stream.
    """

    raw_json = await request.json()
    xlogger.debug("[ENDPOINT] /v1/completions", {"raw": raw_json})

    async with load_lock:
        if data.model:
            await load_inline_model(data.model, request)
        else:
            await check_model_container()
        model_path = model.container.model_dir

    # Prepare raw prompt (will be str or list[str])
    prompt = data.prompt

    # Set an empty JSON schema if the request wants a JSON response
    if data.response_format.type == "json":
        data.json_schema = {"type": "object"}

    # Also accept specific schema from response_format
    if data.response_format.type == "json_schema":
        data.json_schema = data.response_format.json_schema

    try:
        disconnect_handler = DisconnectHandler(request, "/v1/completions")
        await disconnect_handler.poll()

        if data.stream and not config.developer.disable_request_streaming:
            model.check_context_length(prompt, data)
            return EventSourceResponse(
                stream_generate_completion(prompt, data, request, model_path, disconnect_handler),
                ping=get_sse_ping_interval(),
                sep="\n",
            )
        else:
            response = await generate_completion(
                prompt, data, request, model_path, disconnect_handler
            )
            return response

    except (CancelledError, InvalidStateError) as ex:
        raise HTTPException(422, "/v1/completions request cancelled by user.") from ex


# Chat completions endpoint
@router.post(
    "/v1/chat/completions",
    dependencies=[Depends(check_api_key), Depends(log_chat_completion_request)],
)
async def chat_completion_request(
    request: Request, data: ChatCompletionRequest
) -> ChatCompletionResponse:
    """
    Generates a chat completion from a prompt.

    If stream = true, this returns an SSE stream.
    """

    raw_json = await request.json()
    xlogger.debug("[ENDPOINT] /v1/chat/completions", {"raw": raw_json})

    api_base = public_api_base(request)
    materialize_pasted_images(data)
    if (request.headers.get("x-tabby-generate-only") or "").strip() == "1":
        disconnect_handler = DisconnectHandler(request, "/v1/chat/completions")
        return await run_chat_completion_turn(
            request,
            data,
            disconnect_handler,
            api_base=api_base,
            source_image=latest_turn_image(data),
            generate_only=True,
        )
    early = handle_if_requested(data, api_base=api_base, defer_switch=True)
    if early is not None:
        return early
    switching = is_restart_request(data) or bool(requested_profile(data))
    if not switching:
        inject_clipboard_save_hint(data, api_base=api_base)
        inject_zero_change_hint(data)
        inject_loop_break(data)

    # Only an image attached on this turn may seed img2img. Clients resend the
    # whole history, so an older one turned a later unrelated image prompt into
    # an img2img render of it.
    source_image = latest_turn_image(data)
    disconnect_handler = DisconnectHandler(request, "/v1/chat/completions")
    from ui.occupancy import StackGate, stream_and_release

    gate = StackGate(
        str(getattr(data, "user", None) or "api"),
        kind="chat",
        prompt=last_user_text(data),
    )
    # On the streaming path the returned generator owns the lease, so this
    # request must not release it on the way out.
    handed_off = False
    try:
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
        result = await run_chat_completion_turn(
            request,
            data,
            disconnect_handler,
            api_base=api_base,
            source_image=source_image,
        )
        if isinstance(result, EventSourceResponse):
            handed_off = True
            # adopt=True: the HTTP handler task finishes when this returns, and
            # occupancy would otherwise reclaim the lease mid-stream.
            return EventSourceResponse(
                stream_and_release(gate, result, adopt=True),
                ping=get_sse_ping_interval(),
                sep="\n",
            )
        return result
    except (CancelledError, InvalidStateError) as ex:
        raise HTTPException(422, "/v1/chat/completions request cancelled by user.") from ex
    finally:
        if not handed_off:
            await gate.release()


# Embeddings endpoint
@router.post(
    "/v1/embeddings",
    dependencies=[Depends(check_api_key), Depends(check_embeddings_container)],
)
async def embeddings(request: Request, data: EmbeddingsRequest) -> EmbeddingsResponse:
    embeddings_task = asyncio.create_task(get_embeddings(data, request))
    response = await run_with_request_disconnect(
        request,
        embeddings_task,
        f"Embeddings request {request.state.id} cancelled",
    )

    return response
