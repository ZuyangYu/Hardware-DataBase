"""一次性存储初始化/校验（容器编排的 ``migrate`` 门槛）。

解决的问题：控制面 schema 是"应用永不隐式建表"（fail-closed），所以全新
PostgreSQL 上直接起 API 会因为缺表而拒绝启动。本模块把"建表 + 记录版本 +
按域建表"收敛成一次幂等调用，容器编排里作为 api/worker 的前置门槛：

    python -m src.storage.bootstrap

行为：
1. ``CONTROL_PLANE_BACKEND=postgres`` 时：
   - 缺 ``control_plane_schema`` → ``apply_schema`` + ``record_schema_version``；
   - 已存在 → ``assert_schema_compatible``（版本不符即失败，绝不静默放行）；
2. 逐域幂等建表（projects / pipeline_documents / document_authoring /
   认证域 / 兼容库），机械映射，表结构由各 store 自己的 SQLite bootstrap 生成；
3. 认证域额外做形状校验（早期按控制面语义建过的库会在此 fail-closed 并提示
   重建命令）。

可重复执行；失败以非零退出码结束，让 ``depends_on:
service_completed_successfully`` 拦住后续服务。
"""

from __future__ import annotations

import sys


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


def main(argv: list[str] | None = None) -> int:
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
