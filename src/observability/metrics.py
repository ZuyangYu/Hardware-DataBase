"""Low-cardinality OpenTelemetry metrics used by API, workers and evaluations."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from opentelemetry import metrics as otel_metrics
from opentelemetry.metrics import Observation as GaugeObservation

from .semantic import METRIC_LABEL_KEYS


_COUNTERS: dict[str, Any] = {}
_HISTOGRAMS: dict[str, Any] = {}
_QUEUE_STATE: dict[str, dict[str, float]] = {}
_QUEUE_GAUGES: dict[str, Any] = {}
_MEMORY_INDEX_STATE: dict[str, dict[str, int | str]] = {}
_MEMORY_PROJECTION_STATE: dict[str, float] = {"pending": 0.0, "lag_seconds": 0.0}
_MEMORY_GAUGES: dict[str, Any] = {}

_EXPORT_FORMATS = frozenset({"md", "xlsx", "docx", "pdf", "pptx"})
_EXPORT_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "cancelled", "dead_letter"})
_EXPORT_QUEUES = frozenset({"result-export", "result_exports", "export"})


def _enabled() -> bool:
    try:
        import src.settings as settings

        return bool(settings.OBS_ENABLED and settings.OBS_METRICS_ENABLED)
    except Exception:
        return False


def metric_attributes(attributes: Mapping[str, Any] | None) -> dict[str, str]:
    """Drop high-cardinality labels before they can reach Prometheus."""

    result: dict[str, str] = {}
    allowed_keys = METRIC_LABEL_KEYS | {"format"}
    for key, value in (attributes or {}).items():
        if key not in allowed_keys or value is None:
            continue
        result[str(key)] = str(value)[:80]
    return result


def _meter():
    return otel_metrics.get_meter("hardware_database")


def _counter(name: str, description: str):
    instrument = _COUNTERS.get(name)
    if instrument is None:
        instrument = _meter().create_counter(name, description=description)
        _COUNTERS[name] = instrument
    return instrument


def _histogram(name: str, description: str, unit: str = "s"):
    instrument = _HISTOGRAMS.get(name)
    if instrument is None:
        instrument = _meter().create_histogram(name, description=description, unit=unit)
        _HISTOGRAMS[name] = instrument
    return instrument


def counter(name: str, *, attributes: Mapping[str, Any] | None = None, value: int = 1, description: str = "") -> None:
    if not _enabled():
        return
    try:
        _counter(name, description or name).add(value, metric_attributes(attributes))
    except Exception:
        pass


def histogram(
    name: str,
    value: float,
    *,
    attributes: Mapping[str, Any] | None = None,
    unit: str = "s",
    description: str = "",
) -> None:
    if not _enabled():
        return
    try:
        _histogram(name, description or name, unit).record(float(value), metric_attributes(attributes))
    except Exception:
        pass


def set_queue_state(queue: str, *, depth: int, oldest_age_s: float) -> None:
    if not _enabled():
        return
    _QUEUE_STATE[queue] = {"depth": max(0, int(depth)), "oldest_age_s": max(0.0, float(oldest_age_s))}
    try:
        if "depth" not in _QUEUE_GAUGES:
            _QUEUE_GAUGES["depth"] = _meter().create_observable_gauge(
                "hdb.queue.depth",
                callbacks=[lambda _options: [
                    GaugeObservation(value["depth"], {"queue": key})
                    for key, value in _QUEUE_STATE.items()
                ]],
                description="Current durable queue depth",
                unit="{tasks}",
            )
        if "oldest_age" not in _QUEUE_GAUGES:
            _QUEUE_GAUGES["oldest_age"] = _meter().create_observable_gauge(
                "hdb.queue.oldest_age",
                callbacks=[lambda _options: [
                    GaugeObservation(value["oldest_age_s"], {"queue": key})
                    for key, value in _QUEUE_STATE.items()
                ]],
                description="Age of the oldest durable queue item",
                unit="s",
            )
    except Exception:
        pass


_RESOURCE_STATE: dict[str, dict[str, float]] = {}
_RESOURCE_GAUGES: dict[str, Any] = {}


def set_resource_state(
    *,
    active: Mapping[str, Any] | None = None,
    waiting: Mapping[str, Any] | None = None,
    capacities: Mapping[str, Any] | None = None,
    reserved: Mapping[str, Any] | None = None,
    uncertain: Mapping[str, Any] | None = None,
) -> None:
    """Publish resource governor state (P0b) as low-cardinality gauges."""
    if not _enabled():
        return
    _RESOURCE_STATE.clear()
    active = active or {}
    waiting = waiting or {}
    capacities = capacities or {}
    reserved = reserved or {}
    uncertain = uncertain or {}
    for key in set(active) | set(waiting) | set(capacities) | set(reserved) | set(uncertain):
        _RESOURCE_STATE[str(key)] = {
            "active": float(active.get(key, 0) or 0),
            "waiting": float(waiting.get(key, 0) or 0),
            "capacity": float(capacities.get(key, 0) or 0),
            "reserved": float(reserved.get(key, 0) or 0),
            "uncertain": float(uncertain.get(key, 0) or 0),
        }
    specs = {
        "active": ("hdb.resource.active", "Currently held resource permits", "{calls}"),
        "waiting": ("hdb.resource.queue.waiting", "Callers queued for a resource permit", "{calls}"),
        "capacity": ("hdb.resource.capacity", "Configured resource capacity", "{slots}"),
        "reserved": ("hdb.resource.reserved", "Interactive-reserved resource slots", "{slots}"),
        "uncertain": ("hdb.resource.uncertain", "Permits in the uncertain state", "{calls}"),
    }
    try:
        for field, (name, description, unit) in specs.items():
            if field in _RESOURCE_GAUGES:
                continue
            _RESOURCE_GAUGES[field] = _meter().create_observable_gauge(
                name,
                callbacks=[
                    lambda _options, field=field: [
                        GaugeObservation(value[field], {"resource": resource})
                        for resource, value in _RESOURCE_STATE.items()
                    ]
                ],
                description=description,
                unit=unit,
            )
        if "chat_reserved" not in _RESOURCE_GAUGES:
            _RESOURCE_GAUGES["chat_reserved"] = _meter().create_observable_gauge(
                "hdb.chat.reserved_capacity",
                callbacks=[
                    lambda _options: [
                        GaugeObservation(_RESOURCE_STATE.get("chat", {}).get("reserved", 0.0))
                    ]
                ],
                description="Interactive slots reserved for chat",
                unit="{slots}",
            )
    except Exception:
        pass


def set_memory_index_health(*, backend: str, healthy: bool, semantic_index: bool) -> None:
    """Publish the latest rebuildable-memory Store health as an OTel gauge."""

    if not _enabled():
        return
    key = f"{str(backend)}:{str(bool(semantic_index)).lower()}"
    _MEMORY_INDEX_STATE[key] = {
        "backend": str(backend),
        "semantic_index": str(bool(semantic_index)).lower(),
        "healthy": 1 if healthy else 0,
    }
    try:
        if "index_health" not in _MEMORY_GAUGES:
            _MEMORY_GAUGES["index_health"] = _meter().create_observable_gauge(
                "hdb.memory.index_health",
                callbacks=[lambda _options: [
                    GaugeObservation(
                        value["healthy"],
                        {"backend": value["backend"], "semantic_index": value["semantic_index"]},
                    )
                    for value in _MEMORY_INDEX_STATE.values()
                ]],
                description="Health of the configured long-term memory projection store",
                unit="1",
            )
    except Exception:
        pass


def set_memory_projection_state(*, pending: int, oldest_age_s: float) -> None:
    """Publish pending projection/deletion outbox depth and oldest-item lag."""

    if not _enabled():
        return
    _MEMORY_PROJECTION_STATE["pending"] = max(0.0, float(pending))
    _MEMORY_PROJECTION_STATE["lag_seconds"] = max(0.0, float(oldest_age_s))
    try:
        if "projection_pending" not in _MEMORY_GAUGES:
            _MEMORY_GAUGES["projection_pending"] = _meter().create_observable_gauge(
                "hdb.memory.projection_outbox_pending",
                callbacks=[lambda _options: [
                    GaugeObservation(_MEMORY_PROJECTION_STATE["pending"], {"queue": "memory_projection"})
                ]],
                description="Pending memory projection and deletion outbox entries",
                unit="{tasks}",
            )
        if "projection_lag" not in _MEMORY_GAUGES:
            _MEMORY_GAUGES["projection_lag"] = _meter().create_observable_gauge(
                "hdb.memory.projection_lag_seconds",
                callbacks=[lambda _options: [
                    GaugeObservation(_MEMORY_PROJECTION_STATE["lag_seconds"], {"queue": "memory_projection"})
                ]],
                description="Age of the oldest pending memory projection/deletion outbox entry",
                unit="s",
            )
    except Exception:
        pass


def record_chat_turn(*, status: str, mode: str, duration_s: float, queue_s: float | None, ttft_s: float | None) -> None:
    attrs = {"status": status, "mode": mode}
    counter("hdb.chat.turns", attributes=attrs, description="Chat turns")
    histogram("hdb.chat.turn.duration", duration_s, attributes={"mode": mode}, description="Chat turn duration")
    if queue_s is not None:
        histogram("hdb.chat.queue.wait", queue_s, attributes={"mode": mode}, description="Chat queue wait")
    if ttft_s is not None:
        histogram("hdb.chat.ttft", ttft_s, attributes={"mode": mode}, description="Time to first token")


def record_agent(*, status: str, mode: str, duration_s: float, retrieval_rounds: int | None = None) -> None:
    counter("hdb.agent.runs", attributes={"status": status, "mode": mode}, description="Agent runs")
    histogram("hdb.agent.duration", duration_s, attributes={"mode": mode}, description="Agent duration")
    if retrieval_rounds is not None:
        histogram("hdb.agent.retrieval_rounds", retrieval_rounds, attributes={"mode": mode}, unit="{rounds}")


def record_agent_stage(*, stage: str, duration_s: float, status: str = "success") -> None:
    normalized_stage = str(stage or "unknown").strip()[:80] or "unknown"
    normalized_status = str(status or "success").strip()[:40] or "success"
    attrs = {"stage": normalized_stage, "status": normalized_status}
    counter("hdb.agent.stage.completed", attributes=attrs, description="Agent pipeline stages")
    histogram(
        "hdb.agent.stage.duration",
        duration_s,
        attributes={"stage": normalized_stage, "status": normalized_status},
        description="Agent pipeline stage duration",
    )


def record_llm(*, provider: str, status: str, duration_s: float, streaming: bool, ttft_s: float | None = None) -> None:
    attrs = {"provider": provider, "status": status, "streaming": str(bool(streaming)).lower()}
    counter("hdb.llm.calls", attributes=attrs, description="LLM calls")
    histogram("hdb.llm.duration", duration_s, attributes={"provider": provider, "status": status})
    if ttft_s is not None:
        histogram("hdb.llm.ttft", ttft_s, attributes={"provider": provider})


def record_worker(*, status: str, duration_s: float | None = None) -> None:
    counter("hdb.worker.task.processed", attributes={"status": status}, description="Worker tasks")
    if status == "failed":
        counter("hdb.worker.task.failed", description="Failed worker tasks")
    if duration_s is not None:
        histogram("hdb.worker.task.duration", duration_s, attributes={"status": status})


_ATTACHMENT_EVENT_LABELS = frozenset(
    {
        "upload",
        "asset_reuse",
        "parse",
        "search",
        "read",
        "waiting_turn",
        "degraded",
        "cleanup",
        "cleanup_retry",
        "visual",
        "visual_failure",
        "dense",
        "ocr",
    }
)


def record_attachment(
    event: str,
    *,
    status: str = "ok",
    duration_s: float | None = None,
    scope: str = "",
) -> None:
    """Domain metrics for chat attachments (design §20).

    Labels are low-cardinality (event/status/scope) — ids, filenames and
    hashes never enter metric attributes.
    """
    event = event if event in _ATTACHMENT_EVENT_LABELS else "other"
    attrs = {"event": event, "status": status}
    if scope:
        attrs["scope"] = scope
    counter("hdb.attachment.events", attributes=attrs, description="Chat attachment events")
    if duration_s is not None:
        histogram("hdb.attachment.duration", duration_s, attributes={"event": event, "status": status})


_ACTIVE_STATE: dict[str, int] = {}
_ACTIVE_GAUGES: dict[str, Any] = {}


def set_active_state(name: str, description: str, count: int, *, unit: str = "{calls}") -> None:
    """Publish one bounded concurrent-activity gauge (agent/llm/rag)."""
    if not _enabled():
        return
    key = str(name)
    _ACTIVE_STATE[key] = max(0, int(count))
    try:
        if key not in _ACTIVE_GAUGES:
            _ACTIVE_GAUGES[key] = _meter().create_observable_gauge(
                key,
                callbacks=[lambda _options, key=key: [GaugeObservation(_ACTIVE_STATE.get(key, 0))]],
                description=description,
                unit=unit,
            )
    except Exception:
        pass


_ACTIVE_COUNTS: dict[str, int] = {}
_ACTIVE_COUNTS_LOCK = threading.Lock()


def incr_active(name: str, description: str, delta: int, *, unit: str = "{calls}") -> int:
    """Adjust one internal active-count and publish its gauge."""
    key = str(name)
    with _ACTIVE_COUNTS_LOCK:
        value = max(0, _ACTIVE_COUNTS.get(key, 0) + int(delta))
        _ACTIVE_COUNTS[key] = value
    set_active_state(key, description, value, unit=unit)
    return value


def record_conversation_claim(*, status: str, wait_s: float) -> None:
    counter(
        "hdb.conversation.claims",
        attributes={"status": status},
        description="Session turn claim outcomes",
    )
    histogram(
        "hdb.conversation.lock.wait",
        max(0.0, float(wait_s)),
        attributes={"status": status},
        description="Time spent acquiring the per-session claim lock",
    )


def record_same_thread_conflict() -> None:
    counter(
        "hdb.conversation.concurrent_claim_blocked",
        description="Same-session claims blocked while another turn held the lease",
    )


def record_governor_lease_lost(*, resource: str, reason: str) -> None:
    counter(
        "hdb.governor.lease_lost",
        attributes={"resource": resource, "reason": reason},
        description="Execution leases lost or rejected by ownership checks",
    )


def record_agent_step(*, step_count: int, status: str) -> None:
    histogram(
        "hdb.agent.step_count",
        max(0, int(step_count)),
        attributes={"status": status},
        unit="{steps}",
        description="Agent steps per run",
    )


def record_agent_cancel(*, reason: str) -> None:
    counter(
        "hdb.agent.cancel_count",
        attributes={"reason": reason},
        description="Agent runs cancelled",
    )


def record_agent_tool_call(*, tool: str, status: str) -> None:
    counter(
        "hdb.agent.tool_call_count",
        attributes={"tool": tool, "status": status},
        description="Agent tool calls",
    )


def record_llm_timeout(*, provider: str) -> None:
    counter(
        "hdb.llm.timeout_total",
        attributes={"provider": provider},
        description="LLM calls that timed out",
    )


def record_llm_tokens(*, provider: str, input_tokens: int, output_tokens: int) -> None:
    if input_tokens:
        counter(
            "hdb.llm.tokens",
            attributes={"provider": provider, "kind": "input"},
            value=max(0, int(input_tokens)),
            description="LLM tokens consumed (enables per-minute rates)",
        )
    if output_tokens:
        counter(
            "hdb.llm.tokens",
            attributes={"provider": provider, "kind": "output"},
            value=max(0, int(output_tokens)),
            description="LLM tokens consumed (enables per-minute rates)",
        )


def record_rag_batch(*, fanout: int) -> None:
    histogram(
        "hdb.rag.batch_fanout",
        max(0, int(fanout)),
        unit="{queries}",
        description="Queries per document batch retrieval call",
    )


def record_rag_zero_hit() -> None:
    counter("hdb.rag.zero_hit", description="RAG retrieval calls that returned no chunks")


def record_job_phase(*, kind: str, phase: str, seconds: float) -> None:
    name = "hdb.job.wait_time" if phase == "wait" else "hdb.job.execution_time"
    histogram(
        name,
        max(0.0, float(seconds)),
        attributes={"kind": kind},
        description=f"{kind} job {phase} duration",
    )


def record_job_dead_letter(*, kind: str) -> None:
    counter(
        "hdb.job.dead_letter_count",
        attributes={"kind": kind},
        description="Jobs that exhausted retries",
    )


def record_action_parallel(*, layer_size: int, duration_s: float, status: str = "ok") -> None:
    """Record one bounded-parallel action layer (P2-001)."""
    counter(
        "hdb.action.parallel.layers",
        attributes={"status": status},
        description="Action DAG parallel layers executed",
    )
    histogram(
        "hdb.action.parallel.layer_size",
        max(0, int(layer_size)),
        unit="{actions}",
        description="Actions merged into one parallel layer",
    )
    histogram(
        "hdb.action.parallel.duration",
        max(0.0, float(duration_s)),
        attributes={"status": status},
        description="Parallel layer execution duration",
    )


def record_retrieval_executor(*, status: str, wait_s: float | None = None) -> None:
    """Record shared retrieval executor admission (P0-007/P2-002)."""
    counter(
        "hdb.retrieval.executor.attempts",
        attributes={"status": status},
        description="Shared retrieval executor admission outcomes",
    )
    if wait_s is not None:
        histogram(
            "hdb.retrieval.executor.queue_wait",
            max(0.0, float(wait_s)),
            attributes={"status": status},
            description="Time waiting for a bounded retrieval queue slot",
        )


def record_evaluation(*, status: str, mode: str) -> None:
    counter("hdb.eval.runs", attributes={"status": status, "mode": mode}, description="Evaluation runs")


def record_evaluation_sample(*, status: str, duration_s: float, mode: str) -> None:
    histogram("hdb.eval.sample.duration", duration_s, attributes={"mode": mode, "status": status})
    if status == "failed":
        counter("hdb.eval.sample.failed", attributes={"mode": mode}, description="Failed evaluation samples")


def record_evaluation_score(*, metric: str, score: float) -> None:
    counter("hdb.eval.score.observations", attributes={"metric": metric}, description="Evaluation score observations")
    histogram("hdb.eval.score", score, attributes={"metric": metric}, unit="1")


def record_authoring(*, status: str, duration_s: float | None = None) -> None:
    counter("hdb.authoring.runs", attributes={"status": status}, description="Document authoring runs")
    if duration_s is not None:
        histogram("hdb.authoring.run.duration", duration_s, attributes={"status": status})


def record_authoring_unit(*, operation: str, status: str, duration_s: float | None = None) -> None:
    counter("hdb.authoring.unit.outcome", attributes={"operation": operation, "status": status}, description="Authoring unit outcomes")
    if duration_s is not None:
        histogram("hdb.authoring.unit.duration", duration_s, attributes={"operation": operation, "status": status})


def record_document_compatibility(*, event_type: str, route: str) -> None:
    """Count compatibility-boundary events with bounded labels only."""

    known = {
        "legacy_direct_execution", "plan_backed_execution",
        "auto_confirmation_attempt", "new_write", "legacy_write",
    }
    kind = str(event_type or "other").strip().lower()
    if kind not in known:
        kind = "other"
    normalized_route = str(route or "system").strip().lower()
    if normalized_route not in {"legacy", "plan_backed", "conversation", "system"}:
        normalized_route = "system"
    counter(
        "hdb.document.authoring.compatibility.events",
        attributes={"kind": kind, "mode": normalized_route},
        description="Document authoring compatibility boundary events",
    )


def record_authoring_agent(*, status: str, mode: str = "external_agent", duration_s: float | None = None) -> None:
    """Record low-cardinality Agent executor metrics.

    Run, field, evidence and proposal identifiers intentionally never become
    metric labels; they belong on spans and the append-only execution events.
    """

    counter(
        "hdb.authoring.agent.runs",
        attributes={"status": status, "mode": mode},
        description="Document authoring agent runs",
    )
    if duration_s is not None:
        histogram(
            "hdb.authoring.agent.duration",
            duration_s,
            attributes={"mode": mode},
            description="Document authoring agent duration",
        )


def record_authoring_tool(*, tool: str, status: str, duration_s: float | None = None) -> None:
    """Record one bounded agent-tool outcome using only tool/status labels."""

    counter(
        "hdb.authoring.agent.tool.calls",
        attributes={"tool": tool, "status": status},
        description="Document authoring agent tool calls",
    )
    if duration_s is not None:
        histogram(
            "hdb.authoring.agent.tool.duration",
            duration_s,
            attributes={"tool": tool},
            description="Document authoring agent tool duration",
        )


_PLANNING_SHADOW_STATUSES = frozenset({"succeeded", "failed"})
_PLANNING_SHADOW_VALIDITIES = frozenset({"valid", "invalid"})


def record_planning_shadow(
    *,
    status: str,
    validity: str,
    blocking_issue_count: int = 0,
    blocking_issue_codes: list[str] | tuple[str, ...] | None = None,
    units: int = 0,
    tables: int = 0,
    duration_s: float | None = None,
) -> None:
    """Record shadow-planning quality without exposing document data.

    Issue *codes* are deliberately represented as a bounded count rather than
    metric labels: a schema or plugin must not be able to turn arbitrary
    identifiers into a high-cardinality telemetry dimension.  The codes are
    retained only in the sanitized planning event emitted by the coordinator.
    """
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in _PLANNING_SHADOW_STATUSES:
        normalized_status = "failed"
    normalized_validity = str(validity or "").strip().lower()
    if normalized_validity not in _PLANNING_SHADOW_VALIDITIES:
        normalized_validity = "invalid"
    attrs = {"status": normalized_status}
    counter("hdb.document.planning.shadow.runs", attributes=attrs, description="Document planning shadow runs")
    counter(
        "hdb.document.planning.shadow.blocking_issues",
        attributes=attrs,
        value=max(0, int(blocking_issue_count)),
        description="Blocking issues observed by document planning shadow mode",
    )
    counter(
        "hdb.document.planning.shadow.issue_codes",
        attributes=attrs,
        value=len({str(code).strip() for code in (blocking_issue_codes or ()) if str(code).strip()}),
        description="Distinct blocking issue codes observed by document planning shadow mode",
    )
    histogram(
        "hdb.document.planning.shadow.validity",
        1 if normalized_validity == "valid" else 0,
        attributes=attrs,
        unit="1",
        description="Whether a document planning shadow plan is executable",
    )
    histogram(
        "hdb.document.planning.shadow.units",
        max(0, int(units)),
        attributes=attrs,
        unit="{units}",
        description="Semantic units in document planning shadow plans",
    )
    histogram(
        "hdb.document.planning.shadow.tables",
        max(0, int(tables)),
        attributes=attrs,
        unit="{tables}",
        description="Table units in document planning shadow plans",
    )
    if duration_s is not None:
        histogram(
            "hdb.document.planning.shadow.duration",
            max(0.0, float(duration_s)),
            attributes=attrs,
            description="Document planning shadow compilation duration",
        )


def _export_format(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _EXPORT_FORMATS else "other"


def _export_status(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _EXPORT_STATUSES else "other"


def record_export_job(
    *,
    format: str,
    status: str,
    duration_s: float,
    queue_s: float | None = None,
    job_id: str | None = None,
) -> None:
    """Record one export task without turning identifiers into metric labels."""
    del job_id
    attrs = {"format": _export_format(format), "status": _export_status(status)}
    counter("hdb.export.jobs", attributes=attrs, description="Export jobs")
    histogram("hdb.export.job.duration", duration_s, attributes=attrs, description="Export job duration")
    if queue_s is not None:
        histogram("hdb.export.queue.wait", queue_s, attributes={"format": attrs["format"]}, description="Export queue wait")


def record_export_render(*, format: str, status: str, duration_s: float) -> None:
    """Record renderer latency by bounded format and outcome."""
    histogram(
        "hdb.export.render.duration",
        duration_s,
        attributes={"format": _export_format(format), "status": _export_status(status)},
        description="Export rendering duration",
    )


def record_export_bytes(*, format: str, byte_count: int) -> None:
    """Record produced artifact size; IDs and filenames are intentionally excluded."""
    histogram(
        "hdb.export.artifact.bytes",
        max(0, int(byte_count)),
        attributes={"format": _export_format(format)},
        unit="By",
        description="Export artifact size",
    )


def record_export_download(*, format: str, status: str = "succeeded") -> None:
    """Count artifact downloads by bounded format and outcome."""
    counter(
        "hdb.export.downloads",
        attributes={"format": _export_format(format), "status": _export_status(status)},
        description="Export artifact downloads",
    )


def set_export_queue_state(*, queue: str, depth: int, oldest_age_s: float) -> None:
    """Publish export queue depth through the shared low-cardinality queue gauges."""
    normalized = str(queue or "").strip().lower()
    set_queue_state(
        queue=normalized if normalized in _EXPORT_QUEUES else "other",
        depth=depth,
        oldest_age_s=oldest_age_s,
    )
