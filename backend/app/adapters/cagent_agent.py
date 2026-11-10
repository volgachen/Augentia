"""Per-session dynamic CAgent agent class bridging Augentia's runtime.

CAgent's hooks are all classmethods while every Augentia concern here (confirm
futures, the event queue, permission config, tool instances) is per-session
instance state. ``make_agent_class`` closes over one ``CAgentAdapter`` instance
and returns a subclass whose classmethods reach back into it, giving the
framework class-shaped hooks over instance-shaped state.

The generated class is created once per adapter (per session) in the adapter's
``start()`` and reused for every turn of that session.
"""

from __future__ import annotations

from cagent import BaseAgent, Tool

from app.adapters.tools.base import BaseTool, ToolOutput


def _call_context(ctx) -> dict | None:
    """The session, issuing assistant message and tool call of the running call.

    CAgent sets ctx.current_tool_call before each handler; the assistant
    message carrying that call is already stored, so its id is the row id.
    Older CAgent versions have no current_tool_call: the context is None.
    """
    call = getattr(ctx, "current_tool_call", None) if ctx is not None else None
    if not call:
        return None
    message_id = None
    for message in reversed(ctx.messages):
        if message.get("role") == "assistant" and any(
                item.get("id") == call["id"] for item in message.get("tool_calls") or []):
            message_id = message.get("id")
            break
    return {"session_id": ctx.session_id, "message_id": message_id, "tool_call_id": call["id"]}


def _wrap_tool(base_tool: BaseTool, adapter) -> Tool:
    """Adapt a registered BaseTool to CAgent's Tool (handler receives store/ctx/agent_cls).

    A ToolOutput result (text + image bytes) is converted by the adapter into
    a CAgent ToolResult with stored image references, or into plain text when
    the agent can't take images.
    """

    async def handler(*, store=None, ctx=None, agent_cls=None, **kwargs):
        # The framework's injected dependencies are not tool parameters.
        base_tool.call_context = _call_context(ctx)
        result = await base_tool.execute(**kwargs)
        if isinstance(result, ToolOutput):
            return await adapter.tool_output_result(result)
        return result

    return Tool(
        handler,
        description=base_tool.description,
        parameters=base_tool.parameters_schema(),
    )


