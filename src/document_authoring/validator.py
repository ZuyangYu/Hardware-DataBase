"""Deterministic validation for P2a Evidence Matrix and render output."""

from __future__ import annotations

import uuid
import re
from typing import Any

from src.document_authoring.models import (
    DocumentUnitDraft,
    LegacyTemplateClaim,
    ValidationReport,
    content_hash,
)


class DocumentValidator:
    def validate(
        self,
        *,
        work_order_id: str,
        matrix_rows: list[dict[str, Any]],
        integrity_manifest: dict[str, Any],
        additional_issues: list[dict[str, Any]] | None = None,
    ) -> ValidationReport:
        issues: list[dict[str, Any]] = []
        for row in matrix_rows:
            status = row.get("coverage_status", "unsearched")
            if status in {"retrieval_failed", "access_denied", "source_unavailable", "partial_failure"}:
                issues.append({"unit_id": row.get("review_item_id") or row.get("field_id"), "kind": status})
            if status == "conflicting":
                issues.append({"unit_id": row.get("review_item_id") or row.get("field_id"), "kind": "conflicting"})
        for violation in integrity_manifest.get("policy_violations", []):
            issues.append({"kind": "renderer_integrity", "message": violation})
        for violation in integrity_manifest.get("cell_policy_violations", []):
            issues.append({"kind": "renderer_integrity", "message": violation})
        issues.extend(additional_issues or [])
        status = "failed" if any(issue["kind"] == "renderer_integrity" for issue in issues) else (
            "requires_human" if issues else "passed"
        )
        return ValidationReport(
            validation_report_id=f"vr-{uuid.uuid4().hex}",
            work_order_id=work_order_id,
            status=status,
            issues=issues,
            evidence_matrix_hash=content_hash(matrix_rows),
            renderer_manifest_hash=integrity_manifest.get("manifest_hash"),
        )

    def validate_unit_draft(
        self,
        draft: DocumentUnitDraft,
        evidence_by_id: dict[str, dict[str, Any]],
    ) -> DocumentUnitDraft:
        """Validate draft assertions against the exact allowed evidence set.

        This is a deterministic guard, not a claim of engineering correctness.
        It rejects unreferenced assertions and requires a basic lexical anchor
        in the cited evidence before a semantic-provider extension is allowed
        to mark the draft supported.
        """
        notes: list[str] = []
        declared_ids = set(draft.evidence_ids)
        if not declared_ids:
            notes.append("draft has no evidence ids")
        for assertion in draft.assertions:
            assertion_ids = set(assertion.evidence_ids)
            if not assertion_ids:
                notes.append(f"assertion {assertion.assertion_id} has no evidence ids")
                continue
            if not assertion_ids <= declared_ids:
                notes.append(f"assertion {assertion.assertion_id} references evidence outside its draft package")
            unknown = assertion_ids - set(evidence_by_id)
            if unknown:
                notes.append(f"assertion {assertion.assertion_id} references unknown evidence: {sorted(unknown)}")
                continue
            if not any(_has_lexical_anchor(assertion.text, str(evidence_by_id[item].get("content") or "")) for item in assertion_ids):
                notes.append(f"assertion {assertion.assertion_id} has no lexical evidence anchor")
        status = "supported" if not notes else "unsupported"
        return draft.model_copy(update={"validation_status": status, "validation_notes": notes})

    def validate_typed_field_draft(
        self,
        draft: DocumentUnitDraft,
        evidence_by_id: dict[str, dict[str, Any]],
        *,
        expected_value_type: str,
        table_requirement: Any | None = None,
        expected_row_keys: list[str] | None = None,
        required_columns: list[str] | None = None,
        row_order: str | None = None,
        duplicate_policy: str | None = None,
    ) -> DocumentUnitDraft:
        """Require an evidence-backed, type-compatible value before filling.

        ``content`` remains reviewable prose.  It is intentionally never used
        as a substitute for ``typed_value`` because a complete evidence chunk
        is not necessarily a safe value for a scalar template cell.
        """
        base = self.validate_unit_draft(draft, evidence_by_id)
        notes = list(base.validation_notes)
        typed = base.typed_value
        table_contract = _table_contract_values(
            table_requirement,
            expected_row_keys=expected_row_keys,
            required_columns=required_columns,
            row_order=row_order,
            duplicate_policy=duplicate_policy,
        )
        if typed is None:
            notes.append("draft has no typed field value")
        else:
            normalized_values = _unique_values(typed.normalized_values)
            expected_kind = _expected_typed_kind(expected_value_type)
            if expected_kind is None:
                notes.append(f"unsupported field value type: {expected_value_type}")
            elif typed.kind != expected_kind:
                notes.append(
                    f"typed value kind {typed.kind} does not match expected {expected_kind}"
                )
            if not typed.display_value.strip():
                notes.append("typed value display value is empty")
            if not typed.evidence_ids:
                notes.append("typed value has no evidence ids")
            unknown = set(typed.evidence_ids) - set(evidence_by_id)
            if unknown:
                notes.append(f"typed value references unknown evidence: {sorted(unknown)}")
            if set(typed.evidence_ids) - set(base.evidence_ids):
                notes.append("typed value references evidence outside its draft package")
            for evidence_id in typed.evidence_ids:
                metadata = evidence_by_id.get(evidence_id, {}).get("metadata") or {}
                if metadata.get("low_confidence") or metadata.get("reused"):
                    notes.append(f"typed value uses non-auto-fill evidence: {evidence_id}")
            if typed.kind == "scalar" and len(normalized_values) != 1:
                notes.append("scalar typed value requires one unique normalized value")
            if typed.kind == "enumeration" and not normalized_values:
                notes.append("enumeration typed value requires at least one normalized value")
            if typed.kind == "table":
                if not typed.rows:
                    notes.append("typed table requires at least one row")
                expected_rows = table_contract["expected_row_keys"]
                expected_columns = table_contract["required_columns"]
                contract_row_order = table_contract["row_order"]
                contract_duplicate_policy = table_contract["duplicate_policy"]
                seen_row_keys: set[str] = set()
                seen_rows = set()
                row_keys: list[str] = []
                for index, row in enumerate(typed.rows):
                    row_key = row.row_key.strip()
                    row_keys.append(row_key)
                    if expected_rows and not row_key:
                        notes.append(f"table row {index} is missing row key")
                    if row_key:
                        if row_key in seen_row_keys and contract_duplicate_policy == "reject":
                            notes.append(f"table row {index} duplicates row key {row_key}")
                        seen_row_keys.add(row_key)
                    elif expected_rows:
                        # An empty key is deliberately never derived from the
                        # row's display cells.  The missing-key issue above is
                        # the complete result for this row.
                        pass

                    ids = set(row.evidence_ids)
                    cell_evidence = {
                        str(column): set(values)
                        for column, values in row.cell_evidence_ids.items()
                    }
                    union_ids = ids | set().union(*cell_evidence.values()) if cell_evidence else ids
                    if (
                        not ids
                        or not union_ids <= set(typed.evidence_ids)
                        or not union_ids <= set(base.evidence_ids)
                        or not union_ids <= set(evidence_by_id)
                    ):
                        notes.append(f"table row {index} requires allowed row evidence")
                        continue

                    unknown_cell_columns = set(cell_evidence) - set(row.cells)
                    if unknown_cell_columns:
                        notes.append(
                            f"table row {index} has cell evidence for unknown columns: "
                            f"{sorted(unknown_cell_columns)}"
                        )
                    outside_row = union_ids - ids
                    if outside_row:
                        notes.append(
                            f"table row {index} cell evidence is outside row evidence: "
                            f"{sorted(outside_row)}"
                        )

                    missing_columns = set(expected_columns) - set(row.cells)
                    unknown_columns = set(row.cells) - set(expected_columns) if expected_columns else set()
                    if missing_columns:
                        notes.append(
                            f"table row {index} is missing required columns: "
                            f"{sorted(missing_columns)}"
                        )
                    if unknown_columns:
                        notes.append(
                            f"table row {index} contains unknown columns: "
                            f"{sorted(unknown_columns)}"
                        )
                    if not row.cells or any(not str(value).strip() for value in row.cells.values()):
                        notes.append(f"table row {index} contains empty cells")
                    for column, value in row.cells.items():
                        support_ids = cell_evidence.get(column, ids)
                        if not support_ids:
                            notes.append(
                                f"table row {index} column {column} has no cell evidence"
                            )
                            continue
                        if not support_ids <= ids:
                            # Keep the ownership failure separate from the
                            # lexical failure so reviewers can locate it.
                            continue
                        texts = [
                            str(evidence_by_id[key].get("content") or "")
                            for key in support_ids
                        ]
                        pattern = r"(?<![A-Za-z0-9_.])" + re.escape(value.strip()) + r"(?![A-Za-z0-9_.])"
                        if value.strip() and not any(re.search(pattern, text, flags=re.IGNORECASE) for text in texts):
                            notes.append(f"table row {index} column {column} is not supported by its row evidence")
                        for evidence_id in support_ids:
                            metadata = evidence_by_id.get(evidence_id, {}).get("metadata") or {}
                            if metadata.get("low_confidence") or metadata.get("reused"):
                                notes.append(
                                    f"table row {index} column {column} uses non-auto-fill evidence: "
                                    f"{evidence_id}"
                                )
                    # Legacy rows have no server-owned identity.  Retain the
                    # old duplicate-cell guard for that compatibility shape;
                    # distinct explicit row keys are allowed to share display
                    # values because identity, not prose, distinguishes them.
                    if not row_key:
                        signature = tuple(sorted(row.cells.items()))
                        if signature in seen_rows and contract_duplicate_policy == "reject":
                            notes.append(f"table row {index} duplicates an earlier record")
                        seen_rows.add(signature)
                if expected_rows:
                    actual = set(row_keys)
                    missing = [key for key in expected_rows if key not in actual]
                    unexpected = [key for key in row_keys if key and key not in set(expected_rows)]
                    if missing:
                        notes.append(f"table is missing row keys: {missing}")
                    if unexpected:
                        notes.append(f"table contains unexpected row keys: {unexpected}")
                    if contract_row_order == "declared" and [key for key in row_keys if key] != expected_rows:
                        notes.append("table rows are not in declared row-key order")
                elif contract_row_order == "stable_key":
                    keyed = [key for key in row_keys if key]
                    if keyed != sorted(keyed):
                        notes.append("table rows are not in stable row-key order")
            typed = typed.model_copy(update={"normalized_values": normalized_values})

        return base.model_copy(update={
            "typed_value": typed,
            "validation_status": "supported" if not notes else "unsupported",
            "validation_notes": notes,
        })

    @staticmethod
    def detect_template_contamination(
        draft: DocumentUnitDraft,
        legacy_claims: list[LegacyTemplateClaim],
    ) -> list[dict[str, Any]]:
        haystack = "\n".join(filter(None, [draft.content or "", *(assertion.text for assertion in draft.assertions)])).casefold()
        findings: list[dict[str, Any]] = []
        for claim in legacy_claims:
            candidate = claim.text.strip()
            if claim.prohibited_as_project_evidence and len(candidate) >= 3 and candidate.casefold() in haystack:
                findings.append({
                    "kind": "template_contamination", "unit_id": draft.unit_id,
                    "legacy_claim_id": claim.claim_id, "locator": claim.locator,
                })
        return findings

    @staticmethod
    def validate_cross_unit_consistency(drafts: list[DocumentUnitDraft]) -> list[dict[str, Any]]:
        values: dict[str, dict[str, list[str]]] = {}
        for draft in drafts:
            for assertion in draft.assertions:
                if not assertion.consistency_key:
                    continue
                value = str(assertion.value if assertion.value is not None else assertion.text).strip()
                if value:
                    values.setdefault(assertion.consistency_key, {}).setdefault(value, []).append(draft.unit_id)
        conflicts = []
        for key, by_value in values.items():
            if len(by_value) <= 1:
                continue
            distinct_values = list(by_value)
            if all(
                _consistency_values_compatible(left, right)
                for index, left in enumerate(distinct_values)
                for right in distinct_values[index + 1:]
            ):
                # Multiple assertions for one field may be complementary
                # views of the same fact (for example a pin and its net).
                continue
            conflicts.append({"kind": "cross_unit_conflict", "consistency_key": key, "values": by_value})
        return conflicts


