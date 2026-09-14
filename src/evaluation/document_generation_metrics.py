"""Document-generation evaluation metrics (Phase A / Task 1).

Pure-function aggregation only: inputs are adapter-produced field
observations; nothing here reads the database. Every metric defines explicit
success, failure and unknown handling — unknown telemetry is marked
``inconclusive`` and never counted as success or failure. Denominators are
reported per required/optional; optional fields that are legally missing are
counted separately, never as failures.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
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
PARITY_METRIC_VERSION = "icd-pin-definition-v1"
PARITY_METRIC_DIRECTIONS = {
    "row_key_precision": "not_below_baseline",
    "row_key_recall": "not_below_baseline",
    "row_key_f1": "not_below_baseline",
    "required_column_completeness": "not_below_baseline",
    "exact_order_rate": "not_below_baseline",
    "relative_order_rate": "not_below_baseline",
    "duplicate_rate": "not_above_baseline",
    "missing_row_rate": "not_above_baseline",
    "extra_row_rate": "not_above_baseline",
    "cross_field_consistency_rate": "not_below_baseline",
    "evidence_support_rate": "not_below_baseline",
    "physical_protection_rate": "not_below_baseline",
}


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
    metric_version: str | None = None
    fixture_id: str | None = None


def _rate(
    numerator: float,
    denominator: int,
    direction: str,
    detail: dict[str, Any] | None = None,
) -> MetricAggregate:
    unknown = int((detail or {}).get("unknown", 0))
    if denominator <= 0 or unknown > 0:
        return MetricAggregate(
            metric_name="",
            value=None,
            status=INCONCLUSIVE,
            denominator=denominator,
            numerator=numerator,
            direction=direction,
            detail=detail or {},
        )
    return MetricAggregate(
        metric_name="",
        value=numerator / denominator,
        status="success",
        denominator=denominator,
        numerator=numerator,
        direction=direction,
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
    duration_known = [
        obs.duration_seconds for obs in attempted if obs.duration_seconds is not None
    ]

    token_usage = _token_totals(attempted)

    metrics: dict[str, MetricAggregate] = {}
    rate = _rate(
        sum(field_success),
        len(attempted),
        METRIC_DIRECTIONS["field_success_rate"],
        {"unknown": field_unknown},
    )
    rate.metric_name = "field_success_rate"
    metrics["field_success_rate"] = rate

    rate = _rate(
        sum(typed_ok),
        len(attempted),
        METRIC_DIRECTIONS["typed_value_success_rate"],
        {"unknown": typed_unknown},
    )
    rate.metric_name = "typed_value_success_rate"
    metrics["typed_value_success_rate"] = rate

    rate = _rate(
        sum(1 for f in writer_fallbacks if f),
        len(attempted),
        METRIC_DIRECTIONS["writer_fallback_rate"],
    )
    rate.metric_name = "writer_fallback_rate"
    metrics["writer_fallback_rate"] = rate

    rate = _rate(
        sum(1 for h in human_reviews if h),
        len(attempted),
        METRIC_DIRECTIONS["human_review_rate"],
    )
    rate.metric_name = "human_review_rate"
    metrics["human_review_rate"] = rate

    if llm_known:
        value = sum(llm_known) / len(llm_known)
        status = "success"
        denominator = len(llm_known)
    else:
        value, status, denominator = None, INCONCLUSIVE, 0
    metrics["avg_llm_calls_per_field"] = MetricAggregate(
        metric_name="avg_llm_calls_per_field",
        value=value,
        status=status,
        denominator=denominator,
        numerator=float(sum(llm_known)),
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
        metric_name="avg_duration_seconds_per_field",
        value=value,
        status=status,
        denominator=denominator,
        numerator=float(sum(duration_known)),
        direction=METRIC_DIRECTIONS["avg_duration_seconds_per_field"],
        detail={"unknown": len(attempted) - denominator},
    )

    metrics["token_usage"] = MetricAggregate(
        metric_name="token_usage",
        value=float(token_usage["total"]),
        status=INCONCLUSIVE if token_usage["unknown_fields"] else "success",
        denominator=len(attempted),
        numerator=float(token_usage["total"]),
        direction=METRIC_DIRECTIONS["token_usage"],
        detail=token_usage,
    )

    rate = _rate(
        sum(required_success),
        len(required),
        METRIC_DIRECTIONS["required_field_success_rate"],
        {"unknown": required_unknown},
    )
    rate.metric_name = "required_field_success_rate"
    metrics["required_field_success_rate"] = rate

    rate = _rate(
        len(optional_missing),
        len(optional),
        METRIC_DIRECTIONS["optional_field_missing_rate"],
    )
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
        usage = (
            event.sanitized_payload.get("token_usage")
            if event.sanitized_payload
            else None
        )
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
        elif validation_status == "supported" and status in (
            "committed",
            "completed",
            None,
        ):
            success = True
        elif status in (
            "requires_human",
            "blocked",
            "conflicting",
            "retrieval_failed",
            "insufficient_evidence",
        ):
            success = False
        elif draft:
            success = False
        else:
            success = None
        observations.append(
            FieldObservation(
                record_id=record_id_by_field.get(field_id, field_id),
                field_id=field_id,
                required=field_required.get(field_id, True),
                attempted=True,
                success=success,
                typed_value_ok=(typed_value is not None) if draft else None,
                writer_mode=str(metadata.get("writer_mode"))
                if metadata.get("writer_mode")
                else None,
                writer_fallback=bool(metadata.get("writer_fallback"))
                or totals["writer_fallback"],
                requires_human=totals["requires_human"] or status == "requires_human",
                optional_missing=optional_missing,
                llm_calls=totals["llm_calls"],
                duration_seconds=totals["duration_seconds"],
                token_usage=totals["token_usage"],
            )
        )
    return observations


def aggregate_document_authoring_parity_metrics(
    comparisons: Mapping[str, Any]
    | list[Mapping[str, Any]]
    | tuple[Mapping[str, Any], ...],
    *,
    fixture_id: str | None = None,
    metric_version: str = PARITY_METRIC_VERSION,
) -> dict[str, Any]:
    """Aggregate versioned offline parity reports without changing legacy metrics.

    ``comparisons`` accepts either the direct parity report returned by the ICD
    comparator or the legacy ``compare_workbooks`` result containing it under
    ``parity``.  Rates are combined from their numerators and denominators,
    never by averaging rounded per-fixture values.
    """

    if isinstance(comparisons, Mapping):
        items = [comparisons]
    else:
        items = list(comparisons)
    if not items:
        raise ValueError("at least one parity comparison is required")
    if not fixture_id:
        fixture_candidates = {
            str(item.get("fixture_id") or "")
            for item in items
            if isinstance(item, Mapping)
        }
        fixture_candidates.discard("")
        if len(fixture_candidates) == 1:
            fixture_id = fixture_candidates.pop()
    if not fixture_id:
        raise ValueError("fixture_id is required for parity metrics")

    metric_maps: list[Mapping[str, Any]] = []
    versions: set[str] = set()
    item_fixture_ids: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise TypeError("parity comparisons must be mappings")
        parity = item.get("parity")
        if isinstance(parity, Mapping) and isinstance(parity.get("metrics"), Mapping):
            metric_map = parity["metrics"]
            version = parity.get("metric_version")
        else:
            metric_map = item.get("metrics")
            version = item.get("metric_version")
        if not isinstance(metric_map, Mapping):
            raise ValueError("comparison does not contain parity metrics")
        if version:
            versions.add(str(version))
        item_fixture = str(item.get("fixture_id") or "")
        if item_fixture:
            item_fixture_ids.add(item_fixture)
        metric_maps.append(metric_map)
    if versions and (versions != {metric_version}):
        raise ValueError("parity metric version mismatch")
    if item_fixture_ids and item_fixture_ids != {fixture_id}:
        raise ValueError("parity fixture id mismatch")

    metric_names = sorted(
        {str(name) for metric_map in metric_maps for name in metric_map}
    )
    if not metric_names:
        raise ValueError("comparison contains no parity metrics")
    aggregated: dict[str, MetricAggregate] = {}
    for name in metric_names:
        numerator = 0.0
        denominator = 0
        unknown_comparisons = 0
        directions: set[str] = set()
        for metric_map in metric_maps:
            raw_metric = metric_map.get(name)
            metric = _metric_mapping(raw_metric)
            if metric is None:
                unknown_comparisons += 1
                continue
            direction = str(
                metric.get("direction")
                or PARITY_METRIC_DIRECTIONS.get(name, "not_below_baseline")
            )
            directions.add(direction)
            raw_numerator = metric.get("numerator", 0)
            raw_denominator = metric.get("denominator", 0)
            try:
                numerator += float(raw_numerator)
                denominator += max(0, int(raw_denominator))
            except (TypeError, ValueError):
                unknown_comparisons += 1
                continue
            if metric.get("status") != "success" or metric.get("value") is None:
                unknown_comparisons += 1
        if len(directions) > 1:
            raise ValueError(f"parity metric direction mismatch for {name}")
        direction = next(
            iter(directions), PARITY_METRIC_DIRECTIONS.get(name, "not_below_baseline")
        )
        status = (
            "success" if unknown_comparisons == 0 and denominator > 0 else INCONCLUSIVE
        )
        value = round(numerator / denominator, 6) if status == "success" else None
        aggregated[name] = MetricAggregate(
            metric_name=name,
            value=value,
            status=status,
            denominator=denominator,
            numerator=numerator,
            direction=direction,
            detail={
                "comparison_count": len(metric_maps),
                "unknown_comparisons": unknown_comparisons,
            },
            metric_version=metric_version,
            fixture_id=fixture_id,
        )
    return {
        "metric_version": metric_version,
        "fixture_id": fixture_id,
        "comparison_count": len(metric_maps),
        "metrics": aggregated,
    }


def load_parity_thresholds(
    path: str | Path,
    *,
    expected_metric_version: str | None = None,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Load and validate the versioned, machine-readable parity gate."""

    if (
        expected_metric_version
        and expected_version
        and expected_metric_version != expected_version
    ):
        raise ValueError("expected metric versions disagree")
    expected = expected_metric_version or expected_version
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid parity threshold file: {exc}") from exc
    return _normalize_threshold_payload(payload, expected_metric_version=expected)


