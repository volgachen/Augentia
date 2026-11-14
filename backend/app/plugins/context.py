"""Context objects handed to plugin code (README §5).

Three levels, each including the one above:

    PluginContext      on_load, global services, HTTP routes, global actions
      SessionContext   hooks, session services, session actions
        ToolContext    one tool call

APIs on every context: global_config, data_dir, log, sessions, events, ui,
services. ``ui`` is bound to the session on a SessionContext and to the
plugin otherwise.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from app.plugins import settings

if TYPE_CHECKING:
    from app.plugins.host import PluginHost
    from app.plugins.services import ServiceState

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


class PluginLogger:
    """Per-plugin logger: backend log, plus plugin_service_logs and the live stream.

    Debug lines only go to the backend log. Lines written while a service
    runs are attributed to that service run (via a context variable set by
    the service wrapper).
    """

    def __init__(self, host: "PluginHost", plugin_id: str, session_id: str | None = None) -> None:
        self._host = host
        self._plugin_id = plugin_id
        self._session_id = session_id
        self._logger = logging.getLogger(f"augentia.plugin.{plugin_id}")

    def _log(self, level: str, msg: str, fields: dict[str, Any]) -> None:
        text = str(msg)
        if fields:
            text = f"{text} " + " ".join(f"{k}={v!r}" for k, v in fields.items())
        self._logger.log(_LEVELS[level], "%s%s", f"[session {self._session_id}] " if self._session_id else "", text)
        if level == "debug":
            return
        from app.plugins.services_context import current_run_id

        self._host.services.log_nowait(self._plugin_id, text, level=level, run_id=current_run_id.get(),
                                       session_id=self._session_id)

    def debug(self, msg: str, **fields: Any) -> None:
        self._log("debug", msg, fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._log("info", msg, fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._log("warning", msg, fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._log("error", msg, fields)


@dataclass(frozen=True)
class SessionInfo:
    id: str
    title: str | None
    agent_id: str
    status: str
    working_dir: str | None


class SessionsAPI:
    def __init__(self, host: "PluginHost", plugin_id: str) -> None:
        self._host = host
        self._plugin_id = plugin_id

    def _db(self):
        db = self._host.db
        if db is None:
            raise RuntimeError("sessions API is not available (no database bound)")
        return db

    @staticmethod
    def _info(session) -> SessionInfo:
        status = session.status.value if hasattr(session.status, "value") else str(session.status)
        return SessionInfo(id=session.id, title=session.title, agent_id=session.agent_id,
                           status=status, working_dir=session.working_dir)

    async def get(self, session_id: str) -> SessionInfo:
        """KeyError if the session does not exist."""
        return self._info(await self._db().get_session(session_id))

    async def list(self) -> list[SessionInfo]:
        return [self._info(s) for s in await self._db().list_sessions()]

    async def send(self, session_id: str, content: str, *, images: list[dict] | None = None,
                   label: str | None = None) -> None:
        """Queue a user message in a session, starting its runner if needed.

        The message source is always ``plugin:<plugin_id>``; ``label`` is a
        free-form display hint stored in metadata.plugin_label.
        """
        if not isinstance(content, str) or (not content.strip() and not images):
            raise ValueError("content must be a non-empty string")
        await self._host.send_session_message(self._plugin_id, session_id, content, images=images, label=label)


class EventsAPI:
    def __init__(self, host: "PluginHost", plugin_id: str) -> None:
        self._host = host
        self._plugin_id = plugin_id

    def subscribe(self, event_type: str, handler: Callable[[Any], Awaitable[None]]) -> Callable[[], None]:
        """Call handler for each event of this type; returns an unsubscribe function.

        Subscriptions end automatically when the plugin unloads.
        """
        return self._host.event_bus.subscribe_to(event_type, handler, owner=self._plugin_id)


class UIAPI:
    """Push a declarative status card (README §5.4). Each update replaces the previous one."""

    def __init__(self, host: "PluginHost", plugin_id: str, session_id: str | None) -> None:
        self._host = host
        self._plugin_id = plugin_id
        self._session_id = session_id

    async def update(self, state: dict) -> None:
        self._host.services.set_ui(self._plugin_id, self._session_id, state)

    def update_nowait(self, state: dict) -> None:
        """Same as update(), for synchronous callbacks."""
        self._host.services.set_ui(self._plugin_id, self._session_id, state)

    async def clear(self) -> None:
        await self.update({"status": "idle"})


class ServiceHandle:
    def __init__(self, host: "PluginHost", plugin_id: str, name: str, session_id: str | None) -> None:
        self._host = host
        self.plugin_id = plugin_id
        self.name = name
        self.session_id = session_id

    @property
    def state(self) -> "ServiceState | None":
        return self._host.services.get(self.plugin_id, self.name, self.session_id)

    @property
    def running(self) -> bool:
        return self._host.services.is_active(self.plugin_id, self.name, self.session_id)

    async def start(self) -> None:
        """Start the service. Does not wait for it: returning from start()
        only means its task was created."""
        await self._host.start_service(self.plugin_id, self.name, self.session_id)

    async def stop(self) -> None:
        """Stop the service and wait (up to a few seconds) for it to finish.

        Do not call this from inside the same service: it would wait for itself.
        """
        if asyncio.current_task() is not None and self._host.is_service_task(
                self.plugin_id, self.name, self.session_id):
            raise RuntimeError("a service cannot stop itself; return from the service function instead")
        await self._host.services.stop(self.plugin_id, self.name, self.session_id)

    async def restart(self) -> None:
        await self.stop()
        await self.start()


class ServicesAPI:
    def __init__(self, host: "PluginHost", plugin_id: str, default_session: str | None) -> None:
        self._host = host
        self._plugin_id = plugin_id
        self._default_session = default_session

    def get(self, name: str, session_id: str | None = None) -> ServiceHandle:
        """Handle of one of this plugin's services.

        For a session-scoped service, session_id defaults to the context's session.
        """
        spec = self._host.service_spec(self._plugin_id, name)
        if spec is None:
            raise KeyError(f"plugin '{self._plugin_id}' has no service '{name}'")
        if spec.scope == "session":
            session_id = session_id or self._default_session
            if session_id is None:
                raise ValueError(f"service '{name}' is per session; pass session_id")
        else:
            session_id = None
        return ServiceHandle(self._host, self._plugin_id, name, session_id)


class PluginContext:
    def __init__(self, host: "PluginHost", plugin_id: str) -> None:
        self._host = host
        self.plugin_id = plugin_id
        self.log = PluginLogger(host, plugin_id)
        self.sessions = SessionsAPI(host, plugin_id)
        self.events = EventsAPI(host, plugin_id)
        self.ui = UIAPI(host, plugin_id, None)
        self.services = ServicesAPI(host, plugin_id, None)

    @property
    def global_config(self) -> dict[str, Any]:
        """Effective global config (schema defaults applied). A fresh copy each time."""
        return self._host.effective_global_config(self.plugin_id)

    @property
    def data_dir(self) -> Path:
        path = settings.augentia_home() / "plugin_data" / self.plugin_id
        path.mkdir(parents=True, exist_ok=True)
        return path


class SessionContext(PluginContext):
    def __init__(
        self,
        host: "PluginHost",
        plugin_id: str,
        session_id: str,
        *,
        working_dir: str | None = None,
    ) -> None:
        super().__init__(host, plugin_id)
        self.session_id = session_id
        self.working_dir = working_dir
        self.log = PluginLogger(host, plugin_id, session_id)
        self.ui = UIAPI(host, plugin_id, session_id)
        self.services = ServicesAPI(host, plugin_id, session_id)

    @property
    def config(self) -> dict[str, Any]:
        """This plugin's effective config for this session (README §9). A fresh copy."""
        return self._host.session_config(self.plugin_id, self.session_id)

    @property
    def state(self) -> dict[str, Any]:
        """In-memory state shared by this plugin's services, hooks, tools and actions
        for this session. Lost on backend restart or plugin unload."""
        return self._host.session_state(self.plugin_id, self.session_id)

    @property
    def session_data_dir(self) -> Path:
        path = self.data_dir / "sessions" / self.session_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def session(self) -> SessionInfo:
        """Fresh snapshot of this session."""
        return await self.sessions.get(self.session_id)


@dataclass(frozen=True)
class ToolCallInfo:
    tool_call_id: str | None
    message_id: str | None
    name: str


class ToolContext(SessionContext):
    def __init__(
        self,
        host: "PluginHost",
        plugin_id: str,
        session_id: str,
        call: ToolCallInfo,
        *,
        working_dir: str | None = None,
    ) -> None:
        super().__init__(host, plugin_id, session_id, working_dir=working_dir)
        self.call = call
