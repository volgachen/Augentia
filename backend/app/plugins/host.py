"""PluginHost: the set of discovered plugins and their state (README §11.1).

One host per backend process (``get_plugin_host()``). It discovers plugin
directories, imports enabled plugins, runs their on_load / on_unload, and owns
the global enable flag and global config. Later steps hang tools (P1), hooks
(P3) and services (P4) off the loaded plugins kept here.

States:
- ``loaded``   enabled, imported, on_load succeeded.
- ``disabled`` turned off in plugin_settings.json; code is not imported.
- ``error``    invalid manifest, missing dependency, import / on_load failure,
               or an id already taken by an earlier plugin directory.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.tools.registry import register_plugin_tools, unregister_plugin_tools
from app.plugins import loader, session_settings, settings
from app.plugins.context import PluginContext, SessionContext
from app.plugins.events import PluginEventBus, get_plugin_event_bus
from app.plugins.manifest import ManifestError, PluginManifest, read_manifest
from app.plugins.sdk import Capabilities, Plugin, ServiceSpec, tool_full_name
from app.plugins.services import ServiceManager, ServiceState, UIState
from app.plugins.services_context import current_run_id, current_service

if TYPE_CHECKING:
    from app.adapters.registry import AdapterRegistry
    from app.db.interface import IAgentDatabase

logger = logging.getLogger("augentia.plugins.host")

PluginState = Literal["loaded", "disabled", "error"]

ON_LOAD_TIMEOUT = 30.0
ON_UNLOAD_TIMEOUT = 10.0
ON_CONFIG_CHANGED_TIMEOUT = 10.0
ACTION_TIMEOUT = 30.0


class PluginNotFound(KeyError):
    pass


class PluginConfigError(ValueError):
    """The submitted config does not match the plugin's schema."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class PluginUnavailable(RuntimeError):
    """The operation needs a valid manifest (the plugin is in an error state)."""


class PluginNotLoaded(RuntimeError):
    """The operation needs the plugin's code (state is not "loaded")."""


class ActionError(RuntimeError):
    """An action handler raised; the message is safe to show to the user."""


@dataclass
class PluginRecord:
    id: str
    path: Path
    manifest: PluginManifest | None = None
    state: PluginState = "disabled"
    error: str | None = None
    missing_dependencies: list[str] = field(default_factory=list)
    instance: Plugin | None = None
    capabilities: Capabilities | None = None
    ctx: PluginContext | None = None


# ---- API views ----


class ToolInfo(BaseModel):
    name: str
    full_name: str
    description: str
    requires_approval: bool
    parameters: dict


class HookInfo(BaseModel):
    event: str
    priority: int
    timeout: float
    on_error: str


class ServiceInfo(BaseModel):
    name: str
    scope: str
    restart: str
    autostart: bool


class CapabilitiesInfo(BaseModel):
    tools: list[ToolInfo]
    hooks: list[HookInfo]
    services: list[ServiceInfo]
    actions: list[str]
    http_routes: bool


