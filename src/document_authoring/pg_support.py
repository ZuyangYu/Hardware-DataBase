"""document_authoring.db 的存储收敛接线（第 2 期）。

这一组 store 共享同一个 SQLite 库（``document_authoring.db``，46 张表）：
工作单/模板、规划、规划提交、生成会话、评审、修订、任务。收敛到 PG 时
必须**成组**建表，否则跨类外键会被静默跳过。

后端跟随 ``AUTHORING_STORE_BACKEND``（空 = 跟随 ``CONTROL_PLANE_BACKEND``），
显式 ``db_path``（测试用 ``:memory:``）永远走 SQLite。
"""

from __future__ import annotations

import os
from typing import Any

PREFIX = "AUTHORING_STORE"
SQLITE_NAME = "document_authoring.db"
PRIMARY_TABLE = "template_versions"
MIGRATION_COMMAND = (
    "uv run python scripts/production_hardening/migrate_sqlite_to_postgres.py "
    "--group document_authoring --dsn $CONTROL_PLANE_DATABASE_URL"
)


def store_classes() -> tuple[type, ...]:
    """共享 document_authoring.db 的全部 store 类（懒导入避免环）。"""
    from src.document_authoring.generation_sessions import GenerationSessionStore
    from src.document_authoring.planning.store import DocumentPlanningStore
    from src.document_authoring.planning.submissions import (
        DocumentPlanSubmissionStore,
    )
    from src.document_authoring.reviews import DocumentReviewStore
    from src.document_authoring.revisions import ArtifactRevisionStore
    from src.document_authoring.tasks import DocumentTaskStore
    from src.document_authoring.work_order_store import DocumentAuthoringStore

    return (
        DocumentAuthoringStore,
        DocumentPlanningStore,
        DocumentPlanSubmissionStore,
        GenerationSessionStore,
        DocumentReviewStore,
        ArtifactRevisionStore,
        DocumentTaskStore,
    )


def configure(store: Any, explicit_db_path: str | None) -> bool:
    """给一个 store 实例配置后端。

    返回 ``True`` 表示调用方应继续走原有的 SQLite 初始化（建目录 + ``_init_db``）；
    ``False`` 表示已在 PG 上（``store.db_path`` 为 ``None``，不要跑 ``_init_db``）。
    """
    from src.storage import domain_pg
    import src.settings as settings

    backend, dsn, effective = domain_pg.resolve_store_backend(
        explicit_db_path=explicit_db_path,
        prefix=PREFIX,
        default_db_path=os.path.join(settings.STORAGE_DIR, SQLITE_NAME),
    )
    store._backend = backend
    store._dsn = dsn
    if backend == "postgres":
        domain_pg.ensure_domain_group_ready(
            dsn=dsn,
            store_classes=store_classes(),
            sqlite_db_path=os.path.join(settings.STORAGE_DIR, SQLITE_NAME),
            primary_table=PRIMARY_TABLE,
            migration_command=MIGRATION_COMMAND,
        )
        store.db_path = None
        return False
    store.db_path = effective
    return True


def connect(store: Any):
    """``_connect`` 的 PG 分支；SQLite 分支由各 store 自己保留。"""
    from src.storage.domain_pg import open_pg_connection

    return open_pg_connection(store._dsn)


# ---------------------------------------------------------------------------
# 文档生成兼容库（独立文件 document_authoring_compatibility.sqlite）
# ---------------------------------------------------------------------------

COMPAT_PREFIX = "COMPATIBILITY_STORE"
COMPAT_TABLES: tuple[str, ...] = (
    "document_authoring_compatibility_events",
    "document_authoring_compatibility_releases",
    "document_authoring_legacy_backfill_markers",
)
COMPAT_PRIMARY_TABLE = "document_authoring_compatibility_events"
COMPAT_REBUILD_COMMAND = (
    "uv run python scripts/production_hardening/rebuild_auth_domain.py"
    " --domain compatibility --dsn $CONTROL_PLANE_DATABASE_URL"
)


def compat_classes() -> tuple[type, ...]:
    from src.document_authoring.compatibility import DocumentAuthoringCompatibilityStore

    return (DocumentAuthoringCompatibilityStore,)


def configure_compat(store: Any, explicit_db_path: str | None) -> bool:
    """兼容库的后端配置；返回 ``True`` 表示继续走 SQLite 初始化。"""
    import src.settings as settings
    from src.storage import domain_pg

    backend, dsn, effective = domain_pg.resolve_store_backend(
        explicit_db_path=explicit_db_path,
        prefix=COMPAT_PREFIX,
        default_db_path=str(settings.DOCUMENT_AUTHORING_COMPATIBILITY_DB_PATH),
    )
    store._backend = backend
    store._dsn = dsn
    if backend == "postgres":
        domain_pg.ensure_domain_group_ready(
            dsn=dsn,
            store_classes=compat_classes(),
            sqlite_db_path=str(settings.DOCUMENT_AUTHORING_COMPATIBILITY_DB_PATH),
            primary_table=COMPAT_PRIMARY_TABLE,
            migration_command=COMPAT_REBUILD_COMMAND,
        )
        domain_pg.assert_mechanical_shape(
            dsn,
            COMPAT_PRIMARY_TABLE,
            {"occurred_at": "text"},
            hint=(
                "兼容库的表需要先重建为机械形状（早期按控制面语义建过）：\n"
                f"  {COMPAT_REBUILD_COMMAND}"
            ),
        )
        store.db_path = None
        return False
    store.db_path = effective
    return True


def compat_connect(store: Any):
    from src.storage.domain_pg import open_pg_connection

    return open_pg_connection(store._dsn)
