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
    evaluation,
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
            "http://localhost:5173",
            "http://127.0.0.1:5173",
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
    app.include_router(conversations.router, prefix=api_v1)
    app.include_router(kbs.router, prefix=api_v1)
    app.include_router(files.router, prefix=api_v1)
    app.include_router(parse_tasks.router, prefix=api_v1)
    app.include_router(query.router, prefix=api_v1)
    app.include_router(upload.router, prefix=api_v1)
    app.include_router(users.router, prefix=api_v1)
    app.include_router(departments.router, prefix=api_v1)
    app.include_router(kb_permissions.router, prefix=api_v1)
    app.include_router(governance.router, prefix=api_v1)
    app.include_router(config.router, prefix=api_v1)
    app.include_router(logs.router, prefix=api_v1)
    app.include_router(evaluation.router, prefix=api_v1)

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
