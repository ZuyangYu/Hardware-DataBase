"""The worker must forward the durable task id into the runner."""

from __future__ import annotations

from types import SimpleNamespace

from src.api.routes.query import _query_generator, _turn_permit_resource


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


def test_task_turn_uses_dedicated_permit_resource():
    assert _turn_permit_resource(SimpleNamespace(trace_context={"task_id": "t1"})) == "task"
    assert _turn_permit_resource(SimpleNamespace(trace_context={})) == "chat"
    assert _turn_permit_resource(SimpleNamespace()) == "chat"
    # Chat-origin turns share the session task but stay interactive.
    assert (
        _turn_permit_resource(
            SimpleNamespace(trace_context={"task_id": "t1", "task_origin": "chat"})
        )
        == "chat"
    )
    assert (
        _turn_permit_resource(
            SimpleNamespace(trace_context={"task_id": "t1", "task_origin": "task"})
        )
        == "task"
    )
