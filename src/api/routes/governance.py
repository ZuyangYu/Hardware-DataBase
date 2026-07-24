"""Governance endpoints (system_admin: global, dept_admin: department-scoped).

Combines ``AppPipeline.governance_stats`` (per-KB document counts) with
``AuthService.list_knowledge_base_summaries`` (KB registration + ownership).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline
from src.api.schemas import KbSummaryView

router = APIRouter(tags=["governance"])


@router.get("/governance/stats")
def governance_stats(
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
) -> dict:
    """Per-KB document statistics.

    - system_admin: keyed by stable KB identity across all departments
    - anyone else: keyed by KB name, scoped to their department
    """
    ctx = build_context_for_user(user, auth=auth)
    return pipeline.governance_stats(ctx=ctx)


@router.get("/governance/kb-summaries", response_model=list[KbSummaryView])
def kb_summaries(
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """KB summaries cross-referenced against physically-existing KBs."""
    ctx = build_context_for_user(user, auth=auth)
    if ctx.is_system_admin():
        existing = pipeline.list_all_knowledge_bases_for_admin(ctx=ctx)
    else:
        existing = pipeline.list_knowledge_bases(ctx=ctx)
    summaries = auth.list_knowledge_base_summaries(existing)
    return [
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
        )
        for s in summaries
    ]