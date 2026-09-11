"""Public FastAPI app: UI, stack routes, chat intercepts, Tabby proxy."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from sidecar.paths import ensure_import_path

ensure_import_path()

from sidecar.backend_key import ensure_backend_key
from sidecar.chat import handle_chat_completion
from sidecar.proxy import forward

_PUBLIC_PROXY = frozenset({"health", ".well-known/serviceinfo"})


@asynccontextmanager
async def _lifespan(app: FastAPI):
    ensure_backend_key()
    try:
        from common import auth as api_auth

        if api_auth.AUTH_KEYS is None and not api_auth.DISABLE_AUTH:
            from common.tabby_config import config

            disable = bool(getattr(getattr(config, "network", None), "disable_auth", False))
            await api_auth.load_auth_keys(disable)
    except Exception:
        pass
    yield


def _stack_router():
    from endpoints.core.router import router as CoreRouter

    from fastapi import APIRouter

    stack = APIRouter()
    keep = ("/v1/gpu", "/v1/images")
    for route in CoreRouter.routes:
        path = getattr(route, "path", "")
        if path.startswith(keep):
            stack.routes.append(route)
    return stack


def create_app(*, mount_stack: bool = True, intercept_chat: bool = True) -> FastAPI:
    app = FastAPI(title="tabbyapi-stack sidecar", lifespan=_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from common.auth import check_api_key, get_key_permission
    from common.errors import ContextLengthHTTPException, context_length_exception_handler

    app.add_exception_handler(ContextLengthHTTPException, context_length_exception_handler)

    @app.middleware("http")
    async def saver_note_generate_posts(request, call_next):
        from common.live_decode import hold, is_generate_post, release

        key = ""
        if is_generate_post(getattr(request, "method", ""), str(request.url.path)):
            key = f"http:{id(request)}"
            hold(key)
        streamed = False
        try:
            response = await call_next(request)
            iterator = getattr(response, "body_iterator", None)
            if key and iterator is not None:
                streamed = True

                async def wrapped():
                    try:
                        async for chunk in iterator:
                            yield chunk
                    finally:
                        release(key)

                response.body_iterator = wrapped()
            return response
        finally:
            if key and not streamed:
                release(key)

    @app.get("/v1/auth/permission", dependencies=[Depends(check_api_key)])
    async def auth_permission(request: Request):
        return {"permission": get_key_permission(request)}

    @app.post("/v1/chat/completions", dependencies=[Depends(check_api_key)])
    async def chat_completions(request: Request):
        if intercept_chat:
            return await handle_chat_completion(request)
        return await forward(request)

    @app.post("/v1/completions", dependencies=[Depends(check_api_key)])
    async def completions(request: Request):
        return await forward(request)

    @app.post("/v1/embeddings", dependencies=[Depends(check_api_key)])
    async def embeddings(request: Request):
        return await forward(request)

    if mount_stack:
        from endpoints.core.mcp import router as McpRouter
        from ui.router import legacy_router as UiLegacyRouter
        from ui.router import router as UiRouter

        app.include_router(_stack_router())
        app.include_router(McpRouter)
        app.include_router(UiRouter)
        app.include_router(UiLegacyRouter)

        try:
            from ui.manager import ensure_gpu_cache
            from ui.metrics import ensure_metrics_sampler

            ensure_metrics_sampler()
            ensure_gpu_cache()
        except Exception:
            pass

    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def proxy_rest(full_path: str, request: Request):
        from common.auth import check_api_key as require_key

        if full_path not in _PUBLIC_PROXY:
            await require_key(
                x_api_key=request.headers.get("x-api-key"),
                authorization=request.headers.get("authorization"),
            )
        return await forward(request)

    return app


app: Optional[FastAPI] = None


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app
