"""Low-cardinality OpenTelemetry metrics used by API, workers and evaluations."""

from __future__ import annotations

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
    pass


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
