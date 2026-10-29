"""Map domain exceptions to HTTP responses.

The permission rules themselves live in RAGFlowBackend._check_kb_access and
RequestContext.has_kb_permission; this layer only translates the exceptions
they raise into HTTP status codes -- no duplicate authorisation logic.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.api.external_support import ExternalApiError


def _is_external(request: Request) -> bool:
    return request.url.path.startswith("/api/v1/external")


def _audit_external_failure(request: Request, code: str, status_code: int) -> None:
    """Best-effort audit for authenticated External failures.

    Missing/invalid keys are deliberately not written: that path is
    unauthenticated and could be used to amplify audit writes.
    """
    principal = getattr(request.state, "external_principal", None)
    if principal is None:
        return
    try:
        from src.api.deps import get_auth_service
        from src.api.external_support import audit_external

        auth = getattr(request.state, "external_auth", None) or get_auth_service()
        audit_external(
            auth,
            action="external_request_failed",
            principal=principal,
            request=request,
            success=False,
            kb_ref=str(getattr(request.state, "external_kb_ref", "") or ""),
            error_code=code,
            metadata={"status_code": status_code, "path": request.url.path},
        )
    except Exception:
        # Audit is fail-soft here as well; authorization has already run.
        pass


def _external_error(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
) -> JSONResponse:
    request_id = str(getattr(request.state, "request_id", "") or "")
    return JSONResponse(
        status_code=status_code,
        headers={"X-Request-ID": request_id} if request_id else {},
        content={
            "request_id": request_id,
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
            },
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ExternalApiError)
    async def _external(request: Request, exc: ExternalApiError) -> JSONResponse:
        _audit_external_failure(request, exc.code, exc.status_code)
        response = _external_error(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
        )
        if exc.retry_after is not None:
            response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        if not _is_external(request):
            return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
        _audit_external_failure(request, "INVALID_REQUEST", 422)
        return _external_error(
            request,
            status_code=422,
            code="INVALID_REQUEST",
            message="请求参数不符合约束",
        )

    @app.exception_handler(PermissionError)
    async def _permission(request: Request, exc: PermissionError) -> JSONResponse:
        if _is_external(request):
            _audit_external_failure(request, "KB_ACCESS_DENIED", 403)
            return _external_error(
                request,
                status_code=403,
                code="KB_ACCESS_DENIED",
                message="无权访问该资源",
            )
        return JSONResponse(status_code=403, content={"detail": str(exc) or "permission denied"})

    @app.exception_handler(ValueError)
    async def _value(request: Request, exc: ValueError) -> JSONResponse:
        if _is_external(request):
            _audit_external_failure(request, "INVALID_REQUEST", 422)
            return _external_error(
                request,
                status_code=422,
                code="INVALID_REQUEST",
                message="请求参数不符合约束",
            )
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(RuntimeError)
    async def _runtime(request: Request, exc: RuntimeError) -> JSONResponse:
        if _is_external(request):
            _audit_external_failure(request, "RETRIEVAL_BACKEND_UNAVAILABLE", 503)
            return _external_error(
                request,
                status_code=503,
                code="RETRIEVAL_BACKEND_UNAVAILABLE",
                message="服务暂时不可用",
                retryable=True,
            )
        return JSONResponse(status_code=500, content={"detail": str(exc)})
