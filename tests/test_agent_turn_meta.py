"""Budget, plan, partial synthesis and verification projections for one turn."""

from __future__ import annotations

import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from src.agents import runner as runner_mod
from src.agents.runner import (
    MultiSourceAgentRunner,
    _agent_evidence_budget,
    _agent_recursion_limit,
    _export_intent_for_turn,
    _build_turn_verification,
    _normalize_citation_labels,
    _research_subagent_spec,
    _scrub_invalid_citations,
    _synthesize_partial_answer,
)
from src.agents.turn_meta import parse_user_tool_call_cap
from src.agents.turn_meta import _semantic_verification
from src.agents.schemas import Evidence


class _FakeRAGBackend:
    name = "fake"

    def retrieve(self, *args, **kwargs):
        return []


def _evidence(number: int, *, attachment: bool = False, content: str = "evidence") -> Evidence:
    metadata = {"citation_number": number}
    if attachment:
        metadata["attachment_id"] = "att-1"
    return Evidence(
        id=f"e{number}",
        content=content,
        source_name="source.md",
        content_kind="text",
        processor_kind="doc",
        score=0.9,
        metadata=metadata,
    )


def _run_with_chunks(monkeypatch, *, query: str, chunks):
    events: list[dict] = []
    fake_agent = SimpleNamespace(
        stream=lambda *args, **kwargs: iter(
            (chunk, {"langgraph_node": "model"}) for chunk in chunks
        )
    )
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", lambda **kwargs: fake_agent)
    monkeypatch.setattr(runner_mod, "record_agent", lambda **kwargs: None)
    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)
    list(runner.stream(query=query, kb_name="kb_hw", history=[], event_callback=events.append))
    return runner, events


def test_complexity_budget_scores_multi_part_attachment_turn():
    query = "\n".join(
        [
            "请完成一个复用上一代 BOM 的新项目选型建议，并输出五类器件、风险、引用和推荐表。",
            "1. 对比上一代 BOM 与新项目需求。",
            "2. 按电源、CAN FD、以太网、MCU、连接器五类给出建议。",
            "3. 至少列出 3 个风险或待确认项。",
            "4. 所有结论必须带引用并区分附件与知识库。",
            "5. 最后输出推荐 BOM 表。",
            "6. 资料不足处明确标注待确认。",
        ]
    )
    limit, budget = _agent_recursion_limit(
        query=query,
        query_mode="deep",
        attachment_refs=[object(), object()],
        knowledge_scope=SimpleNamespace(sources=[object()]),
    )
    assert budget["complexity"]["level"] == "complex"
    assert budget["complexity"]["numbered_items"] >= 6
    assert budget["base_recursion_limit"] == 72
    assert 72 < limit <= 120
    assert budget["recursion_limit"] == limit


def test_simple_turn_keeps_historical_recursion_limit():
    limit, budget = _agent_recursion_limit(
        query="这个器件的工作电压是多少？",
        query_mode="deep",
        attachment_refs=[],
        knowledge_scope=None,
    )
    assert limit == 72
    assert budget["complexity"]["level"] == "simple"


def test_short_hardware_question_gets_complex_budget_and_evidence_room():
    query = "CAN0 从对外连接器进板到 U1200 收发器之间经过哪些器件？给出位号和信号名。"
    limit, budget = _agent_recursion_limit(
        query=query,
        query_mode="deep",
        attachment_refs=[],
        knowledge_scope=None,
    )
    assert budget["complexity"]["hardware"]["is_hardware"] is True
    assert budget["complexity"]["level"] == "complex"
    assert limit > 72
    assert _agent_evidence_budget([], question=query) > _agent_evidence_budget([], question="你好")


def test_minimum_system_question_is_treated_as_hardware_multihop():
    limit, budget = _agent_recursion_limit(
        query="TC377 最小系统包含哪些关键器件？",
        query_mode="deep",
        attachment_refs=[],
        knowledge_scope=None,
    )
    assert budget["complexity"]["hardware"]["is_hardware"] is True
    assert budget["complexity"]["level"] == "complex"
    assert limit > 72


