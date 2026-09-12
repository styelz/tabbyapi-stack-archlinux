"""Forward HTTP and SSE to the localhost Tabby backend."""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from sidecar.backend_key import ensure_backend_key
from sidecar.settings import backend_url

_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

_CLIENTS: dict[str, httpx.AsyncClient] = {}
_DEFAULT_CLIENT: Optional[httpx.AsyncClient] = None
GENERATE_ONLY_HEADER = "x-tabby-generate-only"


def get_client(base: Optional[str] = None) -> httpx.AsyncClient:
    global _DEFAULT_CLIENT
    url = (base or backend_url()).rstrip("/")
    if base is None:
        if _DEFAULT_CLIENT is None:
            _DEFAULT_CLIENT = httpx.AsyncClient(
                base_url=url,
                timeout=httpx.Timeout(None),
            )
        return _DEFAULT_CLIENT
    client = _CLIENTS.get(url)
    if client is None:
        client = httpx.AsyncClient(base_url=url, timeout=httpx.Timeout(None))
        _CLIENTS[url] = client
    return client


def reset_client() -> None:
    global _DEFAULT_CLIENT, _CLIENTS
    _DEFAULT_CLIENT = None
    _CLIENTS = {}


def set_client(client: Optional[httpx.AsyncClient]) -> None:
    global _DEFAULT_CLIENT
    _DEFAULT_CLIENT = client


def _outgoing_headers(request: Request, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    key = ensure_backend_key()
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        if name.lower() in _HOP:
            continue
        if name.lower() in ("authorization", "x-api-key", "x-admin-key"):
            continue
        headers[name] = value
    headers["authorization"] = f"Bearer {key}"
    headers["x-api-key"] = key
    if extra:
        headers.update(extra)
    return headers


async def forward(
    request: Request,
    *,
    path: Optional[str] = None,
    body: Optional[bytes] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Response:
    """Replay the request at the backend, swapping in the internal key."""
    http = client or get_client()
    target = path if path is not None else request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    payload = body if body is not None else await request.body()
    headers = _outgoing_headers(request)
    req = http.build_request(
        request.method,
        target,
        headers=headers,
        content=payload if payload else None,
    )
    backend = await http.send(req, stream=True)
    excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    out_headers = {
        name: value
        for name, value in backend.headers.items()
        if name.lower() not in excluded
    }

    async def chunks() -> Iterable[bytes]:
        try:
            async for chunk in backend.aiter_raw():
                yield chunk
        finally:
            await backend.aclose()

    return StreamingResponse(
        chunks(),
        status_code=backend.status_code,
        headers=out_headers,
        media_type=backend.headers.get("content-type"),
    )


def llm_forward_client() -> tuple[bool, Optional[httpx.AsyncClient]]:
    """True plus a llama-server client when GGUF owns the GPU."""
    from sidecar.settings import chat_backend_url, llama_url

    target = chat_backend_url()
    llama = False
    try:
        from common.phrase_switch import gpu_is_llama

        llama = gpu_is_llama()
    except Exception:
        llama = target.rstrip("/") == llama_url().rstrip("/")
    if not llama:
        return False, None
    return True, get_client(llama_url())


def _rewrite_llama_stream(result: StreamingResponse) -> StreamingResponse:
    from sidecar.llama_adapter import rewrite_sse_line

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

    return StreamingResponse(
        _rewrite(),
        status_code=result.status_code,
        headers=dict(result.headers),
        media_type=result.media_type,
    )


def _is_http_request(request: Optional[Request]) -> bool:
    """True for a Starlette/FastAPI request. Console flights pass a stand-in."""
    return request is not None and getattr(request, "url", None) is not None


async def forward_llm_chat(
    payload: dict,
    *,
    request: Optional[Request] = None,
    forward_fn: Optional[Callable[..., Any]] = None,
) -> Response:
    """POST /v1/chat/completions at Tabby, or llama-server when GGUF is loaded."""
    import json

    from sidecar.llama_adapter import adapt_chat_payload

    llama, llama_client = llm_forward_client()
    body = adapt_chat_payload(payload) if llama else dict(payload or {})
    raw = json.dumps(body).encode("utf-8")
    client = llama_client if llama else None
    if _is_http_request(request):
        send = forward_fn or forward
        result = await send(
            request,
            path="/v1/chat/completions",
            body=raw,
            client=client,
        )
    else:
        result = await forward_chat(raw, client=client)
    if llama and isinstance(result, StreamingResponse):
        return _rewrite_llama_stream(result)
    return result


async def forward_chat(body: bytes, *, client: Optional[httpx.AsyncClient] = None) -> Response:
    """POST /v1/chat/completions at Tabby. Console jobs have no inbound FastAPI Request."""
    http = client or get_client()
    key = ensure_backend_key()
    headers = {
        "authorization": f"Bearer {key}",
        "x-api-key": key,
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    req = http.build_request(
        "POST",
        "/v1/chat/completions",
        headers=headers,
        content=body,
    )
    backend = await http.send(req, stream=True)
    excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    out_headers = {
        name: value
        for name, value in backend.headers.items()
        if name.lower() not in excluded
    }

    async def chunks() -> Iterable[bytes]:
        try:
            async for chunk in backend.aiter_raw():
                yield chunk
        finally:
            await backend.aclose()

    return StreamingResponse(
        chunks(),
        status_code=backend.status_code,
        headers=out_headers,
        media_type=backend.headers.get("content-type"),
    )


def _generate_headers(*, stream: bool) -> dict[str, str]:
    key = ensure_backend_key()
    return {
        "authorization": f"Bearer {key}",
        "x-api-key": key,
        GENERATE_ONLY_HEADER: "1",
        "content-type": "application/json",
        "accept": "text/event-stream" if stream else "application/json",
    }


async def generate_chat(
    payload: dict,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """Non-streaming completion on Tabby or llama-server, skipping public image intercepts."""
    from sidecar.llama_adapter import adapt_chat_payload

    llama = False
    if client is None:
        llama, llama_client = llm_forward_client()
        if llama:
            client = llama_client
    http = client or get_client()
    body = adapt_chat_payload(payload) if llama else dict(payload or {})
    body["stream"] = False
    response = await http.post(
        "/v1/chat/completions",
        headers=_generate_headers(stream=False),
        json=body,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Backend chat completion returned a non-object.")
    return data


async def generate_chat_stream(
    payload: dict,
    *,
    client: Optional[httpx.AsyncClient] = None,
):
    """Streaming completion on Tabby or llama-server, skipping public image intercepts."""
    from sidecar.llama_adapter import adapt_chat_payload, rewrite_sse_line

    llama = False
    if client is None:
        llama, llama_client = llm_forward_client()
        if llama:
            client = llama_client
    http = client or get_client()
    body = adapt_chat_payload(payload) if llama else dict(payload or {})
    body["stream"] = True
    req = http.build_request(
        "POST",
        "/v1/chat/completions",
        headers=_generate_headers(stream=True),
        json=body,
    )
    backend = await http.send(req, stream=True)
    in_think = False
    try:
        async for line in backend.aiter_lines():
            if llama and line.startswith("data:"):
                line, in_think = rewrite_sse_line(line, in_think=in_think)
            yield line
    finally:
        await backend.aclose()
