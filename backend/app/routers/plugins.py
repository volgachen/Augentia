"""Plugin management API (app/plugins/README.md §12)."""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.auth import is_websocket_authenticated
from app.db.deps import get_db
from app.db.interface import IAgentDatabase
from app.models.domain import PluginServiceLog, PluginServiceRun
from app.plugins.host import (
    ActionError, PluginConfigError, PluginHost, PluginInfo, PluginNotFound, PluginNotLoaded,
    PluginUnavailable, get_plugin_host,
)
from app.plugins.services import ServiceState, UIState


logger = logging.getLogger("augentia.plugins")
router = APIRouter(prefix="/plugins", tags=["plugins"])



class SetPluginEnabledRequest(BaseModel):
    enabled: bool


class SetPluginConfigRequest(BaseModel):
    config: dict


class ReloadPluginsResponse(BaseModel):
    changed: list[str]
    plugins: list[PluginInfo]


def _plugin_or_404(host: PluginHost, plugin_id: str):
    try:
        return host.get(plugin_id)
    except PluginNotFound as e:
        raise HTTPException(status_code=404, detail=str(e.args[0]))


@router.get("", response_model=list[PluginInfo])
async def list_plugins(host: PluginHost = Depends(get_plugin_host)):
    return host.list_info()


@router.post("/reload", response_model=ReloadPluginsResponse)
async def reload_plugins(host: PluginHost = Depends(get_plugin_host)):
    changed = await host.reload()
    return ReloadPluginsResponse(changed=changed, plugins=host.list_info())


