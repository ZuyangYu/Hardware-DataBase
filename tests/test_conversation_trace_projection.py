from types import SimpleNamespace
import unittest

from src.api.routes.conversations import _trace_steps_from_events


def event(event_type: str, payload: dict):
    return SimpleNamespace(event_type=event_type, payload=payload)


class TraceStepProjectionTests(unittest.TestCase):
    def test_tool_call_pair_becomes_one_completed_step(self):
        steps = _trace_steps_from_events(
            [
                event(
                    "tool_started",
                    {"call_id": "inv-1", "tool_name": "document_search", "query": "电源结构"},
                ),
                event(
                    "tool_result",
                    {
                        "call_id": "inv-1",
                        "tool_name": "document_search",
                        "hit_count": 3,
                        "status": "done",
                        "latency_ms": 12,
                    },
                ),
            ]
        )
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["key"], "inv-1")
        self.assertEqual(steps[0]["label"], "文档检索")
        self.assertEqual(steps[0]["status"], "done")
        self.assertEqual(steps[0]["detail"], "3 条命中 · 12ms")

    def test_repeated_tool_calls_keep_separate_steps(self):
        steps = _trace_steps_from_events(
            [
                event("tool_started", {"call_id": "inv-1", "tool_name": "memory_search", "query": "a"}),
                event("tool_result", {"call_id": "inv-1", "tool_name": "memory_search", "hit_count": 0}),
                event("tool_started", {"call_id": "inv-2", "tool_name": "memory_search", "query": "b"}),
                event("tool_result", {"call_id": "inv-2", "tool_name": "memory_search", "hit_count": 2}),
            ]
        )
        self.assertEqual([step["key"] for step in steps], ["inv-1", "inv-2"])
        self.assertEqual(steps[0]["detail"], "0 条命中")
        self.assertEqual(steps[1]["detail"], "2 条命中")

    def test_tool_result_summary_is_projected_without_commentary(self):
        steps = _trace_steps_from_events(
            [
                event("narration", {"text": "先确认电源方案"}),
                event(
                    "tool_started",
                    {"call_id": "inv-1", "tool_name": "document_search", "query": "电源方案"},
                ),
                event(
                    "tool_result",
                    {
                        "call_id": "inv-1",
                        "tool_name": "document_search",
                        "hit_count": 5,
                        "evidence_count": 3,
                        "candidate_count": 2,
                        "sources": ["设计说明.docx", "BOM.xlsx"],
                        "citation_numbers": [1, 2, 3],
                        "status": "done",
                    },
                ),
            ]
        )
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["kind"], "tool")
        self.assertEqual(steps[0]["sources"], ["设计说明.docx", "BOM.xlsx"])
        self.assertEqual(steps[0]["evidence_count"], 3)
        self.assertEqual(steps[0]["candidate_count"], 2)
        self.assertEqual(steps[0]["citation_numbers"], [1, 2, 3])

    def test_empty_tool_result_keeps_reason(self):
        steps = _trace_steps_from_events(
            [
                event("tool_started", {"call_id": "inv-1", "tool_name": "circuit_search", "query": "PMU"}),
                event(
                    "tool_result",
                    {
                        "call_id": "inv-1",
                        "tool_name": "circuit_search",
                        "status": "done",
                        "hit_count": 0,
                        "sources": [],
                        "empty_reason": "no_match",
                    },
                ),
            ]
        )
        self.assertEqual(steps[0]["status"], "done")
        self.assertEqual(steps[0]["empty_reason"], "no_match")
        self.assertEqual(steps[0]["sources"], [])

    def test_parallel_same_tool_calls_pair_by_call_id(self):
        steps = _trace_steps_from_events(
            [
                event("tool_started", {"call_id": "inv-1", "tool_name": "document_search", "query": "a"}),
                event("tool_started", {"call_id": "inv-2", "tool_name": "document_search", "query": "b"}),
                event(
                    "tool_result",
                    {"call_id": "inv-2", "tool_name": "document_search", "hit_count": 2, "status": "done"},
                ),
                event(
                    "tool_result",
                    {"call_id": "inv-1", "tool_name": "document_search", "hit_count": 1, "status": "done"},
                ),
            ]
        )
        self.assertEqual([step["key"] for step in steps], ["inv-1", "inv-2"])
        self.assertEqual([step["status"] for step in steps], ["done", "done"])
        self.assertEqual([step["detail"] for step in steps], ["1 条命中", "2 条命中"])

    def test_failed_tool_result_marks_error(self):
        steps = _trace_steps_from_events(
            [
                event("tool_started", {"call_id": "inv-1", "tool_name": "circuit_search", "query": "net"}),
                event(
                    "tool_result",
                    {"call_id": "inv-1", "tool_name": "circuit_search", "hit_count": 0, "status": "failed"},
                ),
            ]
        )
        self.assertEqual(steps[0]["status"], "error")

    def test_stage_events_merge_and_internal_keys_are_ignored(self):
        steps = _trace_steps_from_events(
            [
                event("stage", {"key": "balanced_route_retry", "label": "来源路由降级重试", "status": "running"}),
                event(
                    "stage",
                    {
                        "key": "balanced_route_retry",
                        "label": "来源路由降级重试",
                        "status": "done",
                        "detail": "补回 2 条",
                    },
                ),
                event("stage", {"key": "generate", "label": "生成回答", "status": "done"}),
                event("stage", {"key": "permission", "label": "权限检查", "status": "done"}),
                event("stage", {"key": "skill_loaded", "label": "Skill 已挂载", "status": "running"}),
            ]
        )
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["key"], "balanced_route_retry")
        self.assertEqual(steps[0]["status"], "done")
        self.assertEqual(steps[0]["detail"], "补回 2 条")

    def test_unknown_tool_name_falls_back_to_raw_name(self):
        steps = _trace_steps_from_events(
            [event("tool_started", {"tool_name": "future_tool", "query": "x"})]
        )
        self.assertEqual(steps[0]["label"], "future_tool")
        self.assertEqual(steps[0]["status"], "running")


if __name__ == "__main__":
    unittest.main()
