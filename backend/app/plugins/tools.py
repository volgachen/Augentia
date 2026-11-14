"""Plugin tools as registry entries (README §7.1).

Each plugin tool becomes a ``ToolEntry`` whose factory builds a fresh
``PluginTool`` per session. The tool resolves the plugin instance at call
time, so a plugin disabled mid-session yields an error result instead of
running stale code, and a re-enabled plugin is picked up without restarting
the session.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from app.adapters.tools.base import BaseTool, ToolOutput
from app.adapters.tools.registry import ToolEntry
from app.plugins.context import ToolCallInfo, ToolContext
from app.plugins.sdk import ToolSpec, tool_full_name

if TYPE_CHECKING:
    from app.plugins.host import PluginHost

logger = logging.getLogger("augentia.plugins.tools")

# Same text style as CAgent's own tool failures, so the model reads them alike.
_FAIL_PREFIX = "工具执行失败："


class PluginTool(BaseTool):
    def __init__(self, host: "PluginHost", plugin_id: str, spec: ToolSpec) -> None:
        self._host = host
        self.plugin_id = plugin_id
        self.spec_name = spec.name
        self.name = tool_full_name(plugin_id, spec.name)
        self.description = spec.description
        self.requires_approval = spec.requires_approval
        self._parameters = spec.parameters
        self._validator = Draft202012Validator(spec.parameters)

    def parameters_schema(self) -> dict:
        return self._parameters

    async def execute(self, **kwargs: Any) -> "str | ToolOutput":
        resolved = self._host.tool_target(self.plugin_id, self.spec_name)
        if isinstance(resolved, str):
            return f"{_FAIL_PREFIX}{self.name} 不可用（{resolved}）"
        method = resolved

        errors = sorted(self._validator.iter_errors(kwargs), key=lambda e: list(e.absolute_path))
        if errors:
            details = "; ".join(
                f"{'.'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in errors[:5]
            )
            return f"{_FAIL_PREFIX}{self.name} 参数不符合 schema（{details}）"

        if self.session_id is None:
            return f"{_FAIL_PREFIX}{self.name} 只能在会话中调用"
        call = self.call_context or {}
        ctx = ToolContext(
            self._host,
            self.plugin_id,
            self.session_id,
            ToolCallInfo(
                tool_call_id=call.get("tool_call_id"),
                message_id=call.get("message_id"),
                name=self.name,
            ),
            working_dir=self.working_dir,
        )
        try:
            inspect.signature(method).bind(ctx, **kwargs)
        except TypeError as e:
            return f"{_FAIL_PREFIX}{self.name} 参数不匹配（{e}）"
        try:
            result = await method(ctx, **kwargs)
        except Exception as e:
            # Plugin code is not ours to trust: an exception escaping here would
            # fail the whole turn in CAgent. Hand it to the model instead.
            logger.exception("plugin tool %s failed session=%s", self.name, self.session_id)
            return f"{_FAIL_PREFIX}{type(e).__name__}: {e}"
        if result is None:
            return ""
        if isinstance(result, (str, ToolOutput)):
            return result
        logger.warning("plugin tool %s returned %s; converting to str", self.name, type(result).__name__)
        return str(result)


def tool_entries(host: "PluginHost", plugin_id: str, specs: tuple[ToolSpec, ...],
                 plugin_name: str | None = None) -> list[ToolEntry]:
    entries = []
    for spec in specs:
        entries.append(ToolEntry(
            name=tool_full_name(plugin_id, spec.name),
            description=spec.description,
            requires_approval=spec.requires_approval,
            factory=lambda spec=spec: PluginTool(host, plugin_id, spec),
            source="plugin",
            plugin_id=plugin_id,
            plugin_name=plugin_name,
        ))
    return entries
