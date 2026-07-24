"""FastAPI application factory and server entry point.

This is the future backend. It holds no business logic -- every route
delegates to the shared :class:`AppPipeline` and the existing auth/context
modules. The Streamlit app and any later frontend become HTTP clients of it.
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.errors import install_error_handlers
from src.api.routes import (
    auth,
    config,
    conversations,
    departments,
    files,
    governance,
    kb_permissions,
    kbs,
    logs,
    parse_tasks,
    query,
    upload,
    users,
)


def create_app() -> FastAPI:
    app = FastAPI(title="Hardware DataBase API", version="0.1.0")
    install_error_handlers(app)

    # CORS: allow browser-based clients and agent tools to reach the API.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(conversations.router)
    app.include_router(kbs.router)
    app.include_router(files.router)
    app.include_router(parse_tasks.router)
    app.include_router(query.router)
    app.include_router(upload.router)
    app.include_router(users.router)
    app.include_router(departments.router)
    app.include_router(kb_permissions.router)
    app.include_router(governance.router)
    app.include_router(config.router)
    app.include_router(logs.router)

    @app.get("/health", tags=["health"])
    def health() -> dict:
        return {"ok": True}

    return app


app = create_app()


def main() -> None:
    """Run the API server (console script `hardware-database-server`)."""
    import uvicorn

    host = os.getenv("HDB_API_HOST", "127.0.0.1")
    port = int(os.getenv("HDB_API_PORT", "8000"))
    uvicorn.run("src.api.app:app", host=host, port=port)
