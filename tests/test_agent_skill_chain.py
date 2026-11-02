"""Agent can promote a Skill output and feed it into the next Skill."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import src.settings as settings
from src.sandbox.agent_tools import make_skill_tools
from src.sandbox.artifact_broker import ArtifactBroker
from src.sandbox.authorization import capability_snapshot_hash
from src.sandbox.catalog import SandboxCatalog
from src.sandbox.models import SandboxTaskSpec
from src.sandbox.skills import SkillCatalog
from src.sandbox.task_store import SandboxTaskStore


def test_skill_output_can_be_promoted_and_passed_to_next_skill(tmp_path, monkeypatch):
    for key, value in {
        "SANDBOX_ENABLED": True,
        "SKILLS_ENABLED": True,
        "SANDBOX_BACKEND": "runner_api",
        "SANDBOX_RUNNER_URL": "https://runner.example",
        "SANDBOX_SERVICE_AUTH_REF": "workload://test",
        "SANDBOX_REQUIRE_RUNTIME_ATTESTATION": False,
        "SANDBOX_ALLOW_DYNAMIC_INSTALL": False,
        "SANDBOX_DEFAULT_NETWORK": "none",
        "SANDBOX_MAX_CONCURRENT": 2,
        "SANDBOX_MAX_CONCURRENT_PER_USER": 2,
        "SANDBOX_MAX_CONCURRENT_PER_DEPARTMENT": 2,
        "SANDBOX_AUTHORIZED_CAPABILITIES": (
            "sandbox.execute",
            "sandbox.internal_data",
            "sandbox.write_artifact",
        ),
        "SANDBOX_ARTIFACT_ALLOWED_EXTENSIONS": "json,txt,csv,svg",
        "SANDBOX_ARTIFACT_SCANNER_REF": "",
    }.items():
        monkeypatch.setattr(settings, key, value)
    db = str(tmp_path / "sandbox.db")
    artifacts = str(tmp_path / "artifacts")
    monkeypatch.setattr(settings, "SANDBOX_TASK_DB_PATH", db)
    monkeypatch.setattr(settings, "SANDBOX_ARTIFACT_STORAGE_DIR", artifacts)
    store = SandboxTaskStore(db)
    catalog = SandboxCatalog(db)
    skills = SkillCatalog(db)
    broker = ArtifactBroker(artifacts, db)
    required = ("sandbox.execute", "sandbox.internal_data", "sandbox.write_artifact")
    bundle = catalog.register(
        bundle_ref="bundle/chain",
        bundle_digest="a" * 64,
        profile_ref="profile/dev-l1",
        required_capabilities=required,
        input_schema={
            "type": "object",
            "properties": {"title": {"type": "string", "maxLength": 80}},
            "required": ["title"],
            "additionalProperties": False,
        },
    )
    catalog.review(bundle.bundle_ref)
    catalog.mark_tested(bundle.bundle_ref)
    bundle = catalog.enable(bundle.bundle_ref)
    skills.register(
        skill_ref="skill/chain",
        version="v1",
        content="Chain report generator",
        department_id="7",
    )
    skills.bind_execution(
        "skill/chain",
        bundle_ref=bundle.bundle_ref,
        bundle_digest=bundle.bundle_digest,
        profile_ref=bundle.profile_ref,
    )
    skills.review("skill/chain")
    skills.mark_tested("skill/chain")
    skills.enable("skill/chain")
    skills.subscribe("skill/chain", principal_id=42, department_id=7)

    first = store.create(
        SandboxTaskSpec(
            idempotency_key="chain-first",
            owner_id="42",
            department_id="7",
            bundle_ref=bundle.bundle_ref,
            bundle_digest=bundle.bundle_digest,
            profile_ref=bundle.profile_ref,
            policy_snapshot_hash=bundle.policy_snapshot_hash,
            capability_snapshot_hash=capability_snapshot_hash(
                user_id="42",
                department_id="7",
                capabilities=required,
                policy_snapshot_hash=bundle.policy_snapshot_hash,
            ),
            required_capabilities=required,
            parameters={"title": "first"},
            skill_ref="skill/chain",
            skill_content_digest=skills.require("skill/chain").content_digest,
            skill_version="v1",
        )
    )
    content = b'{"nodes": []}'
    staged = broker.stage_output(
        task_id=first.task_id,
        owner_id="42",
        department_id="7",
        filename="spec.json",
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    broker.scan(staged.artifact_id, department_id="7", task_id=first.task_id)
    output = broker.commit(staged.artifact_id, department_id="7", task_id=first.task_id)

    events: list[tuple[str, dict]] = []
    rt = SimpleNamespace(
        chat_session_id="chain-session",
        check_cancel=lambda: None,
        emit=lambda kind, payload: events.append((kind, payload)),
    )
    tools = {tool.name: tool for tool in make_skill_tools(rt, user_id=42, department_id=7)}
    page = json.loads(
        tools["skill_artifact_read"].invoke(
            {"artifact_id": output.artifact_id, "offset": 0, "limit": 5}
        )
    )
    assert page["content"] == '{"nod' and page["truncated"] is True
    query = json.loads(
        tools["skill_artifact_query"].invoke(
            {"artifact_id": output.artifact_id, "query": "nodes"}
        )
    )
    assert query["found"] is True
    promoted = json.loads(tools["skill_artifact_promote"].invoke({"artifact_id": output.artifact_id}))
    assert promoted["role"] == "input"
    assert promoted["source_artifact_id"] == output.artifact_id

    result = json.loads(
        tools["skill_invoke"].invoke(
            {
                "skill_ref": "skill/chain",
                "parameters": {"title": "second"},
                "input_artifact_ids": [promoted["artifact_id"]],
                "parent_task_id": first.task_id,
            }
        )
    )
    assert result.get("task_id"), result
    second = store.require(result["task_id"])
    assert second.input_artifact_ids == (promoted["artifact_id"],)
    assert second.parent_task_id == first.task_id
    assert second.parameters == {"title": "second"}

    replay = json.loads(
        tools["skill_invoke"].invoke(
            {
                "skill_ref": "skill/chain",
                "parameters": {"title": "second"},
                "input_artifact_ids": [promoted["artifact_id"]],
                "parent_task_id": first.task_id,
            }
        )
    )
    assert replay["task_id"] == second.task_id

    resumed = json.loads(
        tools["skill_task_resume"].invoke(
            {
                "task_id": first.task_id,
                "parameters": {"title": "second"},
                "input_artifact_ids": [promoted["artifact_id"]],
            }
        )
    )
    assert resumed["task_id"] == second.task_id

    stages = [payload for kind, payload in events if kind == "stage"]
    assert stages and stages[-1]["key"] == f"skill-task:{second.task_id}"
    assert stages[-1]["status"] == "running"
