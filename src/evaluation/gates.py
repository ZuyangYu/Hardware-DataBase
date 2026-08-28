from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping

from .cohorts import is_ragas_metric
from .schemas import GateResult, SampleResult


DEFAULT_THRESHOLDS: dict[str, float] = {
    "answer_correctness": 0.75,
    "faithfulness": 0.75,
    "completeness": 0.75,
    "evidence_consistency": 0.75,
    "answer_relevancy": 0.70,
    "context_precision": 0.70,
    "context_recall": 0.70,
    "missing_information_honesty": 0.90,
    "conflict_disclosure": 0.90,
}

DOCUMENT_GENERATION_HARD_ZERO_METRICS = {
    "fixed_content_overwrite_rate",
    "source_scope_violation_count",
    "unsupported_required_field_fill_count",
}


def evaluate_gate(
    results: list[SampleResult],
    thresholds: dict[str, float] | None = None,
    *,
    fail_on_threshold: bool = False,
    sample_cohorts: Mapping[str, str] | None = None,
    min_coverage: float = 0.8,
) -> GateResult:
    active_thresholds = dict(DEFAULT_THRESHOLDS if thresholds is None else thresholds)
    scores: dict[str, list[float]] = defaultdict(list)
    failures: list[str] = []
    sample_total = len(results)

    for sample in results:
        for metric in sample.metrics:
            if (
                sample_cohorts is not None
                and sample_cohorts.get(sample.sample_id) == "non_retrieval"
                and is_ragas_metric(metric.metric_name)
            ):
                continue
            threshold = active_thresholds.get(metric.metric_name)
            if sample.critical and metric.status == "failed" and threshold is not None:
                failures.append(
                    f"critical sample {sample.sample_id}: {metric.metric_name} evaluation failed"
                )
            if metric.status != "success" or metric.score is None:
                continue
            scores[metric.metric_name].append(metric.score)
            if metric.metric_name in DOCUMENT_GENERATION_HARD_ZERO_METRICS and metric.score != 0.0:
                failures.append(
                    f"{sample.sample_id}: {metric.metric_name} must be 0.0, got {metric.score:.3f}"
                )
            if sample.critical and threshold is not None and metric.score < threshold:
                failures.append(
                    f"critical sample {sample.sample_id}: {metric.metric_name} "
                    f"{metric.score:.3f} < {threshold:.3f}"
                )

    metric_scores = {
        name: sum(values) / len(values)
        for name, values in scores.items()
        if values
    }
    metric_counts = {name: len(values) for name, values in scores.items()}
    needed = max(1, math.ceil(min_coverage * sample_total)) if sample_total else 0
    low_coverage = {
        name for name, count in metric_counts.items() if sample_total and count < needed
    }
    for name, threshold in active_thresholds.items():
        if name not in metric_scores or name in low_coverage:
            continue
        if metric_scores[name] < threshold:
            failures.append(f"{name}: {metric_scores[name]:.3f} < {threshold:.3f}")

    passed = not failures
    return GateResult(
        passed=passed,
        exit_code=2 if fail_on_threshold and not passed else 0,
        metric_scores=metric_scores,
        metric_counts=metric_counts,
        failures=failures,
    )
