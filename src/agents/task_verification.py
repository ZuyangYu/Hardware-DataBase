"""Deterministic acceptance checks for a finished task attempt.

This is the "rubric" floor: it never pretends to judge quality, it only
checks the things that must be true for the deliverable to be usable.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_CITATION_RE = re.compile(r"\[\d+\]")


def _check(check_id: str, label: str, status: str, detail: str) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "status": status,  # pass | warn | fail
        "detail": detail,
    }


def build_task_verification(
    *,
    artifacts: Iterable[Any],
    answer_text: str,
    evidence_count: int = 0,
    partial: bool = False,
    kb_mounted: bool = False,
) -> dict[str, Any]:
    artifact_list = list(artifacts or [])
    answer = str(answer_text or "").strip()
    checks: list[dict[str, Any]] = []

    checks.append(
        _check(
            "artifact_present",
            "交付物",
            "pass" if artifact_list else "warn",
            f"{len(artifact_list)} 个产物" if artifact_list else "本轮未登记产物",
        )
    )
    checks.append(
        _check(
            "answer_present",
            "回答",
            "pass" if answer else "warn",
            f"{len(answer)} 字" if answer else "未生成回答",
        )
    )
    if kb_mounted:
        citations = len(_CITATION_RE.findall(answer))
        checks.append(
            _check(
                "citations",
                "引用覆盖",
                "pass" if citations else "warn",
                f"{citations} 处引用" if citations else "回答中没有引用编号",
            )
        )
        checks.append(
            _check(
                "evidence",
                "证据",
                "pass" if evidence_count else "warn",
                f"{evidence_count} 条证据" if evidence_count else "本轮未登记证据",
            )
        )
    if partial:
        checks.append(
            _check(
                "partial",
                "完整性",
                "warn",
                "本轮为部分交付，下一轮可继续",
            )
        )

    status = "pass" if checks and all(item["status"] == "pass" for item in checks) else "warn"
    return {
        "status": status,
        "checks": checks,
        "artifact_count": len(artifact_list),
        "evidence_count": int(evidence_count or 0),
        "answer_chars": len(answer),
    }


__all__ = ["build_task_verification"]
