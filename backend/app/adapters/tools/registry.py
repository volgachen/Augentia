"""Tool registry: built-in tools plus tools contributed by loaded plugins.

Built-in tools register their class with ``@register_tool`` at import time.
Plugin tools are registered by the plugin host when a plugin loads and
removed when it unloads (README in app/plugins, §7.1). Each registry entry
builds a fresh tool instance per session.
"""

import re
from dataclasses import dataclass
from typing import Callable, Literal, Type

from app.adapters.tools.base import BaseTool

ToolSource = Literal["builtin", "plugin"]


@dataclass(frozen=True)
class ToolEntry:
    name: str
    description: str
    requires_approval: bool
    factory: Callable[[], BaseTool]
    source: ToolSource = "builtin"
    plugin_id: str | None = None
    plugin_name: str | None = None


_REGISTRY: dict[str, ToolEntry] = {}
# <plugin_id>__<tool>; plugin ids follow app.plugins.manifest.PLUGIN_ID_RE.
_PLUGIN_TOOL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}__[A-Za-z0-9_-]+$")


def is_plugin_tool_name(name: str) -> bool:
    return bool(_PLUGIN_TOOL_RE.match(name)) and len(name) <= 64


def register_tool(cls: Type[BaseTool]) -> Type[BaseTool]:
    _REGISTRY[cls.name] = ToolEntry(
        name=cls.name,
        description=cls.description,
        requires_approval=cls.requires_approval,
        factory=cls,
    )
    return cls


def register_plugin_tools(plugin_id: str, entries: list[ToolEntry]) -> None:
    """Register all tools of one plugin, or none if any name is already taken."""
    taken = [e.name for e in entries if e.name in _REGISTRY and _REGISTRY[e.name].plugin_id != plugin_id]
    if taken:
        raise ValueError(f"tool name(s) already registered: {', '.join(taken)}")
    unregister_plugin_tools(plugin_id)
    for entry in entries:
        _REGISTRY[entry.name] = entry


def unregister_plugin_tools(plugin_id: str) -> None:
    for name in [n for n, e in _REGISTRY.items() if e.plugin_id == plugin_id]:
        del _REGISTRY[name]


def get_entry(name: str) -> ToolEntry | None:
    return _REGISTRY.get(name)


def load_tools(names: list[str]) -> list[BaseTool]:
    """Instantiate the named tools; ValueError if any is unknown."""
    missing = [n for n in names if n not in _REGISTRY]
    if missing:
        raise ValueError(f"Unknown tools: {missing}. Available: {list(_REGISTRY)}")
    return [_REGISTRY[n].factory() for n in names]


def resolve_tools(names: list[str]) -> tuple[list[BaseTool], list[str]]:
    """Instantiate the available tools among names; the rest become warnings.

    Used at session start: an agent may list a plugin tool whose plugin is
    disabled, broken or removed. The session still starts without that tool.
    """
    tools: list[BaseTool] = []
    warnings: list[str] = []
    for name in names:
        entry = _REGISTRY.get(name)
        if entry is None:
            warnings.append(f"tool '{name}' is not available: {unavailable_reason(name)}")
            continue
        tools.append(entry.factory())
    return tools, warnings


def unavailable_reason(name: str) -> str:
    """Why a tool name is not in the registry (best effort, for messages)."""
    plugin_id, sep, _ = name.partition("__")
    if sep:
        from app.plugins.host import PluginNotFound, get_plugin_host

        try:
            record = get_plugin_host().get(plugin_id)
        except PluginNotFound:
            return f"plugin '{plugin_id}' is not installed"
        if record.state == "disabled":
            return f"plugin '{plugin_id}' is disabled"
        if record.state == "error":
            return f"plugin '{plugin_id}' failed to load: {record.error}"
        return f"plugin '{plugin_id}' does not provide this tool"
    return "unknown tool"


def list_available() -> list[str]:
    return list(_REGISTRY.keys())


def list_entries() -> list[ToolEntry]:
    """Built-in tools first (registration order), then plugin tools by name."""
    builtin = [e for e in _REGISTRY.values() if e.source == "builtin"]
    plugin = sorted((e for e in _REGISTRY.values() if e.source == "plugin"), key=lambda e: e.name)
    return builtin + plugin


def default_approvals() -> dict[str, bool]:
    """Map each registered tool name to its class-level requires_approval default.
    This is the backend-wide baseline an agent's config.tool_approvals overrides."""
    return {name: entry.requires_approval for name, entry in _REGISTRY.items()}
