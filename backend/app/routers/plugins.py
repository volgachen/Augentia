import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.adapters.registry import AdapterRegistry, get_registry as get_session_registry
from app.auth import is_websocket_authenticated
from app.db.deps import get_db
from app.db.interface import IAgentDatabase
from app.models.domain import PluginInstance, PluginLog, PluginRun, PluginStatus
from app.plugins.actions import PluginActionDispatcher
from app.plugins.catalog import PluginCatalog, PluginDefinition, get_plugin_catalog
from app.plugins.registry import PluginRunnerRegistry, get_plugin_registry


logger = logging.getLogger("augentia.plugins")
router = APIRouter(prefix="/plugins", tags=["plugins"])


class CreatePluginInstanceRequest(BaseModel):
    plugin_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    config: dict | None = None
    auto_start: bool = False


class UpdatePluginInstanceRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict | None = None
    auto_start: bool | None = None


class PluginCommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=300)
    data: dict = Field(default_factory=dict)
    timeout_ms: int = Field(default=10000, ge=100, le=60000)


@router.get("/catalog", response_model=list[PluginDefinition])
async def list_plugin_catalog(
    catalog: PluginCatalog = Depends(get_plugin_catalog),
):
    return catalog.list()


@router.get("/catalog/{plugin_id}", response_model=PluginDefinition)
async def get_plugin_definition(
    plugin_id: str,
    catalog: PluginCatalog = Depends(get_plugin_catalog),
):
    try:
        return catalog.get(plugin_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/instances", response_model=list[PluginInstance])
async def list_plugin_instances(db: IAgentDatabase = Depends(get_db)):
    return await db.list_plugin_instances()


@router.post("/instances", response_model=PluginInstance, status_code=201)
async def create_plugin_instance(
    body: CreatePluginInstanceRequest,
    db: IAgentDatabase = Depends(get_db),
    catalog: PluginCatalog = Depends(get_plugin_catalog),
):
    try:
        catalog.get(body.plugin_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return await db.create_plugin_instance(
        plugin_id=body.plugin_id,
        display_name=body.display_name,
        config=body.config,
        auto_start=body.auto_start,
    )


@router.get("/instances/{instance_id}", response_model=PluginInstance)
async def get_plugin_instance(instance_id: str, db: IAgentDatabase = Depends(get_db)):
    try:
        return await db.get_plugin_instance(instance_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/instances/{instance_id}", response_model=PluginInstance)
async def update_plugin_instance(
    instance_id: str,
    body: UpdatePluginInstanceRequest,
    db: IAgentDatabase = Depends(get_db),
):
    try:
        instance = await db.get_plugin_instance(instance_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if instance.status in {
        PluginStatus.STARTING,
        PluginStatus.WAITING_INPUT,
        PluginStatus.RUNNING,
        PluginStatus.STOPPING,
    }:
        raise HTTPException(status_code=409, detail="Stop the plugin instance before editing it.")
    return await db.update_plugin_instance(
        instance_id,
        display_name=body.display_name,
        config=body.config,
        auto_start=body.auto_start,
    )


@router.delete("/instances/{instance_id}", status_code=204)
async def delete_plugin_instance(
    instance_id: str,
    db: IAgentDatabase = Depends(get_db),
    registry: PluginRunnerRegistry = Depends(get_plugin_registry),
):
    try:
        await db.get_plugin_instance(instance_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await registry.remove(instance_id)
    await db.delete_plugin_instance(instance_id)


@router.post("/instances/{instance_id}/start", response_model=PluginRun)
async def start_plugin_instance(
    instance_id: str,
    db: IAgentDatabase = Depends(get_db),
    registry: PluginRunnerRegistry = Depends(get_plugin_registry),
    catalog: PluginCatalog = Depends(get_plugin_catalog),
    session_registry: AdapterRegistry = Depends(get_session_registry),
):
    try:
        instance = await db.get_plugin_instance(instance_id)
        definition = catalog.get(instance.plugin_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))

    dispatcher = PluginActionDispatcher(db, session_registry)
    runner = registry.get_or_create(instance_id, db, dispatcher)
    if runner.is_running:
        raise HTTPException(status_code=409, detail="plugin instance already running")

    try:
        return await runner.start(definition, instance)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/instances/{instance_id}/stop", response_model=PluginInstance)
async def stop_plugin_instance(
    instance_id: str,
    db: IAgentDatabase = Depends(get_db),
    registry: PluginRunnerRegistry = Depends(get_plugin_registry),
):
    try:
        await db.get_plugin_instance(instance_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    runner = registry.get(instance_id)
    if runner is not None:
        await runner.stop()
    return await db.get_plugin_instance(instance_id)


@router.post("/instances/{instance_id}/commands")
async def send_plugin_command(
    instance_id: str,
    body: PluginCommandRequest,
    db: IAgentDatabase = Depends(get_db),
    registry: PluginRunnerRegistry = Depends(get_plugin_registry),
):
    try:
        await db.get_plugin_instance(instance_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    runner = registry.get(instance_id)
    if runner is None:
        raise HTTPException(status_code=409, detail="plugin instance is not running")
    try:
        return await runner.send_command(
            command=body.command,
            data=body.data,
            timeout=body.timeout_ms / 1000,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/instances/{instance_id}/restart", response_model=PluginRun)
async def restart_plugin_instance(
    instance_id: str,
    db: IAgentDatabase = Depends(get_db),
    registry: PluginRunnerRegistry = Depends(get_plugin_registry),
    catalog: PluginCatalog = Depends(get_plugin_catalog),
    session_registry: AdapterRegistry = Depends(get_session_registry),
):
    try:
        instance = await db.get_plugin_instance(instance_id)
        definition = catalog.get(instance.plugin_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    dispatcher = PluginActionDispatcher(db, session_registry)
    runner = registry.get_or_create(instance_id, db, dispatcher)
    if runner.is_running:
        await runner.stop()
        instance = await db.get_plugin_instance(instance_id)
    try:
        return await runner.start(definition, instance)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/instances/{instance_id}/runs", response_model=list[PluginRun])
async def list_plugin_runs(instance_id: str, db: IAgentDatabase = Depends(get_db)):
    try:
        return await db.list_plugin_runs(instance_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/runs/{run_id}", response_model=PluginRun)
async def get_plugin_run(run_id: str, db: IAgentDatabase = Depends(get_db)):
    try:
        return await db.get_plugin_run(run_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/runs/{run_id}/logs", response_model=list[PluginLog])
async def get_plugin_run_logs(
    run_id: str,
    limit: int = Query(default=500, ge=1, le=5000),
    session_id: str | None = Query(default=None),
    db: IAgentDatabase = Depends(get_db),
):
    try:
        await db.get_plugin_run(run_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return await db.list_plugin_logs(plugin_run_id=run_id, session_id=session_id, limit=limit)


@router.get("/instances/{instance_id}/logs", response_model=list[PluginLog])
async def get_plugin_instance_logs(
    instance_id: str,
    limit: int = Query(default=500, ge=1, le=5000),
    session_id: str | None = Query(default=None),
    db: IAgentDatabase = Depends(get_db),
):
    try:
        await db.get_plugin_instance(instance_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return await db.list_plugin_logs(plugin_instance_id=instance_id, session_id=session_id, limit=limit)


@router.websocket("/instances/{instance_id}/stream")
async def plugin_instance_stream(
    instance_id: str,
    ws: WebSocket,
    db: IAgentDatabase = Depends(get_db),
    registry: PluginRunnerRegistry = Depends(get_plugin_registry),
):
    if not is_websocket_authenticated(ws):
        await ws.close(code=1008, reason="authentication required")
        return
    await ws.accept()
    try:
        instance = await db.get_plugin_instance(instance_id)
    except KeyError:
        await ws.send_text(json.dumps({"type": "error", "data": f"plugin instance '{instance_id}' not found"}))
        await ws.close()
        return

    runner = registry.get_or_create(instance_id, db)
    queue, snapshot, status = await runner.subscribe()

    async def _send_json(frame: dict) -> bool:
        try:
            await ws.send_text(json.dumps(frame))
            return True
        except (WebSocketDisconnect, RuntimeError):
            return False

    try:
        if not await _send_json({
            "type": "plugin_instance_state",
            "data": instance.model_dump(mode="json"),
        }):
            return
        for entry in snapshot:
            if not await _send_json({
                "type": "log",
                "data": entry.model_dump(mode="json"),
            }):
                return
        if not await _send_json({
            "type": "status",
            "data": {"status": status.value, "run_id": runner.run_id},
        }):
            return

        async def _client_pinger() -> None:
            while True:
                await ws.receive_text()

        pinger = asyncio.create_task(_client_pinger())
        try:
            while True:
                frame = await queue.get()
                if not await _send_json(frame):
                    return
        finally:
            pinger.cancel()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("plugin WS error instance=%s", instance_id)
    finally:
        await runner.unsubscribe(queue)
