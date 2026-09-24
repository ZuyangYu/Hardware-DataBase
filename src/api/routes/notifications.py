"""In-app notification endpoints (per authenticated user)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import current_user
from src.api.schemas import OkResponse
from src.core.auth import AuthUser
from src.core.notifications import NotificationService

router = APIRouter(tags=["notifications"])


def _service() -> NotificationService:
    return NotificationService()


@router.get("/notifications")
def list_notifications(
    unread_only: bool = False,
    limit: int = 50,
    user: AuthUser = Depends(current_user),
):
    service = _service()
    items = service.list_for_user(
        user_id=user.id, unread_only=unread_only, limit=limit
    )
    return {
        "items": [item.to_dict() for item in items],
        "unread_count": service.unread_count(user_id=user.id),
    }


@router.post("/notifications/{notification_id}/read", response_model=OkResponse)
def mark_notification_read(
    notification_id: int,
    user: AuthUser = Depends(current_user),
):
    if not _service().mark_read(
        user_id=user.id, notification_id=notification_id
    ):
        raise HTTPException(
            status_code=404, detail="notification not found or already read"
        )
    return OkResponse(ok=True)


@router.post("/notifications/read-all", response_model=OkResponse)
def mark_all_notifications_read(user: AuthUser = Depends(current_user)):
    changed = _service().mark_all_read(user_id=user.id)
    return OkResponse(ok=True, message=str(changed))
