"""Two-replica HTTP drill for shared state and single-owner turn execution.

Starts two API processes against the same storage (SQLite default), then:

1. creates a session and a turn on replica A and starts it on both replicas
   concurrently — exactly one owner may execute;
2. asserts the turn reaches a terminal state exactly once (one ``turn_end``
   event, non-empty answer), proving claim/fencing holds across processes;
3. asserts a session created on A is visible on B (shared state).

The report is written as JSON; the drill is repeatable in a dev workspace and
does not require a model to be reachable beyond what the deployment provides.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _seed_actor(username: str, password: str, db_path: str) -> None:
    os.environ["AUTH_DB_PATH"] = db_path
    import src.settings  # noqa: E402

    src.settings.AUTH_DB_PATH = db_path
    from src.core.auth import ROLE_EMPLOYEE, AuthService  # noqa: E402

    auth = AuthService(db_path=db_path)
    sysadmin = auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
    if sysadmin is None:
        raise RuntimeError("default system admin is not configured (AUTH_DEFAULT_ADMIN_*)")
    existing = auth.get_user_by_username(username)
    if existing is None:
        department = auth.create_department("mi-drill")
        auth.create_user_as(sysadmin, username, password, ROLE_EMPLOYEE, department.id)


class _Replica:
    def __init__(self, port: int, env: dict[str, str]) -> None:
        self.port = port
        child_env = dict(os.environ)
        child_env.update(env)
        child_env["HDB_API_HOST"] = "127.0.0.1"
        child_env["HDB_API_PORT"] = str(port)
        child_env["HDB_API_SPAWN_MEMORY_WORKER"] = "0"
        self.proc = subprocess.Popen(
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
            cwd=str(REPO_ROOT),
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.base_url = f"http://127.0.0.1:{port}"

    def wait_ready(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.base_url}/health", timeout=2.0).status_code == 200:
                    return
            except Exception:
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(f"replica on :{self.port} exited early rc={self.proc.returncode}")
            time.sleep(0.5)
        raise TimeoutError(f"replica on :{self.port} never became ready")

    def stop(self) -> None:
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


def _login(base_url: str, username: str, password: str) -> str:
    response = httpx.post(
        f"{base_url}/api/v1/login", json={"username": username, "password": password}, timeout=15
    )
    response.raise_for_status()
    return str(response.json()["token"])


def _wait_terminal(base_url: str, token: str, turn_id: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    headers = {"Authorization": f"Bearer {token}"}
    while time.monotonic() < deadline:
        response = httpx.get(f"{base_url}/api/v1/turns/{turn_id}", headers=headers, timeout=15)
        if response.status_code == 200:
            status = str(response.json().get("status") or "")
            if status in {"completed", "failed", "cancelled"}:
                return status
        time.sleep(1.0)
    return "timeout"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port-a", type=int, default=8125)
    parser.add_argument("--port-b", type=int, default=8126)
    parser.add_argument("--username", default="midrill")
    parser.add_argument("--password", default="StrongPass123!")
    parser.add_argument("--auth-db", default=str(REPO_ROOT / "storage" / "auth.db"))
    parser.add_argument("--turn-timeout", type=float, default=300.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    _seed_actor(args.username, args.password, args.auth_db)

    replicas = [
        _Replica(args.port_a, {"AUTH_DB_PATH": args.auth_db}),
        _Replica(args.port_b, {"AUTH_DB_PATH": args.auth_db}),
    ]
    report: dict[str, object] = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "auth_db": args.auth_db,
        "replicas": [args.port_a, args.port_b],
        "checks": {},
    }
    try:
        for replica in replicas:
            replica.wait_ready()
        token = _login(replicas[0].base_url, args.username, args.password)
        headers = {"Authorization": f"Bearer {token}"}

        # Session + turn created on A, looked up on B (shared state).
        session = httpx.post(
            f"{replicas[0].base_url}/api/v1/conversations",
            json={"kb_name": "__general__", "title": "multi-instance drill"},
            headers=headers,
            timeout=20,
        )
        session.raise_for_status()
        session_id = int(session.json()["id"])
        visible_on_b = httpx.get(
            f"{replicas[1].base_url}/api/v1/conversations/{session_id}",
            headers=headers,
            timeout=15,
        )
        report["checks"]["session_visible_on_both"] = bool(
            visible_on_b.status_code == 200 and visible_on_b.json().get("id") == session_id
        )

        turn = httpx.post(
            f"{replicas[0].base_url}/api/v1/conversations/{session_id}/turns",
            json={
                "query": "多实例演练：请用一句话确认收到。",
                "client_request_id": f"mi-drill-{int(time.time())}",
                "source_scope": "knowledge_base_only",
            },
            headers=headers,
            timeout=20,
        )
        turn.raise_for_status()
        turn_id = str(turn.json()["turn"]["id"])

        started_at = time.monotonic()
        start_a = httpx.post(
            f"{replicas[0].base_url}/api/v1/turns/{turn_id}/start", headers=headers, timeout=20
        )
        start_b = httpx.post(
            f"{replicas[1].base_url}/api/v1/turns/{turn_id}/start", headers=headers, timeout=20
        )
        report["start_status"] = {"a": start_a.status_code, "b": start_b.status_code}

        terminal = _wait_terminal(
            replicas[0].base_url, token, turn_id, timeout=args.turn_timeout
        )
        report["checks"]["terminal_status"] = terminal
        report["e2e_seconds"] = round(time.monotonic() - started_at, 3)

        detail = httpx.get(
            f"{replicas[0].base_url}/api/v1/turns/{turn_id}", headers=headers, timeout=15
        ).json()
        report["checks"]["answer_present"] = bool(str(detail.get("answer") or "").strip())

        with closing(sqlite3.connect(args.auth_db)) as conn:
            row = conn.execute(
                "SELECT COUNT(*), status FROM chat_turn_events e JOIN chat_turns t ON t.id = e.turn_id"
                " WHERE e.turn_id = ? AND e.event_type = 'turn_end'",
                (turn_id,),
            ).fetchone()
        turn_end_events = int(row[0] or 0)
        report["checks"]["single_turn_end_event"] = turn_end_events == 1
        report["turn_end_events"] = turn_end_events

        ok = (
            report["checks"]["session_visible_on_both"]
            and terminal == "completed"
            and report["checks"]["answer_present"]
            and report["checks"]["single_turn_end_event"]
        )
        report["ok"] = ok
    finally:
        for replica in replicas:
            replica.stop()

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
