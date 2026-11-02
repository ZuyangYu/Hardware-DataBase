"""Lifecycle, authorization and schema tests for the sandbox control plane.

These tests exercise the HDB-side state machine with a fake Runner client.
They are deliberately not runtime/security acceptance evidence: no external
Runner, gateway, OCI runtime or scanner is started here.
"""

from __future__ import annotations

import base64
import hashlib
import os

import pytest
from fastapi import HTTPException

import src.settings as settings
from src.agents.mcp.gateway import (
    GatewayPolicyError,
    GatewayRegistry,
    GatewayStateError,
)
from src.sandbox.artifact_broker import ArtifactBroker
from src.sandbox.catalog import BundleNotAuthorized, SandboxCatalog
from src.sandbox.dependencies import close_service_client, configure_service_client_factory
from src.sandbox.models import SandboxTaskSpec
from src.sandbox.protocol import RequestEnvelope, RunnerObservation
from src.sandbox.service import SandboxExecutionAdapter, SandboxExecutionError
from src.sandbox.skills import SkillCatalog, SkillNotAuthorized
from src.sandbox.task_store import SandboxTaskStore


def _enable_sandbox(monkeypatch, **extra):
    values = {
        "SANDBOX_ENABLED": True,
        "SANDBOX_BACKEND": "runner_api",
        "SANDBOX_RUNNER_URL": "https://runner.example",
        "SANDBOX_SERVICE_AUTH_REF": "workload://hdb",
        "SANDBOX_REQUIRE_RUNTIME_ATTESTATION": False,
        "SANDBOX_ALLOW_DYNAMIC_INSTALL": False,
        "SANDBOX_DEFAULT_NETWORK": "none",
        "SANDBOX_MAX_CONCURRENT": 4,
        "SANDBOX_MAX_CONCURRENT_PER_USER": 4,
        "SANDBOX_MAX_CONCURRENT_PER_DEPARTMENT": 4,
        "SANDBOX_ARTIFACT_ALLOWED_EXTENSIONS": "txt,json",
        "SANDBOX_ARTIFACT_SCANNER_REF": "",
    }
    values.update(extra)
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)


def _runtime(tmp_path, monkeypatch, *, output_schema=None, capabilities=("sandbox.execute", "sandbox.write_artifact")):
    _enable_sandbox(monkeypatch)
    db_path = str(tmp_path / "sandbox.db")
    store = SandboxTaskStore(db_path)
    catalog = SandboxCatalog(db_path)
    broker = ArtifactBroker(str(tmp_path / "artifacts"), db_path)
    bundle = catalog.register(
        bundle_ref="bundle/demo",
        bundle_digest="a" * 64,
        profile_ref="profile/demo",
        required_capabilities=capabilities,
        output_schema=output_schema or {},
        input_schema={
            "type": "object",
            "properties": {"input_ref": {"type": "string", "maxLength": 64}},
            "required": ["input_ref"],
            "additionalProperties": False,
        },
    )
    catalog.review(bundle.bundle_ref)
    catalog.mark_tested(bundle.bundle_ref)
    bundle = catalog.enable(bundle.bundle_ref)
    return store, catalog, broker, bundle


def _spec(bundle, **kwargs):
    values = {
        "idempotency_key": "request-1",
        "owner_id": "user-1",
        "department_id": "dept-1",
        "bundle_ref": bundle.bundle_ref,
        "bundle_digest": bundle.bundle_digest,
        "profile_ref": bundle.profile_ref,
        "policy_snapshot_hash": bundle.policy_snapshot_hash,
        "required_capabilities": ("sandbox.execute", "sandbox.write_artifact"),
        "parameters": {"input_ref": "art-1234567890abcdef"},
    }
    values.update(kwargs)
    return SandboxTaskSpec(**values)


