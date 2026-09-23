#!/usr/bin/env python3
"""Kill an isolated Runner mid-execution and verify startup reconciliation.

Runs on an ephemeral localhost port, separate SQLite/workspace and an isolated
Docker label; the deployed Runner and Gateway are not stopped.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


HERE = Path(__file__).resolve().parent
TOKEN = (HERE / "secrets/token").read_text(encoding="utf-8").strip()


def _request(url: str, path: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        url + path,
        method="POST" if payload is not None else "GET",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "X-Audience": "sandbox-runner", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def _running_container(name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _wait_until(predicate, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (urllib.error.URLError, TimeoutError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError("runtime did not reach the expected state")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sandbox-restart-", dir="/tmp/opencode") as root:
        root_path = Path(root)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        run_id = uuid.uuid4().hex[:12]
        env = {
            **os.environ,
            "SANDBOX_RUNNER_PORT": str(port),
            "SANDBOX_RUNNER_STATE_DIR": str(root_path / "state"),
            "SANDBOX_RUNNER_WORKSPACE_ROOT": str(root_path / "work"),
            "SANDBOX_RUNNER_CONTAINER_LABEL": f"hdb.sandbox.restart={run_id}",
            "SANDBOX_RUNNER_TASK_TIMEOUT_SECONDS": "30",
        }
        processes: list[subprocess.Popen] = []

        def launch():
            proc = subprocess.Popen(
                [sys.executable, str(HERE / "runner.py")],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            processes.append(proc)
            _wait_until(lambda: _request(url, "/v1/health").get("status") == "up", 10)
            return proc

        task_id = f"task-{uuid.uuid4().hex[:24]}"
        name = f"hdb-task-{task_id}"
        failure: list[str] = []
        try:
            first = launch()
            payload = {
                "task_id": task_id,
                "bundle_ref": "bundle/slow",
                "bundle_digest": hashlib.sha256((HERE / "bundles/slow/entrypoint.py").read_bytes()).hexdigest(),
                "profile_ref": "profile/dev-l1",
                "parameters": {"seconds": 30},
                "kind": "task",
                "network_policy_ref": "none",
            }

            def submit() -> None:
                try:
                    _request(url, "/v1/tasks", payload)
                except Exception as exc:  # noqa: BLE001 - connection reset is expected after SIGKILL
                    failure.append(type(exc).__name__)

            worker = threading.Thread(target=submit)
            worker.start()
            _wait_until(lambda: _running_container(name), 10)
            assert (root_path / "work" / task_id).is_dir()
            first.kill()
            first.wait(timeout=5)
            worker.join(timeout=10)

            second = launch()
            observation = _request(url, f"/v1/tasks/{task_id}")["observation"]
            assert observation["status"] == "unknown", observation
            assert observation["cleanup_state"] == "complete", observation
            assert not _running_container(name)
            assert not (root_path / "work" / task_id).exists()
            assert second.poll() is None
            print(f"[ok] SIGKILL/restart reconciled unknown execution and removed container/workspace ({len(failure)} reset)")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)


if __name__ == "__main__":
    main()
