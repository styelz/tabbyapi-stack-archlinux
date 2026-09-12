"""Map Tabby-shaped chat payloads onto llama-server's OpenAI dialect."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

EXL_ONLY_KEYS = {
    "xtc_probability",
    "xtc_threshold",
    "adaptive_p",
    "dry_multiplier",
    "dry_base",
    "dry_allowed_length",
    "dry_range",
    "dry_sequence_breakers",
    "repetition_decay",
    "adaptive_target",
    "adaptive_decay",
    "token_healing",
    "banned_strings",
    "loop_detect_window",
    "cache_mode",
    "cache_size",
    "chunk_size",
    "draft_model",
    "response_prefix",
    "continue_final_message",
    "add_generation_prompt",
    "template_vars",
    "template_vars_force",
    "json_schema",
    "regex_pattern",
    "grammar_string",
}

# GGUFs without a native chat template get llama.cpp's ChatML wrapper, but
# many (DeepSeek Coder/Kexer, base models) do not have <|im_end|> as EOS, so
# they keep emitting a fake conversation unless these strings halt decoding.
DEFAULT_STOPS = (
    "<|im_end|>",
    "<|im_start|>",
    "<|endoftext|>",
    "<|eot_id|>",
    "<|eom_id|>",
    "<｜end▁of▁sentence｜>",
    "<|fim_start|>",
    "<|fim_middle|>",
    "<|fim_end|>",
    "<|fim_prefix|>",
    "<|fim_suffix|>",
    "<|fim_hole|>",
    "<|fim_begin|>",
    "<fim_prefix>",
    "<fim_suffix>",
    "<fim_middle>",
)

TOOL_PAYLOAD_KEYS = ("tools", "functions", "tool_choice", "parallel_tool_calls")
NO_TOOLS_SYSTEM = (
    "You are a helpful assistant in the TabbyAPI Stack console. "
    "Reply in this conversation only. You cannot call tools or write project files. "
    "Put any code in markdown fences."
)
_TOOLISH_SYSTEM = re.compile(
    r"file tools|Use the file tools|tool_calls|Grep, Glob, Write",
    re.I,
)

_CAPS_CACHE: tuple[str, dict[str, Any], float] | None = None
_CAPS_TTL_S = 30.0


def _truthy_logprobs(value: Any) -> bool:
    if value is True:
        return True
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def llama_chat_caps() -> dict[str, Any]:
    """Cached llama-server /props: tool support and the real EOS token."""
    global _CAPS_CACHE
    try:
        from common.llama_runtime import llama_url, read_llama_runtime

        target = llama_url()
        model = str(read_llama_runtime().get("model") or "")
    except Exception:
        return {}
    key = f"{target}|{model}"
    now = time.monotonic()
    if _CAPS_CACHE and _CAPS_CACHE[0] == key and now - _CAPS_CACHE[2] < _CAPS_TTL_S:
        return dict(_CAPS_CACHE[1])
    try:
        req = Request(str(target).rstrip("/") + "/props", method="GET")
        with urlopen(req, timeout=1) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except (URLError, HTTPError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    caps = payload.get("chat_template_caps")
    if not isinstance(caps, dict):
        caps = {}
    info: dict[str, Any] = {
        "eos_token": str(payload.get("eos_token") or "").strip(),
        "bos_token": str(payload.get("bos_token") or "").strip(),
        "chat_template": str(payload.get("chat_template") or ""),
    }
    if "supports_tools" in caps or "supports_tool_calls" in caps:
        info["supports_tools"] = bool(
            caps.get("supports_tools") or caps.get("supports_tool_calls")
        )
    _CAPS_CACHE = (key, info, now)
    return dict(info)


def reset_llama_caps_cache() -> None:
    global _CAPS_CACHE
    _CAPS_CACHE = None


def _stop_list(value: Any) -> list[str]:
    if value is None or value is False:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)]
    return []


def _merge_stops(body: dict[str, Any], extra: tuple[str, ...] | list[str] = ()) -> None:
    seen: list[str] = []
    for item in (*_stop_list(body.get("stop")), *DEFAULT_STOPS, *extra):
        if item and item not in seen:
            seen.append(item)
    body["stop"] = seen


def cut_at_stop(text: str) -> tuple[str, bool]:
    """Trim leaked ChatML / FIM markers. Returns (text, hit_a_stop)."""
    if not text:
        return text, False
    cut = len(text)
    hit = False
    for marker in DEFAULT_STOPS:
        idx = text.find(marker)
        if 0 <= idx < cut:
            cut = idx
            hit = True
    return text[:cut], hit


def cut_completion_stops(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, list):
        return data
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        msg = choice.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            text, _ = cut_at_stop(msg["content"])
            msg["content"] = text
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            text, _ = cut_at_stop(delta["content"])
            delta["content"] = text
    return data


def _strip_tools(body: dict[str, Any]) -> None:
    had_tools = any(key in body for key in TOOL_PAYLOAD_KEYS)
    for key in TOOL_PAYLOAD_KEYS:
        body.pop(key, None)
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    out: list[dict[str, Any]] = []
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "user")
        item = {key: value for key, value in raw.items() if key != "tool_calls"}
        if role == "tool":
            content = item.get("content") or ""
            if isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict):
                        parts.append(str(part.get("text") or ""))
                    else:
                        parts.append(str(part))
                content = "\n".join(part for part in parts if part)
            out.append({"role": "user", "content": f"Tool result:\n{content}"})
            continue
        if role == "system" and had_tools and _TOOLISH_SYSTEM.search(str(item.get("content") or "")):
            item["content"] = NO_TOOLS_SYSTEM
        out.append(item)
    body["messages"] = out


def adapt_chat_payload(
    payload: dict[str, Any],
    *,
    caps: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    body = dict(payload or {})
    body["model"] = "gpt-4o"
    for key in list(body):
        if key in EXL_ONLY_KEYS:
            body.pop(key, None)
    # llama.cpp 400s on top_logprobs unless logprobs is true, including 0.
    if not _truthy_logprobs(body.get("logprobs")):
        body.pop("logprobs", None)
        body.pop("top_logprobs", None)
    elif not _truthy_logprobs(body.get("top_logprobs")):
        body.pop("top_logprobs", None)
    grammar = payload.get("grammar_string") if payload else None
    schema = payload.get("json_schema") if payload else None
    if schema and not body.get("response_format"):
        body["response_format"] = {"type": "json_schema", "json_schema": {"schema": schema}}
    if grammar:
        body["grammar"] = grammar
    info = caps if caps is not None else llama_chat_caps()
    extra_stops: list[str] = []
    eos = str((info or {}).get("eos_token") or "").strip()
    if eos:
        extra_stops.append(eos)
    _merge_stops(body, extra_stops)
    if (info or {}).get("supports_tools") is False:
        _strip_tools(body)
    return body


def _delta_reasoning_from_think(text: str, in_think: bool) -> tuple[str, str, bool]:
    """Split a streamed fragment into reasoning vs content via <think> tags."""
    if not text:
        return "", "", in_think
    reasoning = ""
    content = ""
    rest = text
    while rest:
        if in_think:
            end = rest.find("</think>")
            if end < 0:
                reasoning += rest
                return reasoning, content, True
            reasoning += rest[:end]
            rest = rest[end + len("</think>") :]
            in_think = False
            continue
        start = rest.find("<think>")
        if start < 0:
            content += rest
            return reasoning, content, False
        content += rest[:start]
        rest = rest[start + len("<think>") :]
        in_think = True
    return reasoning, content, in_think


def rewrite_sse_line(
    line: str,
    *,
    in_think: bool,
    state: Optional[dict[str, Any]] = None,
) -> tuple[str, bool]:
    raw = line.strip()
    if not raw.startswith("data:"):
        return line, in_think
    data = raw[5:].strip()
    if not data or data == "[DONE]":
        return line, in_think
    try:
        event = json.loads(data)
    except ValueError:
        return line, in_think
    if not isinstance(event, dict):
        return line, in_think
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return line, in_think
    choice = choices[0] if isinstance(choices[0], dict) else None
    if not choice:
        return line, in_think
    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else None
    if not delta:
        return line, in_think
    if state and state.get("stopped"):
        delta["content"] = ""
        if "reasoning_content" in delta:
            delta["reasoning_content"] = ""
        choices[0]["delta"] = delta
        event["choices"] = choices
        return "data: " + json.dumps(event), in_think
    if delta.get("reasoning_content"):
        text = str(delta.get("reasoning_content") or "")
        trimmed, hit = cut_at_stop(text)
        if hit:
            delta["reasoning_content"] = trimmed
            if state is not None:
                state["stopped"] = True
            choices[0]["delta"] = delta
            event["choices"] = choices
            return "data: " + json.dumps(event), in_think
        return line, in_think
    text = str(delta.get("content") or "")
    if not text:
        return line, in_think
    reasoning, content, in_think = _delta_reasoning_from_think(text, in_think)
    reasoning, hit_r = cut_at_stop(reasoning)
    content, hit_c = cut_at_stop(content)
    if hit_r or hit_c:
        in_think = False
        if state is not None:
            state["stopped"] = True
    if reasoning:
        delta["reasoning_content"] = reasoning
    delta["content"] = content
    choices[0]["delta"] = delta
    event["choices"] = choices
    return "data: " + json.dumps(event), in_think