class _FakeRunner:
    """Bounded in-process stand-in for the external Runner HTTP API."""

    def __init__(self) -> None:
        self.submit_error: Exception | None = None
        self.observe_status = "succeeded"
        self.collect_response: dict | None = None
        self.cancelled = False
        self.observed_envelopes: list[RequestEnvelope] = []

    def submit(self, **kwargs):
        if self.submit_error is not None:
            raise self.submit_error
        return {"execution_id": "exec-1", "observed_state": "running"}

    def observe(self, task_id, *, envelope=None):
        self.observed_envelopes.append(envelope)
        return RunnerObservation(
            task_id=task_id,
            status=self.observe_status,
            observed_state=self.observe_status,
            execution_id="exec-1",
            cleanup_state="running",
        )

    def collect(self, task_id, *, envelope=None):
        if self.collect_response is None:
            raise AssertionError("collect response was not configured")
        return self.collect_response

    def cancel(self, task_id, *, envelope=None):
        self.cancelled = True
        return {"status": "cancelled", "cleanup_state": "complete"}


def _inline_output(content: bytes, filename: str = "out.txt", **extra):
    item = {
        "filename": filename,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "sha256": hashlib.sha256(content).hexdigest(),
        "execution_id": "exec-1",
    }
    item.update(extra)
    return {"status": "succeeded", "execution_id": "exec-1", "output_manifest": {"items": [item]}}


def test_lifecycle_submit_observe_collect_commits_verified_output(tmp_path, monkeypatch):
    store, catalog, broker, bundle = _runtime(tmp_path, monkeypatch)
    runner = _FakeRunner()
    adapter = SandboxExecutionAdapter(store=store, catalog=catalog, broker=broker, client=runner)
    task = adapter.admit(_spec(bundle))
    assert task.status == "admitted"

    task = adapter.submit(task.task_id)
    assert task.status == "running" and task.execution_id == "exec-1"

    task = adapter.observe(task.task_id)
    assert task.status == "collecting"

    content = b"verified output"
    runner.collect_response = _inline_output(content)
    result = adapter.collect(task.task_id)
    assert result["task"]["status"] == "succeeded"
    items = result["output_manifest"]["items"]
    assert len(items) == 1 and items[0]["state"] == "committed"

    artifact_id = items[0]["artifact_id"]
    assert broker.read(artifact_id, principal_id="user-1", department_id="dept-1") == content
    committed = broker.require(artifact_id)
    assert committed.role == "output" and committed.task_id == task.task_id


def test_observe_persists_skill_runtime_events_and_business_result(tmp_path, monkeypatch):
    store, catalog, broker, bundle = _runtime(tmp_path, monkeypatch)
    runner = _FakeRunner()
    adapter = SandboxExecutionAdapter(store=store, catalog=catalog, broker=broker, client=runner)
    task = adapter.admit(_spec(bundle))
    task = adapter.submit(task.task_id)

    def observe_with_runtime(task_id, *, envelope=None):
        return RunnerObservation(
            task_id=task_id,
            status="running",
            observed_state="running",
            execution_id="exec-1",
            cleanup_state="running",
            result={
                "runtime_contract_version": "v1",
                "result_status": "validation_failed",
                "issues": [{"code": "missing_interface"}],
            },
            runtime_events=(
                {
                    "runtime_contract_version": "v1",
                    "event_id": "evt-observe-1",
                    "seq": 1,
                    "task_id": task_id,
                    "attempt": 1,
                    "step_id": "validate",
                    "event": "validation_issue",
                    "status": "failed",
                    "progress": 0.75,
                    "message": "missing interface",
                },
            ),
        )

    runner.observe = observe_with_runtime
    observed = adapter.observe(task.task_id)

    assert observed.status == "running"
    assert observed.result_status == "validation_failed"
    assert observed.latest_step_id == "validate"
    assert observed.latest_progress == 0.75
    assert store.runtime_events(task.task_id)[0]["event"] == "validation_issue"

    # Polling replays the same event snapshot; it must not duplicate events.
    adapter.observe(task.task_id)
    assert len(store.runtime_events(task.task_id)) == 1


