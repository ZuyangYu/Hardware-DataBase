"""Tests for the model gateway seam: usage ledger, adapters, construction."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import src.settings as settings
from src.core.llm_client import LLMClient, LLMClientConfig
from src.core.llm_governor import PRIORITY_BATCH, PRIORITY_INTERACTIVE
from src.core.model_gateway import (
    _GovernedChatOpenAI,
    _UsageLedgerCallback,
    build_chat_model,
    get_usage_ledger,
    reset_usage_ledger,
)


class UsageLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_usage_ledger()

    def tearDown(self) -> None:
        reset_usage_ledger()

    def test_record_and_snapshot(self) -> None:
        ledger = get_usage_ledger()
        ledger.record(channel="interactive", model="m1", prompt_tokens=10, completion_tokens=5)
        ledger.record(channel="interactive", model="m1", prompt_tokens=1, completion_tokens=1, total_tokens=7)
        ledger.record(channel="batch", model="m2", prompt_tokens=100, completion_tokens=50, usage_returned=False)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["calls"], 3)
        self.assertEqual(snapshot["prompt_tokens"], 111)
        self.assertEqual(snapshot["completion_tokens"], 56)
        self.assertEqual(snapshot["total_tokens"], 172)
        self.assertEqual(snapshot["by_channel"]["interactive"]["calls"], 2)
        self.assertEqual(snapshot["by_channel"]["batch"]["total_tokens"], 150)
        self.assertEqual(snapshot["by_model"]["m2"]["calls"], 1)

    def test_callback_records_llm_result_usage(self) -> None:
        callback = _UsageLedgerCallback(channel="batch", model="wiki-model")
        result = SimpleNamespace(
            llm_output={"token_usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50}},
            generations=[],
        )
        callback.on_llm_end(result)
        snapshot = get_usage_ledger().snapshot()
        self.assertEqual(snapshot["total_tokens"], 50)
        self.assertEqual(snapshot["by_channel"]["batch"]["calls"], 1)

    def test_llm_client_reports_to_ledger(self) -> None:
        client = LLMClient(
            LLMClientConfig(provider=settings.Provider.CUSTOM, base_url="http://127.0.0.1:1/v1", model="authoring"),
            priority=PRIORITY_BATCH,
        )
        client._record_usage(client.config, "summary", {"prompt_tokens": 5, "completion_tokens": 3})
        snapshot = get_usage_ledger().snapshot()
        self.assertEqual(snapshot["by_channel"]["batch"]["total_tokens"], 8)
        self.assertEqual(snapshot["by_model"]["authoring"]["calls"], 1)


class BuildChatModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original = (
            settings.AGENT_LLM_PROVIDER,
            settings.AGENT_CUSTOM_BASE_URL,
            settings.AGENT_CUSTOM_API_KEY,
            settings.AGENT_CUSTOM_MODEL,
            settings.AGENT_MODEL_MAX_INPUT_TOKENS,
        )
        settings.AGENT_LLM_PROVIDER = "custom"
        settings.AGENT_CUSTOM_BASE_URL = "http://127.0.0.1:1/v1"
        settings.AGENT_CUSTOM_API_KEY = "test-key"
        settings.AGENT_CUSTOM_MODEL = "test-model"

    def tearDown(self) -> None:
        (
            settings.AGENT_LLM_PROVIDER,
            settings.AGENT_CUSTOM_BASE_URL,
            settings.AGENT_CUSTOM_API_KEY,
            settings.AGENT_CUSTOM_MODEL,
            settings.AGENT_MODEL_MAX_INPUT_TOKENS,
        ) = self._original

    def test_build_returns_explicit_governed_adapter(self) -> None:
        model = build_chat_model(provider="custom", model="test-model", priority=PRIORITY_BATCH)
        self.assertIsInstance(model, _GovernedChatOpenAI)
        self.assertEqual(model.hdb_priority, PRIORITY_BATCH)
        self.assertEqual(model.model_name, "test-model")
        self.assertIsInstance(model.callbacks[0], _UsageLedgerCallback)

    def test_interactive_priority_default(self) -> None:
        model = build_chat_model(provider="custom", model="test-model")
        self.assertEqual(model.hdb_priority, PRIORITY_INTERACTIVE)


if __name__ == "__main__":
    unittest.main()
