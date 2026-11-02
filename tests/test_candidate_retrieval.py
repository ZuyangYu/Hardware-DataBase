"""Candidate-first retrieval: broad hits must not consume the evidence budget."""

import unittest

from src.agents.schemas import Evidence
from src.agents.tools.evidence_tools import make_evidence_read
from src.agents.tools.runtime import (
    ToolRuntime,
    format_tool_result,
    timed_tool_call,
)


def _evidence(eid: str, content: str | None = None) -> Evidence:
    return Evidence(
        id=eid,
        content=content or f"{eid} 的完整正文：MCU 供电为 VCC3V3_MCU。",
        source_name="设计说明.docx",
        content_kind="document_text",
        processor_kind="ragflow",
        score=0.9,
        locator={"page": 3},
        metadata={},
    )


def _search(rt: ToolRuntime, query: str, count: int = 5):
    items = [_evidence(f"doc:{query}:{index}") for index in range(count)]
    return timed_tool_call(rt, "document_search", query, None, lambda: items)


class CandidateRetrievalTests(unittest.TestCase):
    def test_search_returns_only_pointers_until_evidence_read(self):
        events: list[dict] = []
        rt = ToolRuntime(kb_name="kb", ctx=None, on_event=events.append)
        rt.candidate_mode = True
        admitted, adds_nothing = _search(rt, "电源")

        self.assertFalse(adds_nothing)
        self.assertEqual(list(admitted), [])
        self.assertEqual(rt.evidence, [])
        self.assertEqual(admitted.candidate_count, 5)
        self.assertEqual(
            [row["id"] for row in admitted.candidate_rows],
            [f"doc:电源:{index}" for index in range(5)],
        )
        result = [event for event in events if event["type"] == "tool_result"][-1]
        self.assertEqual(result["payload"]["evidence_count"], 0)
        self.assertEqual(result["payload"]["candidate_count"], 5)

        rendered = format_tool_result(rt, adds_nothing, admitted)
        self.assertIn("候选证据", rendered)
        self.assertIn("id=doc:电源:0", rendered)
        self.assertNotIn("<evidence ", rendered)

    def test_candidates_do_not_consume_budget_across_searches(self):
        rt = ToolRuntime(kb_name="kb", ctx=None)
        rt.candidate_mode = True
        for index in range(3):
            _search(rt, f"q{index}")

        self.assertEqual(len(rt.evidence), 0)
        self.assertEqual(len(rt.candidate_evidence), 15)
        self.assertEqual(rt.evidence_budget, 30)

    def test_evidence_read_promotes_only_the_selected_candidate(self):
        events: list[dict] = []
        rt = ToolRuntime(kb_name="kb", ctx=None, on_event=events.append)
        rt.candidate_mode = True
        _search(rt, "电源")
        read = make_evidence_read(rt)

        rendered = read(["doc:电源:3"])

        self.assertEqual([item.id for item in rt.evidence], ["doc:电源:3"])
        self.assertIn("[1] 来源", rendered)
        self.assertIn("doc:电源:3 的完整正文", rendered)
        self.assertNotIn("doc:电源:2", rendered)
        self.assertNotIn("doc:电源:3", rt.candidate_evidence)
        self.assertEqual(len(rt.candidate_evidence), 4)
        result = [event for event in events if event["type"] == "tool_result"][-1]
        self.assertEqual(result["payload"]["tool_name"], "evidence_read")
        self.assertEqual(result["payload"]["citation_numbers"], [1])

    def test_evidence_read_after_budget_is_full_keeps_the_candidate(self):
        rt = ToolRuntime(kb_name="kb", ctx=None, evidence_budget=1)
        rt.candidate_mode = True
        rt.add_evidence([_evidence("formal")])
        _search(rt, "电源")
        read = make_evidence_read(rt)

        rendered = read(["doc:电源:2"])

        self.assertEqual(len(rt.evidence), 1)
        self.assertIn("预算", rendered)
        self.assertIn("doc:电源:2", rt.candidate_evidence)

    def test_repeat_search_keeps_pointers_but_adds_no_formal_evidence(self):
        rt = ToolRuntime(kb_name="kb", ctx=None)
        rt.candidate_mode = True
        admitted, _ = _search(rt, "电源")
        repeated, _ = _search(rt, "电源")

        rendered = format_tool_result(rt, False, repeated)

        self.assertEqual(rt.evidence, [])
        self.assertIn("候选证据", rendered)
        self.assertEqual(rendered.count("<evidence "), 0)
        self.assertEqual(admitted.candidate_count, repeated.candidate_count)

    def test_candidate_read_fills_a_target_that_was_only_a_pointer(self):
        from types import SimpleNamespace

        from src.services.turn_evidence_plan import TurnEvidencePlan
        from tests.test_turn_evidence_plan import evidence as plan_evidence, target

        plan = TurnEvidencePlan.from_targets(
            [target(record_id=101), target(record_id=102, target_id="t2")],
            selected_kb_ids=[5],
        )
        rt = ToolRuntime(
            kb_name="kb",
            ctx=None,
            knowledge_scope=SimpleNamespace(sources=[SimpleNamespace(kb_id=5)]),
            turn_evidence_plan=plan,
            evidence_budget=5,
        )
        rt.candidate_mode = True
        first = plan_evidence(record_id=101)
        first.id = "e-101a"
        second = plan_evidence(record_id=101)
        second.id = "e-101b"
        third = plan_evidence(record_id=102, target_ids=("t2",))
        admitted, _ = timed_tool_call(
            rt, "document_search", "pin 2 voltage", None,
            lambda: [first, second, third],
        )

        self.assertEqual(len(admitted), 0)
        self.assertEqual(
            [row["id"] for row in admitted.candidate_rows],
            ["e-101a", "e-101b", "e-102"],
        )

        make_evidence_read(rt)(["e-102"])

        snapshot = plan.coverage_snapshot()
        self.assertEqual(snapshot["states"]["admitted"], 1)
        self.assertEqual(snapshot["states"]["candidate"], 0)

    def test_evidence_read_points_missing_refdes_back_to_search(self):
        rt = ToolRuntime(kb_name="kb", ctx=None, question="Q1600 的引脚 4 连接到哪里？")
        rt.candidate_mode = True
        _search(rt, "电源")

        rendered = make_evidence_read(rt)(["doc:电源:0"])

        self.assertIn("Q1600", rendered)
        self.assertIn("circuit_search", rendered)

    def test_evidence_read_points_threshold_gap_to_tables(self):
        rt = ToolRuntime(
            kb_name="kb", ctx=None, question="VCC3V3_EQ 的过压门限是多少？"
        )
        rt.candidate_mode = True
        _search(rt, "电源")

        rendered = make_evidence_read(rt)(["doc:电源:0"])

        self.assertIn("spreadsheet_row_search", rendered)
        self.assertIn("WCCA", rendered)

    def test_candidate_render_lists_remaining_ids_as_pointers(self):
        rt = ToolRuntime(kb_name="kb", ctx=None)
        rt.candidate_mode = True
        admitted, _ = _search(rt, "电源", count=20)

        rendered = format_tool_result(rt, False, admitted)

        self.assertIn("其余候选指针", rendered)
        self.assertIn("id=doc:电源:19", rendered)

    def test_evidence_read_reminds_about_unread_candidates(self):
        rt = ToolRuntime(kb_name="kb", ctx=None)
        rt.candidate_mode = True
        _search(rt, "电源", count=20)

        rendered = make_evidence_read(rt)(["doc:电源:0"])

        self.assertIn("仍有 19 条候选未读", rendered)
        self.assertIn("再决定是否声明缺口", rendered)


if __name__ == "__main__":
    unittest.main()
