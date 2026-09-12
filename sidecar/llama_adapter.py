"""Map Tabby-shaped chat payloads onto llama-server's OpenAI dialect."""

from __future__ import annotations

import json
from typing import Any, Optional

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


def _truthy_logprobs(value: Any) -> bool:
    if value is True:
        return True
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def adapt_chat_payload(payload: dict[str, Any]) -> dict[str, Any]:
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


def rewrite_sse_line(line: str, *, in_think: bool) -> tuple[str, bool]:
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
    if delta.get("reasoning_content"):
        return line, in_think
    text = str(delta.get("content") or "")
    if not text:
        return line, in_think
    reasoning, content, in_think = _delta_reasoning_from_think(text, in_think)
    if reasoning:
        delta["reasoning_content"] = reasoning
    delta["content"] = content
    choices[0]["delta"] = delta
    event["choices"] = choices
    return "data: " + json.dumps(event), in_think