def test_submit_forwards_authorized_input_artifact_bytes(tmp_path, monkeypatch):
    store, catalog, broker, bundle = _runtime(
        tmp_path,
        monkeypatch,
        capabilities=("sandbox.execute", "sandbox.write_artifact", "sandbox.internal_data"),
    )
    monkeypatch.setattr(settings, "SANDBOX_ARTIFACT_ALLOWED_EXTENSIONS", "csv,txt,json")
    content = b"pin,function\n1,gpio\n"
    staged = broker.stage_input(
        task_id="task-source-input-123456",
        owner_id="user-1",
        department_id="dept-1",
        filename="pins.csv",
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    broker.scan(staged.artifact_id, department_id="dept-1", task_id="task-source-input-123456")
    broker.commit(staged.artifact_id, department_id="dept-1", task_id="task-source-input-123456")

    runner = _FakeRunner()
    captured = {}

    def submit(**kwargs):
        captured.update(kwargs)
        return {"execution_id": "exec-1", "observed_state": "queued"}

    runner.submit = submit
    adapter = SandboxExecutionAdapter(store=store, catalog=catalog, broker=broker, client=runner)
    task = adapter.admit(
        _spec(
            bundle,
            required_capabilities=(
                "sandbox.execute",
                "sandbox.write_artifact",
                "sandbox.internal_data",
            ),
            input_artifact_ids=(staged.artifact_id,),
        )
    )
    adapter.submit(task.task_id)

    transfer = captured["input_artifacts"]
    assert len(transfer) == 1
    assert transfer[0]["artifact_id"] == staged.artifact_id
    assert base64.b64decode(transfer[0]["content_base64"]) == content
    assert transfer[0]["sha256"] == hashlib.sha256(content).hexdigest()


def test_needs_user_input_collect_succeeds_without_final_artifacts(tmp_path, monkeypatch):
    store, catalog, broker, bundle = _runtime(tmp_path, monkeypatch)
    runner = _FakeRunner()
    adapter = SandboxExecutionAdapter(store=store, catalog=catalog, broker=broker, client=runner)
    task = adapter.submit(adapter.admit(_spec(bundle)).task_id)
    task = adapter.observe(task.task_id)
    assert task.status == "collecting"

    runner.collect_response = {
        "status": "succeeded",
        "execution_id": "exec-1",
        "output_manifest": {"items": []},
        "result": {
            "runtime_contract_version": "v1",
            "result_status": "needs_user_input",
            "required_inputs": ["pin_map.csv"],
            "issues": [{"code": "missing_pin_map"}],
        },
    }
    result = adapter.collect(task.task_id)

    assert result["task"]["status"] == "succeeded"
    assert result["task"]["result_status"] == "needs_user_input"
    assert result["task"]["skill_result"]["required_inputs"] == ["pin_map.csv"]
    assert result["output_manifest"] == {"items": []}


def test_expired_input_artifact_is_rejected(tmp_path, monkeypatch):
    _, _, broker, _ = _runtime(tmp_path, monkeypatch)
    content = b"expired"
    staged = broker.stage_input(
        task_id="task-expired-1234567890",
        owner_id="user-1",
        department_id="dept-1",
        filename="old.txt",
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    broker.scan(staged.artifact_id, department_id="dept-1", task_id="task-expired-1234567890")
    broker.commit(staged.artifact_id, department_id="dept-1", task_id="task-expired-1234567890")
    conn = broker._connect()
    try:
        conn.execute(
            "UPDATE sandbox_artifacts SET expires_at = '2020-01-01T00:00:00+00:00' "
            "WHERE artifact_id = ?",
            (staged.artifact_id,),
        )
    finally:
        conn.close()

    with pytest.raises(Exception, match="expired"):
        broker.authorize_input(staged.artifact_id, principal_id="user-1", department_id="dept-1")


def test_failed_collect_commits_validation_report(tmp_path, monkeypatch):
    store, catalog, broker, bundle = _runtime(tmp_path, monkeypatch)
    runner = _FakeRunner()
    adapter = SandboxExecutionAdapter(store=store, catalog=catalog, broker=broker, client=runner)
    task = adapter.submit(adapter.admit(_spec(bundle)).task_id)
    task = adapter.observe(task.task_id)
    assert task.status == "collecting"

    response = _inline_output(b'{"error": "missing_interface"}', filename="report.json")
    response["status"] = "failed"
    response["error_code"] = "bundle_failed"
    response["result"] = {
        "runtime_contract_version": "v1",
        "result_status": "validation_failed",
        "issues": [{"code": "missing_interface"}],
    }
    runner.collect_response = response
    result = adapter.collect(task.task_id)

    assert result["task"]["status"] == "failed"
    assert result["task"]["result_status"] == "validation_failed"
    items = result["task"]["output_manifest"]["items"]
    assert len(items) == 1 and items[0]["state"] == "committed"
    assert broker.read(
        items[0]["artifact_id"], principal_id="user-1", department_id="dept-1",
    ) == b'{"error": "missing_interface"}'


def test_task_lineage_and_parent_artifact_diff(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from src.api.routes import sandbox as routes

    store, _, broker, bundle = _runtime(tmp_path, monkeypatch)
    parent = store.create(_spec(bundle, idempotency_key="parent-run"))
    child = store.create(_spec(
        bundle, idempotency_key="child-run", parent_task_id=parent.task_id,
    ))
    user = SimpleNamespace(id="user-1", department_id="dept-1", role="employee")

    def output(task_id: str, content: bytes):
        staged = broker.stage_output(
            task_id=task_id, owner_id="user-1", department_id="dept-1",
            filename="result.json", content=content,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        broker.scan(staged.artifact_id, department_id="dept-1", task_id=task_id)
        return broker.commit(staged.artifact_id, department_id="dept-1", task_id=task_id)

    previous = output(parent.task_id, b'{"nodes": 1}\n')
    current = output(child.task_id, b'{"nodes": 2}\n')
    payload = routes.diff_artifact_with_parent(
        child.task_id, current.artifact_id, user=user, store=store, broker=broker,
    )
    assert payload["base_artifact_id"] == previous.artifact_id
    assert payload["changed"] is True
    assert payload["added_lines"] == 1 and payload["removed_lines"] == 1
    assert '+{"nodes": 2}' in payload["diff"]

    lineage = routes.task_lineage(child.task_id, user=user, store=store)
    assert [item["task_id"] for item in lineage["items"]] == [child.task_id, parent.task_id]

    outsider = SimpleNamespace(id="user-2", department_id="dept-1", role="employee")
    with pytest.raises(HTTPException) as denied:
        routes.diff_artifact_with_parent(
            child.task_id, current.artifact_id, user=outsider, store=store, broker=broker,
        )
    assert denied.value.status_code == 404


def test_output_promotion_creates_new_input_with_lineage(tmp_path, monkeypatch):
    _, _, broker, _ = _runtime(tmp_path, monkeypatch)
    content = b"promoted report"
    staged = broker.stage_output(
        task_id="task-output-1234567890",
        owner_id="user-1",
        department_id="dept-1",
        filename="report.txt",
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    broker.scan(staged.artifact_id, department_id="dept-1", task_id="task-output-1234567890")
    output = broker.commit(staged.artifact_id, department_id="dept-1", task_id="task-output-1234567890")

    promoted = broker.promote_output(
        output.artifact_id, principal_id="user-1", department_id="dept-1",
    )

    assert promoted.artifact_id != output.artifact_id
    assert promoted.role == "input" and promoted.state == "committed"
    assert promoted.sha256 == output.sha256
    assert promoted.metadata["source_artifact_id"] == output.artifact_id
    assert broker.authorize_input(
        promoted.artifact_id, principal_id="user-1", department_id="dept-1",
    ).artifact_id == promoted.artifact_id


def test_bind_inputs_requires_authorized_capability_and_recomputes_fingerprint(tmp_path, monkeypatch):
    store, catalog, broker, bundle = _runtime(
        tmp_path,
        monkeypatch,
        capabilities=("sandbox.execute", "sandbox.write_artifact", "sandbox.internal_data"),
    )
    monkeypatch.setattr(settings, "SANDBOX_ARTIFACT_ALLOWED_EXTENSIONS", "csv,txt,json")
    task = store.create(
        _spec(
            bundle,
            required_capabilities=(
                "sandbox.execute",
                "sandbox.write_artifact",
                "sandbox.internal_data",
            ),
            input_artifact_ids=(),
        )
    )
    content = b"a,b\n1,2\n"
    staged = broker.stage_input(
        task_id="task-source-input-123456",
        owner_id="user-1",
        department_id="dept-1",
        filename="pins.csv",
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    broker.scan(staged.artifact_id, department_id="dept-1", task_id="task-source-input-123456")
    broker.commit(staged.artifact_id, department_id="dept-1", task_id="task-source-input-123456")

    original_fingerprint = task.request_fingerprint
    bound = store.bind_inputs(task.task_id, (staged.artifact_id,))
    assert bound.input_artifact_ids == (staged.artifact_id,)
    assert bound.request_fingerprint != original_fingerprint


def test_collect_rejects_uncommitted_artifact_and_marks_reconcile(tmp_path, monkeypatch):
    store, catalog, broker, bundle = _runtime(tmp_path, monkeypatch)
    runner = _FakeRunner()
    adapter = SandboxExecutionAdapter(store=store, catalog=catalog, broker=broker, client=runner)
    task = adapter.admit(_spec(bundle))
    task = adapter.submit(task.task_id)
    task = adapter.observe(task.task_id)

    staged = broker.stage_output(
        task_id=task.task_id,
        owner_id="user-1",
        department_id="dept-1",
        filename="staged.txt",
        content=b"not committed",
        sha256=hashlib.sha256(b"not committed").hexdigest(),
    )
    runner.collect_response = {
        "status": "succeeded",
        "output_manifest": {"items": [{"artifact_id": staged.artifact_id}]},
    }
    with pytest.raises(SandboxExecutionError):
        adapter.collect(task.task_id)
    assert store.require(task.task_id).status == "reconcile_required"


def test_collect_rejects_foreign_artifact_ownership(tmp_path, monkeypatch):
    store, catalog, broker, bundle = _runtime(tmp_path, monkeypatch)
    runner = _FakeRunner()
    adapter = SandboxExecutionAdapter(store=store, catalog=catalog, broker=broker, client=runner)
    task = adapter.admit(_spec(bundle))
    task = adapter.submit(task.task_id)
    task = adapter.observe(task.task_id)

    foreign = broker.stage_output(
        task_id="task-other",
        owner_id="user-2",
        department_id="dept-2",
        filename="foreign.txt",
        content=b"foreign",
        sha256=hashlib.sha256(b"foreign").hexdigest(),
    )
    runner.collect_response = {
        "status": "succeeded",
        "output_manifest": {"items": [{"artifact_id": foreign.artifact_id}]},
    }
    with pytest.raises(SandboxExecutionError):
        adapter.collect(task.task_id)
    assert store.require(task.task_id).status == "reconcile_required"


def test_collect_enforces_reviewed_output_schema(tmp_path, monkeypatch):
    output_schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "minItems": 2}},
        "required": ["items"],
        "additionalProperties": False,
    }
    store, catalog, broker, bundle = _runtime(tmp_path, monkeypatch, output_schema=output_schema)
    runner = _FakeRunner()
    adapter = SandboxExecutionAdapter(store=store, catalog=catalog, broker=broker, client=runner)
    task = adapter.admit(_spec(bundle))
    task = adapter.submit(task.task_id)
    task = adapter.observe(task.task_id)
    runner.collect_response = _inline_output(b"schema output")
    with pytest.raises(SandboxExecutionError):
        adapter.collect(task.task_id)
    assert store.require(task.task_id).status == "reconcile_required"


def test_submit_unknown_side_effect_requires_reconciliation(tmp_path, monkeypatch):
    store, catalog, broker, bundle = _runtime(tmp_path, monkeypatch)
    runner = _FakeRunner()
    runner.submit_error = RuntimeError("connection reset")
    adapter = SandboxExecutionAdapter(store=store, catalog=catalog, broker=broker, client=runner)
    task = adapter.admit(_spec(bundle))
    task = adapter.submit(task.task_id)
    assert task.status == "reconcile_required"
    assert not task.lease_owner and task.error_code == "runner_submit_unknown"


def test_cancel_rejects_identity_mismatch(tmp_path, monkeypatch):
    store, catalog, broker, bundle = _runtime(tmp_path, monkeypatch)
    runner = _FakeRunner()
    adapter = SandboxExecutionAdapter(store=store, catalog=catalog, broker=broker, client=runner)
    task = adapter.admit(_spec(bundle))
    task = adapter.submit(task.task_id)
    foreign_envelope = RequestEnvelope.create(
        actor_id="other-user",
        department_id="dept-1",
        policy_snapshot_hash=task.policy_snapshot_hash,
    )
    with pytest.raises(SandboxExecutionError):
        adapter.cancel(task.task_id, actor_id="user-1", envelope=foreign_envelope)
    assert not runner.cancelled


def test_request_envelope_requires_hex_capability_hash():
    with pytest.raises(ValueError):
        RequestEnvelope.create(
            actor_id="user-1",
            department_id="dept-1",
            policy_snapshot_hash="policy-1",
            capability_snapshot_hash="not-a-digest",
        )
    envelope = RequestEnvelope.create(
        actor_id="user-1",
        department_id="dept-1",
        policy_snapshot_hash="policy-1",
        capability_snapshot_hash="sha256:" + "a" * 64,
    )
    assert envelope.capability_snapshot_hash == "a" * 64


def test_bundle_input_schema_rejects_unreviewed_parameters(tmp_path, monkeypatch):
    _store, catalog, _broker, bundle = _runtime(tmp_path, monkeypatch)
    with pytest.raises(BundleNotAuthorized):
        catalog.authorize(
            bundle_ref=bundle.bundle_ref,
            bundle_digest=bundle.bundle_digest,
            profile_ref=bundle.profile_ref,
            required_capabilities=("sandbox.execute", "sandbox.write_artifact"),
            parameters={"input_ref": "x" * 200},
        )
    with pytest.raises(BundleNotAuthorized):
        catalog.authorize(
            bundle_ref=bundle.bundle_ref,
            bundle_digest=bundle.bundle_digest,
            profile_ref=bundle.profile_ref,
            required_capabilities=("sandbox.execute", "sandbox.write_artifact"),
            parameters={"input_ref": "ok", "command": "rm -rf /"},
        )


def _gateway(monkeypatch, tmp_path, *, write_tools=("write",), capabilities=("external_tool.invoke", "external_tool.write")):
    monkeypatch.setattr(settings, "SANDBOX_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_GATEWAY_URL", "https://gateway.example")
    registry = GatewayRegistry(str(tmp_path / "gateway.db"))
    registry.register(
        deployment_ref="gateway/demo",
        profile_ref="profile/demo",
        endpoint="https://gateway.example",
        digest="b" * 64,
        capabilities=list(capabilities),
        tool_allowlist={
            "read": {
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 200}},
                "required": ["query"],
                "additionalProperties": False,
            },
            "write": {
                "type": "object",
                "properties": {"payload": {"type": "string", "maxLength": 200}},
                "required": ["payload"],
                "additionalProperties": False,
            },
        },
        metadata={"write_tools": list(write_tools)},
        actor="admin",
    )
    registry.review("gateway/demo", actor="admin")
    registry.mark_tested("gateway/demo", actor="admin")
    return registry.enable("gateway/demo", actor="admin")


def test_gateway_blocks_unlisted_tool_and_unreviewed_arguments(monkeypatch, tmp_path):
    item = _gateway(monkeypatch, tmp_path)
    registry = GatewayRegistry(str(tmp_path / "gateway.db"))
    with pytest.raises(GatewayPolicyError):
        registry.authorize_invocation(
            item.deployment_ref,
            user_id="user-1",
            department_id="dept-1",
            tool_name="shell",
            arguments={},
            capabilities={"external_tool.invoke"},
            is_admin=True,
        )
    with pytest.raises(GatewayPolicyError):
        registry.authorize_invocation(
            item.deployment_ref,
            user_id="user-1",
            department_id="dept-1",
            tool_name="read",
            arguments={"query": "x", "command": "rm -rf /"},
            capabilities={"external_tool.invoke"},
            is_admin=True,
        )


def test_gateway_requires_subscription_and_write_consent(monkeypatch, tmp_path):
    item = _gateway(monkeypatch, tmp_path)
    registry = GatewayRegistry(str(tmp_path / "gateway.db"))
    with pytest.raises(GatewayPolicyError):
        registry.authorize_invocation(
            item.deployment_ref,
            user_id="user-1",
            department_id="dept-1",
            tool_name="read",
            arguments={"query": "x"},
            capabilities={"external_tool.invoke"},
        )
    registry.subscribe(item.deployment_ref, user_id="user-1", department_id="dept-1")
    assert registry.authorize_invocation(
        item.deployment_ref,
        user_id="user-1",
        department_id="dept-1",
        tool_name="read",
        arguments={"query": "x"},
        capabilities={"external_tool.invoke"},
    )
    with pytest.raises(GatewayPolicyError):
        registry.authorize_invocation(
            item.deployment_ref,
            user_id="user-1",
            department_id="dept-1",
            tool_name="write",
            arguments={"payload": "x"},
            capabilities={"external_tool.invoke", "external_tool.write"},
        )
    registry.subscribe(item.deployment_ref, user_id="user-1", department_id="dept-1", allow_write=True)
    assert registry.authorize_invocation(
        item.deployment_ref,
        user_id="user-1",
        department_id="dept-1",
        tool_name="write",
        arguments={"payload": "x"},
        capabilities={"external_tool.invoke", "external_tool.write"},
    )


def test_gateway_discovery_only_returns_reviewed_tools(monkeypatch, tmp_path):
    item = _gateway(monkeypatch, tmp_path)
    registry = GatewayRegistry(str(tmp_path / "gateway.db"))
    filtered = registry.filter_discovery(
        item.deployment_ref,
        {
            "tools": [
                {"name": "unreviewed", "inputSchema": {}},
                {"name": "read", "description": "safe"},
            ]
        },
    )
    assert [tool["name"] for tool in filtered["tools"]] == ["read"]
    with pytest.raises(GatewayStateError):
        registry.filter_discovery(
            item.deployment_ref,
            {
                "tools": [
                    {
                        "name": "read",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string", "maxLength": 200}},
                            "required": ["query"],
                            "additionalProperties": True,
                        },
                    }
                ]
            },
        )


