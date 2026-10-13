from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Collection, Mapping

from .cohorts import is_ragas_metric
from .schemas import GateResult, SampleResult


DEFAULT_THRESHOLDS: dict[str, float] = {
    "answer_correctness": 0.75,
    "faithfulness": 0.75,
    "evidence_consistency": 0.75,
    "answer_relevancy": 0.70,
    "context_precision": 0.70,
    "context_recall": 0.70,
    "forbidden_claims": 1.0,
}

DOCUMENT_GENERATION_HARD_ZERO_METRICS = {
    "fixed_content_overwrite_rate",
    "source_scope_violation_count",
    "unsupported_required_field_fill_count",
}

FRACTION_METRICS = set(DEFAULT_THRESHOLDS) | {
    "completeness", "honesty", "conflict_disclosure", "template_mapping_precision",
    "fixed_content_overwrite_rate", "field_recall_at_k", "evidence_support_rate",
    "auto_approval_rate",
}


def evaluate_gate(
    results: list[SampleResult],
    thresholds: dict[str, float] | None = None,
    *,
    fail_on_threshold: bool = False,
    sample_cohorts: Mapping[str, str] | None = None,
    min_coverage: float = 0.95,
    expected_metrics: Mapping[str, Collection[str]] | None = None,
    complete: bool = True,
) -> GateResult:
    """Separate a quality failure from an incomplete/unusable evaluation.

    Applicability supplied by the scorer is independent of observed results.
    Without that contract, explicit N/A and non-retrieval cohorts remain
    supported, but a missing configured metric is never treated as passing.
    """
    if not math.isfinite(min_coverage) or not 0 < min_coverage <= 1:
        raise ValueError("min_coverage must be finite and in (0, 1]")
    active_thresholds = dict(DEFAULT_THRESHOLDS if thresholds is None else thresholds)
    for name, threshold in active_thresholds.items():
        if not math.isfinite(threshold) or (name in FRACTION_METRICS and not 0 <= threshold <= 1):
            raise ValueError(f"invalid threshold for {name}")
    scores: dict[str, list[float]] = defaultdict(list)
    failures: list[str] = []
    insufficient: list[str] = []
    expected: dict[str, int] = defaultdict(int)
    if not results:
        insufficient.append("no evaluation samples")
    if not complete:
        insufficient.append("evaluation is incomplete")
    seen_samples: set[str] = set()
    for sample in results:
        if sample.sample_id in seen_samples:
            insufficient.append(f"duplicate sample {sample.sample_id}")
            continue
        seen_samples.add(sample.sample_id)
        if sample.snapshot_status != "success":
            failures.append(f"{sample.sample_id}: answer collection failed")
        access = sample.access_check or {}
        if access.get("expected") and access.get("observed") != access["expected"]:
            failures.append(f"{sample.sample_id}: access expectation was not satisfied")
        elif sample.metadata.get("expected_access") == "denied" and not access:
            insufficient.append(f"{sample.sample_id}: denied-access verification is missing")
        by_name = {}
        for metric in sample.metrics:
            if metric.sample_id != sample.sample_id or metric.metric_name in by_name:
                insufficient.append(f"{sample.sample_id}: duplicate or misattributed {metric.metric_name}")
                by_name[metric.metric_name] = None
            else:
                by_name[metric.metric_name] = metric
        if expected_metrics is not None and sample.sample_id not in expected_metrics:
            insufficient.append(f"{sample.sample_id}: applicability contract is missing")
        required = None if expected_metrics is None else set(expected_metrics.get(sample.sample_id, ()))
        names = set(active_thresholds) | set(by_name)
        if required is not None:
            names |= required
        for name in sorted(names):
            metric = by_name.get(name)
            if (
                sample_cohorts is not None
                and sample_cohorts.get(sample.sample_id) == "non_retrieval"
                and is_ragas_metric(name)
            ):
                continue
            applicable = (
                name in required if required is not None
                else not (metric is not None and metric.status == "not_applicable")
            )
            if applicable and (
                name in active_thresholds or name in DOCUMENT_GENERATION_HARD_ZERO_METRICS
                or (required is not None and name in required)
            ):
                expected[name] += 1
            if metric is None or metric.status != "success" or metric.score is None:
                if applicable and sample.critical and name in expected:
                    insufficient.append(f"critical sample {sample.sample_id}: {name} evaluation failed or missing")
                continue
            score = metric.score
            valid = math.isfinite(score)
            if name in FRACTION_METRICS:
                valid = valid and 0 <= score <= 1
            elif name in DOCUMENT_GENERATION_HARD_ZERO_METRICS:
                valid = valid and score >= 0
            if not valid:
                insufficient.append(f"{sample.sample_id}: {name} has invalid score")
                continue
            if not applicable:
                continue
            scores[name].append(score)
            if name in DOCUMENT_GENERATION_HARD_ZERO_METRICS and score != 0.0:
                failures.append(
                    f"{sample.sample_id}: {name} must be 0.0, got {score:.3f}"
                )
            threshold = active_thresholds.get(name)
            if sample.critical and threshold is not None and score < threshold:
                failures.append(
                    f"critical sample {sample.sample_id}: {name} {score:.3f} < {threshold:.3f}"
                )
    metric_scores = {
        name: sum(values) / len(values)
        for name, values in scores.items()
        if values
    }
    metric_counts = {name: len(values) for name, values in scores.items()}
    coverage = {name: metric_counts.get(name, 0) / count for name, count in expected.items() if count}
    for name, count in expected.items():
        valid_count = metric_counts.get(name, 0)
        if valid_count < math.ceil(min_coverage * count):
            insufficient.append(f"{name}: valid coverage {valid_count}/{count} < {min_coverage:.0%}")
    for name, threshold in active_thresholds.items():
        if name not in metric_scores or coverage.get(name, 1.0) < min_coverage:
            continue
        if metric_scores[name] < threshold:
            failures.append(f"{name}: {metric_scores[name]:.3f} < {threshold:.3f}")

    verdict = "fail" if failures else "insufficient_evaluation" if insufficient else "pass"
    passed = verdict == "pass"
    return GateResult(
        passed=passed,
        verdict=verdict,
        enforced=fail_on_threshold,
        exit_code=(2 if verdict == "fail" else 3) if fail_on_threshold and not passed else 0,
        metric_scores=metric_scores,
        metric_counts=metric_counts,
        failures=failures + insufficient,
        metric_expected_counts=dict(expected),
        metric_coverage=coverage,
        insufficient_reasons=insufficient,
    )
