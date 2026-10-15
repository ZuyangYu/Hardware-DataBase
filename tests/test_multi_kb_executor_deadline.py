"""Separate queue admission deadlines from actual multi-KB execution deadlines."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.agents.tools.retrieval_executor import RetrievalExecutor, RetrievalSaturated


def test_execution_deadline_discards_late_running_work_and_keeps_slot_leased():
    pool = RetrievalExecutor(max_workers=1, queue_max_depth=2)
    release = threading.Event()
    started = time.monotonic()
    try:
        result = pool.map_bounded(lambda _: release.wait(timeout=2) or "late", [1, 2], execution_deadline_at=time.monotonic() + 0.05)
        assert time.monotonic() - started < 0.5
        assert [item.status for item in result] == ["timeout", "timeout"]
        assert [item.value for item in result] == [None, None]
        assert pool.stats()["pending"] == 1
    finally:
        release.set()
        assert pool.close(deadline_seconds=1) == 0


def test_concurrent_admission_never_exceeds_global_queue_depth():
    pool = RetrievalExecutor(max_workers=1, queue_max_depth=1)
    barrier = threading.Barrier(8)
    release = threading.Event()

    def request(_):
        barrier.wait(timeout=1)
        return pool.map_bounded(lambda _: release.wait(timeout=2), [1], execution_deadline_at=time.monotonic() + 0.1)

    try:
        with ThreadPoolExecutor(max_workers=8) as callers:
            results = list(callers.map(request, range(8)))
        assert pool.stats()["high_water"] == 1
        assert all(batch[0].value is None for batch in results)
    finally:
        release.set()
        pool.close(deadline_seconds=1)


def test_direct_submission_also_respects_global_queue_depth():
    pool = RetrievalExecutor(max_workers=1, queue_max_depth=1)
    release = threading.Event()
    try:
        pool.submit(lambda: release.wait(timeout=2))
        with pytest.raises(RetrievalSaturated):
            pool.submit(lambda: "must not be queued")
    finally:
        release.set()
        pool.close(deadline_seconds=1)


def test_nested_map_in_same_pool_is_rejected_without_deadlock():
    pool = RetrievalExecutor(max_workers=1)
    try:
        result = pool.map_bounded(lambda _: pool.map_bounded(lambda x: x, [1]), [0], execution_deadline_at=time.monotonic() + 0.2)
        assert result[0].status == "failed"
        assert "nested_retrieval_wait" in str(result[0].error)
    finally:
        pool.close(deadline_seconds=1)
