"""Progress-aware loop control: productive searches may continue, repeats stop."""

from __future__ import annotations

from src.agents.schemas import Evidence
from src.agents.tools.runtime import ToolRuntime, timed_tool_call


def _evidence(eid: str, content: str = "MCU 供电为 VCC3V3_MCU。") -> Evidence:
    return Evidence(
        id=eid,
        content=content,
        source_name="设计说明.docx",
        content_kind="document_text",
        processor_kind="ragflow",
        score=0.9,
        locator={"page": 3},
        metadata={},
    )


def _search(rt: ToolRuntime, query: str, items: list[Evidence]):
    return timed_tool_call(rt, "document_search", query, None, lambda: list(items))


def test_repeated_search_without_new_material_stops_the_loop():
    rt = ToolRuntime(kb_name="kb", ctx=None)
    rt.no_progress_limit = 2
    items = [_evidence("doc:1")]

    _search(rt, "电源", items)
    _search(rt, "电源", items)
    assert not rt.should_stop_for_no_progress()

    _search(rt, "电源", items)
    assert rt.should_stop_for_no_progress()
    assert rt.loop_stop_reason == "no_progress"


def test_new_evidence_resets_the_no_progress_streak():
    rt = ToolRuntime(kb_name="kb", ctx=None)
    rt.no_progress_limit = 3
    first = [_evidence("doc:1")]
    second = [_evidence("doc:2", content="复位引脚为低有效。")]

    _search(rt, "电源", first)
    _search(rt, "电源", first)
    _search(rt, "电源", first)
    assert not rt.should_stop_for_no_progress()

    _search(rt, "复位", second)
    assert rt.no_progress_streak == 0
    assert not rt.should_stop_for_no_progress()

    _search(rt, "复位", second)
    assert not rt.should_stop_for_no_progress()


def test_candidate_only_search_still_counts_as_progress():
    rt = ToolRuntime(kb_name="kb", ctx=None)
    rt.no_progress_limit = 2
    rt.candidate_mode = True

    _search(rt, "电源", [_evidence(f"doc:{index}") for index in range(5)])
    _search(rt, "时钟", [_evidence(f"clk:{index}") for index in range(5)])

    assert rt.progress_events == 2
    assert not rt.should_stop_for_no_progress()


def test_candidate_search_does_not_consume_the_evidence_budget():
    rt = ToolRuntime(kb_name="kb", ctx=None, evidence_budget=1)
    rt.no_progress_limit = 2
    rt.candidate_mode = True

    _search(rt, "电源", [_evidence(f"doc:{index}") for index in range(5)])
    _search(rt, "时钟", [_evidence(f"clk:{index}") for index in range(5)])
    _search(rt, "复位", [_evidence(f"rst:{index}") for index in range(5)])

    assert rt.evidence == []
    assert len(rt.candidate_evidence) == 15
    assert not rt.should_stop_for_no_progress()


def test_failed_tool_counts_as_no_progress():
    rt = ToolRuntime(kb_name="kb", ctx=None)
    rt.no_progress_limit = 2

    def _fail():
        raise RuntimeError("backend unavailable")

    for _ in range(3):
        timed_tool_call(rt, "document_search", "电源", None, _fail)

    assert rt.should_stop_for_no_progress()


def test_no_progress_without_evidence_reports_not_found_not_budget_jargon():
    from src.agents.turn_meta import _agent_step_budget_answer

    rt = ToolRuntime(kb_name="AAA", ctx=None)
    rt.queries = [{"tool_name": "knowledge_search", "query": "U9999"}]
    rt.diagnostics = [{"tool_name": "knowledge_search", "hit_count": 0}]
    text = _agent_step_budget_answer(rt, "no_progress")
    assert "未找到" in text
    assert "U9999" in text
    assert "连续多次检索没有带来新证据" not in text
