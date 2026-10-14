"""Support tickets are visible to their requester and system administrators."""
import logging
from fastapi import APIRouter, Depends, Query

from src.api.deps import get_auth_service, require_dashboard_access, require_system_admin
from src.api.routes.quality import _call, _store
from src.core.auth import ROLE_SYSTEM_ADMIN
from src.quality.tickets import TicketService, TicketStatus, TicketUpdateRequest

router = APIRouter(tags=["feedback-tickets"])
logger = logging.getLogger(__name__)


def _audit(viewer, ticket_id, action):
    from src.core.app_logs import AppLogService
    AppLogService().record_audit("quality.ticket." + action, actor=viewer,
                                 target_type="quality_ticket", target_id=ticket_id)


def notify_ticket_created(ticket_id, users):
    from src.core.notifications import NotificationService

    notifications = NotificationService()
    for user in users:
        if not user.is_active or user.role != ROLE_SYSTEM_ADMIN:
            continue
        try:
            notifications.create(user_id=user.id, kind="quality_ticket_created", title="有新的反馈工单",
                target_type="quality_ticket", target_id=ticket_id,
                body="有新的员工反馈工单，请打开查看并认领处理。")
        except Exception as exc:
            logger.warning("Ticket intake notification failed: ticket=%s recipient=%s error=%s", ticket_id, user.id, type(exc).__name__)


def _notify(viewer, ticket, action=None):
    """Only generic status notifications leave the support material boundary."""
    from src.core.notifications import NotificationService
    targets = {ticket["owner_user_id"]} if viewer.role == ROLE_SYSTEM_ADMIN else {ticket["assignee_user_id"]}
    if action == "assign":
        targets.add(ticket["assignee_user_id"])
    for target in targets:
        if not target or target == viewer.id:
            continue
        try:
            NotificationService().create(user_id=target, kind="quality_ticket_updated",
                target_type="quality_ticket", target_id=ticket["ticket_id"], title="反馈工单有更新",
                body="工单有新的回复或状态变更，请打开查看。")
        except Exception:
            # The durable ticket/thread remains available even if delivery fails.
            pass


@router.get("/quality/ticket-assignees")
def assignees(viewer=Depends(require_system_admin), auth=Depends(get_auth_service)):
    return [{"id": user.id, "username": user.username} for user in auth.list_users_as(viewer)
            if user.is_active and user.role == ROLE_SYSTEM_ADMIN]


@router.get("/quality/tickets")
def tickets(status: TicketStatus | None = None, department_id: int | None = None,
            search: str = Query("", max_length=200), limit: int = Query(50, ge=1, le=100),
            offset: int = Query(0, ge=0), viewer=Depends(require_dashboard_access), store=Depends(_store)):
    return _call(TicketService(store).list, viewer, status=status, department_id=department_id,
                 search=search, limit=limit, offset=offset)


@router.get("/quality/tickets/{ticket_id}")
def ticket(ticket_id: str, viewer=Depends(require_dashboard_access), store=Depends(_store)):
    return _call(TicketService(store).get, ticket_id, viewer)


@router.post("/quality/tickets/{ticket_id}/actions")
def action(ticket_id: str, body: TicketUpdateRequest, viewer=Depends(require_dashboard_access), store=Depends(_store)):
    result = _call(TicketService(store).update, viewer, ticket_id, body)
    if not result.get("deduplicated"):
        _audit(viewer, ticket_id, body.action)
        _notify(viewer, result, action=body.action)
    return result


@router.delete("/quality/tickets/{ticket_id}")
def delete(ticket_id: str, row_version: int = Query(ge=1), viewer=Depends(require_system_admin), store=Depends(_store)):
    result = _call(TicketService(store).delete, viewer, ticket_id, row_version=row_version)
    _audit(viewer, ticket_id, "delete")
    return result
