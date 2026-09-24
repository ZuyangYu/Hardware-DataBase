"""PostgreSQL adapters for the existing memory job/catalog repositories.

The memory implementation historically speaks sqlite3 directly.  This adapter
keeps its repository semantics while translating the small SQL dialect surface
(``?`` parameters, ``BEGIN IMMEDIATE`` and ``INSERT OR IGNORE``) to psycopg.
All claims still execute in one transaction and use ``FOR UPDATE SKIP LOCKED``
for the worker queue.
"""

from __future__ import annotations

from psycopg.rows import dict_row

from .sql_adapter import (
    PgSqlAdapterConnection as _Connection,
    PgSqlAdapterCursor as _Cursor,  # noqa: F401 - kept for adapter parity
    normalize_value as _normalize,  # noqa: F401
)


class PostgresMemoryJobRepository:
    """Drop-in job repository backed by the control-plane PostgreSQL schema."""

    def __init__(self, database_url: str):
        from src.memory.jobs import MemoryJobRepository

        self._base = MemoryJobRepository.__new__(MemoryJobRepository)
        self.database_url = str(database_url)

    def _connect(self):
        import psycopg

        # The SQLite repositories run with ``isolation_level=None`` (every
        # statement auto-commits unless an explicit BEGIN is open).  Mirror
        # that contract: psycopg autocommit keeps standalone repository
        # methods durable, while explicit BEGIN/COMMIT still spans the
        # multi-statement claim/apply paths.
        return _Connection(
            psycopg.connect(
                self.database_url, row_factory=dict_row, autocommit=True
            )
        )

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 180,
        max_retries: int = 5,
        now: str | None = None,
    ):
        """Claim one job with a PostgreSQL row lease.

        The SQLite implementation obtains an equivalent write lock with
        ``BEGIN IMMEDIATE``.  PostgreSQL workers must use ``SKIP LOCKED`` so
        a busy or slow candidate cannot block unrelated jobs, and so two
        workers can never receive the same lease token.
        """
        del now  # PostgreSQL time is the lease authority.
        from src.memory.catalog import row_to_job
        from src.memory.jobs import _dead_letter_alert
        from src.observability.metrics import counter
        import uuid

        token = str(uuid.uuid4())
        lease = max(1, int(lease_seconds))
        attempts_limit = max(1, int(max_retries))
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                current = conn.execute(
                    """
                    SELECT * FROM memory_jobs
                    WHERE (
                        status IN ('pending', 'retrying')
                        AND available_at <= clock_timestamp()
                        AND (next_retry_at IS NULL OR next_retry_at <= clock_timestamp())
                    ) OR (
                        status = 'running'
                        AND lease_expires_at IS NOT NULL
                        AND lease_expires_at <= clock_timestamp()
                    )
                    ORDER BY CASE WHEN job_kind = 'user_reflection' THEN 0 ELSE 1 END,
                             requested_at, job_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """
                ).fetchone()
                if current is None:
                    conn.commit()
                    return None
                attempts = int(current["retry_count"] or 0)
                if attempts >= attempts_limit:
                    conn.execute(
                        """
                        UPDATE memory_jobs
                        SET status = 'dead_letter', retry_count = %s,
                            last_error = %s, lease_owner = NULL,
                            lease_token = NULL, lease_expires_at = NULL
                        WHERE job_id = %s
                        """,
                        (
                            attempts,
                            "maximum memory job attempts exceeded",
                            current["job_id"],
                        ),
                    )
                    conn.commit()
                    counter("hdb.memory.job_dead_letter", attributes={"job_kind": current["job_kind"]})
                    _dead_letter_alert(
                        {
                            "queue": "job",
                            "job_id": str(current["job_id"]),
                            "job_kind": str(current["job_kind"]),
                            "attempts": attempts,
                            "reason": "max_attempts_exceeded_on_claim",
                        }
                    )
                    return None
                claimed = conn.execute(
                    """
                    UPDATE memory_jobs
                    SET status = 'running', lease_owner = %s, lease_token = %s,
                        lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                        claimed_at = clock_timestamp(), retry_count = %s,
                        next_retry_at = NULL
                    WHERE job_id = %s
                    RETURNING *
                    """,
                    (
                        str(worker_id),
                        token,
                        lease,
                        attempts + 1,
                        current["job_id"],
                    ),
                ).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if claimed is None:  # pragma: no cover - row lock guarantees presence
            return None
        counter("hdb.memory.jobs_total", attributes={"job_kind": claimed["job_kind"]})
        if current["status"] == "running":
            counter("hdb.memory.job_lease_expired", attributes={"job_kind": current["job_kind"]})
        if attempts > 0:
            counter("hdb.memory.job_replayed", attributes={"job_kind": claimed["job_kind"]})
        return row_to_job(claimed)

    def __getattr__(self, name):
        # Bind inherited repository methods to this adapter instance.  The
        # methods only rely on ``_connect`` and do not inspect sqlite state.
        from src.memory.jobs import MemoryJobRepository

        attr = getattr(MemoryJobRepository, name, None)
        if attr is None:
            raise AttributeError(name)
        return attr.__get__(self, type(self)) if callable(attr) else attr

    def catalog(self):
        return PostgresMemoryCatalogRepository(self.database_url)


class PostgresMemoryCatalogRepository:
    """Drop-in catalog repository using the same translated SQL boundary."""

    def __init__(self, database_url: str):
        self.database_url = str(database_url)
        self._external_conn = False
        self._conn = self._connect()

    def _connect(self):
        import psycopg

        # Same autocommit contract as the job adapter (see above): standalone
        # catalog methods must persist without an explicit commit, while
        # BEGIN/COMMIT from callers still spans multi-statement work.
        return _Connection(
            psycopg.connect(
                self.database_url, row_factory=dict_row, autocommit=True
            )
        )

    @property
    def conn(self):
        return self._conn

    def close(self):
        conn = getattr(self, "_conn", None)
        if conn is not None:
            conn.close()
            self._conn = None

    def __getattr__(self, name):
        from src.memory.catalog import MemoryCatalogRepository

        attr = getattr(MemoryCatalogRepository, name, None)
        if attr is None:
            raise AttributeError(name)
        return attr.__get__(self, type(self)) if callable(attr) else attr


__all__ = ["PostgresMemoryJobRepository", "PostgresMemoryCatalogRepository"]
