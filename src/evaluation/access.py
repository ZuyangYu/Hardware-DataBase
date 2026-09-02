"""Evaluation knowledge-base binding and dataset access normalization.

The evaluation dataset is user-controlled input.  This module is the single
place where a selected, persisted knowledge-base identity is turned into a
bounded evaluation context so the API, preflight and worker use the same
rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from src.core.auth import KnowledgeBaseSummary
from src.pipelines.document_rag.schemas import RequestContext, kb_scope_key

from .history import cohort_fingerprint
from .schemas import EvaluationSample


_PERMISSION_LEVELS = {"": 0, "read": 1, "write": 2, "admin": 3}
_ACCESS_DENIED_STATUSES = frozenset(
    {"access_denied", "permission_denied", "forbidden", "unauthorized", "denied"}
)
_ACCESS_ALLOWED_STATUSES = frozenset({"access_granted", "allowed", "authorized"})


@dataclass(frozen=True)
class KnowledgeBaseBinding:
    kb_id: int
    kb_name: str
    department_id: int | None
    department_name: str | None = None
    registered: bool = True
    physical_exists: bool = False

    @property
    def scope_key(self) -> str:
        return kb_scope_key(self.kb_name, self.department_id)


@dataclass(frozen=True)
class EvaluationSampleSelection:
    samples: list[EvaluationSample]
    dataset_total_count: int
    matched_sample_count: int
    filtered_sample_count: int
    dataset_sample_count: int
    normal_sample_count: int
    expected_denied_sample_count: int
    cohort_fingerprint: str


def _summary_value(summary: KnowledgeBaseSummary | dict[str, Any], key: str) -> Any:
    if isinstance(summary, dict):
        return summary.get(key)
    return getattr(summary, key, None)


def _normalized_name(value: object) -> str:
    return str(value or "").strip()


def resolve_knowledge_base(
    summaries: Iterable[KnowledgeBaseSummary | dict[str, Any]],
    *,
    kb_id: int | str | None = None,
    kb_name: str | None = None,
) -> KnowledgeBaseBinding:
    """Resolve a registered KB, using its stable ID as the authority.

    Name-only resolution remains available for legacy callers only when the
    name maps to exactly one registered record.  Physical-only records do not
    have a stable application identity and cannot be selected.
    """

    rows = list(summaries)
    normalized_name = _normalized_name(kb_name)
    candidates = [
        row
        for row in rows
        if bool(_summary_value(row, "registered"))
        and _summary_value(row, "kb_id") not in (None, "")
    ]
    if kb_id not in (None, ""):
        try:
            wanted_id = int(kb_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("kb_id 必须是整数") from exc
        matched = [row for row in candidates if int(_summary_value(row, "kb_id")) == wanted_id]
        if not matched:
            raise ValueError("所选知识库不存在、未注册或缺少稳定 kb_id")
        row = matched[0]
        row_name = _normalized_name(_summary_value(row, "name") or _summary_value(row, "kb_name"))
        if normalized_name and normalized_name != row_name:
            raise ValueError("kb_id 与 kb_name 不匹配")
    else:
        if not normalized_name:
            raise ValueError("必须指定 kb_id")
        matched = [
            row
            for row in candidates
            if _normalized_name(_summary_value(row, "name") or _summary_value(row, "kb_name"))
            == normalized_name
        ]
        if not matched:
            raise ValueError("所选知识库不存在或未注册")
        if len(matched) > 1:
            raise ValueError("知识库名称不唯一，请指定 kb_id")
        row = matched[0]
        wanted_id = int(_summary_value(row, "kb_id"))
        row_name = normalized_name

    department_id = _summary_value(row, "department_id")
    if department_id in (None, ""):
        normalized_department_id = None
    else:
        try:
            normalized_department_id = int(department_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("知识库 department_id 无效") from exc
    return KnowledgeBaseBinding(
        kb_id=wanted_id,
        kb_name=row_name,
        department_id=normalized_department_id,
        department_name=_summary_value(row, "department_name"),
        registered=bool(_summary_value(row, "registered")),
        physical_exists=bool(_summary_value(row, "physical_exists")),
    )


def _scope_name(value: object) -> str:
    text = _normalized_name(value)
    if ":" in text:
        return text.rsplit(":", 1)[1].strip()
    return text


def _mentions_selected_kb(value: object, binding: KnowledgeBaseBinding) -> bool:
    return _scope_name(value) == binding.kb_name


def _iter_values(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _permission_level(value: object) -> int:
    return _PERMISSION_LEVELS.get(_normalized_name(value).casefold(), 0)


def _dataset_allows_selected(raw: dict[str, Any], binding: KnowledgeBaseBinding) -> bool:
    """Apply dataset scope as a narrowing constraint, never as an authority."""

    allowed_kbs = raw.get("allowed_kbs")
    if "allowed_kbs" in raw and not any(
        _mentions_selected_kb(value, binding) for value in _iter_values(allowed_kbs)
    ):
        return False

    permissions = raw.get("kb_permissions")
    if "kb_permissions" in raw:
        if not isinstance(permissions, dict):
            return False
        selected_permissions = [
            value
            for key, value in (permissions or {}).items()
            if _mentions_selected_kb(key, binding)
        ]
        if not selected_permissions or max(map(_permission_level, selected_permissions)) < 1:
            return False
    return True


def normalize_sample_for_binding(
    sample: EvaluationSample,
    binding: KnowledgeBaseBinding,
) -> EvaluationSample:
    """Return a sample whose request context is bounded to ``binding``."""

    raw = dict(sample.request_context or {})
    session_id = _normalized_name(raw.get("session_id"))
    expected_access = sample.expected_access
    allowed = expected_access == "allowed" and _dataset_allows_selected(raw, binding)
    scope = binding.scope_key
    context: dict[str, Any] = {
        # The dataset may describe the historical actor, but it must never
        # choose the runtime identity used for an evaluation request.
        "user_id": "evaluation",
        "session_id": session_id,
        "department_id": binding.department_id,
        "roles": ["user"],
        "allowed_kbs": [scope] if allowed else [],
        "kb_permissions": {scope: "read"} if allowed else {},
    }
    context = {key: value for key, value in context.items() if value not in (None, "")}
    return sample.model_copy(update={"request_context": context})


def build_evaluation_context(sample: EvaluationSample) -> RequestContext:
    """Build the runtime context from a normalized evaluation sample."""

    raw = sample.request_context or {}
    department_id = raw.get("department_id")
    metadata: dict[str, Any] = {}
    if department_id not in (None, ""):
        metadata["department_id"] = department_id
    declared_user = _normalized_name(raw.get("user_id"))
    if declared_user:
        metadata["declared_user"] = declared_user
    return RequestContext(
        user_id="evaluation",
        session_id=_normalized_name(raw.get("session_id")) or f"eval-{sample.id}",
        roles=["user"],
        allowed_kbs=[_normalized_name(value) for value in _iter_values(raw.get("allowed_kbs")) if _normalized_name(value)],
        kb_permissions={
            _normalized_name(key): _normalized_name(value)
            for key, value in (
                raw.get("kb_permissions")
                if isinstance(raw.get("kb_permissions"), dict)
                else {}
            ).items()
            if _normalized_name(key)
        },
        metadata=metadata,
    )


def select_evaluation_samples(
    samples: list[EvaluationSample],
    binding: KnowledgeBaseBinding,
    *,
    sample_ids: list[str] | set[str] | None = None,
    tags: list[str] | set[str] | None = None,
) -> EvaluationSampleSelection:
    """Filter by KB identity first, then apply explicit user filters."""

    matched = [sample for sample in samples if _normalized_name(sample.kb_name) == binding.kb_name]
    selected = matched
    if sample_ids:
        wanted_ids = {str(value).strip() for value in sample_ids if str(value).strip()}
        selected = [sample for sample in selected if sample.id in wanted_ids]
    if tags:
        wanted_tags = {str(value).strip() for value in tags if str(value).strip()}
        selected = [sample for sample in selected if wanted_tags.intersection(sample.tags)]
    normalized = [normalize_sample_for_binding(sample, binding) for sample in selected]
    denied_count = sum(sample.expected_access == "denied" for sample in normalized)
    return EvaluationSampleSelection(
        samples=normalized,
        dataset_total_count=len(samples),
        matched_sample_count=len(matched),
        filtered_sample_count=len(samples) - len(selected),
        dataset_sample_count=len(selected),
        normal_sample_count=len(selected) - denied_count,
        expected_denied_sample_count=denied_count,
        cohort_fingerprint=cohort_fingerprint(sample.id for sample in selected),
    )


def _structured_access_decision(retrieval_summary: dict[str, Any]) -> str | None:
    for key in (
        "access_decision",
        "access_status",
        "permission_status",
        "authorization_status",
        "status",
    ):
        value = _normalized_name(retrieval_summary.get(key)).casefold()
        if value in _ACCESS_DENIED_STATUSES:
            return "denied"
        if value in _ACCESS_ALLOWED_STATUSES:
            return "allowed"
    for key in ("authorization", "access_check", "permission"):
        value = retrieval_summary.get(key)
        if isinstance(value, dict):
            decision = _structured_access_decision(value)
            if decision:
                return decision
    if retrieval_summary.get("access_denied") is True or retrieval_summary.get("permission_denied") is True:
        return "denied"
    if retrieval_summary.get("access_granted") is True or retrieval_summary.get("authorized") is True:
        return "allowed"
    return None


def assess_access(
    sample: EvaluationSample,
    context: RequestContext,
    retrieval_summary: dict[str, Any] | None,
    *,
    response: str = "",
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Classify access using structured state and the bounded context.

    Response text is deliberately ignored.  Evidence returned under a
    context that has no read permission is treated as a leak so the result is
    not reported as a successful denial test.
    """

    del response  # Access classification must never inspect answer wording.
    summary = retrieval_summary or {}
    effective_evidence = evidence if evidence is not None else summary.get("evidence") or []
    explicit = _structured_access_decision(summary)
    has_permission = context.has_kb_permission(sample.kb_name, "read")
    if not has_permission and effective_evidence:
        # A denial status does not excuse evidence returned under a context
        # that has no read permission: the evidence itself is the observable
        # authorization leak that the negative test must report.
        observed = "allowed"
        reason = "evidence was returned despite a context without read permission"
    elif explicit == "denied":
        observed = "denied"
        reason = "retrieval authorization decision denied access"
    elif explicit == "allowed":
        observed = "allowed"
        reason = "retrieval authorization decision allowed access"
    elif not has_permission:
        if effective_evidence:
            observed = "allowed"
            reason = "evidence was returned despite a context without read permission"
        else:
            observed = "denied"
            reason = "evaluation context has no read permission"
    else:
        observed = "allowed"
        reason = "evaluation context has read permission"
    return {"expected": sample.expected_access, "observed": observed, "reason": reason}
