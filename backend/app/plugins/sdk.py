"""Public interface for Augentia plugins (README §4).

Plugins import host APIs only from this module; everything else under ``app``
may change without notice. A plugin is a ``Plugin`` subclass whose methods are
marked with the decorators below. Decorators only attach declarations; the
host (loader.py) collects and validates them when the plugin is loaded.

Status: P0 declares and validates tools/hooks/services/actions so the plugin
page can show them. Wiring them into sessions comes in P1 (tools), P3 (hooks)
and P4 (services, actions).
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.adapters.tools.base import ToolImage, ToolOutput

if TYPE_CHECKING:
    from fastapi import APIRouter

    from app.plugins.context import PluginContext, SessionContext

__all__ = [
    "Plugin", "PluginContext", "SessionContext", "ToolContext", "Reject", "ToolImage", "ToolOutput",
    "tool", "hook", "service", "action",
    "IncomingMessage", "ModelRequest", "ModelReply", "ToolCall", "ToolResult", "TurnSummary",
    "HOOK_EVENTS", "ToolSpec", "HookSpec", "ServiceSpec", "ActionSpec",
]

# Hook points, in turn order (README §7.2).
HOOK_EVENTS = (
    "message.received",
    "model.before",
    "message.generated",
    "tool.before",
    "tool.after",
    "turn.end",
)
# Hooks that run in the background; they cannot change or fail the turn.
NOTIFY_HOOKS = frozenset({"turn.end"})

TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
ACTION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Model APIs cap function names at 64 characters.
MAX_TOOL_FULL_NAME = 64
MAX_HOOK_TIMEOUT = 60.0

_TOOL_ATTR = "__augentia_tool__"
_HOOK_ATTR = "__augentia_hooks__"
_SERVICE_ATTR = "__augentia_service__"
_ACTION_ATTR = "__augentia_action__"


class Reject(Exception):
    """Raised by an intercepting hook to refuse the message, request or call."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Plugin:
    """Base class for plugins. One instance per plugin, shared by all sessions."""

    async def on_load(self, ctx: "PluginContext") -> None:
        """Called after the plugin is imported and enabled."""

    async def on_unload(self, ctx: "PluginContext") -> None:
        """Called when the plugin is disabled or the backend shuts down."""

    async def on_config_changed(self, ctx: "PluginContext", old: dict, new: dict) -> None:
        """Called after the global config is saved.

        Not overriding it means: the host restarts the plugin's global
        services so they read the new config.
        """

    async def on_session_enabled(self, ctx: "SessionContext") -> None:
        """The plugin became active for a session (switch turned on, plugin
        loaded, or backend started with it on). Typical use: push the
        initial UI card, e.g. a Connect button. Autostart session services
        are started right after this returns."""

    async def on_session_disabled(self, ctx: "SessionContext") -> None:
        """The plugin is no longer active for a session (switch off or session
        deleted). Its session services are already stopped and its UI card
        is cleared afterwards. Not called when the plugin itself unloads."""

    def routes(self, router: "APIRouter") -> None:
        """Register HTTP routes (README §8). Mounted from P6 on."""


# ---- hook payloads (README §7.2) ----
#
# Hooks receive a deep copy. An intercepting hook returns None (no change) or
# an object of the same type; only the fields listed as changeable for that
# hook are taken from it, the rest are ignored.


@dataclass
class IncomingMessage:
    """message.received. Changeable: content, images."""

    content: str
    images: list[dict] | None
    # None: typed by the user; "session:<id>": sent by another session;
    # "plugin:<id>": sent by a plugin.
    source: str | None
    turn_overrides: dict | None


@dataclass
class ModelRequest:
    """model.before. Changeable: messages, tools (this request only, not saved)."""

    step: int
    messages: list[dict]
    tools: list[dict]


@dataclass
class ModelReply:
    """message.generated. Changeable: content; tool_calls may be removed or
    get new arguments, but not added or renamed."""

    step: int
    content: str
    tool_calls: list[dict]
    usage: dict | None

    @property
    def final(self) -> bool:
        return not self.tool_calls


