"""Own two isolated API replicas and verify replay plus abrupt worker takeover."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import httpx
import psycopg
from dotenv import dotenv_values

from acceptance_deployment import IsolatedDeployment, REPO_ROOT, TEST_PASSWORD, free_port
from src.evaluation.http_events import iter_sse_events


def wait_turn(client: httpx.Client, turn_id: str, *, status: str | None = None, timeout: float = 180) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/turns/{turn_id}")
        response.raise_for_status()
        turn = response.json()
        if status == turn["status"] or (status is None and turn["status"] in {"completed", "failed", "cancelled"}):
            return turn
        time.sleep(0.25)
    raise TimeoutError("turn did not reach expected state")


def stop_group(process):
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-env", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = {"checks": {}}
    replica_b = worker_b = None
    try:
        with IsolatedDeployment(args.output, args.provider_env, settings_overrides={
            "CHAT_TURN_HEARTBEAT_TTL_SECONDS": "30", "CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS": "2",
            "CHAT_TURN_LEASE_SECONDS": "10", "CHAT_TURN_LEASE_RENEW_SECONDS": "2",
        }) as deployment:
            log_path = deployment.root / "replica-b.log"
            with log_path.open("w", encoding="utf-8") as log:
                port_b = free_port()
                env_b = {**deployment.env, "HDB_API_SPAWN_WORKER": "0", "HDB_API_SPAWN_EXPORT_WORKER": "0"}
                replica_b = subprocess.Popen([sys.executable, "-m", "uvicorn", "src.api.app:app", "--host", "127.0.0.1",
                    "--port", str(port_b), "--log-level", "warning"], cwd=REPO_ROOT, env=env_b,
                    stdout=log, stderr=log, start_new_session=True)
                try:
                    with httpx.Client(base_url=deployment.base_url, timeout=180) as a, httpx.Client(base_url=f"http://127.0.0.1:{port_b}", timeout=180) as b:
                        deadline = time.monotonic() + 60
                        while time.monotonic() < deadline:
                            if replica_b.poll() is not None:
                                raise RuntimeError("replica B exited")
                            try:
                                if b.get("/health/ready", timeout=3).status_code == 200:
                                    break
                            except httpx.HTTPError:
                                pass
                            time.sleep(0.25)
                        else:
                            raise TimeoutError("replica B did not become ready")
                        login = a.post("/api/v1/login", json={"username": "acceptance", "password": TEST_PASSWORD})
                        login.raise_for_status()
                        headers = {"Authorization": "Bearer " + login.json()["token"]}
                        a.headers.update(headers)
                        b.headers.update(headers)
                        session_response = a.post("/api/v1/conversations", json={"kb_name": "__general__", "title": "Replica acceptance"})
                        session_response.raise_for_status()
                        session = int(session_response.json()["id"])
                        report["checks"]["session_visible_on_b"] = b.get(f"/api/v1/conversations/{session}").json()["id"] == session
                        payload = b"cross-replica file bytes"
                        upload = a.post(f"/api/v1/conversations/{session}/attachments", files={"file": ("replica.txt", payload, "text/plain")})
                        upload.raise_for_status()
                        attachment = upload.json()
                        download = b.get(f"/api/v1/conversations/{session}/attachments/{attachment['attachment_id']}/download")
                        download.raise_for_status()
                        report["checks"]["cross_replica_file_sha256"] = hashlib.sha256(download.content).hexdigest() == hashlib.sha256(payload).hexdigest()
                        body = {"query": "请记住验收暗号 RPL-2026-OK，只回复已记住。", "client_request_id": uuid.uuid4().hex}
                        with ThreadPoolExecutor(max_workers=2) as executor:
                            requests = [executor.submit(client.post, f"/api/v1/conversations/{session}/turns", json=body) for client in (a, b)]
                            turns = [future.result() for future in requests]
                        for response in turns:
                            response.raise_for_status()
                        turn_id = turns[0].json()["turn"]["id"]
                        report["checks"]["duplicate_submission_single_id"] = turns[1].json()["turn"]["id"] == turn_id
                        completed = wait_turn(b, turn_id)
                        report["first_answer"] = completed.get("answer", "")
                        report["checks"]["first_turn_completed"] = completed["status"] == "completed"
                        events = b.get(f"/api/v1/turns/{turn_id}/events")
                        events.raise_for_status()
                        replay = list(iter_sse_events(events.text.splitlines()))
                        report["checks"]["first_turn_single_terminal"] = sum(event["event"] == "turn_end" for event in replay) == 1
                        follow = b.post(f"/api/v1/conversations/{session}/turns", json={"query": "先写出刚才的验收暗号，然后用约500字解释欧姆定律。", "client_request_id": uuid.uuid4().hex})
                        follow.raise_for_status()
                        follow_id = follow.json()["turn"]["id"]
                        wait_turn(b, follow_id, status="streaming")
                        # Wait for real output from the owned worker, then kill
                        # its API/supervisor process group without graceful exit.
                        with b.stream("GET", f"/api/v1/turns/{follow_id}/events") as events:
                            events.raise_for_status()
                            for event in iter_sse_events(events.iter_lines()):
                                if event["event"] == "delta":
                                    break
                        killed_at = time.monotonic()
                        stop_group(deployment.process)
                        worker_b = subprocess.Popen([sys.executable, "-m", "src.workers.main", "--role", "chat"],
                            cwd=REPO_ROOT, env=deployment.env, stdout=log, stderr=log, start_new_session=True)
                        recovered = wait_turn(b, follow_id)
                        report["recovered_answer"] = recovered.get("answer", "")
                        report["short_term_memory"] = (recovered.get("metrics") or {}).get("short_term_memory", {})
                        report["recovery_seconds"] = time.monotonic() - killed_at
                        report["checks"]["takeover_completed"] = recovered["status"] == "completed"
                        report["checks"]["conversation_context_preserved"] = "RPL-2026-OK" in recovered.get("answer", "")
                        config = dotenv_values(deployment.root / "active.env")
                        with psycopg.connect(config["CONTROL_PLANE_DATABASE_URL"], autocommit=True) as conn:
                            retries = conn.execute("SELECT retry_count FROM chat_turns WHERE id = %s", (follow_id,)).fetchone()[0]
                        report["retry_count"] = retries
                        report["checks"]["durable_retry_observed"] = retries >= 1
                        replay_response = b.get(f"/api/v1/turns/{follow_id}/events")
                        replay_response.raise_for_status()
                        replay = list(iter_sse_events(replay_response.text.splitlines()))
                        report["checks"]["takeover_single_terminal"] = sum(event["event"] == "turn_end" for event in replay) == 1
                        b.delete(f"/api/v1/conversations/{session}").raise_for_status()
                finally:
                    stop_group(worker_b)
                    stop_group(replica_b)
                    worker_b = replica_b = None
            report["ok"] = all(report["checks"].values())
    except Exception as exc:  # noqa: BLE001 - retain a failed drill report
        report.update(ok=False, error_type=type(exc).__name__)
    finally:
        stop_group(worker_b)
        stop_group(replica_b)
    args.output.joinpath("replica-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
