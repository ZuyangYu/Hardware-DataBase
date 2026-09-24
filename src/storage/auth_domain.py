"""认证域（auth.db 的账号 + 审计 + 治理杂项）存储收敛接线。

这一组表由多个类写入，必须一起迁移（``AuthService._audit`` 写的是
``AppLogService`` 建的 ``audit_events``）：

- ``AuthService``: users / departments / auth_sessions / login_attempts / knowledge_bases
- ``AppLogService``: audit_events / query_traces / retrieved_evidence
- ``WikiService``: wiki_pages / wiki_page_revisions / wiki_jobs
- ``DocumentAssetService``: doc_assets / doc_asset_versions / doc_asset_events / doc_asset_links
- ``AssetService``: hardware_assets / asset_candidates / asset_evidence
- ``NotificationService``: notifications

``auth.db`` 里其余的表（无）属于别的域；文件类数据（归档/导出字节）不进库。

后端跟随 ``AUTH_STORE_BACKEND``：**默认 sqlite**（不跟随控制面）——这批表
在 PG 里可能还是早期按控制面语义建的形状，必须先跑重建脚本再显式切换：

    uv run python scripts/production_hardening/rebuild_auth_domain.py \
        --dsn "$CONTROL_PLANE_DATABASE_URL"
    # 然后在 .env 里设 AUTH_STORE_BACKEND=postgres 并重启
"""

from __future__ import annotations

PREFIX = "AUTH_STORE"
SQLITE_NAME = "auth.db"
PRIMARY_TABLE = "users"
# 本次迁移的表（全部写入方的并集，顺序 = 建表顺序）。
TABLES: tuple[str, ...] = (
    "departments",
    "users",
    "auth_sessions",
    "login_attempts",
    "knowledge_bases",
    "audit_events",
    "query_traces",
    "retrieved_evidence",
    "wiki_pages",
    "wiki_page_revisions",
    "wiki_jobs",
    "doc_assets",
    "doc_asset_versions",
    "doc_asset_events",
    "doc_asset_links",
    "hardware_assets",
    "asset_candidates",
    "asset_evidence",
    "notifications",
)
REBUILD_COMMAND = (
    "uv run python scripts/production_hardening/rebuild_auth_domain.py"
    " --dsn $CONTROL_PLANE_DATABASE_URL"
)


def domain_classes() -> tuple[type, ...]:
    """共享 auth.db 的 store 类（懒导入避免环）。"""
    from src.core.app_logs import AppLogService
    from src.core.assets import AssetService
    from src.core.auth import AuthService
    from src.core.doc_assets import DocumentAssetService
    from src.core.notifications import NotificationService
    from src.core.wiki import WikiService

    return (
        AuthService,
        AppLogService,
        WikiService,
        DocumentAssetService,
        AssetService,
        NotificationService,
    )


def configure_service(service, explicit_db_path: str | None) -> bool:
    """给认证域的一个 store 配置后端。

    返回 ``True`` 表示调用方继续走原有的 SQLite 初始化（建目录 + ``_init_db``）；
    ``False`` 表示已在 PG（``service.db_path`` 为 ``None``，不要跑 ``_init_db``）。
    """
    import src.settings as settings
    from src.storage import domain_pg

    backend, dsn, effective = domain_pg.resolve_store_backend(
        explicit_db_path=explicit_db_path,
        prefix=PREFIX,
        default_db_path=settings.AUTH_DB_PATH,
    )
    service._backend = backend
    service._dsn = dsn
    if backend == "postgres":
        import os

        domain_pg.ensure_domain_group_ready(
            dsn=dsn,
            store_classes=domain_classes(),
            sqlite_db_path=os.path.join(settings.STORAGE_DIR, SQLITE_NAME)
            if not settings.AUTH_DB_PATH
            else settings.AUTH_DB_PATH,
            primary_table=PRIMARY_TABLE,
            migration_command=REBUILD_COMMAND,
        )
        service.db_path = None
        return False
    service.db_path = effective
    return True


def connect(service):
    """``_connect`` 的 PG 分支；SQLite 分支由各 store 自己保留。"""
    from src.storage.domain_pg import open_pg_connection

    return open_pg_connection(service._dsn)


def assert_shape(dsn: str, table: str, expectations: dict[str, str]) -> None:
    """把形状守卫的提示统一成重建命令。"""
    from src.storage import domain_pg

    domain_pg.assert_mechanical_shape(
        dsn,
        table,
        expectations,
        hint=(
            "认证域的表需要先重建为机械形状（早期按控制面语义建过）：\n"
            f"  {REBUILD_COMMAND}"
        ),
    )
