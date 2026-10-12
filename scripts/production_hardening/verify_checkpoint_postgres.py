"""Validate the PostgreSQL agent checkpointer against a real database (P1-001).

Checks: setup, put/get parity, fork copy with pending writes, delete isolation
and thread cleanup.  Run before switching AGENT_CHECKPOINT_BACKEND=postgres.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from langgraph.graph import END, START, StateGraph  # noqa: E402

from src.agents.checkpoints.fork import (  # noqa: E402
    copy_checkpoint_namespace,
    copy_latest_checkpoint,
)
from src.agents.checkpoints.postgres_provider import PostgresCheckpointerProvider  # noqa: E402


def _build_app(saver):
    def append(state: dict) -> dict:
        return {"log": [*state.get("log", []), "node"]}

    builder = StateGraph(dict)
    builder.add_node("append", append)
    builder.add_edge(START, "append")
    builder.add_edge("append", END)
    return builder.compile(checkpointer=saver)


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="PostgreSQL connection URL")
    parser.add_argument("--report", default="")
    parser.add_argument(
        "--setup",
        action="store_true",
        help="演练库尚未建检查点表时执行 DDL（运行期应用从不建表）",
    )
    args = parser.parse_args(argv)

    run_id = uuid.uuid4().hex[:8]
    thread = f"verify-{run_id}"
    fork_thread = f"verify-{run_id}::attempt-2"
    report: dict[str, object] = {"dsn": args.dsn, "thread": thread, "checks": {}}
    provider = PostgresCheckpointerProvider(args.dsn, setup=args.setup)
    try:
        saver = provider.create()
        app = _build_app(saver)

        app.invoke({"log": ["u1"]}, _config(thread))
        state = app.get_state(_config(thread))
        report["checks"]["put_get"] = state.values == {"log": ["u1", "node"]}

        source_tuple = saver.get_tuple(_config(thread))
        saver.put_writes(source_tuple.config, [("log", ["pending"])], task_id="task-1")
        copied = copy_latest_checkpoint(saver, thread, fork_thread, copy_pending_writes=True)
        target_tuple = saver.get_tuple(_config(fork_thread))
        report["checks"]["fork_state"] = (
            copied is not None
            and target_tuple is not None
            and [tuple(item) for item in (target_tuple.pending_writes or [])]
            == [("task-1", "log", ["pending"])]
        )
        app.invoke({"log": ["u2"]}, _config(fork_thread))
        report["checks"]["fork_isolated"] = app.get_state(_config(thread)).values == {"log": ["u1", "node"]}

        deleted = saver.get_tuple(_config(fork_thread))
        saver.delete_thread(fork_thread)
        report["checks"]["delete_isolated"] = (
            deleted is not None and saver.get_tuple(_config(fork_thread)) is None
        )

        second = copy_checkpoint_namespace(saver, _config(thread), f"verify-{run_id}::attempt-3")
        report["checks"]["explicit_namespace_copy"] = second is not None

        saver.delete_thread(thread)
        report["checks"]["cleanup"] = saver.get_tuple(_config(thread)) is None
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["ok"] = False
    else:
        report["ok"] = all(report["checks"].values())
    finally:
        provider.close()

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"report={output}")
    else:
        print(text)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
