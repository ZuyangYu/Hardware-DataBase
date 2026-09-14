"""Resolve ``waiting_for_attachments`` turns when assets settle (design §12).

The chat worker keeps consuming only ``pending`` turns. This coordinator is
invoked by the attachment worker after every asset settles and performs the
atomic transition:

    waiting_for_attachments -> pending    all required assets ready/degraded
    waiting_for_attachments -> failed     any required asset failed

It also emits durable turn events so SSE subscribers see the wait end.
"""

from __future__ import annotations

from contextlib import closing
from typing import Any

from src.attachments.models import (
    PARSE_STATUS_FAILED,
    PARSE_STATUS_QUEUED,
    PARSE_STATUS_READY,
    PARSE_STATUS_RUNNING,
)
from src.attachments.store import AttachmentStore
from src.observability.metrics import record_attachment


class AttachmentTurnCoordinator:
    def __init__(self, store: AttachmentStore | None = None):
        self.store = store or AttachmentStore()

    def notify_asset_settled(self, asset_id: str) -> int:
        """Resolve every waiting turn that references the given asset."""
        asset = self.store.get_asset(asset_id)
        if asset is None:
            return 0
        attachment_ids = self._attachment_ids_for_asset(asset_id)
        if not attachment_ids:
            return 0
        resolved = 0
        for turn_id in self._waiting_turn_ids(attachment_ids):
            if self._resolve_turn(turn_id):
                resolved += 1
        return resolved

    def notify_attachment_deleted(self, attachment_id: str) -> int:
        """Fail waiting turns whose required attachment was deleted.

        A deleted attachment no longer produces an asset-settled callback (the
        last asset reference may be reclaimed immediately), so deletion has to
        wake its own waiters synchronously.  The turn remains in the durable
        state machine and its frozen history snapshot is left untouched.
        """
        attachment_id = str(attachment_id or "").strip()
        if not attachment_id:
            return 0
        resolved = 0
        for turn_id in self._waiting_turn_ids([attachment_id]):
            if self._resolve_turn(turn_id):
                resolved += 1
        return resolved

    def reconcile_waiting_turns(self, limit: int = 100) -> int:
        """Resolve waiting turns whose settlement event was missed.

        The attachment worker normally wakes a turn from an asset-settled
        callback.  A worker restart or a fail-soft callback can lose that
        notification, so a bounded scan is also run from the durable queue
        state.  ``_resolve_turn`` keeps the transition conditional on the
        current waiting status, making concurrent event and recovery paths
        safe and idempotent.
        """
        resolved = 0
        for turn_id in self._all_waiting_turn_ids(limit=limit):
            if self._resolve_turn(turn_id):
                resolved += 1
        return resolved

    # -- conversation-db helpers (lazy import keeps modules decoupled) ------

    def _conv(self):
        from src.core.conversation import ConversationService

        return ConversationService()

    def _attachment_ids_for_asset(self, asset_id: str) -> list[str]:
        with closing(self.store._connect()) as conn:
            rows = conn.execute(
                "SELECT attachment_id FROM chat_attachments WHERE asset_id = ?",
                (asset_id,),
            ).fetchall()
        return [row["attachment_id"] for row in rows]

    def _waiting_turn_ids(self, attachment_ids: list[str]) -> list[str]:
        conv = self._conv()
        placeholders = ",".join("?" for _ in attachment_ids)
        with closing(conv._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT ta.turn_id
                FROM turn_attachments ta
                JOIN chat_turns t ON t.id = ta.turn_id
                WHERE ta.attachment_id IN ({placeholders}) AND t.status = 'waiting_for_attachments'
                """,
                attachment_ids,
            ).fetchall()
        return [row["turn_id"] for row in rows]

    def _all_waiting_turn_ids(self, *, limit: int) -> list[str]:
        try:
            bounded_limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            bounded_limit = 100
        conv = self._conv()
        with closing(conv._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id FROM chat_turns
                WHERE status = 'waiting_for_attachments'
                ORDER BY created_at, id
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        return [row["id"] for row in rows]

    def _resolve_turn(self, turn_id: str) -> bool:
        conv = self._conv()
        turn = conv.get_turn_unscoped(turn_id)
        if turn is None or turn.status != "waiting_for_attachments":
            return False
        required = list(getattr(turn, "required_attachment_ids", []) or [])
        if not required:
            return False
        statuses = self._statuses_for_attachments(required)
        if any(status == PARSE_STATUS_FAILED for status in statuses.values()):
            event_payload = {
                "attachment_states": statuses,
                "error_code": "attachment_parse_failed",
            }
            updated = conv.transition_waiting_turn(
                turn_id,
                to_status="failed",
                error_message="附件解析失败，本轮请求已取消；可重试解析后重新发送。",
            )
            if updated:
                self._emit_turn_event(turn_id, "attachment_failed", event_payload)
                record_attachment("waiting_turn", status="failed")
            return updated
        if all(
            status not in {PARSE_STATUS_QUEUED, PARSE_STATUS_RUNNING}
            for status in statuses.values()
        ):
            degraded = [
                attachment_id
                for attachment_id, status in statuses.items()
                if status not in {PARSE_STATUS_READY, PARSE_STATUS_FAILED}
            ]
            updated = conv.transition_waiting_turn(turn_id, to_status="pending")
            if updated:
                self._emit_turn_event(
                    turn_id,
                    "attachment_ready",
                    {"attachment_states": statuses, "degraded": degraded},
                )
                record_attachment(
                    "waiting_turn", status="degraded" if degraded else "ready"
                )
            return updated
        return False

    def _statuses_for_attachments(self, attachment_ids: list[str]) -> dict[str, str]:
        statuses: dict[str, str] = {}
        with closing(self.store._connect()) as conn:
            placeholders = ",".join("?" for _ in attachment_ids)
            rows = conn.execute(
                f"""
                SELECT attachment_id, asset_id, status FROM chat_attachments
                WHERE attachment_id IN ({placeholders})
                """,
                attachment_ids,
            ).fetchall()
            for row in rows:
                if row["status"] != "active":
                    statuses[row["attachment_id"]] = PARSE_STATUS_FAILED
                    continue
                asset = self.store.get_asset(row["asset_id"])
                statuses[row["attachment_id"]] = (
                    asset.parse_status if asset is not None else PARSE_STATUS_FAILED
                )
        for attachment_id in attachment_ids:
            statuses.setdefault(attachment_id, PARSE_STATUS_FAILED)
        return statuses

    def _emit_turn_event(self, turn_id: str, event_type: str, payload: dict[str, Any]) -> None:
        try:
            conv = self._conv()
            conv.append_turn_event(turn_id, event_type, payload)
        except Exception:
            # Event emission is best-effort; the state transition is durable.
            pass
