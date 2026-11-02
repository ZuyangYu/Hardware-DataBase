"""A Skill can invoke only an independently reviewed, fixed bundle."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ValidationError
from deepagents import create_deep_agent

import src.settings as settings
from src.api.routes import sandbox as routes
from src.api.schemas import SandboxSkillExecutionBindingRequest, SandboxSkillRunRequest
from src.sandbox.artifact_broker import ArtifactBroker
from src.sandbox.agent_tools import make_skill_tools
from src.sandbox import agent_tools
from src.sandbox.authorization import SandboxAuthorizationStore
from src.sandbox.catalog import SandboxCatalog
from src.sandbox.skills import SkillCatalog, SkillNotAuthorized, SkillNotFound, SkillStateConflict
from src.sandbox.service import SandboxExecutionAdapter, SandboxExecutionError
from src.sandbox.task_store import SandboxTaskStore


class _SkillRuntime:
    """Minimal ToolRuntime surface used by the Skill tool factory."""

    def __init__(self, events: list[tuple[str, dict]]):
        self.events = events
        self._seq = 0

    def check_cancel(self) -> None:
        return None

    def next_invocation_id(self) -> str:
        self._seq += 1
        return f"inv-{self._seq}"

    def tool_started(self, **payload) -> None:
        self.events.append(("tool_started", payload))

    def tool_result(self, **payload) -> None:
        self.events.append(("tool_result", payload))


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
    skills.subscribe("skill/report", principal_id=user.id, department_id=user.department_id)

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
    bundles.revoke(bundle.bundle_ref)
    with pytest.raises(SkillStateConflict, match="unfinished"):
        skills.delete_revoked("skill/report")
    with pytest.raises(HTTPException) as revoked:
        run(key="retry-3")
    assert revoked.value.status_code == 403
    assert len(store.list()) == 1
    with pytest.raises(SandboxExecutionError, match="subscription was removed"):
        SandboxExecutionAdapter(store=store, catalog=bundles, broker=broker, client=object()).submit(task["task_id"])
    assert store.require(task["task_id"]).status == "admitted"
    report = SandboxExecutionAdapter(store=store, catalog=bundles, broker=broker, client=object()).dispatch_once()
    assert report.failed == 1
    assert store.require(task["task_id"]).status == "rejected"
    assert store.require(task["task_id"]).error_code == "skill_not_authorized"
    skills.delete_revoked("skill/report")
    assert store.require(task["task_id"]).skill_ref == "skill/report"


def test_available_skills_is_narrowed_by_scope_and_capabilities(setup, monkeypatch):
    _, _, skills, _, bundle = setup
    _reviewed_skill(skills, bundle)
    authorized = {"sandbox.execute", "sandbox.write_artifact"}
    monkeypatch.setattr(routes, "_context_capabilities", lambda *args: authorized)

    def available(user_id, department_id="dept-1"):
        user = SimpleNamespace(id=user_id, department_id=department_id, role="employee")
        return routes.available_skills(user=user, auth=None, skills=skills, authorization=None)["items"]

    assert [item["skill_ref"] for item in available("user-1")] == ["skill/report"]
    assert "reviewed report" in available("user-1")[0]["description"]
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


def test_skill_subscription_requires_new_consent_after_version_change(setup):
    _, _, skills, _, bundle = setup
    _reviewed_skill(skills, bundle)
    skills.subscribe("skill/report", principal_id="user-1", department_id="dept-1")
    assert skills.is_subscribed("skill/report", "user-1")
    skills.revoke("skill/report")
    skills.register(skill_ref="skill/report", version="v2", content="Updated reviewed report",
                    owner_id="user-1", department_id="dept-1")
    skills.bind_execution("skill/report", bundle_ref=bundle.bundle_ref,
                          bundle_digest=bundle.bundle_digest, profile_ref=bundle.profile_ref)
    skills.review("skill/report")
    skills.mark_tested("skill/report")
    skills.enable("skill/report")
    assert not skills.is_subscribed("skill/report", "user-1")


def test_text_skill_requires_department_consent_and_is_removed_on_delete(setup):
    _, _, skills, _, _ = setup
    skills.register(skill_ref="skill/checklist", version="v1", content="Check missing ratings",
                    department_id="dept-1")
    skills.review("skill/checklist")
    skills.mark_tested("skill/checklist")
    skills.enable("skill/checklist")
    listed = routes.available_skills(
        user=SimpleNamespace(id="user-1", department_id="dept-1"),
        auth=None, skills=skills, authorization=None,
    )["items"]
    assert any(item["skill_ref"] == "skill/checklist" and item["kind"] == "text" for item in listed)
    assert routes.available_skills(
        user=SimpleNamespace(id="user-2", department_id="dept-2"),
        auth=None, skills=skills, authorization=None,
    )["items"] == []
    with pytest.raises(SkillNotAuthorized):
        skills.subscribe("skill/checklist", principal_id="user-2", department_id="dept-2")
    skills.subscribe("skill/checklist", principal_id="user-1", department_id="dept-1")
    assert [item.skill_ref for item in skills.subscribed_text(principal_id="user-1", department_id="dept-1")] == ["skill/checklist"]
    assert skills.subscribed_text(principal_id="user-1", department_id="dept-2") == []
    with pytest.raises(SkillStateConflict, match="revoked"):
        skills.delete_revoked("skill/checklist")
    skills.revoke("skill/checklist")
    assert not skills.is_subscribed("skill/checklist", "user-1")
    assert skills.subscribed_text(principal_id="user-1", department_id="dept-1") == []
    deleted = skills.delete_revoked("skill/checklist")
    assert deleted.skill_ref == "skill/checklist" and not skills.is_subscribed("skill/checklist", "user-1")
    with pytest.raises(SkillNotFound):
        skills.delete_revoked("skill/checklist")
    skills.register(skill_ref="skill/checklist", version="v1", content="Check missing ratings", department_id="dept-1")
    skills.review("skill/checklist")
    skills.mark_tested("skill/checklist")
    skills.enable("skill/checklist")
    assert not skills.is_subscribed("skill/checklist", "user-1")


def test_admin_published_all_department_skills_require_employee_department_and_subscription(setup, monkeypatch):
    _, _, skills, _, bundle = setup
    for ref in ("skill/global-text", "skill/global-run"):
        skills.register(skill_ref=ref, version="v1", content="Demo for all departments", department_id="*")
    skills.bind_execution("skill/global-run", bundle_ref=bundle.bundle_ref,
                          bundle_digest=bundle.bundle_digest, profile_ref=bundle.profile_ref)
    for ref in ("skill/global-text", "skill/global-run"):
        skills.review(ref)
        skills.mark_tested(ref)
        skills.enable(ref)

    monkeypatch.setattr(routes, "_context_capabilities", lambda *args: {"sandbox.execute", "sandbox.write_artifact"})
    for department in ("47", "79136"):
        user = SimpleNamespace(id=f"employee-{department}", department_id=department, role="employee")
        available = routes.available_skills(user=user, auth=None, skills=skills, authorization=None)["items"]
        assert {item["skill_ref"] for item in available} == {"skill/global-text", "skill/global-run"}
        for ref in ("skill/global-text", "skill/global-run"):
            skills.subscribe(ref, principal_id=user.id, department_id=department)
            assert skills.is_subscribed(ref, user.id)
        assert [item.skill_ref for item in skills.subscribed_text(
            principal_id=user.id, department_id=department,
        )] == ["skill/global-text"]
        assert "Demo for all" in skills.prompt_block(
            "skill/global-text", principal_id=user.id, department_id=department,
        )
    blank_user = SimpleNamespace(id="employee-none", department_id=None, role="employee")
    assert routes.available_skills(user=blank_user, auth=None, skills=skills, authorization=None)["items"] == []
    with pytest.raises(SkillNotAuthorized):
        skills.subscribe("skill/global-text", principal_id="employee-none", department_id="")
    with pytest.raises(SkillNotAuthorized):
        skills.subscribe("skill/global-run", principal_id="employee-none", department_id="")


def test_subscribed_text_is_mounted_in_employee_conversation_only(setup, monkeypatch):
    from src.agents.runner import MultiSourceAgentRunner
    import src.core.auth as auth_module

    _, _, skills, _, _ = setup
    monkeypatch.setattr(settings, "SANDBOX_TASK_DB_PATH", skills.db_path)
    monkeypatch.setattr(auth_module, "AuthService", lambda: SimpleNamespace(
        get_user_by_username=lambda username: SimpleNamespace(
            id=42, role="employee", is_active=True, department_id="dept-1",
        ) if username == "tester" else None,
    ))
    skills.register(skill_ref="skill/checklist", version="v1", content="Verify component voltage",
                    department_id="dept-1")
    skills.review("skill/checklist")
    skills.mark_tested("skill/checklist")
    skills.enable("skill/checklist")
    context = SimpleNamespace(user_id="tester", metadata={"actor_department_id": "dept-1"},
                              is_system_admin=lambda: False)
    load = MultiSourceAgentRunner._load_skill_prompt_context
    assert load(context) == ""
    skills.subscribe("skill/checklist", principal_id=42, department_id="dept-1")
    assert "Verify component voltage" in load(context)
    assert load(SimpleNamespace(user_id="tester", metadata={"actor_department_id": "dept-2"},
                                is_system_admin=lambda: False)) == ""
    skills.unsubscribe("skill/checklist", principal_id=42)
    assert load(context) == ""


def test_skill_prompt_context_failure_reports_bounded_error(setup, monkeypatch):
    from src.agents.runner import MultiSourceAgentRunner
    from src.sandbox import skills as skills_module
    import src.core.auth as auth_module

    _, _, skills, _, _ = setup
    monkeypatch.setattr(settings, "SANDBOX_TASK_DB_PATH", skills.db_path)
    monkeypatch.setattr(auth_module, "AuthService", lambda: SimpleNamespace(
        get_user_by_username=lambda username: SimpleNamespace(
            id=42, role="employee", is_active=True, department_id="dept-1",
        ) if username == "tester" else None,
    ))

    def explode(*args, **kwargs):
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(skills_module.SkillCatalog, "prompt_blocks", explode)
    context = SimpleNamespace(user_id="tester", metadata={"actor_department_id": "dept-1"},
                              is_system_admin=lambda: False)
    seen: list[str] = []
    text = MultiSourceAgentRunner._load_skill_prompt_context(
        context, on_error=lambda exc: seen.append(type(exc).__name__),
    )
    assert text == ""
    assert seen == ["RuntimeError"]


def test_revoked_executable_skill_delete_requires_revoked_bundle_and_finished_task(setup):
    _, bundles, skills, _, bundle = setup
    _reviewed_skill(skills, bundle)
    skills.subscribe("skill/report", principal_id="user-1", department_id="dept-1")
    skills.revoke("skill/report")
    with pytest.raises(SkillStateConflict, match="bundle"):
        skills.delete_revoked("skill/report")
    bundles.revoke(bundle.bundle_ref)
    skills.delete_revoked("skill/report")
    assert not skills.is_subscribed("skill/report", "user-1")
    assert bundles.require(bundle.bundle_ref).approval_state == "revoked"


def test_agent_sees_only_subscribed_reviewed_skills_and_rechecks_at_call(setup, monkeypatch):
    store, _, skills, _, bundle = setup
    monkeypatch.setattr(settings, "SANDBOX_TASK_DB_PATH", store.db_path)
    skills.register(skill_ref="skill/agent-report", version="v1", content="Report task for sandbox",
                    department_id="dept-1")
    skills.bind_execution("skill/agent-report", bundle_ref=bundle.bundle_ref,
                          bundle_digest=bundle.bundle_digest, profile_ref=bundle.profile_ref)
    skills.review("skill/agent-report")
    skills.mark_tested("skill/agent-report")
    skills.enable("skill/agent-report")
    events = []
    rt = _SkillRuntime(events)
    assert make_skill_tools(rt, user_id=42, department_id="dept-1") == []
    skills.subscribe("skill/agent-report", principal_id=42, department_id="dept-1")
    tools = make_skill_tools(rt, user_id=42, department_id="dept-1")
    invocation = next(tool for tool in tools if tool.name.startswith("skill__"))
    assert "skill/agent-report" in invocation.description
    assert '"title"' in invocation.description
    assert "skill_request_rejected" in invocation.invoke({"parameters_json": "not JSON"})
    skills.unsubscribe("skill/agent-report", principal_id=42)
    assert "skill_not_subscribed" in invocation.invoke({"parameters_json": '{"title":"report"}'})
    assert make_skill_tools(rt, user_id=42, department_id="dept-1") == []
    assert events == []


def test_agent_skill_tool_returns_broker_verified_preview_for_followup(setup, monkeypatch):
    store, _, skills, _, bundle = setup
    monkeypatch.setattr(settings, "SANDBOX_TASK_DB_PATH", store.db_path)
    skills.register(skill_ref="skill/agent-report", version="v1", content="Report task",
                    department_id="dept-1")
    skills.bind_execution("skill/agent-report", bundle_ref=bundle.bundle_ref,
                          bundle_digest=bundle.bundle_digest, profile_ref=bundle.profile_ref)
    skills.review("skill/agent-report")
    skills.mark_tested("skill/agent-report")
    skills.enable("skill/agent-report")
    skills.subscribe("skill/agent-report", principal_id=42, department_id="dept-1")

    class FinishedStore:
        db_path = store.db_path

        def create(self, spec, *, actor_id=""):
            return store.create(spec, actor_id=actor_id)

        def require(self, task_id):
            original = store.require(task_id)
            return SimpleNamespace(task_id=task_id, status="succeeded", owner_id=original.owner_id,
                                   department_id=original.department_id, skill_ref=original.skill_ref)

    class VerifiedBroker:
        def list_for_task(self, task_id, *, states=None):
            return [SimpleNamespace(artifact_id="art-" + "0" * 32, filename="input.txt",
                                    sha256="c" * 64, size=6, role="input"),
                    SimpleNamespace(artifact_id="art-" + "a" * 32, filename="report.txt",
                                    sha256="b" * 64, size=5, role="output")]

        def read(self, artifact_id, **kwargs):
            assert kwargs["principal_id"] == "42"
            return b"hello"

    monkeypatch.setattr(agent_tools, "SandboxTaskStore", lambda: FinishedStore())
    monkeypatch.setattr(agent_tools, "ArtifactBroker", lambda **kwargs: VerifiedBroker())
    events = []
    rt = _SkillRuntime(events)
    tool = next(tool for tool in make_skill_tools(rt, user_id=42, department_id="dept-1")
                if tool.name.startswith("skill__"))
    response = tool.invoke({"parameters_json": '{"title":"report"}'})
    assert '"status": "succeeded"' in response and '"untrusted_preview": "hello"' in response
    assert "input.txt" not in response
    assert [kind for kind, _ in events] == ["tool_started", "tool_result"]

    class ToolCallingModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "skill-tool-test"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = next((message for message in messages if isinstance(message, ToolMessage)), None)
            if result is not None:
                output = AIMessage(content="Report result: " + str(result.content))
            else:
                output = AIMessage(content="", tool_calls=[{
                    "name": tool.name, "args": {"parameters_json": '{"title":"report"}'},
                    "id": "call-skill", "type": "tool_call",
                }])
            return ChatResult(generations=[ChatGeneration(message=output)])

    # Exercise Deep Agents' real tool loop: its next model turn receives the
    # broker-filtered tool result and can use it in the final answer.
    agent = create_deep_agent(model=ToolCallingModel(), tools=[tool], system_prompt="Use the subscribed Skill")
    final = agent.invoke({"messages": [{"role": "user", "content": "Run the report Skill"}]})
    assert '"untrusted_preview": "hello"' in str(final["messages"][-1].content)


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
    skills.subscribe("skill/report", principal_id=user.id, department_id=user.department_id)
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


def test_queued_skill_task_is_rejected_after_unsubscribe(setup, monkeypatch):
    store, bundles, skills, broker, bundle = setup
    _reviewed_skill(skills, bundle)
    monkeypatch.setattr(routes, "_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "_context_capabilities", lambda *args: {"sandbox.execute", "sandbox.write_artifact"})
    user = SimpleNamespace(id="user-1", department_id="dept-1", username="reviewer", role="employee")
    skills.subscribe("skill/report", principal_id=user.id, department_id=user.department_id)
    task = routes.run_skill(
        "skill/report", SandboxSkillRunRequest(idempotency_key="unsubscribe-before-dispatch",
                                               parameters={"title": "report"}),
        user=user, auth=None, skills=skills, store=store, catalog=bundles,
        broker=broker, authorization=None,
    ).task
    skills.unsubscribe("skill/report", principal_id=user.id)
    report = SandboxExecutionAdapter(store=store, catalog=bundles, broker=broker, client=object()).dispatch_once()
    assert report.failed == 1
    assert store.require(task["task_id"]).status == "rejected"
    assert store.require(task["task_id"]).error_code == "skill_not_authorized"
