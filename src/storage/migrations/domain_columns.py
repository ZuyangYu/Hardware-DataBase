"""Idempotent column repair for mechanically provisioned domain tables."""

from __future__ import annotations

from typing import Any, Callable


def add_missing_columns(
    conn: Any,
    table: str,
    columns: list[dict[str, Any]],
    *,
    ident: Callable[[str], str],
    type_for: Callable[[str], str],
    default_for: Callable[[Any], str | None],
) -> None:
    """Add columns introduced after a PostgreSQL table was first provisioned.

    ``CREATE TABLE IF NOT EXISTS`` never changes an existing shape.  Without
    this step a new column exists in the SQLite template and in fresh
    PostgreSQL deployments, but not in an upgraded one; subsequent index
    creation then fails with ``UndefinedColumn``.
    """

    existing = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table,),
        ).fetchall()
    }
    for column in columns:
        name = str(column["name"])
        if name in existing:
            continue
        if int(column.get("pk") or 0) > 0:
            raise RuntimeError(
                f"cannot add missing primary-key column {table}.{name}; "
                "rebuild the table explicitly"
            )
        parts = [ident(name), type_for(str(column["type"]))]
        default = default_for(column.get("dflt_value"))
        if int(column.get("notnull") or 0):
            if default is None:
                raise RuntimeError(
                    f"cannot add NOT NULL column {table}.{name} without a "
                    "mechanical default; backfill it explicitly"
                )
            parts.append("NOT NULL")
        if default is not None:
            parts.append(f"DEFAULT {default}")
        conn.execute(
            f"ALTER TABLE {ident(table)} ADD COLUMN {' '.join(parts)}"
        )
