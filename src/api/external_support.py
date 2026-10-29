"""Shared security helpers for the employee-key External API.

The External surface intentionally does not reuse ``current_user``.  Session
authentication and API-key authentication have different headers and different
route allowlists; keeping this boundary in one module makes accidental cross
use harder to review.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from fastapi import Depends, Header, HTTPException, Request

import src.settings
import src.external_settings as external_settings
from src.api.context import build_context_for_user
from src.core.auth import (
    AuthService,
    AuthUser,
    EmployeeApiKeyPrincipal,
    ExternalKnowledgeBaseReference,
)
from src.api.deps import get_auth_service


class ExternalApiError(RuntimeError):
    """Safe, stable error contract for External callers."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(code)


@dataclass(frozen=True)
class ExternalAuthorization:
    principal: EmployeeApiKeyPrincipal
    user: AuthUser
    kb: ExternalKnowledgeBaseReference
    context: object


def request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


def get_employee_api_key_principal(
    request: Request,
    x_hdb_api_key: str | None = Header(default=None, alias="X-HDB-API-Key"),
    authorization: str | None = Header(default=None),
    auth: AuthService = Depends(get_auth_service),
) -> EmployeeApiKeyPrincipal:
    """Authenticate only the External API-key channel.

    ``auth`` is resolved by FastAPI through the route dependency graph; callers
    invoking this helper directly must pass an explicit ``AuthService``.
    """
    if authorization and authorization.strip():
        raise ExternalApiError(401, "AUTH_INVALID", "External API 仅接受 X-HDB-API-Key")
    if not x_hdb_api_key or not x_hdb_api_key.strip():
        raise ExternalApiError(401, "AUTH_REQUIRED", "需要 External API Key")
    auth = auth or get_auth_service()
    try:
        principal, failure_code = auth.resolve_employee_api_key(x_hdb_api_key.strip())
    except Exception as exc:  # noqa: BLE001 - authentication backend failure is retryable
        raise ExternalApiError(
            503,
            "AUTH_BACKEND_UNAVAILABLE",
            "认证服务暂不可用",
            retryable=True,
        ) from exc
    if principal is None:
        if failure_code == "KEY_SCOPE_DENIED":
            raise ExternalApiError(403, "KEY_SCOPE_DENIED", "API Key 的访问模式已关闭")
        if failure_code == "KEY_EXPIRED":
            raise ExternalApiError(401, "KEY_EXPIRED", "External API Key 已过期")
        if failure_code == "KEY_REVOKED":
            raise ExternalApiError(401, "KEY_REVOKED", "External API Key 已撤销")
        raise ExternalApiError(401, "AUTH_INVALID", "External API Key 无效")
    # Keep the authenticated identity on the request so the central error
    # handler can audit a later 403/404/409 without trusting route payloads.
    request.state.external_principal = principal
    request.state.external_auth = auth
    return principal


def require_external_principal(
    request: Request,
    x_hdb_api_key: str | None = Header(default=None, alias="X-HDB-API-Key"),
    authorization: str | None = Header(default=None),
    auth: AuthService = Depends(get_auth_service),
) -> EmployeeApiKeyPrincipal:
    """FastAPI-friendly wrapper with an explicit AuthService dependency."""
    return get_employee_api_key_principal(request, x_hdb_api_key, authorization, auth)


