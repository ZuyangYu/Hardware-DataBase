from __future__ import annotations

from collections import defaultdict

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


def evaluate_gate(
    results: list[SampleResult],
    thresholds: dict[str, float] | None = None,
    *,
    fail_on_threshold: bool = False,
) -> GateResult:
    active_thresholds = dict(DEFAULT_THRESHOLDS if thresholds is None else thresholds)
    scores: dict[str, list[float]] = defaultdict(list)
    failures: list[str] = []

    for sample in results:
        for metric in sample.metrics:
            threshold = active_thresholds.get(metric.metric_name)
            if sample.critical and metric.status == "failed" and threshold is not None:
                failures.append(
                    f"critical sample {sample.sample_id}: {metric.metric_name} evaluation failed"
                )
            if metric.status != "success" or metric.score is None:
                continue
            scores[metric.metric_name].append(metric.score)
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
    for name, threshold in active_thresholds.items():
        if name in metric_scores and metric_scores[name] < threshold:
            failures.append(f"{name}: {metric_scores[name]:.3f} < {threshold:.3f}")

    passed = not failures
    return GateResult(
        passed=passed,
        exit_code=2 if fail_on_threshold and not passed else 0,
        metric_scores=metric_scores,
        metric_counts=metric_counts,
        failures=failures,
    )
