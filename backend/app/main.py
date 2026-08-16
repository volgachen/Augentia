from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from app.logging_config import setup_logging
setup_logging()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.auth import is_request_authenticated, validate_auth_config
from app.config import get_settings
from app.db.deps import get_db, init_db, close_db
from app.plugins.catalog import get_plugin_catalog
from app.plugins.events import get_plugin_event_bus
from app.plugins.registry import get_plugin_registry
from app.plugins.service import (
    auto_start_plugin_instances,
    recover_interrupted_plugin_runs,
    register_plugin_event_delivery,
    stop_running_plugin_instances,
)
from app.routers import agents, auth, sessions, fs, plugins, tools, tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_auth_config(get_settings())
    await init_db()
    db = get_db()
    registry = get_plugin_registry()
    catalog = get_plugin_catalog()
    event_bus = get_plugin_event_bus()
    register_plugin_event_delivery(event_bus, db, registry, catalog)
    await recover_interrupted_plugin_runs(db)
    # await auto_start_plugin_instances(db, registry, catalog)
    try:
        yield
    finally:
        await stop_running_plugin_instances(db, registry)
        await close_db()


settings = get_settings()
app = FastAPI(
    title="Agent Gateway",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if settings.auth_enabled else "/docs",
    redoc_url=None if settings.auth_enabled else "/redoc",
    openapi_url=None if settings.auth_enabled else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins if settings.auth_enabled else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def require_authentication(request: Request, call_next):
    public_paths = {"/health", "/api/v1/auth/login", "/api/v1/auth/me"}
    if request.url.path.startswith("/api/v1") and request.url.path not in public_paths:
        if not is_request_authenticated(request, settings):
            return JSONResponse(status_code=401, content={"detail": "authentication required"})
    return await call_next(request)


app.include_router(auth.router, prefix="/api/v1")
app.include_router(agents.router, prefix="/api/v1")
app.include_router(sessions.router, prefix="/api/v1")
app.include_router(fs.router, prefix="/api/v1")
app.include_router(plugins.router, prefix="/api/v1")
app.include_router(tools.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok"}
