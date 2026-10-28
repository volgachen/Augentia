"""Per-agent model call parameters (config.model_params / compression_model_params).

An agent's config may carry extra Chat Completions parameters, e.g.
``{"temperature": 0.7, "reasoning_effort": "high", "max_tokens": 4096}``.
They are merged as-is into every ``chat.completions.create`` call the agent
makes, so new knobs need no code change. Parameters the openai SDK does not
know (gateway- or provider-specific) go under ``extra_body``, which the SDK
sends verbatim in the request body.

Keys that carry the request itself (model, messages, tools, tool_choice, ...)
are reserved: the engine owns them, and letting config override tool_choice
would undo the summarizer's tool_choice="none" (CAgent COMPRESSION_SEND_TOOLS).
"""

from __future__ import annotations

# Request-shaping keys the engine sets itself; never taken from config.
RESERVED_KEYS = frozenset({
    "model", "messages", "tools", "tool_choice", "functions", "function_call",
    "stream", "stream_options", "n",
})


def validate_model_params(value, key: str = "model_params") -> dict:
    """Return a copy of a config params dict; None/absent means no extra params.

    Raises ValueError for a non-object, a reserved key, or an extra_body that
    is not an object, so a bad config fails at session start rather than on
    the first model call.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"config.{key} must be an object or null")
    reserved = sorted(k for k in value if k in RESERVED_KEYS)
    if reserved:
        raise ValueError(f"config.{key} may not set {reserved}; the engine controls them")
    if "extra_body" in value and not isinstance(value["extra_body"], dict):
        raise ValueError(f"config.{key}.extra_body must be an object")
    return dict(value)


def resolve_model_params(config: dict) -> tuple[dict, dict]:
    """(agent params, summarizer params) from an agent/session config.

    compression_model_params, when present, replaces model_params for the
    summarizer entirely (no merge): a separate compression model often takes
    different parameters. Absent -> the summarizer uses model_params.
    """
    params = validate_model_params(config.get("model_params"))
    if "compression_model_params" in config:
        return params, validate_model_params(
            config.get("compression_model_params"), "compression_model_params")
    return params, params
