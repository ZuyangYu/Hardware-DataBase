"""FastAPI application factory and server entry point.

This is the future backend. It holds no business logic -- every route
delegates to the shared :class:`AppPipeline` and the existing auth/context
modules. The Streamlit app and any later frontend become HTTP clients of it.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from src.api.errors import install_error_handlers
from src.api.deps import require_system_admin
from src.api.routes import (
    artifacts,
    assets,
    attachments,
    auth,
    config,
    costs,
    conversations,
    departments,
    doc_assets,
    evaluation,
    files,
    governance,
    kb_assign,
    kbs,
    logs,
    memories,
    notifications,
    parse_tasks,
    plugins,
    query,
    quality,
    quality_workflow,
    quality_tickets,
    exports,
    status,
    sandbox,
    structured,
    tasks,
    upload,
    users,
    wiki,
    workspaces,
)
from src.observability import (
    attach_log_handler,
    init_observability,
    instrument_fastapi,
    shutdown_observability,
)
from src.observability.health import check_dependencies, check_live, check_ready
from src.api.worker_supervisor import WorkerSupervisor


def _should_spawn_worker() -> bool:
    """Whether the API server should auto-spawn the background parse worker.

    On is the default so a single `hardware-database-server` command also runs
    the Excel-parse worker that the spreadsheet pipeline depends on. Multi-host
    deployments (N API instances) should set HDB_API_SPAWN_WORKER=0 and run the
    worker as a dedicated service instead, to avoid N workers competing on the
    shared SQLite claim queue.
    """
    return os.getenv("HDB_API_SPAWN_WORKER", "1").lower() not in {"0", "false", "no", "off"}


def _should_spawn_memory_worker() -> bool:
    """Keep Memory reflection in a separately deployable process.

    It is opt-in for the API process because a production deployment normally
    runs ``hardware-database-memory-worker`` as its own low-priority service.
    """
    return os.getenv("HDB_API_SPAWN_MEMORY_WORKER", "0").lower() not in {"0", "false", "no", "off"}


def _harden_storage_permissions() -> None:
    """Converge .env / storage/*.db to owner-only mode at boot.

    .env holds provider API keys; storage/*.db (plus -wal/-shm) hold raw user
    queries and evidence text. Default umasks leave them 0664/0644, readable
    by every local account — deployment shape is single-user, so 0600 is safe.
    """
    import src.settings

    for path in [src.settings.ENV_FILE_PATH] + [
        os.path.join(src.settings.STORAGE_DIR, name)
        for name in (os.listdir(src.settings.STORAGE_DIR) if os.path.isdir(src.settings.STORAGE_DIR) else [])
        if name.endswith((".db", "-wal", "-shm"))
    ]:
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            print(f"[hardware-database] chmod 600 failed for {path}: {exc}")


def _prune_query_traces_once(retention_days: int = 30) -> None:
    """Best-effort retention sweep of query_traces / retrieved_evidence at boot."""
    from src.core.app_logs import AppLogService

    try:
        traces_deleted, evidence_deleted = AppLogService().prune_query_traces(retention_days)
        if traces_deleted or evidence_deleted:
            print(f"[hardware-database] pruned {traces_deleted} traces / {evidence_deleted} evidence rows (>{retention_days}d)")
    except Exception as exc:
        print(f"[hardware-database] query trace prune skipped: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Co-manage the background parse worker with the API server process.

    The spreadsheet pipeline requires a standalone worker to claim and index
    queued xlsx records (see ``src/pipelines/runtime.py``). We spawn it here as
    a child subprocess so a single backend command also runs Excel parsing, while
    keeping it a separate OS process (crash isolation + DB-level claim queue).
    """
    import src.settings

    obsolete = src.settings.obsolete_env_keys()
    if obsolete:
        logging.getLogger(__name__).warning(
            "配置中包含已废弃/内置项，可删除: %s", ", ".join(obsolete)
        )

    # P0-008: fail fast on provider selections that cannot run; never fall back
    # to a silently created local database.
    src.settings.validate_provider_configuration()
    from pathlib import Path
    from src.evaluation.execution import EvaluationExecutionLedger

    EvaluationExecutionLedger(Path(src.settings.STORAGE_DIR) / "evaluations")
    from src.quality.store import QualityStore

    if src.settings.CONTROL_PLANE_BACKEND == "sqlite":
        from src.core.conversation import ConversationService

        ConversationService()
    QualityStore()
    from src.costs.store import CostStore

    CostStore()
    from src.quality.workflow import WorkflowStore

    WorkflowStore()
    # Redis 是硬依赖（REDIS_REQUIRED=true，默认）：未配置或不可达时拒绝启动，
    # 不提供"降级成 DB 轮询"的运行形态。
    from src.core.redis_client import assert_redis_ready

    assert_redis_ready(purpose="API 启动")
    # uvicorn 的 dictConfig 会重建它配置的 logger 的 handler，把导入期挂上的 OTel
    # 日志 handler 冲掉；lifespan 运行时再挂一次，API 的日志才进得了 Loki。
    attach_log_handler()
    # 检查点表只由 migrate 建好；缺失或版本不符时拒绝启动（fail-closed，不并发建表）。
    from src.agents.checkpoints.postgres_provider import (
        CheckpointSchemaNotReady,
        verify_configured_checkpoint_schemas,
    )

    try:
        verify_configured_checkpoint_schemas()
    except CheckpointSchemaNotReady:
        # 异常文本自带修复命令：traceback 第一行就是可执行的指引。
        raise
    # P0-006: deployment-scope quota maintenance (reap/escalate uncertain).
    try:
        from src.core.concurrency import start_governor_maintenance

        start_governor_maintenance()
    except Exception as exc:  # pragma: no cover - governance must not block boot
        print(f"[hardware-database] governor maintenance disabled: {exc}")
    # P0-007: own the shared retrieval executor lifecycle for this process.
    try:
        from src.agents.tools.retrieval_executor import initialize_retrieval_executor

        initialize_retrieval_executor()
    except Exception as exc:  # pragma: no cover
        print(f"[hardware-database] shared retrieval executor unavailable: {type(exc).__name__}")
    _harden_storage_permissions()
    _prune_query_traces_once()

    modules = []
    if _should_spawn_worker():
        modules.append("src.workers.main")
    if os.getenv("HDB_API_SPAWN_EXPORT_WORKER", "1").lower() not in {"0", "false", "no", "off"}:
        modules.append("src.result_exports.worker")
    if _should_spawn_memory_worker():
        modules.append("src.memory.worker")
    if os.getenv("HDB_API_SPAWN_QUALITY_WORKER", "1").lower() not in {"0", "false", "no", "off"}:
        modules.append("src.quality.worker")
    supervisor = WorkerSupervisor(modules, src.settings.BASE_DIR)
    sandbox_dispatcher = None
    sandbox_reconciler = None
    sandbox_client = None
    if bool(getattr(src.settings, "SANDBOX_ENABLED", False)) or bool(
        getattr(src.settings, "SANDBOX_GATEWAY_ENABLED", False)
    ):
        # Constructing this dependency never opens a remote connection.  It
        # only succeeds when deployment injected an authenticated identity
        # provider; otherwise the optional loops stay stopped and health can
        # report the closed/degraded state without weakening the API boundary.
        try:
            from src.sandbox.dependencies import get_service_client

            sandbox_client = get_service_client()
            if sandbox_client is not None and bool(getattr(src.settings, "SANDBOX_ENABLED", False)):
                from src.sandbox.artifact_broker import ArtifactBroker
                from src.sandbox.artifacts import build_registered_scanner
                from src.sandbox.catalog import SandboxCatalog
                from src.sandbox.reconciler import SandboxReconciler, SandboxReconcilerLoop
                from src.sandbox.service import SandboxDispatcherLoop, SandboxExecutionAdapter
                from src.sandbox.task_store import SandboxTaskStore

                sandbox_store = SandboxTaskStore()
                sandbox_catalog = SandboxCatalog(sandbox_store.db_path)
                sandbox_broker = ArtifactBroker(scanner=build_registered_scanner())
                sandbox_adapter = SandboxExecutionAdapter(
                    store=sandbox_store,
                    catalog=sandbox_catalog,
                    broker=sandbox_broker,
                    client=sandbox_client,
                )
                sandbox_dispatcher = SandboxDispatcherLoop(
                    sandbox_adapter,
                    interval_seconds=float(
                        getattr(src.settings, "SANDBOX_DISPATCH_INTERVAL_SECONDS", 2.0)
                    ),
                )
                sandbox_reconciler = SandboxReconcilerLoop(
                    SandboxReconciler(
                        store=sandbox_store,
                        client=sandbox_client,
                        broker=sandbox_broker,
                    ),
                    interval_seconds=float(
                        getattr(src.settings, "SANDBOX_RECONCILE_INTERVAL_SECONDS", 30.0)
                    ),
                )
                sandbox_dispatcher.start()
                sandbox_reconciler.start()
                print("[hardware-database] sandbox dispatcher/reconciler started")
            elif bool(getattr(src.settings, "SANDBOX_ENABLED", False)):
                print("[hardware-database] sandbox loops not started: authenticated client is unavailable")
        except Exception as exc:  # pragma: no cover - startup remains fail-closed
            print(f"[hardware-database] sandbox loops not started: {exc}")
    try:
        supervisor.start()
        yield
    finally:
        supervisor.stop()
        from src.costs.sink import shutdown_sinks

        shutdown_sinks()
        if sandbox_dispatcher is not None:
            sandbox_dispatcher.stop()
        if sandbox_reconciler is not None:
            sandbox_reconciler.stop()
        try:
            from src.sandbox.dependencies import close_service_client

            close_service_client()
        except Exception:  # pragma: no cover - teardown is best-effort
            pass
        try:
            from src.agents.tools.retrieval_executor import reset_retrieval_executor

            reset_retrieval_executor(
                deadline_seconds=float(
                    getattr(src.settings, "RETRIEVAL_SHUTDOWN_DEADLINE_SECONDS", 10)
                )
            )
        except Exception:  # pragma: no cover - teardown is best-effort
            pass
        try:
            from src.agents.mcp.client import reset_mcp_runtime

            reset_mcp_runtime()
        except Exception:  # pragma: no cover - teardown is best-effort
            pass
        shutdown_observability()


def create_app() -> FastAPI:
    import src.settings as settings

    init_observability(
        "hardware-database-api",
        service_version=settings.OBS_SERVICE_VERSION,
        environment=settings.OBS_ENVIRONMENT,
    )
    app = FastAPI(title="Hardware DataBase API", version="0.1.0", lifespan=lifespan)
    instrument_fastapi(app)
    install_error_handlers(app)

    # CORS: allow browser-based clients and agent tools to reach the API.
    # allow_origins=["*"] + allow_credentials=True is rejected by browsers, so
    # origins are configurable via HDB_API_CORS_ORIGINS (comma-separated). When
    # unset we default to local dev origins; credentials are enabled to support
    # cookie/header-based sessions from the future frontend.
    cors_env = os.getenv("HDB_API_CORS_ORIGINS", "")
    if cors_env.strip():
        origins = [o.strip() for o in cors_env.split(",") if o.strip()]
    else:
        origins = [
            "http://localhost:8501",
            "http://127.0.0.1:8501",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:5175",
            "http://127.0.0.1:5175",
            "http://localhost:5176",
            "http://127.0.0.1:5176",
        ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    api_v1 = "/api/v1"
    app.include_router(auth.router, prefix=api_v1)
    app.include_router(assets.router, prefix=api_v1)
    app.include_router(doc_assets.router, prefix=api_v1)
    app.include_router(wiki.router, prefix=api_v1)
    app.include_router(attachments.router, prefix=api_v1)
    app.include_router(conversations.router, prefix=api_v1)
    app.include_router(workspaces.router, prefix=api_v1)
    app.include_router(kbs.router, prefix=api_v1)
    app.include_router(files.router, prefix=api_v1)
    app.include_router(parse_tasks.router, prefix=api_v1)
    app.include_router(query.router, prefix=api_v1)
    app.include_router(tasks.router, prefix=api_v1)
    app.include_router(artifacts.router, prefix=api_v1)
    app.include_router(quality.router, prefix=api_v1)
    app.include_router(costs.router, prefix=api_v1)
    app.include_router(quality_workflow.router, prefix=api_v1)
    app.include_router(quality_tickets.router, prefix=api_v1)
    app.include_router(exports.router, prefix=api_v1)
    app.include_router(upload.router, prefix=api_v1)
    app.include_router(users.router, prefix=api_v1)
    app.include_router(departments.router, prefix=api_v1)
    app.include_router(kb_assign.router, prefix=api_v1)
    app.include_router(governance.router, prefix=api_v1)
    app.include_router(config.router, prefix=api_v1)
    app.include_router(logs.router, prefix=api_v1)
    app.include_router(memories.router, prefix=api_v1)
    app.include_router(notifications.router, prefix=api_v1)
    app.include_router(plugins.router, prefix=api_v1)
    app.include_router(status.router, prefix=api_v1)
    app.include_router(sandbox.router, prefix=api_v1)
    app.include_router(evaluation.router, prefix=api_v1)
    app.include_router(structured.router, prefix=api_v1)

    @app.get("/health", tags=["health"])
    def health() -> dict:
        return {"ok": True, **check_live()}

    @app.get("/health/live", tags=["health"])
    def health_live() -> dict:
        return check_live()

    @app.get("/health/ready", tags=["health"])
    def health_ready() -> JSONResponse:
        result = check_ready()
        # Detailed errors, roles and filesystem paths belong to /system/status.
        public = {
            "status": result["status"],
            "config_revision": result.get("config_revision", ""),
            "dependencies": {
                name: {"status": item["status"]}
                for name, item in result.get("dependencies", {}).items()
            },
        }
        return JSONResponse(public, status_code=200 if result["status"] == "ready" else 503)

    @app.get("/health/dependencies", tags=["health"])
    def health_dependencies(_viewer=Depends(require_system_admin)) -> dict:
        return check_dependencies()

    return app


app = create_app()


def main() -> None:
    """Run the API server (console script `hardware-database-server`)."""
    import uvicorn

    host = os.getenv("HDB_API_HOST", "127.0.0.1")
    port = int(os.getenv("HDB_API_PORT", "8001"))
    uvicorn.run(
        "src.api.app:app", host=host, port=port,
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
