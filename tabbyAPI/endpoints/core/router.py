import asyncio
import pathlib
from typing import Optional
from common.multimodal import MultimodalEmbeddingWrapper
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sse_starlette import EventSourceResponse

from common import model, sampling
from common.auth import check_admin_key, check_api_key, get_key_permission
from common.downloader import hf_repo_download
from common.model import check_embeddings_container, check_model_container
from common.networking import (
    get_sse_ping_interval,
    handle_request_error,
    run_with_request_disconnect,
)
from common.tabby_config import config
from common.templating import PromptTemplate, get_all_templates
from common.utils import unwrap
from common.health import HealthManager
from endpoints.OAI.utils.chat_completion import format_messages_with_template
from endpoints.core.types.auth import AuthPermissionResponse
from endpoints.core.types.download import DownloadRequest, DownloadResponse
from endpoints.core.types.lora import LoraList, LoraLoadRequest, LoraLoadResponse
from endpoints.core.types.model import (
    EmbeddingModelLoadRequest,
    ModelCard,
    ModelDefaultGenerationSettings,
    ModelList,
    ModelLoadRequest,
    ModelLoadResponse,
    ModelPropsResponse,
)
from endpoints.core.types.gpu import (
    GpuModeRequest,
    GpuModeResponse,
    ImageGenerationRequest,
    ImageGenerationResponse,
)
from endpoints.core.types.health import HealthCheckResponse
from endpoints.core.types.sampler_overrides import (
    SamplerOverrideListResponse,
    SamplerOverrideSwitchRequest,
)
from endpoints.core.types.template import TemplateList, TemplateSwitchRequest
from endpoints.core.types.token import (
    TokenDecodeRequest,
    TokenDecodeResponse,
    TokenEncodeRequest,
    TokenEncodeResponse,
)
from endpoints.core.utils.lora import get_active_loras, get_lora_list
from endpoints.core.utils.model import (
    get_current_model,
    get_current_model_list,
    get_dummy_models,
    get_model_list,
    stream_model_load,
)


router = APIRouter()


# Healthcheck endpoint
@router.get("/health")
async def healthcheck(response: Response) -> HealthCheckResponse:
    """Get the current service health status"""
    healthy, issues = await HealthManager.is_service_healthy()

    if not healthy:
        response.status_code = 503

    return HealthCheckResponse(status="healthy" if healthy else "unhealthy", issues=issues)


@router.get("/.well-known/serviceinfo")
async def service_info():
    return JSONResponse(
        content={
            "version": 0.1,
            "software": {
                "name": "TabbyAPI",
                "repository": "https://github.com/theroyallab/tabbyAPI",
                "homepage": "https://github.com/theroyallab/tabbyAPI",
            },
            "api": {
                "openai": {
                    "name": "OpenAI API",
                    "relative_url": "/v1",
                    "documentation": "https://theroyallab.github.io/tabbyAPI",
                    "version": 1,
                },
                "koboldai": {
                    "name": "KoboldAI API",
                    "relative_url": "/api",
                    "documentation": "https://theroyallab.github.io/tabbyAPI",
                    "version": 1,
                },
            },
        }
    )


# Model list endpoint
@router.get("/v1/models", dependencies=[Depends(check_api_key)])
@router.get("/v1/model/list", dependencies=[Depends(check_api_key)])
async def list_models(request: Request) -> ModelList:
    """
    Lists all models in the model directory.

    Requires an admin key to see all models.
    """

    model_dir = config.model.model_dir
    model_path = pathlib.Path(model_dir)

    draft_model_dir = config.draft_model.draft_model_dir

    if get_key_permission(request) == "admin":
        models = get_model_list(model_path.resolve(), draft_model_dir)
    else:
        models = await get_current_model_list()

    if config.model.use_dummy_models:
        models.data[:0] = get_dummy_models()

    return models


# Currently loaded model endpoint
@router.get(
    "/v1/model",
    dependencies=[Depends(check_api_key), Depends(check_model_container)],
)
async def current_model() -> ModelCard:
    """Returns the currently loaded model."""

    return get_current_model()


