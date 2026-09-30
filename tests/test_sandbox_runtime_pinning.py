"""The independent reference services must reject unreviewed runtime state."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import src.settings as settings
from src.agents.mcp.gateway import GatewayRegistry, GatewayStateError
from src.api.routes import sandbox as sandbox_routes
from src.sandbox.artifact_broker import ArtifactBroker, ArtifactBrokerError, ArtifactStateConflict


ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner(tmp_path, monkeypatch):
    module = _load(ROOT / "deploy/sandbox-runner/runner.py", "hdb_reference_runner")
    hdb_token = tmp_path / "hdb-token"
    gateway_token = tmp_path / "gateway-token"
    hdb_token.write_text("hdb-secret", encoding="utf-8")
    gateway_token.write_text("gateway-secret", encoding="utf-8")
    hdb_token.chmod(0o600)
    gateway_token.chmod(0o600)
    profile_file = tmp_path / "profile"
    profile_file.write_text("reviewed profile", encoding="utf-8")
    digest = hashlib.sha256(profile_file.read_bytes()).hexdigest()
    image = "sha256:" + "a" * 64
    catalog = tmp_path / "services.json"
    catalog.write_text(json.dumps({"deploy/reviewed": {
        "profile_ref": "profile/dev-l1",
        "profile_digest": digest,
        "image_digest": image,
        "entrypoint": ["/usr/local/bin/python3", "/opt/mcp/stdio_bridge.py"],
        "environment": ["MCP_BRIDGE_PORT=8080", "MCP_STDIO_SERVER=/opt/mcp/stdio_server.py"],
        "source_labels": {"org.hdb.stdio.server.sha256": "b" * 64},
    }}), encoding="utf-8")
    monkeypatch.setattr(module, "TOKEN_FILE", str(hdb_token))
    monkeypatch.setattr(module, "GATEWAY_TOKEN_FILE", str(gateway_token))
    monkeypatch.setattr(module, "PROFILE_FILE", str(profile_file))
    monkeypatch.setattr(module, "SERVICE_PROFILES_FILE", str(catalog))
    monkeypatch.setattr(module, "STATE_DIR", str(tmp_path / "state"))
    return module, image, digest


def test_runner_service_requires_separate_identity_and_exact_profile(runner, monkeypatch):
    module, image, profile_digest = runner
    inspected = []
    monkeypatch.setattr(module, "_reconcile_tasks", lambda: {"reconciled": True})
    monkeypatch.setattr(module, "_inspect_image", lambda pinned, labels: inspected.append((pinned, labels)))
    monkeypatch.setattr(module, "_service_ports", lambda ref, pinned, entry: (
        "service", "172.23.0.2", "svc-" + "1" * 64,
    ))
    with TestClient(module.app) as client:
        path = "/v1/services"
        payload = {"deployment_ref": "deploy/reviewed", "profile_ref": "profile/dev-l1", "image_digest": image}
        hdb = {"Authorization": "Bearer hdb-secret", "X-Audience": "sandbox-gateway"}
        gateway = {"Authorization": "Bearer gateway-secret", "X-Audience": "sandbox-gateway"}
        assert client.post(path, json=payload, headers=hdb).status_code == 401
        assert client.post(path, json=payload, headers={"Authorization": "Bearer gateway-secret"}).status_code == 403
        assert client.post(path, json={**payload, "profile_ref": "profile/unknown"}, headers=gateway).status_code == 409
        assert client.post(path, json={**payload, "image_digest": "sha256:" + "c" * 64}, headers=gateway).status_code == 409
        assert client.post(path, json={**payload, "deployment_ref": "deploy/unreviewed"}, headers=gateway).status_code == 404
        assert not inspected
        response = client.post(path, json=payload, headers=gateway)
        assert response.status_code == 200
        assert response.json()["image_digest"] == image
        assert response.json()["profile_digest"] == profile_digest
        assert inspected == [(image, {"org.hdb.stdio.server.sha256": "b" * 64})]


def test_runner_refuses_tampered_image_labels(runner, monkeypatch):
    module, image, _ = runner
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0,
        stdout=json.dumps([{"Id": image, "Config": {"Labels": {"org.hdb.stdio.server.sha256": "c" * 64}}}]),
    ))
    with pytest.raises(HTTPException) as error:
        module._service_profile("deploy/reviewed", "profile/dev-l1", image)
    assert error.value.detail["error"]["code"] == "image_unverified"


def test_runner_refuses_changed_runtime_policy(runner, monkeypatch, tmp_path):
    module, image, _ = runner
    changed = tmp_path / "changed"
    changed.write_text("unreviewed profile", encoding="utf-8")
    monkeypatch.setattr(module, "PROFILE_FILE", str(changed))
    with pytest.raises(HTTPException) as error:
        module._service_profile("deploy/reviewed", "profile/dev-l1", image)
    assert error.value.detail["error"]["code"] == "profile_mismatch"


def test_runner_cleans_service_when_post_start_attestation_fails(runner, monkeypatch):
    module, image, _ = runner
    entry = module._read_service_entry("deploy/reviewed")
    launched_id = "1" * 64
    commands = []

    def docker(argv, **kwargs):
        commands.append(argv)
        if argv[1] == "ps":
            return SimpleNamespace(returncode=0, stdout="")
        if argv[1] == "run":
            return SimpleNamespace(returncode=0, stdout=launched_id)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(module, "_ensure_service_network", lambda: None)
    monkeypatch.setattr(module, "_service_ip", lambda *args: (_ for _ in ()).throw(
        module._safe_error("service_unverified", "wrong instance", status=503)
    ))
    monkeypatch.setattr(module.subprocess, "run", docker)
    with pytest.raises(HTTPException):
        module._service_ports("deploy/reviewed", image, entry)
    assert ["docker", "rm", "-f", launched_id] in commands


def test_runner_rejects_service_with_second_egress_network(runner, monkeypatch):
    module, image, _ = runner
    entry = module._read_service_entry("deploy/reviewed")
    container = {
        "Id": "1" * 64,
        "Name": "/hdb-svc-deploy-reviewed",
        "Image": image,
        "State": {"Running": True},
        "Config": {
            "Image": image, "Entrypoint": entry["entrypoint"], "Env": entry["environment"],
            "Cmd": None, "WorkingDir": "/opt/mcp", "User": module._container_user(),
            "Labels": {"hdb.sandbox.runner": "1"},
        },
        "HostConfig": {
            "Privileged": False, "NetworkMode": module.SERVICE_NETWORK,
            "ReadonlyRootfs": True, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
            "PidsLimit": 128, "Memory": 256 * 1024 * 1024, "NanoCpus": 1_000_000_000,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=16m"},
        },
        "NetworkSettings": {"Networks": {
            module.SERVICE_NETWORK: {"IPAddress": "172.23.0.2"},
            "egress": {"IPAddress": "172.24.0.2"},
        }},
        "Mounts": [],
    }
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps([container]),
    ))
    with pytest.raises(HTTPException) as error:
        module._service_ip("hdb-svc-deploy-reviewed", image, entry)
    assert error.value.detail["error"]["code"] == "service_unverified"


def test_runner_stop_rejects_still_running_service(runner, monkeypatch):
    module, _, _ = runner
    name = "hdb-svc-deploy-reviewed"

    def docker(args, **kwargs):
        if args[1] == "ps":
            return SimpleNamespace(returncode=0, stdout=name + "\n")
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(module.subprocess, "run", docker)
    with pytest.raises(HTTPException) as error:
        module._stop_service("deploy/reviewed")
    assert error.value.detail["error"]["code"] == "service_stop_unverified"


def test_runner_executes_pinned_id_without_registry_pull(runner, monkeypatch):
    module, image, _ = runner
    argv = []

    class FakeProcess:
        returncode = 0

        def __init__(self, args, **kwargs):
            argv.extend(args)

        def communicate(self, timeout):
            return b"ok", None

    monkeypatch.setattr(module.subprocess, "Popen", FakeProcess)
    code, stdout = module._run_container("task-" + "1" * 24, "/tmp/workspace", "/opt/bundle/report/entrypoint.py", image, timeout=10)
    assert (code, stdout) == (0, b"ok")
    assert argv[-2:] == [image, "/opt/bundle/report/entrypoint.py"]
    assert argv[argv.index("--pull") + 1] == "never"
    assert argv[argv.index("--network") + 1] == "none"


def test_runner_build_applies_resource_limits(runner, monkeypatch, tmp_path):
    module, image, _ = runner
    calls: list[list[str]] = []

    class Result:
        def __init__(self, stdout: str = "", returncode: int = 0):
            self.stdout = stdout
            self.returncode = returncode

    def fake_run(argv, **kwargs):
        calls.append([str(item) for item in argv])
        if argv[:2] == ["docker", "build"]:
            return Result()
        if argv[:3] == ["docker", "image", "inspect"]:
            if argv[3] == "hdb-sandbox-bundle:1.0":
                return Result(image)
            return Result("sha256:" + "b" * 64)
        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "GENERATED_BUNDLE_ROOT", str(tmp_path / "generated"))
    monkeypatch.setattr(module, "WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setattr(module, "_skill_build_base_allowed", lambda digest: None)
    monkeypatch.setattr(
        module, "_load_bundle",
        lambda ref: {"image_digest": image, "profile_ref": "profile/dev-l1"},
    )
    monkeypatch.setattr(
        module, "_validate_skill_archive",
        lambda raw, digest: ({"entrypoint.py": b"print('ok')\n", "SKILL.md": b"# ok\n"}, digest),
    )
    module._build_skill_bundle({
        "skill_ref": "skill/report",
        "package_digest": "c" * 64,
        "base_bundle_ref": "bundle/report",
        "profile_ref": "profile/dev-l1",
        "package_base64": base64.b64encode(b"zip-bytes").decode("ascii"),
        "input_schema": {},
        "required_capabilities": ["sandbox.execute"],
    })
    build = next(call for call in calls if call[:2] == ["docker", "build"])
    assert build[build.index("--resource") + 1] == "memory=1g"
    assert "cpu-quota=100000" in build


def test_runner_purges_generated_bundle_only_when_idle(runner, monkeypatch, tmp_path):
    module, image, _ = runner
    generated = tmp_path / "generated"
    bundle_dir = generated / "skill-purge-abc"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "bundle.json").write_text(
        json.dumps({"bundle_ref": "bundle/skill-purge-abc", "image_digest": image}),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "GENERATED_BUNDLE_ROOT", str(generated))
    monkeypatch.setattr(module, "_reconcile_tasks", lambda: {"reconciled": True})
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""

    def fake_run(argv, **kwargs):
        calls.append([str(item) for item in argv])
        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    path = "/v1/generated-bundles/bundle/skill-purge-abc/purge"
    hdb = {"Authorization": "Bearer hdb-secret", "X-Audience": "sandbox-runner"}
    gateway = {"Authorization": "Bearer gateway-secret", "X-Audience": "sandbox-runner"}
    with TestClient(module.app) as client:
        assert client.post(path, headers=gateway).status_code == 401
        assert client.post(path, headers={**hdb, "X-Audience": "sandbox-gateway"}).status_code == 403
        conn = module._connect()
        try:
            conn.execute(
                "INSERT INTO runner_tasks(task_id, bundle_ref, bundle_digest, profile_ref, status, created_at, updated_at) "
                "VALUES(?, ?, ?, 'profile/dev-l1', 'running', 'now', 'now')",
                ("task-" + "1" * 24, "bundle/skill-purge-abc", image),
            )
        finally:
            conn.close()
        blocked = client.post(path, headers=hdb)
        assert blocked.status_code == 409, blocked.text
        conn = module._connect()
        try:
            conn.execute(
                "UPDATE runner_tasks SET status = 'succeeded' WHERE bundle_ref = ?",
                ("bundle/skill-purge-abc",),
            )
        finally:
            conn.close()
        ok = client.post(path, headers=hdb)
        assert ok.status_code == 200, ok.text
        body = ok.json()
    assert body["purged"] is True
    assert body["image_removed"] is True
    assert not bundle_dir.exists()
    assert ["docker", "rmi", image] in calls


def test_runner_rejects_unsafe_or_colliding_task_identifiers(runner):
    module, _, _ = runner
    with pytest.raises(HTTPException):
        module._require_task_id("task-../../etc/passwd")
    first = "task-" + "a" * 24 + "x"
    second = "task-" + "a" * 24 + "y"
    assert module._task_container(first) != module._task_container(second)


def test_runner_restart_marks_abandoned_execution_unknown(runner, monkeypatch, tmp_path):
    module, _, _ = runner
    monkeypatch.setattr(module, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(module, "WORKSPACE_ROOT", str(tmp_path / "work"))
    module._init_db()
    task_id = "task-" + "a" * 24
    module._insert_task(task_id=task_id, bundle_ref="bundle/slow", bundle_digest="b" * 64, profile_ref="profile/dev-l1")
    module._update_task(task_id, status="running")
    workspace = tmp_path / "work" / task_id
    workspace.mkdir(parents=True)
    (workspace / "parameters.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=""))
    result = module._reconcile_tasks()
    assert result["removed_count"] == 0
    task = module._get_task(task_id)
    assert task["status"] == "unknown"
    assert task["cleanup_state"] == "complete"
    assert not workspace.exists()


def test_reconcile_only_confirms_cancel_requested_or_marked_cleanup(runner, monkeypatch, tmp_path):
    module, _, _ = runner
    monkeypatch.setattr(module, "WORKSPACE_ROOT", str(tmp_path / "work"))
    module._init_db()
    statuses = {
        "a": ("cancel_requested", "", "cancelled"),
        "b": ("cleanup_pending", "cancel_cleanup_pending", "cancelled"),
        "c": ("cleanup_pending", "", "unknown"),
        "d": ("unknown", "restart_reconciled", "unknown"),
    }
    for key, (status, code, _) in statuses.items():
        task_id = "task-" + key * 24
        module._insert_task(task_id=task_id, bundle_ref="bundle/slow", bundle_digest="a" * 64, profile_ref="profile/dev-l1")
        module._update_task(task_id, status=status, error_code=code)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=""))
    assert module._reconcile_tasks()["removed_count"] == 0
    for key, (_, _, expected) in statuses.items():
        assert module._get_task("task-" + key * 24)["status"] == expected


def test_runner_idempotency_binds_payload_and_supports_pending_retry(runner, monkeypatch):
    module, _, _ = runner
    digest = "b" * 64
    monkeypatch.setattr(module, "_reconcile_tasks", lambda: {"reconciled": True})
    monkeypatch.setattr(module, "_load_bundle", lambda ref: {
        "digest": digest,
        "profile_ref": "profile/dev-l1",
        "input_schema": {},
    })

    def execute(task, bundle, params):
        module._update_task(task["task_id"], status="succeeded", observed_state="succeeded", execution_id="exec-reviewed")

    monkeypatch.setattr(module, "_execute_task", execute)
    headers = {"Authorization": "Bearer hdb-secret", "X-Audience": "sandbox-runner", "Idempotency-Key": "idem-review"}
    payload = {
        "task_id": "pending", "bundle_ref": "bundle/report", "bundle_digest": digest,
        "profile_ref": "profile/dev-l1", "parameters": {"rows": 1},
    }
    with TestClient(module.app) as client:
        first = client.post("/v1/tasks", json=payload, headers=headers)
        assert first.status_code == 200
        task_id = first.json()["task_id"]
        replay = client.post("/v1/tasks", json=payload, headers=headers)
        assert replay.status_code == 200 and replay.json()["task_id"] == task_id
        different = client.post("/v1/tasks", json={**payload, "parameters": {"rows": 2}}, headers=headers)
        assert different.status_code == 409
        changed_id = client.post("/v1/tasks", json={**payload, "task_id": task_id, "parameters": {"rows": 2}}, headers={
            **headers, "Idempotency-Key": "idem-other",
        })
        assert changed_id.status_code == 409
        assert module._get_task(task_id)["request_fingerprint"]
        assert module._get_task(task_id)["status"] == "succeeded"


def test_runner_migrates_old_ledger_and_fails_closed_on_legacy_replay(runner, monkeypatch):
    module, _, _ = runner
    module._init_db()
    with module._connect() as conn:
        conn.execute("DROP TABLE runner_tasks")
        conn.execute("CREATE TABLE runner_tasks(task_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL DEFAULT '', "
                     "bundle_ref TEXT NOT NULL, bundle_digest TEXT NOT NULL, profile_ref TEXT NOT NULL, "
                     "kind TEXT NOT NULL DEFAULT 'task', execution_id TEXT NOT NULL DEFAULT '', "
                     "status TEXT NOT NULL DEFAULT 'queued', observed_state TEXT NOT NULL DEFAULT '', "
                     "cleanup_state TEXT NOT NULL DEFAULT 'pending', manifest_json TEXT NOT NULL DEFAULT '', "
                     "error_code TEXT NOT NULL DEFAULT '', container_name TEXT NOT NULL DEFAULT '', "
                     "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    module._init_db()
    task_id = "task-" + "f" * 24
    module._insert_task(task_id=task_id, idempotency_key="idem-legacy", bundle_ref="bundle/report",
                        bundle_digest="b" * 64, profile_ref="profile/dev-l1")
    monkeypatch.setattr(module, "_reconcile_tasks", lambda: {"reconciled": True})
    monkeypatch.setattr(module, "_load_bundle", lambda ref: {
        "digest": "b" * 64, "profile_ref": "profile/dev-l1", "input_schema": {},
    })
    with TestClient(module.app) as client:
        result = client.post("/v1/tasks", json={
            "task_id": task_id, "bundle_ref": "bundle/report", "bundle_digest": "b" * 64,
            "profile_ref": "profile/dev-l1", "parameters": {},
        }, headers={"Authorization": "Bearer hdb-secret", "X-Audience": "sandbox-runner", "Idempotency-Key": "idem-legacy"})
        assert result.status_code == 409


def test_gateway_rejects_runner_attestation_mismatch(monkeypatch):
    module = _load(ROOT / "deploy/mcp-gateway/gateway.py", "hdb_reference_gateway")
    deployment = module._deployment("deploy/sandbox-info")
    monkeypatch.setattr(module, "_token", lambda path=module.TOKEN_FILE: "hdb-secret")
    monkeypatch.setattr(module, "_runner_request", lambda *args: {
        "deployment_ref": "deploy/sandbox-info",
        "profile_ref": deployment["profile_ref"],
        "profile_digest": deployment["profile_digest"],
        "image_digest": "sha256:" + "0" * 64,
        "endpoint": "http://172.23.0.2:8080",
    })
    headers = {"Authorization": "Bearer hdb-secret", "X-Audience": "sandbox-gateway"}
    with TestClient(module.app) as client:
        response = client.post("/v1/gateway/attach", json={
            "deployment_ref": "deploy/sandbox-info", "profile_ref": deployment["profile_ref"],
        }, headers=headers)
    assert response.status_code == 503
    assert "deploy/sandbox-info" not in module._attached


def test_gateway_disable_is_served_while_invoke_waits_on_bridge(monkeypatch):
    module = _load(ROOT / "deploy/mcp-gateway/gateway.py", "hdb_reference_gateway_concurrent")
    deployment = module._deployment("deploy/sandbox-info")
    endpoint = "http://172.23.0.2:8080"
    module._attached["deploy/sandbox-info"] = {"endpoint": endpoint, "execution_id": "svc-" + "1" * 64}
    monkeypatch.setattr(module, "_token", lambda path=module.TOKEN_FILE: "hdb-secret")

    def runner_request(method, path, payload=None):
        if method == "DELETE":
            return {}
        return {
            "deployment_ref": "deploy/sandbox-info",
            "profile_ref": deployment["profile_ref"],
            "profile_digest": deployment["profile_digest"],
            "image_digest": deployment["image_digest"],
            "endpoint": endpoint,
            "execution_id": "svc-" + "1" * 64,
        }

    monkeypatch.setattr(module, "_runner_request", runner_request)
    started = threading.Event()
    release = threading.Event()

    def slow_bridge(endpoint, tool_name, arguments):
        started.set()
        assert release.wait(5)
        return {"result": {"content": []}}

    monkeypatch.setattr(module, "_bridge_invoke", slow_bridge)
    headers = {"Authorization": "Bearer hdb-secret", "X-Audience": "sandbox-gateway"}

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test") as client:
            invocation = asyncio.create_task(client.post(
                "/v1/gateway/deploy/sandbox-info/invoke",
                headers=headers,
                json={"tool_name": "sandbox_runtime_info", "arguments": {}},
            ))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                disabled = await asyncio.wait_for(
                    client.post("/v1/gateway/deploy/sandbox-info/disable", headers=headers), timeout=2
                )
                assert disabled.status_code == 200
                assert "deploy/sandbox-info" not in module._attached
            finally:
                release.set()
            assert (await invocation).status_code == 409

    asyncio.run(exercise())


def test_gateway_detects_same_image_service_instance_replacement(monkeypatch):
    module = _load(ROOT / "deploy/mcp-gateway/gateway.py", "hdb_reference_gateway_replaced")
    deployment = module._deployment("deploy/sandbox-info")
    module._attached["deploy/sandbox-info"] = {
        "endpoint": "http://172.23.0.2:8080", "execution_id": "svc-" + "1" * 64,
    }
    monkeypatch.setattr(module, "_runner_request", lambda *args: {
        "deployment_ref": "deploy/sandbox-info",
        "profile_ref": deployment["profile_ref"],
        "profile_digest": deployment["profile_digest"],
        "image_digest": deployment["image_digest"],
        "endpoint": "http://172.23.0.2:8080",
        "execution_id": "svc-" + "2" * 64,
    })
    with pytest.raises(HTTPException) as error:
        module._verified_endpoint("deploy/sandbox-info", deployment)
    assert error.value.detail["error"]["code"] == "service_mismatch"
    assert "deploy/sandbox-info" not in module._attached


def test_gateway_detaches_before_remote_stop_failure(monkeypatch):
    module = _load(ROOT / "deploy/mcp-gateway/gateway.py", "hdb_reference_gateway_stop_failed")
    module._attached["deploy/sandbox-info"] = {"endpoint": "http://172.23.0.2:8080"}
    monkeypatch.setattr(module, "_token", lambda path=module.TOKEN_FILE: "hdb-secret")
    monkeypatch.setattr(module, "_runner_request", lambda *args: (_ for _ in ()).throw(
        module._safe_error("runner_unavailable", "not reachable", status=503)
    ))
    headers = {"Authorization": "Bearer hdb-secret", "X-Audience": "sandbox-gateway"}
    with TestClient(module.app) as client:
        stopped = client.post("/v1/gateway/deploy/sandbox-info/disable", headers=headers)
    assert stopped.status_code == 503
    assert "deploy/sandbox-info" not in module._attached


def test_hdb_gateway_registry_binds_reviewed_image_and_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setattr(settings, "SANDBOX_RUNTIME_PROFILE_DIGEST", "b" * 64)
    registry = GatewayRegistry(str(tmp_path / "gateway.db"))
    registry.register(
        deployment_ref="deploy/reviewed",
        profile_ref="profile/dev-l1",
        endpoint="https://gateway.example",
        digest="sha256:" + "a" * 64,
        tool_allowlist={"read_info": {"type": "object"}},
    )
    registry.review("deploy/reviewed")
    registry.mark_tested("deploy/reviewed")
    registry.enable("deploy/reviewed")

    class Client:
        gateway_url = "https://gateway.example"
        image_digest = "sha256:" + "c" * 64

        def gateway_attach(self, **kwargs):
            return {
                "attached": True,
                "deployment_ref": kwargs["deployment_ref"],
                "profile_ref": kwargs["profile_ref"],
                "profile_digest": "b" * 64,
                "image_digest": self.image_digest,
            }

    client = Client()
    with pytest.raises(GatewayStateError, match="reviewed image"):
        registry.attach("deploy/reviewed", client=client)
    client.image_digest = "sha256:" + "a" * 64
    assert registry.attach("deploy/reviewed", client=client)["attached"] is True
    assert registry.require("deploy/reviewed").health_status == "healthy"


def test_reviewed_gateway_can_be_reenabled_after_disable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_GATEWAY_URL", "https://gateway.example")
    registry = GatewayRegistry(str(tmp_path / "gateway.db"))
    registry.register(deployment_ref="deploy/reviewed", profile_ref="profile/dev-l1",
                      endpoint="https://gateway.example", digest="a" * 64,
                      tool_allowlist={"read_info": {"type": "object"}})
    registry.review("deploy/reviewed")
    registry.mark_tested("deploy/reviewed")
    registry.enable("deploy/reviewed")
    assert registry.disable("deploy/reviewed").enabled is False
    restored = registry.enable("deploy/reviewed")
    assert restored.approval_state == "enabled" and restored.enabled is True


def test_gateway_attach_cleans_up_if_local_disable_wins_race(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setattr(settings, "SANDBOX_RUNTIME_PROFILE_DIGEST", "b" * 64)
    ref = "deploy/reviewed"
    registry = GatewayRegistry(str(tmp_path / "gateway.db"))
    registry.register(deployment_ref=ref, profile_ref="profile/dev-l1",
                      endpoint="https://gateway.example", digest="a" * 64,
                      tool_allowlist={"read_info": {"type": "object"}})
    registry.review(ref)
    registry.mark_tested(ref)
    registry.enable(ref)

    class Client:
        gateway_url = "https://gateway.example"
        stopped = False

        def gateway_attach(self, **kwargs):
            registry.disable(ref)
            return {
                "attached": True, "deployment_ref": ref, "profile_ref": "profile/dev-l1",
                "profile_digest": "b" * 64, "image_digest": "sha256:" + "a" * 64,
            }

        def gateway_disable(self, deployment_ref, *, envelope):
            self.stopped = True
            return {"disabled": True}

    client = Client()
    with pytest.raises(GatewayStateError, match="changed during attachment"):
        registry.attach(ref, client=client)
    assert client.stopped is True
    assert registry.require(ref).enabled is False


def test_hdb_disable_and_revoke_keep_local_deny_on_remote_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_GATEWAY_URL", "https://gateway.example")
    registry = GatewayRegistry(str(tmp_path / "gateway.db"))
    ref = "deploy/reviewed"
    registry.register(deployment_ref=ref, profile_ref="profile/dev-l1",
                      endpoint="https://gateway.example", digest="a" * 64,
                      tool_allowlist={"read_info": {"type": "object"}})
    registry.review(ref)
    registry.mark_tested(ref)
    registry.enable(ref)
    actor = SimpleNamespace(username="admin", id=1, department_id=1)
    audit = []
    monkeypatch.setattr(sandbox_routes, "_audit", lambda *args, **kwargs: audit.append(kwargs))

    class Client:
        fail = True
        calls = []

        def gateway_disable(self, deployment_ref, *, envelope):
            self.calls.append("disable")
            if self.fail:
                raise OSError("temporary failure")
            return {"disabled": True}

        def gateway_revoke(self, deployment_ref, *, envelope):
            self.calls.append("revoke")
            if self.fail:
                raise OSError("temporary failure")
            return {"revoked": True}

    client = Client()
    with pytest.raises(HTTPException, match="remote cleanup pending"):
        sandbox_routes.disable_gateway(ref, actor=actor, registry=registry, client=client)
    assert registry.require(ref).enabled is False
    assert registry.require(ref).health_status == "cleanup_pending"
    assert any(event["action"] == "plugins.mcp.gateway_cleanup_pending" and not event["success"] for event in audit)
    client.fail = False
    result = sandbox_routes.disable_gateway(ref, actor=actor, registry=registry, client=client)
    assert result.gateway["health_status"] == "stopped"
    client.fail = True
    with pytest.raises(HTTPException, match="remote cleanup pending"):
        sandbox_routes.revoke_gateway(ref, actor=actor, registry=registry, client=client)
    assert registry.require(ref).approval_state == "revoked"
    assert registry.require(ref).health_status == "cleanup_pending"
    client.fail = False
    result = sandbox_routes.revoke_gateway(ref, actor=actor, registry=registry, client=client)
    assert result.gateway["health_status"] == "stopped"
    assert client.calls == ["disable", "disable", "revoke", "revoke"]


def test_broker_concurrent_same_artifact_retries_preserve_durable_bytes(tmp_path, monkeypatch):
    broker = ArtifactBroker(tmp_path / "artifacts", str(tmp_path / "broker.db"), scanner=lambda *_: True)
    content = b"reviewed output"
    artifact_id = "art-" + "a" * 32
    kwargs = {
        "task_id": "task-" + "b" * 24,
        "owner_id": "owner-1",
        "department_id": "department-1",
        "filename": "report.txt",
        "content": content,
        "sha256": hashlib.sha256(content).hexdigest(),
        "execution_id": "exec-reviewed",
        "artifact_id": artifact_id,
    }
    original = broker._write_atomic
    both_ready = threading.Barrier(2)

    def concurrent_write(storage_ref, data):
        both_ready.wait(timeout=5)
        return original(storage_ref, data)

    monkeypatch.setattr(broker, "_write_atomic", concurrent_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: broker.stage_output(**kwargs), range(2)))
    assert [item.artifact_id for item in results] == [artifact_id, artifact_id]
    broker.scan(artifact_id, task_id=kwargs["task_id"], department_id="department-1")
    broker.commit(artifact_id, task_id=kwargs["task_id"], department_id="department-1")
    assert broker.read(artifact_id, task_id=kwargs["task_id"], department_id="department-1") == content
    with pytest.raises(ArtifactStateConflict):
        broker.stage_output(**{**kwargs, "department_id": "other"})
    payload = tmp_path / "artifacts" / artifact_id / "payload"
    payload.unlink()
    with pytest.raises(ArtifactBrokerError, match="missing or invalid"):
        broker.stage_output(**kwargs)
