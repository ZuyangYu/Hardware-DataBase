"""A Skill can invoke only an independently reviewed, fixed bundle."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import src.settings as settings
from src.api.routes import sandbox as routes
from src.api.schemas import SandboxSkillExecutionBindingRequest, SandboxSkillRunRequest
from src.sandbox.artifact_broker import ArtifactBroker
from src.sandbox.authorization import SandboxAuthorizationStore
from src.sandbox.catalog import SandboxCatalog
from src.sandbox.skills import SkillCatalog, SkillStateConflict
from src.sandbox.service import SandboxExecutionAdapter, SandboxExecutionError
from src.sandbox.task_store import SandboxTaskStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
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
        "SANDBOX_AUTHORIZED_CAPABILITIES": ("sandbox.execute", "sandbox.write_artifact"),
    }.items():
        monkeypatch.setattr(settings, key, value)
    db = str(tmp_path / "sandbox.db")
    store = SandboxTaskStore(db)
    bundles = SandboxCatalog(db)
    skills = SkillCatalog(db)
    broker = ArtifactBroker(str(tmp_path / "artifacts"), db)
    bundle = bundles.register(
        bundle_ref="bundle/report", bundle_digest="a" * 64,
        profile_ref="profile/dev-l1",
        required_capabilities=("sandbox.execute", "sandbox.write_artifact"),
        input_schema={
            "type": "object", "properties": {"title": {"type": "string", "maxLength": 80}},
            "required": ["title"], "additionalProperties": False,
        },
    )
    bundles.review(bundle.bundle_ref)
    bundles.mark_tested(bundle.bundle_ref)
    bundles.enable(bundle.bundle_ref)
    return store, bundles, skills, broker, bundle


def _reviewed_skill(skills, bundle):
    skills.register(
        skill_ref="skill/report", version="v1", content="Use the reviewed report bundle; no shell commands.",
        owner_id="user-1", department_id="dept-1",
    )
    skills.bind_execution(
        "skill/report", bundle_ref=bundle.bundle_ref,
        bundle_digest=bundle.bundle_digest, profile_ref=bundle.profile_ref,
    )
    skills.review("skill/report")
    skills.mark_tested("skill/report")
    return skills.enable("skill/report")


def test_skill_binding_requires_reviewed_bundle_and_fresh_skill_review(setup):
    _, bundles, skills, _, bundle = setup
    skills.register(skill_ref="skill/report", version="v1", content="text only", department_id="dept-1")
    with pytest.raises(SkillStateConflict):
        skills.bind_execution("skill/report", bundle_ref=bundle.bundle_ref,
                              bundle_digest="b" * 64, profile_ref=bundle.profile_ref)
    bound = skills.bind_execution("skill/report", bundle_ref=bundle.bundle_ref,
                                  bundle_digest=bundle.bundle_digest, profile_ref=bundle.profile_ref)
    assert bound.approval_state == "draft"
    assert bound.execution_capabilities == ()
    assert bound.to_dict()["execution_allowed"] is False
    skills.review("skill/report")
    with pytest.raises(SkillStateConflict):
        skills.bind_execution("skill/report", bundle_ref=bundle.bundle_ref,
                              bundle_digest=bundle.bundle_digest, profile_ref=bundle.profile_ref)
    with pytest.raises(ValidationError):
        SandboxSkillExecutionBindingRequest(
            bundle_ref=bundle.bundle_ref, bundle_digest=bundle.bundle_digest,
            profile_ref=bundle.profile_ref, script="print('unsafe')",
        )
    changed = skills.register(skill_ref="skill/report", version="v2", content="new text")
    assert changed.approval_state == "draft" and changed.execution_bundle_ref == ""
    bundles.revoke(bundle.bundle_ref)
    with pytest.raises(SkillStateConflict):
        skills.bind_execution("skill/report", bundle_ref=bundle.bundle_ref,
                              bundle_digest=bundle.bundle_digest, profile_ref=bundle.profile_ref)


def test_skill_run_fences_scope_capabilities_revocation_and_idempotency(setup, monkeypatch):
    store, bundles, skills, broker, bundle = setup
    _reviewed_skill(skills, bundle)
    monkeypatch.setattr(routes, "_audit", lambda *args, **kwargs: None)
    authorized = {"sandbox.execute", "sandbox.write_artifact"}
    monkeypatch.setattr(routes, "_context_capabilities", lambda *args: authorized)
    user = SimpleNamespace(id="user-1", department_id="dept-1", username="reviewer", role="employee")

    def run(parameters=None, key="retry-1", actor=user):
        return routes.run_skill(
            "skill/report", SandboxSkillRunRequest(idempotency_key=key, parameters=parameters or {"title": "report"}),
            user=actor, auth=None, skills=skills, store=store, catalog=bundles,
            broker=broker, authorization=None,
        )

    task = run().task
    assert task["bundle_ref"] == "bundle/report" and task["status"] == "admitted"
    assert task["skill_ref"] == "skill/report"
    assert store.require(task["task_id"]).skill_content_digest == skills.require("skill/report").content_digest
    assert run().task["task_id"] == task["task_id"]
    with pytest.raises(HTTPException) as conflict:
        run({"title": "different"})
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException) as forbidden:
        run(actor=SimpleNamespace(id="user-2", department_id="dept-1", username="other", role="employee"))
    assert forbidden.value.status_code == 403
    authorized.remove("sandbox.write_artifact")
    with pytest.raises(HTTPException) as no_capability:
        run(key="retry-2")
    assert no_capability.value.status_code == 403
    authorized.add("sandbox.write_artifact")
    skills.revoke("skill/report")
    with pytest.raises(HTTPException) as revoked:
        run(key="retry-3")
    assert revoked.value.status_code == 403
    assert len(store.list()) == 1
    with pytest.raises(SandboxExecutionError, match="no longer approved"):
        SandboxExecutionAdapter(store=store, catalog=bundles, broker=broker, client=object()).submit(task["task_id"])
    assert store.require(task["task_id"]).status == "admitted"
    report = SandboxExecutionAdapter(store=store, catalog=bundles, broker=broker, client=object()).dispatch_once()
    assert report.failed == 1
    assert store.require(task["task_id"]).status == "rejected"
    assert store.require(task["task_id"]).error_code == "skill_not_authorized"


def test_available_skills_is_narrowed_by_scope_and_capabilities(setup, monkeypatch):
    _, _, skills, _, bundle = setup
    _reviewed_skill(skills, bundle)
    authorized = {"sandbox.execute", "sandbox.write_artifact"}
    monkeypatch.setattr(routes, "_context_capabilities", lambda *args: authorized)

    def available(user_id, department_id="dept-1"):
        user = SimpleNamespace(id=user_id, department_id=department_id, role="employee")
        return routes.available_skills(user=user, auth=None, skills=skills, authorization=None)["items"]

    assert [item["skill_ref"] for item in available("user-1")] == ["skill/report"]
    assert available("user-2") == []
    assert available("user-1", "dept-2") == []
    authorized.remove("sandbox.write_artifact")
    assert available("user-1") == []
    authorized.add("sandbox.write_artifact")
    monkeypatch.setattr(settings, "SKILLS_ENABLED", False)
    assert available("user-1") == []


def test_skill_cannot_be_enabled_after_bound_bundle_is_revoked(setup):
    _, bundles, skills, _, bundle = setup
    skills.register(skill_ref="skill/report", version="v1", content="reviewed reference text",
                    department_id="dept-1")
    skills.bind_execution("skill/report", bundle_ref=bundle.bundle_ref,
                          bundle_digest=bundle.bundle_digest, profile_ref=bundle.profile_ref)
    skills.review("skill/report")
    skills.mark_tested("skill/report")
    bundles.revoke(bundle.bundle_ref)
    with pytest.raises(SkillStateConflict, match="changed or been revoked"):
        skills.enable("skill/report")
    assert skills.require("skill/report").approval_state == "tested"


def test_global_text_skill_cannot_become_a_global_executable_skill(setup):
    _, _, skills, _, bundle = setup
    skills.register(skill_ref="skill/global-text", version="v1", content="reference data")
    with pytest.raises(SkillStateConflict, match="explicit department scope"):
        skills.bind_execution("skill/global-text", bundle_ref=bundle.bundle_ref,
                              bundle_digest=bundle.bundle_digest, profile_ref=bundle.profile_ref)


def test_skill_queued_task_fails_closed_when_user_capability_is_revoked(setup, monkeypatch):
    store, bundles, skills, broker, bundle = setup
    _reviewed_skill(skills, bundle)
    monkeypatch.setattr(routes, "_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "_context_capabilities", lambda *args: {"sandbox.execute", "sandbox.write_artifact"})
    user = SimpleNamespace(id="user-1", department_id="dept-1", username="reviewer", role="employee")
    grants = SandboxAuthorizationStore(store.db_path)
    grants.grant("user", user.id, ["sandbox.execute", "sandbox.write_artifact"])
    task = routes.run_skill(
        "skill/report", SandboxSkillRunRequest(idempotency_key="capability-revoked", parameters={"title": "report"}),
        user=user, auth=None, skills=skills, store=store, catalog=bundles,
        broker=broker, authorization=grants,
    ).task
    grants.revoke("user", user.id, ["sandbox.execute"])
    report = SandboxExecutionAdapter(store=store, catalog=bundles, broker=broker, client=object()).dispatch_once()
    assert report.failed == 1
    assert store.require(task["task_id"]).status == "rejected"
