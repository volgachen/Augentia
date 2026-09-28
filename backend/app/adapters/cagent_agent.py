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

from app.adapters.tools.base import BaseTool


def _wrap_tool(base_tool: BaseTool) -> Tool:
    """Adapt a registered BaseTool to CAgent's Tool (handler receives store/ctx/agent_cls)."""

    async def handler(*, store=None, ctx=None, agent_cls=None, **kwargs):
        # The framework's injected dependencies are not tool parameters.
        return await base_tool.execute(**kwargs)

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
) -> type[BaseAgent]:
    """Build the per-session BaseAgent subclass bound to one adapter instance."""

    class SessionAgent(BaseAgent):
        NAME = name
        SYSTEM_PROMPT = system_prompt or "You are a helpful assistant."
        MAX_STEPS = max_steps
        # Compression stays off by default in PR1 (thresholds None); enabling it
        # per agent template config flips it on without code changes.
        COMPRESSION_TOKEN_THRESHOLD = compression_token_threshold
        COMPRESSION_TURN_THRESHOLD = compression_turn_threshold

        @classmethod
        def get_tools(cls, ctx):
            return {tool.name: _wrap_tool(tool) for tool in adapter.tools}

        @classmethod
        async def approve_tool_call(cls, store, ctx, tool_call, tool):
            return await adapter.gate_tool_call(store, ctx, tool_call)

    SessionAgent.__name__ = f"SessionAgent[{name}]"
    SessionAgent.__qualname__ = SessionAgent.__name__
    return SessionAgent
