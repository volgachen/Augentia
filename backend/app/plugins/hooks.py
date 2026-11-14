"""Hook dispatch (README §7.2).

``HookRunner`` is created per session by the CAgent adapter. For one hook
point it collects the handlers of plugins that are loaded, globally enabled
and enabled for this session, orders them by (priority, plugin id, method)
and runs them as a chain: each handler gets a deep copy of the current value
and its return value (if not None) becomes the input of the next one.

Errors:
- ``Reject(reason)`` stops the chain; the caller decides what rejecting means
  at that hook point (raised to the caller as ``HookRejected``).
- Any other exception, or the hook timing out, is handled per the hook's
  ``on_error``: ``skip`` ignores that plugin (logged, and reported through
  ``on_warning`` so the session shows it); ``fail`` raises ``HookFailed``,
  which fails the turn.
- A returned value of the wrong type is treated as an error the same way.

Notification hooks (``turn.end``) run in background tasks; their return
value and errors only get logged.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from app.plugins.context import SessionContext
from app.plugins.sdk import NOTIFY_HOOKS, HookSpec, Reject

if TYPE_CHECKING:
    from app.plugins.host import PluginHost

logger = logging.getLogger("augentia.plugins.hooks")


class HookRejected(Exception):
    def __init__(self, plugin_id: str, event: str, reason: str) -> None:
        super().__init__(f"{event} rejected by plugin '{plugin_id}': {reason}")
        self.plugin_id = plugin_id
        self.event = event
        self.reason = reason


class HookFailed(Exception):
    def __init__(self, plugin_id: str, event: str, detail: str) -> None:
        super().__init__(f"{event} hook of plugin '{plugin_id}' failed: {detail}")
        self.plugin_id = plugin_id
        self.event = event
        self.detail = detail


@dataclass(frozen=True)
class BoundHook:
    plugin_id: str
    spec: HookSpec
    fn: Callable[..., Awaitable[Any]]


WarningCallback = Callable[[str], Awaitable[None]]


class HookRunner:
    def __init__(
        self,
        host: "PluginHost",
        session_id: str,
        *,
        working_dir: str | None = None,
        on_warning: WarningCallback | None = None,
    ) -> None:
        self._host = host
        self._session_id = session_id
        self._working_dir = working_dir
        self._on_warning = on_warning
        self._background: set[asyncio.Task] = set()

    def set_working_dir(self, working_dir: str | None) -> None:
        self._working_dir = working_dir

    def handlers(self, event: str) -> list[BoundHook]:
        bound: list[BoundHook] = []
        for record in self._host.loaded():
            caps = record.capabilities
            if caps is None or record.instance is None:
                continue
            specs = [h for h in caps.hooks if h.event == event]
            if not specs or not self._host.session_enabled(record.id, self._session_id):
                continue
            for spec in specs:
                bound.append(BoundHook(record.id, spec, getattr(record.instance, spec.attr)))
        bound.sort(key=lambda b: (b.spec.priority, b.plugin_id, b.spec.attr))
        return bound

    def has(self, event: str) -> bool:
        return bool(self.handlers(event))

    def _ctx(self, plugin_id: str) -> SessionContext:
        return SessionContext(self._host, plugin_id, self._session_id, working_dir=self._working_dir)

    async def run(
        self,
        event: str,
        value: Any,
        *extra: Any,
        validate: Callable[[Any, Any], str | None] | None = None,
        changed_by: list[str] | None = None,
        reject_allowed: bool = True,
    ) -> Any:
        """Run an intercepting hook chain; returns the final value.

        Returns ``value`` itself (same object) when no handler changed it.
        ``extra`` are read-only arguments passed after the value (deep copied
        per handler). ``validate(previous, returned)`` may reject a returned
        value with a message; that counts as an error of that handler.
        ``changed_by`` collects the ids of plugins whose return value differed.
        With ``reject_allowed=False`` a Reject counts as an error of that handler.
        Raises HookRejected / HookFailed.
        """
        expected = type(value)
        for bound in self.handlers(event):
            arg = copy.deepcopy(value)
            extras = [copy.deepcopy(x) for x in extra]
            try:
                result = await asyncio.wait_for(
                    bound.fn(self._ctx(bound.plugin_id), arg, *extras), bound.spec.timeout)
            except Reject as e:
                if reject_allowed:
                    raise HookRejected(bound.plugin_id, event, e.reason or "rejected") from None
                await self._error(bound, event, f"Reject is not supported at {event}: {e.reason}")
                continue
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                await self._error(bound, event, f"timed out after {bound.spec.timeout:g}s")
                continue
            except Exception as e:
                logger.exception("hook %s of plugin %s failed session=%s", event, bound.plugin_id, self._session_id)
                await self._error(bound, event, f"{type(e).__name__}: {e}")
                continue
            if result is None:
                continue
            if not isinstance(result, expected):
                await self._error(bound, event,
                                  f"returned {type(result).__name__}, expected {expected.__name__} or None")
                continue
            problem = validate(value, result) if validate is not None else None
            if problem:
                await self._error(bound, event, f"returned an invalid value: {problem}")
                continue
            if changed_by is not None and result != value and bound.plugin_id not in changed_by:
                changed_by.append(bound.plugin_id)
            value = result
        return value

    async def _error(self, bound: BoundHook, event: str, detail: str) -> None:
        if bound.spec.on_error == "fail":
            raise HookFailed(bound.plugin_id, event, detail)
        message = f"plugin '{bound.plugin_id}' {event} hook skipped: {detail}"
        logger.warning("session=%s %s", self._session_id, message)
        if self._on_warning is not None:
            try:
                await self._on_warning(message)
            except Exception:
                logger.exception("hook warning callback failed")

    def notify(self, event: str, *args: Any) -> None:
        """Fire a notification hook in the background (never blocks, never raises)."""
        assert event in NOTIFY_HOOKS, event
        for bound in self.handlers(event):
            task = asyncio.create_task(
                self._notify_one(bound, event, [copy.deepcopy(a) for a in args]),
                name=f"hook:{event}:{bound.plugin_id}",
            )
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _notify_one(self, bound: BoundHook, event: str, args: list[Any]) -> None:
        try:
            await asyncio.wait_for(bound.fn(self._ctx(bound.plugin_id), *args), bound.spec.timeout)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning("hook %s of plugin %s timed out session=%s", event, bound.plugin_id, self._session_id)
        except Exception:
            logger.exception("hook %s of plugin %s failed session=%s", event, bound.plugin_id, self._session_id)

    async def drain(self, timeout: float = 5.0) -> None:
        """Wait (bounded) for background notification hooks; used on stop and in tests."""
        pending = list(self._background)
        if not pending:
            return
        done, still = await asyncio.wait(pending, timeout=timeout)
        for task in still:
            task.cancel()