@dataclass
class ToolCall:
    """tool.before (changeable: arguments) and tool.after (read-only)."""

    id: str
    name: str
    arguments: dict
    message_id: str | None


@dataclass
class ToolResult:
    """tool.after. Changeable: text, images."""

    text: str
    images: list[ToolImage]


@dataclass
class TurnSummary:
    """turn.end (notification only)."""

    stop_reason: str
    # Text of the turn's last model reply (after message.generated hooks).
    final_text: str | None
    # Model calls made in the turn.
    steps: int
    tool_calls: int
    # Usage reported by the last model call.
    usage: dict | None


# ---- declarations ----


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    requires_approval: bool = True
    attr: str = ""


@dataclass(frozen=True)
class HookSpec:
    event: str
    priority: int = 100
    timeout: float = 10.0
    on_error: str = "skip"
    attr: str = ""


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    scope: str = "global"
    restart: str = "on-failure"
    autostart: bool = True
    attr: str = ""


@dataclass(frozen=True)
class ActionSpec:
    id: str
    attr: str = ""


def _require_coroutine(fn: Callable, what: str) -> None:
    if not inspect.iscoroutinefunction(fn):
        raise TypeError(f"{what} {getattr(fn, '__name__', fn)!r} must be an async function")


def tool(
    name: str,
    *,
    description: str,
    parameters: dict,
    requires_approval: bool = True,
) -> Callable[[Callable], Callable]:
    """Expose a method as an agent tool named ``<plugin_id>__<name>``."""
    if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
        raise ValueError(f"tool name {name!r} must match {TOOL_NAME_RE.pattern}")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"tool {name!r} needs a non-empty description")
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise ValueError(f"tool {name!r} parameters must be a JSON Schema with type 'object'")
    try:
        Draft202012Validator.check_schema(parameters)
    except SchemaError as e:
        raise ValueError(f"tool {name!r} parameters are not a valid JSON Schema: {e.message}") from e
    if not isinstance(requires_approval, bool):
        raise ValueError(f"tool {name!r} requires_approval must be a boolean")
    spec = ToolSpec(name, description, parameters, requires_approval)

    def decorate(fn: Callable) -> Callable:
        _require_coroutine(fn, "tool")
        setattr(fn, _TOOL_ATTR, spec)
        return fn

    return decorate


def hook(
    event: str,
    *,
    priority: int = 100,
    timeout: float = 10.0,
    on_error: str = "skip",
) -> Callable[[Callable], Callable]:
    """Run a method at a hook point (README §7.2). One method may carry several."""
    if event not in HOOK_EVENTS:
        raise ValueError(f"unknown hook {event!r}; known: {', '.join(HOOK_EVENTS)}")
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValueError(f"hook {event!r} priority must be an integer")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_HOOK_TIMEOUT:
        raise ValueError(f"hook {event!r} timeout must be in (0, {MAX_HOOK_TIMEOUT:g}] seconds")
    if on_error not in ("skip", "fail"):
        raise ValueError(f"hook {event!r} on_error must be 'skip' or 'fail'")
    if on_error == "fail" and event in NOTIFY_HOOKS:
        raise ValueError(f"hook {event!r} runs in the background; on_error='fail' has no effect")
    spec = HookSpec(event, priority, float(timeout), on_error)

    def decorate(fn: Callable) -> Callable:
        _require_coroutine(fn, "hook")
        setattr(fn, _HOOK_ATTR, (*getattr(fn, _HOOK_ATTR, ()), spec))
        return fn

    return decorate


def service(
    name: str,
    *,
    scope: str = "global",
    restart: str = "on-failure",
    autostart: bool = True,
) -> Callable[[Callable], Callable]:
    """Run a method as a long-lived background task (README §6)."""
    if not isinstance(name, str) or not SERVICE_NAME_RE.match(name):
        raise ValueError(f"service name {name!r} must match {SERVICE_NAME_RE.pattern}")
    if scope not in ("global", "session"):
        raise ValueError(f"service {name!r} scope must be 'global' or 'session'")
    if restart not in ("never", "on-failure", "always"):
        raise ValueError(f"service {name!r} restart must be 'never', 'on-failure' or 'always'")
    if not isinstance(autostart, bool):
        raise ValueError(f"service {name!r} autostart must be a boolean")
    spec = ServiceSpec(name, scope, restart, autostart)

    def decorate(fn: Callable) -> Callable:
        _require_coroutine(fn, "service")
        setattr(fn, _SERVICE_ATTR, spec)
        return fn

    return decorate


