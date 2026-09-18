from types import SimpleNamespace
import unittest

from src.api.routes.conversations import _trace_steps_from_events


def event(event_type: str, payload: dict):
    return SimpleNamespace(event_type=event_type, payload=payload)


class TraceStepProjectionTests(unittest.TestCase):
    def test_tool_call_pair_becomes_one_completed_step(self):
        steps = _trace_steps_from_events(
            [
                event("tool_started", {"tool_name": "document_search", "query": "电源结构"}),
                event(
                    "tool_result",
                    {
                        "tool_name": "document_search",
                        "hit_count": 3,
                        "status": "done",
                        "latency_ms": 12,
                    },
                ),
            ]
        )
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["key"], "document_search#1")
        self.assertEqual(steps[0]["label"], "文档检索")
        self.assertEqual(steps[0]["status"], "done")
        self.assertEqual(steps[0]["detail"], "3 条命中 · 12ms")

    def test_repeated_tool_calls_keep_separate_steps(self):
        steps = _trace_steps_from_events(
            [
                event("tool_started", {"tool_name": "memory_search", "query": "a"}),
                event("tool_result", {"tool_name": "memory_search", "hit_count": 0}),
                event("tool_started", {"tool_name": "memory_search", "query": "b"}),
                event("tool_result", {"tool_name": "memory_search", "hit_count": 2}),
            ]
        )
        self.assertEqual([step["key"] for step in steps], ["memory_search#1", "memory_search#2"])
        self.assertEqual(steps[0]["detail"], "0 条命中")
        self.assertEqual(steps[1]["detail"], "2 条命中")

    def test_failed_tool_result_marks_error(self):
        steps = _trace_steps_from_events(
            [
                event("tool_started", {"tool_name": "circuit_search", "query": "net"}),
                event(
                    "tool_result",
                    {"tool_name": "circuit_search", "hit_count": 0, "status": "failed"},
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
