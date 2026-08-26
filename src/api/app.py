"""FastAPI application factory and server entry point.

This is the future backend. It holds no business logic -- every route
delegates to the shared :class:`AppPipeline` and the existing auth/context
modules. The Streamlit app and any later frontend become HTTP clients of it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.errors import install_error_handlers
from src.api.deps import require_system_admin
from src.api.routes import (
    assets,
    auth,
    config,
    conversations,
    departments,
    document_generation,
    evaluation,
    files,
    governance,
    kb_permissions,
    kbs,
    logs,
    metrics,
    parse_tasks,
    query,
    status,
    structured,
    upload,
    users,
)
from src.observability import init_observability, instrument_fastapi, shutdown_observability
from src.observability.health import check_dependencies, check_live, check_ready


def _should_spawn_worker() -> bool:
    """Whether the API server should auto-spawn the background parse worker.

    On is the default so a single `hardware-database-server` command also runs
    the Excel-parse worker that the spreadsheet pipeline depends on. Multi-host
    deployments (N API instances) should set HDB_API_SPAWN_WORKER=0 and run the
    worker as a dedicated service instead, to avoid N workers competing on the
    shared SQLite claim queue.
    """
    return os.getenv("HDB_API_SPAWN_WORKER", "1").lower() not in {"0", "false", "no", "off"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Co-manage the background parse worker with the API server process.

    The spreadsheet pipeline requires a standalone worker to claim and index
    queued xlsx records (see ``src/pipelines/runtime.py``). We spawn it here as
    a child subprocess so a single backend command also runs Excel parsing, while
    keeping it a separate OS process (crash isolation + DB-level claim queue).
    """
    import src.settings

    worker_proc: subprocess.Popen | None = None
    if _should_spawn_worker():
        log_dir = os.path.join(src.settings.STORAGE_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "worker.log")
        log_file = open(log_path, "ab", buffering=0)
        worker_proc = subprocess.Popen(
            [sys.executable, "-m", "src.workers.main"],
            cwd=src.settings.BASE_DIR,
            stdout=log_file,
            stderr=log_file,
        )
        print(f"[hardware-database] spawned parse worker pid={worker_proc.pid} log={log_path}")
    try:
        yield
    finally:
        if worker_proc is not None and worker_proc.poll() is None:
            worker_proc.terminate()
            try:
                worker_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker_proc.kill()
        shutdown_observability()


def create_app() -> FastAPI:
    import src.settings as settings

    init_observability(
        "hardware-database-api",
        service_version=settings.OBS_SERVICE_VERSION,
        environment=settings.OBS_ENVIRONMENT,
    )
    app = FastAPI(title="Hardware DataBase API", version="0.1.0", lifespan=lifespan)
    instrument_fastapi(app)
    install_error_handlers(app)

    # CORS: allow browser-based clients and agent tools to reach the API.
    # allow_origins=["*"] + allow_credentials=True is rejected by browsers, so
    # origins are configurable via HDB_API_CORS_ORIGINS (comma-separated). When
    # unset we default to local dev origins; credentials are enabled to support
    # cookie/header-based sessions from the future frontend.
    cors_env = os.getenv("HDB_API_CORS_ORIGINS", "")
    if cors_env.strip():
        origins = [o.strip() for o in cors_env.split(",") if o.strip()]
    else:
        origins = [
            "http://localhost:8501",
            "http://127.0.0.1:8501",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:5174",
            "http://127.0.0.1:5174",
        ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    api_v1 = "/api/v1"
    app.include_router(auth.router, prefix=api_v1)
    app.include_router(assets.router, prefix=api_v1)
    app.include_router(conversations.router, prefix=api_v1)
    app.include_router(kbs.router, prefix=api_v1)
    app.include_router(files.router, prefix=api_v1)
    app.include_router(parse_tasks.router, prefix=api_v1)
    app.include_router(query.router, prefix=api_v1)
    app.include_router(upload.router, prefix=api_v1)
    app.include_router(users.router, prefix=api_v1)
    app.include_router(departments.router, prefix=api_v1)
    app.include_router(document_generation.router, prefix=api_v1)
    app.include_router(kb_permissions.router, prefix=api_v1)
    app.include_router(governance.router, prefix=api_v1)
    app.include_router(config.router, prefix=api_v1)
    app.include_router(logs.router, prefix=api_v1)
    app.include_router(metrics.router, prefix=api_v1)
    app.include_router(status.router, prefix=api_v1)
    app.include_router(evaluation.router, prefix=api_v1)
    app.include_router(structured.router, prefix=api_v1)

    @app.get("/health", tags=["health"])
    def health() -> dict:
        return {"ok": True, **check_live()}

    @app.get("/health/live", tags=["health"])
    def health_live() -> dict:
        return check_live()

    @app.get("/health/ready", tags=["health"])
    def health_ready() -> dict:
        return check_ready()

    @app.get("/health/dependencies", tags=["health"])
    def health_dependencies(_viewer=Depends(require_system_admin)) -> dict:
        return check_dependencies()

    return app


app = create_app()


def main() -> None:
    """Run the API server (console script `hardware-database-server`)."""
    import uvicorn

    host = os.getenv("HDB_API_HOST", "127.0.0.1")
    port = int(os.getenv("HDB_API_PORT", "8001"))
    uvicorn.run("src.api.app:app", host=host, port=port)
