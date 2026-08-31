"""Long-term memory governance HTTP boundary.

The route layer is intentionally thin.  ``MemoryService`` is the sole owner
of Catalog lookup, scope construction, ACL checks, expected-revision CAS,
consent validation, and projection/outbox semantics.  This module never
accepts a user id, department id, KB id, namespace, or Store key from the
client as an authorization input.

The service module is allowed to land independently of this API slice.  Until
it is importable, authenticated requests fail closed with HTTP 503 rather than
pretending that an authorization decision was made.
"""
from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import current_user
from src.api.schemas import (
    MemoryActionRequest,
    MemoryConsentCreateRequest,
    MemoryConsentListResponse,
    MemoryConsentView,
    MemoryDraftRequest,
    MemoryExtractionRequest,
    MemoryListResponse,
    MemoryListScope,
    MemoryOperationResponse,
    MemoryStatus,
    MemoryView,
    RevokeMemoryConsentRequest,
    SupersedeMemoryRequest,
    UserMemorySettingsRequest,
    UserMemorySettingsView,
    VerifyMemoryRequest,
)
from src.core.auth import AuthUser

try:  # The service is implemented in a separate workstream.
    from src.memory.service import MemoryService
except ImportError:  # pragma: no cover - exercised until the service lands
    MemoryService = None  # type: ignore[assignment,misc]


router = APIRouter(tags=["memories"])


def get_memory_service() -> Generator[Any, None, None]:
    """Dependency with a fail-closed boundary while the service is absent."""

    if MemoryService is None:
        raise HTTPException(
            status_code=503,
            detail="long-term memory service is not available",
        )
    service = MemoryService()
    try:
        yield service
    finally:
        service.close()


def _payload(value: Any) -> Any:
    """Make service dataclasses/dicts acceptable to the transport models."""

    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return vars(value)
    return value


def _call(service: Any, method: str, **kwargs: Any) -> Any:
    """Call the explicit service contract and translate only known failures."""

    operation: Callable[..., Any] | None = getattr(service, method, None)
    if operation is None:
        raise HTTPException(status_code=501, detail=f"MemoryService.{method} is not implemented")
    try:
        return operation(**kwargs)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc) or "memory access denied") from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc) or "memory not found") from exc
    except RuntimeError as exc:
        # Includes expected-revision/fence conflicts and other service-level
        # governance rejections; never turn a stale CAS into a 500.
        raise HTTPException(status_code=409, detail=str(exc) or "memory operation rejected") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc) or "memory operation rejected") from exc


def _list_response(value: Any, *, cursor: str | None = None, limit: int = 0) -> MemoryListResponse:
    data = _payload(value)
    if isinstance(data, list):
        items = data
        try:
            start = max(0, int(cursor or 0))
        except (TypeError, ValueError):
            start = 0
        data = {
            "items": items,
            "next_cursor": str(start + len(items)) if limit > 0 and len(items) >= limit else None,
        }
    if not isinstance(data, dict):
        data = {"items": []}
    return MemoryListResponse.model_validate(data)


def _operation_response(value: Any) -> MemoryOperationResponse:
    data = _payload(value)
    if isinstance(data, str):
        data = {"operation_id": data}
    if not isinstance(data, dict):
        data = {}
    return MemoryOperationResponse.model_validate(data)


@router.get("/memories", response_model=MemoryListResponse)
def list_memories(
    scope: MemoryListScope = Query(default="all"),
    status: MemoryStatus | None = Query(default=None),
    kb_name: str | None = Query(default=None, min_length=1, max_length=200),
    query: str = Query(default="", max_length=2_000),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
    user: AuthUser = Depends(current_user),
    service: Any = Depends(get_memory_service),
) -> MemoryListResponse:
    """List only Catalog records the service authorizes for this actor."""

    result = _call(
        service,
        "list_memories",
        actor=user,
        scope=scope,
        status=status,
        kb_name=kb_name,
        query=query,
        limit=limit,
        cursor=cursor,
    )
    return _list_response(result, cursor=cursor, limit=limit)


@router.get("/memories/{memory_id}", response_model=MemoryView)
def get_memory(
    memory_id: str,
    user: AuthUser = Depends(current_user),
    service: Any = Depends(get_memory_service),
) -> MemoryView:
    result = _call(service, "get_memory", actor=user, memory_id=memory_id)
    if result is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return MemoryView.model_validate(_payload(result))


@router.patch("/memories/{memory_id}/draft", response_model=MemoryOperationResponse, status_code=202)
def update_memory_draft(
    memory_id: str,
    body: MemoryDraftRequest,
    user: AuthUser = Depends(current_user),
    service: Any = Depends(get_memory_service),
) -> MemoryOperationResponse:
    result = _call(
        service,
        "update_draft",
        actor=user,
        memory_id=memory_id,
        content=body.content,
        expected_revision=body.expected_revision,
        reason=body.reason,
        request_id=body.request_id,
    )
    return _operation_response(result)


@router.post("/memories/{memory_id}/verify", response_model=MemoryOperationResponse, status_code=202)
def verify_memory(
    memory_id: str,
    body: VerifyMemoryRequest,
    user: AuthUser = Depends(current_user),
    service: Any = Depends(get_memory_service),
) -> MemoryOperationResponse:
    result = _call(
        service,
        "verify_memory",
        actor=user,
        memory_id=memory_id,
        expected_revision=body.expected_revision,
        reason=body.reason,
        request_id=body.request_id,
        evidence_refs=body.evidence_refs,
    )
    return _operation_response(result)


