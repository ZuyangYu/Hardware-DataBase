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
        return [
            {"kind": "cross_unit_conflict", "consistency_key": key, "values": by_value}
            for key, by_value in values.items() if len(by_value) > 1
        ]


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
