from __future__ import annotations

from src.evaluation.gates import DEFAULT_THRESHOLDS
from src.evaluation.schemas import EvaluationSummary, SampleResult


_METRIC_DISPLAY_LABELS = {
    "completeness": "完整性",
    "evidence_consistency": "证据一致性",
    "answer_correctness": "答案正确性",
    "faithfulness": "忠实性",
    "answer_relevancy": "答案相关性",
    "context_precision": "上下文精确率",
    "context_recall": "上下文召回率",
    "missing_information_honesty": "缺失信息诚实性",
    "conflict_disclosure": "冲突披露",
}

_METRIC_CHART_ORDER = tuple(_METRIC_DISPLAY_LABELS)


def _metric_chart_sort_key(metric_name: str) -> tuple[int, str]:
    try:
        return (_METRIC_CHART_ORDER.index(metric_name), metric_name)
    except ValueError:
        return (len(_METRIC_CHART_ORDER), metric_name)


def metric_display_label(metric_name: str) -> str:
    """Return a user-facing Chinese label while preserving unknown metric codes."""
    chinese_name = _METRIC_DISPLAY_LABELS.get(metric_name)
    return f"{chinese_name} ({metric_name})" if chinese_name else metric_name


def classify_sample_result(result: SampleResult) -> str:
    """Return a mutually exclusive, user-facing execution state for one sample."""
    if result.snapshot_status == "failed":
        return "采集失败"
    if any(metric.status == "failed" for metric in result.metrics):
        return "评分失败"
    if result.critical and not result.retrieved_contexts:
        return "关键样本待复核"
    if not result.retrieved_contexts:
        return "无检索证据"
    return "正常完成"


def build_credibility_summary(
    summary: EvaluationSummary, results: list[SampleResult]
) -> dict[str, int | str]:
    """Summarize coverage and technical failures without inferring score quality."""
    collection_failures = sum(
        result.snapshot_status == "failed" for result in results
    )
    metric_failures = sum(
        any(metric.status == "failed" for metric in result.metrics)
        for result in results
    )
    evidence_samples = sum(bool(result.retrieved_contexts) for result in results)
    scored_samples = sum(
        any(metric.status == "success" and metric.score is not None for metric in result.metrics)
        for result in results
    )

    if collection_failures or metric_failures or summary.failed_samples:
        status = "存在技术失败"
    elif not summary.metric_scores or not scored_samples:
        status = "评分覆盖不足"
    else:
        status = "结果可解读"

    return {
        "status": status,
        "evidence_samples": evidence_samples,
        "scored_samples": scored_samples,
        "collection_failures": collection_failures,
        "metric_failures": metric_failures,
    }


def build_comparison(
    current: EvaluationSummary, baseline: EvaluationSummary
) -> list[dict[str, float | str]]:
    """Compare only metrics produced by both completed evaluation runs."""
    return [
        {
            "指标": name,
            "当前": round(current.metric_scores[name], 4),
            "基线": round(baseline.metric_scores[name], 4),
            "变化": round(current.metric_scores[name] - baseline.metric_scores[name], 4),
        }
        for name in sorted(set(current.metric_scores) & set(baseline.metric_scores))
    ]


def build_current_metric_chart_rows(
    summary: EvaluationSummary,
) -> list[dict[str, float | int | str | bool | None]]:
    """Build score rows with coverage details for the current-run chart."""
    return [
        {
            "metric": name,
            "metric_label": metric_display_label(name),
            "score": score,
            "threshold": DEFAULT_THRESHOLDS.get(name),
            "meets_threshold": (
                DEFAULT_THRESHOLDS.get(name) is not None
                and score >= DEFAULT_THRESHOLDS[name]
            ),
            "applicable_samples": summary.metric_counts.get(name, 0),
            "scoring_failures": summary.metric_failures.get(name, 0),
        }
        for name, score in sorted(
            summary.metric_scores.items(), key=lambda item: _metric_chart_sort_key(item[0])
        )
    ]


def build_baseline_chart_rows(
    current: EvaluationSummary, baseline: EvaluationSummary
) -> list[dict[str, float | str]]:
    """Build grouped-score rows only for metrics available in both runs."""
    return [
        {
            "metric": name,
            "metric_label": metric_display_label(name),
            "current": current.metric_scores[name],
            "baseline": baseline.metric_scores[name],
            "change": round(current.metric_scores[name] - baseline.metric_scores[name], 4),
        }
        for name in sorted(
            set(current.metric_scores) & set(baseline.metric_scores),
            key=_metric_chart_sort_key,
        )
    ]


def build_sample_rows(results: list[SampleResult]) -> list[dict[str, int | str]]:
    """Create concise per-sample rows suitable for filtering before drill-down."""
    rows: list[dict[str, int | str]] = []
    for result in results:
        failed_metrics = [metric for metric in result.metrics if metric.status == "failed"]
        failure_reason = "; ".join(
            f"{metric.metric_name}: {metric.reason or '评分未完成'}"
            for metric in failed_metrics
        )
        rows.append(
            {
                "样本": result.sample_id,
                "状态": classify_sample_result(result),
                "关键样本": "是" if result.critical else "否",
                "证据数": len(result.retrieved_contexts),
                "已评分指标": sum(
                    metric.status == "success" and metric.score is not None
                    for metric in result.metrics
                ),
                "失败说明": failure_reason,
            }
        )
    return rows