@router.get("/props", dependencies=[Depends(check_api_key), Depends(check_model_container)])
async def model_props() -> ModelPropsResponse:
    """
    Returns specific properties of a model for clients.

    To get all properties, use /v1/model instead.
    """

    current_model_card = get_current_model()
    resp = ModelPropsResponse(
        total_slots=current_model_card.parameters.max_batch_size,
        default_generation_settings=ModelDefaultGenerationSettings(
            n_ctx=current_model_card.parameters.max_seq_len,
        ),
    )

    if current_model_card.parameters.prompt_template_content:
        resp.chat_template = current_model_card.parameters.prompt_template_content

    return resp


@router.get("/v1/model/draft/list", dependencies=[Depends(check_api_key)])
async def list_draft_models(request: Request) -> ModelList:
    """
    Lists all draft models in the model directory.

    Requires an admin key to see all draft models.
    """

    if get_key_permission(request) == "admin":
        draft_model_dir = config.draft_model.draft_model_dir
        draft_model_path = pathlib.Path(draft_model_dir)

        models = get_model_list(draft_model_path.resolve())
    else:
        models = await get_current_model_list(model_type="draft")

    return models


# Load model endpoint
@router.post("/v1/model/load", dependencies=[Depends(check_admin_key)])
async def load_model(data: ModelLoadRequest) -> ModelLoadResponse:
    """Loads a model into the model container. This returns an SSE stream."""

    # Verify request parameters
    if not data.model_name:
        error_message = handle_request_error(
            "A model name was not provided for load.",
            exc_info=False,
        ).error.message

        raise HTTPException(400, error_message)

    model_path = pathlib.Path(config.model.model_dir)
    model_path = model_path / data.model_name

    if not model_path.exists():
        error_message = handle_request_error(
            "Could not find the model path for load. Check model name or config.yml?",
            exc_info=False,
        ).error.message

        raise HTTPException(400, error_message)

    return EventSourceResponse(stream_model_load(data, model_path), ping=get_sse_ping_interval())


# Unload model endpoint
@router.post(
    "/v1/model/unload",
    dependencies=[Depends(check_admin_key), Depends(check_model_container)],
)
async def unload_model():
    """Unloads the currently loaded model."""
    await model.unload_model(skip_wait=True)


@router.post("/v1/download", dependencies=[Depends(check_admin_key)])
async def download_model(request: Request, data: DownloadRequest) -> DownloadResponse:
    """Downloads a model from HuggingFace."""

    try:
        download_task = asyncio.create_task(hf_repo_download(**data.model_dump()))

        # For now, the downloader and request data are 1:1
        download_path = await run_with_request_disconnect(
            request,
            download_task,
            "Download request cancelled by user. Files have been cleaned up.",
        )

        return DownloadResponse(download_path=str(download_path))
    except Exception as exc:
        error_message = handle_request_error(str(exc)).error.message

        raise HTTPException(400, error_message) from exc


# Lora list endpoint
@router.get("/v1/loras", dependencies=[Depends(check_api_key)])
@router.get("/v1/lora/list", dependencies=[Depends(check_api_key)])
async def list_all_loras(request: Request) -> LoraList:
    """
    Lists all LoRAs in the lora directory.

    Requires an admin key to see all LoRAs.
    """

    if get_key_permission(request) == "admin":
        lora_path = pathlib.Path(config.lora.lora_dir)
        loras = get_lora_list(lora_path.resolve())
    else:
        loras = get_active_loras()

    return loras


# Currently loaded loras endpoint
@router.get(
    "/v1/lora",
    dependencies=[Depends(check_api_key), Depends(check_model_container)],
)
async def active_loras() -> LoraList:
    """Returns the currently loaded loras."""

    return get_active_loras()


# Load lora endpoint
@router.post(
    "/v1/lora/load",
    dependencies=[Depends(check_admin_key), Depends(check_model_container)],
)
async def load_lora(data: LoraLoadRequest) -> LoraLoadResponse:
    """Loads a LoRA into the model container."""

    if not data.loras:
        error_message = handle_request_error(
            "List of loras to load is not found.",
            exc_info=False,
        ).error.message

        raise HTTPException(400, error_message)

    lora_dir = pathlib.Path(config.lora.lora_dir)
    if not lora_dir.exists():
        error_message = handle_request_error(
            "A parent lora directory does not exist for load. Check your config.yml?",
            exc_info=False,
        ).error.message

        raise HTTPException(400, error_message)

    load_result = await model.load_loras(lora_dir, **data.model_dump(), skip_wait=data.skip_queue)

    return LoraLoadResponse(
        success=unwrap(load_result.get("success"), []),
        failure=unwrap(load_result.get("failure"), []),
    )