def test_skill_prompt_blocks_respect_scope(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
    catalog = SkillCatalog(str(tmp_path / "skills.db"))
    skill = catalog.register(
        skill_ref="skill/scoped",
        version="v1",
        content="reviewed reference text",
        owner_id="user-1",
        department_id="dept-1",
    )
    catalog.review(skill.skill_ref)
    catalog.mark_tested(skill.skill_ref)
    catalog.enable(skill.skill_ref)

    with pytest.raises(SkillNotAuthorized):
        catalog.prompt_block(skill.skill_ref, principal_id="user-2", department_id="dept-1")
    assert catalog.prompt_blocks(
        [skill.skill_ref],
        principal_id="user-2",
        department_id="dept-1",
    ) == []
    blocks = catalog.prompt_blocks(
        [skill.skill_ref],
        principal_id="user-1",
        department_id="dept-1",
    )
    assert len(blocks) == 1 and "<untrusted_skill>" in blocks[0]


def test_dispatcher_advances_admitted_task_to_success(tmp_path, monkeypatch):
    store, catalog, broker, bundle = _runtime(tmp_path, monkeypatch)
    runner = _FakeRunner()
    runner.collect_response = _inline_output(b"dispatched output")
    adapter = SandboxExecutionAdapter(store=store, catalog=catalog, broker=broker, client=runner)
    task = adapter.admit(_spec(bundle))

    report = adapter.dispatch_once()
    assert report.submitted == 1
    assert report.collected == 1
    assert store.require(task.task_id).status == "succeeded"


def test_authorization_grants_only_narrow_the_deployment_allowlist(monkeypatch, tmp_path):
    from src.sandbox.authorization import SandboxAuthorizationStore

    monkeypatch.setattr(
        settings,
        "SANDBOX_AUTHORIZED_CAPABILITIES",
        ("sandbox.execute", "external_tool.invoke"),
    )
    store = SandboxAuthorizationStore(str(tmp_path / "auth.db"))
    assert store.effective_capabilities(user_id="user-1", department_id="dept-1") == {
        "sandbox.execute",
        "external_tool.invoke",
    }
    store.grant("user", "user-1", ("sandbox.execute",), actor="admin")
    assert store.effective_capabilities(user_id="user-1", department_id="dept-1") == {
        "sandbox.execute"
    }
    # A grant cannot introduce a capability outside the deployment allowlist.
    store.grant("user", "user-1", ("sandbox.write_artifact",), actor="admin")
    assert "sandbox.write_artifact" not in store.effective_capabilities(
        user_id="user-1", department_id="dept-1"
    )


def test_broker_fails_closed_when_registered_scanner_is_missing(tmp_path, monkeypatch):
    _enable_sandbox(monkeypatch, SANDBOX_ARTIFACT_SCANNER_REF="scanner://deployed")
    broker = ArtifactBroker(str(tmp_path / "artifacts"), str(tmp_path / "scan.db"))
    content = b"unscanned"
    manifest = broker.stage(
        task_id="task-1",
        owner_id="user-1",
        department_id="dept-1",
        filename="result.txt",
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    with pytest.raises(Exception) as exc_info:
        broker.scan(manifest.artifact_id, principal_id="user-1")
    assert getattr(exc_info.value, "code", "") == "scanner_unavailable"


def test_sandbox_health_is_closed_or_degraded_without_real_identity(monkeypatch, tmp_path):
    from src.observability.health import _check_sandbox

    close_service_client()
    configure_service_client_factory(None)
    try:
        monkeypatch.setattr(settings, "SANDBOX_ENABLED", False)
        monkeypatch.setattr(settings, "SANDBOX_GATEWAY_ENABLED", False)
        assert _check_sandbox()["status"] == "not_configured"

        monkeypatch.setattr(settings, "SANDBOX_ENABLED", True)
        monkeypatch.setattr(settings, "SANDBOX_BACKEND", "runner_api")
        monkeypatch.setattr(settings, "SANDBOX_RUNNER_URL", "https://runner.example")
        monkeypatch.setattr(settings, "SANDBOX_SERVICE_AUTH_REF", "workload://hdb")
        monkeypatch.setattr(settings, "SANDBOX_REQUIRE_RUNTIME_ATTESTATION", False)
        monkeypatch.setattr(settings, "SANDBOX_SERVICE_TOKEN_FILE", "")
        assert _check_sandbox()["status"] == "degraded"

        token_path = tmp_path / "service-token"
        token_path.write_text("short-lived-token", encoding="utf-8")
        os.chmod(token_path, 0o600)
        monkeypatch.setattr(settings, "SANDBOX_SERVICE_TOKEN_FILE", str(token_path))
        assert _check_sandbox()["status"] == "up"
    finally:
        close_service_client()
        configure_service_client_factory(None)
