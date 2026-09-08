"""HTTP MCP generate_image: wait until GPU PNGs exist, then return URLs."""

from __future__ import annotations

from typing import Any, Callable, Optional

from common.mcp_images import (
    GET_JOB_NAME,
    TOOL_NAME,
    clamp_count,
    format_mcp_job_text,
    initialize_result,
    list_tools_result,
    normalize_prompt,
    parse_image_items,
    rpc_error,
    rpc_result,
    run_get_job_tool,
    tool_text,
)


async def run_generate_tool(arguments: Optional[dict[str, Any]], request=None) -> dict[str, Any]:
    """Start the same GPU job as chat, then wait until files exist."""
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

    suggested = str(args.get("output_path") or "").strip() or "images/generated.png"
    api_base = public_api_base(request)
    from common.mcp_images import mcp_owner_from_request

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
        owner=mcp_owner_from_request(request),
    )
    if kind == "busy":
        return tool_text(
            format_mcp_job_text(job, busy_other=True),
            is_error=False,
        )
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
    from common.mcp_images import dispatch as _dispatch

    generate = call_generate or run_generate_tool
    return await _dispatch(message, call_generate=generate)