def test_hardware_removal_target_mismatch_is_a_failed_check():
    result = _build_turn_verification(
        answer_text="应拆除 U1200，受影响的是 X1900 的 2、3 脚。",
        evidence=[],
        query="拆除 L1200 会造成什么故障？受影响的是哪个连接器的哪两个脚？",
        model=object(),
        complexity={"level": "complex"},
        budget_exhausted=False,
        require_evidence=True,
    )
    checks = {check["key"]: check for check in result["checks"]}
    assert checks["removal_target"]["status"] == "fail"
    assert checks["hardware_entities"]["status"] == "warn"
    assert result["status"] == "fail"


def test_research_subagent_receives_read_only_tools_only():
    tools = [
        SimpleNamespace(name="document_search"),
        SimpleNamespace(name="attachment_read"),
        SimpleNamespace(name="attachment_edit"),
        SimpleNamespace(name="generate_document_from_template"),
    ]
    spec = _research_subagent_spec(tools=tools, model=object())
    assert spec is not None
    assert {tool.name for tool in spec["tools"]} == {"document_search", "attachment_read"}


def test_partial_synthesis_trims_unregistered_citations_and_compacts_context():
    class _Model:
        def invoke(self, messages):
            assert "已登记证据" in messages[-1].content
            return AIMessage(content="已确认 5V 支路 [1]，但 8A 主轨 [9] 待确认。")

    events: list[dict] = []
    budget: dict = {}
    text = _synthesize_partial_answer(
        model=_Model(),
        query="给出电源建议",
        evidence=[_evidence(1)],
        partial_answer="",
        emit_event=events.append,
        budget_state=budget,
    )
    assert text is not None
    assert "[1]" in text
    assert "[9]" not in text
    assert budget["context_compressed"]["evidence_count"] == 1
    assert events[0]["type"] == "evidence_compressed"


def test_semantic_verifier_sanitizes_json_and_exposes_gaps():
    class _Model:
        def invoke(self, messages):
            assert "只输出一个 JSON 对象" in messages[-1].content
            return AIMessage(
                content='```json\n{"status":"warn","checks":[{"key":"task_completion",'
                '"label":"五类覆盖","status":"warn","detail":"连接器待确认"}],'
                '"gaps":["缺少车规连接器型号"]}\n```'
            )

    result = _semantic_verification(
        model=_Model(),
        query="给出五类 BOM",
        answer_text="结论" * 400,
        evidence=[_evidence(1)],
        complexity={"level": "complex"},
        budget_exhausted=False,
    )
    assert result is not None
    assert result["status"] == "warn"
    assert result["checks"][0]["source"] == "model_rubric"
    assert result["gaps"] == ["缺少车规连接器型号"]


def test_source_labelled_citations_are_normalized_to_numeric_markers():
    assert _normalize_citation_labels("附件 [A2]，知识库 [K6]，【知识库:7】") == (
        "附件 [2]，知识库 [6]，[7]"
    )


def test_runner_normalizes_source_labelled_citations(monkeypatch):
    events: list[dict] = []
    fake_agent = SimpleNamespace(
        stream=lambda *args, **kwargs: iter([
            (AIMessage(content="附件建议 [A1]，知识库约束 [K2]。"), {"langgraph_node": "model"}),
        ])
    )
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", lambda **kwargs: fake_agent)
    monkeypatch.setattr(runner_mod, "record_agent", lambda **kwargs: None)
    original_prepare = MultiSourceAgentRunner._prepare_turn

    def _prepare(self, **kwargs):
        plan = original_prepare(self, **kwargs)
        plan.rt.add_evidence([_evidence(1, attachment=True), _evidence(2)])
        return plan

    monkeypatch.setattr(MultiSourceAgentRunner, "_prepare_turn", _prepare)
    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)
    deltas = list(
        runner.stream(
            query="复杂任务：请对比附件与知识库并给出引用和风险。",
            kb_name="kb_hw",
            history=[],
            event_callback=events.append,
        )
    )
    assert "".join(deltas)  # live deltas are corrected by the authoritative done answer
    assert runner_mod._current_run().answer == "附件建议 [1]，知识库约束 [2]。"
    assert runner.get_last_agent_answer() == "附件建议 [1]，知识库约束 [2]。"
    summary = runner.get_last_retrieval_summary()
    assert summary["verification"]["invalid_citations"] == []
    assert summary["verification"]["citation_coverage"] == 1.0


