from __future__ import annotations

import re
import unicodedata

from .schemas import (
    AnswerSnapshot,
    EvaluationSample,
    MetricResult,
)


_EVIDENCE_TYPE_ALIASES = {
    "document_text": "document",
    # 表格管线的存储证据 kind → 数据集词汇 "spreadsheet"
    "spreadsheet_table": "spreadsheet",
    "spreadsheet_schema": "spreadsheet",
    "spreadsheet_sql_result": "spreadsheet",
    "table": "spreadsheet",
    # 电路与目录证据的常见变体
    "circuit": "circuit_design",
    "catalog": "catalog",
}


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


def _canonical_evidence_type(value: object) -> str:
    """Normalize storage-specific evidence kinds to dataset vocabulary."""

    normalized = str(value or "").strip().casefold()
    return _EVIDENCE_TYPE_ALIASES.get(normalized, normalized)


def _result(sample: EvaluationSample, name: str, score: float | None, **kwargs) -> MetricResult:
    status = kwargs.pop("status", "success" if score is not None else "not_applicable")
    return MetricResult(sample_id=sample.id, metric_name=name, score=score, status=status, **kwargs)


def score_hardware_rules(sample: EvaluationSample, snapshot: AnswerSnapshot) -> list[MetricResult]:
    """确定性词法层评估。

    始终执行：forbidden_claims（禁词命中=确定性错误）、evidence_consistency（工具路由审计）。
    这两项是天然词法的检查，子串匹配在此是恰当的，且可复现、可进 CI 门禁。

    语义性质的评估（事实覆盖度、诚实度、冲突披露）不由规则承担——
    子串/标记词实现对自然语言答案存在系统性误杀，统一交给 RAGAS 裁判层
    （answer_correctness / faithfulness）按参考答案语义判卷。
    """
    answer = snapshot.response or ""

    # 禁词命中：出现即确定性错误（如把 U900 判成 TC377）
    forbidden_hits = [claim for claim in sample.rubric.forbidden_claims if _contains(answer, claim)]
    if sample.rubric.forbidden_claims:
        forbidden = _result(
            sample,
            "forbidden_claims",
            0.0 if forbidden_hits else 1.0,
            details={"forbidden_hits": forbidden_hits},
        )
    else:
        forbidden = _result(sample, "forbidden_claims", None, details={"forbidden_hits": []})

    actual_types = {
        _canonical_evidence_type(
            item.get("content_kind") or (item.get("metadata") or {}).get("content_kind") or ""
        )
        for item in snapshot.evidence
    }
    actual_types.discard("")
    expected_types = [_canonical_evidence_type(kind) for kind in sample.required_evidence_types]
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

    return [forbidden, evidence_consistency]
