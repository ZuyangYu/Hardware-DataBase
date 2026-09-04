"""Post-collection quality gate.

采集完成后、进入评分前的一次自动体检：识别会让评分失真或白跑的采集
缺陷（失败快照、fail-open 降级答案、样本缺失），产出可持久化的质检
报告（verdict + issues + warnings + 逐样本标记），供前端展示与
「开始评分」的门禁判断使用。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .schemas import AnswerSnapshot, EvaluationSample

_DEGRADED_ANSWER_PREFIXES = ("系统错误", "模型服务调用过于频繁")


def _latest_by_sample(snapshots: list[AnswerSnapshot]) -> dict[str, AnswerSnapshot]:
    latest: dict[str, AnswerSnapshot] = {}
    for snapshot in snapshots:
        latest[snapshot.sample_id] = snapshot
    return latest


def run_collection_qc(
    samples: list[EvaluationSample],
    snapshots: list[AnswerSnapshot],
) -> dict[str, Any]:
    """Assess a finished collection; never raises on data problems."""

    latest = _latest_by_sample(snapshots)
    issues: list[str] = []
    warnings: list[str] = []
    sample_rows: list[dict[str, Any]] = []

    failed = degraded = zero_evidence = denied_total = 0
    for sample in samples:
        snap = latest.get(sample.id)
        flags: list[str] = []
        if snap is None:
            flags.append("missing_snapshot")
            if sample.expected_access == "allowed":
                issues.append(f"{sample.id}: 缺少采集快照")
        else:
            if snap.status != "success":
                flags.append("collection_failed")
                failed += 1
                issues.append(
                    f"{sample.id}: 采集失败（{snap.error_stage or 'unknown'}）"
                )
            answer = (snap.scored_response or snap.response or "").strip()
            if answer.startswith(_DEGRADED_ANSWER_PREFIXES):
                flags.append("degraded_answer")
                degraded += 1
                issues.append(f"{sample.id}: 降级答案（{answer[:24]}…），无评分价值")
            evidence_count = len(snap.retrieved_contexts or [])
            if sample.expected_access == "denied":
                denied_total += 1
            elif evidence_count == 0:
                flags.append("no_evidence")
                zero_evidence += 1
                warnings.append(
                    f"{sample.id}: 无检索证据，上下文类指标不适用"
                )
            sample_rows.append(
                {
                    "sample_id": sample.id,
                    "status": snap.status,
                    "evidence_count": evidence_count,
                    "duration_seconds": round(snap.duration_seconds or 0.0, 1),
                    "flags": flags,
                }
            )

    missing = len(samples) - len(latest)
    if missing > 0:
        issues.append(f"快照数量不足：{missing} 个样本未采集")

    if issues:
        verdict = "fail"
    elif warnings:
        verdict = "pass_with_warnings"
    else:
        verdict = "pass"

    return {
        "verdict": verdict,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "samples": len(samples),
            "snapshots": len(latest),
            "failed": failed,
            "degraded": degraded,
            "zero_evidence": zero_evidence,
            "expected_denied": denied_total,
        },
        "issues": issues,
        "warnings": warnings,
        "samples": sample_rows,
    }
