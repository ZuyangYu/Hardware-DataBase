"""Per-user in-app notifications (control-plane, auth.db).

Notifications are a delivery surface only: the authoritative audit data stays
in the source tables (for example ``chat_attachment_revisions``).  Writes are
fail-soft for callers, and reads are always scoped by ``user_id``.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any



def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _post_webhook(
    url: str, payload: dict[str, Any], *, token: str = "", timeout: float = 2.0
) -> None:
    """POST one notification payload; separated for tests and fail-soft use."""

    import json
    import urllib.request

    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout):  # noqa: S310 - configured URL
        pass


def _dispatch_webhook(notification: "Notification") -> None:
    """Outbound notification delivery; never breaks the in-app write."""

    try:
        import src.settings

        url = str(getattr(src.settings, "NOTIFY_WEBHOOK_URL", "") or "").strip()
        if not url:
            return
        token = str(getattr(src.settings, "NOTIFY_WEBHOOK_TOKEN", "") or "").strip()
        timeout = float(getattr(src.settings, "NOTIFY_WEBHOOK_TIMEOUT_SECONDS", 2) or 2)
        _post_webhook(
            url,
            {
                "kind": notification.kind,
                "title": notification.title,
                "body": notification.body,
                "target_type": notification.target_type,
                "target_id": notification.target_id,
                "payload": notification.payload,
                "created_at": notification.created_at,
            },
            token=token,
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 - delivery is best-effort
        pass


def _row_to_notification(row: sqlite3.Row) -> "Notification":
    import json

    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    return Notification(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        kind=str(row["kind"] or ""),
        target_type=str(row["target_type"] or ""),
        target_id=str(row["target_id"] or ""),
        title=str(row["title"] or ""),
        body=str(row["body"] or ""),
        payload=payload if isinstance(payload, dict) else {},
        read_at=row["read_at"],
        created_at=str(row["created_at"] or ""),
    )


@dataclass(frozen=True)
class Notification:
    id: int
    user_id: int
    kind: str
    target_type: str
    target_id: str
    title: str
    body: str
    payload: dict[str, Any] = field(default_factory=dict)
    read_at: str | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.id,
            "kind": self.kind,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "title": self.title,
            "body": self.body,
            "payload": dict(self.payload),
            "read": self.read_at is not None,
            "created_at": self.created_at,
        }


class NotificationService:
    """Minimal store for in-app notifications."""

    def __init__(self, db_path: str | None = None):
        from src.storage import auth_domain

        if auth_domain.configure_service(self, db_path):
            parent = self.db_path.rsplit("/", 1)[0] if "/" in self.db_path else "."
            import os

            os.makedirs(parent, exist_ok=True)
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if getattr(self, "_backend", "sqlite") == "postgres":
            from src.storage import auth_domain

            return auth_domain.connect(self)
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    target_type TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    read_at TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_notifications_user_read
                ON notifications(user_id, read_at, id)
                """
            )

    def create(
        self,
        *,
        user_id: int,
        kind: str,
        title: str,
        body: str = "",
        target_type: str = "",
        target_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> Notification | None:
        import json

        if int(user_id or 0) <= 0:
            return None
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT INTO notifications (
                    user_id, kind, target_type, target_id, title, body,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(user_id),
                    str(kind)[:100],
                    str(target_type)[:100],
                    str(target_id)[:200],
                    str(title)[:300],
                    str(body)[:2000],
                    json.dumps(payload or {}, ensure_ascii=False, default=str),
                    _utc_now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM notifications WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        notification = _row_to_notification(row) if row else None
        if notification is not None:
            _dispatch_webhook(notification)
        return notification

    def list_for_user(
        self, *, user_id: int, unread_only: bool = False, limit: int = 50
    ) -> list[Notification]:
        clauses = ["user_id = ?"]
        if unread_only:
            clauses.append("read_at IS NULL")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM notifications
                WHERE {' AND '.join(clauses)}
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(user_id), max(1, min(int(limit), 200))),
            ).fetchall()
        return [_row_to_notification(row) for row in rows]

    def unread_count(self, *, user_id: int) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND read_at IS NULL",
                (int(user_id),),
            ).fetchone()
        return int(row["n"] or 0) if row else 0

    def mark_read(self, *, user_id: int, notification_id: int) -> bool:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                UPDATE notifications SET read_at = ?
                WHERE id = ? AND user_id = ? AND read_at IS NULL
                """,
                (_utc_now(), int(notification_id), int(user_id)),
            )
            changed = int(cursor.rowcount or 0)
        return changed > 0

    def mark_all_read(self, *, user_id: int) -> int:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
                (_utc_now(), int(user_id)),
            )
            changed = int(cursor.rowcount or 0)
        return changed


__all__ = ["Notification", "NotificationService"]