def _has_lexical_anchor(assertion_text: str, evidence_text: str) -> bool:
    assertion_tokens = {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{1,}|[\u4e00-\u9fff]{2,}", assertion_text)
    }
    evidence_tokens = {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{1,}|[\u4e00-\u9fff]{2,}", evidence_text)
    }
    return bool(assertion_tokens & evidence_tokens)


def _consistency_values_compatible(left: str, right: str) -> bool:
    """Treat assertions sharing a meaningful anchor as complementary facts."""
    stopwords = {
        "a", "an", "and", "at", "by", "for", "in", "is", "net", "of",
        "on", "the", "to", "with", "连接", "位于", "为", "在", "和",
    }
    left_tokens = {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{1,}|[\u4e00-\u9fff]{2,}", left)
        if token.casefold() not in stopwords
    }
    right_tokens = {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{1,}|[\u4e00-\u9fff]{2,}", right)
        if token.casefold() not in stopwords
    }
    shared = left_tokens & right_tokens
    return any(len(token) >= 3 or any(char.isdigit() for char in token) for token in shared)


def _expected_typed_kind(value_type: str) -> str | None:
    normalized = value_type.strip().casefold()
    if normalized in {"table", "repeating_table"}:
        return "table"
    if normalized in {
        "text", "string", "scalar", "number", "integer", "float", "date",
        "datetime", "boolean", "bool",
    }:
        return "scalar"
    if normalized in {"enum", "enumeration", "list", "set", "array", "multi_enum"}:
        return "enumeration"
    return None


def _table_contract_values(
    table_requirement: Any | None,
    *,
    expected_row_keys: list[str] | None,
    required_columns: list[str] | None,
    row_order: str | None,
    duplicate_policy: str | None,
) -> dict[str, Any]:
    """Normalize the optional table contract without importing planning code."""

    def value(name: str, fallback: Any):
        if table_requirement is None:
            return fallback
        if isinstance(table_requirement, dict):
            return table_requirement.get(name, fallback)
        return getattr(table_requirement, name, fallback)

    expected = expected_row_keys if expected_row_keys is not None else value("row_keys", [])
    columns = required_columns if required_columns is not None else value("required_columns", [])
    order = row_order if row_order is not None else value("row_order", "input")
    duplicates = duplicate_policy if duplicate_policy is not None else value("duplicate_policy", "reject")
    return {
        "expected_row_keys": [str(item).strip() for item in expected or [] if str(item).strip()],
        "required_columns": [str(item).strip() for item in columns or [] if str(item).strip()],
        "row_order": str(order or "input").strip(),
        "duplicate_policy": str(duplicates or "reject").strip(),
    }


def _unique_values(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized.casefold() not in seen:
            result.append(normalized)
            seen.add(normalized.casefold())
    return result
