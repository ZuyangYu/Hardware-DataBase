"""一次性存储初始化/校验（容器编排的 ``migrate`` 门槛）。

解决的问题：控制面 schema 是"应用永不隐式建表"（fail-closed），所以全新
PostgreSQL 上直接起 API 会因为缺表而拒绝启动。本模块把"建表 + 记录版本 +
按域建表"收敛成一次幂等调用，容器编排里作为 api/worker 的前置门槛：

    python -m src.storage.bootstrap

行为：
0. 涉及的每个 PostgreSQL 库上先取同一把 advisory lock，整个过程串行；
   多个 bootstrap（容器 migrate、``scripts/hdb.sh start``、手动执行）并发时，
   后到者等待前者完成后再幂等确认，不会撞唯一约束。
1. ``CONTROL_PLANE_BACKEND=postgres`` 时：
   - 缺 ``control_plane_schema`` → ``apply_schema`` + ``record_schema_version``；
   - 已存在 → ``assert_schema_compatible``（版本不符即失败，绝不静默放行）；
2. 逐域幂等建表（projects / pipeline_documents / document_authoring /
   认证域 / 兼容库），机械映射，表结构由各 store 自己的 SQLite bootstrap 生成；
   控制面 PG 上另外建 memory / result_exports 账本，以及认证镜像表 users /
   departments（认证域保持 SQLite 时）；
3. 认证域额外做形状校验（早期按控制面语义建过的库会在此 fail-closed 并提示
   重建命令）；
4. agent / 文档生成的 LangGraph 检查点表（仅当对应后端为 postgres 时），串行建表，
   避免运行期并发 ``setup()`` 竞态。

可重复执行；失败以非零退出码结束，让 ``depends_on:
service_completed_successfully`` 拦住后续服务。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager


def _control_plane(dsn: str) -> str:
    """返回一句人类可读的结果描述；失败时抛异常。"""
    import psycopg

    from src.storage.control_plane.loader import _add_constraint_if_missing
    from src.storage.control_plane.schema import (
        CORE_FOREIGN_KEYS,
        SCHEMA_TABLE,
        apply_schema,
    )
    from src.storage.control_plane.schema_version import (
        assert_schema_compatible,
        record_schema_version,
    )

    with psycopg.connect(dsn, autocommit=True, options="-c TimeZone=UTC") as conn:
        exists = conn.execute(
            "SELECT to_regclass(%s) IS NOT NULL", (f"public.{SCHEMA_TABLE}",)
        ).fetchone()[0]
        if not exists:
            apply_schema(conn)
            record_schema_version(conn)
            return "控制面 schema 已创建（版本已记录）"
        version = assert_schema_compatible(conn)
        # 控制面外键（如 chat_sessions → users）在数据迁移路径上补挂；
        # 全新库由 apply_schema 内联建好，这里只在缺失时补齐。
        for name, table, constraint_sql in CORE_FOREIGN_KEYS:
            reference = (
                constraint_sql.split("REFERENCES")[1].split("(")[0].strip().strip('"')
            )
            ready = conn.execute(
                "SELECT to_regclass(%s) IS NOT NULL AND to_regclass(%s) IS NOT NULL",
                (f"public.{table}", f"public.{reference}"),
            ).fetchone()[0]
            if ready:
                _add_constraint_if_missing(conn, table, name, constraint_sql)
        return f"控制面 schema 已存在（版本 {version}）"


# 所有 bootstrap 进程共用的 advisory lock key（会话级，连接断开自动释放）。
_STORAGE_LOCK_KEY = 0x48444247  # "HDBG"


def _postgres_dsns() -> list[str]:
    """本次 bootstrap 会改动的全部 PostgreSQL 数据库（去重、排序）。"""
    import src.settings as settings
    from src.storage import domain_pg

    dsns: set[str] = set()
    if str(getattr(settings, "CONTROL_PLANE_BACKEND", "sqlite") or "").strip().lower() == "postgres":
        dsns.add(str(getattr(settings, "CONTROL_PLANE_DATABASE_URL", "") or "").strip())
    for prefix in (
        "PROJECT_STORE",
        "PIPELINE_DOCUMENT",
        "AUTHORING_STORE",
        "AUTH_STORE",
        "COMPATIBILITY_STORE",
    ):
        try:
            backend_for_domain, domain_dsn = domain_pg.resolve_domain_backend(prefix)
        except RuntimeError:
            continue  # 配置错误由 _bootstrap 的各域步骤报告
        if backend_for_domain == "postgres":
            dsns.add(domain_dsn)
    for backend_key, url_key in (
        ("AGENT_CHECKPOINT_BACKEND", "AGENT_CHECKPOINT_DATABASE_URL"),
        ("DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND", "DOCUMENT_AUTHORING_CHECKPOINTER_DATABASE_URL"),
    ):
        if str(getattr(settings, backend_key, "") or "").strip().lower() == "postgres":
            dsns.add(str(getattr(settings, url_key, "") or "").strip())
    dsns.discard("")
    return sorted(dsns)


@contextmanager
def _storage_lock(dsns: list[str], *, timeout: float = 900.0) -> Iterator[None]:
    """在每个数据库上持有同一把 advisory lock，按固定顺序加锁防止死锁。

    同一时刻只允许一个 bootstrap 修改这些库的 schema。等待方用
    ``pg_try_advisory_lock`` 轮询而不是阻塞式 ``pg_advisory_lock``：阻塞等待的
    语句会持有快照，而持锁方执行的 ``CREATE INDEX CONCURRENTLY``（langgraph 迁移）
    要等所有旧快照结束，两者互等会永久挂起，PostgreSQL 的死锁检测覆盖不到这种等待。
    """
    import time

    import psycopg

    conns: list[psycopg.Connection] = []
    deadline = time.monotonic() + timeout
    try:
        for dsn in dsns:
            conn = psycopg.connect(dsn, autocommit=True, options="-c TimeZone=UTC", connect_timeout=10)
            conns.append(conn)
            while not conn.execute("SELECT pg_try_advisory_lock(%s)", (_STORAGE_LOCK_KEY,)).fetchone()[0]:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"等待存储锁超过 {timeout:.0f}s，可能有僵死的 bootstrap 进程")
                time.sleep(0.5)
        yield
    finally:
        for conn in conns:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_STORAGE_LOCK_KEY,))
            finally:
                conn.close()


def main(argv: list[str] | None = None) -> int:
    dsns = _postgres_dsns()
    if not dsns:
        return _bootstrap()
    try:
        with _storage_lock(dsns):
            return _bootstrap()
    except Exception as exc:  # noqa: BLE001 - 无法加锁/连库必须可见
        print(f"[bootstrap] 无法获取存储锁或连接数据库: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


def _bootstrap() -> int:
    import src.settings as settings
    from src.storage import domain_pg

    backend = str(getattr(settings, "CONTROL_PLANE_BACKEND", "sqlite") or "sqlite")
    dsn = str(getattr(settings, "CONTROL_PLANE_DATABASE_URL", "") or "").strip()

    print(f"[bootstrap] 控制面后端 = {backend}")
    if backend == "postgres":
        if not dsn:
            print("[bootstrap] 错误：CONTROL_PLANE_BACKEND=postgres 但缺少 DSN", file=sys.stderr)
            return 2
        try:
            print(f"[bootstrap] {_control_plane(dsn)}")
        except Exception as exc:  # noqa: BLE001 - 一次性门槛：失败必须可见
            print(f"[bootstrap] 控制面初始化失败: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 3

    groups: list[tuple[str, tuple[type, ...]]] = []
    from src.document_authoring.pg_support import compat_classes, store_classes
    from src.pipelines.document_store_sqlite import PipelineDocumentStore
    from src.projects.store import ProjectStore
    from src.storage.auth_domain import domain_classes

    groups = [
        ("projects", (ProjectStore,)),
        ("pipeline_documents", (PipelineDocumentStore,)),
        ("document_authoring", store_classes()),
        ("auth", domain_classes()),
        ("compatibility", compat_classes()),
    ]

    failed = 0
    for name, classes in groups:
        backend_for_domain, domain_dsn = domain_pg.resolve_domain_backend(
            _prefix_for(name)
        )
        if backend_for_domain != "postgres":
            print(f"[bootstrap] {name}: sqlite（跳过建表）")
            continue
        try:
            tables = domain_pg.provision_domain_group(domain_dsn, classes)
            print(f"[bootstrap] {name}: 已就绪 {len(tables)} 张表")
        except Exception as exc:  # noqa: BLE001 - 门槛失败要可见
            print(f"[bootstrap] {name}: 建表失败 {type(exc).__name__}: {exc}", file=sys.stderr)
            failed += 1

    # 控制面 PG 上还需要三类表，它们的 schema 只存在于 SQLite 方言的 store 里：
    # memory / result_exports 账本（运行时直接读写 PG），以及认证域的镜像表
    # users / departments（控制面外键与 JOIN 依赖它们，见 auth_mirror）。
    if backend == "postgres":
        from src.storage.auth_mirror import MIRROR_TABLES

        auth_backend, _ = domain_pg.resolve_domain_backend("AUTH_STORE")
        extras = (
            ("memory", (_MemoryCatalogTemplate,), None),
            ("result_exports", (_ResultExportTemplate,), None),
        )
        for name, classes, only in extras:
            try:
                # 语义映射（与 loader 的迁移形状一致）：memory / result_exports 的表由
                # 控制面事务直接读写，SQL 用的是 PG 布尔/时间戳语法；机械映射（TEXT/BIGINT）
                # 会让 `AND source_valid`、`= TRUE` 这类查询直接报类型错误。
                tables = _provision_semantic(dsn, classes, tables=only)
                print(f"[bootstrap] {name}: 已就绪 {len(tables)} 张表")
            except Exception as exc:  # noqa: BLE001 - 门槛失败要可见
                print(f"[bootstrap] {name}: 建表失败 {type(exc).__name__}: {exc}", file=sys.stderr)
                failed += 1
        if auth_backend != "postgres":
            from src.storage.auth_domain import domain_classes as auth_classes

            try:
                tables = domain_pg.provision_domain_group(
                    dsn,
                    auth_classes(),
                    tables=list(MIRROR_TABLES),
                    include_foreign_keys=False,
                )
                print(f"[bootstrap] auth-mirror: 已就绪 {len(tables)} 张表（{', '.join(tables)}）")
            except Exception as exc:  # noqa: BLE001 - 门槛失败要可见
                print(f"[bootstrap] auth-mirror: 建表失败 {type(exc).__name__}: {exc}", file=sys.stderr)
                failed += 1

    # LangGraph 检查点表（agent / 文档生成两路）。运行期 saver 并发 setup() 在表
    # 不存在时会撞 pg_type 唯一约束，所以这里串行建好，运行期只剩空操作。
    from src.agents.checkpoints.postgres_provider import ensure_checkpoint_schema

    checkpoint_targets = (
        (
            "agent-checkpoint",
            getattr(settings, "AGENT_CHECKPOINT_BACKEND", "sqlite"),
            getattr(settings, "AGENT_CHECKPOINT_DATABASE_URL", ""),
        ),
        (
            "authoring-checkpoint",
            getattr(settings, "DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND", "sqlite"),
            getattr(settings, "DOCUMENT_AUTHORING_CHECKPOINTER_DATABASE_URL", ""),
        ),
    )
    for name, checkpoint_backend, checkpoint_dsn in checkpoint_targets:
        if str(checkpoint_backend or "").strip().lower() != "postgres":
            continue
        try:
            if not str(checkpoint_dsn or "").strip():
                raise RuntimeError("缺少 DATABASE_URL")
            ensure_checkpoint_schema(str(checkpoint_dsn).strip())
            print(f"[bootstrap] {name}: 检查点表已就绪")
        except Exception as exc:  # noqa: BLE001 - 门槛失败要可见
            print(f"[bootstrap] {name}: 建表失败 {type(exc).__name__}: {exc}", file=sys.stderr)
            failed += 1

    # 认证域形状校验：早期按控制面语义建过的库必须显式重建
    auth_backend, auth_dsn = domain_pg.resolve_domain_backend("AUTH_STORE")
    if auth_backend == "postgres":
        from src.storage.auth_domain import assert_shape

        try:
            assert_shape(auth_dsn, "users", {"is_active": "bigint", "created_at": "text"})
            print("[bootstrap] auth: 表形状校验通过")
        except Exception as exc:  # noqa: BLE001
            print(f"[bootstrap] auth: {exc}", file=sys.stderr)
            failed += 1

    if failed:
        print(f"[bootstrap] 完成但有 {failed} 项失败", file=sys.stderr)
        return 4
    print("[bootstrap] 全部就绪")
    return 0


# pg_type_for 的输出 → information_schema.data_type 口径，用于形状校验。
_PG_TYPE_TO_INFO = {
    "TEXT": "text",
    "BIGINT": "bigint",
    "INTEGER": "integer",
    "SMALLINT": "smallint",
    "BOOLEAN": "boolean",
    "JSONB": "jsonb",
    "TIMESTAMPTZ": "timestamp with time zone",
    "DOUBLE PRECISION": "double precision",
    "BYTEA": "bytea",
}


def _provision_semantic(dsn: str, store_classes, tables=None) -> list[str]:
    """按控制面语义映射建表（BOOLEAN / TIMESTAMPTZ / JSONB），与 loader 的迁移形状一致。

    memory / result_exports 的表由控制面事务直接读写，SQL 使用 PG 原生布尔与时间戳
    语法（`AND source_valid`、`= TRUE`、`::timestamptz`）；机械映射（TEXT/BIGINT）只
    适用于 SQLite 方言的域 store。旧版本误用机械映射建过的空表会自动重建，有数据时
    fail-closed 并提示迁移。
    """
    import psycopg

    from src.storage import domain_pg
    from src.storage.control_plane.loader import _add_constraint_if_missing
    from src.storage.control_plane.schema import (
        PARTIAL_INDEX_OVERRIDES,
        generated_table_ddl,
        pg_ident,
        pg_type_for,
    )

    metadata = domain_pg._template_metadata(tuple(store_classes))
    wanted = list(tables) if tables else list(metadata["columns"].keys())
    unknown = [t for t in wanted if t not in metadata["columns"]]
    if unknown:
        raise RuntimeError(f"store 组缺少 SQLite 表: {unknown}")

    def _expected(table: str, column: dict) -> str:
        semantic = pg_type_for(table, str(column["name"]), str(column["type"] or ""))
        return _PG_TYPE_TO_INFO.get(semantic, "text")

    with psycopg.connect(dsn, autocommit=True, options="-c TimeZone=UTC") as conn:
        for table in wanted:
            columns = metadata["columns"][table]
            rows = conn.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                """,
                (table,),
            ).fetchall()
            if not rows:
                continue
            actual = {str(row[0]): str(row[1]).lower() for row in rows}
            mismatched = [
                (str(column["name"]), actual.get(str(column["name"]), "缺失"), _expected(table, column))
                for column in columns
                if actual.get(str(column["name"])) != _expected(table, column)
            ]
            if not mismatched:
                continue
            count = int(conn.execute(f"SELECT count(*) FROM {pg_ident(table)}").fetchone()[0])
            if count:
                detail = "；".join(f"{name}: 实际 {got} 期望 {want}" for name, got, want in mismatched)
                raise RuntimeError(
                    f"{table} 表结构与控制面语义映射不一致且已有 {count} 行数据（{detail}）。"
                    " 需先迁移该表（导出 → 重建 → 导入）再重跑 bootstrap。"
                )
            conn.execute(f"DROP TABLE IF EXISTS {pg_ident(table)} CASCADE")
            print(f"[bootstrap] {table}: 旧的机械映射形状且无数据，已重置为语义形状")

        loaded: set[str] = set()
        for table in wanted:
            conn.execute(generated_table_ddl(table, metadata["columns"][table]))
            loaded.add(table)
        for table in wanted:
            for statement in metadata["indexes"].get(table, []):
                conn.execute(statement)
        # 表达式/部分索引通用生成器翻不了，用注册的覆写补上（loader 同款策略）。
        for statement in PARTIAL_INDEX_OVERRIDES.values():
            if any(f"ON {table}(" in statement for table in wanted):
                conn.execute(statement)
        for table in wanted:
            for name, constraint_sql in metadata["foreign_keys"].get(table, []):
                reference = constraint_sql.split("REFERENCES")[1].split("(")[0].strip().strip('"')
                if reference not in loaded:
                    continue
                _add_constraint_if_missing(conn, table, name, constraint_sql)
    return wanted


class _MemoryCatalogTemplate:
    """仅用于生成 PostgreSQL 表结构：借用 memory 控制面的 SQLite 方言 DDL。"""

    def __init__(self, db_path: str) -> None:
        import sqlite3

        from src.memory.catalog import ensure_memory_schema

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            ensure_memory_schema(conn)
        finally:
            conn.close()


class _ResultExportTemplate:
    """仅用于生成 PostgreSQL 表结构：借用导出账本的 SQLite 方言 DDL。

    绕过 ``ResultExportStore.__init__``，避免其在 CONTROL_PLANE_BACKEND=postgres
    时把实例切换为 PG 实现（模板必须始终落在临时 SQLite 文件上）。
    """

    def __init__(self, db_path: str) -> None:
        from src.result_exports.store import ResultExportStore

        store = object.__new__(ResultExportStore)
        store.db_path = db_path
        store._init_db()


def _prefix_for(group: str) -> str:
    return {
        "projects": "PROJECT_STORE",
        "pipeline_documents": "PIPELINE_DOCUMENT",
        "document_authoring": "AUTHORING_STORE",
        "auth": "AUTH_STORE",
        "compatibility": "COMPATIBILITY_STORE",
    }[group]


if __name__ == "__main__":
    raise SystemExit(main())