def test_invalid_citations_are_removed_with_a_turn_scope_note():
    text, removed = _scrub_invalid_citations(
        "本轮证据 [1]，上一轮引用 [9]，另一个无效 [12]。",
        [_evidence(1)],
    )
    assert "[1]" in text
    assert "[9]" not in text and "[12]" not in text
    assert "不在本轮证据中的引用编号" in text
    assert removed == [9, 12]


def test_runner_scrubs_cross_turn_dangling_citations(monkeypatch):
    fake_agent = SimpleNamespace(
        stream=lambda *args, **kwargs: iter([
            (AIMessage(content="本轮证据 [1]，上一轮引用 [9]。"), {"langgraph_node": "model"}),
        ])
    )
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", lambda **kwargs: fake_agent)
    monkeypatch.setattr(runner_mod, "record_agent", lambda **kwargs: None)
    original_prepare = MultiSourceAgentRunner._prepare_turn

    def _prepare(self, **kwargs):
        plan = original_prepare(self, **kwargs)
        plan.rt.add_evidence([_evidence(1)])
        return plan

    monkeypatch.setattr(MultiSourceAgentRunner, "_prepare_turn", _prepare)
    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)
    list(runner.stream(query="简单追问：上一轮结论是什么？", kb_name="kb_hw", history=[]))
    persisted = runner_mod._current_run().answer
    assert "[1]" in persisted
    assert "[9]" not in persisted
    summary = runner.get_last_retrieval_summary()
    assert summary["verification"]["removed_citations"] == [9]


def test_semantic_verifier_retries_unparseable_output():
    class _Model:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content="这不是 JSON")
            return AIMessage(
                content='{"status":"warn","checks":[{"key":"task_completion",'
                '"label":"覆盖","status":"warn","detail":"缺失连接器型号"}],'
                '"gaps":["缺少连接器型号"]}'
            )

    model = _Model()
    result = _semantic_verification(
        model=model,
        query="给出五类 BOM",
        answer_text="结论" * 400,
        evidence=[_evidence(1)],
        complexity={"level": "complex"},
        budget_exhausted=False,
    )
    assert model.calls == 2
    assert result is not None
    assert result["status"] == "warn"
    assert result["gaps"] == ["缺少连接器型号"]


def test_turn_verification_marks_partial_and_detects_source_separation():
    result = _build_turn_verification(
        answer_text="附件说明 5V/3A [1]，知识库给出主轨约束 [2]。",
        evidence=[_evidence(1, attachment=True), _evidence(2)],
        query="对比附件和知识库",
        model=object(),
        complexity={"level": "complex"},
        budget_exhausted=True,
        require_evidence=True,
    )
    assert result["status"] == "partial"
    checks = {check["key"]: check for check in result["checks"]}
    assert checks["source_separation"]["status"] == "pass"
    assert checks["budget"]["status"] == "warn"
    assert result["budget_exhausted"] is True