def evaluate_parity_thresholds(
    metrics: Mapping[str, Any],
    thresholds: Mapping[str, Any] | str | Path,
) -> dict[str, Any]:
    """Evaluate parity metrics against a threshold file and fail closed."""

    if isinstance(thresholds, (str, Path)):
        threshold_payload = load_parity_thresholds(thresholds)
    else:
        threshold_payload = _normalize_threshold_payload(thresholds)
    report_version = str(metrics.get("metric_version") or "")
    report_fixture_id = str(metrics.get("fixture_id") or "")
    failures: list[str] = []
    if report_version != threshold_payload["metric_version"]:
        failures.append("metric_version")
    if report_fixture_id != threshold_payload["fixture_id"]:
        failures.append("fixture_id")
    raw_metrics = metrics.get("metrics", metrics)
    if not isinstance(raw_metrics, Mapping):
        raw_metrics = {}
    results: dict[str, dict[str, Any]] = {}
    for name, threshold in threshold_payload["thresholds"].items():
        metric = _metric_mapping(raw_metrics.get(name))
        observed = metric.get("value") if metric is not None else None
        direction = threshold["direction"]
        limit = threshold["value"]
        passed = (
            metric is not None
            and metric.get("status") == "success"
            and isinstance(observed, (int, float))
            and math.isfinite(float(observed))
            and (
                float(observed) >= limit
                if direction == "minimum"
                else float(observed) <= limit
            )
        )
        results[name] = {
            "observed": observed,
            "threshold": limit,
            "direction": direction,
            "denominator": metric.get("denominator") if metric is not None else 0,
            "status": metric.get("status") if metric is not None else INCONCLUSIVE,
            "passed": passed,
        }
        if not passed:
            failures.append(name)
    return {
        "passed": not failures,
        "metric_version": report_version,
        "fixture_id": report_fixture_id,
        "threshold_version": threshold_payload["threshold_version"],
        "failures": failures,
        "results": results,
    }


