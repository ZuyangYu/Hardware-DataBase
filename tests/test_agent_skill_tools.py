"""Conversation exposes only opted-in, reviewed Skill tools to the model."""

from unittest.mock import Mock

from langchain_core.messages import AIMessage

import src.settings as settings
from src.agents import runner as runner_mod
from src.pipelines.document_rag.schemas import RequestContext
from src.sandbox.agent_tools import _emit_task_stage
from src.sandbox.catalog import SandboxCatalog
from src.sandbox.skills import SkillCatalog


class _Backend:
    name = "fake"

    def retrieve(self, *args, **kwargs):
        return []


def test_needs_user_input_stage_emits_a_bounded_input_request():
    events = []
    _emit_task_stage(
        type("RT", (), {"emit": lambda self, kind, payload: events.append((kind, payload))})(),
        {
            "task_id": "task-1", "skill_ref": "skill/report", "status": "succeeded",
            "result_status": "needs_user_input",
            "latest_checkpoint_artifact_id": "art-checkpoint",
            "skill_result": {"required_inputs": ["电源电压", "目标器件"]},
        },
    )
    assert events[0][0] == "stage"
    assert events[0][1]["skill_request"] == {
        "task_id": "task-1", "skill_ref": "skill/report",
        "required_inputs": ["电源电压", "目标器件"],
        "checkpoint_artifact_id": "art-checkpoint",
    }


def test_subscribed_skill_is_model_tool_but_disabled_mcp_is_not(tmp_path, monkeypatch):
    for key, value in {
        "SANDBOX_TASK_DB_PATH": str(tmp_path / "task.db"),
        "SANDBOX_ARTIFACT_STORAGE_DIR": str(tmp_path / "artifacts"),
        "SANDBOX_ENABLED": True, "SANDBOX_BACKEND": "runner_api",
        "SANDBOX_RUNNER_URL": "https://runner.example",
        "SANDBOX_SERVICE_AUTH_REF": "workload://test",
        "SANDBOX_REQUIRE_RUNTIME_ATTESTATION": False,
        "SANDBOX_ALLOW_DYNAMIC_INSTALL": False,
        "SANDBOX_AUTHORIZED_CAPABILITIES": ("sandbox.execute", "sandbox.write_artifact"),
        "SANDBOX_ARTIFACT_SCANNER_REF": "scanner://builtin/basic-v1",
        "SKILLS_ENABLED": True, "MCP_ENABLED": False,
        "CONVERSATION_UNIFIED_TOOLS_ENABLED": True,
    }.items():
        monkeypatch.setattr(settings, key, value)
    bundles = SandboxCatalog(settings.SANDBOX_TASK_DB_PATH)
    bundle = bundles.register(bundle_ref="bundle/report", bundle_digest="a" * 64,
                              profile_ref="profile/dev-l1",
                              required_capabilities=("sandbox.execute", "sandbox.write_artifact"),
                              input_schema={"type": "object", "properties": {"title": {"type": "string"}},
                                            "required": ["title"], "additionalProperties": False})
    bundles.review(bundle.bundle_ref)
    bundles.mark_tested(bundle.bundle_ref)
    bundles.enable(bundle.bundle_ref)
    skills = SkillCatalog(settings.SANDBOX_TASK_DB_PATH)
    skills.register(skill_ref="skill/agent-report", version="v1", content="Report generator",
                    department_id="7")
    skills.bind_execution("skill/agent-report", bundle_ref=bundle.bundle_ref,
                          bundle_digest=bundle.bundle_digest, profile_ref=bundle.profile_ref)
    skills.review("skill/agent-report")
    skills.mark_tested("skill/agent-report")
    skills.enable("skill/agent-report")
    captured = {}
    fake_agent = type("FakeAgent", (), {})()
    fake_agent.stream = lambda *a, **kw: iter([(AIMessage(content="done"), {"langgraph_node": "model"})])
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", lambda **kw: captured.update(kw) or fake_agent)
    monkeypatch.setattr(runner_mod, "record_agent", Mock())
    runner = runner_mod.MultiSourceAgentRunner(rag_backend=_Backend(), circuit_service=None)

    def turn():
        list(runner.stream(query="generate report", kb_name="", history=[],
                           ctx=RequestContext(user_id="alice", roles=["employee"],
                                              metadata={"department_id": 7}),
                           thread_id="t1", attachment_user_id=42))
        return {str(getattr(tool, "name", "") or getattr(tool, "__name__", "")): tool
                for tool in captured["tools"]}

    assert "skill_invoke" not in turn()
    skills.subscribe("skill/agent-report", principal_id=42, department_id=7)
    skills.register(
        skill_ref="skill/agent-report-two",
        version="v1",
        content="Second report generator",
        department_id="7",
    )
    skills.bind_execution(
        "skill/agent-report-two",
        bundle_ref=bundle.bundle_ref,
        bundle_digest=bundle.bundle_digest,
        profile_ref=bundle.profile_ref,
    )
    skills.review("skill/agent-report-two")
    skills.mark_tested("skill/agent-report-two")
    skills.enable("skill/agent-report-two")
    skills.subscribe("skill/agent-report-two", principal_id=42, department_id=7)
    tools = turn()
    assert "skill_task_status" in tools
    assert "skill_invoke" in tools
    assert not any(name.startswith("skill__") for name in tools)
    assert "skill/agent-report-two" in tools["skill_invoke"].description
    assert not any(name.startswith("mcp__") for name in tools)
    # Executable Skill tools are available even when the unified/MCP surface
    # is disabled; they have their own authorization path.
    monkeypatch.setattr(settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", False)
    assert "skill_invoke" in turn()
    monkeypatch.setattr(settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", True)
    invoke = tools["skill_invoke"]
    assert "skill_request_rejected" in invoke.invoke({"skill_ref": "skill/agent-report", "parameters": {}})
    skills.unsubscribe("skill/agent-report", principal_id=42)
    skills.unsubscribe("skill/agent-report-two", principal_id=42)
    assert "skill_invoke" not in turn()
    assert "skill_not_subscribed" in invoke.invoke({"skill_ref": "skill/agent-report", "parameters": {"title": "report"}})
