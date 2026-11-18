"""Plugin services, logs, UI state and live updates (README §5.4, §6).

``ServiceManager`` owns every running service instance. An instance is keyed
by (plugin_id, service name, session_id or None). Each instance has a
supervisor task that starts the service coroutine, records a
``PluginServiceRun`` per start, and restarts it per the service's policy:

- backoff 1, 2, 4, ... seconds, capped at 60;
- more than 5 restarts within 10 minutes -> ``errored``, no more retries;
- the restart counter resets after the service ran 10 minutes without exiting.

Stopping cancels the service task and waits up to 5 seconds for it.

The manager is also the hub for:
- plugin log lines (``PluginLogger`` calls ``log``): persisted, kept in a
  per-plugin ring buffer, and broadcast;
- UI state pushed through ``ctx.ui`` (README §5.4), kept in memory;
- live subscribers (``WS /plugins/stream``), optionally filtered by session.

It holds no references to plugin code beyond the bound service methods, so
the host can drop a plugin after ``stop_plugin``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from pydantic import BaseModel

from app.plugins.sdk import ServiceSpec

if TYPE_CHECKING:
    from app.db.interface import IAgentDatabase

logger = logging.getLogger("augentia.plugins.services")

STOP_GRACE = 5.0
BACKOFF_START = 1.0
BACKOFF_MAX = 60.0
RESTART_WINDOW = 600.0
RESTART_LIMIT = 5
STABLE_AFTER = 600.0
LOG_BUFFER = 500
LOG_KEEP = 20000
# Prune the table once per this many inserted lines per plugin.
PRUNE_EVERY = 500
SUBSCRIBER_QUEUE = 1000

ServiceKey = tuple[str, str, str | None]  # plugin_id, service, session_id


class ServiceState(BaseModel):
    plugin_id: str
    service: str
    session_id: str | None
    scope: str
    restart: str
    # stopped / starting / running / stopping / exited / errored / backoff
    status: str
    run_id: str | None = None
    restart_count: int = 0
    error: str | None = None
    # When status == "backoff": when the next attempt starts (UTC).
    next_restart_at: datetime | None = None
    started_at: datetime | None = None


class LogLine(BaseModel):
    id: int | None = None
    plugin_id: str
    run_id: str | None = None
    session_id: str | None = None
    ts: datetime
    level: str
    line: str


class UIState(BaseModel):
    plugin_id: str
    session_id: str | None = None
    status: str = "idle"
    message: str | None = None
    link: str | None = None
    qr: str | None = None
    actions: list[dict] = []
    # Optional sub-cards (e.g. one per account), each with the same fields
    # except sections; see validate_ui.
    sections: list[dict] = []
    updated_at: datetime


UI_STATUSES = ("idle", "waiting", "ok", "error")
UI_MAX_SECTIONS = 50


def _validate_action(item: Any) -> dict:
    """One button: id, label, optional style, data (passed to the action) and input.

    ``input`` adds a text box next to the button; its text is sent as
    ``data[input.name]``. ``input`` is ``{"name", "placeholder"?, "value"?}``.
    """
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("label"), str):
        raise ValueError("each ui action needs string id and label")
    unknown = set(item) - {"id", "label", "style", "data", "input"}
    if unknown:
        raise ValueError(f"unknown ui action keys: {sorted(unknown)}")
    clean: dict[str, Any] = {"id": item["id"], "label": item["label"]}
    style = item.get("style")
    if style is not None:
        if style not in ("default", "primary", "danger"):
            raise ValueError("ui action style must be default, primary or danger")
        clean["style"] = style
    data = item.get("data")
    if data is not None:
        if not isinstance(data, dict):
            raise ValueError("ui action data must be an object")
        try:
            json.dumps(data)
        except (TypeError, ValueError):
            raise ValueError("ui action data must be JSON-serializable") from None
        clean["data"] = data
    field_ = item.get("input")
    if field_ is not None:
        if not isinstance(field_, dict) or not isinstance(field_.get("name"), str) or not field_["name"]:
            raise ValueError("ui action input needs a string name")
        unknown = set(field_) - {"name", "placeholder", "value"}
        if unknown:
            raise ValueError(f"unknown ui action input keys: {sorted(unknown)}")
        for key in ("placeholder", "value"):
            if field_.get(key) is not None and not isinstance(field_[key], str):
                raise ValueError(f"ui action input {key} must be a string")
        clean["input"] = {k: field_[k] for k in ("name", "placeholder", "value") if field_.get(k) is not None}
    return clean


def _validate_block(data: dict, *, allowed: set[str]) -> dict:
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown ui keys: {sorted(unknown)}")
    status = data.get("status", "idle")
    if status not in UI_STATUSES:
        raise ValueError(f"ui status must be one of {UI_STATUSES}")
    for key in ("message", "link", "qr", "title"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ValueError(f"ui {key} must be a string")
    actions = data.get("actions") or []
    if not isinstance(actions, list):
        raise ValueError("ui actions must be a list")
    clean = {"status": status, "message": data.get("message"), "link": data.get("link"),
             "qr": data.get("qr"), "actions": [_validate_action(a) for a in actions]}
    if "title" in allowed:
        clean["title"] = data.get("title")
    return clean


def validate_ui(data: Any) -> dict:
    """Checked copy of a ctx.ui.update payload; ValueError names the problem."""
    if not isinstance(data, dict):
        raise ValueError("ui state must be an object")
    clean = _validate_block(data, allowed={"status", "message", "link", "qr", "actions", "sections"})
    sections = data.get("sections") or []
    if not isinstance(sections, list):
        raise ValueError("ui sections must be a list")
    if len(sections) > UI_MAX_SECTIONS:
        raise ValueError(f"at most {UI_MAX_SECTIONS} ui sections")
    clean_sections = []
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError("each ui section must be an object")
        clean_sections.append(_validate_block(
            section, allowed={"title", "status", "message", "link", "qr", "actions"}))
    clean["sections"] = clean_sections
    return clean


@dataclass
class _Instance:
    spec: ServiceSpec
    key: ServiceKey
    target: Callable[[], Coroutine[Any, Any, Any]]
    state: ServiceState
    task: asyncio.Task | None = None
    # Timestamps (monotonic) of automatic restarts within RESTART_WINDOW.
    restarts: deque = field(default_factory=deque)
    stop_requested: bool = False


class _Subscriber:
    def __init__(self, session_id: str | None, plugin_id: str | None) -> None:
        self.session_id = session_id
        self.plugin_id = plugin_id
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)

    def wants(self, plugin_id: str, session_id: str | None) -> bool:
        if self.plugin_id is not None and plugin_id != self.plugin_id:
            return False
        # A session-filtered stream gets that session's items plus global ones.
        return self.session_id is None or session_id is None or session_id == self.session_id

    def put(self, frame: dict) -> None:
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()  # drop the oldest
                self.queue.put_nowait(frame)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass


class ServiceManager:
    def __init__(self) -> None:
        self._db: "IAgentDatabase | None" = None
        self._instances: dict[ServiceKey, _Instance] = {}
        self._logs: dict[str, deque[LogLine]] = {}
        self._log_counts: dict[str, int] = {}
        self._ui: dict[tuple[str, str | None], UIState] = {}
        self._subscribers: set[_Subscriber] = set()
        self._log_tasks: set[asyncio.Task] = set()

    def bind_db(self, db: "IAgentDatabase | None") -> None:
        self._db = db

    # ---- queries ----

    def states(self, plugin_id: str | None = None, session_id: str | None = None) -> list[ServiceState]:
        result = [i.state.model_copy() for i in self._instances.values()
                  if (plugin_id is None or i.key[0] == plugin_id)
                  and (session_id is None or i.key[2] == session_id)]
        return sorted(result, key=lambda s: (s.plugin_id, s.service, s.session_id or ""))

    def get(self, plugin_id: str, service: str, session_id: str | None) -> ServiceState | None:
        inst = self._instances.get((plugin_id, service, session_id))
        return inst.state.model_copy() if inst else None

    def is_active(self, plugin_id: str, service: str, session_id: str | None) -> bool:
        inst = self._instances.get((plugin_id, service, session_id))
        return inst is not None and inst.task is not None and not inst.task.done()

    def recent_logs(self, plugin_id: str, session_id: str | None = None, limit: int = 200) -> list[LogLine]:
        lines = [line for line in self._logs.get(plugin_id, ())
                 if session_id is None or line.session_id in (None, session_id)]
        return lines[-limit:]

    def ui_state(self, plugin_id: str, session_id: str | None) -> UIState | None:
        state = self._ui.get((plugin_id, session_id))
        return state.model_copy() if state else None

    def ui_states(self, session_id: str | None = None) -> list[UIState]:
        return [s.model_copy() for (pid, sid), s in self._ui.items() if session_id is None or sid == session_id]

    # ---- lifecycle ----

    async def start(self, spec: ServiceSpec, plugin_id: str, session_id: str | None,
                    target: Callable[[], Coroutine[Any, Any, Any]]) -> ServiceState:
        """Start (or restart, if it is not running) one service instance. Idempotent while active."""
        key = (plugin_id, spec.name, session_id)
        inst = self._instances.get(key)
        if inst is not None and inst.task is not None and not inst.task.done():
            return inst.state.model_copy()
        state = ServiceState(plugin_id=plugin_id, service=spec.name, session_id=session_id,
                             scope=spec.scope, restart=spec.restart, status="starting")
        inst = _Instance(spec=spec, key=key, target=target, state=state)
        self._instances[key] = inst
        inst.task = asyncio.create_task(self._supervise(inst), name=f"service:{plugin_id}:{spec.name}:{session_id}")
        await asyncio.sleep(0)  # let the supervisor record the run
        return inst.state.model_copy()

    async def stop(self, plugin_id: str, service: str, session_id: str | None) -> ServiceState | None:
        inst = self._instances.get((plugin_id, service, session_id))
        if inst is None:
            return None
        await self._stop_instance(inst)
        return inst.state.model_copy()

    async def stop_where(self, *, plugin_id: str | None = None, session_id: str | None = None,
                         scope: str | None = None) -> None:
        """Stop every matching instance in parallel."""
        matching = [i for i in self._instances.values()
                    if (plugin_id is None or i.key[0] == plugin_id)
                    and (session_id is None or i.key[2] == session_id)
                    and (scope is None or i.spec.scope == scope)]
        await asyncio.gather(*(self._stop_instance(i) for i in matching))

    def forget_plugin(self, plugin_id: str) -> None:
        """Drop stopped instances and UI state of an unloaded plugin (logs stay)."""
        for key in [k for k, i in self._instances.items()
                    if k[0] == plugin_id and (i.task is None or i.task.done())]:
            del self._instances[key]
        for key in [k for k in self._ui if k[0] == plugin_id]:
            del self._ui[key]

    async def shutdown(self) -> None:
        await self.stop_where()
        if self._log_tasks:
            await asyncio.wait(list(self._log_tasks), timeout=2.0)

    async def _stop_instance(self, inst: _Instance) -> None:
        task = inst.task
        inst.stop_requested = True
        if task is None or task.done():
            if inst.state.status == "backoff":
                inst.state.status = "stopped"
                inst.state.next_restart_at = None
                self._broadcast_state(inst)
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), STOP_GRACE + 1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            logger.exception("service supervisor crashed %s", inst.key)

    async def _supervise(self, inst: _Instance) -> None:
        plugin_id, name, session_id = inst.key
        backoff = BACKOFF_START
        try:
            while True:
                run_id = await self._record_start(inst)
                started = time.monotonic()
                inst.state.status = "running"
                inst.state.error = None
                inst.state.next_restart_at = None
                inst.state.started_at = datetime.now(timezone.utc)
                self._broadcast_state(inst)
                await self._update_run(run_id, status="running", running_at=datetime.now(timezone.utc))
                service_task = asyncio.create_task(inst.target(), name=f"service-body:{plugin_id}:{name}")
                error: str | None = None
                try:
                    await asyncio.shield(service_task)
                    outcome = "exited"
                except asyncio.CancelledError:
                    # Stop requested (or backend shutdown): cancel the body and
                    # give it STOP_GRACE seconds for its finally blocks.
                    inst.state.status = "stopping"
                    self._broadcast_state(inst)
                    await self._update_run(run_id, status="stopping")
                    service_task.cancel()
                    done, _ = await asyncio.wait({service_task}, timeout=STOP_GRACE)
                    if not done:
                        logger.warning("service %s did not stop within %ss", inst.key, STOP_GRACE)
                        await self.log(plugin_id, f"service '{name}' did not stop within {STOP_GRACE:g}s",
                                       level="warning", run_id=run_id, session_id=session_id)
                    inst.state.status = "stopped"
                    self._broadcast_state(inst)
                    await self._update_run(run_id, status="exited", exited_at=datetime.now(timezone.utc))
                    raise
                except Exception as e:
                    outcome = "errored"
                    error = f"{type(e).__name__}: {e}"
                    logger.exception("service %s crashed", inst.key)
                    await self.log(plugin_id, f"service '{name}' crashed: {error}", level="error",
                                   run_id=run_id, session_id=session_id)
                await self._update_run(run_id, status=outcome, exited_at=datetime.now(timezone.utc),
                                       error=error)
                inst.state.status = outcome
                inst.state.error = error
                if time.monotonic() - started >= STABLE_AFTER:
                    inst.restarts.clear()
                    backoff = BACKOFF_START
                if not self._should_restart(inst, outcome):
                    self._broadcast_state(inst)
                    return
                now = time.monotonic()
                while inst.restarts and now - inst.restarts[0] > RESTART_WINDOW:
                    inst.restarts.popleft()
                if len(inst.restarts) >= RESTART_LIMIT:
                    inst.state.status = "errored"
                    inst.state.error = (f"{error or 'exited'}; gave up after {RESTART_LIMIT} restarts "
                                        f"in {RESTART_WINDOW / 60:g} minutes")
                    self._broadcast_state(inst)
                    await self.log(plugin_id, f"service '{name}' {inst.state.error}", level="error",
                                   run_id=run_id, session_id=session_id)
                    return
                inst.restarts.append(now)
                inst.state.restart_count += 1
                inst.state.status = "backoff"
                inst.state.next_restart_at = datetime.fromtimestamp(time.time() + backoff, timezone.utc)
                self._broadcast_state(inst)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                inst.state.status = "starting"
        except asyncio.CancelledError:
            if inst.state.status in ("backoff", "starting"):
                inst.state.status = "stopped"
                inst.state.next_restart_at = None
                self._broadcast_state(inst)

    def _should_restart(self, inst: _Instance, outcome: str) -> bool:
        if inst.stop_requested:
            return False
        if inst.spec.restart == "always":
            return True
        return inst.spec.restart == "on-failure" and outcome == "errored"

    async def _record_start(self, inst: _Instance) -> str | None:
        plugin_id, name, session_id = inst.key
        if self._db is None:
            inst.state.run_id = None
            return None
        try:
            run = await self._db.create_service_run(plugin_id, name, session_id=session_id,
                                                    restart_count=inst.state.restart_count)
        except Exception:
            logger.exception("could not record service run %s", inst.key)
            inst.state.run_id = None
            return None
        inst.state.run_id = run.id
        return run.id

    async def _update_run(self, run_id: str | None, **fields: Any) -> None:
        if run_id is None or self._db is None:
            return
        try:
            await self._db.update_service_run(run_id, **fields)
        except Exception:
            logger.exception("could not update service run %s", run_id)

    # ---- logs ----

    async def log(self, plugin_id: str, line: str, *, level: str = "info",
                  run_id: str | None = None, session_id: str | None = None) -> None:
        """Persist, buffer and broadcast one plugin log line. Never raises."""
        entry = LogLine(plugin_id=plugin_id, run_id=run_id, session_id=session_id,
                        ts=datetime.now(timezone.utc), level=level, line=line)
        if self._db is not None:
            try:
                saved = await self._db.add_service_log(plugin_id, line, level=level, run_id=run_id,
                                                       session_id=session_id)
                entry.id, entry.ts = saved.id, saved.ts
                count = self._log_counts.get(plugin_id, 0) + 1
                self._log_counts[plugin_id] = count
                if count % PRUNE_EVERY == 0:
                    await self._db.prune_service_logs(plugin_id, LOG_KEEP)
            except Exception:
                logger.exception("could not persist plugin log for %s", plugin_id)
        self._logs.setdefault(plugin_id, deque(maxlen=LOG_BUFFER)).append(entry)
        self._broadcast(plugin_id, session_id, {"type": "log", "data": entry.model_dump(mode="json")})

    def log_nowait(self, plugin_id: str, line: str, **kwargs: Any) -> None:
        """Fire-and-forget log() for synchronous callers (PluginLogger)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.log(plugin_id, line, **kwargs))
        self._log_tasks.add(task)
        task.add_done_callback(self._log_tasks.discard)

    # ---- UI state ----

    def clear_ui(self, plugin_id: str, session_id: str | None) -> None:
        if self._ui.pop((plugin_id, session_id), None) is not None:
            self._broadcast(plugin_id, session_id, {"type": "ui", "data": UIState(
                plugin_id=plugin_id, session_id=session_id, updated_at=datetime.now(timezone.utc),
            ).model_dump(mode="json")})

    def set_ui(self, plugin_id: str, session_id: str | None, data: Any) -> UIState:
        clean = validate_ui(data)
        state = UIState(plugin_id=plugin_id, session_id=session_id, updated_at=datetime.now(timezone.utc), **clean)
        self._ui[(plugin_id, session_id)] = state
        self._broadcast(plugin_id, session_id, {"type": "ui", "data": state.model_dump(mode="json")})
        return state

    # ---- live updates ----

    def subscribe(self, *, session_id: str | None = None, plugin_id: str | None = None) -> _Subscriber:
        sub = _Subscriber(session_id, plugin_id)
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        self._subscribers.discard(sub)

    def broadcast_plugin(self, plugin_id: str, frame: dict) -> None:
        """A plugin-level change (state, enabled) that every subscriber of it should see."""
        self._broadcast(plugin_id, None, frame)

    def _broadcast_state(self, inst: _Instance) -> None:
        self._broadcast(inst.key[0], inst.key[2], {"type": "service", "data": inst.state.model_dump(mode="json")})

    def _broadcast(self, plugin_id: str, session_id: str | None, frame: dict) -> None:
        for sub in list(self._subscribers):
            if sub.wants(plugin_id, session_id):
                sub.put(frame)
