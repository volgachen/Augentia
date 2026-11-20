"""Per-agent model call parameters and wire format.

config.model_params / compression_model_params: extra request parameters,
e.g. ``{"temperature": 0.7, "reasoning_effort": "high", "max_tokens": 4096}``.
They are merged as-is into every model call the agent makes
(``chat.completions.create`` or ``responses.create``, see API formats below),
so new knobs need no code change. Parameters the openai SDK does not know
(gateway- or provider-specific) go under ``extra_body``, which the SDK sends
verbatim in the request body.

Params are never translated between formats: with the "responses" format
write the Responses names yourself (``max_output_tokens`` instead of
``max_tokens``, ``reasoning: {"effort": ...}`` instead of
``reasoning_effort``).

Keys that carry the request itself (model, messages / input, tools,
tool_choice, ...) are reserved: the engine owns them, and letting config
override tool_choice would undo the summarizer's tool_choice="none" (CAgent
COMPRESSION_SEND_TOOLS).

API formats (AgentTemplate.openai_api_format, Session.api_format,
config.compression_api_format): "chat" calls /chat/completions, "responses"
calls /responses. Which one a model supports depends on the gateway.
"""

from __future__ import annotations

# Request-shaping keys the engine sets itself; never taken from config.
RESERVED_KEYS = frozenset({
    "model", "messages", "tools", "tool_choice", "functions", "function_call",
    "stream", "stream_options", "n",
    # Responses API counterparts of messages / the system message.
    "input", "instructions",
})

API_FORMATS = ("chat", "responses")
DEFAULT_API_FORMAT = "chat"


def validate_api_format(value, key: str = "api_format", *, nullable: bool = True) -> str | None:
    """Return a valid API format; None passes when nullable. ValueError otherwise."""
    if value is None and nullable:
        return None
    if value not in API_FORMATS:
        allowed = " or ".join(repr(f) for f in API_FORMATS)
        raise ValueError(f"{key} must be {allowed}" + (" or null" if nullable else ""))
    return value


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
