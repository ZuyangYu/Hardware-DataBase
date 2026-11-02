"""Low-cardinality Sandbox metrics and durable trace lineage tests."""

from __future__ import annotations

from unittest.mock import patch

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import format_span_id, format_trace_id

from src.sandbox.models import SandboxTaskSpec
from src.sandbox.task_store import SandboxTaskStore


def test_sandbox_metrics_include_bounded_skill_result_labels():
    import src.observability.metrics as metrics

    calls = []
    with patch.object(
        metrics,
        "counter",
        lambda name, **kwargs: calls.append((name, kwargs.get("attributes"))),
    ):
        metrics.record_sandbox_task(
            operation="collect",
            status="ok",
            result_status="needs_tool",
            skill_kind="executable",
        )

    attributes = next(item[1] for item in calls if item[0] == "hdb.sandbox.task_count")
    assert attributes["operation"] == "collect"
    assert attributes["outcome"] == "ok"
    assert attributes["result_status"] == "needs_tool"
    assert attributes["skill_kind"] == "executable"


def test_task_event_persists_skill_lineage_and_current_trace(tmp_path):
    store = SandboxTaskStore(str(tmp_path / "tasks.db"), validate_on_create=False)
    task = store.create(
        SandboxTaskSpec(
            idempotency_key="observability-lineage",
            owner_id="42",
            department_id="7",
            bundle_ref="bundle/diagram",
            bundle_digest="a" * 64,
            profile_ref="profile/dev-l1",
            policy_snapshot_hash="policy-1",
            required_capabilities=("sandbox.execute",),
            skill_ref="skill/diagram",
            skill_content_digest="b" * 64,
            runtime_contract_version="v1",
        )
    )
    tracer = TracerProvider().get_tracer("sandbox-test")
    with tracer.start_as_current_span("sandbox-call") as span:
        store.append_runtime_event(
            task.task_id,
            {
                "runtime_contract_version": "v1",
                "event_id": "evt-lineage",
                "seq": 1,
                "task_id": task.task_id,
                "attempt": 0,
                "step_id": "run",
                "event": "step_started",
            },
        )

    metadata = store.events(task.task_id)[-1]["metadata"]
    context = span.get_span_context()
    assert metadata["skill_content_digest"] == "b" * 64
    assert metadata["bundle_digest"] == "a" * 64
    assert metadata["runtime_contract_version"] == "v1"
    assert metadata["trace_id"] == format_trace_id(context.trace_id)
    assert metadata["span_id"] == format_span_id(context.span_id)
