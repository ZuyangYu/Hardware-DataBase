"""Model-aware context windows, context manifests and real compaction events."""

from __future__ import annotations

import unittest

from langchain_core.messages import HumanMessage

from src.agents.context_budget import (
    build_compaction_event,
    build_context_manifest,
    summarization_snapshot,
    text_sha256,
)
from src.core.model_context import (
    catalog_context_window,
    model_context_window,
    resolve_model_context_window,
)


class ModelContextWindowTests(unittest.TestCase):
    def test_known_small_model_caps_stale_global_default(self):
        resolved = resolve_model_context_window("ollama", "qwen2.5:32b", 65536)
        self.assertEqual(resolved.window, 32768)
        self.assertEqual(resolved.source, "model_catalog_cap")
        self.assertTrue(resolved.capped)

    def test_smaller_configured_value_is_kept(self):
        resolved = resolve_model_context_window("custom", "deepseek-chat", 32768)
        self.assertEqual(resolved.window, 32768)
        self.assertEqual(resolved.source, "configured")
        self.assertFalse(resolved.capped)

    def test_unknown_model_keeps_configured_value(self):
        resolved = resolve_model_context_window("custom", "private-model", 65536)
        self.assertEqual(resolved.window, 65536)
        self.assertIsNone(resolved.catalog_window)

    def test_zero_disables_profile(self):
        resolved = resolve_model_context_window("custom", "deepseek-chat", 0)
        self.assertEqual(resolved.window, 0)
        self.assertEqual(resolved.source, "disabled")

    def test_catalog_and_instance_profile(self):
        self.assertEqual(catalog_context_window("gpt-4o-2024-08-06"), 128000)
        self.assertEqual(catalog_context_window("unknown"), None)

        class _Model:
            profile = {"max_input_tokens": 32768}

        self.assertEqual(model_context_window(_Model(), fallback=65536), 32768)


class ContextManifestTests(unittest.TestCase):
    def test_manifest_hashes_without_persisting_content(self):
        class _Evidence:
            content = "电源芯片必须使用 3.3V。"

        manifest = build_context_manifest(
            system_prompt="fixed rules",
            fragments=[("base_rules", "fixed rules"), ("memory", "remember this")],
            evidence=[_Evidence()],
            model_context_window=32768,
            estimated_input_tokens=1200,
            history_turns=3,
        )
        self.assertEqual(manifest["system_prompt_sha256"], text_sha256("fixed rules"))
        self.assertEqual(manifest["model_context_window"], 32768)
        self.assertEqual(manifest["history_turns"], 3)
        self.assertEqual(manifest["evidence_count"], 1)
        self.assertGreater(manifest["evidence_bytes"], 0)
        kinds = [item["kind"] for item in manifest["fragments"]]
        self.assertEqual(kinds, ["base_rules", "memory", "evidence"])
        self.assertNotIn("remember this", repr(manifest))
        self.assertNotIn("3.3V", repr(manifest))


class CompactionEventTests(unittest.TestCase):
    def _state(self, *, cutoff: int, summary: str) -> dict:
        return {
            "_summarization_event": {
                "cutoff_index": cutoff,
                "summary_message": HumanMessage(content=summary),
                "file_path": "/conversation_history/t1.md",
            }
        }

    def test_snapshot_is_stable_and_serializable(self):
        snapshot = summarization_snapshot(self._state(cutoff=10, summary="摘要"))
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["cutoff_index"], 10)
        self.assertEqual(snapshot["summary_sha256"], text_sha256("摘要"))
        self.assertEqual(snapshot["offload_path"], "/conversation_history/t1.md")

    def test_unchanged_snapshot_does_not_emit(self):
        previous = summarization_snapshot(self._state(cutoff=10, summary="摘要"))
        current = summarization_snapshot(self._state(cutoff=10, summary="摘要"))
        self.assertIsNone(
            build_compaction_event(
                previous=previous,
                current=current,
                messages=[HumanMessage(content="x")] * 12,
                window_tokens=32768,
                turn_id="t1",
            )
        )

    def test_changed_snapshot_emits_real_counts(self):
        current = summarization_snapshot(self._state(cutoff=8, summary="压缩后的摘要"))
        event = build_compaction_event(
            previous=None,
            current=current,
            messages=[HumanMessage(content="old")] * 8
            + [HumanMessage(content="recent")] * 4,
            window_tokens=32768,
            turn_id="turn-1",
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["trigger"], "auto")
        self.assertEqual(event["stage"], "summarization")
        self.assertEqual(event["cutoff_index"], 8)
        self.assertEqual(event["dropped_messages"], 8)
        self.assertEqual(event["retained_messages"], 4)
        self.assertGreater(event["before_tokens"], 0)
        self.assertGreater(event["after_tokens"], 0)
        self.assertTrue(event["window_id"].startswith("turn-1:"))
        self.assertNotIn("summary", event)
        self.assertNotIn("_summary_message", event)


if __name__ == "__main__":
    unittest.main()