def authorize_kb(
    principal: EmployeeApiKeyPrincipal,
    kb_ref: str,
    *,
    auth: AuthService,
    required_scope: str = "read",
    required_kb_permission: str = "read",
    request: Request | None = None,
) -> ExternalAuthorization:
    if required_scope not in principal.scopes:
        raise ExternalApiError(403, "KEY_SCOPE_DENIED", "API Key 缺少所需操作权限")
    kb = auth.get_external_kb_ref(kb_ref)
    if kb is None:
        # Deliberately do not distinguish an unknown ref from an inaccessible
        # ref: external references must not become an enumeration oracle.
        raise ExternalApiError(404, "RESOURCE_NOT_FOUND", "资源不存在")
    if request is not None:
        request.state.external_kb_ref = kb_ref
    if principal.kb_access_mode == "selected_kbs" and kb.kb_id not in principal.kb_ids:
        raise ExternalApiError(403, "KEY_SCOPE_DENIED", "API Key 未授权该知识库")
    if principal.kb_access_mode == "all_current_accessible_kbs" and not bool(
        external_settings.HDB_EXTERNAL_ALL_KBS_MODE_ENABLED
    ):
        raise ExternalApiError(403, "KEY_SCOPE_DENIED", "API Key 的访问模式已关闭")

    user = auth.get_user_by_id(principal.user_id)
    if user is None or not user.is_active or user.role != "employee":
        raise ExternalApiError(401, "AUTH_INVALID", "绑定员工账号已停用")
    if kb.department_id != user.department_id:
        raise ExternalApiError(403, "KB_ACCESS_DENIED", "无权访问该知识库")
    context = build_context_for_user(user, kb.kb_name, auth=auth)
    if not context.has_kb_permission(kb.kb_name, required_kb_permission):
        raise ExternalApiError(403, "KB_ACCESS_DENIED", "无权访问该知识库")
    # The opaque mapping is authoritative here; pin the internal KB id so the
    # downstream record-scope validator cannot silently fall back to a name
    # lookup or a broad dataset.
    context.metadata["kb_id"] = kb.kb_id
    context.metadata["resource_department_id"] = kb.department_id
    return ExternalAuthorization(principal=principal, user=user, kb=kb, context=context)


def audit_external(
    auth: AuthService,
    *,
    action: str,
    principal: EmployeeApiKeyPrincipal | None,
    request: Request,
    success: bool,
    kb_ref: str = "",
    error_code: str = "",
    metadata: dict | None = None,
) -> None:
    """Write a bounded audit event without ever persisting the raw secret."""
    try:
        actor = auth.get_user_by_id(principal.user_id) if principal else None
    except Exception:  # noqa: BLE001 - audit remains fail-soft
        actor = None
    started_at = getattr(request.state, "started_at", None)
    payload = {
        "request_id": request_id(request),
        "public_key_id": principal.key_id if principal else "",
        "external_kb_ref": kb_ref,
        "error_code": error_code,
        "duration_ms": (
            max(0, int((time.monotonic() - started_at) * 1000))
            if started_at is not None
            else None
        ),
        "client_ip_hash": (
            hashlib.sha256(request.client.host.encode("utf-8")).hexdigest()[:16]
            if request.client is not None and request.client.host
            else ""
        ),
        **(metadata or {}),
    }
    auth._audit(  # noqa: SLF001 - central AuthService audit boundary
        action,
        actor=actor,
        target_type="external_api",
        target_id=principal.key_id if principal else "",
        kb_name=kb_ref,
        success=success,
        error_message=error_code,
        metadata=payload,
    )


class _LocalRateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def consume(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True


_LOCAL_LIMITER = _LocalRateLimiter()
_SEMAPHORE_LOCK = threading.Lock()
_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}


def _redis_increment(bucket: str, limit: int, ttl_seconds: int) -> bool | None:
    try:
        from src.core.redis_client import get_redis_client

        client = get_redis_client()
        if client is None:
            return None
        pipe = client.pipeline(transaction=True)
        pipe.incr(bucket)
        pipe.expire(bucket, ttl_seconds)
        count, _ = pipe.execute()
        return int(count) <= limit
    except Exception:  # noqa: BLE001 - unavailable Redis is handled by caller
        return None


def _redis_consume(key: str, limit: int) -> bool | None:
    bucket = f"hdb:external:rate:{key}:{int(time.time() // 60)}"
    return _redis_increment(bucket, limit, 120)


def _redis_consume_daily(key: str, limit: int) -> bool | None:
    day = time.strftime("%Y%m%d", time.gmtime())
    bucket = f"hdb:external:daily:{key}:{day}"
    return _redis_increment(bucket, limit, 172800)


