"""Fault-isolation drill: role-worker and RAGFlow failures must not take chat down.

Scenarios (all processes are owned by this drill; the shared RAGFlow service is
never touched):

1. baseline: a general-chat turn completes with a healthy chat worker;
2. parse worker killed → the next general-chat turn still completes;
3. the healthy chat worker killed → the surviving chat replica still
   completes a general-chat turn;
4. a second API replica configured with an unreachable ``RAGFLOW_BASE_URL``
   still serves ``/health`` and completes general-chat turns (retrieval fault
   ≠ API/main-link outage).

A JSON report is written with per-scenario outcomes; the drill stops every
process it started.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]


class _Process:
    def __init__(self, args: list[str], env: dict[str, str], name: str) -> None:
        child_env = dict(os.environ)
        child_env.update(env)
        self.name = name
        self.proc = subprocess.Popen(
            args,
            cwd=str(REPO_ROOT),
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), 15)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), 9)
                except ProcessLookupError:
                    pass

    def stop(self) -> None:
        self.kill()


def _start_api(port: int, env: dict[str, str]) -> _Process:
    return _Process(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "src.api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        {
            "HDB_API_HOST": "127.0.0.1",
            "HDB_API_PORT": str(port),
            "HDB_API_SPAWN_WORKER": "0",
            "HDB_API_SPAWN_EXPORT_WORKER": "0",
            **env,
        },
        name=f"api:{port}",
    )


def _start_worker(role: str, env: dict[str, str]) -> _Process:
    return _Process(
        [sys.executable, "-m", "src.workers.main", "--role", role],
        env,
        name=f"worker:{role}",
    )


def _wait_ready(base_url: str, proc: _Process, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=2.0).status_code == 200:
                return
        except Exception:
            pass
        if not proc.alive():
            raise RuntimeError(f"{proc.name} exited early")
        time.sleep(0.5)
    raise TimeoutError(f"{proc.name} never became ready")


def _seed_user(username: str, password: str) -> None:
    import src.settings as settings

    from src.core.auth import ROLE_EMPLOYEE, AuthService

    auth = AuthService()
    sysadmin = auth.get_user_by_username(settings.AUTH_DEFAULT_ADMIN_USERNAME)
    if sysadmin is None:
        raise RuntimeError("default system admin is not configured")
    if auth.get_user_by_username(username) is None:
        department = auth.create_department("fault-drill")
        auth.create_user_as(sysadmin, username, password, ROLE_EMPLOYEE, department.id)


def _run_chat_turn(base_url: str, token: str, *, label: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    session = httpx.post(
        f"{base_url}/api/v1/conversations",
        json={"kb_name": "__general__", "title": f"fault-drill-{label}"},
        headers=headers,
        timeout=20,
    )
    session.raise_for_status()
    session_id = int(session.json()["id"])
    created = httpx.post(
        f"{base_url}/api/v1/conversations/{session_id}/turns",
        json={
            "query": "请用一句话确认系统正常。",
            "client_request_id": f"fault-drill-{label}-{int(time.time())}",
        },
        headers=headers,
        timeout=20,
    )
    created.raise_for_status()
    turn_id = str(created.json()["turn"]["id"])
    httpx.post(f"{base_url}/api/v1/turns/{turn_id}/start", headers=headers, timeout=20)
    deadline = time.monotonic() + 240
    status = "timeout"
    while time.monotonic() < deadline:
        detail = httpx.get(f"{base_url}/api/v1/turns/{turn_id}", headers=headers, timeout=15)
        if detail.status_code == 200 and str(detail.json().get("status")) in {
            "completed",
            "failed",
            "cancelled",
        }:
            status = str(detail.json()["status"])
            break
        time.sleep(1.0)
    return {"label": label, "status": status, "session_id": session_id, "turn_id": turn_id}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8128)
    parser.add_argument("--bad-ragflow-port", type=int, default=8129)
    parser.add_argument("--username", default="faultdrill")
    parser.add_argument("--password", default="StrongPass123!")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    _seed_user(args.username, args.password)
    import src.settings as settings

    storage_env = {"AUTH_DB_PATH": settings.AUTH_DB_PATH}

    api = _start_api(args.port, storage_env)
    chat_worker = _start_worker("chat", storage_env)
    parse_worker = _start_worker("parse", storage_env)
    bad_api = _start_api(args.bad_ragflow_port, {**storage_env, "RAGFLOW_BASE_URL": "http://127.0.0.1:9"})
    bad_worker = _start_worker(
        "chat", {**storage_env, "RAGFLOW_BASE_URL": "http://127.0.0.1:9"}
    )
    processes = [api, chat_worker, parse_worker, bad_api, bad_worker]
    report: dict[str, object] = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "scenarios": {},
        "health": {},
    }
    try:
        _wait_ready(f"http://127.0.0.1:{args.port}", api)
        _wait_ready(f"http://127.0.0.1:{args.bad_ragflow_port}", bad_api)
        base_url = f"http://127.0.0.1:{args.port}"
        bad_url = f"http://127.0.0.1:{args.bad_ragflow_port}"

        login = httpx.post(
            f"{base_url}/api/v1/login",
            json={"username": args.username, "password": args.password},
            timeout=15,
        )
        login.raise_for_status()
        token = str(login.json()["token"])

        report["scenarios"]["baseline_chat"] = _run_chat_turn(base_url, token, label="baseline")

        parse_worker.kill()
        report["scenarios"]["parse_worker_killed"] = _run_chat_turn(
            base_url, token, label="after-parse-kill"
        )
        report["health"]["after_parse_kill"] = httpx.get(
            f"{base_url}/health", timeout=5
        ).status_code

        report["scenarios"]["ragflow_unreachable_health"] = httpx.get(
            f"{bad_url}/health", timeout=5
        ).status_code
        bad_login = httpx.post(
            f"{bad_url}/api/v1/login",
            json={"username": args.username, "password": args.password},
            timeout=15,
        )
        bad_login.raise_for_status()
        report["scenarios"]["ragflow_unreachable_chat"] = _run_chat_turn(
            bad_url, str(bad_login.json()["token"]), label="ragflow-down"
        )

        # The healthy chat worker dies; the surviving chat replica (whose
        # RAGFlow URL is unreachable) must still finish general-chat turns.
        chat_worker.kill()
        report["scenarios"]["chat_worker_killed"] = _run_chat_turn(
            base_url, token, label="after-chat-kill"
        )

        checks = report["scenarios"]
        completed = lambda name: checks.get(name, {}).get("status") == "completed"  # noqa: E731
        report["ok"] = (
            completed("baseline_chat")
            and completed("parse_worker_killed")
            and completed("chat_worker_killed")
            and completed("ragflow_unreachable_chat")
            and report["health"].get("after_parse_kill") == 200
            and checks.get("ragflow_unreachable_health") == 200
        )
    finally:
        for proc in processes:
            proc.stop()

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"report={output}")
    else:
        print(text)
    print(f"ok={report.get('ok')}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