# Unload lora endpoint
@router.post(
    "/v1/lora/unload",
    dependencies=[Depends(check_admin_key), Depends(check_model_container)],
)
async def unload_loras():
    """Unloads the currently loaded loras."""

    await model.unload_loras()


@router.get("/v1/model/embedding/list", dependencies=[Depends(check_api_key)])
async def list_embedding_models(request: Request) -> ModelList:
    """
    Lists all embedding models in the model directory.

    Requires an admin key to see all embedding models.
    """

    if get_key_permission(request) == "admin":
        embedding_model_dir = config.embeddings.embedding_model_dir
        embedding_model_path = pathlib.Path(embedding_model_dir)

        models = get_model_list(embedding_model_path.resolve())
    else:
        models = await get_current_model_list(model_type="embedding")

    return models


@router.get(
    "/v1/model/embedding",
    dependencies=[Depends(check_api_key), Depends(check_embeddings_container)],
)
async def get_embedding_model() -> ModelCard:
    """Returns the currently loaded embedding model."""
    models = await get_current_model_list(model_type="embedding")

    return models.data[0]


@router.post("/v1/model/embedding/load", dependencies=[Depends(check_admin_key)])
async def load_embedding_model(
    request: Request, data: EmbeddingModelLoadRequest
) -> ModelLoadResponse:
    # Verify request parameters
    if not data.embedding_model_name:
        error_message = handle_request_error(
            "A model name was not provided for load.",
            exc_info=False,
        ).error.message

        raise HTTPException(400, error_message)

    embedding_model_dir = pathlib.Path(config.embeddings.embedding_model_dir)
    embedding_model_path = embedding_model_dir / data.embedding_model_name

    if not embedding_model_path.exists():
        error_message = handle_request_error(
            "Could not find the embedding model path for load. "
            + "Check model name or config.yml?",
            exc_info=False,
        ).error.message

        raise HTTPException(400, error_message)

    try:
        load_task = asyncio.create_task(
            model.load_embedding_model(embedding_model_path, **data.model_dump())
        )
        await run_with_request_disconnect(
            request, load_task, "Embedding model load request cancelled by user."
        )
    except Exception as exc:
        error_message = handle_request_error(str(exc)).error.message

        raise HTTPException(400, error_message) from exc

    response = ModelLoadResponse(
        model_type="embedding_model", module=1, modules=1, status="finished"
    )

    return response


@router.post(
    "/v1/model/embedding/unload",
    dependencies=[Depends(check_admin_key), Depends(check_embeddings_container)],
)
async def unload_embedding_model():
    """Unloads the current embedding model."""

    await model.unload_embedding_model()


# Encode tokens endpoint
@router.post(
    "/v1/token/encode",
    dependencies=[Depends(check_api_key), Depends(check_model_container)],
)
async def encode_tokens(data: TokenEncodeRequest) -> TokenEncodeResponse:
    """Encodes a string or chat completion messages into tokens."""

    mm_embeddings: Optional[MultimodalEmbeddingWrapper] = None

    if isinstance(data.text, str):
        text = data.text
    elif isinstance(data.text, list):
        if "oai" not in config.network.api_servers:
            error_message = handle_request_error(
                "Enable the OAI server to handle chat completion messages.",
                exc_info=False,
            ).error.message

            raise HTTPException(422, error_message)

        if not model.container.prompt_template:
            error_message = handle_request_error(
                "Cannot tokenize chat completion message because "
                + "a prompt template is not set.",
                exc_info=False,
            ).error.message

            raise HTTPException(422, error_message)

        template_vars = {
            **(data.template_vars or {}),
            "add_generation_prompt": False,
        }

        text, mm_embeddings, rendered_template_vars = await format_messages_with_template(
            data.text, template_vars
        )

        # Let encode_tokens be the sole authority on whether BOS is added.
        bos_token = rendered_template_vars.get("bos_token")
        if bos_token and text.startswith(bos_token):
            text = text.removeprefix(bos_token)
    else:
        error_message = handle_request_error(
            "Unable to tokenize the provided text. Check your formatting?",
            exc_info=False,
        ).error.message

        raise HTTPException(422, error_message)

    raw_tokens = model.container.encode_tokens(text, embeddings=mm_embeddings, **data.get_params())
    tokens = unwrap(raw_tokens, [])
    response = TokenEncodeResponse(tokens=tokens, length=len(tokens))

    return response


