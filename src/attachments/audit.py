"""Central audit helper for attachment mutations.

The HTTP boundary has an authenticated :class:`AuthUser`; tool and
deterministic runner paths only carry the numeric attachment identity.  This
helper resolves the actor when possible and writes one fail-soft row to the
shared ``audit_events`` trail, so candidate creation and chat-driven release
requests are covered as well as the REST routes.

Audit metadata is append-only and widely readable: callers must never pass
file bytes, replacement text, or user comments here.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def record_attachment_audit(
    *,
    action: str,
    target_type: str,
    target_id: str,
    actor: Any | None = None,
    actor_user_id: int | None = None,
    kb_name: str = "",
    success: bool = True,
    error_message: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append one attachment audit event; never raise to the caller."""

    resolved_actor = actor
    numeric_actor = int(actor_user_id) if actor_user_id else None
    if resolved_actor is None and numeric_actor:
        try:
            from src.core.auth import AuthService

            resolved_actor = AuthService().get_user_by_id(numeric_actor)
        except Exception:  # noqa: BLE001 - identity lookup is best effort
            resolved_actor = None

    payload = dict(metadata or {})
    if resolved_actor is None and numeric_actor:
        # Preserve traceability even when the auth row is unavailable (for
        # example a deleted user during retention cleanup).
        payload.setdefault("actor_user_id", numeric_actor)

    try:
        from src.core.app_logs import AppLogService

        AppLogService().record_audit(
            action=action,
            actor=resolved_actor,
            target_type=target_type,
            target_id=str(target_id or "")[:200],
            kb_name=str(kb_name or "")[:200],
            success=success,
            error_message=str(error_message or "")[:500],
            metadata=payload,
        )
    except Exception:  # noqa: BLE001 - audit must not fail the operation
        logger.exception("failed to append attachment audit event: %s", action)


@dataclass(frozen=True)
class AttachmentAuditAttempt:
    """Correlation handle for a write-ahead audit pair.

    ``requested`` is written before the mutation; ``finished`` is written
    afterwards with the real outcome.  A crash can therefore leave a visible
    request without a completion row, but can never make the mutation
    completely invisible.
    """

    action: str
    attempt_id: str
    target_type: str
    target_id: str
    kb_name: str = ""
    actor: Any | None = None
    actor_user_id: int | None = None


def begin_attachment_audit(
    *,
    action: str,
    target_type: str,
    target_id: str,
    actor: Any | None = None,
    actor_user_id: int | None = None,
    kb_name: str = "",
    metadata: dict[str, Any] | None = None,
) -> AttachmentAuditAttempt:
    """Write the pre-mutation ``*.requested`` row and return its handle."""

    attempt = AttachmentAuditAttempt(
        action=action,
        attempt_id=uuid.uuid4().hex,
        target_type=target_type,
        target_id=target_id,
        kb_name=kb_name,
        actor=actor,
        actor_user_id=actor_user_id,
    )
    record_attachment_audit(
        action=f"{action}.requested",
        target_type=target_type,
        target_id=target_id,
        actor=actor,
        actor_user_id=actor_user_id,
        kb_name=kb_name,
        metadata={
            **(metadata or {}),
            "attempt_id": attempt.attempt_id,
            "phase": "requested",
        },
    )
    return attempt


def finish_attachment_audit(
    attempt: AttachmentAuditAttempt,
    *,
    success: bool,
    error_message: str = "",
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write the post-mutation outcome row for one attempt."""

    record_attachment_audit(
        action=attempt.action,
        target_type=target_type or attempt.target_type,
        target_id=target_id or attempt.target_id,
        actor=attempt.actor,
        actor_user_id=attempt.actor_user_id,
        kb_name=attempt.kb_name,
        success=success,
        error_message=error_message,
        metadata={
            **(metadata or {}),
            "attempt_id": attempt.attempt_id,
            "phase": "finished",
        },
    )
