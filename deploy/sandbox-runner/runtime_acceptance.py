#!/usr/bin/env python3
"""Real OCI inspection for the fixed dev-l1 bundle; no mock runtime.

Submit the reviewed slow bundle, inspect its running container and deny-list
mounts, then verify timeout/cancellation both remove the container/workspace.
Requires a live Runner with a task timeout shorter than 30 seconds.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent
BASE = os.getenv("SANDBOX_RUNNER_URL", "http://127.0.0.1:8780").rstrip("/")
TOKEN_FILE = Path(os.getenv("SANDBOX_RUNNER_TOKEN_FILE", str(ROOT / "secrets/token")))
TOKEN = TOKEN_FILE.read_text(encoding="utf-8").strip()


def _request(path: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload or {}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Audience": "sandbox-runner",
            "X-Request-ID": f"req-{uuid.uuid4().hex}",
            "Idempotency-Key": f"run-{uuid.uuid4().hex}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def _submit(task_id: str, result: dict, failures: list[str]) -> None:
    source = (ROOT / "bundles/slow/entrypoint.py").read_bytes()
    try:
        result.update(_request("/v1/tasks", {
            "task_id": task_id,
            "bundle_ref": "bundle/slow",
            "bundle_digest": hashlib.sha256(source).hexdigest(),
            "profile_ref": "profile/dev-l1",
            "kind": "task",
            "network_policy_ref": "none",
            "parameters": {"seconds": 30},
        }))
    except Exception as exc:  # noqa: BLE001 - report only bounded error class
        failures.append(type(exc).__name__)


def _inspect(name: str) -> dict:
    result = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, timeout=10)
    return json.loads(result.stdout)[0] if result.returncode == 0 else {}


def _run_case(*, cancel: bool) -> None:
    task_id = f"task-{uuid.uuid4().hex[:24]}"
    name = f"hdb-task-{task_id}"
    result: dict = {}
    failures: list[str] = []
    worker = threading.Thread(target=_submit, args=(task_id, result, failures))
    worker.start()
    deadline = time.monotonic() + 10
    container: dict = {}
    while time.monotonic() < deadline:
        container = _inspect(name)
        if container.get("State", {}).get("Running"):
            break
        time.sleep(0.1)
    assert container.get("State", {}).get("Running"), "slow bundle was not observed running"
    host = container["HostConfig"]
    assert container["Config"]["User"] != "0"
    assert host["ReadonlyRootfs"] is True
    assert host["NetworkMode"] == "none"
    assert "ALL" in host["CapDrop"]
    assert "no-new-privileges" in host["SecurityOpt"]
    assert host["PidsLimit"] == 128 and host["Memory"] == 256 * 1024 * 1024
    assert host["NanoCpus"] == 1_000_000_000
    assert {mount["Destination"] for mount in container["Mounts"]} == {"/work/in", "/work/out"}
    assert all(mount["Type"] == "bind" for mount in container["Mounts"])

    if cancel:
        cancelled = _request(f"/v1/tasks/{task_id}/cancel")
        assert cancelled["status"] in {"cancel_requested", "cancelled"}, cancelled["status"]
    worker.join(timeout=90)
    assert not worker.is_alive() and not failures, failures
    assert result["status"] == ("cancelled" if cancel else "timed_out"), result["status"]
    assert not _inspect(name), "container survived cancellation/timeout"
    assert not (Path(os.getenv("SANDBOX_RUNNER_WORKSPACE_ROOT", "/tmp/hdb-sandbox-runner")) / task_id).exists()
    print(f"[ok] real container flags, mounts and {'cancel' if cancel else 'timeout'} cleanup")


if __name__ == "__main__":
    _run_case(cancel=False)
    _run_case(cancel=True)