def test_complex_deep_turn_does_not_offer_a_planner_but_keeps_research_subagent(monkeypatch):
    captured: dict = {}

    class _Agent:
        def stream(self, *args, **kwargs):
            yield AIMessage(content="ok"), {"langgraph_node": "model"}

    def _create_agent(**kwargs):
        captured.update(kwargs)
        return _Agent()

    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", _create_agent)
    monkeypatch.setattr(runner_mod, "record_agent", lambda **kwargs: None)
    query = "\n".join(
        [
            "请完成一份跨来源的复杂硬件选型报告，要求逐项对比、给出风险、引用和推荐表。"
            "必须列出至少三类器件，说明复用或替换理由，并标注所有待确认项。"
            "至少给出三个风险，所有结论必须带引用，最后输出表格和下一步建议。",
            "1. 读取上一代 BOM。",
            "2. 检索知识库中的供电和温度约束。",
            "3. 检索附件中的接口定义。",
            "4. 按五类器件给出建议。",
            "5. 输出风险清单。",
            "6. 输出推荐表。",
            "补充要求：" + "每个结论都要说明证据来源、适用条件、余量和不能外推的边界。" * 8,
        ]
    )
    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)
    list(runner.stream(query=query, kb_name="kb_hw", history=[]))
    middleware_names = {
        type(item).__name__.lower() for item in captured.get("middleware") or []
    }
    assert not any("plan" in name or "todo" in name for name in middleware_names)
    assert "loopbudgetmiddleware" in middleware_names
    subagents = captured.get("subagents") or []
    assert [item["name"] for item in subagents] == ["research"]
    tool_names = {
        getattr(tool, "name", None) or getattr(tool, "__name__", "")
        for tool in subagents[0]["tools"]
    }
    assert "document_search" in tool_names
    assert "knowledge_search" in tool_names
    assert "evidence_read" in tool_names
    assert "attachment_edit" not in tool_names
    assert "generate_document_from_template" not in tool_names


def test_runner_does_not_emit_plan_events(monkeypatch):
    query = "\n".join(
        [
            "请完成五类 BOM 复用建议并给出风险、引用和推荐表。",
            "1. 对比上一代 BOM。",
            "2. 按电源、CAN FD、以太网、MCU、连接器输出。",
            "3. 至少列出 3 个风险。",
            "4. 所有结论必须带引用。",
            "5. 最后输出表格。",
            "6. 资料不足处标注待确认。",
        ]
    )
    final = AIMessage(content="候选 BOM 如下。")
    runner, events = _run_with_chunks(monkeypatch, query=query, chunks=[final])

    event_types = [event["type"] for event in events]
    assert "plan_created" not in event_types
    assert "plan_updated" not in event_types
    assert "budget" in event_types
    assert "verification" in event_types
    summary = runner.get_last_retrieval_summary()
    assert "plan" not in summary
    assert summary["budget"]["complexity"]["level"] in {"moderate", "complex"}
    assert summary["verification"]["status"] in {"warn", "fail", "pass"}


def test_runner_recovers_short_uncited_complex_answer(monkeypatch):
    short_prefix = "# 复用上一代项目 BOM 的新项目选型建议\n\n> 证据说明：本报告将对比附件和知识库。"

    class _Model:
        def invoke(self, messages):
            return AIMessage(content="完整的部分 BOM 建议 [1]。")

    class _Agent:
        def stream(self, *args, **kwargs):
            yield AIMessage(content=short_prefix), {"langgraph_node": "model"}

    events: list[dict] = []
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: _Model())
    monkeypatch.setattr(runner_mod, "create_deep_agent", lambda **kwargs: _Agent())
    monkeypatch.setattr(runner_mod, "record_agent", lambda **kwargs: None)
    original_prepare = MultiSourceAgentRunner._prepare_turn

    def _prepare(self, **kwargs):
        plan = original_prepare(self, **kwargs)
        plan.rt.add_evidence([_evidence(1)])
        return plan

    monkeypatch.setattr(MultiSourceAgentRunner, "_prepare_turn", _prepare)
    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)
    query = "\n".join(
        [
            "请完成一份复用上一代 BOM 的新项目选型建议，要求对比、输出、风险、引用和推荐表。",
            "1. 读取 BOM。",
            "2. 检索知识库。",
            "3. 按五类器件输出建议。",
            "4. 至少列出 3 个风险。",
            "5. 所有结论必须带引用。",
            "6. 最后输出推荐表。",
        ]
    )
    deltas = list(runner.stream(query=query, kb_name="kb_hw", history=[], event_callback=events.append))
    assert any("完整的部分 BOM 建议" in part for part in deltas)
    assert runner_mod._current_run().answer == "完整的部分 BOM 建议 [1]。"
    summary = runner.get_last_retrieval_summary()
    assert summary["budget"]["status"] == "partial"
    assert summary["budget"]["partial_answer"] is True
    assert summary["verification"]["status"] == "partial"