def _rate_limit_backend_unavailable() -> ExternalApiError:
    return ExternalApiError(
        503,
        "RATE_LIMIT_BACKEND_UNAVAILABLE",
        "限流服务暂时不可用",
        retryable=True,
        retry_after=5,
    )


def enforce_external_rate_limit(principal: EmployeeApiKeyPrincipal) -> None:
    limit = int(external_settings.HDB_EXTERNAL_RATE_LIMIT_PER_MINUTE or 0)
    daily_quota = int(external_settings.HDB_EXTERNAL_DAILY_QUOTA or 0)
    if limit <= 0 and daily_quota <= 0:
        return
    backend = external_settings.HDB_EXTERNAL_RATE_LIMIT_BACKEND
    if backend == "redis":
        results: list[bool | None] = []
        if limit > 0:
            results.append(_redis_consume(f"key:{principal.key_id}", limit))
            results.append(_redis_consume(f"user:{principal.user_id}", limit))
        if daily_quota > 0:
            results.append(_redis_consume_daily(f"key:{principal.key_id}", daily_quota))
            results.append(_redis_consume_daily(f"user:{principal.user_id}", daily_quota))
        if any(result is False for result in results):
            raise ExternalApiError(
                429, "RATE_LIMITED", "请求频率超过限制", retryable=True, retry_after=60
            )
        if any(result is None for result in results):
            if bool(getattr(src.settings, "REDIS_REQUIRED", True)):
                raise _rate_limit_backend_unavailable()
            # Test/development isolation explicitly disables the Redis hard
            # dependency, so a local limiter is acceptable there.
            results = []
            if limit > 0:
                results.append(_LOCAL_LIMITER.consume(f"key:{principal.key_id}", limit))
                results.append(_LOCAL_LIMITER.consume(f"user:{principal.user_id}", limit))
            if daily_quota > 0:
                results.append(
                    _LOCAL_LIMITER.consume(
                        f"daily:key:{principal.key_id}", daily_quota, window_seconds=86400
                    )
                )
                results.append(
                    _LOCAL_LIMITER.consume(
                        f"daily:user:{principal.user_id}", daily_quota, window_seconds=86400
                    )
                )
    else:
        results = []
        if limit > 0:
            results.append(_LOCAL_LIMITER.consume(f"key:{principal.key_id}", limit))
            results.append(_LOCAL_LIMITER.consume(f"user:{principal.user_id}", limit))
        if daily_quota > 0:
            results.append(
                _LOCAL_LIMITER.consume(
                    f"daily:key:{principal.key_id}", daily_quota, window_seconds=86400
                )
            )
            results.append(
                _LOCAL_LIMITER.consume(
                    f"daily:user:{principal.user_id}", daily_quota, window_seconds=86400
                )
            )
    if not all(results):
        raise ExternalApiError(429, "RATE_LIMITED", "请求频率超过限制", retryable=True, retry_after=60)


@contextmanager
def external_concurrency(principal: EmployeeApiKeyPrincipal) -> Iterator[None]:
    limit = int(external_settings.HDB_EXTERNAL_CONCURRENCY_LIMIT or 0)
    if limit <= 0:
        yield
        return
    with _SEMAPHORE_LOCK:
        semaphore = _SEMAPHORES.get(principal.key_id)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(limit)
            _SEMAPHORES[principal.key_id] = semaphore
    if not semaphore.acquire(blocking=False):
        raise ExternalApiError(429, "RATE_LIMITED", "并发请求超过限制", retryable=True, retry_after=1)
    try:
        yield
    finally:
        semaphore.release()


def http_error_from_external(exc: ExternalApiError) -> HTTPException:
    """Compatibility helper for callers that still translate explicitly."""
    return HTTPException(status_code=exc.status_code, detail=exc.code)


__all__ = [
    "ExternalApiError",
    "ExternalAuthorization",
    "authorize_kb",
    "audit_external",
    "enforce_external_rate_limit",
    "external_concurrency",
    "require_external_principal",
    "request_id",
]
