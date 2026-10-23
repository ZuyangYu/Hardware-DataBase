"""Read-only access to frozen whole-document comparison reports."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

import src.settings
from src.api.deps import current_user
from src.core.auth import AuthUser
from src.services.document_comparison import ComparisonError
from src.storage.document_comparison_store import DocumentComparisonStore

router = APIRouter(tags=["comparisons"])


def _require_enabled() -> None:
    if not src.settings.DOCUMENT_COMPARISON_ENABLED:
        raise HTTPException(status_code=404, detail="document comparison is not enabled")


def _map_error(exc: ComparisonError) -> HTTPException:
    if exc.error_code in {"comparison_report_not_found"}:
        return HTTPException(status_code=404, detail="comparison report not found")
    if exc.error_code in {"comparison_report_denied", "comparison_report_tampered"}:
        return HTTPException(status_code=404, detail="comparison report not found")
    if exc.error_code == "comparison_report_conflict":
        return HTTPException(status_code=409, detail="comparison report conflict")
    return HTTPException(status_code=400, detail=exc.error_code)


@router.get("/comparisons/{report_id}")
def get_comparison(report_id: str, user: AuthUser = Depends(current_user)):
    """Return one immutable report owned by the caller."""

    _require_enabled()
    try:
        return DocumentComparisonStore().get(report_id, owner_user_id=int(user.id))
    except ComparisonError as exc:
        raise _map_error(exc) from exc


@router.get("/comparisons/{report_id}/sections/{section_key}")
def get_comparison_section(
    report_id: str,
    section_key: str,
    user: AuthUser = Depends(current_user),
):
    """Return only the changed items for one section key."""

    _require_enabled()
    try:
        report = DocumentComparisonStore().get(report_id, owner_user_id=int(user.id))
    except ComparisonError as exc:
        raise _map_error(exc) from exc
    items = [
        item for item in report.get("items", [])
        if str(item.get("section_key") or "") == str(section_key)
        or str(item.get("left_section_id") or "") == str(section_key)
        or str(item.get("right_section_id") or "") == str(section_key)
    ]
    if not items:
        raise HTTPException(status_code=404, detail="section not found in comparison report")
    return {
        "report_id": report_id,
        "section_key": section_key,
        "items": items,
        "immutable_hash": report.get("immutable_hash"),
    }