def test_runner_emits_subagent_lifecycle_without_raw_context(monkeypatch):
    task_call = AIMessage(
        content="",
        tool_calls=[{
            "name": "task",
            "args": {"description": "检索电源与温度约束", "subagent_type": "research"},
            "id": "call-task",
            "type": "tool_call",
        }],
    )
    task_result = ToolMessage(
        content="SUBAGENT_RAW_CONTEXT_SHOULD_NOT_BE_SHOWN",
        tool_call_id="call-task",
        name="task",
    )
    fake_agent = SimpleNamespace(
        stream=lambda *args, **kwargs: iter([
            (task_call, {"langgraph_node": "model"}),
            (task_result, {"langgraph_node": "tools"}),
            (AIMessage(content="最终回答"), {"langgraph_node": "model"}),
        ])
    )
    events: list[dict] = []
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", lambda **kwargs: fake_agent)
    monkeypatch.setattr(runner_mod, "record_agent", lambda **kwargs: None)
    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)
    list(
        runner.stream(
            query="复杂任务：请先检索多个来源，再给出推荐表。",
            kb_name="kb_hw",
            history=[],
            event_callback=events.append,
        )
    )
    started = [event for event in events if event["type"] == "subagent_started"]
    completed = [event for event in events if event["type"] == "subagent_completed"]
    assert started and started[0]["payload"]["name"] == "research"
    assert completed and completed[0]["payload"]["status"] == "completed"
    assert "SUBAGENT_RAW_CONTEXT" not in json.dumps(events, ensure_ascii=False)
    summary = runner.get_last_retrieval_summary()
    assert summary["subagents"][0]["status"] == "completed"
    assert summary["subagents"][0]["result_chars"] == len("SUBAGENT_RAW_CONTEXT_SHOULD_NOT_BE_SHOWN")
    assert "SUBAGENT_RAW_CONTEXT" not in json.dumps(summary["subagents"], ensure_ascii=False)


def test_runner_empty_final_answer_synthesizes_partial_delivery(monkeypatch):
    class _Model:
        def invoke(self, messages):
            return AIMessage(content="已基于已登记证据给出部分 BOM [1]。")

    events: list[dict] = []
    checkpoint_appends: list[str] = []

    class _Agent:
        def stream(self, *args, **kwargs):
            yield (
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "document_search",
                        "args": {"query": "电源", "top_k": 4},
                        "id": "call-1",
                        "type": "tool_call",
                    }],
                ),
                {"langgraph_node": "model"},
            )

        def update_state(self, config, values):
            checkpoint_appends.extend(
                str(message.content) for message in values.get("messages", [])
            )

    class _Checkpointer:
        def get_tuple(self, config):
            return None

    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: _Model())
    monkeypatch.setattr(runner_mod, "create_deep_agent", lambda **kwargs: _Agent())
    monkeypatch.setattr(runner_mod, "_create_checkpointer", lambda: _Checkpointer())
    monkeypatch.setattr(runner_mod, "record_agent", lambda **kwargs: None)
    original_prepare = MultiSourceAgentRunner._prepare_turn

    def _prepare(self, **kwargs):
        plan = original_prepare(self, **kwargs)
        plan.rt.add_evidence([_evidence(1)])
        return plan

    monkeypatch.setattr(MultiSourceAgentRunner, "_prepare_turn", _prepare)
    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)
    deltas = list(
        runner.stream(
            query="\n".join(
                [
                    "复杂任务：请完成五类 BOM 并给出风险、引用和表格。",
                    "1. 对比上一代 BOM。",
                    "2. 五类器件。",
                    "3. 至少 3 个风险。",
                    "4. 必须带引用。",
                    "5. 输出表格。",
                    "6. 标注待确认。",
                ]
            ),
            kb_name="kb_hw",
            history=[],
            thread_id="budget-test",
            persist_thread=True,
            event_callback=events.append,
        )
    )
    assert deltas == ["已基于已登记证据给出部分 BOM [1]。"]
    assert checkpoint_appends == ["已基于已登记证据给出部分 BOM [1]。"]
    summary = runner.get_last_retrieval_summary()
    assert summary["budget"]["status"] == "partial"
    assert summary["budget"]["partial_answer"] is True
    assert summary["budget"]["stopped_reason"] == "empty_answer"
    assert summary["verification"]["status"] == "partial"
    assert any(
        event["type"] == "partial_result"
        and event["payload"]["reason"] == "empty_answer"
        for event in events
    )