@router.websocket("/stream")
async def plugins_stream(
    ws: WebSocket,
    session_id: str | None = Query(default=None),
    plugin_id: str | None = Query(default=None),
    host: PluginHost = Depends(get_plugin_host),
):
    """Live plugin updates (README §10.5).

    On connect: one ``snapshot`` frame {plugins, services, ui, logs} (logs:
    recent buffered lines), then incremental frames:
    ``plugin`` (PluginInfo), ``service`` (ServiceState), ``ui`` (UIState),
    ``log`` (log line). With ``session_id``, only that session's items plus
    global ones; with ``plugin_id``, only that plugin.
    """
    if not is_websocket_authenticated(ws):
        await ws.close(code=1008, reason="authentication required")
        return
    await ws.accept()
    sub = host.services.subscribe(session_id=session_id, plugin_id=plugin_id)
    try:
        plugins = [p for p in host.list_info() if plugin_id is None or p.id == plugin_id]
        services = [s for s in host.services.states(plugin_id)
                    if session_id is None or s.session_id in (None, session_id)]
        ui = [u for u in host.services.ui_states()
              if (plugin_id is None or u.plugin_id == plugin_id)
              and (session_id is None or u.session_id in (None, session_id))]
        logs = []
        for p in plugins:
            logs.extend(host.services.recent_logs(p.id, session_id, limit=200))
        logs.sort(key=lambda line: line.ts)
        await ws.send_text(json.dumps({"type": "snapshot", "data": {
            "plugins": [p.model_dump(mode="json") for p in plugins],
            "services": [s.model_dump(mode="json") for s in services],
            "ui": [u.model_dump(mode="json") for u in ui],
            "logs": [line.model_dump(mode="json") for line in logs],
        }}))

        async def client_reader() -> None:
            # Clients may send pings; a closed socket ends the stream.
            while True:
                await ws.receive_text()

        reader = asyncio.create_task(client_reader())
        try:
            while True:
                getter = asyncio.create_task(sub.queue.get())
                done, _ = await asyncio.wait({getter, reader}, return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    getter.cancel()
                    return
                await ws.send_text(json.dumps(getter.result()))
        finally:
            reader.cancel()
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        logger.exception("plugins stream failed")
    finally:
        host.services.unsubscribe(sub)


@router.get("/{plugin_id}", response_model=PluginInfo)
async def get_plugin(plugin_id: str, host: PluginHost = Depends(get_plugin_host)):
    return host.info(_plugin_or_404(host, plugin_id))


@router.put("/{plugin_id}/enabled", response_model=PluginInfo)
async def set_plugin_enabled(
    plugin_id: str,
    body: SetPluginEnabledRequest,
    host: PluginHost = Depends(get_plugin_host),
):
    _plugin_or_404(host, plugin_id)
    record = await host.set_enabled(plugin_id, body.enabled)
    return host.info(record)


@router.get("/{plugin_id}/config")
async def get_plugin_config(plugin_id: str, host: PluginHost = Depends(get_plugin_host)):
    _plugin_or_404(host, plugin_id)
    try:
        return host.masked_global_config(plugin_id)
    except PluginUnavailable as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.put("/{plugin_id}/config")
async def set_plugin_config(
    plugin_id: str,
    body: SetPluginConfigRequest,
    host: PluginHost = Depends(get_plugin_host),
):
    _plugin_or_404(host, plugin_id)
    try:
        return await host.set_global_config(plugin_id, body.config)
    except PluginUnavailable as e:
        raise HTTPException(status_code=409, detail=str(e))
    except PluginConfigError as e:
        raise HTTPException(status_code=400, detail={"message": "invalid plugin config", "errors": e.errors})


class ActionRequest(BaseModel):
    session_id: str | None = None
    data: dict = Field(default_factory=dict)


def _service_error(e: Exception) -> HTTPException:
    if isinstance(e, PluginNotFound):
        return HTTPException(status_code=404, detail=str(e.args[0]))
    if isinstance(e, PluginNotLoaded):
        return HTTPException(status_code=409, detail=str(e))
    if isinstance(e, KeyError):
        return HTTPException(status_code=404, detail=str(e.args[0]))
    if isinstance(e, ValueError):
        return HTTPException(status_code=400, detail=str(e))
    raise e


@router.get("/{plugin_id}/services", response_model=list[ServiceState])
async def list_plugin_services(
    plugin_id: str,
    session_id: str | None = Query(default=None),
    host: PluginHost = Depends(get_plugin_host),
):
    _plugin_or_404(host, plugin_id)
    return host.services.states(plugin_id, session_id)


@router.post("/{plugin_id}/services/{service}/{op}", response_model=ServiceState)
async def control_plugin_service(
    plugin_id: str,
    service: str,
    op: str,
    session_id: str | None = Query(default=None),
    host: PluginHost = Depends(get_plugin_host),
):
    """op: start / stop / restart. Session services need ?session_id=."""
    _plugin_or_404(host, plugin_id)
    if op not in ("start", "stop", "restart"):
        raise HTTPException(status_code=404, detail=f"unknown operation '{op}'")
    spec = host.service_spec(plugin_id, service)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' has no service '{service}'")
    if spec.scope == "global":
        session_id = None
    elif not session_id:
        raise HTTPException(status_code=400, detail=f"service '{service}' is per session; pass ?session_id=")
    try:
        if op in ("stop", "restart"):
            await host.services.stop(plugin_id, service, session_id)
        if op in ("start", "restart"):
            await host.start_service(plugin_id, service, session_id)
    except Exception as e:
        raise _service_error(e)
    state = host.services.get(plugin_id, service, session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="service was never started")
    return state


@router.get("/{plugin_id}/runs", response_model=list[PluginServiceRun])
async def list_plugin_service_runs(
    plugin_id: str,
    service: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    host: PluginHost = Depends(get_plugin_host),
    db: IAgentDatabase = Depends(get_db),
):
    _plugin_or_404(host, plugin_id)
    return await db.list_service_runs(plugin_id, service=service, session_id=session_id, limit=limit)


@router.get("/{plugin_id}/logs", response_model=list[PluginServiceLog])
async def list_plugin_service_logs(
    plugin_id: str,
    session_id: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    host: PluginHost = Depends(get_plugin_host),
    db: IAgentDatabase = Depends(get_db),
):
    _plugin_or_404(host, plugin_id)
    return await db.list_service_logs(plugin_id, run_id=run_id, session_id=session_id, limit=limit)


@router.get("/{plugin_id}/ui", response_model=UIState | None)
async def get_plugin_ui(
    plugin_id: str,
    session_id: str | None = Query(default=None),
    host: PluginHost = Depends(get_plugin_host),
):
    _plugin_or_404(host, plugin_id)
    return host.services.ui_state(plugin_id, session_id)


@router.post("/{plugin_id}/actions/{action_id}")
async def run_plugin_action(
    plugin_id: str,
    action_id: str,
    body: ActionRequest,
    host: PluginHost = Depends(get_plugin_host),
    db: IAgentDatabase = Depends(get_db),
):
    """Call a plugin action; returns {"result": <what the action returned>}."""
    _plugin_or_404(host, plugin_id)
    if body.session_id is not None:
        try:
            await db.get_session(body.session_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
    try:
        result = await host.run_action(plugin_id, action_id, body.session_id, body.data)
    except ActionError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise _service_error(e)
    try:
        json.dumps(result)
    except (TypeError, ValueError):
        result = repr(result)
    return {"result": result}
