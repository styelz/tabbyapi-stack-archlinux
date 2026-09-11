"""Forward HTTP and SSE to the localhost Tabby backend."""

from __future__ import annotations

from typing import Iterable, Optional

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

_DEFAULT_CLIENT: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = httpx.AsyncClient(
            base_url=backend_url(),
            timeout=httpx.Timeout(None),
        )
    return _DEFAULT_CLIENT


def reset_client() -> None:
    global _DEFAULT_CLIENT
    _DEFAULT_CLIENT = None


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
