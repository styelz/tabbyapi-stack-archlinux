"""Keys sent on /v1/model/load so a profile switch keeps tool and reasoning settings."""

LOAD_FIELDS = (
    "backend",
    "max_seq_len",
    "cache_size",
    "cache_mode",
    "chunk_size",
    "autosplit_reserve",
    "cpu_moe_offload_layers",
    "cpu_moe_split_experts",
    "cpu_moe_threads",
    "ngram_ram",
    "vision",
    "vision_offload",
    "tool_format",
    "template_vars_force",
    "template_vars_default",
    "reasoning",
    "reasoning_start_token",
    "reasoning_end_token",
    "answer_start_token",
    "answer_end_token",
    "start_in_reasoning",
    "harmony",
    "muse_glimmer",
    "tool_calls_in_reasoning",
)


def load_payload(model_name: str, model_cfg: dict) -> dict:
    payload = {"model_name": model_name}
    for key in LOAD_FIELDS:
        if key in model_cfg and model_cfg[key] is not None:
            payload[key] = model_cfg[key]
    return payload
