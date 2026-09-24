"""Control-plane concurrency benchmark (v3 §4.2/§4.3, PR-4 evidence).

Measures the PostgreSQL execution store's concurrency contract with no model
or network in the loop:

1. claim throughput/latency with N worker threads across independent sessions
   (claim -> complete -> next), including duplicate-terminal assertions;
2. single-owner guarantee under 8-way contention on one Turn;
3. hot-session isolation: while one session's ``conversation_execution_state``
   row is locked by an open transaction, claims for other sessions must
   complete immediately (``SKIP LOCKED`` + short transactions).

A disposable database is created and dropped by this script.

Example::

    HDB_TEST_POSTGRES_DSN=postgresql://hdb:hdbtest@127.0.0.1:55432/postgres \
        python scripts/production_hardening/control_plane_benchmark.py \
        --output /tmp/control-plane-benchmark.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.storage.control_plane import apply_schema  # noqa: E402
from src.storage.control_plane.postgres import PostgresExecutionStore  # noqa: E402


def _dsn_with_database(dsn: str, name: str) -> str:
    params = conninfo_to_dict(dsn)
    params["dbname"] = name
    return make_conninfo(**params)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _stats(values: list[float]) -> dict:
    return {
        "count": len(values),
        "p50_ms": round(_percentile(values, 0.50) * 1000, 2),
        "p95_ms": round(_percentile(values, 0.95) * 1000, 2),
        "p99_ms": round(_percentile(values, 0.99) * 1000, 2),
        "mean_ms": round((statistics.fmean(values) if values else 0.0) * 1000, 2),
        "max_ms": round((max(values) if values else 0.0) * 1000, 2),
    }


def _claim_and_complete(
    store: PostgresExecutionStore,
    turn_id: str,
    worker_id: str,
    latencies: list[float],
    errors: list[str],
    terminals: list[str],
) -> bool:
    started = time.perf_counter()
    try:
        claimed = store.claim_turn(turn_id, worker_id)
    except Exception as exc:  # noqa: BLE001 - aggregated below
        errors.append(f"claim:{exc}")
        return False
    latencies.append(time.perf_counter() - started)
    if claimed.status != "claimed":
        return False
    try:
        completion = store.complete_turn(
            1,
            turn_id,
            "benchmark answer",
            {"status": "ok"},
            worker_id=worker_id,
            fencing_token=claimed.turn.fencing_token,
            expected_generation=claimed.turn.session_generation,
            terminal_events_builder=lambda turn: [
                ("done", {"answer": turn.answer}),
                ("turn_end", {"status": "completed"}),
            ],
        )
        terminals.extend(event.event_type for event in completion.terminal_events)
    except Exception as exc:  # noqa: BLE001 - aggregated below
        errors.append(f"complete:{exc}")
        return False
    return True


def _run_claim_throughput(store, turn_count: int, workers: int) -> dict:
    # One session per Turn keeps the measurement on the cross-session path:
    # same-session turns intentionally serialise (or overlap, per the legacy
    # switch) and are covered by the single-owner/hot-session scenarios.
    turn_ids = [
        store.create_turn(
            1, store.create_session(1, "__general__").id, f"bench-{index}"
        ).id
        for index in range(turn_count)
    ]
    queue = list(turn_ids)
    lock = threading.Lock()
    latencies: list[float] = []
    errors: list[str] = []
    terminals: list[str] = []
    claims: list[int] = [0]

    def worker(worker_id: str) -> None:
        while True:
            with lock:
                if not queue:
                    return
                turn_id = queue.pop(0)
            deadline = time.perf_counter() + 30.0
            while time.perf_counter() < deadline:
                if _claim_and_complete(store, turn_id, worker_id, latencies, errors, terminals):
                    with lock:
                        claims[0] += 1
                    break
                time.sleep(0.005)
            else:
                errors.append(f"timeout:{turn_id}")

    started = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(f"bench-{i}",)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    duration = max(0.001, time.perf_counter() - started)
    return {
        "turns": len(turn_ids),
        "workers": workers,
        "claimed": claims[0],
        "duration_s": round(duration, 3),
        "claims_per_second": round(claims[0] / duration, 1),
        "claim_latency": _stats(latencies),
        "duplicate_terminal_events": len(terminals) - 2 * claims[0],
        "errors": errors[:5],
    }


def _run_single_owner(store) -> dict:
    session = store.create_session(1, "__general__").id
    turn = store.create_turn(1, session, "single-owner")
    barrier = threading.Barrier(8)
    results: list[str] = []
    lock = threading.Lock()

    def claim(worker_id: str) -> None:
        barrier.wait(timeout=10)
        outcome = store.claim_turn(turn.id, worker_id)
        with lock:
            results.append(outcome.status)

    threads = [threading.Thread(target=claim, args=(f"owner-{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return {
        "contenders": 8,
        "claimed": results.count("claimed"),
        "other": sorted(status for status in results if status != "claimed"),
    }


def _run_hot_session_isolation(store, dsn: str) -> dict:
    hot_session = store.create_session(1, "__general__").id
    # Materialise the session's execution_state row (claims create it on first
    # use); the primer turn is released so the lock below hits a real row.
    primer = store.create_turn(1, hot_session, "hot-primer")
    primed = store.claim_turn(primer.id, "hot-primer")
    if primed.status == "claimed":
        store.release_turn_ownership(
            primer.id, "hot-primer", fencing_token=primed.turn.fencing_token
        )
    hot_turn = store.create_turn(1, hot_session, "hot-session")
    other_turns = []
    for index in range(10):
        session = store.create_session(1, "__general__").id
        other_turns.append(store.create_turn(1, session, f"cool-{index}").id)

    hold_released = threading.Event()
    hold_locked = threading.Event()

    def hold_row_lock() -> None:
        with psycopg.connect(dsn, autocommit=False) as conn:
            conn.execute(
                "SELECT session_id FROM conversation_execution_state"
                " WHERE session_id = %s FOR UPDATE",
                (hot_session,),
            )
            hold_locked.set()
            hold_released.wait(30)

    holder = threading.Thread(target=hold_row_lock)
    holder.start()
    hold_locked.wait(10)
    try:
        hot_latencies = []
        hot_status = []
        for _ in range(3):
            start = time.perf_counter()
            outcome = store.claim_turn(hot_turn.id, "hot-claimer")
            hot_latencies.append(time.perf_counter() - start)
            hot_status.append(outcome.status)
        cool_latencies = []
        cool_claimed = 0
        for turn_id in other_turns:
            start = time.perf_counter()
            outcome = store.claim_turn(turn_id, "cool-claimer")
            cool_latencies.append(time.perf_counter() - start)
            if outcome.status == "claimed":
                cool_claimed += 1
                store.release_turn_ownership(
                    turn_id, "cool-claimer", fencing_token=outcome.turn.fencing_token
                )
    finally:
        hold_released.set()
        holder.join(10)
    recovery_started = time.perf_counter()
    recovery = store.claim_turn(hot_turn.id, "hot-recovery")
    recovery_latency = time.perf_counter() - recovery_started
    if recovery.status == "claimed":
        store.release_turn_ownership(
            hot_turn.id, "hot-recovery", fencing_token=recovery.turn.fencing_token
        )
    return {
        "hot_session_claims": {
            "statuses": hot_status,
            "latency": _stats(hot_latencies),
        },
        "other_session_claims": {
            "claimed": cool_claimed,
            "total": len(other_turns),
            "latency": _stats(cool_latencies),
        },
        "hot_session_recovery": {
            "status": recovery.status,
            "latency_ms": round(recovery_latency * 1000, 2),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get("HDB_TEST_POSTGRES_DSN", ""),
        help="PostgreSQL DSN (defaults to HDB_TEST_POSTGRES_DSN)",
    )
    parser.add_argument("--sessions", type=int, default=8)
    parser.add_argument("--turns-per-session", type=int, default=20)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    if not args.dsn:
        print("HDB_TEST_POSTGRES_DSN or --dsn is required", file=sys.stderr)
        return 2

    database = f"hdb_bench_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(_dsn_with_database(args.dsn, "postgres"), autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        admin.close()
    dsn = _dsn_with_database(args.dsn, database)
    report: dict = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "database": database,
        "config": {
            "sessions": args.sessions,
            "turns_per_session": args.turns_per_session,
            "workers": args.workers,
        },
    }
    store = PostgresExecutionStore(dsn, max_size=max(4, args.workers + 2))
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            apply_schema(conn)
            # create_session resolves a missing department from the users
            # table; the benchmark does not need auth, only the lookup.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                " id BIGINT PRIMARY KEY, username TEXT UNIQUE,"
                " department_id BIGINT, is_active BOOLEAN DEFAULT TRUE)"
            )
            conn.execute(
                "INSERT INTO users (id, username, department_id, is_active)"
                " VALUES (1, 'control-plane-bench', NULL, TRUE)"
                " ON CONFLICT (id) DO NOTHING"
            )
        report["claim_throughput"] = _run_claim_throughput(
            store,
            max(1, args.sessions * args.turns_per_session),
            args.workers,
        )
        report["single_owner"] = _run_single_owner(store)
        report["hot_session_isolation"] = _run_hot_session_isolation(store, dsn)
        report["ok"] = (
            report["claim_throughput"]["claimed"]
            == report["claim_throughput"]["turns"]
            and report["claim_throughput"]["duplicate_terminal_events"] == 0
            and not report["claim_throughput"]["errors"]
            and report["single_owner"]["claimed"] == 1
            and report["hot_session_isolation"]["other_session_claims"]["claimed"]
            == report["hot_session_isolation"]["other_session_claims"]["total"]
        )
    finally:
        store.close()
        admin = psycopg.connect(_dsn_with_database(args.dsn, "postgres"), autocommit=True)
        try:
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        finally:
            admin.close()

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"report={output}")
    else:
        print(text)
    print(f"ok={report['ok']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