# Decode tokens endpoint
@router.post(
    "/v1/token/decode",
    dependencies=[Depends(check_api_key), Depends(check_model_container)],
)
async def decode_tokens(data: TokenDecodeRequest) -> TokenDecodeResponse:
    """Decodes tokens into a string."""

    message = model.container.decode_tokens(data.tokens, **data.get_params())
    response = TokenDecodeResponse(text=unwrap(message, ""))

    return response


@router.get("/v1/auth/permission", dependencies=[Depends(check_api_key)])
async def key_permission(request: Request) -> AuthPermissionResponse:
    """
    Gets the access level/permission of a provided key in headers.

    Priority:
    - X-admin-key
    - X-api-key
    - Authorization
    """

    try:
        permission = get_key_permission(request)
        return AuthPermissionResponse(permission=permission)
    except ValueError as exc:
        error_message = handle_request_error(str(exc)).error.message

        raise HTTPException(400, error_message) from exc


@router.get("/v1/templates", dependencies=[Depends(check_api_key)])
@router.get("/v1/template/list", dependencies=[Depends(check_api_key)])
async def list_templates(request: Request) -> TemplateList:
    """
    Get a list of all templates.

    Requires an admin key to see all templates.
    """

    template_strings = []
    if get_key_permission(request) == "admin":
        templates = get_all_templates()
        template_strings = [template.stem for template in templates]
    else:
        if model.container and model.container.prompt_template:
            template_strings.append(model.container.prompt_template.name)

    return TemplateList(data=template_strings)


@router.post(
    "/v1/template/switch",
    dependencies=[Depends(check_admin_key), Depends(check_model_container)],
)
async def switch_template(data: TemplateSwitchRequest):
    """Switch the currently loaded template."""

    if not data.prompt_template_name:
        error_message = handle_request_error(
            "New template name not found.",
            exc_info=False,
        ).error.message

        raise HTTPException(400, error_message)

    try:
        template_path = pathlib.Path("templates") / data.prompt_template_name
        model.container.prompt_template = await PromptTemplate.from_file(template_path)
    except FileNotFoundError as e:
        error_message = handle_request_error(
            f"The template name {data.prompt_template_name} doesn't exist. "
            + "Check the spelling?",
            exc_info=False,
        ).error.message

        raise HTTPException(400, error_message) from e


@router.post(
    "/v1/template/unload",
    dependencies=[Depends(check_admin_key), Depends(check_model_container)],
)
async def unload_template():
    """Unloads the currently selected template"""

    model.container.prompt_template = None


# Sampler override endpoints
@router.get("/v1/sampling/overrides", dependencies=[Depends(check_api_key)])
@router.get("/v1/sampling/override/list", dependencies=[Depends(check_api_key)])
async def list_sampler_overrides(request: Request) -> SamplerOverrideListResponse:
    """
    List all currently applied sampler overrides.

    Requires an admin key to see all override presets.
    """

    if get_key_permission(request) == "admin":
        presets = sampling.get_all_presets()
    else:
        presets = []

    return SamplerOverrideListResponse(presets=presets, **sampling.overrides_container.model_dump())


@router.post(
    "/v1/sampling/override/switch",
    dependencies=[Depends(check_admin_key)],
)
async def switch_sampler_override(data: SamplerOverrideSwitchRequest):
    """Switch the currently loaded override preset"""

    if data.preset:
        try:
            await sampling.overrides_from_file(data.preset)
        except FileNotFoundError as e:
            error_message = handle_request_error(
                f"Sampler override preset with name {data.preset} does not exist. "
                + "Check the spelling?",
                exc_info=False,
            ).error.message

            raise HTTPException(400, error_message) from e
    elif data.overrides:
        sampling.overrides_from_dict(data.overrides)
    else:
        error_message = handle_request_error(
            "A sampler override preset or dictionary wasn't provided.",
            exc_info=False,
        ).error.message

        raise HTTPException(400, error_message)


