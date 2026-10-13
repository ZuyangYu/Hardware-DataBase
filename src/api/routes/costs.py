"""Scoped call accounting, exact currency totals and matching CSV export."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from src.api.deps import require_dashboard_access, require_system_admin
from src.core.auth import AuthUser
from src.costs.pricing import PriceVersion
from src.costs.budgets import BudgetVersion
from src.costs.store import CostConflict, CostStore

router = APIRouter(tags=["costs"])


def _store():
    return CostStore()


def _filters(start: datetime | None = None, end: datetime | None = None, purpose: str | None = Query(None, max_length=80),
             model_alias: str | None = Query(None, max_length=80), endpoint_alias: str | None = Query(None, max_length=80),
             department_id: int | None = Query(None, ge=1), project_id: str | None = Query(None, max_length=128)):
    def stamp(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            raise HTTPException(422, "time window requires timezone")
        return dt.astimezone(timezone.utc).isoformat()
    start, end = stamp(start), stamp(end)
    if start and end and start >= end:
        raise HTTPException(422, "time window must be nonempty")
    return {"start": start, "end": end, "purpose": purpose, "model_alias": model_alias, "endpoint_alias": endpoint_alias,
            "department_id": department_id, "project_id": project_id}


@router.get("/costs/summary")
def summary(user: AuthUser = Depends(require_dashboard_access), store: CostStore = Depends(_store), filters: dict = Depends(_filters)):
    return store.summary(user, **filters)


@router.get("/costs/invocations")
def invocations(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), user: AuthUser = Depends(require_dashboard_access), store: CostStore = Depends(_store), filters: dict = Depends(_filters)):
    return store.invocations(user, limit=limit, offset=offset, **filters)


@router.get("/costs/trends")
def trends(bucket: str = Query("day", pattern="^(day|hour)$"), user: AuthUser = Depends(require_dashboard_access), store: CostStore = Depends(_store), filters: dict = Depends(_filters)):
    try:
        return store.trends(user, bucket=bucket, **filters)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/costs/tasks/{task_type}/{task_id}")
def task(task_type: str, task_id: str, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), user: AuthUser = Depends(require_dashboard_access), store: CostStore = Depends(_store)):
    filters = {"task_type": task_type, "task_id": task_id}
    result = store.summary(user, **filters)
    if not result["calls"]:
        raise HTTPException(404, "no visible invocation for this task")
    return {"summary": result, "invocations": store.invocations(user, limit=limit, offset=offset, **filters), "limit": limit, "offset": offset}


@router.post("/costs/prices")
def register_price(body: PriceVersion, user: AuthUser = Depends(require_system_admin), store: CostStore = Depends(_store)):
    try:
        store.add_price(body)
    except CostConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    from src.core.app_logs import AppLogService

    AppLogService().record_audit("cost.price_register", actor=user, target_type="price_version", target_id=body.price_version)
    return {"price_version": body.price_version}


@router.get("/costs/export")
def export(user: AuthUser = Depends(require_dashboard_access), store: CostStore = Depends(_store), filters: dict = Depends(_filters)):
    fields = ["invocation_id", "task_type", "task_id", "purpose", "endpoint_alias", "requested_model", "response_model",
              "started_at", "status", "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens",
              "usage_status", "cost_status", "currency", "cost_amount", "price_version", "attempt_visibility"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    # Bounded export, explicitly fail rather than silently truncate a large set.
    rows = store.invocations(user, limit=10001, **filters)
    if len(rows) > 10000:
        raise HTTPException(422, "narrow the time window; export supports at most 10000 calls")
    for row in rows:
        writer.writerow({k: "'" + str(v) if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v for k, v in row.items()})
    return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="llm-costs.csv"'})


@router.post("/costs/budgets")
def register_budget(body: BudgetVersion, user: AuthUser = Depends(require_system_admin), store: CostStore = Depends(_store)):
    try:
        store.add_budget(user, body)
    except CostConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    from src.core.app_logs import AppLogService

    AppLogService().record_audit("cost.budget_register", actor=user, target_type="budget_version", target_id=body.budget_version)
    return {"budget_version": body.budget_version}


@router.get("/costs/budgets")
def budgets(user: AuthUser = Depends(require_dashboard_access), store: CostStore = Depends(_store)):
    try:
        return store.budget_statuses(user)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