def test_runner_empty_final_answer_falls_back_to_evidence_digest(monkeypatch):
    class _Model:
        def invoke(self, messages):
            raise RuntimeError("provider unavailable")

    events: list[dict] = []

    class _Agent:
        def stream(self, *args, **kwargs):
            yield (
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "document_search",
                        "args": {"query": "电源", "top_k": 4},
                        "id": "call-1",
                        "type": "tool_call",
                    }],
                ),
                {"langgraph_node": "model"},
            )

    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: _Model())
    monkeypatch.setattr(runner_mod, "create_deep_agent", lambda **kwargs: _Agent())
    monkeypatch.setattr(runner_mod, "record_agent", lambda **kwargs: None)
    original_prepare = MultiSourceAgentRunner._prepare_turn

    def _prepare(self, **kwargs):
        plan = original_prepare(self, **kwargs)
        plan.rt.add_evidence([_evidence(1, content="电源证据")])
        return plan

    monkeypatch.setattr(MultiSourceAgentRunner, "_prepare_turn", _prepare)
    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)
    deltas = list(
        runner.stream(
            query="复杂任务：请完成五类 BOM 并给出风险、引用和表格。",
            kb_name="kb_hw",
            history=[],
            event_callback=events.append,
        )
    )
    text = deltas[-1]
    assert "[1]" in text
    assert "电源证据" in text
    summary = runner.get_last_retrieval_summary()
    assert summary["budget"]["status"] == "partial"
    assert summary["budget"]["synthesis"] == "digest"
    assert summary["verification"]["partial_delivery"] is True
    assert any(event["type"] == "partial_synthesis_failed" for event in events)
    assert any(
        event["type"] == "partial_result"
        and event["payload"].get("fallback") == "evidence_digest"
        for event in events
    )


def test_runner_recursion_stop_uses_partial_synthesis_when_model_supports_it(monkeypatch):
    from langgraph.errors import GraphRecursionError

    class _Model:
        def invoke(self, messages):
            return AIMessage(content="已基于已登记证据给出部分建议 [1]。")

    events: list[dict] = []

    class _Agent:
        def stream(self, *args, **kwargs):
            raise GraphRecursionError("Recursion limit reached")

    def _create_agent(**kwargs):
        # Register one piece of evidence through the request-scoped runtime by
        # invoking the document tool closure directly; the real loop is then
        # forced past its recursion limit.
        for tool in kwargs["tools"]:
            name = getattr(tool, "name", "")
            if name == "document_search":
                try:
                    tool.invoke({"query": "电源", "top_k": 4})
                except Exception:
                    pass
                break
        return _Agent()

    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: _Model())
    monkeypatch.setattr(runner_mod, "create_deep_agent", _create_agent)
    monkeypatch.setattr(runner_mod, "record_agent", lambda **kwargs: None)
    monkeypatch.setattr(
        runner_mod,
        "_synthesize_partial_answer",
        lambda **kwargs: (
            kwargs["budget_state"].update({"context_compressed": {"evidence_count": 0}})
            or "已基于已登记证据给出部分建议。"
        ),
    )
    # Provide evidence directly on the runtime used by the prepared turn.
    original_prepare = MultiSourceAgentRunner._prepare_turn

    def _prepare(self, **kwargs):
        plan = original_prepare(self, **kwargs)
        plan.rt.add_evidence([_evidence(1)])
        return plan

    monkeypatch.setattr(MultiSourceAgentRunner, "_prepare_turn", _prepare)
    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)
    deltas = list(
        runner.stream(
            query="\n".join(
                [
                    "复杂任务：请完成五类 BOM 并给出风险、引用和表格。",
                    "1. 对比上一代 BOM。",
                    "2. 五类器件。",
                    "3. 至少 3 个风险。",
                    "4. 必须带引用。",
                    "5. 输出表格。",
                    "6. 标注待确认。",
                ]
            ),
            kb_name="kb_hw",
            history=[],
            event_callback=events.append,
        )
    )
    assert deltas == ["已基于已登记证据给出部分建议。"]
    summary = runner.get_last_retrieval_summary()
    assert summary["budget"]["exhausted"] is True
    assert summary["budget"]["synthesis"] == "model"
    assert summary["verification"]["status"] == "partial"
    assert any(event["type"] == "partial_result" for event in events)
    assert any(event["type"] == "budget_exhausted" for event in events)


