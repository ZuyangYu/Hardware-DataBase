"""Control-plane storage package (v3 §5).

PR-2 scope: the versioned PostgreSQL schema, schema compatibility assertions
and the SQLite→PostgreSQL loader/verifier.  The repository/Facade split and
the execution-protocol switch land with the following PRs; this package owns
schema truth and the migration contract only.
"""

from .base import ExecutionStore, RequeueSummary
from .budget import (
    ConnectionBudget,
    budget_fits,
    connection_budget,
    settings_connection_budget,
)
from .factory import build_conversation_service, build_execution_store
from .loader import LoadReport, TableReport, load_sqlite_snapshot
from .schema import (
    CORE_TABLES,
    SCHEMA_TABLE,
    SCHEMA_VERSION,
    TURN_STATUSES,
    apply_schema,
    create_core_indexes,
    is_core_table,
    pg_type_for,
)
from .schema_version import assert_schema_compatible, record_schema_version

__all__ = [
    "CORE_TABLES",
    "ConnectionBudget",
    "ExecutionStore",
    "LoadReport",
    "RequeueSummary",
    "SCHEMA_TABLE",
    "SCHEMA_VERSION",
    "TURN_STATUSES",
    "TableReport",
    "apply_schema",
    "assert_schema_compatible",
    "budget_fits",
    "build_conversation_service",
    "build_execution_store",
    "connection_budget",
    "create_core_indexes",
    "is_core_table",
    "load_sqlite_snapshot",
    "pg_type_for",
    "record_schema_version",
    "settings_connection_budget",
]
