"""Production feedback, scoped queue and explicitly shared review copies."""
from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, HTTPException, Query

import src.settings as settings
from src.api.deps import get_auth_service, require_dashboard_access, require_employee, require_system_admin
from src.core.auth import AuthService, AuthUser
from src.quality.schemas import CaseVersionRequest, FeedbackRequest, ReviewRequest, ReviewRequestCreate, ReviewRequestReject, ShareRequest
from src.quality.service import QualityService
from src.quality.store import QualityConflict, QualityStore

router = APIRouter(tags=["quality"])


def _store() -> QualityStore:
    if not settings.QUALITY_ENABLED:
        raise HTTPException(503, "quality feedback is disabled")
    if settings.CONTROL_PLANE_BACKEND == "sqlite":
        from src.core.conversation import ConversationService

        ConversationService()
    return QualityStore()


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'")) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except QualityConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _audit(action: str, user: AuthUser, case_id: str):
    from src.core.app_logs import AppLogService

    AppLogService().record_audit(action, actor=user, target_type="quality_case", target_id=case_id)


@router.get("/chat/turns/{turn_id}/feedback")
def feedback_state(turn_id: str, user: AuthUser = Depends(require_employee), store: QualityStore = Depends(_store)):
    return _call(store.feedback_state, user.id, turn_id)


@router.post("/chat/turns/{turn_id}/feedback")
def submit_feedback(turn_id: str, body: FeedbackRequest, user: AuthUser = Depends(require_employee), store: QualityStore = Depends(_store), auth: AuthService = Depends(get_auth_service)):
    result = _call(store.submit_feedback, user, turn_id, body)
    if result.get("ticket_created"):
        from src.api.routes.quality_tickets import notify_ticket_created

        try:
            notify_ticket_created(result["ticket_id"], auth.list_users())
        except Exception as exc:
            # Notification delivery must never undo a committed support request.
            logging.getLogger(__name__).warning("Ticket intake notification unavailable: ticket=%s error=%s", result["ticket_id"], type(exc).__name__)
    return result


@router.get("/quality/reviewers")
def reviewers(user: AuthUser = Depends(require_employee), store: QualityStore = Depends(_store), auth: AuthService = Depends(get_auth_service)):
    return QualityService(store, auth).reviewers(user)


@router.get("/quality/cases")
def cases(status: str | None = None, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), user: AuthUser = Depends(require_dashboard_access), store: QualityStore = Depends(_store)):
    return store.list_cases(user, status=status, limit=limit, offset=offset)


@router.get("/quality/cases/{case_id}")
def case_detail(case_id: str, user: AuthUser = Depends(require_dashboard_access), store: QualityStore = Depends(_store)):
    return _call(store.get_case, case_id, user)


@router.post("/quality/cases/{case_id}/share")
def share(case_id: str, body: ShareRequest, user: AuthUser = Depends(require_employee), store: QualityStore = Depends(_store), auth: AuthService = Depends(get_auth_service)):
    result = _call(QualityService(store, auth).share, user, case_id, body)
    _audit("quality.share", user, case_id)
    return result


@router.post("/quality/cases/{case_id}/share/revoke")
def revoke_share(case_id: str, body: CaseVersionRequest, user: AuthUser = Depends(require_employee), store: QualityStore = Depends(_store)):
    result = _call(store.change_share, user, case_id, row_version=body.row_version)
    _audit("quality.share_revoke", user, case_id)
    return result


@router.post("/quality/cases/{case_id}/review-requests")
def create_review_request(case_id: str, body: ReviewRequestCreate, user: AuthUser = Depends(require_system_admin), store: QualityStore = Depends(_store)):
    result = _call(store.create_review_request, user, case_id, body)
    if result["created"]:
        from src.core.notifications import NotificationService

        try:
            NotificationService().create(
                user_id=result["owner_user_id"], kind="quality_review_request",
                title="有新的质量案例复核请求",
                body="管理员请求你确认共享固定提问、回答或证据；请打开质量工作台处理。",
                target_type="quality_case", target_id=case_id,
                payload={"request_id": result["request_id"], "case_id": case_id},
            )
        except Exception:  # notification delivery must not undo the durable request
            pass
        _audit("quality.review_request", user, case_id)
    return {key: value for key, value in result.items() if key != "owner_user_id"}


@router.post("/quality/cases/{case_id}/review-requests/{request_id}/approve")
def approve_review_request(case_id: str, request_id: str, body: ShareRequest, user: AuthUser = Depends(require_employee), store: QualityStore = Depends(_store), auth: AuthService = Depends(get_auth_service)):
    result = _call(QualityService(store, auth).approve_review_request, user, case_id, request_id, body)
    from src.core.notifications import NotificationService

    for reviewer_id in result.get("share", {}).get("reviewer_user_ids", []):
        try:
            NotificationService().create(
                user_id=reviewer_id, kind="quality_review_available",
                title="质量案例已授权复核",
                body="原回答用户已共享固定复核材料，请打开质量工作台提交复核结论。",
                target_type="quality_case", target_id=case_id,
                payload={"case_id": case_id, "request_id": request_id},
            )
        except Exception:  # notification delivery must not undo the durable share
            pass
    _audit("quality.review_request_approve", user, case_id)
    return result


@router.post("/quality/cases/{case_id}/review-requests/{request_id}/reject")
def reject_review_request(case_id: str, request_id: str, body: ReviewRequestReject, user: AuthUser = Depends(require_employee), store: QualityStore = Depends(_store)):
    result = _call(store.reject_review_request, user, case_id, request_id, body)
    _audit("quality.review_request_reject", user, case_id)
    return result


@router.post("/quality/cases/{case_id}/reviews")
def review(case_id: str, body: ReviewRequest, user: AuthUser = Depends(require_dashboard_access), store: QualityStore = Depends(_store)):
    result = _call(store.submit_review, user, case_id, body)
    if not result["deduplicated"]:
        _audit("quality.review", user, case_id)
    return result


@router.post("/quality/backfill")
def backfill(limit: int = Query(100, ge=1, le=1000), user: AuthUser = Depends(require_system_admin), store: QualityStore = Depends(_store)):
    count = store.backfill(limit=limit)
    _audit("quality.backfill", user, "")
    return {"captured": count}