@router.post(
    "/v1/sampling/override/unload",
    dependencies=[Depends(check_admin_key)],
)
async def unload_sampler_override():
    """Unloads the currently selected override preset"""

    sampling.overrides_from_dict({})


def _loaded_tabby_name() -> Optional[str]:
    from endpoints.core.image_jobs import loaded_tabby_name

    return loaded_tabby_name()


@router.get("/v1/gpu/mode", dependencies=[Depends(check_api_key)])
async def get_gpu_mode() -> GpuModeResponse:
    """Report whether the GPU is owned by the LLM or ComfyUI."""
    from common.gpu_mode import comfy_up, read_mode

    tabby_model = _loaded_tabby_name()
    status = read_mode()
    mode = "llm" if tabby_model else status.get("mode") or "llm"
    return GpuModeResponse(
        mode=mode,
        tabby_model=tabby_model,
        comfy_up=comfy_up(),
    )


@router.post("/v1/gpu/mode", dependencies=[Depends(check_admin_key)])
async def set_gpu_mode(data: GpuModeRequest) -> GpuModeResponse:
    """Hand the GPU to ComfyUI or load a TabbyAPI profile. Exclusive."""
    from common.gpu_mode import GPU_ALIASES, comfy_up
    from endpoints.core.image_jobs import ensure_comfy, loaded_tabby_name, reload_last_llm
    from select_model import available_profiles, last_profile, profile_aliases

    token = (data.mode or "").strip().lower()
    if not token:
        raise HTTPException(400, "mode is required")

    if token in GPU_ALIASES:
        try:
            await ensure_comfy()
        except (SystemExit, RuntimeError) as exc:
            raise HTTPException(500, str(exc)) from exc
        return GpuModeResponse(
            mode="comfy",
            tabby_model=None,
            comfy_up=comfy_up(),
            message="GPU handed to ComfyUI. Generate via chat or POST /v1/images/generations.",
        )

    names = available_profiles()
    aliases = profile_aliases()
    if token == "llm":
        name = last_profile() if last_profile() in names else (names[0] if names else None)
    else:
        name = aliases.get(token) or aliases.get(data.mode.strip())
    if not name:
        raise HTTPException(400, f"Unknown mode {data.mode!r}")

    try:
        await reload_last_llm(name)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return GpuModeResponse(
        mode="llm",
        tabby_model=loaded_tabby_name(),
        comfy_up=comfy_up(),
        message=f"GPU handed to TabbyAPI ({name})",
    )


@router.post("/v1/images/generations", dependencies=[Depends(check_api_key)])
async def images_generations(
    request: Request, data: ImageGenerationRequest
) -> ImageGenerationResponse:
    """OpenAI-shaped image gen via ComfyUI.

    If an LLM owns the GPU, it is unloaded, the image is generated, and the
    last LLM is reloaded (unless restore=false). Remote clients use this
    endpoint or chat; they never talk to ComfyUI.
    """
    from images.http import generate_response

    return await generate_response(request, data)


def _gallery_pager(page: int, pages: int, per_page: int) -> str:
    def href(n: int) -> str:
        query = f"?page={n}"
        if per_page != 24:
            query += f"&per_page={per_page}"
        return query

    def link(n: int, label: str, enabled: bool = True) -> str:
        if not enabled:
            return f'<span class="off">{label}</span>'
        return f'<a href="{href(n)}">{label}</a>'

    numbers = []
    for n in range(1, pages + 1):
        near = abs(n - page) <= 2
        edge = n == 1 or n == pages
        if not (near or edge):
            if numbers and numbers[-1] != '<span class="off">…</span>':
                numbers.append('<span class="off">…</span>')
            continue
        if n == page:
            numbers.append(f'<span class="cur">{n}</span>')
        else:
            numbers.append(link(n, str(n)))
    return (
        '<nav class="pager">'
        + link(1, "First", page > 1)
        + link(page - 1, "Prev", page > 1)
        + "".join(numbers)
        + link(page + 1, "Next", page < pages)
        + link(pages, "Last", page < pages)
        + f'<span class="meta">Page {page} of {pages}</span>'
        + "</nav>"
    )


