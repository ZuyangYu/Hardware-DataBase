"""Tests for the process-wide LLM governor (concurrency, priority, timeout)."""
from __future__ import annotations

import threading
import time
import unittest

import src.settings as settings
from src.core.llm_governor import (
    LLMQueueTimeoutError,
    PRIORITY_BATCH,
    PRIORITY_INTERACTIVE,
    get_llm_governor,
    reset_llm_governor,
)


class _SettingsGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._old = {
            "LLM_MAX_CONCURRENCY": settings.LLM_MAX_CONCURRENCY,
            "LLM_BATCH_MAX_CONCURRENCY": settings.LLM_BATCH_MAX_CONCURRENCY,
            "LLM_QUEUE_TIMEOUT_SECONDS": settings.LLM_QUEUE_TIMEOUT_SECONDS,
            "LLM_QUEUE_WARN_SECONDS": settings.LLM_QUEUE_WARN_SECONDS,
        }
        settings.LLM_QUEUE_TIMEOUT_SECONDS = 5
        settings.LLM_QUEUE_WARN_SECONDS = 0
        reset_llm_governor()

    def tearDown(self) -> None:
        for key, value in self._old.items():
            setattr(settings, key, value)
        reset_llm_governor()


class LLMGovernorTests(_SettingsGuard):
    def test_global_cap_admits_after_release(self) -> None:
        settings.LLM_MAX_CONCURRENCY = 1
        settings.LLM_BATCH_MAX_CONCURRENCY = 0
        governor = get_llm_governor()
        release_first = threading.Event()
        second_entered = threading.Event()

        def first() -> None:
            with governor.slot(PRIORITY_INTERACTIVE):
                release_first.wait(3)

        def second() -> None:
            with governor.slot(PRIORITY_INTERACTIVE):
                second_entered.set()

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=second)
        t1.start()
        time.sleep(0.05)
        t2.start()
        time.sleep(0.1)
        self.assertFalse(second_entered.is_set())
        self.assertEqual(governor.snapshot()["queued"], 1)
        release_first.set()
        t1.join(3)
        t2.join(3)
        self.assertTrue(second_entered.is_set())
        self.assertEqual(governor.snapshot()["active"], 0)

    def test_batch_is_capped_and_interactive_keeps_reserved_capacity(self) -> None:
        settings.LLM_MAX_CONCURRENCY = 2
        settings.LLM_BATCH_MAX_CONCURRENCY = 1
        governor = get_llm_governor()
        batch_holds = threading.Event()
        release_batch = threading.Event()
        interactive_entered = threading.Event()
        second_batch_entered = threading.Event()

        def batch_one() -> None:
            with governor.slot(PRIORITY_BATCH):
                batch_holds.set()
                release_batch.wait(3)

        def batch_two() -> None:
            with governor.slot(PRIORITY_BATCH):
                second_batch_entered.set()

        def interactive() -> None:
            with governor.slot(PRIORITY_INTERACTIVE):
                interactive_entered.set()

        threads = [
            threading.Thread(target=batch_one),
            threading.Thread(target=batch_two),
            threading.Thread(target=interactive),
        ]
        threads[0].start()
        self.assertTrue(batch_holds.wait(2))
        threads[1].start()
        threads[2].start()
        time.sleep(0.15)
        # 批量第二个必须排队, 交互请求可从预留容量进入
        self.assertFalse(second_batch_entered.is_set())
        self.assertTrue(interactive_entered.is_set())
        release_batch.set()
        for t in threads:
            t.join(3)
        self.assertTrue(second_batch_entered.is_set())

    def test_queue_timeout_raises_and_releases(self) -> None:
        settings.LLM_MAX_CONCURRENCY = 1
        settings.LLM_BATCH_MAX_CONCURRENCY = 0
        governor = get_llm_governor()
        hold = threading.Event()
        holder_ready = threading.Event()

        def holder() -> None:
            with governor.slot(PRIORITY_INTERACTIVE):
                holder_ready.set()
                hold.wait(5)

        t = threading.Thread(target=holder)
        t.start()
        self.assertTrue(holder_ready.wait(2))
        settings.LLM_QUEUE_TIMEOUT_SECONDS = 0.2
        started = time.monotonic()
        with self.assertRaises(LLMQueueTimeoutError):
            with governor.slot(PRIORITY_INTERACTIVE):
                pass
        self.assertLess(time.monotonic() - started, 3)
        hold.set()
        t.join(3)
        snapshot = governor.snapshot()
        self.assertEqual(snapshot["timeouts"], 1)
        self.assertEqual(snapshot["active"], 0)
        self.assertEqual(snapshot["completed"], 1)

    def test_slot_released_on_exception(self) -> None:
        settings.LLM_MAX_CONCURRENCY = 1
        settings.LLM_BATCH_MAX_CONCURRENCY = 0
        governor = get_llm_governor()
        with self.assertRaises(RuntimeError):
            with governor.slot(PRIORITY_INTERACTIVE):
                raise RuntimeError("boom")
        self.assertEqual(governor.snapshot()["active"], 0)
        with governor.slot(PRIORITY_INTERACTIVE):
            self.assertEqual(governor.snapshot()["active"], 1)


class GovernedModelTests(_SettingsGuard):
    def test_governed_adapter_holds_slot_around_generate_and_stream(self) -> None:
        from unittest import mock

        from langchain_openai import ChatOpenAI

        from src.core.model_gateway import build_governed_model

        settings.LLM_MAX_CONCURRENCY = 2
        settings.LLM_BATCH_MAX_CONCURRENCY = 1
        governor = get_llm_governor()
        seen: list[int] = []

        def fake_generate(self, *args, **kwargs):  # noqa: ANN001
            seen.append(governor.snapshot()["active"])
            return "generated"

        def fake_stream(self, *args, **kwargs):  # noqa: ANN001
            seen.append(governor.snapshot()["active"])
            yield "a"
            seen.append(governor.snapshot()["active"])
            yield "b"

        model = build_governed_model(
            provider="custom",
            model="fake",
            base_url="http://127.0.0.1:1/v1",
            api_key="test-key",
            priority=PRIORITY_BATCH,
        )
        with mock.patch.object(ChatOpenAI, "_generate", fake_generate), \
                mock.patch.object(ChatOpenAI, "_stream", fake_stream):
            self.assertEqual(model._generate([]), "generated")
            self.assertEqual(list(model._stream([])), ["a", "b"])
        self.assertEqual(seen, [1, 1, 1])
        self.assertEqual(governor.snapshot()["active"], 0)


if __name__ == "__main__":
    unittest.main()
