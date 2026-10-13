"""Independent shared-ledger evaluation worker (EVAL_EXECUTION_MODE=worker)."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import uuid
from pathlib import Path

import src.settings as settings

from .execution import EvaluationLeaseLost
from .run_control import EvaluationRunController, RunStateStore
from .service import EvaluationService


def run_pending(controller: EvaluationRunController) -> int:
    completed = 0
    for run_id in controller.ownership.pending_ids():
        store = controller._store(run_id)
        try:
            with store._lock:
                state = store.load()
                claim = controller.ownership.claim(run_id)
            if state.status not in {"queued", "running", "pause_requested", "cancel_requested"}:
                controller.ownership.release(claim)
                continue
            # A crashed scoring owner leaves per-cell checkpoints. Reuse them.
            if state.status == "running":
                from .execution import execution_context

                with execution_context(controller.ownership, claim):
                    store.mutate(lambda current: RunStateStore._with_update(
                        current, evaluation_config={**current.evaluation_config, "scoring_resume": "continue"},
                    ))
            controller._execute_claimed(claim)
            completed += 1
        except (RuntimeError, EvaluationLeaseLost):
            # A competing worker may have claimed after pending_ids().
            logging.getLogger(__name__).info("evaluation claim unavailable: %s", run_id)
        except Exception:
            logging.getLogger(__name__).exception("evaluation worker failed: %s", run_id)
    return completed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.getenv("EVAL_OUTPUT_ROOT", str(Path(settings.STORAGE_DIR) / "evaluations")))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    controller = EvaluationRunController(EvaluationService, args.root)
    from src.observability import init_observability, shutdown_observability
    from src.observability.worker_registry import WorkerHeartbeat

    init_observability(
        "hardware-database-evaluation-worker",
        service_version=settings.OBS_SERVICE_VERSION,
        environment=settings.OBS_ENVIRONMENT,
    )
    try:
        with WorkerHeartbeat(f"evaluation-{uuid.uuid4().hex}", "evaluation"):
            while not stop.is_set():
                run_pending(controller)
                if args.once:
                    break
                stop.wait(2)
    finally:
        shutdown_observability()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