def _metric_mapping(metric: Any) -> dict[str, Any] | None:
    if isinstance(metric, MetricAggregate):
        return metric.model_dump()
    if isinstance(metric, Mapping):
        return dict(metric)
    return None


def _normalize_threshold_payload(
    payload: Mapping[str, Any],
    *,
    expected_metric_version: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("parity thresholds must be a JSON object")
    metric_version = str(payload.get("metric_version") or "")
    threshold_version = str(payload.get("threshold_version") or "")
    fixture_id = str(payload.get("fixture_id") or "")
    if not metric_version or not threshold_version or not fixture_id:
        raise ValueError("parity thresholds require versions and fixture_id")
    if expected_metric_version and metric_version != expected_metric_version:
        raise ValueError("parity threshold metric version mismatch")
    raw_thresholds = payload.get("thresholds")
    if not isinstance(raw_thresholds, Mapping) or not raw_thresholds:
        raise ValueError("parity thresholds require a non-empty thresholds object")
    normalized: dict[str, dict[str, float | str]] = {}
    for raw_name, raw_spec in raw_thresholds.items():
        name = str(raw_name)
        if isinstance(raw_spec, Mapping):
            raw_direction = raw_spec.get("direction")
            raw_value = raw_spec.get("value")
        else:
            raw_direction = None
            raw_value = raw_spec
        direction = _threshold_direction(name, raw_direction)
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid parity threshold for {name}") from exc
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"parity threshold for {name} must be between 0 and 1")
        normalized[name] = {"direction": direction, "value": value}
    return {
        "threshold_version": threshold_version,
        "metric_version": metric_version,
        "fixture_id": fixture_id,
        "thresholds": normalized,
    }


def _threshold_direction(name: str, raw_direction: Any) -> str:
    if raw_direction is None:
        direction = PARITY_METRIC_DIRECTIONS.get(name)
        if direction is None:
            raise ValueError(f"threshold direction is required for {name}")
        raw_direction = direction
    normalized = str(raw_direction).casefold()
    if normalized in {"minimum", "min", "not_below", "not_below_baseline"}:
        return "minimum"
    if normalized in {"maximum", "max", "not_above", "not_above_baseline"}:
        return "maximum"
    raise ValueError(f"invalid threshold direction for {name}")
