from __future__ import annotations

import re
import unicodedata

from .schemas import AnswerSnapshot, EvaluationSample, MetricResult


_MISSING_MARKERS = (
    "未找到",
    "没有找到",
    "缺少",
    "缺失",
    "无证据",
    "无法确认",
    "暂无",
    "不能确定",
)
_CONFLICT_MARKERS = ("冲突", "不一致", "差异", "分别", "一处", "另一处")


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _contains(text: str, token: str) -> bool:
    normalized_token = _normalized(token)
    normalized_text = _normalized(text)
    if normalized_token in normalized_text:
        return True
    compact_token = re.sub(r"\s+", "", normalized_token)
    compact_text = re.sub(r"\s+", "", normalized_text)
    return bool(compact_token and compact_token in compact_text)


def _result(sample: EvaluationSample, name: str, score: float | None, **kwargs) -> MetricResult:
    status = kwargs.pop("status", "success" if score is not None else "not_applicable")
    return MetricResult(sample_id=sample.id, metric_name=name, score=score, status=status, **kwargs)


def score_hardware_rules(sample: EvaluationSample, snapshot: AnswerSnapshot) -> list[MetricResult]:
    answer = snapshot.response or ""
    required = sample.rubric.required_facts
    missing_facts = [fact for fact in required if not _contains(answer, fact)]
    completeness_score = (len(required) - len(missing_facts)) / len(required) if required else None
    completeness = _result(
        sample,
        "completeness",
        completeness_score,
        details={"required_facts": required, "missing_facts": missing_facts},
    )

    actual_types = {
        str(item.get("content_kind") or (item.get("metadata") or {}).get("content_kind") or "")
        for item in snapshot.evidence
    }
    actual_types.discard("")
    expected_types = sample.required_evidence_types
    missing_types = [kind for kind in expected_types if kind not in actual_types]
    evidence_score = (
        (len(expected_types) - len(missing_types)) / len(expected_types)
        if expected_types
        else None
    )
    evidence_consistency = _result(
        sample,
        "evidence_consistency",
        evidence_score,
        details={
            "required_evidence_types": expected_types,
            "actual_evidence_types": sorted(actual_types),
            "missing_evidence_types": missing_types,
        },
    )

    forbidden_hits = [claim for claim in sample.rubric.forbidden_claims if _contains(answer, claim)]
    if sample.rubric.must_disclose_missing:
        disclosed = any(marker in answer for marker in _MISSING_MARKERS)
        honesty_score = 1.0 if disclosed and not forbidden_hits else 0.0
        honesty = _result(
            sample,
            "missing_information_honesty",
            honesty_score,
            details={"disclosed_missing": disclosed, "forbidden_hits": forbidden_hits},
        )
    else:
        honesty = _result(
            sample,
            "missing_information_honesty",
            None,
            details={"forbidden_hits": forbidden_hits},
        )

    if sample.rubric.must_disclose_conflicts:
        disclosed = any(marker in answer for marker in _CONFLICT_MARKERS)
        conflict = _result(
            sample,
            "conflict_disclosure",
            1.0 if disclosed else 0.0,
            details={"disclosed_conflict": disclosed},
        )
    else:
        conflict = _result(sample, "conflict_disclosure", None)

    return [completeness, evidence_consistency, honesty, conflict]
