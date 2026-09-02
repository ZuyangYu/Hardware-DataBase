"""Document-generation evaluation metrics (Phase A / Task 1).

Pure-function aggregation only: inputs are adapter-produced field
observations; nothing here reads the database. Every metric defines explicit
success, failure and unknown handling — unknown telemetry is marked
``inconclusive`` and never counted as success or failure. Denominators are
reported per required/optional; optional fields that are legally missing are
counted separately, never as failures.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.document_authoring.models import (
    AuthoringExecutionEvent,
    EvidenceRegistryEntry,
    HarnessRun,
)

METRIC_KEYS = (
    "field_success_rate",
    "typed_value_success_rate",
    "writer_fallback_rate",
    "human_review_rate",
    "avg_llm_calls_per_field",
    "avg_duration_seconds_per_field",
    "token_usage",
    "required_field_success_rate",
    "optional_field_missing_rate",
)

METRIC_DIRECTIONS = {
    "field_success_rate": "not_below_baseline",
    "typed_value_success_rate": "not_below_baseline",
    "required_field_success_rate": "not_below_baseline",
    "writer_fallback_rate": "not_above_baseline",
    "human_review_rate": "not_above_baseline",
    "optional_field_missing_rate": "not_above_baseline",
    "avg_llm_calls_per_field": "budget",
    "avg_duration_seconds_per_field": "budget",
    "token_usage": "budget",
}

INCONCLUSIVE = "inconclusive"


class FieldObservation(BaseModel):
    """One adapted per-field observation (adapter output, not DB rows)."""

    record_id: str
    field_id: str
    required: bool = True
    attempted: bool = True
    success: bool | None = None
    typed_value_ok: bool | None = None
    writer_mode: str | None = None
    writer_fallback: bool = False
    requires_human: bool = False
    optional_missing: bool = False
    llm_calls: int | None = None
    duration_seconds: float | None = None
    token_usage: dict[str, Any] = Field(default_factory=dict)


class MetricAggregate(BaseModel):
    metric_name: str
    value: float | None = None
    status: str = "success"
    denominator: int = 0
    numerator: float = 0.0
    direction: str = "not_below_baseline"
    detail: dict[str, Any] = Field(default_factory=dict)


def _rate(numerator: float, denominator: int, direction: str,
          detail: dict[str, Any] | None = None) -> MetricAggregate:
    unknown = int((detail or {}).get("unknown", 0))
    if denominator <= 0 or unknown > 0:
        return MetricAggregate(
            metric_name="", value=None, status=INCONCLUSIVE,
            denominator=denominator, numerator=numerator, direction=direction,
            detail=detail or {},
        )
    return MetricAggregate(
        metric_name="", value=numerator / denominator, status="success",
        denominator=denominator, numerator=numerator, direction=direction,
        detail=detail or {},
    )


def _token_totals(observations: list[FieldObservation]) -> dict[str, Any]:
    total = {"prompt": 0, "completion": 0, "total": 0, "unknown_fields": 0}
    for obs in observations:
        usage = obs.token_usage or {}
        if not usage:
            total["unknown_fields"] += 1
            continue
        for key in ("prompt", "completion", "total"):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                total[key] += int(value)
            else:
                total["unknown_fields"] += 1
    return total


def aggregate_document_generation_metrics(
    observations: list[FieldObservation],
) -> dict[str, MetricAggregate]:
    """Aggregate the nine required metric keys with explicit denominators."""
    all_attempted = [obs for obs in observations if obs.attempted]
    attempted = [obs for obs in all_attempted if not obs.optional_missing]
    required = [obs for obs in attempted if obs.required]
    optional = [obs for obs in all_attempted if not obs.required]

    def known(values: list[bool | None]) -> tuple[list[bool], int]:
        definite = [bool(v) for v in values if v is not None]
        return definite, len(values) - len(definite)

    field_success, field_unknown = known([obs.success for obs in attempted])
    typed_ok, typed_unknown = known([obs.typed_value_ok for obs in attempted])
    required_success, required_unknown = known([obs.success for obs in required])
    writer_fallbacks = [obs.writer_fallback for obs in attempted]
    human_reviews = [obs.requires_human for obs in attempted]
    optional_missing = [
        obs for obs in optional if obs.optional_missing or obs.success is False
    ]
    llm_known = [obs.llm_calls for obs in attempted if obs.llm_calls is not None]
    duration_known = [obs.duration_seconds for obs in attempted if obs.duration_seconds is not None]

    token_usage = _token_totals(attempted)

    metrics: dict[str, MetricAggregate] = {}
    rate = _rate(sum(field_success), len(attempted), METRIC_DIRECTIONS["field_success_rate"],
                 {"unknown": field_unknown})
    rate.metric_name = "field_success_rate"
    metrics["field_success_rate"] = rate

    rate = _rate(sum(typed_ok), len(attempted), METRIC_DIRECTIONS["typed_value_success_rate"],
                 {"unknown": typed_unknown})
    rate.metric_name = "typed_value_success_rate"
    metrics["typed_value_success_rate"] = rate

    rate = _rate(sum(1 for f in writer_fallbacks if f), len(attempted),
                 METRIC_DIRECTIONS["writer_fallback_rate"])
    rate.metric_name = "writer_fallback_rate"
    metrics["writer_fallback_rate"] = rate

    rate = _rate(sum(1 for h in human_reviews if h), len(attempted),
                 METRIC_DIRECTIONS["human_review_rate"])
    rate.metric_name = "human_review_rate"
    metrics["human_review_rate"] = rate

    if llm_known:
        value = sum(llm_known) / len(llm_known)
        status = "success"
        denominator = len(llm_known)
    else:
        value, status, denominator = None, INCONCLUSIVE, 0
    metrics["avg_llm_calls_per_field"] = MetricAggregate(
        metric_name="avg_llm_calls_per_field", value=value, status=status,
        denominator=denominator, numerator=float(sum(llm_known)),
        direction=METRIC_DIRECTIONS["avg_llm_calls_per_field"],
        detail={"unknown": len(attempted) - denominator},
    )

    if duration_known:
        value = sum(duration_known) / len(duration_known)
        status = "success"
        denominator = len(duration_known)
    else:
        value, status, denominator = None, INCONCLUSIVE, 0
    metrics["avg_duration_seconds_per_field"] = MetricAggregate(
        metric_name="avg_duration_seconds_per_field", value=value, status=status,
        denominator=denominator, numerator=float(sum(duration_known)),
        direction=METRIC_DIRECTIONS["avg_duration_seconds_per_field"],
        detail={"unknown": len(attempted) - denominator},
    )

    metrics["token_usage"] = MetricAggregate(
        metric_name="token_usage", value=float(token_usage["total"]),
        status=INCONCLUSIVE if token_usage["unknown_fields"] else "success",
        denominator=len(attempted), numerator=float(token_usage["total"]),
        direction=METRIC_DIRECTIONS["token_usage"], detail=token_usage,
    )

    rate = _rate(sum(required_success), len(required),
                 METRIC_DIRECTIONS["required_field_success_rate"], {"unknown": required_unknown})
    rate.metric_name = "required_field_success_rate"
    metrics["required_field_success_rate"] = rate

    rate = _rate(len(optional_missing), len(optional),
                 METRIC_DIRECTIONS["optional_field_missing_rate"])
    rate.metric_name = "optional_field_missing_rate"
    metrics["optional_field_missing_rate"] = rate

    return metrics


def _event_totals_for_field(
    events: list[AuthoringExecutionEvent], field_id: str
) -> dict[str, Any]:
    llm_calls = 0
    duration = 0.0
    token_usage: dict[str, Any] = {}
    requires_human = False
    writer_fallback = False
    for event in events:
        if event.field_id not in (None, field_id):
            continue
        if event.event_type == "llm_called":
            llm_calls += 1
        if event.duration_seconds is not None:
            duration += float(event.duration_seconds)
        if event.event_type == "human_waiting":
            requires_human = True
        if event.event_type in ("fallback_started", "fallback_completed"):
            writer_fallback = True
        usage = event.sanitized_payload.get("token_usage") if event.sanitized_payload else None
        if isinstance(usage, dict):
            for key in ("prompt", "completion", "total"):
                if key in usage:
                    token_usage[key] = usage[key]
    return {
        "llm_calls": llm_calls,
        "duration_seconds": duration or None,
        "token_usage": token_usage,
        "requires_human": requires_human,
        "writer_fallback": writer_fallback,
    }


def collect_observations(
    run: HarnessRun | None,
    events: list[AuthoringExecutionEvent],
    evidence_entries: list[EvidenceRegistryEntry] | None,
    drafts: list[dict[str, Any]],
    *,
    record_id_by_field: dict[str, str] | None = None,
    field_required: dict[str, bool] | None = None,
) -> list[FieldObservation]:
    """Adapt persisted business facts into per-field observations.

    Accepts the HarnessRun, its AuthoringExecutionEvent log, the run's
    EvidenceRegistry entries and the saved unit drafts (as plain dicts with
    ``unit_id``/``validation_status``/``typed_value``/``metadata``). Missing
    telemetry stays explicitly unknown — it is never defaulted to success.
    """
    record_id_by_field = record_id_by_field or {}
    field_required = field_required or {}
    observations: list[FieldObservation] = []
    unit_statuses: dict[str, str] = dict(getattr(run, "unit_statuses", {}) or {})
    field_ids: list[str] = []
    for draft in drafts:
        field_id = str(draft.get("unit_id") or "")
        if field_id and field_id not in field_ids:
            field_ids.append(field_id)
    for field_id in unit_statuses:
        if field_id and field_id not in field_ids:
            field_ids.append(field_id)

    for field_id in field_ids:
        draft = next((d for d in drafts if str(d.get("unit_id")) == field_id), None)
        status = unit_statuses.get(field_id)
        metadata = dict(draft.get("metadata") or {}) if draft else {}
        totals = _event_totals_for_field(events, field_id)
        validation_status = str(draft.get("validation_status") or "") if draft else ""
        typed_value = draft.get("typed_value") if draft else None
        optional_missing = bool(
            not field_required.get(field_id, True)
            and (status in (None, "tbd", "missing") or validation_status == "missing")
        )
        success: bool | None
        if optional_missing:
            success = None
        elif validation_status == "supported" and status in ("committed", "completed", None):
            success = True
        elif status in ("requires_human", "blocked", "conflicting", "retrieval_failed",
                        "insufficient_evidence"):
            success = False
        elif draft:
            success = False
        else:
            success = None
        observations.append(FieldObservation(
            record_id=record_id_by_field.get(field_id, field_id),
            field_id=field_id,
            required=field_required.get(field_id, True),
            attempted=True,
            success=success,
            typed_value_ok=(typed_value is not None) if draft else None,
            writer_mode=str(metadata.get("writer_mode")) if metadata.get("writer_mode") else None,
            writer_fallback=bool(metadata.get("writer_fallback")) or totals["writer_fallback"],
            requires_human=totals["requires_human"] or status == "requires_human",
            optional_missing=optional_missing,
            llm_calls=totals["llm_calls"],
            duration_seconds=totals["duration_seconds"],
            token_usage=totals["token_usage"],
        ))
    return observations