@router.post("/memories/{memory_id}/reject", response_model=MemoryOperationResponse, status_code=202)
def reject_memory(
    memory_id: str,
    body: MemoryActionRequest,
    user: AuthUser = Depends(current_user),
    service: Any = Depends(get_memory_service),
) -> MemoryOperationResponse:
    result = _call(
        service,
        "reject_memory",
        actor=user,
        memory_id=memory_id,
        expected_revision=body.expected_revision,
        reason=body.reason,
        request_id=body.request_id,
        evidence_refs=body.evidence_refs,
    )
    return _operation_response(result)


@router.post("/memories/{memory_id}/supersede", response_model=MemoryOperationResponse, status_code=202)
def supersede_memory(
    memory_id: str,
    body: SupersedeMemoryRequest,
    user: AuthUser = Depends(current_user),
    service: Any = Depends(get_memory_service),
) -> MemoryOperationResponse:
    result = _call(
        service,
        "supersede_memory",
        actor=user,
        memory_id=memory_id,
        expected_revision=body.expected_revision,
        reason=body.reason,
        request_id=body.request_id,
        evidence_refs=body.evidence_refs,
        successor_memory_id=body.successor_memory_id,
    )
    return _operation_response(result)


@router.delete("/memories/{memory_id}", response_model=MemoryOperationResponse, status_code=202)
def delete_memory(
    memory_id: str,
    body: MemoryActionRequest,
    user: AuthUser = Depends(current_user),
    service: Any = Depends(get_memory_service),
) -> MemoryOperationResponse:
    result = _call(
        service,
        "delete_memory",
        actor=user,
        memory_id=memory_id,
        expected_revision=body.expected_revision,
        reason=body.reason,
        request_id=body.request_id,
        evidence_refs=body.evidence_refs,
    )
    return _operation_response(result)


@router.post("/conversations/{session_id}/extract-memory", response_model=MemoryOperationResponse, status_code=202)
def extract_memory(
    session_id: int,
    body: MemoryExtractionRequest,
    user: AuthUser = Depends(current_user),
    service: Any = Depends(get_memory_service),
) -> MemoryOperationResponse:
    """Queue an explicit project reflection for a completed conversation."""

    result = _call(
        service,
        "extract_memory",
        actor=user,
        session_id=session_id,
        reason=body.reason,
        request_id=body.request_id,
    )
    return _operation_response(result)


@router.put("/memory-settings", response_model=UserMemorySettingsView)
def update_user_memory_settings(
    body: UserMemorySettingsRequest,
    user: AuthUser = Depends(current_user),
    service: Any = Depends(get_memory_service),
) -> UserMemorySettingsView:
    """Change only the authenticated user's opt-in state."""

    result = _call(
        service,
        "set_user_memory_settings",
        actor=user,
        opt_in=body.opt_in,
        reason=body.reason,
        request_id=body.request_id,
    )
    return UserMemorySettingsView.model_validate(_payload(result))


@router.get("/memory-settings", response_model=UserMemorySettingsView)
def get_user_memory_settings(
    user: AuthUser = Depends(current_user),
    service: Any = Depends(get_memory_service),
) -> UserMemorySettingsView:
    result = _call(service, "get_user_settings", actor=user)
    return UserMemorySettingsView.model_validate(_payload(result))


@router.post("/conversations/{session_id}/memory-consents", response_model=MemoryConsentView, status_code=202)
def create_memory_consent(
    session_id: int,
    body: MemoryConsentCreateRequest,
    user: AuthUser = Depends(current_user),
    service: Any = Depends(get_memory_service),
) -> MemoryConsentView:
    """Create a server-bound immutable source manifest for the current user."""

    result = _call(
        service,
        "create_memory_consent",
        actor=user,
        session_id=session_id,
        message_ids=body.message_ids,
        reason=body.reason,
        request_id=body.request_id,
    )
    return MemoryConsentView.model_validate(_payload(result))


@router.get("/memory-consents", response_model=MemoryConsentListResponse)
def list_memory_consents(
    session_id: int | None = None,
    user: AuthUser = Depends(current_user),
    service: Any = Depends(get_memory_service),
) -> MemoryConsentListResponse:
    """List only consent events owned by the authenticated user."""

    result = _call(service, "list_memory_consents", actor=user, session_id=session_id)
    data = _payload(result)
    if isinstance(data, list):
        data = {"items": data}
    return MemoryConsentListResponse.model_validate(data if isinstance(data, dict) else {"items": []})


@router.delete("/memory-consents/{consent_event_id}", response_model=MemoryOperationResponse, status_code=202)
def revoke_memory_consent(
    consent_event_id: str,
    body: RevokeMemoryConsentRequest,
    user: AuthUser = Depends(current_user),
    service: Any = Depends(get_memory_service),
) -> MemoryOperationResponse:
    result = _call(
        service,
        "revoke_memory_consent",
        actor=user,
        consent_event_id=consent_event_id,
        reason=body.reason,
        request_id=body.request_id,
    )
    return _operation_response(result)
