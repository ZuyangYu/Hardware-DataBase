"""Log query endpoints.

Both audit and query-trace logs use the same viewer-scoping enforced inside
:class:`AppLogService` — ``system_admin`` sees everything, ``dept_admin``
sees their department only, ordinary users don't reach this router (guarded
by :func:`require_any_admin`).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from src.core.app_logs import AppLogService
from src.core.auth import AuthUser

from src.api.deps import require_any_admin
from src.api.schemas import (
    AuditEventView,
    AuditStatsResponse,
    EvidenceView,
    QueryStatsResponse,
    QueryTraceView,
)

router = APIRouter(tags=["logs"])


def _log_service() -> AppLogService:
    return AppLogService()


def _audit_view(e) -> AuditEventView:
    return AuditEventView(
        id=e.id,
        actor_username=e.actor_username,
        actor_role=e.actor_role,
        department_id=e.department_id,
        action=e.action,
        target_type=e.target_type,
        target_id=e.target_id,
        kb_name=e.kb_name,
        success=bool(e.success),
        error_message=e.error_message,
        metadata_json=e.metadata_json,
        created_at=e.created_at,
    )


def _trace_view(t) -> QueryTraceView:
    return QueryTraceView(
        id=t.id,
        username=t.username,
        department_id=t.department_id,
        chat_session_id=t.chat_session_id,
        kb_name=t.kb_name,
        original_query=t.original_query,
        rewritten_query=t.rewritten_query,
        backend=t.backend,
        retriever_type=t.retriever_type,
        final_top_k=t.final_top_k,
        latency_ms=t.latency_ms,
        status=t.status,
        error_message=t.error_message,
        metadata_json=t.metadata_json,
        created_at=t.created_at,
    )


def _evidence_view(e) -> EvidenceView:
    return EvidenceView(
        id=e.id,
        trace_id=e.trace_id,
        rank=e.rank,
        file_name=e.file_name,
        document_id=e.document_id,
        chunk_id=e.chunk_id,
        vector_score=e.vector_score,
        bm25_score=e.bm25_score,
        rrf_score=e.rrf_score,
        rerank_score=e.rerank_score,
        text_preview=e.text_preview,
        metadata_json=e.metadata_json,
        created_at=e.created_at,
    )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@router.get("/logs/audit", response_model=list[AuditEventView])
def list_audit(
    action: str | None = None,
    kb_name: str | None = None,
    success: bool | None = None,
    keyword: str | None = None,
    limit: int = 300,
    viewer: AuthUser = Depends(require_any_admin),
    logs: AppLogService = Depends(_log_service),
):
    """Return audit events matching the filters (scoped to viewer)."""
    events = logs.list_audit_events(viewer, action=action, kb_name=kb_name, success=success, keyword=keyword, limit=limit)
    return [_audit_view(e) for e in events]


@router.get("/logs/audit/stats", response_model=AuditStatsResponse)
def audit_stats(
    action: str | None = None,
    kb_name: str | None = None,
    success: bool | None = None,
    keyword: str | None = None,
    viewer: AuthUser = Depends(require_any_admin),
    logs: AppLogService = Depends(_log_service),
):
    """Audit totals, success/failure breakdown, top actions, and 7-day trend."""
    total = logs.count_audit_events(viewer, action=action, kb_name=kb_name, success=success, keyword=keyword)
    breakdown = logs.audit_breakdown(viewer, action=action, kb_name=kb_name, success=success, keyword=keyword)
    actions = logs.audit_action_breakdown(viewer, kb_name=kb_name, success=success, keyword=keyword)
    daily = logs.audit_recent_daily(viewer, days=7)
    return AuditStatsResponse(
        total=total,
        breakdown=breakdown,
        actions=[list(a) for a in actions],
        daily=[list(d) for d in daily],
    )


@router.get("/logs/audit/actions", response_model=list[str])
def list_audit_actions(
    viewer: AuthUser = Depends(require_any_admin),
    logs: AppLogService = Depends(_log_service),
):
    """List distinct audit action names visible to this viewer (for filters)."""
    return logs.list_audit_actions(viewer)


# ---------------------------------------------------------------------------
# Query traces
# ---------------------------------------------------------------------------

@router.get("/logs/query", response_model=list[QueryTraceView])
def list_query_traces(
    kb_name: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    limit: int = 300,
    viewer: AuthUser = Depends(require_any_admin),
    logs: AppLogService = Depends(_log_service),
):
    """Return query traces matching the filters (scoped + redacted per viewer)."""
    traces = logs.list_query_traces(viewer, kb_name=kb_name, status=status, keyword=keyword, limit=limit)
    return [_trace_view(t) for t in traces]


@router.get("/logs/query/stats", response_model=QueryStatsResponse)
def query_stats(
    kb_name: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    viewer: AuthUser = Depends(require_any_admin),
    logs: AppLogService = Depends(_log_service),
):
    """Query totals, per-status breakdown, and top failure reasons."""
    total = logs.count_query_traces(viewer, kb_name=kb_name, status=status, keyword=keyword)
    breakdown = logs.query_status_breakdown(viewer, kb_name=kb_name, status=status, keyword=keyword)
    failures = logs.query_failure_top(viewer, kb_name=kb_name, keyword=keyword)
    return QueryStatsResponse(
        total=total,
        breakdown=breakdown,
        failures=[list(f) for f in failures],
    )


@router.get("/logs/query/{trace_id}/evidence", response_model=list[EvidenceView])
def get_trace_evidence(
    trace_id: int,
    viewer: AuthUser = Depends(require_any_admin),
    logs: AppLogService = Depends(_log_service),
):
    """Return retrieved evidence for a query trace.

    Returns an empty list if the trace exists but no evidence was recorded
    (e.g. a small-talk short-circuit) *or* if the trace is not visible to
    the viewer. The service layer already redacts per-viewer.
    """
    evidence = logs.list_evidence(viewer, trace_id)
    return [_evidence_view(e) for e in evidence]