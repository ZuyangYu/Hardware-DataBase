"""Map domain exceptions to HTTP responses.

The permission rules themselves live in RAGFlowBackend._check_kb_access and
RequestContext.has_kb_permission; this layer only translates the exceptions
they raise into HTTP status codes -- no duplicate authorisation logic.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(PermissionError)
    async def _permission(_request: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc) or "permission denied"})

    @app.exception_handler(ValueError)
    async def _value(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(RuntimeError)
    async def _runtime(_request: Request, exc: RuntimeError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
