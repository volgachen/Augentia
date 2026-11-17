import logging
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
from app.adapters.registry import get_registry as get_session_registry
from app.plugins.host import get_plugin_host
from app.routers import agents, attachments, auth, sessions, share, fs, plugins, tools, tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_auth_config(get_settings())
    await init_db()
    db = get_db()
    plugin_host = get_plugin_host()
    plugin_host.bind(db, get_session_registry())
    await plugin_host.load_all()
    # fork-dispatch branches whose turn ended while the backend was down (or
    # whose join was cut short) get their result appended now.
    from app.adapters.registry import get_registry as get_adapter_registry
    from app.core.session_runtime import join_pending_branches
    try:
        await join_pending_branches(db, get_adapter_registry())
    except Exception:
        logging.getLogger("augentia").exception("joining pending branches failed")
    try:
        yield
    finally:
        await plugin_host.shutdown()
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
    path = request.url.path
    # /share serves only sessions their owner explicitly shared (read-only).
    is_public = path in public_paths or path.startswith("/api/v1/share/")
    if path.startswith("/api/v1") and not is_public:
        if not is_request_authenticated(request, settings):
            return JSONResponse(status_code=401, content={"detail": "authentication required"})
    return await call_next(request)


app.include_router(auth.router, prefix="/api/v1")
app.include_router(agents.router, prefix="/api/v1")
app.include_router(sessions.router, prefix="/api/v1")
app.include_router(share.router, prefix="/api/v1")
app.include_router(fs.router, prefix="/api/v1")
app.include_router(plugins.router, prefix="/api/v1")
app.include_router(tools.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")
app.include_router(attachments.router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok"}
