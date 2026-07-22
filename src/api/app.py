"""FastAPI application factory and server entry point.

This is the future backend. It holds no business logic -- every route
delegates to the shared :class:`AppPipeline` and the existing auth/context
modules. The Streamlit app and any later frontend become HTTP clients of it.
"""
from __future__ import annotations

import os

from fastapi import FastAPI

from src.api.errors import install_error_handlers
from src.api.routes import auth, kbs, query, upload


def create_app() -> FastAPI:
    app = FastAPI(title="Hardware DataBase API", version="0.1.0")
    install_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(kbs.router)
    app.include_router(query.router)
    app.include_router(upload.router)

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
