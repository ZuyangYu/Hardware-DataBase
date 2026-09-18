"""v3 §6.1: slot-based chat refill and a process-lifetime executor."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from src.workers.main import HardwareWorker


class _TurnQueue:
    """Fake pending queue: listing simulates the claim made inside _run_turn."""

    def __init__(self, count: int):
        self.pending = [SimpleNamespace(id=f"turn-{index}") for index in range(count)]
        self.listed: list[str] = []

    def list_pending_turn_work(self, limit=8):
        batch = self.pending[:limit]
        del self.pending[:limit]
        self.listed.extend(turn.id for turn in batch)
        return [(turn, 1) for turn in batch]


class _StickyQueue:
    """Always returns the same candidate; the pass must still terminate."""

    def __init__(self):
        self.turn = SimpleNamespace(id="turn-0")

    def list_pending_turn_work(self, limit=8):
        return [(self.turn, 1)]


def _worker(queue, process):
    worker = object.__new__(HardwareWorker)
    worker.worker_id = "worker-test"
    worker.running = True
    worker.conversations = queue
    worker._process_turn = process
    return worker


def test_refills_a_free_slot_without_waiting_for_the_whole_batch(monkeypatch):
    from src.workers import main as worker_module

    monkeypatch.setattr(worker_module.src.settings, "WORKER_TURN_CONCURRENCY", 2)
    lock = threading.Lock()
    active = 0
    peak = 0
    processed: list[str] = []

    def process(turn, _user_id):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            processed.append(turn.id)
            active -= 1

    worker = _worker(_TurnQueue(5), process)
    assert worker._process_chat_claims() is True
    assert sorted(processed) == [f"turn-{index}" for index in range(5)]
    assert peak == 2
    pool = worker._chat_pool
    assert pool is not None and not pool._shutdown


def test_executor_is_reused_across_passes(monkeypatch):
    from src.workers import main as worker_module

    monkeypatch.setattr(worker_module.src.settings, "WORKER_TURN_CONCURRENCY", 2)
    worker = _worker(_TurnQueue(2), lambda turn, user: None)
    assert worker._process_chat_claims() is True
    pool = worker._chat_pool
    worker.conversations = _TurnQueue(2)
    assert worker._process_chat_claims() is True
    assert worker._chat_pool is pool
    assert not pool._shutdown


def test_single_concurrency_keeps_sequential_path(monkeypatch):
    from src.workers import main as worker_module

    monkeypatch.setattr(worker_module.src.settings, "WORKER_TURN_CONCURRENCY", 1)
    seen: list[str] = []
    worker = _worker(_TurnQueue(3), lambda turn, user: seen.append(turn.id))
    assert worker._process_chat_claims() is True
    assert seen == ["turn-0", "turn-1", "turn-2"]
    assert getattr(worker, "_chat_pool", None) is None


def test_duplicate_candidates_are_not_resubmitted(monkeypatch):
    from src.workers import main as worker_module

    monkeypatch.setattr(worker_module.src.settings, "WORKER_TURN_CONCURRENCY", 2)
    processed: list[str] = []
    worker = _worker(_StickyQueue(), lambda turn, user: processed.append(turn.id))
    assert worker._process_chat_claims() is True
    assert processed == ["turn-0"]


def test_stop_shuts_down_the_executor(monkeypatch):
    from src.workers import main as worker_module

    monkeypatch.setattr(worker_module.src.settings, "WORKER_TURN_CONCURRENCY", 2)
    worker = _worker(_TurnQueue(2), lambda turn, user: None)
    assert worker._process_chat_claims() is True
    pool = worker._chat_pool
    worker.stop()
    assert worker.running is False
    assert pool._shutdown is True
