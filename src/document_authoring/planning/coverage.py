"""Deterministic evaluation of plan-backed coverage contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from src.document_authoring.models import DocumentUnitDraft

from .models import CoverageContract, CoverageRequirement
from .review_contracts import CoverageReport, CoverageRequirementResult, ReviewIssue


class CoverageEvaluator:
    """Evaluate semantic candidates against an immutable ``CoverageContract``."""

    def evaluate(
        self,
        plan: Any,
        typed_drafts: Mapping[str, DocumentUnitDraft] | Sequence[DocumentUnitDraft],
        evidence_registry: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> CoverageReport:
        contract, plan_id, plan_version, plan_hash = _contract_and_identity(plan)
        drafts = _draft_map(typed_drafts)
        evidence = _evidence_map(evidence_registry)
        results: dict[str, CoverageRequirementResult] = {}
        for requirement in contract.requirements:
            draft = _draft_for_unit(drafts, requirement.unit_id)
            results[requirement.requirement_id] = self._evaluate_requirement(
                requirement, draft, drafts, evidence,
            )
        return CoverageReport(
            plan_id=plan_id,
            plan_version=plan_version,
            plan_hash=plan_hash,
            requirement_results=results,
            expected_count=len(contract.requirements),
            covered_count=sum(result.status == "complete" for result in results.values()),
            missing_count=sum(result.missing_count for result in results.values()),
            unsupported_count=sum(result.unsupported_count for result in results.values()),
            duplicate_count=sum(result.duplicate_count for result in results.values()),
        )

    def _evaluate_requirement(
        self,
        requirement: CoverageRequirement,
        draft: DocumentUnitDraft | None,
        drafts: dict[str, DocumentUnitDraft],
        evidence: dict[str, dict[str, Any]],
    ) -> CoverageRequirementResult:
        if requirement.kind == "cross_unit":
            return self._evaluate_cross_unit(requirement, drafts)

        if draft is None:
            expected = len(requirement.row_keys) if requirement.kind == "table" and requirement.row_keys else 1
            missing_rows = list(requirement.row_keys) if requirement.kind == "table" else []
            issues = [self._issue(
                "missing_requirement", requirement,
                suggested_action="retrieve_and_draft_unit",
            )]
            return CoverageRequirementResult(
                requirement_id=requirement.requirement_id,
                unit_id=requirement.unit_id,
                kind=requirement.kind,
                required=requirement.required,
                status="missing",
                expected_count=expected,
                missing_count=expected,
                missing_row_keys=missing_rows,
                issues=issues,
            )

        typed = draft.typed_value
        if requirement.kind == "table":
            return self._evaluate_table(requirement, draft, typed, evidence)

        issues: list[ReviewIssue] = []
        evidence_ids = set(draft.evidence_ids)
        if not evidence_ids:
            issues.append(self._issue(
                "missing_evidence", requirement, suggested_action="retrieve_evidence",
            ))
        unknown = evidence_ids - set(evidence)
        if unknown:
            issues.append(self._issue(
                "unknown_evidence", requirement, evidence_ids=sorted(unknown),
                suggested_action="reload_frozen_evidence",
            ))
        if typed is None and requirement.kind in {"scalar", "paragraph", "section", "artifact"}:
            if requirement.kind == "paragraph" and (draft.content or "").strip():
                pass
            else:
                issues.append(self._issue(
                    "missing_typed_value", requirement,
                    suggested_action="return_typed_candidate",
                ))
        elif typed is not None and requirement.kind == "scalar" and typed.kind == "table":
            issues.append(self._issue(
                "table_scalarized", requirement, suggested_action="return_scalar_candidate",
            ))
        elif typed is not None:
            expected_ids = set(typed.evidence_ids)
            unknown_typed = expected_ids - set(evidence)
            outside = expected_ids - set(draft.evidence_ids)
            if unknown_typed or outside:
                issues.append(self._issue(
                    "unknown_evidence" if unknown_typed else "evidence_ownership",
                    requirement,
                    evidence_ids=sorted(unknown_typed or outside),
                    suggested_action="bind_evidence_to_current_unit",
                ))
            if requirement.min_evidence_items and len(expected_ids) < requirement.min_evidence_items:
                issues.append(self._issue(
                    "insufficient_evidence", requirement,
                    evidence_ids=sorted(expected_ids),
                    suggested_action="retrieve_more_evidence",
                ))
        if draft.validation_status in {"unsupported", "requires_human", "partial"}:
            issues.append(self._issue(
                "draft_not_supported", requirement,
                suggested_action="rework_unit_draft",
            ))
        status = "complete" if not issues else (
            "unsupported" if any(issue.code in {
                "unknown_evidence", "evidence_ownership", "table_scalarized", "draft_not_supported",
            } for issue in issues) else "missing"
        )
        return CoverageRequirementResult(
            requirement_id=requirement.requirement_id,
            unit_id=requirement.unit_id,
            kind=requirement.kind,
            required=requirement.required,
            status=status,
            expected_count=1,
            covered_count=1 if status == "complete" else 0,
            missing_count=0 if status == "complete" else 1,
            unsupported_count=1 if status == "unsupported" else 0,
            evidence_ids=sorted(evidence_ids),
            issues=issues,
        )

    def _evaluate_table(
        self,
        requirement: CoverageRequirement,
        draft: DocumentUnitDraft,
        typed: Any,
        evidence: dict[str, dict[str, Any]],
    ) -> CoverageRequirementResult:
        issues: list[ReviewIssue] = []
        expected_keys = list(requirement.row_keys)
        expected_columns = set(requirement.required_columns)
        if typed is None or typed.kind != "table":
            issues.append(self._issue(
                "table_scalarized", requirement,
                suggested_action="return_typed_table_rows",
            ))
            return CoverageRequirementResult(
                requirement_id=requirement.requirement_id,
                unit_id=requirement.unit_id,
                kind=requirement.kind,
                required=requirement.required,
                status="unsupported",
                expected_count=len(expected_keys) or 1,
                missing_count=len(expected_keys) or 1,
                unsupported_count=1,
                missing_row_keys=expected_keys,
                issues=issues,
            )

        rows = list(typed.rows)
        actual_keys = [row.row_key.strip() for row in rows]
        missing_keys = [key for key in expected_keys if key not in set(actual_keys)]
        # An empty ``row_keys`` contract means identity is server-owned per
        # row (for example frozen EDF connectors), not that no rows may exist.
        unexpected_keys = [
            key for key in actual_keys
            if expected_keys and key and key not in set(expected_keys)
        ]
        duplicate_keys = sorted({key for key in actual_keys if key and actual_keys.count(key) > 1})
        if expected_keys and any(not key for key in actual_keys):
            for index, key in enumerate(actual_keys):
                if not key:
                    issues.append(self._issue(
                        "row_identity_missing", requirement, row_key=None,
                        suggested_action="return_server_owned_row_key",
                    ))
        if missing_keys:
            issues.extend(self._issue(
                "missing_row", requirement, row_key=key,
                suggested_action="retrieve_and_draft_missing_row",
            ) for key in missing_keys)
        if unexpected_keys:
            issues.extend(self._issue(
                "unexpected_row", requirement, row_key=key,
                suggested_action="remove_row_outside_frozen_scope",
            ) for key in unexpected_keys)
        if duplicate_keys and requirement.duplicate_policy == "reject":
            issues.extend(self._issue(
                "duplicate_row_key", requirement, row_key=key,
                suggested_action="deduplicate_row_identity",
            ) for key in duplicate_keys)
        if expected_keys and requirement.row_order == "declared" and actual_keys != expected_keys:
            issues.append(self._issue(
                "row_order_mismatch", requirement,
                suggested_action="sort_by_declared_row_key_order",
            ))
        if not expected_keys and requirement.row_order == "stable_key":
            keyed = [key for key in actual_keys if key]
            if any(not key for key in actual_keys) or keyed != sorted(keyed):
                issues.append(self._issue(
                    "row_order_mismatch", requirement,
                    suggested_action="sort_by_stable_row_key",
                ))

        valid_rows = 0
        evidence_ids: set[str] = set()
        missing_columns: set[str] = set()
        for row in rows:
            row_key = row.row_key.strip() or None
            row_ids = set(row.evidence_ids)
            evidence_ids.update(row_ids)
            unknown_row_ids = row_ids - set(evidence)
            outside_row_ids = row_ids - set(typed.evidence_ids) - set(draft.evidence_ids)
            if unknown_row_ids:
                issues.append(self._issue(
                    "unknown_evidence", requirement, row_key=row_key,
                    evidence_ids=sorted(unknown_row_ids),
                    suggested_action="reload_frozen_evidence",
                ))
            if outside_row_ids:
                issues.append(self._issue(
                    "evidence_ownership", requirement, row_key=row_key,
                    evidence_ids=sorted(outside_row_ids),
                    suggested_action="bind_row_evidence_to_current_unit",
                ))
            row_missing = expected_columns - set(row.cells)
            row_unknown = set(row.cells) - expected_columns if expected_columns else set()
            if row_missing:
                missing_columns.update(row_missing)
                for column in sorted(row_missing):
                    issues.append(self._issue(
                        "missing_required_column", requirement, row_key=row_key,
                        column_id=column, suggested_action="return_required_table_cell",
                    ))
            if row_unknown:
                for column in sorted(row_unknown):
                    issues.append(self._issue(
                        "unexpected_column", requirement, row_key=row_key,
                        column_id=column, suggested_action="remove_unregistered_table_column",
                    ))
            row_is_valid = not row_missing and not row_unknown and bool(row_ids)
            for column, value in row.cells.items():
                cell_ids = set(row.cell_evidence_ids.get(column, row_ids))
                evidence_ids.update(cell_ids)
                if cell_ids - row_ids:
                    issues.append(self._issue(
                        "cell_evidence_ownership", requirement, row_key=row_key,
                        column_id=column, evidence_ids=sorted(cell_ids - row_ids),
                        suggested_action="bind_cell_evidence_to_row",
                    ))
                    row_is_valid = False
                unknown_cell_ids = cell_ids - set(evidence)
                if unknown_cell_ids:
                    issues.append(self._issue(
                        "unknown_evidence", requirement, row_key=row_key,
                        column_id=column, evidence_ids=sorted(unknown_cell_ids),
                        suggested_action="reload_frozen_evidence",
                    ))
                    row_is_valid = False
                if not str(value).strip():
                    issues.append(self._issue(
                        "empty_required_cell", requirement, row_key=row_key,
                        column_id=column, suggested_action="return_non_empty_table_cell",
                    ))
                    row_is_valid = False
            if row_is_valid and (not expected_keys or row_key in set(expected_keys)):
                valid_rows += 1

        if draft.validation_status in {"unsupported", "requires_human", "partial"}:
            issues.append(self._issue(
                "draft_not_supported", requirement,
                suggested_action="rework_unit_draft",
            ))
        status = "complete" if not issues else (
            "unsupported" if any(issue.code in {
                "table_scalarized", "row_identity_missing", "unknown_evidence",
                "evidence_ownership", "cell_evidence_ownership", "draft_not_supported",
            } for issue in issues) else "partial"
        )
        missing_count = len(missing_keys) + (1 if not rows else 0)
        if status == "complete":
            missing_count = 0
        return CoverageRequirementResult(
            requirement_id=requirement.requirement_id,
            unit_id=requirement.unit_id,
            kind=requirement.kind,
            required=requirement.required,
            status=status,
            expected_count=len(expected_keys) or max(1, len(rows)),
            covered_count=valid_rows,
            missing_count=missing_count,
            unsupported_count=1 if status == "unsupported" else 0,
            duplicate_count=len(duplicate_keys),
            missing_row_keys=missing_keys,
            unexpected_row_keys=unexpected_keys,
            missing_columns=sorted(missing_columns),
            evidence_ids=sorted(evidence_ids),
            issues=issues,
        )

    def _evaluate_cross_unit(
        self,
        requirement: CoverageRequirement,
        drafts: dict[str, DocumentUnitDraft],
    ) -> CoverageRequirementResult:
        observations: dict[str, list[str]] = {}
        evidence_ids: set[str] = set()
        for draft in drafts.values():
            for assertion in draft.assertions:
                if assertion.consistency_key != requirement.unit_id:
                    continue
                value = str(assertion.value if assertion.value is not None else assertion.text).strip()
                if value:
                    observations.setdefault(value, []).append(draft.unit_id)
                    evidence_ids.update(assertion.evidence_ids)
        issues: list[ReviewIssue] = []
        if len(observations) > 1:
            issues.append(self._issue(
                "cross_unit_conflict", requirement,
                evidence_ids=sorted(evidence_ids), suggested_action="resolve_cross_unit_conflict",
            ))
            status = "conflicting"
        elif not observations:
            issues.append(self._issue(
                "missing_cross_unit_fact", requirement,
                suggested_action="retrieve_cross_unit_evidence",
            ))
            status = "missing"
        else:
            status = "complete"
        return CoverageRequirementResult(
            requirement_id=requirement.requirement_id,
            unit_id=requirement.unit_id,
            kind=requirement.kind,
            required=requirement.required,
            status=status,
            expected_count=1,
            covered_count=1 if status == "complete" else 0,
            missing_count=1 if status == "missing" else 0,
            unsupported_count=0,
            evidence_ids=sorted(evidence_ids),
            issues=issues,
        )

    @staticmethod
    def _issue(
        code: str,
        requirement: CoverageRequirement,
        *,
        row_key: str | None = None,
        column_id: str | None = None,
        evidence_ids: list[str] | None = None,
        suggested_action: str = "",
    ) -> ReviewIssue:
        severity = "warning" if not requirement.required else "error"
        return ReviewIssue(
            stage="coverage",
            code=code,
            severity=severity,
            blocking=requirement.required,
            unit_id=requirement.unit_id,
            row_key=row_key,
            column_id=column_id,
            evidence_ids=list(evidence_ids or []),
            suggested_action=suggested_action,
        )


def _contract_and_identity(plan: Any) -> tuple[CoverageContract, str, int | None, str]:
    if isinstance(plan, CoverageContract):
        return plan, "", None, ""
    if isinstance(plan, Mapping):
        raw_contract = plan.get("coverage_contract", plan)
        contract = CoverageContract.model_validate(raw_contract)
        return (
            contract,
            str(plan.get("document_plan_id") or plan.get("plan_id") or ""),
            plan.get("version") or plan.get("plan_version"),
            str(plan.get("plan_hash") or ""),
        )
    contract = getattr(plan, "coverage_contract", None)
    if contract is None:
        raise TypeError("coverage evaluation requires a CoverageContract or DocumentPlan")
    return (
        contract if isinstance(contract, CoverageContract) else CoverageContract.model_validate(contract),
        str(getattr(plan, "document_plan_id", "") or ""),
        getattr(plan, "version", None),
        str(getattr(plan, "plan_hash", "") or ""),
    )


def _draft_map(drafts: Mapping[str, DocumentUnitDraft] | Sequence[DocumentUnitDraft]) -> dict[str, DocumentUnitDraft]:
    values = drafts.values() if isinstance(drafts, Mapping) else drafts
    result: dict[str, DocumentUnitDraft] = {}
    for draft in values:
        if not isinstance(draft, DocumentUnitDraft):
            draft = DocumentUnitDraft.model_validate(draft)
        result[draft.unit_id] = draft
        result[draft.unit_id.removeprefix("field:").removeprefix("review:")] = draft
    return result


def _draft_for_unit(drafts: dict[str, DocumentUnitDraft], unit_id: str) -> DocumentUnitDraft | None:
    return drafts.get(unit_id) or drafts.get(unit_id.removeprefix("field:").removeprefix("review:"))


def _evidence_map(value: Mapping[str, Any] | Sequence[Any] | None) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        items = value.items()
    else:
        items = ((getattr(item, "id", None) or getattr(item, "evidence_id", None), item) for item in value)
    result: dict[str, dict[str, Any]] = {}
    for key, item in items:
        evidence_id = str(key or getattr(item, "id", None) or getattr(item, "evidence_id", None) or "").strip()
        if not evidence_id:
            continue
        if isinstance(item, Mapping):
            result[evidence_id] = dict(item)
        else:
            result[evidence_id] = {
                "id": evidence_id,
                "content": getattr(item, "content", ""),
                "metadata": getattr(item, "metadata", {}) or {},
            }
    return result
