"""Terminal domain writes for the PostgreSQL ``complete_turn`` (v3 §4.7/§5.1).

The SQLite completion path writes the result-export snapshot/jobs and the
Memory project-reflection outbox inside the answer transaction.  The
PostgreSQL execution store exposes a ``domain_writer(conn, turn)`` hook for the
same purpose; this module is the canonical writer so API and Worker get the
same atomic semantics on either backend.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import src.settings as settings

from .sql_adapter import PgSqlAdapterConnection


def write_terminal_domains(
    conn: Any,
    turn: Any,
    *,
    owner_user_id: int | str,
    settings_module: Any | None = None,
) -> None:
    """Write export snapshot/jobs and the Memory outbox in ``conn``'s tx.

    ``conn`` is the raw psycopg connection owned by the execution store's
    transaction; nothing here commits or closes it, so any failure rolls the
    answer, terminal status and events back together.
    """
    config = settings_module or settings

    if bool(getattr(config, "RESULT_EXPORT_ENABLED", True)) and turn.export_plan:
        from src.core.conversation import GENERAL_CHAT_KB_NAME
        from src.result_exports.content import envelope_from_turn
        from src.result_exports.store import ResultExportStore

        store = ResultExportStore.__new__(ResultExportStore)
        store.enqueue_completed_turn_in_connection(
            PgSqlAdapterConnection(conn),
            owner_user_id=owner_user_id,
            tenant_id="default",
            department_id=turn.department_id,
            knowledge_base_name=(
                "" if turn.kb_name == GENERAL_CHAT_KB_NAME else turn.kb_name
            ),
            session_id=turn.session_id,
            turn_id=turn.id,
            assistant_message_id=turn.assistant_message_id,
            envelope=envelope_from_turn(turn),
            export_plan=turn.export_plan,
        )

    if (
        bool(getattr(config, "MEMORY_ENABLED", True))
        and bool(getattr(config, "MEMORY_EXTRACTION_ENABLED", True))
        and turn.department_id not in (None, "")
        and turn.kb_id not in (None, "")
    ):
        from src.memory.catalog import scope_fingerprint

        from .memory_jobs import enqueue_project_reflection

        debounce = max(0, int(getattr(config, "MEMORY_DEBOUNCE_SECONDS", 300)))
        available_at = (
            datetime.now(timezone.utc).replace(microsecond=0)
            + timedelta(seconds=debounce)
        ).isoformat()
        enqueue_project_reflection(
            conn,
            session_id=int(turn.session_id),
            scope_fingerprint=scope_fingerprint(
                scope="project",
                department_id=turn.department_id,
                kb_id=turn.kb_id,
            ),
            target_turn_id=turn.id,
            target_message_id=int(turn.assistant_message_id),
            available_at=available_at,
        )


__all__ = ["write_terminal_domains"]
