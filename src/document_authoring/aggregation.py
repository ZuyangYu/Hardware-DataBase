"""Deterministic aggregation of accepted unit drafts into ``DocumentModel``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from src.document_authoring.models import DocumentUnitDraft, TypedTableRow, content_hash
from src.document_authoring.planning.models import CoverageRequirement
from src.document_authoring.planning.review_contracts import ReviewIssue, UnitReviewResult

from .document_model import (
    Block,
    Citation,
    DocumentModel,
    ListBlock,
    MissingItem,
    ParagraphBlock,
    SectionBlock,
    TypedTableBlock,
)


class DocumentAggregator:
    """Build one format-neutral document without retrieval or model calls."""

    def aggregate(
        self,
        plan: Any,
        accepted_drafts: Mapping[str, DocumentUnitDraft] | Sequence[DocumentUnitDraft],
        reviews: Mapping[str, UnitReviewResult | Mapping[str, Any]] | None = None,
    ) -> DocumentModel:
        drafts = _draft_map(accepted_drafts)
        review_map = _review_map(reviews)
        requirements = _requirements(plan)
        semantic_units = list(getattr(plan, "semantic_units", []) or [])
        if isinstance(plan, Mapping):
            semantic_units = list(plan.get("semantic_units") or [])
        blocks: list[Block] = []
        citations: list[Citation] = []
        missing_items: list[MissingItem] = []
        issues: list[ReviewIssue] = []
        draft_hashes: dict[str, str] = {}
        review_hashes: dict[str, str] = {}

        for unit in semantic_units:
            unit_id = str(_value(unit, "unit_id", "")).strip()
            if not unit_id:
                continue
            normalized_unit_id = unit_id.removeprefix("field:").removeprefix("review:")
            draft = _draft_for_unit(drafts, normalized_unit_id)
            review = _review_for_unit(review_map, normalized_unit_id)
            if draft is not None:
                draft_hashes[normalized_unit_id] = content_hash(draft.model_dump(mode="json"))
            if review is not None:
                review_hashes[normalized_unit_id] = str(_value(review, "report_hash", ""))
            if review is not None and str(_value(review, "status", "")) != "pass":
                review_issues = _review_issues(review)
                issues.extend(review_issues)
                missing = _missing_from_review(normalized_unit_id, review, requirements.get(normalized_unit_id))
                missing_items.extend(missing)
                continue
            if draft is None:
                missing_items.extend(_missing_for_requirement(normalized_unit_id, requirements.get(normalized_unit_id)))
                continue
            typed = draft.typed_value
            requirement = requirements.get(normalized_unit_id)
            if _is_table_unit(unit, typed, requirement):
                block, table_missing, table_issues = self._table_block(
                    normalized_unit_id, draft, requirement,
                )
                blocks.append(block)
                missing_items.extend(table_missing)
                issues.extend(table_issues)
                citations.extend(_citations_for_draft(draft))
                continue
            display = typed.display_value if typed is not None else (draft.content or "")
            if not str(display).strip():
                missing_items.extend(_missing_for_requirement(normalized_unit_id, requirement))
                continue
            block_kind = str(_value(unit, "kind", "paragraph"))
            if block_kind == "section":
                blocks.append(SectionBlock(
                    block_id=f"block:{normalized_unit_id}", unit_id=normalized_unit_id,
                    title=str(_value(unit, "title", "") or ""), content=str(display),
                    citations=_citations_for_draft(draft),
                ))
            elif block_kind == "list":
                items = [item.strip() for item in str(display).split(",") if item.strip()]
                blocks.append(ListBlock(
                    block_id=f"block:{normalized_unit_id}", unit_id=normalized_unit_id,
                    items=items, citations=_citations_for_draft(draft),
                ))
            else:
                blocks.append(ParagraphBlock(
                    block_id=f"block:{normalized_unit_id}", unit_id=normalized_unit_id,
                    content=str(display), citations=_citations_for_draft(draft),
                ))
            citations.extend(_citations_for_draft(draft))

        # If a plan adapter exposes drafts without semantic unit rows, retain
        # them in stable id order rather than silently dropping accepted facts.
        known_units = {
            str(_value(unit, "unit_id", "")).removeprefix("field:").removeprefix("review:")
            for unit in semantic_units
        }
        for unit_id in sorted(set(drafts) - known_units):
            if unit_id.startswith("field:") or unit_id.startswith("review:"):
                continue
            missing_items.append(MissingItem(
                unit_id=unit_id, reason="draft is not present in the accepted plan",
                required=True, issue_code="unexpected_draft",
            ))

        plan_id = str(_value(plan, "document_plan_id", _value(plan, "plan_id", "")) or "")
        plan_version = _value(plan, "version", _value(plan, "plan_version", None))
        plan_hash = str(_value(plan, "plan_hash", "") or "")
        return DocumentModel(
            document_id=f"document:{plan_id}:v{plan_version}" if plan_id else "document:unplanned",
            plan_id=plan_id,
            plan_version=plan_version,
            plan_hash=plan_hash,
            blocks=blocks,
            citations=_dedupe_citations(citations),
            missing_items=missing_items,
            issues=_dedupe_issues(issues),
            draft_hashes=draft_hashes,
            review_hashes=review_hashes,
        )

    def _table_block(
        self,
        unit_id: str,
        draft: DocumentUnitDraft,
        requirement: CoverageRequirement | None,
    ) -> tuple[TypedTableBlock, list[MissingItem], list[ReviewIssue]]:
        typed = draft.typed_value
        if typed is None or typed.kind != "table":
            missing = _missing_for_requirement(unit_id, requirement)
            issue = ReviewIssue(
                stage="unit_review", code="table_scalarized", unit_id=unit_id,
                suggested_action="return_typed_table_rows",
            )
            return TypedTableBlock(
                block_id=f"block:{unit_id}", unit_id=unit_id,
                columns=list(getattr(requirement, "required_columns", []) or []),
                rows=[], expected_row_keys=list(getattr(requirement, "row_keys", []) or []),
                missing_cells=missing,
            ), missing, [issue]

        expected_keys = list(getattr(requirement, "row_keys", []) or [])
        required_columns = list(getattr(requirement, "required_columns", []) or [])
        columns = list(required_columns)
        for row in typed.rows:
            for column in row.cells:
                if column not in columns:
                    columns.append(column)
        rows = list(typed.rows)
        if expected_keys:
            order = {key: index for index, key in enumerate(expected_keys)}
            rows.sort(key=lambda row: (order.get(row.row_key, len(order)), row.row_key))
        elif getattr(requirement, "row_order", "input") == "stable_key":
            rows.sort(key=lambda row: row.row_key)

        missing: list[MissingItem] = []
        issues: list[ReviewIssue] = []
        seen_keys: set[str] = set()
        retained: list[TypedTableRow] = []
        expected_set = set(expected_keys)
        for row in rows:
            row_key = row.row_key.strip()
            if row_key and row_key in seen_keys:
                issues.append(ReviewIssue(
                    stage="unit_review", code="duplicate_row_key", unit_id=unit_id,
                    row_key=row_key, suggested_action="deduplicate_row_identity",
                ))
                continue
            if row_key:
                seen_keys.add(row_key)
            retained.append(row)
            if expected_keys and row_key not in expected_set:
                issues.append(ReviewIssue(
                    stage="unit_review", code="unexpected_row", unit_id=unit_id,
                    row_key=row_key or None, suggested_action="remove_row_outside_frozen_scope",
                ))
            for column in required_columns:
                if column not in row.cells or not str(row.cells.get(column) or "").strip():
                    missing.append(MissingItem(
                        unit_id=unit_id, row_key=row_key or None, column_id=column,
                        reason="required table cell is missing", required=True,
                        issue_code="missing_required_cell",
                    ))
                    issues.append(ReviewIssue(
                        stage="unit_review", code="missing_required_column", unit_id=unit_id,
                        row_key=row_key or None, column_id=column,
                        suggested_action="return_required_table_cell",
                    ))
        if expected_keys:
            for key in expected_keys:
                if key not in seen_keys:
                    missing.append(MissingItem(
                        unit_id=unit_id, row_key=key,
                        reason="required row is missing", required=True,
                        issue_code="missing_row",
                    ))
                    issues.append(ReviewIssue(
                        stage="unit_review", code="missing_row", unit_id=unit_id,
                        row_key=key, suggested_action="retrieve_and_draft_missing_row",
                    ))
        return TypedTableBlock(
            block_id=f"block:{unit_id}", unit_id=unit_id,
            columns=columns, rows=retained, expected_row_keys=expected_keys,
            missing_cells=missing, citations=_citations_for_draft(draft),
        ), missing, issues


def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _requirements(plan: Any) -> dict[str, CoverageRequirement]:
    contract = _value(plan, "coverage_contract", None)
    raw = _value(contract, "requirements", []) if contract is not None else []
    result: dict[str, CoverageRequirement] = {}
    for item in raw or []:
        requirement = item if isinstance(item, CoverageRequirement) else CoverageRequirement.model_validate(item)
        result[requirement.unit_id] = requirement
    return result


def _draft_map(drafts: Mapping[str, DocumentUnitDraft] | Sequence[DocumentUnitDraft]) -> dict[str, DocumentUnitDraft]:
    values = drafts.values() if isinstance(drafts, Mapping) else drafts
    result: dict[str, DocumentUnitDraft] = {}
    for draft in values:
        if not isinstance(draft, DocumentUnitDraft):
            draft = DocumentUnitDraft.model_validate(draft)
        normalized = draft.unit_id.removeprefix("field:").removeprefix("review:")
        result[draft.unit_id] = draft
        result[normalized] = draft
    return result


def _draft_for_unit(drafts: Mapping[str, DocumentUnitDraft], unit_id: str) -> DocumentUnitDraft | None:
    return drafts.get(unit_id) or drafts.get(f"field:{unit_id}") or drafts.get(f"review:{unit_id}")


def _review_map(reviews: Mapping[str, UnitReviewResult | Mapping[str, Any]] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in (reviews or {}).items():
        normalized = str(key).removeprefix("field:").removeprefix("review:")
        result[str(key)] = value
        result[normalized] = value
    return result


def _review_for_unit(reviews: Mapping[str, Any], unit_id: str) -> Any | None:
    return reviews.get(unit_id) or reviews.get(f"field:{unit_id}") or reviews.get(f"review:{unit_id}")


def _is_table_unit(unit: Any, typed: Any, requirement: CoverageRequirement | None) -> bool:
    return (
        str(_value(unit, "kind", "")).strip() == "table"
        or str(_value(typed, "kind", "")).strip() == "table"
        or str(_value(requirement, "kind", "")).strip() == "table"
    )


def _citations_for_draft(draft: DocumentUnitDraft) -> list[Citation]:
    citations: list[Citation] = []
    if draft.evidence_ids:
        citations.append(Citation(evidence_ids=list(dict.fromkeys(draft.evidence_ids))))
    for assertion in draft.assertions:
        if assertion.evidence_ids:
            citations.append(Citation(
                evidence_ids=list(dict.fromkeys(assertion.evidence_ids)),
                claim_id=assertion.claim_id,
            ))
    if draft.typed_value is not None and draft.typed_value.evidence_ids:
        citations.append(Citation(evidence_ids=list(dict.fromkeys(draft.typed_value.evidence_ids))))
    return citations


def _dedupe_citations(citations: list[Citation]) -> list[Citation]:
    seen: set[tuple[tuple[str, ...], str | None]] = set()
    result: list[Citation] = []
    for citation in citations:
        key = (tuple(citation.evidence_ids), citation.claim_id)
        if key not in seen:
            seen.add(key)
            result.append(citation)
    return result


def _dedupe_issues(issues: list[ReviewIssue]) -> list[ReviewIssue]:
    seen: set[str] = set()
    result: list[ReviewIssue] = []
    for issue in issues:
        if issue.issue_id not in seen:
            seen.add(issue.issue_id)
            result.append(issue)
    return result


def _review_issues(review: Any) -> list[ReviewIssue]:
    values = _value(review, "issues", []) or []
    result: list[ReviewIssue] = []
    for issue in values:
        result.append(issue if isinstance(issue, ReviewIssue) else ReviewIssue.model_validate(issue))
    return result


def _missing_from_review(
    unit_id: str,
    review: Any,
    requirement: CoverageRequirement | None,
) -> list[MissingItem]:
    result: list[MissingItem] = []
    for issue in _review_issues(review):
        if issue.code in {"missing_row", "row_identity_missing"} and issue.row_key:
            result.append(MissingItem(
                unit_id=unit_id, row_key=issue.row_key,
                reason=issue.suggested_action or issue.code,
                required=True, issue_code=issue.code,
            ))
        elif issue.code in {"missing_required_column", "empty_required_cell"} and issue.column_id:
            result.append(MissingItem(
                unit_id=unit_id, row_key=issue.row_key, column_id=issue.column_id,
                reason=issue.suggested_action or issue.code,
                required=True, issue_code=issue.code,
            ))
    return result or _missing_for_requirement(unit_id, requirement)


def _missing_for_requirement(unit_id: str, requirement: CoverageRequirement | None) -> list[MissingItem]:
    if requirement is not None and requirement.kind == "table" and requirement.row_keys:
        return [MissingItem(
            unit_id=unit_id, row_key=key, reason="required row is missing", required=requirement.required,
            issue_code="missing_row",
        ) for key in requirement.row_keys]
    return [MissingItem(
        unit_id=unit_id, reason="required semantic value is missing",
        required=bool(requirement.required) if requirement is not None else True,
        issue_code="missing_requirement",
    )]

