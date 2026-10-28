"""Own an isolated PostgreSQL/Redis/API/Worker deployment for live acceptance.

Only model/RAGFlow connection settings are copied from --provider-env. Business
state, containers, users and file bytes belong to this run. --serve keeps the
deployment available for browser/load tests until interrupted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

import httpx
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[2]
PROVIDER_KEYS = (
    "AGENT_LLM_PROVIDER", "AGENT_CUSTOM_API_KEY", "AGENT_CUSTOM_BASE_URL", "AGENT_CUSTOM_MODEL",
    "AGENT_CUSTOM_MAX_TOKENS", "AGENT_OLLAMA_BASE_URL", "AGENT_OLLAMA_MODEL",
    "RAGFLOW_BASE_URL", "RAGFLOW_API_KEY", "RAGFLOW_TIMEOUT_SECONDS",
)
STORE_PREFIXES = (
    "CONTROL_PLANE", "PIPELINE_DOCUMENT_STORE",
    "AUTH_STORE", "COMPATIBILITY_STORE", "MEMORY_STORE", "AGENT_CHECKPOINT",
    "RESOURCE_GOVERNOR",
)
TEST_PASSWORD = "AcceptanceTestPassword123!"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def source_fingerprint() -> dict[str, str]:
    """Identify both the committed baseline and the uncommitted source bytes."""
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
    paths = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=REPO_ROOT)
    digest = hashlib.sha256()
    for name in sorted(set(paths.split(b"\0")) - {b""}):
        path = REPO_ROOT / os.fsdecode(name)
        if path.is_file():
            digest.update(name + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return {"git_commit": commit, "source_sha256": digest.hexdigest()}


class IsolatedDeployment:
    def __init__(self, root: Path, provider_env: Path, *, port: int = 0, settings_overrides: dict[str, str] | None = None):
        self.root = root.resolve()
        self.provider_env = provider_env
        self.port = port or free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.containers: list[str] = []
        self.process = None
        self.env: dict[str, str] = {}
        self.metadata: dict = {}
        self.log = None
        self.settings_overrides = settings_overrides or {}

    def _container(self, image: str, service: str, container_port: int, *options: str) -> int:
        name = f"hdb-accept-{service}-{secrets.token_hex(5)}"
        command = ["docker", "run", "--detach", "--name", name,
                   "--publish", f"127.0.0.1::{container_port}", *options, image]
        subprocess.run(command, check=True, capture_output=True, text=True)
        self.containers.append(name)
        inspect = subprocess.run(["docker", "inspect", name], check=True, capture_output=True, text=True)
        port = int(json.loads(inspect.stdout)[0]["NetworkSettings"]["Ports"][f"{container_port}/tcp"][0]["HostPort"])
        health = ["pg_isready", "-U", "hdbaccept", "-d", "hdbaccept"] if service == "pg" else ["redis-cli", "ping"]
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            result = subprocess.run(["docker", "exec", name, *health], capture_output=True)
            if result.returncode == 0:
                return port
            time.sleep(0.25)
        raise TimeoutError(f"{service} did not become ready")

    def _run(self, command: list[str]):
        return subprocess.run(command, cwd=REPO_ROOT, env=self.env, check=True,
                              text=True, capture_output=True, timeout=120)

    def __enter__(self):
        self.root.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.log = (self.root / "deployment.log").open("w", encoding="utf-8")
        try:
            password = secrets.token_urlsafe(24)
            pg_port = self._container("pgvector/pgvector:pg17", "pg", 5432,
                                      "--env", "POSTGRES_USER=hdbaccept", "--env", "POSTGRES_DB=hdbaccept",
                                      "--env", f"POSTGRES_PASSWORD={password}", "--tmpfs", "/var/lib/postgresql/data")
            redis_port = self._container("redis:8-alpine", "redis", 6379)
            source = dotenv_values(self.provider_env)
            selected = {key: str(os.environ.get(key, source.get(key))) for key in PROVIDER_KEYS
                        if os.environ.get(key, source.get(key)) is not None}
            dsn = f"postgresql://hdbaccept:{password}@127.0.0.1:{pg_port}/hdbaccept"
            values = {
                **selected,
                "STORAGE_DIR": str(self.root / "data"),
                "AUTH_DEFAULT_ADMIN_PASSWORD": TEST_PASSWORD,
                "REDIS_URL": f"redis://127.0.0.1:{redis_port}/0", "REDIS_REQUIRED": "true",
                "OBS_ENABLED": "false", "MEMORY_ENABLED": "false", "MCP_ENABLED": "false",
                "SANDBOX_ENABLED": "false", "SANDBOX_GATEWAY_ENABLED": "false",
                "AGENT_TEMPERATURE": "0", "AGENT_TIMEOUT_SECONDS": "120",
                "RESOURCE_GOVERNOR_SCOPE": "deployment",
                "RESOURCE_GOVERNOR_DEPLOYMENT_ID": "acceptance-" + secrets.token_hex(5),
                "HDB_API_SPAWN_WORKER": "1", "HDB_API_SPAWN_EXPORT_WORKER": "1",
                "HDB_API_SPAWN_MEMORY_WORKER": "0", "HDB_REQUIRED_WORKER_ROLES": "chat,parse,export",
                "CHAT_ATTACHMENTS_ENABLED": "true",
                "CONVERSATION_SERIALIZE_SAME_THREAD": "true",
                "ADMISSION_CONTROL_ENABLED": "false",
            }
            for prefix in STORE_PREFIXES:
                values[prefix + "_BACKEND"] = "postgres"
                database_key = {"MEMORY_STORE": "MEMORY", "PIPELINE_DOCUMENT_STORE": "PIPELINE_DOCUMENT"}.get(prefix, prefix)
                values[database_key + "_DATABASE_URL"] = dsn
            values.update(self.settings_overrides)
            config = self.root / "active.env"
            config.write_text("".join(f"{key}={json.dumps(value)}\n" for key, value in values.items()), encoding="utf-8")
            config.chmod(0o600)
            # Remove application settings inherited from the parent shell; the
            # dedicated config is the only source for this child deployment.
            self.env = {key: value for key, value in os.environ.items()
                        if key not in source and key not in values and not key.startswith(STORE_PREFIXES)}
            self.env.update({"HDB_ENV_PATH": str(config), "PYTHONUNBUFFERED": "1"})
            self.log.write(self._run([sys.executable, "-m", "src.storage.bootstrap"]).stdout)
            self.log.flush()
            seed = self._run([sys.executable, "-c", '''
import json
from src.core.auth import AuthService, ROLE_EMPLOYEE
import src.settings as settings
auth = AuthService()
admin = auth.get_user_by_username(settings.AUTH_DEFAULT_ADMIN_USERNAME)
department = auth.create_department("acceptance")
user = auth.create_user_as(admin, "acceptance", settings.AUTH_DEFAULT_ADMIN_PASSWORD, ROLE_EMPLOYEE, department.id)
browser = auth.create_user_as(admin, "acceptance_browser", settings.AUTH_DEFAULT_ADMIN_PASSWORD, ROLE_EMPLOYEE, department.id)
auth.register_knowledge_base("acceptance", owner=user, is_shared=True)
print(json.dumps({"department_id": department.id, "user_id": user.id, "browser_user_id": browser.id}))
'''])
            identities = json.loads(seed.stdout.strip().splitlines()[-1])
            self.process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "src.api.app:app", "--host", "127.0.0.1",
                 "--port", str(self.port), "--log-level", "warning"],
                cwd=REPO_ROOT, env=self.env, stdout=self.log, stderr=self.log, start_new_session=True,
            )
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"API exited; see {self.root / 'deployment.log'}")
                try:
                    ready = httpx.get(self.base_url + "/health/ready", timeout=3)
                    if ready.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            else:
                raise TimeoutError(f"API/Workers not ready; see {self.root / 'deployment.log'}")
            self.metadata = {
                "recorded_at": datetime.now(timezone.utc).isoformat(), "base_url": self.base_url,
                "controller_pid": os.getpid(), "provider": selected.get("AGENT_LLM_PROVIDER", "ollama"),
                "model": selected.get("AGENT_CUSTOM_MODEL") or selected.get("AGENT_OLLAMA_MODEL", ""),
                "config_revision": hashlib.sha256(config.read_bytes()).hexdigest()[:16],
                "backends": {"control_plane": "postgres", "checkpoint": "postgres", "realtime": "redis"},
                "containers": self.containers, "identities": identities, "kb": "acceptance",
                "username": "acceptance", "test_password": TEST_PASSWORD,
                "browser_username": "acceptance_browser",
                **source_fingerprint(),
            }
            (self.root / "deployment.json").write_text(json.dumps(self.metadata, indent=2), encoding="utf-8")
            print(f"ready={self.base_url} manifest={self.root / 'deployment.json'}", flush=True)
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=10)
            # Reap children in the owned process group if startup/shutdown failed.
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for name in reversed(self.containers):
            subprocess.run(["docker", "rm", "--force", "--volumes", name], capture_output=True)
        self.containers.clear()
        (self.root / "active.env").unlink(missing_ok=True)
        if self.log is not None:
            self.log.close()
            self.log = None

    def __exit__(self, *args):
        self.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-env", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "storage/evaluations" / ("acceptance-" + datetime.now().strftime("%Y%m%d-%H%M%S")))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--fault-isolation", action="store_true", help="run role-worker and unreachable-RAGFlow drill")
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--case", action="append", default=[])
    args = parser.parse_args(argv)
    if args.serve and args.fault_isolation:
        parser.error("--serve and --fault-isolation are mutually exclusive")
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        overrides = {"HDB_API_SPAWN_WORKER": "0", "HDB_API_SPAWN_EXPORT_WORKER": "0", "HDB_REQUIRED_WORKER_ROLES": ""} if args.fault_isolation else None
        with IsolatedDeployment(args.output, args.provider_env, port=args.port, settings_overrides=overrides) as deployment:
            if args.serve:
                while True:
                    time.sleep(60)
            if args.fault_isolation:
                return subprocess.run([sys.executable, str(REPO_ROOT / "scripts/production_hardening/fault_isolation_drill.py"),
                    "--port", str(free_port()), "--bad-ragflow-port", str(free_port()), "--password", TEST_PASSWORD,
                    "--output", str(deployment.root / "fault-report.json")], cwd=REPO_ROOT, env=deployment.env).returncode
            command = [sys.executable, "-m", "src.evaluation.e2e_runner", "--base-url", deployment.base_url,
                       "--kb", "acceptance", "--department-id", str(deployment.metadata["identities"]["department_id"]),
                       "--output", str(deployment.root / "attachment-e2e"), "--limit", str(args.limit)]
            for case in args.case:
                command.extend(["--case", case])
            env = {**deployment.env, "HDB_E2E": "1"}
            return subprocess.run(command, cwd=REPO_ROOT, env=env).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
