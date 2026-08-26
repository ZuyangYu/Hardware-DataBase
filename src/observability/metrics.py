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


def _enabled() -> bool:
    try:
        import src.settings as settings

        return bool(settings.OBS_ENABLED and settings.OBS_METRICS_ENABLED)
    except Exception:
        return False


def metric_attributes(attributes: Mapping[str, Any] | None) -> dict[str, str]:
    """Drop high-cardinality labels before they can reach Prometheus."""

    result: dict[str, str] = {}
    for key, value in (attributes or {}).items():
        if key not in METRIC_LABEL_KEYS or value is None:
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
    histogram("hdb.agent.stage.duration", duration_s, attributes={"stage": stage, "status": status})


def record_retrieval(*, retriever: str, status: str, duration_s: float, hit_count: int, supplemental: bool = False) -> None:
    attrs = {"retriever": retriever, "status": status}
    counter("hdb.retrieval.calls", attributes=attrs, description="Retriever calls")
    histogram("hdb.retrieval.duration", duration_s, attributes={"retriever": retriever, "status": status})
    histogram("hdb.retrieval.hits", hit_count, attributes={"retriever": retriever}, unit="{hits}")
    if hit_count == 0:
        counter("hdb.retrieval.empty", attributes={"retriever": retriever}, description="Empty retrievals")
    if supplemental:
        counter("hdb.retrieval.supplemental", attributes={"retriever": retriever}, description="Supplemental retrievals")


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
