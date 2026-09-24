"""Expand -> backfill -> enforce migration for ``chat_turns.turn_sequence``.

The runtime schema helper already performs expand+backfill on startup; this
script is the production path: it can validate without writing, backfill legacy
rows deterministically by ``(session_id, user_message_id)``, and optionally
rebuild the table to enforce ``NOT NULL``.

Ordering: run with writes stopped (expand), then backfill, validate, and only
then enforce.  Every step is idempotent.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.core.conversation import ConversationService  # noqa: E402


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.DatabaseError:
        return set()


def expand(conn: sqlite3.Connection) -> dict[str, bool]:
    """Add nullable columns and the unique index; safe for existing writers."""
    result = {"added_session_counter": False, "added_turn_sequence": False}
    session_columns = _columns(conn, "chat_sessions")
    turn_columns = _columns(conn, "chat_turns")
    if not session_columns or not turn_columns:
        raise RuntimeError("conversation tables are missing; run the application schema init first")
    if "next_turn_sequence" not in session_columns:
        conn.execute("ALTER TABLE chat_sessions ADD COLUMN next_turn_sequence INTEGER NOT NULL DEFAULT 1")
        result["added_session_counter"] = True
    if "turn_sequence" not in turn_columns:
        conn.execute("ALTER TABLE chat_turns ADD COLUMN turn_sequence INTEGER")
        result["added_turn_sequence"] = True
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_chat_turns_session_sequence ON chat_turns(session_id, turn_sequence)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_turns_session_status_sequence ON chat_turns(session_id, status, turn_sequence)"
    )
    return result


def backfill(conn: sqlite3.Connection) -> int:
    """Assign sequences to NULL rows; returns how many rows were filled."""
    before = conn.execute("SELECT COUNT(*) FROM chat_turns WHERE turn_sequence IS NULL").fetchone()[0]
    ConversationService._backfill_turn_sequences(conn)
    after = conn.execute("SELECT COUNT(*) FROM chat_turns WHERE turn_sequence IS NULL").fetchone()[0]
    return int(before) - int(after)


def validate(conn: sqlite3.Connection) -> dict[str, object]:
    turn_columns = _columns(conn, "chat_turns")
    session_columns = _columns(conn, "chat_sessions")
    if not turn_columns or not session_columns:
        return {"schema_ready": False, "valid": False, "error": "conversation tables are missing"}
    if "turn_sequence" not in turn_columns or "next_turn_sequence" not in session_columns:
        return {"schema_ready": False, "valid": False, "error": "expand step has not run yet"}
    total = int(conn.execute("SELECT COUNT(*) FROM chat_turns").fetchone()[0])
    nulls = int(conn.execute("SELECT COUNT(*) FROM chat_turns WHERE turn_sequence IS NULL").fetchone()[0])
    zeroes = int(conn.execute("SELECT COUNT(*) FROM chat_turns WHERE turn_sequence = 0").fetchone()[0])
    duplicates = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT session_id, turn_sequence FROM chat_turns
                GROUP BY session_id, turn_sequence HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
    )
    order_violations = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT session_id, turn_sequence,
                       LAG(turn_sequence) OVER (PARTITION BY session_id ORDER BY user_message_id) AS prev_seq
                FROM chat_turns
            ) WHERE prev_seq IS NOT NULL AND turn_sequence <= prev_seq
            """
        ).fetchone()[0]
    )
    counter_deficits = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM chat_sessions s
            WHERE s.next_turn_sequence <= (
                SELECT COALESCE(MAX(t.turn_sequence), 0) FROM chat_turns t WHERE t.session_id = s.id
            )
              AND EXISTS (SELECT 1 FROM chat_turns t WHERE t.session_id = s.id)
            """
        ).fetchone()[0]
    )
    return {
        "schema_ready": True,
        "total_turns": total,
        "null_sequence": nulls,
        "zero_sequence": zeroes,
        "duplicate_sequences": duplicates,
        "order_violations": order_violations,
        "counter_deficits": counter_deficits,
        "valid": nulls == 0 and duplicates == 0 and order_violations == 0 and counter_deficits == 0,
    }


def enforce_not_null(conn: sqlite3.Connection) -> bool:
    """Rebuild chat_turns with NOT NULL; returns True when a rebuild ran."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'chat_turns'"
    ).fetchone()
    if row is None:
        raise RuntimeError("chat_turns table does not exist")
    ddl = str(row[0])
    if re.search(r"turn_sequence\s+INTEGER\s+NOT NULL", ddl):
        return False
    migrated_ddl = re.sub(r"turn_sequence\s+INTEGER(?!\s+NOT NULL)", "turn_sequence INTEGER NOT NULL", ddl, count=1)
    if migrated_ddl == ddl:
        raise RuntimeError("cannot locate turn_sequence column in schema DDL")
    migrated_ddl = migrated_ddl.replace("chat_turns", "chat_turns_migrated", 1)
    indexes = [
        str(item[0])
        for item in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'chat_turns' AND sql IS NOT NULL"
        ).fetchall()
    ]
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(migrated_ddl)
        conn.execute("INSERT INTO chat_turns_migrated SELECT * FROM chat_turns")
        conn.execute("DROP TABLE chat_turns")
        conn.execute("ALTER TABLE chat_turns_migrated RENAME TO chat_turns")
        for statement in indexes:
            conn.execute(statement)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("foreign key check failed after rebuild")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(REPO_ROOT / "storage" / "auth.db"))
    parser.add_argument("--validate-only", action="store_true", help="do not modify the database")
    parser.add_argument("--enforce-not-null", action="store_true", help="rebuild the table after a clean validation")
    parser.add_argument("--report", default="", help="JSON report path")
    args = parser.parse_args(argv)

    path = Path(args.db)
    if not path.is_file():
        print(f"database not found: {path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    report: dict[str, object] = {
        "db": str(path),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "validate_only": args.validate_only,
    }
    try:
        report["before"] = validate(conn)
        if args.validate_only:
            report["valid"] = report["before"]["valid"]
        else:
            report["expand"] = expand(conn)
            report["rows_backfilled"] = backfill(conn)
            report["after_backfill"] = validate(conn)
            if args.enforce_not_null:
                if not report["after_backfill"]["valid"]:
                    raise RuntimeError("cannot enforce NOT NULL before a clean validation")
                report["rebuilt"] = enforce_not_null(conn)
            report["after"] = validate(conn)
            report["valid"] = report["after"]["valid"]
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["valid"] = False
    finally:
        conn.close()

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"report={output}")
    else:
        print(text)
    return 0 if report.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