def action(action_id: str) -> Callable[[Callable], Callable]:
    """Handle a UI button / API call with this id (README §5.4)."""
    if not isinstance(action_id, str) or not ACTION_ID_RE.match(action_id):
        raise ValueError(f"action id {action_id!r} must match {ACTION_ID_RE.pattern}")
    spec = ActionSpec(action_id)

    def decorate(fn: Callable) -> Callable:
        _require_coroutine(fn, "action")
        setattr(fn, _ACTION_ATTR, spec)
        return fn

    return decorate


# ---- collection (used by the loader) ----


@dataclass(frozen=True)
class Capabilities:
    tools: tuple[ToolSpec, ...] = ()
    hooks: tuple[HookSpec, ...] = ()
    services: tuple[ServiceSpec, ...] = ()
    actions: tuple[ActionSpec, ...] = ()
    has_routes: bool = False


def collect_capabilities(cls: type, plugin_id: str) -> Capabilities:
    """Gather the decorated members of a Plugin subclass and check for clashes.

    Walks the MRO so a subclass overriding a member replaces the base one.
    """
    members: dict[str, Any] = {}
    for klass in reversed(cls.__mro__):
        members.update(vars(klass))
    tools: list[ToolSpec] = []
    hooks: list[HookSpec] = []
    services: list[ServiceSpec] = []
    actions: list[ActionSpec] = []
    for attr, value in members.items():
        fn = value.__func__ if isinstance(value, (staticmethod, classmethod)) else value
        spec = getattr(fn, _TOOL_ATTR, None)
        if isinstance(spec, ToolSpec):
            tools.append(_with_attr(spec, attr))
        for hook_spec in getattr(fn, _HOOK_ATTR, ()):
            if isinstance(hook_spec, HookSpec):
                hooks.append(_with_attr(hook_spec, attr))
        spec = getattr(fn, _SERVICE_ATTR, None)
        if isinstance(spec, ServiceSpec):
            services.append(_with_attr(spec, attr))
        spec = getattr(fn, _ACTION_ATTR, None)
        if isinstance(spec, ActionSpec):
            actions.append(_with_attr(spec, attr))

    _unique((t.name for t in tools), "tool")
    _unique((s.name for s in services), "service")
    _unique((a.id for a in actions), "action")
    for spec in tools:
        full = tool_full_name(plugin_id, spec.name)
        if len(full) > MAX_TOOL_FULL_NAME:
            raise ValueError(f"tool name {full!r} is longer than {MAX_TOOL_FULL_NAME} characters")
    return Capabilities(
        tools=tuple(sorted(tools, key=lambda t: t.name)),
        hooks=tuple(sorted(hooks, key=lambda h: (HOOK_EVENTS.index(h.event), h.priority, h.attr))),
        services=tuple(sorted(services, key=lambda s: s.name)),
        actions=tuple(sorted(actions, key=lambda a: a.id)),
        has_routes=getattr(cls, "routes", None) is not Plugin.routes,
    )


def tool_full_name(plugin_id: str, name: str) -> str:
    return f"{plugin_id}__{name}"


def _with_attr(spec, attr: str):
    from dataclasses import replace

    return replace(spec, attr=attr)


def _unique(names, what: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValueError(f"duplicate {what} {name!r}")
        seen.add(name)


def __getattr__(name: str):
    # The context classes live in context.py, which imports this module;
    # resolve them lazily so `from app.plugins.sdk import PluginContext` works.
    if name in ("PluginContext", "SessionContext", "ToolContext"):
        from app.plugins import context

        return getattr(context, name)
    raise AttributeError(name)