@router.get("/v1/images", dependencies=[Depends(check_api_key)])
@router.get("/v1/images/", dependencies=[Depends(check_api_key)])
async def generated_images_index(page: int = 1, per_page: int = 24):
    """Browser gallery of generated Flux PNGs."""
    import html
    from datetime import datetime, timezone

    from common.gpu_mode import gallery_page, gallery_thumb_href, list_generated_files

    files = list_generated_files()
    shown, page, pages, per_page = gallery_page(files, page, per_page)
    cards = []
    for index, path in enumerate(shown):
        name = html.escape(path.name)
        thumb = html.escape(gallery_thumb_href(path.name))
        try:
            stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            when = stamp.strftime("%Y-%m-%d %H:%M UTC")
        except OSError:
            when = ""
        cards.append(
            f'<figure data-name="{name}" data-index="{index}">'
            f'<span class="pick"><input type="checkbox" aria-label="Select {name}"></span>'
            f'<a class="open" href="{name}" data-full="{name}">'
            f'<img src="{thumb}" alt="{name}" loading="lazy" decoding="async"></a>'
            f"<figcaption>{name}<br>{html.escape(when)}</figcaption>"
            "</figure>"
        )
    body = (
        "\n".join(cards)
        if cards
        else "<p>No generated images yet. Send switch to comfy, then a prompt.</p>"
    )
    pager = _gallery_pager(page, pages, per_page) if files else ""
    toolbar = (
        '<div class="bar">'
        '<span id="sel-count">0 selected</span>'
        '<button type="button" id="del-sel" disabled>Delete selected</button>'
        '<button type="button" id="del-all" class="danger">Delete all</button>'
        "</div>"
        if files
        else ""
    )
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Generated images</title>"
        "<style>"
        "body{font-family:system-ui,sans-serif;margin:1.5rem;background:#111;color:#eee}"
        "h1{font-size:1.2rem} .count{color:#aaa;font-weight:normal}"
        ".bar{display:flex;flex-wrap:wrap;align-items:center;gap:.6rem;margin:0 0 1rem}"
        ".bar button{padding:.4rem .75rem;border:0;border-radius:6px;background:#345;"
        "color:#fff;cursor:pointer}"
        ".bar button:hover{background:#456}"
        ".bar button:disabled{background:#333;color:#777;cursor:not-allowed}"
        ".bar button.danger{background:#622}"
        ".bar button.danger:hover{background:#833}"
        ".bar #sel-count{color:#aaa}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:1rem}"
        "figure{margin:0;background:#1c1c1c;border-radius:8px;overflow:hidden;position:relative}"
        "figure.is-on{outline:2px solid #6af;outline-offset:2px}"
        ".pick{position:absolute;top:.45rem;right:.45rem;z-index:2;margin:0;"
        "width:1.35rem;height:1.35rem;display:flex;align-items:center;justify-content:center;"
        "background:#111c;border-radius:4px}"
        ".pick input{width:1.1rem;height:1.1rem;margin:0;cursor:pointer;accent-color:#6af}"
        ".grid img{display:block;width:100%;height:240px;object-fit:cover;background:#000}"
        ".grid a.open{display:block;cursor:zoom-in}"
        "figcaption{padding:.6rem .75rem;font-size:.8rem;word-break:break-all;color:#ccc}"
        "a{color:#9cf}"
        ".pager{display:flex;flex-wrap:wrap;align-items:center;gap:.4rem;margin:1rem 0}"
        ".pager a,.pager span{padding:.35rem .65rem;border-radius:6px;background:#1c1c1c;"
        "text-decoration:none;color:#eee}"
        ".pager a:hover{background:#2a2a2a}"
        ".pager .cur{background:#345;font-weight:600}"
        ".pager .off{color:#666}"
        ".pager .meta{background:transparent;color:#aaa}"
        "#modal{position:fixed;inset:0;z-index:20;display:none;align-items:center;"
        "justify-content:center;padding:1.5rem}"
        "#modal.is-open{display:flex}"
        "#modal .backdrop{position:absolute;inset:0;border:0;padding:0;background:#000c;"
        "cursor:zoom-out}"
        "#modal .sheet{position:relative;z-index:1;max-width:96vw;max-height:94vh;"
        "display:flex;flex-direction:column;align-items:center;gap:.6rem}"
        "#modal img{max-width:96vw;max-height:86vh;object-fit:contain;background:#000;"
        "border-radius:8px}"
        "#modal .caption{margin:0;color:#ccc;font-size:.85rem;word-break:break-all}"
        "#modal .close{position:absolute;top:.4rem;right:.4rem;z-index:2;width:2.2rem;"
        "height:2.2rem;border:0;border-radius:999px;background:#222;color:#fff;"
        "font-size:1.4rem;line-height:1;cursor:pointer}"
        "#modal .close:hover{background:#333}"
        "</style></head><body>"
        f"<h1>Generated images <span class='count'>({len(files)})</span></h1>"
        f"{toolbar}"
        f"{pager}"
        f'<div class="grid">{body}</div>'
        f"{pager}"
        '<div id="modal" role="dialog" aria-modal="true" aria-label="Image preview">'
        '<button type="button" class="backdrop" aria-label="Close"></button>'
        '<div class="sheet">'
        '<button type="button" class="close" aria-label="Close">&times;</button>'
        '<img alt="">'
        '<p class="caption"></p>'
        "</div></div>"
        "<script>"
        "(function(){"
        "var modal=document.getElementById('modal');"
        "var full=modal.querySelector('img');"
        "var cap=modal.querySelector('.caption');"
        "var boxes=[].slice.call(document.querySelectorAll('.pick input'));"
        "var last=0;"
        "var count=document.getElementById('sel-count');"
        "var delSel=document.getElementById('del-sel');"
        "function selected(){"
        "return boxes.filter(function(b){return b.checked;})"
        ".map(function(b){return b.closest('figure').getAttribute('data-name');});"
        "}"
        "function paint(){"
        "boxes.forEach(function(b){"
        "b.closest('figure').classList.toggle('is-on',b.checked);"
        "});"
        "var n=selected().length;"
        "if(count)count.textContent=n+' selected';"
        "if(delSel)delSel.disabled=!n;"
        "}"
        "boxes.forEach(function(box,i){"
        "box.addEventListener('click',function(e){"
        "e.stopPropagation();"
        "if(e.shiftKey){"
        "e.preventDefault();"
        "var a=Math.min(last,i),z=Math.max(last,i);"
        "for(var j=a;j<=z;j++)boxes[j].checked=true;"
        "}"
        "last=i;paint();"
        "});"
        "box.addEventListener('change',paint);"
        "});"
        "function wipe(body){"
        "return fetch('delete',{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify(body)}).then(function(r){"
        "if(!r.ok)return r.text().then(function(t){throw new Error(t||r.status);});"
        "location.reload();"
        "}).catch(function(err){alert('Delete failed: '+err);});"
        "}"
        "if(delSel)delSel.addEventListener('click',function(){"
        "var names=selected();"
        "if(!names.length)return;"
        "if(!confirm('Delete '+names.length+' selected image(s)?'))return;"
        "wipe({names:names});"
        "});"
        "var delAll=document.getElementById('del-all');"
        "if(delAll)delAll.addEventListener('click',function(){"
        "if(!confirm('Delete ALL generated images?'))return;"
        "wipe({all:true});"
        "});"
        "function open(name){"
        "full.src=name;full.alt=name;cap.textContent=name;"
        "modal.classList.add('is-open');document.body.style.overflow='hidden';"
        "}"
        "function close(){"
        "modal.classList.remove('is-open');full.removeAttribute('src');"
        "document.body.style.overflow='';"
        "}"
        "document.querySelector('.grid').addEventListener('click',function(e){"
        "if(e.target.closest('.pick'))return;"
        "var a=e.target.closest('a.open');"
        "if(!a)return;e.preventDefault();open(a.getAttribute('data-full'));"
        "});"
        "modal.addEventListener('click',function(e){"
        "if(e.target.classList.contains('backdrop')||e.target.classList.contains('close'))"
        "close();"
        "});"
        "document.addEventListener('keydown',function(e){"
        "if(e.key==='Escape'&&modal.classList.contains('is-open'))close();"
        "});"
        "paint();"
        "})();"
        "</script>"
        "</body></html>"
    )