class SessionPluginInfo(BaseModel):
    """One plugin as seen from a session (GET /sessions/{id}/plugins)."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    description: str = ""
    state: PluginState
    error: str | None = None
    enabled: bool
    default_enabled: bool
    # Effective config with schema defaults, secrets masked.
    config: dict
    schema_: dict | None = Field(default=None, alias="schema", serialization_alias="schema")
    # Stored entry no longer matches the schema (e.g. after a plugin upgrade).
    config_errors: list[str] = []
    tools: list[str] = []
    hooks: list[str] = []
    session_services: list[str] = []
    actions: list[str] = []
    # Live state of this session's service instances and the plugin's UI card.
    services: list[ServiceState] = []
    ui: UIState | None = None


class PluginInfo(BaseModel):
    id: str
    path: str
    state: PluginState
    enabled: bool
    error: str | None = None
    missing_dependencies: list[str] = []
    # From the manifest; None when the manifest itself is invalid.
    name: str | None = None
    version: str | None = None
    description: str = ""
    author: str = ""
    api_version: int | None = None
    python_dependencies: list[str] = []
    global_config_schema: dict | None = None
    session_config_schema: dict | None = None
    session_default_enabled: bool = False
    public_routes: list[str] = []
    # Only known once the code is imported (state == "loaded").
    capabilities: CapabilitiesInfo | None = None
    # Running / recent service instances (global and per session) and the global UI card.
    services: list[ServiceState] = []
    ui: UIState | None = None


def _capabilities_info(plugin_id: str, caps: Capabilities) -> CapabilitiesInfo:
    return CapabilitiesInfo(
        tools=[
            ToolInfo(
                name=t.name,
                full_name=tool_full_name(plugin_id, t.name),
                description=t.description,
                requires_approval=t.requires_approval,
                parameters=t.parameters,
            )
            for t in caps.tools
        ],
        hooks=[HookInfo(event=h.event, priority=h.priority, timeout=h.timeout, on_error=h.on_error)
               for h in caps.hooks],
        services=[ServiceInfo(name=s.name, scope=s.scope, restart=s.restart, autostart=s.autostart)
                  for s in caps.services],
        actions=[a.id for a in caps.actions],
        http_routes=caps.has_routes,
    )


class PluginHost:
    def __init__(self) -> None:
        self._records: dict[str, PluginRecord] = {}
        # Later directories whose id an earlier one already took (shown as errors).
        self._shadowed: list[PluginRecord] = []
        self._lock = asyncio.Lock()
        # (plugin_id, session_id) -> SessionContext.state; dropped on unload.
        self._session_state: dict[tuple[str, str], dict[str, Any]] = {}
        # (plugin_id, session_id) pairs on_session_enabled has been called for.
        self._active_sessions: set[tuple[str, str]] = set()
        self.services = ServiceManager()
        self.event_bus: PluginEventBus = get_plugin_event_bus()
        # Bound by main.py at startup; None in tests that only load plugins.
        self.db: "IAgentDatabase | None" = None
        self.session_registry: "AdapterRegistry | None" = None

    def bind(self, db: "IAgentDatabase | None", session_registry: "AdapterRegistry | None") -> None:
        self.db = db
        self.session_registry = session_registry
        self.services.bind_db(db)

    # ---- queries ----

    def get(self, plugin_id: str) -> PluginRecord:
        record = self._records.get(plugin_id)
        if record is None:
            raise PluginNotFound(f"Plugin '{plugin_id}' not found")
        return record

    def loaded(self) -> list[PluginRecord]:
        return [r for r in self._records.values() if r.state == "loaded"]

    def info(self, record: PluginRecord) -> PluginInfo:
        manifest = record.manifest
        data: dict[str, Any] = {
            "id": record.id,
            "path": str(record.path),
            "state": record.state,
            "enabled": settings.is_enabled(record.id),
            "error": record.error,
            "missing_dependencies": list(record.missing_dependencies),
        }
        if manifest is not None:
            data.update(
                name=manifest.name,
                version=manifest.version,
                description=manifest.description,
                author=manifest.author,
                api_version=manifest.api_version,
                python_dependencies=list(manifest.python_dependencies),
                global_config_schema=manifest.config.global_,
                session_config_schema=manifest.config.session,
                session_default_enabled=manifest.session.default_enabled,
                public_routes=list(manifest.http.public_routes),
            )
        if record.capabilities is not None:
            data["capabilities"] = _capabilities_info(record.id, record.capabilities)
        data["services"] = self.services.states(record.id)
        data["ui"] = self.services.ui_state(record.id, None)
        return PluginInfo(**data)

    def list_info(self) -> list[PluginInfo]:
        records = sorted(self._records.values(), key=lambda r: r.id)
        return [self.info(r) for r in records] + [self.info(r) for r in self._shadowed]

    def tool_target(self, plugin_id: str, tool_name: str):
        """The bound method behind a plugin tool, or a reason string if unavailable."""
        record = self._records.get(plugin_id)
        if record is None:
            return f"plugin '{plugin_id}' is not installed"
        if record.state != "loaded" or record.instance is None or record.capabilities is None:
            return f"plugin '{plugin_id}' is {record.state}"
        for spec in record.capabilities.tools:
            if spec.name == tool_name:
                return getattr(record.instance, spec.attr)
        return f"plugin '{plugin_id}' no longer provides tool '{tool_name}'"

    def session_state(self, plugin_id: str, session_id: str) -> dict[str, Any]:
        return self._session_state.setdefault((plugin_id, session_id), {})

    # ---- services ----

    def service_spec(self, plugin_id: str, name: str) -> ServiceSpec | None:
        record = self._records.get(plugin_id)
        if record is None or record.capabilities is None:
            return None
        return next((s for s in record.capabilities.services if s.name == name), None)

    def is_service_task(self, plugin_id: str, name: str, session_id: str | None) -> bool:
        return current_service.get() == (plugin_id, name, session_id)

    async def start_service(self, plugin_id: str, name: str, session_id: str | None) -> ServiceState:
        """Start one service instance (autostart, manual start, or from an action).

        A session service needs a session_id. Starting one by hand works even
        if the plugin is not enabled for that session; sync_session_services
        does not stop it until the plugin's session switch changes.
        """
        record = self._records.get(plugin_id)
        if record is None:
            raise PluginNotFound(f"Plugin '{plugin_id}' not found")
        if record.state != "loaded" or record.instance is None or record.capabilities is None:
            raise PluginNotLoaded(f"Plugin '{plugin_id}' is {record.state}")
        spec = self.service_spec(plugin_id, name)
        if spec is None:
            raise KeyError(f"Plugin '{plugin_id}' has no service '{name}'")
        if spec.scope == "session" and not session_id:
            raise ValueError(f"service '{name}' is per session; session_id is required")
        if spec.scope == "global":
            session_id = None
        method = getattr(record.instance, spec.attr)
        key = (plugin_id, name, session_id)
        services = self.services

        def make_ctx() -> PluginContext:
            if session_id is None:
                return PluginContext(self, plugin_id)
            return SessionContext(self, plugin_id, session_id, working_dir=self._session_working_dir(session_id))

        async def target() -> None:
            # Runs in its own task: these context variables tag its logs and
            # let ServiceHandle.stop() detect a service stopping itself.
            current_service.set(key)
            state = services.get(plugin_id, name, session_id)
            current_run_id.set(state.run_id if state else None)
            await method(make_ctx())

        return await services.start(spec, plugin_id, session_id, target)

    def _session_working_dir(self, session_id: str) -> str | None:
        registry = self.session_registry
        if registry is None:
            return None
        try:
            adapter = registry.get(session_id)._adapter  # type: ignore[attr-defined]
        except (KeyError, AttributeError):
            return None
        return getattr(adapter, "_working_dir", None)

    async def sync_session_services(self, session_id: str) -> None:
        """Bring one session's plugins in line with its plugin switches.

        For each loaded plugin: when it becomes enabled for the session,
        on_session_enabled runs and its autostart session services start;
        when it stops being enabled, its session services stop,
        on_session_disabled runs and its UI card for the session is cleared.
        Called after the session config is saved, when a session is created,
        when a plugin loads, and at startup.
        """
        for record in self.loaded():
            caps = record.capabilities
            if caps is None or record.instance is None:
                continue
            key = (record.id, session_id)
            enabled = self.session_enabled(record.id, session_id)
            specs = [s for s in caps.services if s.scope == "session"]
            if enabled:
                if key not in self._active_sessions:
                    self._active_sessions.add(key)
                    await self._session_callback(record, "on_session_enabled", session_id)
                for spec in specs:
                    if spec.autostart and not self.services.is_active(record.id, spec.name, session_id):
                        try:
                            await self.start_service(record.id, spec.name, session_id)
                        except Exception:
                            logger.exception("could not start %s/%s for session %s",
                                             record.id, spec.name, session_id)
            elif key in self._active_sessions or any(
                    self.services.get(record.id, s.name, session_id) is not None for s in specs):
                await self._deactivate_session(record, session_id)

    async def _deactivate_session(self, record: PluginRecord, session_id: str) -> None:
        if record.capabilities is not None:
            for spec in record.capabilities.services:
                if spec.scope == "session" and self.services.get(record.id, spec.name, session_id) is not None:
                    await self.services.stop(record.id, spec.name, session_id)
        if (record.id, session_id) in self._active_sessions:
            self._active_sessions.discard((record.id, session_id))
            await self._session_callback(record, "on_session_disabled", session_id)
        self.services.clear_ui(record.id, session_id)

    async def _session_callback(self, record: PluginRecord, name: str, session_id: str) -> None:
        method = getattr(record.instance, name, None)
        if method is None or getattr(type(record.instance), name, None) is getattr(Plugin, name):
            return
        ctx = SessionContext(self, record.id, session_id, working_dir=self._session_working_dir(session_id))
        try:
            await asyncio.wait_for(method(ctx), ON_CONFIG_CHANGED_TIMEOUT)
        except Exception:
            logger.exception("plugin %s %s failed for session %s", record.id, name, session_id)

    async def start_all_session_services(self) -> None:
        """Start session services for every live session whose config enables them."""
        from app.core.session_config import peek_session_config

        sessions_dir = settings.augentia_home() / "sessions"
        if not sessions_dir.is_dir():
            return
        live: set[str] | None = None
        if self.db is not None:
            try:
                live = {s.id for s in await self.db.list_sessions()}
            except Exception:
                logger.exception("could not list sessions; using every session config")
        for child in sorted(sessions_dir.iterdir()):
            if not child.is_dir() or (live is not None and child.name not in live):
                continue
            await self.sync_session_services(child.name)

    async def session_deleted(self, session_id: str) -> None:
        for record in self.loaded():
            await self._deactivate_session(record, session_id)
        await self.services.stop_where(session_id=session_id)
        for key in [k for k in self._session_state if k[1] == session_id]:
            del self._session_state[key]

    async def _start_global_services(self, record: PluginRecord) -> None:
        if record.capabilities is None:
            return
        for spec in record.capabilities.services:
            if spec.scope == "global" and spec.autostart:
                try:
                    await self.start_service(record.id, spec.name, None)
                except Exception:
                    logger.exception("could not start %s/%s", record.id, spec.name)

    # ---- actions ----

    async def run_action(self, plugin_id: str, action_id: str, session_id: str | None, data: dict) -> Any:
        """Call a plugin action (UI button or API). ActionError carries the plugin's message."""
        record = self._records.get(plugin_id)
        if record is None:
            raise PluginNotFound(f"Plugin '{plugin_id}' not found")
        if record.state != "loaded" or record.instance is None or record.capabilities is None:
            raise PluginNotLoaded(f"Plugin '{plugin_id}' is {record.state}")
        spec = next((a for a in record.capabilities.actions if a.id == action_id), None)
        if spec is None:
            raise KeyError(f"Plugin '{plugin_id}' has no action '{action_id}'")
        ctx: PluginContext
        if session_id:
            ctx = SessionContext(self, plugin_id, session_id, working_dir=self._session_working_dir(session_id))
        else:
            ctx = PluginContext(self, plugin_id)
        method = getattr(record.instance, spec.attr)
        try:
            return await asyncio.wait_for(method(ctx, dict(data)), ACTION_TIMEOUT)
        except asyncio.TimeoutError:
            raise ActionError(f"action '{action_id}' did not finish within {ACTION_TIMEOUT:g}s") from None
        except Exception as e:
            logger.exception("action %s of plugin %s failed", action_id, plugin_id)
            raise ActionError(f"{type(e).__name__}: {e}") from e

    # ---- sessions API backing ----

    async def send_session_message(self, plugin_id: str, session_id: str, content: str, *,
                                   images: list[dict] | None, label: str | None) -> None:
        """ctx.sessions.send: queue a user turn in a session as plugin:<id>."""
        from app.core.session_runtime import get_or_rehydrate

        if self.db is None or self.session_registry is None:
            raise RuntimeError("sessions API is not available (host not bound)")
        session = await self.db.get_session(session_id)  # KeyError if missing
        runner = await get_or_rehydrate(session, self.db, self.session_registry)
        if runner is None:
            raise RuntimeError(f"session '{session_id}' cannot run turns")
        await runner.submit(content, from_session_id=f"plugin:{plugin_id}", images=images,
                            metadata={"plugin_label": label} if label else None)

    # ---- config ----

    def manifest_of(self, plugin_id: str) -> PluginManifest | None:
        record = self._records.get(plugin_id)
        return record.manifest if record is not None else None

    def session_entry(self, plugin_id: str, session_id: str) -> Any:
        """The stored plugins.<id> entry of a session's config.json (None if absent)."""
        from app.core.session_config import peek_session_config

        return session_settings.plugins_section(peek_session_config(session_id)).get(plugin_id)

    def session_config(self, plugin_id: str, session_id: str) -> dict[str, Any]:
        """Effective per-session config of a plugin (README §9). A fresh copy."""
        _, config = session_settings.effective(
            self.manifest_of(plugin_id), self.session_entry(plugin_id, session_id))
        return config

    def session_enabled(self, plugin_id: str, session_id: str) -> bool:
        """Whether the plugin's hooks / session services are on for this session."""
        record = self._records.get(plugin_id)
        if record is None or record.state != "loaded":
            return False
        enabled, _ = session_settings.effective(record.manifest, self.session_entry(plugin_id, session_id))
        return enabled

    def session_plugins(self, config: dict[str, Any] | None,
                        session_id: str | None = None) -> list["SessionPluginInfo"]:
        """Plugins that have something to configure per session, for one session config.

        Listed: every loaded plugin with a session schema, hooks or session
        services, plus any plugin the session has an entry for (so a disabled
        or broken plugin's settings stay visible).
        """
        section = session_settings.plugins_section(config)
        views: list[SessionPluginInfo] = []
        for record in sorted(self._records.values(), key=lambda r: r.id):
            manifest = record.manifest
            caps = record.capabilities
            relevant = record.id in section or (
                record.state == "loaded" and manifest is not None and (
                    manifest.config.session is not None
                    or (caps is not None and (caps.hooks or any(s.scope == "session" for s in caps.services)))
                )
            )
            if not relevant:
                continue
            entry = section.get(record.id)
            enabled, effective_config = session_settings.effective(manifest, entry)
            errors = session_settings.entry_errors(manifest, entry) if manifest and entry is not None else []
            schema = manifest.config.session if manifest else None
            views.append(SessionPluginInfo(
                id=record.id,
                name=manifest.name if manifest else record.id,
                description=manifest.description if manifest else "",
                state=record.state,
                error=record.error,
                enabled=enabled,
                default_enabled=manifest.session.default_enabled if manifest else False,
                config=settings.mask_secrets(schema, effective_config),
                schema=schema,
                config_errors=errors,
                tools=[tool_full_name(record.id, t.name) for t in caps.tools] if caps else [],
                hooks=sorted({h.event for h in caps.hooks}) if caps else [],
                session_services=[sv.name for sv in caps.services if sv.scope == "session"] if caps else [],
                actions=[a.id for a in caps.actions] if caps else [],
                services=self.services.states(record.id, session_id) if session_id else [],
                ui=self.services.ui_state(record.id, session_id) if session_id else None,
            ))
        return views

    def effective_global_config(self, plugin_id: str) -> dict[str, Any]:
        record = self.get(plugin_id)
        schema = record.manifest.config.global_ if record.manifest else None
        return settings.apply_defaults(schema, settings.stored_global_config(plugin_id))

    def masked_global_config(self, plugin_id: str) -> dict[str, Any]:
        schema = self._require_manifest(plugin_id).config.global_
        return settings.mask_secrets(schema, self.effective_global_config(plugin_id))

    async def set_global_config(self, plugin_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """Validate and save the global config; returns the masked effective config.

        Masked secret values sent back unchanged keep the stored secret. The
        stored value omits nothing the client sent, but defaults are applied
        on read, so later default changes in the manifest still take effect
        for keys the user never set.
        """
        async with self._lock:
            schema = self._require_manifest(plugin_id).config.global_
            record = self.get(plugin_id)
            stored = settings.stored_global_config(plugin_id)
            new = settings.restore_secrets(schema, config, stored)
            errors = settings.validation_errors(schema, settings.apply_defaults(schema, new))
            if errors:
                raise PluginConfigError(errors)
            old_effective = self.effective_global_config(plugin_id)
            settings.write_global_config(plugin_id, new)
            new_effective = self.effective_global_config(plugin_id)
        if record.state == "loaded" and record.instance is not None and record.ctx is not None:
            if type(record.instance).on_config_changed is Plugin.on_config_changed:
                # Default reaction: restart global services so they read the new config.
                await self.services.stop_where(plugin_id=plugin_id, scope="global")
                await self._start_global_services(record)
            else:
                try:
                    await asyncio.wait_for(
                        record.instance.on_config_changed(record.ctx, old_effective, new_effective),
                        ON_CONFIG_CHANGED_TIMEOUT,
                    )
                except Exception:
                    # The config is saved either way; the plugin's own reaction failed.
                    logger.exception("plugin %s on_config_changed failed", plugin_id)
        return settings.mask_secrets(schema, new_effective)

    def _require_manifest(self, plugin_id: str) -> PluginManifest:
        record = self.get(plugin_id)
        if record.manifest is None:
            raise PluginUnavailable(f"Plugin '{plugin_id}' has an invalid manifest: {record.error}")
        return record.manifest

    # ---- lifecycle ----

    async def load_all(self) -> None:
        """Discover every plugin and load the enabled ones (backend startup).

        Loading starts each plugin's global autostart services; then session
        services are started for every session that enables them.
        """
        if self.db is not None:
            try:
                count = await self.db.interrupt_active_service_runs("backend restarted while the service ran")
                if count:
                    logger.info("marked %d interrupted service run(s) as errored", count)
            except Exception:
                logger.exception("could not mark interrupted service runs")
        async with self._lock:
            for record in await self._scan(skip=set()):
                await self._activate(record)
        await self.start_all_session_services()

    async def reload(self) -> list[str]:
        """Rescan plugin directories.

        New plugins are loaded and plugins in an error state are retried
        (e.g. after installing a missing dependency or fixing plugin.json).
        Loaded and disabled plugins keep their current code and manifest;
        disable and enable a plugin to re-import its code. Plugins whose
        directory disappeared are unloaded. Returns the ids that changed.
        """
        async with self._lock:
            keep = {pid for pid, r in self._records.items() if r.state in ("loaded", "disabled")}
            present = {d.dir_name for d in loader.discover()}
            changed: list[str] = []
            for plugin_id in sorted(set(self._records) - present):
                await self._deactivate(self._records.pop(plugin_id))
                changed.append(plugin_id)
            for record in await self._scan(skip=keep):
                await self._activate(record)
                changed.append(record.id)
        if changed:
            await self.start_all_session_services()
        return changed

    async def set_enabled(self, plugin_id: str, enabled: bool) -> PluginRecord:
        async with self._lock:
            record = self.get(plugin_id)
            settings.set_enabled(plugin_id, enabled)
            if enabled and record.state != "loaded":
                if record.manifest is None:
                    # Invalid manifest: re-read it, the user may have fixed it.
                    fresh = self._read(record.id, record.path)
                    self._records[plugin_id] = record = fresh
                await self._activate(record)
            elif not enabled and record.state != "disabled":
                await self._deactivate(record)
                if record.manifest is not None:
                    record.state = "disabled"
                    record.error = None
        if enabled and record.state == "loaded":
            await self.start_all_session_services()
        self.services.broadcast_plugin(plugin_id, {"type": "plugin", "data": self.info(record).model_dump(mode="json")})
        return record

    async def shutdown(self) -> None:
        async with self._lock:
            await self.services.stop_where()
            for record in list(self._records.values()):
                await self._deactivate(record)
        await self.services.shutdown()

    # ---- internals ----

    def _read(self, plugin_id: str, path: Path) -> PluginRecord:
        record = PluginRecord(id=plugin_id, path=path)
        try:
            record.manifest = read_manifest(path)
        except ManifestError as e:
            record.state = "error"
            record.error = f"invalid plugin.json: {e}"
        return record

    async def _scan(self, *, skip: set[str]) -> list[PluginRecord]:
        """Read manifests of discovered dirs not in skip; register and return them."""
        self._shadowed = []
        seen: dict[str, Path] = {}
        fresh: list[PluginRecord] = []
        for found in loader.discover():
            if found.dir_name in seen:
                shadowed = PluginRecord(
                    id=found.dir_name, path=found.path, state="error",
                    error=f"id already provided by {seen[found.dir_name]}; this copy is ignored",
                )
                self._shadowed.append(shadowed)
                logger.warning("plugin %s at %s shadowed by %s", found.dir_name, found.path, seen[found.dir_name])
                continue
            seen[found.dir_name] = found.path
            if found.dir_name in skip:
                continue
            old = self._records.get(found.dir_name)
            if old is not None:
                await self._deactivate(old)
            record = self._read(found.dir_name, found.path)
            self._records[record.id] = record
            fresh.append(record)
        return fresh

    async def _activate(self, record: PluginRecord) -> None:
        """Import and on_load the plugin if its manifest is valid and it is enabled."""
        if record.manifest is None:
            return
        if not settings.is_enabled(record.id):
            record.state, record.error = "disabled", None
            return
        record.missing_dependencies = loader.missing_dependencies(record.manifest)
        if record.missing_dependencies:
            record.state = "error"
            record.error = "missing Python packages: " + ", ".join(record.missing_dependencies)
            logger.warning("plugin %s: %s", record.id, record.error)
            return
        try:
            imported = loader.import_plugin(record.manifest, record.path)
        except loader.PluginImportError as e:
            record.state, record.error = "error", f"import failed: {e}"
            logger.warning("plugin %s: %s", record.id, record.error)
            return
        ctx = PluginContext(self, record.id)
        from app.plugins.tools import tool_entries

        try:
            register_plugin_tools(record.id, tool_entries(
                self, record.id, imported.capabilities.tools, record.manifest.name))
        except ValueError as e:
            loader.unload_modules(record.id)
            record.state, record.error = "error", str(e)
            logger.warning("plugin %s: %s", record.id, record.error)
            return
        try:
            await asyncio.wait_for(imported.instance.on_load(ctx), ON_LOAD_TIMEOUT)
        except asyncio.TimeoutError:
            unregister_plugin_tools(record.id)
            loader.unload_modules(record.id)
            record.state, record.error = "error", f"on_load did not finish within {ON_LOAD_TIMEOUT:g}s"
            logger.warning("plugin %s: %s", record.id, record.error)
            return
        except Exception as e:
            unregister_plugin_tools(record.id)
            loader.unload_modules(record.id)
            record.state, record.error = "error", f"on_load failed: {type(e).__name__}: {e}"
            logger.exception("plugin %s on_load failed", record.id)
            return
        record.instance = imported.instance
        record.capabilities = imported.capabilities
        record.ctx = ctx
        record.state, record.error = "loaded", None
        await self._start_global_services(record)
        logger.info(
            "loaded plugin %s %s from %s (tools=%d hooks=%d services=%d)",
            record.id, record.manifest.version, record.path,
            len(imported.capabilities.tools), len(imported.capabilities.hooks),
            len(imported.capabilities.services),
        )

    async def _deactivate(self, record: PluginRecord) -> None:
        if record.state != "loaded" or record.instance is None or record.ctx is None:
            return
        # Tools go first so no new call reaches a plugin that is shutting down,
        # then services and event subscriptions, then on_unload.
        unregister_plugin_tools(record.id)
        await self.services.stop_where(plugin_id=record.id)
        self.event_bus.unsubscribe_owner(record.id)
        try:
            await asyncio.wait_for(record.instance.on_unload(record.ctx), ON_UNLOAD_TIMEOUT)
        except Exception:
            logger.exception("plugin %s on_unload failed", record.id)
        record.instance = None
        record.capabilities = None
        record.ctx = None
        record.state = "disabled"
        for key in [k for k in self._session_state if k[0] == record.id]:
            del self._session_state[key]
        self._active_sessions = {k for k in self._active_sessions if k[0] != record.id}
        self.services.forget_plugin(record.id)
        loader.unload_modules(record.id)
        logger.info("unloaded plugin %s", record.id)


_host = PluginHost()


def get_plugin_host() -> PluginHost:
    return _host
