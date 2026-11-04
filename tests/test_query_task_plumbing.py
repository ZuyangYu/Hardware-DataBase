"""The worker must forward the durable task id into the runner."""

from __future__ import annotations

from src.api.routes.query import _query_generator


class _FakePipeline:
    def __init__(self) -> None:
        self.task_id = None

    def query(self, query, kb_name, history, ctx, *, task_id="", **kwargs):
        self.task_id = task_id
        yield "ok"


def test_query_generator_forwards_task_id():
    pipeline = _FakePipeline()
    result = list(
        _query_generator(
            pipeline,
            "q",
            "AAA",
            [],
            None,
            "thread-1",
            None,
            task_id="task-123",
        )
    )
    assert result == ["ok"]
    assert pipeline.task_id == "task-123"


def test_query_generator_without_task_id():
    pipeline = _FakePipeline()
    list(
        _query_generator(
            pipeline,
            "q",
            "AAA",
            [],
            None,
            "thread-1",
            None,
        )
    )
    assert pipeline.task_id == ""