@router.post("/v1/images/delete", dependencies=[Depends(check_api_key)])
async def delete_generated_image_files(request: Request):
    """Delete selected generated-*.png files, or every gallery image."""
    from common.gpu_mode import delete_generated_images

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    wipe_all = bool(body.get("all"))
    names = body.get("names") if isinstance(body.get("names"), list) else []
    if not wipe_all and not names:
        raise HTTPException(400, "Provide names or all=true")
    removed = delete_generated_images(names, delete_all=wipe_all)
    return {"deleted": removed, "count": len(removed)}


@router.get("/v1/images/jobs", dependencies=[Depends(check_api_key)])
@router.get("/v1/images/jobs/", dependencies=[Depends(check_api_key)])
async def latest_image_job():
    """JSON status for the latest MCP/Comfy image job (stdio saver polls this)."""
    from endpoints.core.image_jobs import get_mcp_image_job, mcp_job_to_dict

    job = get_mcp_image_job()
    if not job:
        raise HTTPException(404, "No image job found")
    return mcp_job_to_dict(job)


@router.get("/v1/images/jobs/{job_id}", dependencies=[Depends(check_api_key)])
async def image_job_status(job_id: str):
    """JSON status for one MCP/Comfy image job."""
    from endpoints.core.image_jobs import get_mcp_image_job, mcp_job_to_dict

    job = get_mcp_image_job(job_id)
    if not job:
        raise HTTPException(404, "No image job found")
    return mcp_job_to_dict(job)