def make_agent_class(
    adapter,
    *,
    name: str,
    system_prompt: str,
    max_steps: int = 50,
    compression_token_threshold: int | None = None,
    compression_turn_threshold: int | None = None,
    turn_compression_tool_call_threshold: int | None = None,
    turn_compression_instruction: str | None = None,
    turn_compression_marker: str | None = None,
    compression_instruction: str | None = None,
    compression_send_tools: bool | None = None,
    compression_model=None,
    tool_image_limit: int | None = None,
    step_reminder_at: int | None = None,
    step_reminder: str | None = None,
    tool_limit_at: int | None = None,
    tool_limit_message: str | None = None,
) -> type[BaseAgent]:
    """Build the per-session BaseAgent subclass bound to one adapter instance."""
    for key, value in (("turn_compression_instruction", turn_compression_instruction),
                       ("compression_instruction", compression_instruction)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{key} must be a non-empty string or None")
    # Unlike the instructions, the marker may be empty ("" is a valid override).
    if turn_compression_marker is not None and not isinstance(turn_compression_marker, str):
        raise ValueError("turn_compression_marker must be a string or None")
    if compression_send_tools is not None and not isinstance(compression_send_tools, bool):
        raise ValueError("compression_send_tools must be a boolean or None")
    if step_reminder_at is not None and (isinstance(step_reminder_at, bool) or not isinstance(step_reminder_at, int)
                                         or step_reminder_at < 1):
        raise ValueError("step_reminder_at must be a positive integer or None")
    if step_reminder is not None and (not isinstance(step_reminder, str) or not step_reminder.strip()):
        raise ValueError("step_reminder must be a non-empty string or None")
    if tool_limit_at is not None and (isinstance(tool_limit_at, bool) or not isinstance(tool_limit_at, int)
                                      or tool_limit_at < 1):
        raise ValueError("tool_limit_at must be a positive integer or None")
    if tool_limit_message is not None and (not isinstance(tool_limit_message, str) or not tool_limit_message.strip()):
        raise ValueError("tool_limit_message must be a non-empty string or None")

    class SessionAgent(BaseAgent):
        NAME = name
        SYSTEM_PROMPT = system_prompt or "You are a helpful assistant."
        MAX_STEPS = max_steps
        # Compression stays off by default (thresholds None); enabling it per
        # agent template config flips it on without code changes.
        COMPRESSION_TOKEN_THRESHOLD = compression_token_threshold
        COMPRESSION_TURN_THRESHOLD = compression_turn_threshold
        # Turn-scoped compression: once one turn makes this many tool calls,
        # the turn is replaced by a summary before turn_end (None disables).
        TURN_COMPRESSION_TOOL_CALL_THRESHOLD = turn_compression_tool_call_threshold
        # Optional custom instruction for the turn-compression summarizer;
        # falls back to the CAgent default when not set.
        TURN_COMPRESSION_INSTRUCTION = (
            turn_compression_instruction or BaseAgent.TURN_COMPRESSION_INSTRUCTION)
        # Optional custom text for the user message that, in the summarizer
        # request, replaces the turn start mark (where compression begins).
        # None -> CAgent default; "" is honored as an empty marker.
        TURN_COMPRESSION_MARKER = (
            BaseAgent.TURN_COMPRESSION_MARKER if turn_compression_marker is None
            else turn_compression_marker)
        # Optional custom instruction for whole-history compression (manual
        # Compress button and threshold-triggered); CAgent default otherwise.
        COMPRESSION_INSTRUCTION = (
            compression_instruction or BaseAgent.COMPRESSION_INSTRUCTION)
        # Summarizer for threshold-triggered compression; None -> the agent's
        # own model. (Manual compression passes it to compress_session.)
        COMPRESSION_MODEL = compression_model
        # Summarizer requests carry the agent's tools with tool_choice="none"
        # (CAgent default): with tools=[] some gateways (Claude via Bedrock)
        # still let the model call tools when the history is full of tool
        # calls, which fails the compression. config.compression_send_tools
        # overrides it; None keeps the CAgent default. The model callable must
        # accept tool_choice (``_make_model`` does).
        COMPRESSION_SEND_TOOLS = (
            BaseAgent.COMPRESSION_SEND_TOOLS if compression_send_tools is None
            else compression_send_tools)
        # Only the newest N tool-result images are sent to the model.
        TOOL_IMAGE_LIMIT = tool_image_limit
        # From this many model calls on, the last tool result of each step gets
        # the reminder appended (CAgent STEP_REMINDER_AT; None disables it).
        STEP_REMINDER_AT = step_reminder_at
        STEP_REMINDER = step_reminder or BaseAgent.STEP_REMINDER
        # From this many model calls on, tool calls are not executed: each gets
        # the limit message, so the model can only wrap up (CAgent TOOL_LIMIT_AT).
        TOOL_LIMIT_AT = tool_limit_at
        TOOL_LIMIT_MESSAGE = tool_limit_message or BaseAgent.TOOL_LIMIT_MESSAGE

        @classmethod
        def get_tools(cls, ctx):
            return {tool.name: _wrap_tool(tool, adapter) for tool in adapter.tools}

        @classmethod
        async def approve_tool_call(cls, store, ctx, tool_call, tool):
            return await adapter.gate_tool_call(store, ctx, tool_call)

        @classmethod
        async def resolve_image(cls, store, ctx, image):
            # Stored references are att:<sha256>; bytes come from the local
            # attachment store and reach the model as a data URL.
            return await adapter.resolve_image(image)

    SessionAgent.__name__ = f"SessionAgent[{name}]"
    SessionAgent.__qualname__ = SessionAgent.__name__
    return SessionAgent
