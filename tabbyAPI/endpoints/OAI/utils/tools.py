"""Tool call processing utilities for OAI server."""

import json
from common.logger import xlogger
from typing import Any, List

from endpoints.OAI.types.tools import ToolCall
from endpoints.OAI.utils.toolcall_formats import (
    qwen3_coder,
    minimax_m2,
    glm4_5,
    harmony,
    hy3,
    muse_glimmer,
    deepseek_v4,
    mistral_old,
    mistral,
    gemma4,
)

ALL_TOOLCALL_FORMATS = {
    "deepseek_v4": deepseek_v4,
    "dsv4": deepseek_v4,
    "gemma4": gemma4,
    "glm4_5": glm4_5,
    "glm4_6": glm4_5,
    "glm4_7": glm4_5,
    "harmony": harmony,
    "hy3": hy3,
    "hy_v3": hy3,
    "laguna": glm4_5,
    "poolside_v1": glm4_5,
    "minimax_m2": minimax_m2,
    "minimax_m2_1": minimax_m2,
    "minimax_m2_5": minimax_m2,
    "mistral_old": mistral_old,
    "mistral": mistral,
    "muse_glimmer": muse_glimmer,
    "glimmer": muse_glimmer,
    "qwen3_coder": qwen3_coder,
    "qwen3_5": qwen3_coder,
    "qwen3_6": qwen3_coder,
    "qwen3_8": qwen3_coder,
    "step3_5": qwen3_coder,
    "step3_7": qwen3_coder,
}


def _get_parser(tool_format: str):
    if not tool_format:
        return None
    parser = ALL_TOOLCALL_FORMATS.get(tool_format)
    if not parser:
        xlogger.error(f"Unknown tool format given: {tool_format}")
    return parser


def get_toolcall_tags(tool_format: str):
    parser = _get_parser(tool_format)
    if not parser:
        return None, None
    starts = getattr(parser, "TOOLCALL_STARTS", None) or parser.TOOLCALL_START
    ends = getattr(parser, "TOOLCALL_ENDS", None) or parser.TOOLCALL_END
    return starts, ends


def is_supported_format(tool_format: str) -> bool:
    return tool_format in ALL_TOOLCALL_FORMATS


def first_json_value(text: str) -> Any:
    """Parse one JSON value. Concatenated objects keep the first."""
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        try:
            value, _end = json.JSONDecoder().raw_decode(raw)
        except json.JSONDecodeError:
            return {}
    if isinstance(value, dict):
        return value
    return {"value": value}


def coerce_tool_arguments(raw) -> str:
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    parsed = first_json_value("" if raw is None else str(raw))
    if parsed:
        return json.dumps(parsed, ensure_ascii=False)
    return "" if raw is None else str(raw)


def mapping_tool_arguments(raw) -> dict:
    """Dict form of tool arguments for templates that call |items."""
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    if isinstance(raw, str):
        return first_json_value(raw)
    return first_json_value(json.dumps(raw, default=str))


def dictify_tool_call_arguments(message_dicts: list) -> None:
    """Make every tool_call.function.arguments a mapping before Jinja render."""
    for msg in message_dicts:
        if not isinstance(msg, dict):
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function")
            if isinstance(func, dict):
                func["arguments"] = mapping_tool_arguments(func.get("arguments"))
            elif "arguments" in tc:
                tc["arguments"] = mapping_tool_arguments(tc.get("arguments"))


def parse_toolcalls(tool_calls_str: str, tool_format: str) -> List[ToolCall]:
    """
    Dispatch tool call parsing to the appropriate format handler.

    Args:
        tool_calls_str: Raw tool call text from model generation.
        tool_format: See below

    Returns:
        List of parsed ToolCall objects. Empty list on parse failure (never raises).
    """

    try:
        parser = _get_parser(tool_format)
        if not parser:
            return []

        calls = parser.parse_toolcalls(tool_calls_str)
        for call in calls:
            func = getattr(call, "function", None)
            if func is None or getattr(func, "arguments", None) is None:
                continue
            func.arguments = coerce_tool_arguments(func.arguments)
        return calls

    except Exception as e:
        xlogger.error(
            "ToolCallProcessor.parse: Failed to parse tool calls",
            {"tool_format": tool_format, "e": str(e)},
            details=f"(format={tool_format}): {e}",
        )
        return []
