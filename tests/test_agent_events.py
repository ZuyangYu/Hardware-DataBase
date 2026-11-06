"""Contract tests for the typed agent-turn event protocol."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from src.contracts.agent_events import (
    EVENT_SCHEMA_VERSION,
    UnknownAgentEventError,
    build_agent_event,
    tool_label,
    validate_agent_event,
    validate_event_envelope,
)


class ToolEventContractTests(unittest.TestCase):
    def test_tool_started_requires_call_id_and_fills_identity(self):
        with self.assertRaises(ValidationError):
            validate_agent_event("tool_started", {"tool_name": "document_search"})

        payload = validate_agent_event(
            "tool_started",
            {"call_id": "inv-7", "tool_name": "document_search", "query": "电源结构"},
        )
        self.assertEqual(payload["schema_version"], EVENT_SCHEMA_VERSION)
        self.assertEqual(payload["call_id"], "inv-7")
        self.assertEqual(payload["item_id"], "tool:inv-7")
        self.assertEqual(payload["label"], "文档检索")
        self.assertEqual(payload["detail"], "电源结构")

    def test_tool_result_carries_correlation_and_ui_detail(self):
        payload = validate_agent_event(
            "tool_result",
            {
                "call_id": "inv-7",
                "tool_name": "document_search",
                "hit_count": 3,
                "status": "done",
                "latency_ms": 9,
            },
        )
        self.assertEqual(payload["item_id"], "tool:inv-7")
        self.assertEqual(payload["detail"], "3 条命中 · 9ms")

    def test_mcp_and_sandbox_details_are_backend_owned(self):
        mcp = validate_agent_event(
            "tool_result",
            {
                "call_id": "inv-8",
                "tool_name": "mcp__kicad__create_schematic",
                "tool_kind": "mcp",
                "server": "kicad",
                "tool": "create_schematic",
                "status": "proposed",
                "latency_ms": 12,
            },
        )
        self.assertEqual(mcp["label"], "外部插件 · kicad · create_schematic")
        self.assertEqual(mcp["detail"], "kicad/create_schematic · proposed · 12ms")

        sandbox = validate_agent_event(
            "tool_result",
            {
                "call_id": "inv-9",
                "tool_name": "skill__abc",
                "tool_kind": "sandbox",
                "skill_ref": "skill/report",
                "task_id": "task-1",
                "status": "succeeded",
            },
        )
        self.assertEqual(sandbox["label"], "沙箱 Skill")
        self.assertEqual(sandbox["detail"], "任务 task-1 · succeeded")

    def test_unknown_tool_fields_fail_closed(self):
        with self.assertRaises(ValidationError):
            validate_agent_event(
                "tool_started",
                {"call_id": "inv-1", "tool_name": "x", "typo_field": True},
            )


class MetaEventContractTests(unittest.TestCase):
    def test_stage_keeps_extra_provenance(self):
        payload = validate_agent_event(
            "stage",
            {
                "key": "route",
                "label": "来源路由",
                "status": "done",
                "detail": "命中 2 个来源组",
                "policy": "balanced",
            },
        )
        self.assertEqual(payload["item_id"], "stage:route")
        self.assertEqual(payload["policy"], "balanced")

    def test_subagent_and_compaction_have_stable_item_ids(self):
        subagent = validate_agent_event(
            "subagent_started",
            {"name": "research", "status": "running", "tool_call_id": "call-1"},
        )
        self.assertEqual(subagent["item_id"], "subagent:call-1")

        compaction = validate_agent_event(
            "context_compacted",
            {
                "stage": "summarization",
                "trigger": "auto",
                "cutoff_index": 20,
                "dropped_messages": 20,
                "retained_messages": 8,
                "before_tokens": 42000,
                "after_tokens": 9000,
                "summary_chars": 1200,
                "summary_sha256": "a" * 64,
                "window_id": "turn-1:aaaaaaaaaaaa",
            },
        )
        self.assertEqual(compaction["item_id"], "context_compaction:turn-1:aaaaaaaaaaaa")
        self.assertEqual(compaction["dropped_messages"], 20)
        self.assertEqual(compaction["after_tokens"], 9000)

        evidence = validate_agent_event(
            "evidence_compressed",
            {"evidence_count": 3, "compressed_bytes": 2048, "max_bytes": 4096},
        )
        self.assertEqual(evidence["item_id"], "evidence_compressed")
        self.assertEqual(evidence["compressed_bytes"], 2048)

        pressure = validate_agent_event(
            "context_pressure",
            {"max_prompt_tokens": 60000, "window_tokens": 65536, "threshold_tokens": 55705},
        )
        self.assertEqual(pressure["item_id"], "context_pressure")
        self.assertEqual(pressure["max_prompt_tokens"], 60000)

    def test_done_allows_optional_payload_fields(self):
        payload = validate_agent_event("done", {"answer": "ok"})
        self.assertEqual(payload["item_id"], "turn")
        self.assertEqual(payload["answer"], "ok")
        self.assertNotIn("summary", payload)

    def test_timeout_events_have_contracts(self):
        payload = validate_agent_event(
            "chat_queue_timeout",
            {"error_code": "chat_queue_timeout", "wait_seconds": 600},
        )
        self.assertEqual(payload["item_id"], "timeout:chat_queue_timeout")
        self.assertEqual(payload["wait_seconds"], 600)

    def test_unknown_event_type_is_rejected(self):
        with self.assertRaises(UnknownAgentEventError):
            validate_agent_event("not_a_real_event", {})


class EnvelopeTests(unittest.TestCase):
    def test_build_and_validate_envelope_round_trip(self):
        event = build_agent_event(
            "tool_started",
            call_id="inv-1",
            tool_name="document_search",
            query="q",
        )
        self.assertEqual(event["type"], "tool_started")
        self.assertEqual(event["payload"]["call_id"], "inv-1")
        self.assertEqual(validate_event_envelope(event), event)

    def test_non_object_payload_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_event_envelope({"type": "delta", "payload": "not-an-object"})

    def test_tool_labels_cover_external_namespaces(self):
        self.assertEqual(tool_label("mcp__kicad__route"), "外部插件 · kicad · route")
        self.assertEqual(tool_label("skill__abc"), "沙箱 Skill")
        self.assertEqual(tool_label("unknown_tool"), "unknown_tool")


if __name__ == "__main__":
    unittest.main()
