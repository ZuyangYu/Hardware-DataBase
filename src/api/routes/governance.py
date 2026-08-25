"""Governance endpoints (system_admin: global, dept_admin: department-scoped).

Combines ``AppPipeline.governance_stats`` (per-KB document counts) with
``AuthService.list_knowledge_base_summaries`` (KB registration + ownership).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser

from src.api.context import build_context_for_user
from src.api.deps import get_auth_service, get_pipeline, require_any_admin
from src.api.schemas import GovernanceStatsResponse, KbStatsEntry, KbSummaryView

router = APIRouter(tags=["governance"])


@router.get("/governance/stats", response_model=GovernanceStatsResponse)
def governance_stats(
    user: AuthUser = Depends(require_any_admin),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
) -> GovernanceStatsResponse:
    """Per-KB document statistics.

    - system_admin: keyed by stable KB identity across all departments
    - dept_admin: keyed by KB name, scoped to their department
    - plain users: rejected (mirrors the former Streamlit governance tab, now the React admin GovernancePage)
    """
    ctx = build_context_for_user(user, auth=auth)
    raw = pipeline.governance_stats(ctx=ctx) or {}
    stats = {
        key: KbStatsEntry(
            files=int(v.get("files", 0) or 0),
            failed=int(v.get("failed", 0) or 0),
            parsing=int(v.get("parsing", 0) or 0),
        )
        for key, v in raw.items()
        if isinstance(v, dict)
    }
    return GovernanceStatsResponse(stats=stats)


def _kb_identity_stats_key(kb_id: int | str | None, department_id: int | str | None, kb_name: str) -> str:
    """Same key governance_stats uses to bucket per-KB document counts, so we
    can join stats onto summaries. Mirrors the former streamlit_app._kb_identity_stats_key."""
    kb_id_value = int(kb_id or 0) if str(kb_id or "").isdigit() else 0
    if kb_id_value:
        return f"kb_id:{kb_id_value}"
    return f"department:{department_id or ''}:kb:{kb_name or ''}"


def _issue_flags(s, stats: dict) -> list[str]:
    """Replicate the former Streamlit governance panel's 5 anomaly checks (now the React admin GovernancePage)."""
    failed = int(stats.get("failed", 0) or 0)
    flags: list[str] = []
    if not s.registered:
        flags.append("未登记")
    if not s.department_id:
        flags.append("未分配部门")
    if s.department_id and s.dept_admin_count == 0:
        flags.append("无部门管理员")
    if failed:
        flags.append(f"解析失败 {failed}")
    if s.permission_count == 0:
        flags.append("未授权")
    return flags


@router.get("/governance/kb-summaries", response_model=list[KbSummaryView])
def kb_summaries(
    user: AuthUser = Depends(require_any_admin),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """KB summaries cross-referenced against physically-existing KBs, with
    per-KB document counts and anomaly flags joined in so the frontend can
    render the full governance panel from this one endpoint."""
    ctx = build_context_for_user(user, auth=auth)
    if ctx.is_system_admin():
        existing = pipeline.list_all_knowledge_bases_for_admin(ctx=ctx)
    else:
        existing = pipeline.list_knowledge_bases(ctx=ctx)
    summaries = auth.list_knowledge_base_summaries(existing)
    stats_by_key = pipeline.governance_stats(ctx=ctx) or {}
    views: list[KbSummaryView] = []
    for s in summaries:
        key = _kb_identity_stats_key(s.kb_id, s.department_id, s.name)
        stats = stats_by_key.get(key, {}) or {}
        views.append(
            KbSummaryView(
                name=s.name,
                kb_id=s.kb_id,
                department_id=s.department_id,
                department_name=s.department_name,
                owner_user_id=s.owner_user_id,
                owner_username=s.owner_username,
                permission_count=s.permission_count,
                dept_admin_count=s.dept_admin_count,
                registered=s.registered,
                physical_exists=s.physical_exists,
                created_at=s.created_at,
                files=int(stats.get("files", 0) or 0),
                failed=int(stats.get("failed", 0) or 0),
                parsing=int(stats.get("parsing", 0) or 0),
                issue_flags=_issue_flags(s, stats),
            )
        )
    return views