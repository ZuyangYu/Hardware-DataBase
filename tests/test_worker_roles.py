"""Worker role routing (chat / parse / all)."""

from __future__ import annotations

import unittest

import src.settings
from src.workers.main import VALID_WORKER_ROLES, HardwareWorker, resolve_worker_role


class _FakeRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def run_once(self) -> bool:
        self.calls += 1
        return False


class _FakeConversations:
    def __init__(self) -> None:
        self.requeued = 0
        self.listed = 0
        self.queue_state = 0

    def requeue_stale_turns(self) -> int:
        self.requeued += 1
        return 0

    def expire_queued_turns(self) -> int:
        return 0

    def pending_turn_queue_state(self) -> tuple[int, float]:
        self.queue_state += 1
        return (0, 0.0)

    def list_pending_turn_work(self, limit: int = 8):
        self.listed += 1
        return []


class WorkerRoleRoutingTests(unittest.TestCase):
    def setUp(self):
        self._original_attachments = src.settings.CHAT_ATTACHMENTS_ENABLED
        self._original_role = getattr(src.settings, "WORKER_ROLE", "all")
        src.settings.CHAT_ATTACHMENTS_ENABLED = False
        self.addCleanup(setattr, src.settings, "CHAT_ATTACHMENTS_ENABLED", self._original_attachments)
        self.addCleanup(setattr, src.settings, "WORKER_ROLE", self._original_role)

    def _worker(self, role: str) -> HardwareWorker:
        worker = object.__new__(HardwareWorker)
        worker.role = role
        worker.running = True
        worker.worker_id = "worker-test"
        worker._env_mtime_ns = HardwareWorker._settings_mtime_ns()
        worker.conversations = _FakeConversations()
        worker.runtime = _FakeRuntime()
        worker.auth = None
        worker.pipeline = None
        return worker

    def test_resolve_role_normalizes_unknown_values(self):
        self.assertEqual(resolve_worker_role("CHAT"), "chat")
        self.assertEqual(resolve_worker_role("bogus"), "all")
        self.assertEqual(resolve_worker_role(""), "all")
        src.settings.WORKER_ROLE = "parse"
        self.assertEqual(resolve_worker_role(), "parse")
        self.assertEqual(set(VALID_WORKER_ROLES), {"all", "chat", "parse"})

    def test_chat_role_only_consumes_chat_work(self):
        worker = self._worker("chat")
        self.assertFalse(worker.run_once())
        self.assertEqual(worker.conversations.requeued, 1)
        self.assertEqual(worker.conversations.listed, 1)
        self.assertEqual(worker.runtime.calls, 0)

    def test_parse_role_only_consumes_parse_work(self):
        worker = self._worker("parse")
        self.assertFalse(worker.run_once())
        self.assertEqual(worker.conversations.requeued, 0)
        self.assertEqual(worker.runtime.calls, 1)

    def test_all_role_consumes_everything(self):
        worker = self._worker("all")
        self.assertFalse(worker.run_once())
        self.assertEqual(worker.conversations.requeued, 1)
        self.assertEqual(worker.runtime.calls, 1)

    def test_instance_without_role_attribute_behaves_like_all(self):
        worker = self._worker("all")
        del worker.role
        self.assertTrue(worker._role_enabled("chat", "parse"))


if __name__ == "__main__":
    unittest.main()
