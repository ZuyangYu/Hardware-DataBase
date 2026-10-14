"""Independent fenced quality outbox consumer (no model calls in P1)."""
from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import os
import time
from pathlib import Path
import uuid
from contextlib import closing

import src.settings as settings
from .phoenix_sync import PhoenixAnnotationClient, PhoenixRejected, PhoenixRetry
from .store import QualityLeaseLost, QualityStore


def process_one(store: QualityStore, owner: str, client=None) -> bool:
    claim = store.claim(owner)
    if claim is None:
        return False
    try:
        if claim["kind"] == "collect":
            store.collect(claim)
        elif claim["kind"] == "phoenix_annotation":
            with closing(store.connect()) as conn:
                row = conn.execute("SELECT manifest_json FROM quality_snapshots WHERE snapshot_id = ?", (claim["snapshot_id"],)).fetchone()
            if row is None:
                return True  # A deleted source cascaded the job away.
            manifest = json.loads(row[0])
            span_id = manifest.get("result_span_id") or ""
            if not span_id or manifest.get("sampling_status") == "not_sampled":
                store.finish(claim, status="no_span")
            elif client is None and (not settings.QUALITY_PHOENIX_SYNC_ENABLED or not settings.QUALITY_PHOENIX_ENDPOINT):
                store.finish(claim, status="disabled")
            else:
                (client or PhoenixAnnotationClient()).sync(span_id, json.loads(claim["payload_json"]))
                store.finish(claim)
        else:
            store.finish(claim, status="failed", error="unsupported_job_kind")
    except PhoenixRejected as exc:
        store.finish(claim, status="failed", error=str(exc))
    except PhoenixRetry as exc:
        attempts = int(claim["attempts"])
        store.finish(claim, status="pending" if attempts < settings.QUALITY_PHOENIX_MAX_ATTEMPTS else "failed",
                     retry_after=min(3600, 2 ** min(attempts, 12)), error=str(exc))
    except QualityLeaseLost:
        logging.getLogger(__name__).info("quality ownership expired: %s", claim["job_id"])
    except Exception as exc:
        # Store failure leaves a running job recoverable after its lease expires.
        logging.getLogger(__name__).error("quality job failed: %s (%s)", claim["job_id"], type(exc).__name__)
        try:
            store.finish(claim, status="pending", retry_after=30, error=type(exc).__name__)
        except QualityLeaseLost:
            pass
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Drain currently available jobs and exit")
    parser.add_argument("--backfill", action="store_true", help="Explicitly capture up to 100 older completed turns")
    parser.add_argument("--requeue-projections", action="store_true", help="Retry disabled/failed Phoenix jobs with the same identifiers")
    args = parser.parse_args(argv)
    if settings.CONTROL_PLANE_BACKEND == "sqlite":
        from src.core.conversation import ConversationService

        ConversationService()
    store = QualityStore()
    if args.backfill:
        store.backfill()
    if args.requeue_projections:
        with store.transaction() as conn:
            conn.execute("UPDATE quality_outbox SET status = 'pending', available_at = 0, attempts = 0 WHERE kind = 'phoenix_annotation' AND status IN ('disabled', 'failed')")
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    from src.observability import init_observability, shutdown_observability
    from src.observability.worker_registry import WorkerHeartbeat

    init_observability("hardware-database-quality-worker", service_version=settings.OBS_SERVICE_VERSION, environment=settings.OBS_ENVIRONMENT)
    owner = f"quality-{uuid.uuid4().hex}"
    try:
        with WorkerHeartbeat(owner, "quality"):
            next_reconcile = 0
            while not stop.is_set():
                if time.monotonic() >= next_reconcile:
                    from datetime import datetime, timezone
                    from src.observability.metrics import set_active_state, set_queue_state
                    from src.observability.worker_registry import list_workers

                    required = {role.strip() for role in os.getenv("HDB_REQUIRED_WORKER_ROLES", "").split(",") if role.strip()}
                    present = {row["worker_type"] for row in list_workers()}
                    set_active_state("hdb.worker.required_roles_missing", "Required worker roles without a fresh durable heartbeat", len(required-present))
                    with closing(store.connect()) as conn:
                        for kind, label in (("collect", "quality_collect"), ("phoenix_annotation", "quality_phoenix")):
                            row = conn.execute("SELECT COUNT(*) AS depth, MIN(created_at) AS oldest FROM quality_outbox WHERE kind=? AND status IN ('pending', 'running')", (kind,)).fetchone()
                            oldest = datetime.fromisoformat(str(row["oldest"]).replace("Z", "+00:00")) if row["oldest"] else None
                            set_queue_state(label, depth=int(row["depth"]), oldest_age_s=max(0, (datetime.now(timezone.utc)-oldest).total_seconds()) if oldest else 0)
                    from src.evaluation.run_control import EvaluationRunController
                    from .evaluation_bridge import reconcile_invalid_runs

                    root = Path(os.getenv("EVAL_OUTPUT_ROOT", str(Path(settings.STORAGE_DIR) / "evaluations")))
                    if root.is_dir():
                        try:
                            reconcile_invalid_runs(EvaluationRunController(lambda: None, root))
                        except Exception as exc:
                            logging.getLogger(__name__).error("quality derived-run reconcile failed (%s)", type(exc).__name__)
                    if os.getenv("HDB_CHAT_SLO_POLICY"):
                        from src.observability.slo import ChatSloPolicy, ChatSloStore, publish_windows

                        try:
                            slo = ChatSloStore()
                            slo.reconcile(ChatSloPolicy.model_validate_json(os.environ["HDB_CHAT_SLO_POLICY"]))
                            publish_windows(slo.windows())
                        except Exception as exc:
                            logging.getLogger(__name__).error("chat SLO reconcile failed (%s)", type(exc).__name__)
                    from src.costs.budgets import publish_statuses
                    from src.costs.store import CostStore
                    from src.core.auth import AuthUser, ROLE_SYSTEM_ADMIN

                    try:
                        publish_statuses(CostStore().budget_statuses(AuthUser(0, "budget-worker", ROLE_SYSTEM_ADMIN, True), active_only=True))
                    except Exception as exc:
                        logging.getLogger(__name__).error("cost budget reconcile failed (%s)", type(exc).__name__)
                    next_reconcile = time.monotonic() + 30
                available = process_one(store, owner)
                if args.once and not available:
                    break
                if not available:
                    stop.wait(2)
    finally:
        shutdown_observability()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
