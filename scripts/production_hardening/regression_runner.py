"""Run the fixed production-hardening regression set and emit a baseline artifact.

The runner is deterministic: it records the git commit, hashes the frozen
datasets, executes the fixed pytest set and writes one JSON baseline per run.
It never calls an LLM or a downstream service; model-based scoring stays in the
RAGAS evaluation subsystem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_TESTS: tuple[str, ...] = (
    "tests/test_document_search_batch.py",
    "tests/test_conversation_action_executor.py",
    "tests/test_worker_fairness.py",
    "tests/test_chat_attachments.py",
    "tests/evaluation/test_hardware_metrics.py",
)

# Delivered by the production-hardening tasks; included automatically once present.
TASK_TESTS: tuple[str, ...] = (
    "tests/test_chat_turn_sequence.py",
    "tests/test_checkpoint_fork_restore.py",
    "tests/test_chat_turn_claim_fencing.py",
    "tests/test_waiting_turn_timeout.py",
    "tests/test_worker_roles.py",
    "tests/test_resource_governor.py",
    "tests/test_shared_retrieval_executor.py",
    "tests/test_checkpoint_provider.py",
    "tests/test_action_parallel_executor.py",
    "tests/test_subagents.py",
    "tests/test_agent_checkpoint_attempts.py",
    "tests/test_admission_control.py",
    "tests/test_memory_postgres_runtime.py",
)

DATASET_PATTERNS: tuple[str, ...] = (
    "evaluation/datasets/*.jsonl",
    "evaluation/datasets/*.manifest.json",
    "evaluation/fixtures/**/*.json",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _dataset_inventory() -> dict[str, str]:
    inventory: dict[str, str] = {}
    for pattern in DATASET_PATTERNS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            if path.is_file():
                inventory[str(path.relative_to(REPO_ROOT))] = _sha256(path)
    return inventory


def _select_tests(include_optional: bool) -> tuple[list[str], list[str]]:
    selected = [name for name in REQUIRED_TESTS if (REPO_ROOT / name).is_file()]
    missing = [name for name in REQUIRED_TESTS if not (REPO_ROOT / name).is_file()]
    if include_optional:
        selected.extend(name for name in TASK_TESTS if (REPO_ROOT / name).is_file())
    return selected, missing


def _run_pytest(tests: list[str], timeout_seconds: float) -> dict[str, object]:
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *tests],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    duration = time.monotonic() - started
    tail = "\n".join((completed.stdout or "").splitlines()[-12:])
    return {
        "command": f"{sys.executable} -m pytest -q {' '.join(tests)}",
        "exit_code": completed.returncode,
        "duration_seconds": round(duration, 3),
        "output_tail": tail,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="regression", help="baseline label, e.g. p0a-before")
    parser.add_argument("--output", default=str(REPO_ROOT / "docs/superpowers/implementation/production-hardening/baselines"))
    parser.add_argument("--timeout", type=float, default=1800.0, help="pytest timeout in seconds")
    parser.add_argument("--skip-optional", action="store_true", help="do not include task-delivered tests")
    parser.add_argument("--strict", action="store_true", help="fail when a required test file is missing")
    args = parser.parse_args(argv)

    tests, missing = _select_tests(include_optional=not args.skip_optional)
    if not tests:
        print("no regression tests found", file=sys.stderr)
        return 2
    if missing and args.strict:
        print(f"missing required tests: {', '.join(missing)}", file=sys.stderr)
        return 2

    result = _run_pytest(tests, args.timeout)
    now = datetime.now(timezone.utc)
    baseline_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{_git_commit()}-{args.label}"
    payload = {
        "baseline_id": baseline_id,
        "label": args.label,
        "recorded_at": now.isoformat(),
        "git_commit": _git_commit(),
        "tests": tests,
        "missing_required_tests": missing,
        "dataset_sha256": _dataset_inventory(),
        "result": result,
        "passed": result["exit_code"] == 0,
    }
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / f"{baseline_id}.json"
    artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"baseline_id={baseline_id}")
    print(f"artifact={artifact}")
    print(f"passed={payload['passed']} exit_code={result['exit_code']} duration={result['duration_seconds']}s")
    if missing:
        print(f"missing required tests: {', '.join(missing)}")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
