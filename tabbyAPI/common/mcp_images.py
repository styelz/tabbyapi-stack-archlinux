"""Cursor MCP (Model Context Protocol) for Comfy image generation.

Remote IDEs look for a generate_image tool. TabbyAPI already has
POST /v1/images/generations; this module is the JSON-RPC surface those
clients actually search for. Same GPU job, no browser, no extra package.

generate_image waits until the PNG files exist on the GPU host, then
returns their URLs. get_image_job is optional (debug / resume), not the
VS Code no-mcp mixed-chat path.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional
from uuid import uuid4

PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOLS = frozenset(
    {
        "2024-11-05",
        "2025-03-26",
        "2025-06-18",
    }
)
SERVER_NAME = "tabby-images"
SERVER_VERSION = "1.3.0"

TOOL_NAME = "generate_image"
GET_JOB_NAME = "get_image_job"
TOOL_DESCRIPTION = (
    "Generate PNG(s) on this TabbyAPI/Comfy GPU (Flux Schnell draft, or "
    "Qwen-Image when the prompt starts with qwen-image: or qwen_image is true). "
    "This call waits until the files exist on the GPU host, then returns their "
    "URLs. If you need several assets (logo, header, page photos), pass them "
    "ALL in one call via the images array so they share one Comfy session and "
    "the coding model reloads once at the end. After this returns, Shell-curl "
    "only the URLs listed. Do not invent /v1/images/generated-*.png timestamps. "
    "Do not use the browser. Do not use Cursor's built-in GenerateImage tool. "
    "Prefix qwen-image: only for readable text (logo, poster, button). "
    "Hero/header photos: describe a scene, not a website or UI. "
    "Set size to WIDTHxHEIGHT from the user's request (for example "
    "1920x1080, 16:9, landscape, portrait). Do not leave every PNG at "
    "1024x1024 when they asked for another size. Headers/banners: "
    "1536x768. Portraits: 768x1344."
)
GET_JOB_DESCRIPTION = (
    "Optional debug poll for a TabbyAPI image job. generate_image already "
    "waits until files exist; use this only if you need status mid-render. "
    "Pass job_id from that call (or omit it to use the latest job). Waits up "
    "to wait_s seconds (default 20) for a progress tick, then returns."
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": TOOL_NAME,
        "description": TOOL_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Image description. Prefix qwen-image: for readable "
                        "text, logos, posters, buttons, or headings. Omit "
                        "when polling with job_id or when using images."
                    ),
                },
                "images": {
                    "type": "array",
                    "description": (
                        "Submit every PNG in one call. One Comfy session, one "
                        "LLM reload at the end. Prefer this over calling "
                        "generate_image once per file."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "prompt": {
                                "type": "string",
                                "description": "Image description. Prefix qwen-image: for text/logos.",
                            },
                            "output_path": {
                                "type": "string",
                                "description": (
                                    "Project-relative PNG path, e.g. "
                                    "pbptours/images/logo.png. Never an absolute "
                                    "/home/... or C:\\... path."
                                ),
                            },
                            "size": {
                                "type": "string",
                                "description": (
                                    "WIDTHxHEIGHT from the user request. "
                                    "Default 1024x1024 only when they did "
                                    "not ask for another size."
                                ),
                            },
                            "n": {
                                "type": "integer",
                                "description": "Copies of this prompt (1-4). Default 1.",
                            },
                            "seed": {"type": "integer"},
                            "qwen_image": {
                                "type": "boolean",
                                "description": "Force the Qwen-Image workflow for text/UI.",
                            },
                        },
                        "required": ["prompt"],
                    },
                },
                "job_id": {
                    "type": "string",
                    "description": (
                        "Poll this job instead of starting a new one. Same "
                        "as get_image_job."
                    ),
                },
                "output_path": {
                    "type": "string",
                    "description": (
                        "Project-relative PNG path (for example images/logo.png). "
                        "Never /home/... or C:\\... — those save in the wrong folder. "
                        "HTTP MCP cannot write the IDE disk; save the returned "
                        "URL there with Shell."
                    ),
                },
                "size": {
                    "type": "string",
                    "description": (
                        "WIDTHxHEIGHT from the user request "
                        "(16:9, landscape, portrait, 1920x1080). "
                        "Default 1024x1024 only when they did not ask "
                        "for another size."
                    ),
                    "default": "1024x1024",
                },
                "n": {
                    "type": "integer",
                    "description": "How many copies of prompt (1-4). Default 1.",
                    "default": 1,
                },
                "seed": {
                    "type": "integer",
                    "description": "Optional RNG seed.",
                },
                "qwen_image": {
                    "type": "boolean",
                    "description": "Force the Qwen-Image workflow for text/UI.",
                    "default": False,
                },
                "restore": {
                    "type": "boolean",
                    "description": (
                        "Reload the last LLM after the whole batch. Default "
                        "true when an LLM was loaded."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": GET_JOB_NAME,
        "description": GET_JOB_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Job id from generate_image. Latest job if omitted.",
                },
                "wait_s": {
                    "type": "integer",
                    "description": (
                        "Seconds to wait for a progress tick (0-45). Default "
                        "20. 0 returns immediately."
                    ),
                    "default": 20,
                },
            },
        },
    },
]

INSTRUCTIONS = (
    "This server generates PNGs on the TabbyAPI GPU via ComfyUI. "
    "If you need several assets (logo, header, section photos), pass them "
    "ALL in one generate_image call using the images array "
    "([{prompt, output_path}, ...]). Always pass a distinct output_path "
    "per image; omitted dests become images/generated.png, "
    "images/generated-2.png, and so on. Extra generate_image calls while a batch "
    "is queued or generating are added to the same Comfy session — do not wait "
    "for one PNG before requesting the next. "
    "generate_image returns a job_id immediately — Cursor will time out if the "
    "tool waits for the PNG. Keep calling tools until the files exist at "
    "output_path. The API requests get_image_job (up to 20s per poll) and then "
    "Shell to write the PNGs. Do not Shell-sleep the full estimate. Do not "
    "stop and tell the user to download. A -32001 timeout does not stop the "
    "GPU. Prefix qwen-image: only for readable text (logo, poster, button). "
    "Hero/header photos: describe a scene, not a website or UI. "
    "Never use the browser to POST JSON. Never use Cursor's built-in "
    "GenerateImage tool."
)


def new_session_id() -> str:
    return str(uuid4())


def rpc_result(rpc_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def rpc_error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def tool_text(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": bool(is_error),
    }


def initialize_result(client_version: Optional[str] = None) -> dict[str, Any]:
    version = client_version if client_version in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": INSTRUCTIONS,
    }


def list_tools_result() -> dict[str, Any]:
    return {"tools": TOOLS}


def normalize_prompt(arguments: Optional[dict[str, Any]]) -> str:
    args = arguments or {}
    prompt = str(args.get("prompt") or "").strip()
    if args.get("qwen_image") and prompt and not prompt.lower().lstrip().startswith(
        "qwen-image"
    ):
        return f"qwen-image: {prompt}"
    return prompt


def mcp_owner_from_request(request) -> str:
    """Stable per-credential id so two API clients cannot share a GPU batch."""
    if request is None:
        return "mcp:local"
    from common.auth import _presented_token, _token_digest

    token = _presented_token(
        request.headers.get("x-api-key") if hasattr(request, "headers") else None,
        request.headers.get("authorization") if hasattr(request, "headers") else None,
    )
    if not token:
        return "mcp:anonymous"
    return "key:" + _token_digest(token)[:16]


def clamp_count(value: Any) -> int:
    try:
        return max(1, min(int(value or 1), 4))
    except (TypeError, ValueError):
        return 1


def parse_image_items(arguments: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    args = arguments or {}
    items: list[dict[str, Any]] = []
    raw_list = args.get("images")
    if isinstance(raw_list, list):
        for entry in raw_list:
            if isinstance(entry, str):
                entry = {"prompt": entry}
            if not isinstance(entry, dict):
                continue
            prompt = normalize_prompt(entry)
            if not prompt:
                continue
            seed = entry.get("seed", args.get("seed"))
            if seed is not None:
                try:
                    seed = int(seed)
                except (TypeError, ValueError):
                    seed = None
            from images.paths import safe_rel_png_path

            items.append(
                {
                    "prompt": prompt,
                    "output_path": safe_rel_png_path(
                        str(entry.get("output_path") or "").strip()
                    ),
                    "size": str(entry.get("size") or args.get("size") or "1024x1024"),
                    "count": clamp_count(entry.get("n")),
                    "seed": seed,
                }
            )
    from images.paths import resolve_output_paths

    resolve_output_paths(items)
    from common.image_prompts import rewrite_comfy_prompt

    for item in items:
        item["prompt"] = rewrite_comfy_prompt(item["prompt"])
    return items


def _phase_label(job) -> str:
    phase = getattr(job, "phase", "") or job.status
    total = max(1, len(getattr(job, "items", None) or [None]))
    index = int(getattr(job, "current_index", 0) or 0) + 1
    if phase == "starting_comfy":
        return "starting Comfy (unloading the coding model)"
    if phase == "generating":
        return f"generating image {index}/{total}"
    if phase == "restoring_llm":
        return "reloading the coding model"
    if phase == "done":
        return "done"
    if phase == "error":
        return "error"
    if job.status == "queued":
        return "queued (handoff in a few seconds)"
    return str(phase or job.status)


def _elapsed_line(job) -> str:
    from common.switch_times import format_duration

    started = float(getattr(job, "started_at", 0) or 0)
    elapsed = max(0, int(time.time() - started)) if started else 0
    remaining = max(0, int(job.wait_s) - elapsed)
    return (
        f"Elapsed: {format_duration(elapsed)}. "
        f"ETA remaining: about {format_duration(remaining)} "
        f"(batch estimate {format_duration(job.wait_s)})."
    )


def format_mcp_job_text(
    job,
    *,
    started: bool = False,
    busy_other: bool = False,
    appended: bool = False,
) -> str:
    items = list(getattr(job, "items", None) or [])
    dests = [item.output_path for item in items if getattr(item, "output_path", None)]
    dest = dests[0] if len(dests) == 1 else (dests[0] if dests else "images/")
    total = max(1, len(items) or int(getattr(job, "count", 1) or 1))
    done = int(getattr(job, "done_count", 0) or 0)
    header = [
        f"Job {job.id}: {job.status}",
    ]
    if started:
        header.append(
            f"Queued {total} image(s) in one Comfy session. "
            "The coding model reloads once at the end. "
            "This call returns now so Cursor does not time out."
        )
    if appended:
        header.append(
            f"Added to the same GPU batch (now {total} image(s)). "
            "Comfy stays up; the coding model reloads once at the end."
        )
    if busy_other:
        header.append(
            "A batch is already on the GPU (restoring the coding model or full). "
            "Call get_image_job to watch it; then retry your new prompt."
        )
    if job.status == "error":
        return "\n".join(
            [
                f"Job {job.id}: error",
                job.error or "Image job failed",
                "You can call generate_image again with a new prompt.",
            ]
        )
    if job.status == "done" and job.urls:
        url_lines = []
        if items:
            for item in items:
                for url in item.urls or []:
                    url_lines.append(f"  {item.output_path} <- {url}")
        if not url_lines:
            url_lines = [f"  {url}" for url in job.urls]
        example_dest = dests[0] if dests else dest
        example_url = (
            items[0].urls[0] if items and items[0].urls else job.urls[0]
        )
        return "\n".join(
            [
                f"Job {job.id}: done",
                f"Generated {len(job.urls)} image(s) in one Comfy session.",
                f"This job was: {job.wait_text}",
                "URLs:",
                *url_lines,
                "The next chat turn writes these into output_path via Shell. "
                "Do not ask the user to download. Do not paste download instructions.",
                "Write/StrReplace cannot save PNG bytes.",
                "Do not use the browser. Do not use Cursor's built-in GenerateImage tool.",
                "Point img src at those local PNG files.",
                f"job_id={job.id} dest={example_dest} url={example_url}",
            ]
        )

    body = [
        f"Phase: {_phase_label(job)}",
        f"Progress: {done}/{total} images",
        f"Prompt: {job.prompt}",
        _elapsed_line(job),
        f"Wait: {job.wait_text}",
    ]
    if items:
        body.append("Assets:")
        # Running batches must not list generated-*.png URLs. The coding
        # model copies the timestamp pattern and GETs planets still on GPU.
        show_urls = job.status not in ("queued", "running")
        for item in items:
            bit = f"  [{item.status}] {item.output_path}: {item.prompt}"
            if show_urls and item.urls:
                bit += " -> " + ", ".join(item.urls)
            body.append(bit)
    body.extend(
        [
            "Call get_image_job again (it waits up to 20s for a progress tick). "
            "Do not Shell-sleep the full estimate. Do not stop. "
            "Do not tell the user to download. "
            "Do not invent generated-*.png URLs for items still rendering. "
            f"job_id={job.id}.",
            "A MCP error -32001 (Request timed out) does not stop this job. "
            "Further generate_image calls are added to this same batch until it restores the LLM.",
            f"When done, the next turn must Shell-write files into {dest}. "
            "Do not tell the user the PNG will appear later. "
            "Write/StrReplace cannot save PNG bytes.",
            "Do not use the browser. Do not use Cursor's built-in GenerateImage tool.",
            "Point img src at those local PNG files.",
        ]
    )
    return "\n".join(header + body)


async def run_get_job_tool(
    arguments: Optional[dict[str, Any]], request=None
) -> dict[str, Any]:
    from endpoints.core.image_jobs import (
        MCP_POLL_WAIT_S,
        get_mcp_image_job,
        wait_mcp_job_progress,
    )

    args = arguments or {}
    job_id = str(args.get("job_id") or "").strip()
    job = get_mcp_image_job(job_id or None)
    if not job:
        return tool_text("No image job found. Call generate_image first.", is_error=True)
    wait_s = args.get("wait_s")
    if wait_s is None:
        wait_s = MCP_POLL_WAIT_S
    try:
        wait_s = int(wait_s)
    except (TypeError, ValueError):
        wait_s = MCP_POLL_WAIT_S
    await wait_mcp_job_progress(job, wait_s)
    return tool_text(format_mcp_job_text(job), is_error=job.status == "error")


async def run_generate_tool(
    arguments: Optional[dict[str, Any]], request=None
) -> dict[str, Any]:
    """Same GPU job as POST /v1/images/generations. Wait until files exist."""
    from common.gpu_mode import public_api_base
    from images.jobs import (
        get_mcp_image_job,
        loaded_tabby_name,
        start_mcp_image_job,
        wait_until_done,
    )

    args = arguments or {}
    job_id = str(args.get("job_id") or "").strip()
    prompt = normalize_prompt(args)
    items = parse_image_items(args)
    if job_id or (not prompt and not items):
        job = get_mcp_image_job(job_id or None)
        if job:
            await wait_until_done(job)
            return tool_text(format_mcp_job_text(job), is_error=job.status == "error")
        if not prompt and not items:
            return tool_text("prompt is required", is_error=True)

    size = str(args.get("size") or "1024x1024")
    count = clamp_count(args.get("n"))
    seed = args.get("seed")
    if seed is not None:
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            seed = None

    was_llm = bool(loaded_tabby_name())
    restore = args.get("restore")
    if restore is None:
        restore = was_llm

    from common.networking import DisconnectHandler
    from images.jobs import active_mcp_image_job
    from ui.occupancy import StackGate

    owner = mcp_owner_from_request(request)
    busy = active_mcp_image_job()
    if busy and str(busy.owner or "").strip() == owner:
        return await _run_generate_job(
            args, request, prompt, items, size, count, seed, restore, owner=owner
        )

    gate = StackGate(owner or "api", kind="image")
    handler = DisconnectHandler(request, "/v1/mcp") if request is not None else None
    if handler is not None:
        await gate.wait_until_acquired(handler)
    else:
        await gate.wait_until_acquired(_NullDisconnect())
    try:
        return await _run_generate_job(
            args, request, prompt, items, size, count, seed, restore, owner=owner
        )
    finally:
        await gate.release()


class _NullDisconnect:
    async def poll(self):
        return None


async def _run_generate_job(
    args, request, prompt, items, size, count, seed, restore, *, owner: str = ""
):
    from common.gpu_mode import public_api_base
    from images.jobs import start_mcp_image_job, wait_until_done

    suggested = str(args.get("output_path") or "").strip() or "images/generated.png"
    api_base = public_api_base(request)
    job, kind = await start_mcp_image_job(
        prompt=prompt,
        output_path=suggested,
        size=size,
        count=count,
        seed=seed,
        restore=bool(restore),
        api_base=api_base,
        items=items or None,
        delay=0.0,
        owner=owner or mcp_owner_from_request(request),
    )
    if kind == "busy":
        return tool_text(format_mcp_job_text(job, busy_other=True), is_error=False)
    await wait_until_done(job)
    return tool_text(
        format_mcp_job_text(
            job,
            started=kind == "started",
            appended=kind == "appended",
        ),
        is_error=job.status == "error",
    )


async def dispatch(
    message: Any,
    *,
    call_generate: Optional[Callable] = None,
) -> Optional[dict[str, Any]]:
    """Handle one JSON-RPC object. None means a notification (HTTP 202)."""
    if not isinstance(message, dict):
        return rpc_error(None, -32600, "Invalid Request")

    rpc_id = message.get("id", None)
    method = message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    is_notification = "id" not in message

    if not method:
        if is_notification:
            return None
        return rpc_error(rpc_id, -32600, "Invalid Request")

    if method == "notifications/initialized" or method.startswith("notifications/"):
        return None

    if method == "initialize":
        client_version = params.get("protocolVersion")
        return rpc_result(rpc_id, initialize_result(client_version))

    if method == "ping":
        return rpc_result(rpc_id, {})

    if method == "tools/list":
        return rpc_result(rpc_id, list_tools_result())

    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name == GET_JOB_NAME:
            result = await run_get_job_tool(arguments)
            return rpc_result(rpc_id, result)
        if name != TOOL_NAME:
            return rpc_result(
                rpc_id, tool_text(f"Unknown tool {name!r}", is_error=True)
            )
        runner = call_generate or run_generate_tool
        result = await runner(arguments)
        return rpc_result(rpc_id, result)

    if is_notification:
        return None
    return rpc_error(rpc_id, -32601, f"Method not found: {method}")
