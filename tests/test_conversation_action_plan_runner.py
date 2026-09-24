"""Runner integration tests for the governed conversation action-plan path."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock
import json

from src.agents.orchestration.contracts import (
    ActionSpec,
    ConversationActionPlan,
)
from src.agents.runner import MultiSourceAgentRunner


class _FakeRAGBackend:
    name = "fake"

    def retrieve(self, *args, **kwargs):
        return []


class _Context:
    user_id = "alice"
    tenant_id = "default"
    metadata: dict[str, str] = {}

    def has_kb_permission(self, _kb_name: str, _permission: str) -> bool:
        return True


class _FixedPlanParser:
    def parse(self, _query, *, context, available_capabilities, fallback):
        assert context["has_kb"] is True
        assert "document.authoring" in available_capabilities
        return ConversationActionPlan(
            plan_id="turn-plan-1",
            goal="先检索再生成候选",
            actions=[
                ActionSpec(
                    action_id="search",
                    kind="search",
                    required_capabilities=["evidence.search"],
                ),
                ActionSpec(
                    action_id="create",
                    kind="create_document",
                    depends_on=["search"],
                    required_capabilities=["document.authoring"],
                ),
            ],
        )


class _BulkDecisionPlanParser:
    def parse(self, _query, *, context, available_capabilities, fallback):
        assert context["has_kb"] is True
        assert {"document.scope.resolve", "document.task.resume"}.issubset(available_capabilities)
        return ConversationActionPlan(
            plan_id="bulk-turn-plan",
            goal="X1900 全部包含，继续生成",
            actions=[
                ActionSpec(
                    action_id="include",
                    kind="resolve_decision",
                    parameters={
                        "action": "include",
                        "refdes": ["X1900"],
                        "scope": "all_pending_for_refdes",
                    },
                    required_capabilities=["document.scope.resolve"],
                ),
                ActionSpec(
                    action_id="resume",
                    kind="resume",
                    depends_on=["include"],
                    required_capabilities=["document.task.resume"],
                ),
            ],
        )


class _ScopeTool:
    name = "resolve_icd_scope_exception"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def invoke(self, arguments: dict):
        self.calls.append(arguments)
        return {
            "status": "succeeded",
            "message": "ICD 范围异常已处理，生成已继续。",
            "work_order_id": "wo-1",
            "run_id": "run-1",
            "data": {"status": "resumed", "resolved_count": 15},
        }


class _GenerationTool:
    """A real action-plan writer whose call must not leak to the model toolset."""

    name = "start_document_generation_session"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def invoke(self, arguments: dict):
        self.calls.append(arguments)
        return {
            "status": "accepted",
            "task_id": "task-1",
            "message": "candidate generation accepted",
        }


class _PlannerModel:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict], dict]] = []

    def invoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return SimpleNamespace(content=json.dumps({
            "plan_id": "model-proposed-plan",
            "goal": "inspect the voltage evidence",
            "context_revision": "revision-9",
            "actions": [{
                "action_id": "inspect",
                "kind": "inspect",
                "required_capabilities": ["evidence.read"],
            }],
            "constraints": [],
            "unresolved_refs": [],
        }))


def test_default_action_plan_parser_uses_the_shared_model_as_a_strict_plan_proposer(monkeypatch):
    """Replacing the model adapter must change the candidate plan, not execution authority."""
    from src.agents import runner as runner_mod

    model = _PlannerModel()
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: model)
    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)

    plan = runner._build_action_plan(
        query="请理解附件中的电压描述",
        thread_id="conversation-9",
        has_attachments=True,
        has_kb=True,
        document_context=None,
        available_capabilities={"evidence.read"},
        context_revision="revision-9",
    )

    assert plan is not None
    assert plan.plan_id == "model-proposed-plan"
    assert [action.kind for action in plan.actions] == ["inspect"]
    assert len(model.calls) == 1
    messages, kwargs = model.calls[0]
    assert messages[0]["role"] == "system"
    assert "可信服务端上下文" in messages[1]["content"]
    assert kwargs["temperature"] == 0


def test_action_plan_parser_receives_bounded_history_and_server_task_projection(monkeypatch):
    """Pronouns can be proposed from context, while object identity stays server-owned."""
    from src.agents import runner as runner_mod

    model = _PlannerModel()
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: model)
    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)

    plan = runner._build_action_plan(
        query="把刚才那个第三章改掉，其他内容保留",
        thread_id="conversation-context",
        has_attachments=False,
        has_kb=True,
        document_context=SimpleNamespace(template_version_id=None, expired=False),
        document_projection={
            "task_id": "task-current",
            "current_artifact_id": "candidate-current",
            "status": "needs_review",
            "candidate_state": "available",
            "release_state": "not_requested",
            "pending_decisions": [{
                "group_key": "X1900",
                "count": 15,
                "default_option": "include",
            }],
        },
        history=[("先生成报告", "候选已生成，请确认第三章。")],
        available_capabilities={"evidence.read"},
        context_revision="revision-context",
    )

    assert plan is not None
    assert len(model.calls) == 1
    prompt = model.calls[0][0][1]["content"]
    assert "task-current" in prompt
    assert "candidate-current" in prompt
    assert "先生成报告" in prompt
    assert "候选已生成，请确认第三章。" in prompt


def test_capability_resolution_uses_server_task_projection_for_current_candidate(monkeypatch):
    from src.agents import runner as runner_mod

    class _Pipeline:
        def get_document_task_projection(self, ctx, task_id):
            assert ctx.user_id == "alice"
            assert task_id == "task-projected"
            return {
                "task_id": task_id,
                "current_artifact_id": "artifact-current",
            }

    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(runner_mod.settings, "DOCUMENT_PLANNING_V2_ENABLED", True)
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=_Pipeline(),
    )

    _resolver, _authorized, state = runner._conversation_capabilities(
        ctx=_Context(),
        kb_name="hardware",
        has_attachments=False,
        document_context=SimpleNamespace(task_id="task-projected"),
        allow_document_write=True,
    )

    assert state["task_ref"] == "task-projected"
    assert state["artifact_ref"] == "artifact-current"


def test_revision_follow_up_recovers_current_task_when_client_context_has_no_task_id():

    class _Pipeline:
        def get_current_chat_document_task_projection(
            self,
            ctx,
            *,
            conversation_id,
            knowledge_base_name,
        ):
            assert ctx.user_id == "alice"
            assert conversation_id == "conversation-revision"
            assert knowledge_base_name == "hardware"
            return {
                "task_id": "task-current",
                "generation_session_id": "session-current",
                "knowledge_base_name": "hardware",
                "template_version_id": "template-current",
            }

    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=_Pipeline(),
    )
    client_context = SimpleNamespace(
        version="v2",
        task_id=None,
        generation_session_id=None,
        output_spec_id=None,
        output_spec_version=None,
        template_version_id=None,
        analysis_id=None,
        knowledge_base_name="hardware",
        attachment_ids=[],
        source_scope="knowledge_base_only",
        expired=False,
    )

    recovered = runner._recover_document_context_for_follow_up(
        query="把上一版报告第三章改成表格，其他章节保持不变",
        ctx=_Context(),
        conversation_id="conversation-revision",
        kb_name="hardware",
        document_context=client_context,
    )

    assert recovered is not None
    assert recovered.task_id == "task-current"
    assert recovered.generation_session_id == "session-current"
    assert recovered.template_version_id == "template-current"


def test_enabled_plan_uses_unified_retrieval_and_document_tools(monkeypatch):
    from src.agents import runner as runner_mod

    captured: dict = {}
    events: list[dict] = []
    generation_tool = _GenerationTool()

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)

        class _Agent:
            def stream(self, *args, **kwargs):
                return iter(())

        return _Agent()

    monkeypatch.setattr(runner_mod.settings, "CONVERSATION_ACTION_PLAN_MODE", "enabled", raising=False)
    monkeypatch.setattr(runner_mod.settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", True, raising=False)
    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(runner_mod, "record_agent", Mock())
    monkeypatch.setattr(
        runner_mod,
        "make_document_authoring_tools",
        lambda *_args, **_kwargs: [
            generation_tool,
            SimpleNamespace(name="get_document_task_status"),
        ],
    )
    monkeypatch.setattr(
        MultiSourceAgentRunner,
        "_prefetch_memory_context",
        lambda *_args, **_kwargs: "",
    )

    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=object(),
    )
    runner.action_plan_parser = _FixedPlanParser()
    document_context = SimpleNamespace(
        expired=False,
        template_version_id=None,
        analysis_id=None,
        task_id="task-1",
    )

    list(runner.stream(
        query="先检索，再生成候选",
        kb_name="hardware",
        history=[],
        ctx=_Context(),
        thread_id="conversation-1",
        document_context=document_context,
        document_flow=None,
        event_callback=events.append,
    ))

    names = [str(getattr(tool, "name", "") or getattr(tool, "__name__", "")) for tool in captured["tools"]]
    assert "document_search" in names
    assert "get_document_task_status" in names
    assert "start_document_generation_session" not in names
    assert generation_tool.calls == [{"purpose": "先检索，再生成候选"}]
    plan_events = [event for event in events if event["payload"].get("key") == "conversation_action_plan"]
    assert plan_events == [{
        "type": "stage",
        "payload": {
            "key": "conversation_action_plan",
            "mode": "enabled",
            "plan": {
                "schema_version": "v1",
                "plan_id": "turn-plan-1",
                "goal": "先检索再生成候选",
                "context_revision": "",
                "actions": [
                    {
                        "action_id": "search",
                        "kind": "search",
                        "target_ref": None,
                        "input_refs": [],
                        "depends_on": [],
                        "parameters": {},
                        "required_capabilities": ["evidence.search"],
                        "completion_contract": "",
                    },
                    {
                        "action_id": "create",
                        "kind": "create_document",
                        "target_ref": None,
                        "input_refs": [],
                        "depends_on": ["search"],
                        "parameters": {},
                        "required_capabilities": ["document.authoring"],
                        "completion_contract": "",
                    },
                ],
                "constraints": [],
                "unresolved_refs": [],
            },
            "issues": [],
        },
    }]
    summary = runner.get_last_retrieval_summary()
    assert summary["conversation_action_plan"]["plan_id"] == "turn-plan-1"
    assert summary["conversation_action_execution"]["plan_id"] == "turn-plan-1"
    assert [receipt["action_id"] for receipt in summary["conversation_action_execution"]["receipts"]] == [
        "search",
        "create",
    ]


def test_enabled_plan_executes_one_bulk_decision_through_the_governed_tool(monkeypatch):
    from src.agents import runner as runner_mod

    events: list[dict] = []
    scope_tool = _ScopeTool()

    class _Agent:
        def stream(self, *args, **kwargs):
            return iter(())

    monkeypatch.setattr(runner_mod.settings, "CONVERSATION_ACTION_PLAN_MODE", "enabled", raising=False)
    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", lambda **_kwargs: _Agent())
    monkeypatch.setattr(runner_mod, "record_agent", Mock())
    monkeypatch.setattr(
        runner_mod,
        "make_document_authoring_tools",
        lambda *_args, **_kwargs: [scope_tool],
    )
    monkeypatch.setattr(MultiSourceAgentRunner, "_prefetch_memory_context", lambda *_args, **_kwargs: "")

    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=object(),
    )
    runner.action_plan_parser = _BulkDecisionPlanParser()

    list(runner.stream(
        query="X1900 全部包含，继续生成",
        kb_name="hardware",
        history=[],
        ctx=_Context(),
        thread_id="conversation-1",
        document_context=SimpleNamespace(
            expired=False,
            template_version_id=None,
            analysis_id=None,
            task_id="task-1",
        ),
        event_callback=events.append,
    ))

    assert scope_tool.calls == [{
        "action": "include",
        "comment": "X1900 全部包含，继续生成",
        "refdes": ["X1900"],
    }]
    execution_events = [event for event in events if event["payload"].get("key") == "conversation_action_execution"]
    assert execution_events == [{
        "type": "stage",
        "payload": {
            "key": "conversation_action_execution",
            "status": "done",
            "plan_id": "bulk-turn-plan",
            "receipts": [
                {
                    "action_id": "include",
                    "status": "completed",
                    "result_refs": ["work_order:wo-1", "run:run-1"],
                    "error_code": "",
                    "message": "ICD 范围异常已处理，生成已继续。",
                    "data": {
                        "status": "succeeded",
                        "message": "ICD 范围异常已处理，生成已继续。",
                        "work_order_id": "wo-1",
                        "run_id": "run-1",
                        "data": {"status": "resumed", "resolved_count": 15},
                    },
                },
                {
                    "action_id": "resume",
                    "status": "completed",
                    "result_refs": ["work_order:wo-1", "run:run-1"],
                    "error_code": "",
                    "message": "the preceding governed decision already resumed the task",
                    "data": {
                        "status": "succeeded",
                        "message": "ICD 范围异常已处理，生成已继续。",
                        "work_order_id": "wo-1",
                        "run_id": "run-1",
                        "data": {"status": "resumed", "resolved_count": 15},
                    },
                },
            ],
        },
    }]


def test_enabled_plan_with_unresolved_reference_never_dispatches_a_document_write(monkeypatch):
    """An ambiguous target must stop before a model-proposed writer is called."""
    from src.agents import runner as runner_mod

    generation_tool = _GenerationTool()
    captured: dict[str, object] = {}

    class _AmbiguousPlanParser:
        def parse(self, _query, *, context, available_capabilities, fallback):
            return ConversationActionPlan(
                plan_id="ambiguous-write-plan",
                goal="那个脚不要了",
                unresolved_refs=["ambiguous_pin_reference"],
                actions=[ActionSpec(
                    action_id="write",
                    kind="create_document",
                    required_capabilities=["document.authoring"],
                )],
            )

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)

        class _Agent:
            def stream(self, *args, **kwargs):
                return iter(())

        return _Agent()

    monkeypatch.setattr(runner_mod.settings, "CONVERSATION_ACTION_PLAN_MODE", "enabled", raising=False)
    monkeypatch.setattr(runner_mod.settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", True, raising=False)
    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(runner_mod, "record_agent", Mock())
    monkeypatch.setattr(
        runner_mod,
        "make_document_authoring_tools",
        lambda *_args, **_kwargs: [generation_tool],
    )
    monkeypatch.setattr(MultiSourceAgentRunner, "_prefetch_memory_context", lambda *_args, **_kwargs: "")

    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=object(),
    )
    runner.action_plan_parser = _AmbiguousPlanParser()

    answer = "".join(runner.stream(
        query="那个脚不要了",
        kb_name="hardware",
        history=[],
        ctx=_Context(),
        thread_id="conversation-ambiguous",
        document_context=SimpleNamespace(
            expired=False,
            template_version_id="template-1",
            analysis_id="analysis-1",
            task_id="task-1",
        ),
    ))

    assert generation_tool.calls == []
    assert "无法确定" in answer
    assert "待确认" in answer
    assert "create_document" not in {
        str(getattr(tool, "name", "") or getattr(tool, "__name__", ""))
        for tool in captured.get("tools", [])
    }


def test_enabled_plan_feedback_explains_ambiguous_output_format():
    from src.agents.runner import _direct_action_plan_feedback

    plan = ConversationActionPlan(
        plan_id="format-clarification",
        goal="请生成 PDF 和 Word 报告",
        unresolved_refs=["multiple_output_formats"],
    )

    answer = _direct_action_plan_feedback(plan)

    assert answer is not None
    assert "只能选择一种" in answer
    assert "PDF" in answer and "Word" in answer
    assert "不会自动选择格式" in answer


def test_enabled_pause_and_status_plan_returns_server_feedback_without_a_second_model_turn(monkeypatch):
    """Pause/status must give a usable candidate response even if narration is empty."""
    from src.agents import runner as runner_mod

    calls = {"agent": 0}

    class _PauseTool:
        name = "pause_document_task"

        def invoke(self, arguments):
            return {"status": "succeeded", "task_id": arguments["task_id"]}

    class _StatusTool:
        name = "get_document_task_status"

        def invoke(self, arguments):
            return {
                "status": "succeeded",
                "task_id": arguments["task_id"],
                "work_order_id": "wo-candidate",
                "data": {
                    "status": "paused",
                    "candidate_state": "available",
                    "release_state": "not_requested",
                    "artifacts": [{"artifact_id": "candidate-1", "stage": "review_candidate"}],
                },
            }

    class _PausePlanParser:
        def parse(self, _query, *, context, available_capabilities, fallback):
            return ConversationActionPlan(
                plan_id="pause-status-plan",
                goal="先停一下，把刚才的候选发我看看",
                actions=[
                    ActionSpec(
                        action_id="pause",
                        kind="pause",
                        required_capabilities=["document.task.control"],
                    ),
                    ActionSpec(
                        action_id="status",
                        kind="get_status",
                        depends_on=["pause"],
                        required_capabilities=["document.task.read"],
                    ),
                ],
            )

    def fake_create_deep_agent(**kwargs):
        calls["agent"] += 1

        class _Agent:
            def stream(self, *args, **kwargs):
                return iter(())

        return _Agent()

    monkeypatch.setattr(runner_mod.settings, "CONVERSATION_ACTION_PLAN_MODE", "enabled", raising=False)
    monkeypatch.setattr(runner_mod.settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", True, raising=False)
    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(runner_mod, "record_agent", Mock())
    monkeypatch.setattr(
        runner_mod,
        "make_document_authoring_tools",
        lambda *_args, **_kwargs: [_PauseTool(), _StatusTool()],
    )
    monkeypatch.setattr(MultiSourceAgentRunner, "_prefetch_memory_context", lambda *_args, **_kwargs: "")

    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=object(),
    )
    runner.action_plan_parser = _PausePlanParser()

    answer = "".join(runner.stream(
        query="先停一下，把刚才的候选发我看看",
        kb_name="hardware",
        history=[],
        ctx=_Context(),
        thread_id="conversation-pause",
        document_context=SimpleNamespace(
            expired=False,
            template_version_id=None,
            analysis_id=None,
            task_id="task-1",
        ),
    ))

    assert calls["agent"] == 0
    assert "已暂停" in answer
    assert "candidate-1" in answer
    assert "下载" in answer


def test_enabled_deferred_generation_executes_search_but_never_exposes_a_writer(monkeypatch):
    """“先查再决定” must remain read-only for the whole turn."""
    from src.agents import runner as runner_mod

    search_tool = SimpleNamespace(
        name="document_search",
        calls=[],
    )

    def search_invoke(arguments):
        search_tool.calls.append(arguments)
        return {"status": "succeeded", "evidence": ["evidence:voltage"]}

    search_tool.invoke = search_invoke
    generation_tool = _GenerationTool()
    captured: dict[str, object] = {}

    class _ReadOnlyPlanParser:
        def parse(self, _query, *, context, available_capabilities, fallback):
            return ConversationActionPlan(
                plan_id="deferred-search-plan",
                goal="先查一下两份手册的电压范围，再决定要不要生成",
                actions=[ActionSpec(
                    action_id="search",
                    kind="search",
                    parameters={"source_scope": "knowledge_base_only"},
                    required_capabilities=["evidence.search"],
                )],
                constraints=[
                    {
                        "constraint_id": "no-create-yet",
                        "kind": "forbid_action",
                        "value": "create_document",
                    },
                ],
            )

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)

        class _Agent:
            def stream(self, *args, **kwargs):
                return iter(())

        return _Agent()

    monkeypatch.setattr(runner_mod.settings, "CONVERSATION_ACTION_PLAN_MODE", "enabled", raising=False)
    monkeypatch.setattr(runner_mod.settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", True, raising=False)
    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(runner_mod, "record_agent", Mock())
    monkeypatch.setattr(runner_mod, "make_document_search", lambda *_args, **_kwargs: search_tool)
    monkeypatch.setattr(
        runner_mod,
        "make_document_authoring_tools",
        lambda *_args, **_kwargs: [generation_tool],
    )
    monkeypatch.setattr(MultiSourceAgentRunner, "_prefetch_memory_context", lambda *_args, **_kwargs: "")

    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=object(),
    )
    runner.action_plan_parser = _ReadOnlyPlanParser()

    list(runner.stream(
        query="先查一下两份手册的电压范围，再决定要不要生成",
        kb_name="hardware",
        history=[],
        ctx=_Context(),
        thread_id="conversation-deferred",
        document_context=SimpleNamespace(
            expired=False,
            template_version_id="template-1",
            analysis_id="analysis-1",
            task_id="task-1",
        ),
    ))

    assert search_tool.calls == [{"query": "先查一下两份手册的电压范围，再决定要不要生成"}]
    assert generation_tool.calls == []
    assert "start_document_generation_session" not in {
        str(getattr(tool, "name", "") or getattr(tool, "__name__", ""))
        for tool in captured["tools"]
    }


def test_enabled_completeness_request_keeps_document_writes_out_of_the_model_toolset(monkeypatch):
    """A completeness check must not create a session as a side effect."""
    from src.agents import runner as runner_mod

    generation_tool = _GenerationTool()
    captured: dict[str, object] = {}

    class _CompletenessPlanParser:
        def parse(self, _query, *, context, available_capabilities, fallback):
            return ConversationActionPlan(
                plan_id="completeness-plan",
                goal="不要生成文档，只告诉我资料是否齐全",
                constraints=[{
                    "constraint_id": "no-document-generation",
                    "kind": "forbid_action",
                    "value": "create_document",
                }],
            )

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)

        class _Agent:
            def stream(self, *args, **kwargs):
                return iter(())

        return _Agent()

    monkeypatch.setattr(runner_mod.settings, "CONVERSATION_ACTION_PLAN_MODE", "enabled", raising=False)
    monkeypatch.setattr(runner_mod.settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", True, raising=False)
    monkeypatch.setattr(runner_mod.settings, "AGENT_DOCUMENT_TOOLS_ENABLED", True)
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(runner_mod, "record_agent", Mock())
    monkeypatch.setattr(
        runner_mod,
        "make_document_authoring_tools",
        lambda *_args, **_kwargs: [generation_tool],
    )
    monkeypatch.setattr(MultiSourceAgentRunner, "_prefetch_memory_context", lambda *_args, **_kwargs: "")

    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=None,
        document_authoring_pipeline=object(),
    )
    runner.action_plan_parser = _CompletenessPlanParser()

    list(runner.stream(
        query="不要生成文档，只告诉我资料是否齐全",
        kb_name="hardware",
        history=[],
        ctx=_Context(),
        thread_id="conversation-completeness",
        document_context=SimpleNamespace(
            expired=False,
            template_version_id="template-1",
            analysis_id="analysis-1",
            task_id="task-1",
        ),
    ))

    assert generation_tool.calls == []
    assert "start_document_generation_session" not in {
        str(getattr(tool, "name", "") or getattr(tool, "__name__", ""))
        for tool in captured["tools"]
    }
