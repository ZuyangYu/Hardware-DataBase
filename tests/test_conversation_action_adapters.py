"""Domain-adapter checks for governed action-plan execution."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.agents.orchestration.contracts import ActionSpec, ConversationActionPlan
from src.agents.orchestration.executor import ConversationActionExecutor


class _Tool:
    def __init__(self, name: str, result):
        self.name = name
        self.result = result
        self.calls: list[dict] = []

    def invoke(self, arguments: dict):
        self.calls.append(arguments)
        return json.dumps(self.result, ensure_ascii=False)


def test_bulk_scope_decision_is_one_governed_tool_call_then_resumes_from_receipt():
    from src.agents.orchestration.adapters import GovernedToolActionAdapters

    scope_tool = _Tool(
        "resolve_icd_scope_exception",
        {
            "status": "succeeded",
            "message": "ICD 范围异常已处理，生成已继续。",
            "work_order_id": "work-order-7",
            "run_id": "run-7",
            "data": {"status": "resumed", "resolved_count": 15},
        },
    )
    plan = ConversationActionPlan(
        plan_id="bulk-include",
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

    adapters = GovernedToolActionAdapters(
        tools=[scope_tool],
        query="X1900 全部包含，继续生成",
    )
    result = ConversationActionExecutor(
        handlers=adapters.handlers(),
        available_capabilities={"document.scope.resolve", "document.task.resume"},
    ).execute(plan)

    assert scope_tool.calls == [{
        "action": "include",
        "comment": "X1900 全部包含，继续生成",
        "refdes": ["X1900"],
    }]
    assert [(receipt.action_id, receipt.status, receipt.result_refs) for receipt in result.receipts] == [
        ("include", "completed", ["work_order:work-order-7", "run:run-7"]),
        ("resume", "completed", ["work_order:work-order-7", "run:run-7"]),
    ]


def test_bulk_process_resolves_the_server_projected_default_option():
    from src.agents.orchestration.adapters import GovernedToolActionAdapters

    scope_tool = _Tool(
        "resolve_icd_scope_exception",
        {
            "status": "succeeded",
            "message": "范围待办已按默认项处理",
            "work_order_id": "work-order-default",
        },
    )
    plan = ConversationActionPlan(
        plan_id="bulk-default",
        goal="X1900 全部处理",
        actions=[ActionSpec(
            action_id="resolve",
            kind="resolve_decision",
            parameters={
                "action": "default",
                "refdes": ["X1900"],
                "scope": "all_pending_for_refdes",
            },
        )],
    )

    result = ConversationActionExecutor(
        handlers=GovernedToolActionAdapters(
            tools=[scope_tool],
            query=plan.goal,
            document_projection={
                "pending_decisions": [{
                    "group_key": "X1900",
                    "default_option": "exclude",
                }],
            },
        ).handlers(),
        available_capabilities={"document.scope.resolve"},
    ).execute(plan)

    assert scope_tool.calls == [{
        "action": "exclude",
        "comment": "X1900 全部处理",
        "refdes": ["X1900"],
    }]
    assert result.receipts[0].status == "completed"


def test_search_action_uses_only_tools_permitted_by_its_source_scope():
    from src.agents.orchestration.adapters import GovernedToolActionAdapters

    attachment_tool = _Tool("attachment_search", {"status": "ok", "evidence": ["attachment:1"]})
    kb_tool = _Tool("document_search", {"status": "ok", "evidence": ["kb:1"]})
    plan = ConversationActionPlan(
        plan_id="attachment-only-search",
        goal="查附件",
        actions=[ActionSpec(
            action_id="search",
            kind="search",
            parameters={"source_scope": "attachment_only"},
            required_capabilities=["evidence.search"],
        )],
    )

    result = ConversationActionExecutor(
        handlers=GovernedToolActionAdapters(
            tools=[attachment_tool, kb_tool],
            query="查附件中的电压范围",
        ).handlers(),
        available_capabilities={"evidence.search"},
    ).execute(plan)

    assert attachment_tool.calls == [{"query": "查附件中的电压范围"}]
    assert kb_tool.calls == []
    assert result.receipts[0].status == "completed"


def test_pause_then_status_uses_only_the_task_bound_control_and_read_tools():
    """A pause request must not create, release, or address a raw Harness run."""
    from src.agents.orchestration.adapters import GovernedToolActionAdapters

    pause_tool = _Tool(
        "pause_document_task",
        {"status": "succeeded", "task_id": "task-8", "run_id": "run-8"},
    )
    status_tool = _Tool(
        "get_document_task_status",
        {
            "status": "succeeded",
            "task_id": "task-8",
            "work_order_id": "wo-8",
            "artifacts": [{"artifact_id": "candidate-8", "stage": "review_candidate"}],
        },
    )
    plan = ConversationActionPlan(
        plan_id="pause-and-candidate",
        goal="先停一下，把刚才的候选发我看看",
        actions=[
            ActionSpec(action_id="pause", kind="pause"),
            ActionSpec(action_id="status", kind="get_status", depends_on=["pause"]),
        ],
    )

    result = ConversationActionExecutor(
        handlers=GovernedToolActionAdapters(
            tools=[pause_tool, status_tool],
            query=plan.goal,
            document_context=SimpleNamespace(task_id="task-8"),
        ).handlers(),
        available_capabilities={"document.task.control", "document.task.read"},
    ).execute(plan)

    assert pause_tool.calls == [{"task_id": "task-8"}]
    assert status_tool.calls == [{"task_id": "task-8"}]
    assert [(receipt.action_id, receipt.status) for receipt in result.receipts] == [
        ("pause", "completed"),
        ("status", "completed"),
    ]


def test_revision_action_binds_current_task_and_candidate_artifact_before_dispatch():
    from src.agents.orchestration.adapters import GovernedToolActionAdapters

    revision_tool = _Tool(
        "create_document_revision",
        {
            "status": "succeeded",
            "task_id": "task-8",
            "work_order_id": "wo-8-revision",
            "data": {"revision_id": "revision-8"},
        },
    )
    plan = ConversationActionPlan(
        plan_id="section-revision",
        goal="把上一版报告第三章改成表格，其他章节保持不变",
        constraints=[{
            "constraint_id": "preserve-rest",
            "kind": "preserve",
            "value": "all_non_target_units",
        }],
        actions=[ActionSpec(
            action_id="revise",
            kind="revise_document",
            parameters={
                "request_type": "section_update",
                "request": "把上一版报告第三章改成表格，其他章节保持不变",
                "changed_sections": ["第三章"],
                "changed_fields": [],
            },
            required_capabilities=["document.revise"],
        )],
    )

    result = ConversationActionExecutor(
        handlers=GovernedToolActionAdapters(
            tools=[revision_tool],
            query=plan.goal,
            document_context=SimpleNamespace(
                task_id="task-8",
                candidate_artifact_id="artifact-8",
            ),
        ).handlers(),
        available_capabilities={"document.revise"},
    ).execute(plan)

    assert revision_tool.calls == [{
        "task_id": "task-8",
        "parent_artifact_id": "artifact-8",
        "request_type": "section_update",
        "request": "把上一版报告第三章改成表格，其他章节保持不变",
        "changed_fields": [],
        "changed_sections": ["第三章"],
        "client_request_id": "conversation-action:revise",
    }]
    assert result.receipts[0].status == "completed"


def test_revision_action_resolves_current_artifact_from_server_projection():
    from src.agents.orchestration.adapters import GovernedToolActionAdapters

    revision_tool = _Tool(
        "create_document_revision",
        {"status": "accepted", "revision_id": "revision-9"},
    )
    plan = ConversationActionPlan(
        plan_id="projected-revision",
        goal="修改第三章",
        actions=[ActionSpec(
            action_id="revise",
            kind="revise_document",
            parameters={
                "request_type": "section_update",
                "request": "修改第三章",
                "changed_sections": ["第三章"],
            },
        )],
    )

    result = ConversationActionExecutor(
        handlers=GovernedToolActionAdapters(
            tools=[revision_tool],
            query=plan.goal,
            document_context=SimpleNamespace(task_id="task-9"),
            document_projection={
                "task_id": "task-9",
                "current_artifact_id": "artifact-9",
            },
        ).handlers(),
        available_capabilities={"document.revise"},
    ).execute(plan)

    assert revision_tool.calls[0]["task_id"] == "task-9"
    assert revision_tool.calls[0]["parent_artifact_id"] == "artifact-9"
    assert result.receipts[0].status == "accepted"


def test_attachment_revision_action_uses_server_parsed_text_and_returns_candidate_refs():
    from src.agents.orchestration.adapters import GovernedToolActionAdapters

    edit_tool = _Tool(
        "attachment_edit",
        {
            "status": "completed",
            "revision_id": "attachment-revision-1",
            "parent_attachment_id": "att-parent",
            "child_attachment_id": "att-child",
            "stage": "review_candidate",
            "release_state": "not_requested",
        },
    )
    plan = ConversationActionPlan(
        plan_id="attachment-revision",
        goal="把附件中的“旧标题”替换为“新标题”",
        actions=[ActionSpec(
            action_id="edit",
            kind="revise_attachment",
            # Model-provided values must not be trusted over the server parse.
            parameters={"before": "猜测的旧值", "after": "猜测的新值"},
            required_capabilities=["attachment.revise"],
        )],
    )

    result = ConversationActionExecutor(
        handlers=GovernedToolActionAdapters(
            tools=[edit_tool],
            query=plan.goal,
        ).handlers(),
        available_capabilities={"attachment.revise"},
    ).execute(plan)

    assert edit_tool.calls == [{
        "before": "旧标题",
        "after": "新标题",
        "attachment_id": "",
        "client_request_id": "conversation-action:edit",
    }]
    assert result.receipts[0].status == "completed"
    assert result.receipts[0].result_refs == [
        "attachment_parent:att-parent",
        "attachment_child:att-child",
        "revision:attachment-revision-1",
    ]


def test_compound_scope_decisions_do_not_queue_until_the_final_resume():
    """Ignoring the defer flag would resume after only the first group choice."""
    from src.agents.orchestration.adapters import GovernedToolActionAdapters

    scope_tool = _Tool(
        "resolve_icd_scope_exception",
        {"status": "succeeded", "data": {"status": "resolved"}},
    )
    resume_tool = _Tool(
        "resume_document_task",
        {"status": "queued", "task_id": "task-8", "run_id": "run-8"},
    )
    plan = ConversationActionPlan(
        plan_id="compound-scope",
        goal="X1900 包含，X1902 排除，然后给我候选",
        actions=[
            ActionSpec(
                action_id="include",
                kind="resolve_decision",
                parameters={
                    "action": "include",
                    "refdes": ["X1900"],
                    "scope": "all_pending_for_refdes",
                    "continue_generation": False,
                },
                required_capabilities=["document.scope.resolve"],
            ),
            ActionSpec(
                action_id="exclude",
                kind="resolve_decision",
                parameters={
                    "action": "exclude",
                    "refdes": ["X1902"],
                    "scope": "all_pending_for_refdes",
                    "continue_generation": False,
                },
                required_capabilities=["document.scope.resolve"],
            ),
            ActionSpec(
                action_id="resume",
                kind="resume",
                depends_on=["include", "exclude"],
                required_capabilities=["document.task.resume"],
            ),
        ],
    )

    result = ConversationActionExecutor(
        handlers=GovernedToolActionAdapters(
            tools=[scope_tool, resume_tool],
            query=plan.goal,
            document_context=SimpleNamespace(task_id="task-8"),
        ).handlers(),
        available_capabilities={"document.scope.resolve", "document.task.resume"},
    ).execute(plan)

    assert scope_tool.calls == [
        {
            "action": "include",
            "comment": plan.goal,
            "refdes": ["X1900"],
            "continue_generation": False,
        },
        {
            "action": "exclude",
            "comment": plan.goal,
            "refdes": ["X1902"],
            "continue_generation": False,
        },
    ]
    assert resume_tool.calls == [{"task_id": "task-8"}]
    assert [receipt.status for receipt in result.receipts] == ["completed", "completed", "accepted"]


def test_compound_scope_decisions_resume_through_the_real_document_tool_surface(tmp_path, monkeypatch):
    """The final resume must remain available after normal v2 tool assembly."""
    from src.agents.orchestration.adapters import GovernedToolActionAdapters
    from src.agents.tools.document_authoring_tools import DocumentAuthoringToolset
    from src.document_authoring.chat_context import DocumentContextInput, build_document_context
    from src.document_authoring.job_store import DocumentAuthoringJobStore
    from src.pipelines.document_rag.schemas import RequestContext
    import src.settings

    monkeypatch.setattr(src.settings, "DOCUMENT_PLANNING_V2_ENABLED", True)
    ctx = RequestContext(
        user_id="user-a",
        tenant_id="tenant-a",
        kb_permissions={"7:hardware": "write"},
        metadata={"resource_department_id": 7},
    )
    context = build_document_context(
        DocumentContextInput(
            analysis_id="analysis-a",
            template_version_id="template-a",
            knowledge_base_name="hardware",
            client_request_id="client-a",
        ),
        ctx=ctx,
        expected_kb="hardware",
    )
    calls: list[tuple[str, object]] = []
    pipeline = SimpleNamespace(
        document_generation=SimpleNamespace(),
        create_document_plan_proposal=lambda *_args, **_kwargs: {},
        confirm_document_plan=lambda *_args, **_kwargs: {},
        get_document_task_projection=lambda *_args, **_kwargs: {},
        get_icd_scope_review=lambda *_args, **_kwargs: SimpleNamespace(
            status="pending",
            exceptions=[
                SimpleNamespace(exception_id="x1900", refdes="X1900", kind="connector_scope_ambiguous"),
                SimpleNamespace(exception_id="x1902", refdes="X1902", kind="connector_scope_ambiguous"),
            ],
        ),
        submit_icd_scope_resolution=lambda _ctx, _order, *, resolutions, comment: calls.append(
            ("resolve", resolutions)
        ) or SimpleNamespace(status="pending"),
        resume_document_task=lambda _ctx, task_id: calls.append(("resume", task_id)) or {
            "task_id": task_id,
            "work_order_id": "wo-8",
            "run_id": "job-8",
            "status": "queued",
        },
    )
    toolset = DocumentAuthoringToolset(
        pipeline=pipeline,
        ctx=ctx,
        context=context,
        chat_session_id="chat-a",
        job_store=DocumentAuthoringJobStore(str(tmp_path / "jobs.db")),
    )
    plan = ConversationActionPlan(
        plan_id="compound-real-tools",
        goal="X1900 包含，X1902 排除，然后给我候选",
        actions=[
            ActionSpec(
                action_id="include",
                kind="resolve_decision",
                parameters={
                    "action": "include",
                    "refdes": ["X1900"],
                    "scope": "all_pending_for_refdes",
                    "continue_generation": False,
                    "work_order_id": "wo-8",
                },
            ),
            ActionSpec(
                action_id="exclude",
                kind="resolve_decision",
                parameters={
                    "action": "exclude",
                    "refdes": ["X1902"],
                    "scope": "all_pending_for_refdes",
                    "continue_generation": False,
                    "work_order_id": "wo-8",
                },
            ),
            ActionSpec(
                action_id="resume",
                kind="resume",
                depends_on=["include", "exclude"],
            ),
        ],
    )

    result = ConversationActionExecutor(
        handlers=GovernedToolActionAdapters(
            tools=toolset.as_tools(),
            query=plan.goal,
            document_context=SimpleNamespace(task_id="task-8"),
        ).handlers(),
        available_capabilities={"document.scope.resolve", "document.task.resume"},
    ).execute(plan)

    assert calls == [
        ("resolve", [{"exception_id": "x1900", "action": "include"}]),
        ("resolve", [{"exception_id": "x1902", "action": "exclude"}]),
        ("resume", "task-8"),
    ]
    assert [receipt.status for receipt in result.receipts] == ["completed", "completed", "completed"]


def test_release_request_adapter_only_calls_the_governed_review_request_tool():
    """Calling an approval tool here would bypass the explicit release gate."""
    from src.agents.orchestration.adapters import GovernedToolActionAdapters

    request_tool = _Tool(
        "request_document_release",
        {"status": "succeeded", "data": {"release_state": "pending"}},
    )
    plan = ConversationActionPlan(
        plan_id="request-release",
        goal="请申请发布候选",
        actions=[ActionSpec(
            action_id="request",
            kind="request_release",
            target_ref="artifact:candidate-9",
            required_capabilities=["document.release.request"],
        )],
    )

    result = ConversationActionExecutor(
        handlers=GovernedToolActionAdapters(tools=[request_tool], query=plan.goal).handlers(),
        available_capabilities={"document.release.request"},
    ).execute(plan)

    assert request_tool.calls == [{"artifact_id": "candidate-9", "comment": plan.goal}]
    assert [(receipt.status, receipt.error_code) for receipt in result.receipts] == [("completed", "")]


def test_release_request_adapter_resolves_current_candidate_from_server_projection():
    from src.agents.orchestration.adapters import GovernedToolActionAdapters

    request_tool = _Tool(
        "request_document_release",
        {"status": "succeeded", "data": {"release_state": "pending"}},
    )
    plan = ConversationActionPlan(
        plan_id="request-current-release",
        goal="请为当前候选申请正式发布",
        actions=[ActionSpec(
            action_id="request",
            kind="request_release",
            required_capabilities=["document.release.request"],
        )],
    )

    result = ConversationActionExecutor(
        handlers=GovernedToolActionAdapters(
            tools=[request_tool],
            query=plan.goal,
            document_projection={"current_artifact_id": "candidate-current"},
        ).handlers(),
        available_capabilities={"document.release.request"},
    ).execute(plan)

    assert request_tool.calls == [{"artifact_id": "candidate-current", "comment": plan.goal}]
    assert result.receipts[0].status == "completed"
