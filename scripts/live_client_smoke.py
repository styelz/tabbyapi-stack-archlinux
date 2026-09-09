#!/usr/bin/env python3
"""Live smoke for UI Chat, UI Code, and OpenAI/IDE clients against a GPU stack.

Credentials stay in the environment. Defaults hit the LAN API and the HTTPS
reverse-proxy /v1 prefix used by VS Code customendpoint clients.

  TABBY_API_KEY='…' TABBY_UI_USER=pbp TABBY_UI_PASSWORD='…' \\
    python3 scripts/live_client_smoke.py

  TABBY_BASES='http://192.168.1.14:5000/v1 https://git.pbptech.com/openai/v1' \\
    python3 scripts/live_client_smoke.py

  python3 scripts/live_client_smoke.py --skip-images

Env
  TABBY_API_KEY       Bearer token (UI login password)
  TABBY_UI_USER       UI username (default pbp)
  TABBY_UI_PASSWORD   UI password (default TABBY_API_KEY)
  TABBY_BASES         Space-separated /v1 bases
  TABBY_SKIP_IMAGES   Set to 1 to skip the Comfy cycle
  TABBY_IMAGE_BASE    /v1 base that runs POST /images/generations
                      (default: first https:// base, else first base)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any

DEFAULT_BASES = (
    "http://192.168.1.14:5000/v1",
    "https://git.pbptech.com/openai/v1",
)
MODEL = "gpt-4o"
COOKIE_NAME = "tabby_ui"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
DATA_PNG = "data:image/png;base64," + base64.b64encode(PNG_1X1).decode("ascii")
READONLY_HINT = "read-only"


def origin_of(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.port:
        default = 443 if parsed.scheme == "https" else 80
        if parsed.port != default:
            return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    return f"{parsed.scheme}://{parsed.hostname}"


def health_candidates(v1: str) -> list[str]:
    base = v1.rstrip("/")
    urls = [f"{base}/health"]
    if base.endswith("/v1"):
        urls.insert(0, f"{base[:-3]}/health")
    return urls


class Check:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = False
        self.detail = ""
        self.ms = 0.0

    def pass_(self, detail: str = "") -> None:
        self.ok = True
        self.detail = detail

    def fail(self, detail: str) -> None:
        self.ok = False
        self.detail = detail


class Client:
    def __init__(self, v1: str, api_key: str, user: str, password: str) -> None:
        self.v1 = v1.rstrip("/")
        self.origin = origin_of(self.v1)
        self.ui = f"{self.v1}/ui"
        self.api_key = api_key
        self.user = user
        self.password = password
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def _headers(
        self,
        *,
        auth: bool = False,
        json_body: bool = False,
        extra: dict[str, str] | None = None,
        csrf: bool = False,
    ) -> dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream, */*"}
        if json_body:
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if csrf:
            headers["Origin"] = self.origin
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        method: str,
        url: str,
        *,
        auth: bool = False,
        csrf: bool = False,
        body: Any = None,
        timeout: float = 60,
        extra_headers: dict[str, str] | None = None,
        raw: bool = False,
    ) -> tuple[int, bytes, dict[str, str]]:
        data = None
        json_body = False
        if body is not None and not isinstance(body, (bytes, bytearray)):
            data = json.dumps(body).encode("utf-8")
            json_body = True
        elif isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        headers = self._headers(
            auth=auth, json_body=json_body, extra=extra_headers, csrf=csrf
        )
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                payload = resp.read()
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, payload, hdrs
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            hdrs = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
            return exc.code, payload, hdrs

    def json(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> tuple[int, Any, bytes]:
        status, raw, _ = self.request(method, url, **kwargs)
        parsed: Any = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                parsed = None
        return status, parsed, raw

    def snippet(self, raw: bytes, limit: int = 240) -> str:
        text = raw.decode("utf-8", errors="replace").replace("\n", " ").strip()
        return text[:limit]


def parse_sse_text(raw: bytes) -> str:
    parts: list[str] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            parts.append(data)
            continue
        if isinstance(obj, dict) and obj.get("error"):
            err = obj["error"]
            if isinstance(err, dict):
                parts.append(str(err.get("message") or err))
            else:
                parts.append(str(err))
            continue
        choices = obj.get("choices") if isinstance(obj, dict) else None
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        for blob in (delta, message):
            for key in ("content", "reasoning_content"):
                content = blob.get(key)
                if isinstance(content, str) and content:
                    parts.append(content)
            tcs = blob.get("tool_calls")
            if tcs:
                parts.append("[tool_calls]")
    return "".join(parts)


def message_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    msg = choices[0].get("message") or {}
    if not isinstance(msg, dict):
        return ""
    for key in ("content", "reasoning_content"):
        text = msg.get(key)
        if isinstance(text, str) and text.strip():
            return text
    return ""


def chat_body(user: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": user}],
        "max_tokens": extra.pop("max_tokens", 128),
        "template_vars": {"enable_thinking": False},
    }
    body.update(extra)
    return body


def png_fetch_urls(url: str, v1: str) -> list[str]:
    urls: list[str] = []
    if url:
        urls.append(url)
        if url.startswith("/"):
            urls.append(origin_of(v1) + url)
        rewritten = url.replace("/v1/images/", "/openai/v1/images/", 1)
        if rewritten != url:
            urls.append(rewritten)
        name = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        if name:
            urls.append(f"{v1.rstrip('/')}/images/{name}")
    seen: list[str] = []
    for item in urls:
        if item and item not in seen:
            seen.append(item)
    return seen


def ws_try(url: str, origin: str, cookie: str, timeout: float = 8.0) -> tuple[bool, str]:
    parsed = urllib.parse.urlsplit(url)
    ssl_ctx = ssl.create_default_context() if parsed.scheme == "wss" else None
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {parsed.netloc}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Origin: {origin}\r\n"
        f"Cookie: {COOKIE_NAME}={cookie}\r\n"
        "\r\n"
    )
    sock = None
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
        sock = ssl_ctx.wrap_socket(raw, server_hostname=host) if ssl_ctx else raw
        sock.settimeout(timeout)
        sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 8192:
            chunk = sock.recv(1024)
            if not chunk:
                break
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1", errors="replace")
        first = head.splitlines()[0] if head else ""
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC5B2F7").encode()).digest()
        ).decode("ascii")
        if " 101 " not in first:
            return False, first or "no websocket handshake"
        if expected.lower() not in head.lower():
            return True, "101 without matching accept"
        return True, "101 switching protocols"
    except Exception as exc:
        return False, str(exc)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def cookie_value(jar: CookieJar) -> str:
    for cookie in jar:
        if cookie.name == COOKIE_NAME:
            return cookie.value or ""
    return ""


def expect(check: Check, cond: bool, detail: str) -> None:
    if cond:
        check.pass_(detail)
    else:
        check.fail(detail)


def run_base(
    client: Client,
    *,
    do_images: bool,
    results: list[tuple[str, Check]],
) -> None:
    tag = client.v1

    def add(name: str) -> Check:
        check = Check(f"{tag}  {name}")
        results.append((tag, check))
        return check

    # --- OpenAI / IDE ---
    chk = add("GET /health")
    t0 = time.time()
    health_ok = False
    health_detail = ""
    for url in health_candidates(client.v1):
        status, raw, _ = client.request("GET", url, timeout=20)
        health_detail = f"{url} -> {status} {client.snippet(raw)}"
        if status == 200:
            health_ok = True
            break
    chk.ms = (time.time() - t0) * 1000
    expect(chk, health_ok, health_detail)

    chk = add("GET /models")
    status, data, raw = client.json("GET", f"{client.v1}/models", auth=True, timeout=30)
    ids = []
    if isinstance(data, dict):
        ids = [item.get("id") for item in (data.get("data") or []) if isinstance(item, dict)]
    expect(
        chk,
        status == 200 and bool(ids),
        f"{status} ids={ids[:6]!r} {client.snippet(raw) if status != 200 else ''}",
    )

    chk = add("GET /auth/permission")
    status, data, raw = client.json(
        "GET", f"{client.v1}/auth/permission", auth=True, timeout=20
    )
    perm = data.get("permission") if isinstance(data, dict) else None
    expect(chk, status == 200 and perm in ("admin", "api"), f"{status} permission={perm}")

    chk = add("GET /gpu/mode")
    status, data, raw = client.json("GET", f"{client.v1}/gpu/mode", auth=True, timeout=20)
    mode = data.get("mode") if isinstance(data, dict) else None
    expect(chk, status == 200 and bool(mode), f"{status} mode={mode}")

    chk = add("POST /chat/completions (short)")
    t0 = time.time()
    status, data, raw = client.json(
        "POST",
        f"{client.v1}/chat/completions",
        auth=True,
        csrf=False,
        timeout=180,
        body=chat_body("Reply with the single word pong."),
    )
    chk.ms = (time.time() - t0) * 1000
    content = message_text(data)
    expect(
        chk,
        status == 200 and bool(content.strip()),
        f"{status} content={content[:80]!r} {client.snippet(raw) if status != 200 else ''}",
    )

    chk = add("POST /chat/completions SSE")
    t0 = time.time()
    status, raw, _ = client.request(
        "POST",
        f"{client.v1}/chat/completions",
        auth=True,
        timeout=180,
        extra_headers={"Accept": "text/event-stream"},
        body=chat_body("Reply with the single word stream.", stream=True),
    )
    chk.ms = (time.time() - t0) * 1000
    text = parse_sse_text(raw)
    expect(chk, status == 200 and bool(text.strip()), f"{status} sse={text[:80]!r}")

    chk = add("POST /chat/completions tools")
    t0 = time.time()
    status, data, raw = client.json(
        "POST",
        f"{client.v1}/chat/completions",
        auth=True,
        timeout=180,
        body={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": "What is the weather in Paris? Use the get_weather tool.",
                }
            ],
            "max_tokens": 256,
            "template_vars": {"enable_thinking": False},
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get the current weather for a location.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "location": {"type": "string", "description": "City name"}
                            },
                            "required": ["location"],
                        },
                    },
                }
            ],
            "tool_choice": "auto",
        },
    )
    chk.ms = (time.time() - t0) * 1000
    tool_calls = []
    if isinstance(data, dict):
        choices = data.get("choices") or []
        if choices and isinstance(choices[0], dict):
            tool_calls = (choices[0].get("message") or {}).get("tool_calls") or []
    expect(
        chk,
        status == 200 and bool(tool_calls),
        f"{status} tool_calls={json.dumps(tool_calls)[:200]}",
    )

    chk = add("POST /chat/completions vision")
    t0 = time.time()
    status, data, raw = client.json(
        "POST",
        f"{client.v1}/chat/completions",
        auth=True,
        timeout=180,
        body=chat_body(
            "What color is this pixel? One word.",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What color is this pixel? One word."},
                        {"type": "image_url", "image_url": {"url": DATA_PNG}},
                    ],
                }
            ],
        ),
    )
    chk.ms = (time.time() - t0) * 1000
    content = message_text(data)
    expect(
        chk,
        status == 200 and bool(content.strip()),
        f"{status} content={content[:80]!r} {client.snippet(raw) if status != 200 else ''}",
    )

    chk = add("POST /embeddings")
    status, data, raw = client.json(
        "POST",
        f"{client.v1}/embeddings",
        auth=True,
        timeout=60,
        body={"model": MODEL, "input": "live client smoke"},
    )
    vec = None
    if isinstance(data, dict):
        items = data.get("data") or []
        if items and isinstance(items[0], dict):
            vec = items[0].get("embedding")
    expect(
        chk,
        status == 200 and isinstance(vec, list) and len(vec) > 8,
        f"{status} dim={len(vec) if isinstance(vec, list) else None}",
    )

    chk = add("POST /chat/completions unauth 401")
    status, raw, _ = client.request(
        "POST",
        f"{client.v1}/chat/completions",
        timeout=20,
        body={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8},
    )
    expect(chk, status in (401, 403), f"{status} {client.snippet(raw)}")

    if do_images:
        chk = add("POST /images/generations")
        t0 = time.time()
        status, data, raw = client.json(
            "POST",
            f"{client.v1}/images/generations",
            auth=True,
            timeout=600,
            body={"prompt": "a red cube on a white table, simple photo", "n": 1},
        )
        chk.ms = (time.time() - t0) * 1000
        url = ""
        if isinstance(data, dict):
            items = data.get("data") or []
            if items and isinstance(items[0], dict):
                url = str(items[0].get("url") or "")
        png_ok = False
        png_detail = ""
        if url:
            for fetch_url in png_fetch_urls(url, client.v1):
                img_status, img_raw, img_hdrs = client.request("GET", fetch_url, timeout=60)
                ctype = img_hdrs.get("content-type", "")
                png_ok = img_status == 200 and (
                    img_raw[:8] == b"\x89PNG\r\n\x1a\n" or "image/png" in ctype
                )
                png_detail = f"GET {fetch_url} -> {img_status} {ctype} {len(img_raw)}B"
                if png_ok:
                    break
        expect(
            chk,
            status == 200 and bool(url) and png_ok,
            f"{status} url={url} {png_detail} {client.snippet(raw) if status != 200 else ''}",
        )

        chk = add("GPU back on LLM after images")
        mode = ""
        last = ""
        deadline = time.time() + 180
        while time.time() < deadline:
            st, payload, _raw = client.json(
                "GET", f"{client.v1}/gpu/mode", auth=True, timeout=20
            )
            mode = payload.get("mode") if isinstance(payload, dict) else ""
            last = f"{st} mode={mode}"
            if st == 200 and str(mode).lower() not in ("comfy", "flux", "image"):
                break
            time.sleep(3)
        st2, payload2, raw2 = client.json(
            "POST",
            f"{client.v1}/chat/completions",
            auth=True,
            timeout=180,
            body=chat_body("Reply with the single word back."),
        )
        content = message_text(payload2)
        expect(
            chk,
            st2 == 200 and bool(content.strip()),
            f"{last}; completion {st2} {content[:40]!r} {client.snippet(raw2) if st2 != 200 else ''}",
        )

    # --- UI Chat ---
    chk = add("POST /ui/auth/login")
    status, data, raw = client.json(
        "POST",
        f"{client.ui}/auth/login",
        csrf=True,
        timeout=20,
        body={"username": client.user, "password": client.password},
    )
    token = cookie_value(client.jar)
    expect(
        chk,
        status == 200 and bool(token) and isinstance(data, dict) and data.get("ok"),
        f"{status} cookie={bool(token)} {client.snippet(raw)}",
    )

    chk = add("GET /ui/auth/check")
    status, data, raw = client.json("GET", f"{client.ui}/auth/check", csrf=True, timeout=20)
    expect(
        chk,
        status == 200 and isinstance(data, dict) and data.get("username") == client.user,
        f"{status} {data if isinstance(data, dict) else client.snippet(raw)}",
    )

    chk = add("GET /ui/status")
    status, data, raw = client.json("GET", f"{client.ui}/status", csrf=True, timeout=30)
    expect(chk, status == 200 and isinstance(data, dict), f"{status} {client.snippet(raw)}")

    def ui_chat(message: str, extra: dict[str, Any] | None = None, timeout: float = 180) -> tuple[int, str]:
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": message}],
            "stream": True,
            "conversation_id": f"smoke-{int(time.time() * 1000)}",
        }
        if extra:
            body.update(extra)
        st, payload, _ = client.request(
            "POST",
            f"{client.ui}/chat",
            csrf=True,
            timeout=timeout,
            extra_headers={"Accept": "text/event-stream, application/json"},
            body=body,
        )
        return st, parse_sse_text(payload) or client.snippet(payload)

    chk = add("POST /ui/chat hello SSE")
    t0 = time.time()
    status, text = ui_chat("Reply with the single word hello.")
    chk.ms = (time.time() - t0) * 1000
    expect(chk, status == 200 and bool(text.strip()), f"{status} {text[:120]!r}")

    chk = add("POST /ui/chat help")
    status, text = ui_chat("help")
    expect(chk, status == 200 and "list models" in text.lower(), f"{status} {text[:160]!r}")

    chk = add("POST /ui/chat list models")
    status, text = ui_chat("list models")
    expect(chk, status == 200 and bool(text.strip()), f"{status} {text[:160]!r}")

    chk = add("GET/PUT /ui/chats identity")
    status, store, raw = client.json("GET", f"{client.ui}/chats", csrf=True, timeout=20)
    put_status = 0
    if status == 200 and isinstance(store, dict):
        put_status, put_data, put_raw = client.json(
            "PUT", f"{client.ui}/chats", csrf=True, timeout=20, body=store
        )
        expect(
            chk,
            put_status == 200 and isinstance(put_data, dict),
            f"GET {status} PUT {put_status} {client.snippet(put_raw)}",
        )
    else:
        expect(chk, False, f"GET {status} {client.snippet(raw)}")

    chk = add("GET/PUT /ui/prefs identity")
    status, prefs, raw = client.json("GET", f"{client.ui}/prefs", csrf=True, timeout=20)
    if status == 200 and isinstance(prefs, dict):
        put_status, put_data, put_raw = client.json(
            "PUT", f"{client.ui}/prefs", csrf=True, timeout=20, body=prefs
        )
        expect(
            chk,
            put_status == 200 and isinstance(put_data, dict),
            f"GET {status} PUT {put_status} {client.snippet(put_raw)}",
        )
    else:
        expect(chk, False, f"GET {status} {client.snippet(raw)}")

    # --- UI Code ---
    chat_id = f"smoke-live-{int(time.time())}"
    ws = f"{client.ui}/workspace/{urllib.parse.quote(chat_id, safe='')}"

    chk = add("PUT workspace file")
    status, data, raw = client.json(
        "PUT",
        f"{ws}/file?path={urllib.parse.quote('index.html')}",
        csrf=True,
        timeout=30,
        body={"contents": "<!doctype html><title>smoke</title><p>ok</p>\n"},
    )
    expect(chk, status == 200 and isinstance(data, dict) and data.get("ok"), f"{status} {client.snippet(raw)}")

    chk = add("GET workspace tree + file")
    st1, listing, raw1 = client.json("GET", ws, csrf=True, timeout=20)
    st2, raw_file, _ = client.request("GET", f"{ws}/file?path=index.html", csrf=True, timeout=20)
    expect(
        chk,
        st1 == 200 and st2 == 200 and b"smoke" in raw_file,
        f"list {st1} file {st2} {client.snippet(raw1) if st1 != 200 else ''}",
    )

    chk = add("POST workspace tools Write/Read/Grep")
    st_w, wr, raw_w = client.json(
        "POST",
        f"{ws}/tools",
        csrf=True,
        timeout=30,
        body={
            "name": "Write",
            "arguments": {"path": "ping.txt", "contents": "ok\n"},
            "agent": "agent",
        },
    )
    st_r, rd, raw_r = client.json(
        "POST",
        f"{ws}/tools",
        csrf=True,
        timeout=30,
        body={"name": "Read", "arguments": {"path": "ping.txt"}, "agent": "agent"},
    )
    st_g, gr, raw_g = client.json(
        "POST",
        f"{ws}/tools",
        csrf=True,
        timeout=30,
        body={"name": "Grep", "arguments": {"pattern": "ok", "path": "."}, "agent": "agent"},
    )
    read_text = ""
    if isinstance(rd, dict):
        read_text = str(rd.get("result") or "")
    expect(
        chk,
        st_w == 200 and st_r == 200 and st_g == 200 and "ok" in read_text,
        f"write {st_w} read {st_r} grep {st_g} {read_text[:80]!r}",
    )

    chk = add("POST /ui/chat mode=code agent")
    t0 = time.time()
    status, text = ui_chat(
        "Say only: ready. Do not call tools.",
        extra={"mode": "code", "agent": "agent", "chat_id": chat_id},
        timeout=180,
    )
    chk.ms = (time.time() - t0) * 1000
    expect(chk, status == 200 and bool(text.strip()), f"{status} {text[:160]!r}")

    chk = add("Ask mode refuses Write")
    status, data, raw = client.json(
        "POST",
        f"{ws}/tools",
        csrf=True,
        timeout=20,
        body={
            "name": "Write",
            "arguments": {"path": "nope.txt", "contents": "no"},
            "agent": "ask",
        },
    )
    result = str((data or {}).get("result") or "") if isinstance(data, dict) else ""
    expect(
        chk,
        status == 200 and READONLY_HINT in result.lower(),
        f"{status} {result[:160]!r} {client.snippet(raw) if status != 200 else ''}",
    )

    chk = add("POST preview + GET token URL")
    status, data, raw = client.json(
        "POST",
        f"{ws}/preview",
        csrf=True,
        timeout=20,
        body={"path": "index.html"},
    )
    preview_ok = False
    detail = f"{status} {client.snippet(raw)}"
    if status == 200 and isinstance(data, dict):
        rel = str(data.get("url") or "")
        preview_url = urllib.parse.urljoin(client.ui + "/", rel)
        pst, praw, _ = client.request("GET", preview_url, timeout=20)
        preview_ok = pst == 200 and b"smoke" in praw
        detail = f"{status} GET {preview_url} -> {pst} {len(praw)}B"
    expect(chk, preview_ok, detail)

    chk = add("GET workspace zip")
    status, raw, hdrs = client.request("GET", f"{ws}/zip", csrf=True, timeout=30)
    expect(
        chk,
        status == 200 and (raw[:2] == b"PK" or "zip" in hdrs.get("content-type", "")),
        f"{status} {len(raw)}B {hdrs.get('content-type', '')}",
    )

    chk = add("WebSocket /workspace/shell")
    cookie = cookie_value(client.jar)
    ws_scheme = "wss" if client.origin.startswith("https") else "ws"
    ws_url = f"{ws_scheme}://{urllib.parse.urlsplit(client.v1).netloc}{urllib.parse.urlsplit(client.ui).path}/workspace/{urllib.parse.quote(chat_id, safe='')}/shell?slot=1"
    ok, detail = ws_try(ws_url, client.origin, cookie)
    if ok:
        chk.pass_(detail)
    else:
        chk.fail(f"{ws_url} {detail}")

    chk = add("POST workspace lsp probe")
    status, data, raw = client.json(
        "POST",
        f"{ws}/lsp",
        csrf=True,
        timeout=20,
        body={"type": "probe", "path": "index.html"},
    )
    expect(
        chk,
        status == 200 and isinstance(data, dict) and data.get("ok"),
        f"{status} {client.snippet(raw)}",
    )

    chk = add("DELETE smoke workspace")
    status, data, raw = client.json("DELETE", ws, csrf=True, timeout=30)
    expect(
        chk,
        status == 200 and isinstance(data, dict) and data.get("ok"),
        f"{status} {client.snippet(raw)}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Chat/Code/IDE smoke against a GPU stack")
    parser.add_argument("--skip-images", action="store_true", help="Skip Comfy image generation")
    args = parser.parse_args()

    api_key = os.environ.get("TABBY_API_KEY") or ""
    user = os.environ.get("TABBY_UI_USER") or "pbp"
    password = os.environ.get("TABBY_UI_PASSWORD") or api_key
    if not api_key or not password:
        print("Set TABBY_API_KEY (and TABBY_UI_PASSWORD if it differs).", file=sys.stderr)
        return 2

    bases = os.environ.get("TABBY_BASES", "").split() or list(DEFAULT_BASES)
    skip_images = args.skip_images or os.environ.get("TABBY_SKIP_IMAGES") == "1"
    image_base = (os.environ.get("TABBY_IMAGE_BASE") or "").rstrip("/")
    if not skip_images and not image_base:
        image_base = next((b.rstrip("/") for b in bases if b.startswith("https://")), bases[0].rstrip("/"))

    results: list[tuple[str, Check]] = []
    for base in bases:
        client = Client(base, api_key, user, password)
        do_images = (not skip_images) and client.v1 == image_base.rstrip("/")
        print(f"== {client.v1}  images={do_images}", flush=True)
        run_base(client, do_images=do_images, results=results)

    failed = 0
    print()
    print(f"{'result':<6} {'ms':>8}  check")
    for _base, check in results:
        mark = "PASS" if check.ok else "FAIL"
        if not check.ok:
            failed += 1
        ms = f"{check.ms:.0f}" if check.ms else "-"
        print(f"{mark:<6} {ms:>8}  {check.name}")
        if check.detail:
            print(f"              {check.detail}")
    print()
    print(f"{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
