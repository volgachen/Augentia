"""Plugin hook points of the CAgent engine (app/plugins/README.md §7.2).

Conversion between the engine's dicts and the SDK payload dataclasses, plus
the checks applied to what hooks return. The call sites live in
cagent_adapter.py (message.received, model.before, message.generated,
tool.before, turn.end) and cagent_agent.py (tool.after).
"""

from __future__ import annotations

import logging
from typing import Any

from app.adapters.tools.base import ToolImage, ToolOutput
from app.plugins.sdk import IncomingMessage, ModelReply, ModelRequest, ToolCall, ToolResult

logger = logging.getLogger("augentia.adapter.hooks")

# Key on the saved tool result (metadata) naming plugins that changed the call's arguments.
ARGS_MODIFIED_KEY = "arguments_modified_by"


# ---- message.received ----


def check_incoming(previous: IncomingMessage, new: IncomingMessage) -> str | None:
    if not isinstance(new.content, str):
        return "content must be a string"
    if new.images is not None:
        if not isinstance(new.images, list) or any(
                not isinstance(i, dict) or not isinstance(i.get("ref"), str) for i in new.images):
            return "images must be a list of {ref, detail?} objects"
        # Plugins may drop or reorder the user's images, not inject new ones
        # (an arbitrary ref could point anywhere and bypasses vision checks).
        allowed = {i["ref"] for i in previous.images or []}
        if any(i["ref"] not in allowed for i in new.images):
            return "images may only keep or remove the message's own images"
    if not new.content.strip() and not new.images:
        return "the message would be empty"
    return None


# ---- model.before / message.generated ----


def check_request(previous: ModelRequest, new: ModelRequest) -> str | None:
    if not isinstance(new.messages, list) or not all(isinstance(m, dict) and "role" in m for m in new.messages):
        return "messages must be a list of message objects"
    if not isinstance(new.tools, list) or not all(
            isinstance(t, dict) and isinstance(t.get("name"), str) for t in new.tools):
        return "tools must be a list of {name, description, parameters}"
    # Tools can only be hidden for this request: an added name would have no handler.
    known = {t["name"] for t in previous.tools}
    unknown = [t["name"] for t in new.tools if t["name"] not in known]
    if unknown:
        return f"cannot add tools {unknown}"
    return None


def reply_from_output(step: int, output: dict) -> ModelReply:
    return ModelReply(
        step=step,
        content=output.get("content") or "",
        tool_calls=[dict(c) for c in output.get("tool_calls") or []],
        usage=output.get("usage"),
    )


def check_reply(previous: ModelReply, new: ModelReply) -> str | None:
    if not isinstance(new.content, str):
        return "content must be a string"
    if not isinstance(new.tool_calls, list) or not all(isinstance(c, dict) for c in new.tool_calls):
        return "tool_calls must be a list"
    original = {c.get("id"): c for c in previous.tool_calls}
    for call in new.tool_calls:
        before = original.get(call.get("id"))
        if before is None:
            return "tool calls can be removed or changed, not added"
        if call.get("name") != before.get("name"):
            return f"tool call {call.get('id')} cannot be renamed"
        if not isinstance(call.get("arguments"), dict):
            return f"tool call {call.get('id')} arguments must be an object"
    if len({c.get("id") for c in new.tool_calls}) != len(new.tool_calls):
        return "duplicate tool call ids"
    return None


def apply_reply(output: dict, reply: ModelReply) -> dict:
    """The model output with the hook-changed content and tool calls."""
    return {
        **output,
        "content": reply.content,
        "tool_calls": [{"id": c["id"], "name": c["name"], "arguments": c["arguments"]}
                       for c in reply.tool_calls],
    }


# ---- tool.before / tool.after ----


def check_tool_call(previous: ToolCall, new: ToolCall) -> str | None:
    if new.id != previous.id or new.name != previous.name:
        return "only arguments can be changed"
    if not isinstance(new.arguments, dict):
        return "arguments must be an object"
    return None


def tool_result_from(value: Any) -> ToolResult | None:
    """SDK view of what a tool returned; None for results hooks cannot see.

    Built-in and plugin tools return str or ToolOutput. Anything else (e.g.
    a CAgent ToolResult from a future tool) passes through untouched.
    """
    if isinstance(value, str):
        return ToolResult(text=value, images=[])
    if isinstance(value, ToolOutput):
        return ToolResult(text=value.text, images=list(value.images))
    return None


def check_tool_result(previous: ToolResult, new: ToolResult) -> str | None:
    if not isinstance(new.text, str):
        return "text must be a string"
    if not isinstance(new.images, list) or not all(isinstance(i, ToolImage) for i in new.images):
        return "images must be a list of ToolImage"
    try:
        ToolOutput(new.text, list(new.images))
    except ValueError as e:
        return str(e)
    return None


def tool_value_from(result: ToolResult) -> str | ToolOutput:
    return ToolOutput(result.text, list(result.images)) if result.images else result.text
