"""PostgreSQL checkpointer provider (multi-instance target, P1-001).

Requires ``langgraph-checkpoint-postgres`` and ``psycopg``.  The schema is
created only by ``migrate`` (``src.storage.bootstrap``, under the storage lock);
the application runtime never runs DDL and fails closed if the checkpoint
schema is missing or at a different version.  Migrations and history/delete
compatibility must be validated by the P1 migration runbook before switching
production writes.

The saver runs on a ``psycopg_pool.ConnectionPool`` when available, so
concurrent API/Worker threads never share one connection (psycopg connections
are not safe for concurrent use).  Pool size is bounded by
``AGENT_CHECKPOINT_POOL_SIZE`` (default 4) and the pool is closed with the
provider.
"""

from __future__ import annotations

from typing import Any

import src.settings as settings


_SCHEMA_NOT_READY_HINT = """\
检查点表未就绪（{detail}）。

应用运行期不会创建表。请先执行 migrate：

  python -m src.storage.bootstrap          # 幂等，可重复执行

源码方式可以直接用 bash scripts/hdb.sh start（它会自动执行上面这步）；
Docker 部署由 migrate 服务负责，请确认它已成功：

  docker compose logs migrate"""


class CheckpointSchemaNotReady(RuntimeError):
    """检查点 schema 缺失或版本不符。fail-closed，异常文本即为完整修复指引。

    启动路径会把它原样抛出：traceback 第一行就是"该执行什么命令"，
    比把提示打印到 stderr 更耐日志截断（worker 侧也不再有第二份输出）。
    """


def ensure_checkpoint_schema(database_url: str) -> None:
    """Create or upgrade the LangGraph checkpoint schema (仅 ``migrate`` 调用)。

    应用运行期从不建表：多个进程同时建表会在 ``pg_type`` 上撞唯一约束。
    建表由 bootstrap 在存储锁内串行完成，运行期只做 :func:`verify_checkpoint_schema`。
    """
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver

    with psycopg.connect(str(database_url), autocommit=True) as conn:
        PostgresSaver(conn).setup()


def verify_checkpoint_schema(database_url: str) -> str:
    """只读校验检查点表版本，不符则 fail-closed。返回已验证的版本号。"""
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver

    expected = len(PostgresSaver.MIGRATIONS) - 1
    try:
        with psycopg.connect(
            str(database_url), autocommit=True, connect_timeout=5
        ) as conn:
            row = conn.execute(
                "SELECT v FROM checkpoint_migrations ORDER BY v DESC LIMIT 1"
            ).fetchone()
    except psycopg.errors.UndefinedTable as exc:
        raise CheckpointSchemaNotReady(
            _SCHEMA_NOT_READY_HINT.format(detail="表不存在")
        ) from exc
    stored = int(row[0]) if row is not None else -1
    if stored != expected:
        raise CheckpointSchemaNotReady(
            _SCHEMA_NOT_READY_HINT.format(
                detail=f"版本不符（stored={stored} expected={expected}）"
            )
        )
    return str(stored)


def verify_configured_checkpoint_schemas(settings_module: Any | None = None) -> list[str]:
    """校验所有使用 postgres 检查点后端的配置项；sqlite 直接跳过。"""
    if settings_module is None:
        import src.settings as settings_module  # type: ignore[no-redef]

    verified: list[str] = []
    targets = (
        (
            "agent-checkpoint",
            getattr(settings_module, "AGENT_CHECKPOINT_BACKEND", "sqlite"),
            getattr(settings_module, "AGENT_CHECKPOINT_DATABASE_URL", ""),
        ),
        (
            "authoring-checkpoint",
            getattr(settings_module, "DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND", "sqlite"),
            getattr(settings_module, "DOCUMENT_AUTHORING_CHECKPOINTER_DATABASE_URL", ""),
        ),
    )
    for name, backend, dsn in targets:
        if str(backend or "").strip().lower() != "postgres":
            continue
        if not str(dsn or "").strip():
            raise CheckpointSchemaNotReady(f"{name}: 缺少 DATABASE_URL 配置")
        try:
            verify_checkpoint_schema(str(dsn).strip())
        except CheckpointSchemaNotReady as exc:
            raise CheckpointSchemaNotReady(f"{name}: {exc}") from exc
        verified.append(name)
    return verified


class PostgresCheckpointerProvider:
    backend = "postgres"

    def __init__(self, database_url: str, *, setup: bool = False) -> None:
        self.database_url = str(database_url or "").strip()
        # ``setup=True`` 只给迁移/运维工具用（DDL）；应用运行期默认只校验版本。
        self.setup = bool(setup)
        self._connection: Any | None = None
        self._pool: Any | None = None
        self._verified = False

    def _pool_size(self) -> int:
        try:
            return max(1, int(getattr(settings, "AGENT_CHECKPOINT_POOL_SIZE", 4) or 4))
        except (TypeError, ValueError):
            return 4

    def create(self) -> Any:
        if not self.database_url:
            raise ValueError("AGENT_CHECKPOINT_DATABASE_URL is required for the postgres backend")
        try:
            import psycopg
            from langgraph.checkpoint.postgres import PostgresSaver
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise RuntimeError(
                "postgres checkpoint backend requires langgraph-checkpoint-postgres and psycopg"
            ) from exc

        # 运行期不建表：schema 缺失或版本不符时在这里 fail-closed，而不是并发建表。
        if self.setup:
            ensure_checkpoint_schema(self.database_url)
            self._verified = True
        elif not self._verified:
            verify_checkpoint_schema(self.database_url)
            self._verified = True

        pool_cls = None
        try:
            from psycopg_pool import ConnectionPool as pool_cls  # type: ignore[assignment]
        except ImportError:  # pragma: no cover - optional dependency
            pool_cls = None

        if pool_cls is not None:
            if self._pool is None:
                self._pool = pool_cls(
                    self.database_url,
                    min_size=1,
                    max_size=self._pool_size(),
                    kwargs={"autocommit": True},
                    open=True,
                    name="hdb-agent-checkpoint",
                )
            saver = PostgresSaver(self._pool)
        else:
            if self._connection is None:
                self._connection = psycopg.connect(self.database_url, autocommit=True)
            saver = PostgresSaver(self._connection)
        # ``setup`` is idempotent.  Re-run it for each saver so a maintenance
        # rehearsal or test that recreates the target schema cannot leave a
        # process-level provider pointing at missing checkpoint tables.
        return saver

    def close(self) -> None:
        if self._pool is not None:
            try:
                self._pool.close()
            finally:
                self._pool = None
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None