@router.get("/v1/images/latest", dependencies=[Depends(check_api_key)])
@router.get("/v1/images/latest.png", dependencies=[Depends(check_api_key)])
async def latest_generated_image():
    """Serve the last Flux PNG so a remote client can download it."""
    from common.gpu_mode import generated_image_path

    path = generated_image_path("generated-latest.png")
    if not path:
        raise HTTPException(404, "No generated image yet.")
    return FileResponse(path, media_type="image/png", filename="generated.png")


@router.get("/v1/images/pasted/{name}", dependencies=[Depends(check_api_key)])
async def pasted_chat_image(name: str):
    """Serve a chat-pasted image so a remote client can fetch it over HTTP."""
    from common.pasted_images import pasted_image_path

    path = pasted_image_path(name)
    if not path:
        raise HTTPException(404, "Pasted image not found.")
    media = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return FileResponse(path, media_type=media, filename=path.name)


@router.get("/v1/images/thumbs/{name}", dependencies=[Depends(check_api_key)])
async def generated_image_thumb(name: str):
    """Serve a small JPEG preview; build it on first request."""
    from common.gpu_mode import generated_image_path, generated_thumb_path

    path = generated_thumb_path(name)
    if path:
        return FileResponse(path, media_type="image/jpeg", filename=path.name)
    png_name = name[: -len(".jpg")] + ".png" if name.endswith(".jpg") else name
    original = generated_image_path(png_name)
    if original:
        return FileResponse(original, media_type="image/png", filename=original.name)
    raise HTTPException(404, "Image not found.")


@router.api_route("/v1/images/{name}", methods=["GET", "HEAD"])
async def generated_image(
    name: str,
    x_api_key: str = Header(None),
    authorization: str = Header(None),
):
    """Serve a generated PNG by filename (generated-*.png only).

    Timestamped gallery files (generated-YYYYMMDD-HHMMSS-PID.png) are public so
    the coding PC can curl them without a bearer. Keep auth on latest.png.
    """
    from common.gpu_mode import generated_image_path, is_public_generated_png

    path = generated_image_path(name)
    if not path:
        raise HTTPException(404, "Image not found.")
    if not is_public_generated_png(name):
        await check_api_key(x_api_key=x_api_key, authorization=authorization)
    return FileResponse(path, media_type="image/png", filename=name)