def test_message_view_projects_persisted_turn_metadata():
    from src.api.routes.conversations import _message_view

    message = SimpleNamespace(
        id=1,
        session_id=2,
        turn_id="turn-1",
        role="assistant",
        content="answer",
        footer="",
        created_at="2026-10-26T00:00:00Z",
        edited_at=None,
        redacted=False,
        memory_context=[],
        citations=[],
    )
    view = _message_view(
        message,
        plan={"items": [{"content": "step", "status": "completed"}], "revision": 1},
        budget={"status": "ok", "exhausted": False},
        verification={"status": "pass", "checks": []},
        subagents=[{"name": "research", "status": "completed"}],
    )
    assert view.plan["revision"] == 1
    assert view.budget["status"] == "ok"
    assert view.verification["status"] == "pass"
    assert view.subagents[0]["name"] == "research"


def test_runner_recursion_stop_without_model_synthesis_keeps_notice(monkeypatch):
    from langgraph.errors import GraphRecursionError

    class _Agent:
        def stream(self, *args, **kwargs):
            raise GraphRecursionError("Recursion limit reached")

    events: list[dict] = []
    monkeypatch.setattr(runner_mod, "create_chat_model", lambda: object())
    monkeypatch.setattr(runner_mod, "create_deep_agent", lambda **kwargs: _Agent())
    monkeypatch.setattr(runner_mod, "record_agent", lambda **kwargs: None)
    runner = MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(), circuit_service=None)
    deltas = list(
        runner.stream(query="完善附件第6章", kb_name="kb_hw", history=[], event_callback=events.append)
    )
    assert len(deltas) == 1
    assert "单轮步数上限" in deltas[0]
    assert runner.get_last_retrieval_summary()["budget"]["exhausted"] is True
    assert any(event["type"] == "partial_result" for event in events)


def test_user_tool_call_cap_parsed_from_query():
    assert parse_user_tool_call_cap("最多 8 次检索工具调用。") == 8
    assert parse_user_tool_call_cap("不要超过 5 次调用") == 5
    assert parse_user_tool_call_cap("至多3次检索") == 3
    # 无检索/调用语境的“最多 N 次”不生效
    assert parse_user_tool_call_cap("每种失效模式最多列出 5 次") is None
    assert parse_user_tool_call_cap("最多 10 次修改意见") is None
    # 空值/超大值不作为上限
    assert parse_user_tool_call_cap("最多 0 次检索") is None
    assert parse_user_tool_call_cap("最多 999 次检索") is None
    assert parse_user_tool_call_cap("帮我查一下 U1800 的型号") is None


def test_tool_call_cap_recorded_in_budget_state():
    query = "关键器件清单草稿,最多 8 次检索工具调用,固定五列。"
    limit, budget = _agent_recursion_limit(
        query=query,
        query_mode="deep",
        attachment_refs=[],
        knowledge_scope=None,
    )
    assert budget.get("tool_call_cap") == 8
    assert budget.get("tool_call_cap_source") == "user_query"


def test_task_mode_suppresses_export_intent():
    # In task mode a document-export phrase must not bypass the agent.
    assert _export_intent_for_turn("导出为 PDF", "task-1") is False
    assert _export_intent_for_turn("导出为 PDF", "") is True
    assert _export_intent_for_turn("输出(running)", "") is False
