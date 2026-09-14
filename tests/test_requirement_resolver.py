from __future__ import annotations

import json

from src.agents.claim_evidence import InformationRequirement
from src.document_authoring.models import DocumentFieldSchema, DocumentSchema, EvidenceMatrixRow
from src.document_authoring.requirement_resolver import RequirementResolver
from src.pipelines.document_rag.schemas import Evidence


def _schema(*fields: DocumentFieldSchema) -> DocumentSchema:
    return DocumentSchema(
        document_schema_id="schema-requirements",
        version="1",
        document_type="hardware-review",
        fields=list(fields),
        status="approved",
    )


def _field(
    field_id: str,
    label: str,
    *,
    required: bool = True,
    missing_policy: str = "mark_tbd",
    allow_derivation: bool = False,
    query_terms: list[str] | None = None,
) -> DocumentFieldSchema:
    return DocumentFieldSchema(
        field_id=field_id,
        label=label,
        required=required,
        missing_policy=missing_policy,
        allow_derivation=allow_derivation,
        query_terms=query_terms or [],
        retrieval_policy_id="retrieval-v1",
        verification_policy_id="verification-v1",
    )


def test_missing_required_field_returns_structured_requirement_and_uses_field_policy():
    result = RequirementResolver().resolve(
        document_schema=_schema(
            _field("pcb_revision", "PCB Revision", missing_policy="block_section"),
        ),
        evidence=[],
    )

    assert len(result.unresolved_requirements) == 1
    requirement = result.unresolved_requirements[0]
    assert requirement.field == "pcb_revision"
    assert requirement.reason == "no_evidence"
    assert "missing_evidence" in requirement.reason_codes
    assert "required_field" in requirement.reason_codes
    assert requirement.evidence_coverage.status == "missing"
    assert requirement.evidence_coverage.coverage_ratio == 0.0
    assert requirement.evidence_coverage.evidence_ids == ()
    assert requirement.candidate_values == ()
    assert requirement.risk_level == "high"
    assert requirement.required is True
    assert requirement.default_policy == "block_section"
    assert requirement.allowed_default_strategy == "block_section"
    assert requirement.requires_clarification is False


def test_multiple_structured_evidence_candidates_require_clarification():
    result = RequirementResolver().resolve(
        document_schema=_schema(_field("pcb_revision", "PCB Revision")),
        evidence=[
            {
                "id": "e-a3",
                "field_id": "pcb_revision",
                "normalized_value": "A3",
                "source_name": "release-a.pdf",
            },
            {
                "evidence_id": "e-a4",
                "target_id": "pcb_revision",
                "value": "A4",
                "source_name": "release-b.pdf",
            },
        ],
    )

    requirement = result.unresolved_requirements[0]
    assert requirement.reason == "multiple_candidates"
    assert requirement.reason_codes == ("multiple_candidates", "required_field")
    assert requirement.evidence_coverage.status == "conflicting"
    assert requirement.evidence_coverage.coverage_ratio == 1.0
    assert requirement.evidence_coverage.evidence_ids == ("e-a3", "e-a4")
    assert requirement.candidate_values == ("A3", "A4")
    assert requirement.risk_level == "high"
    assert requirement.requires_clarification is True


def test_project_context_value_resolves_field_without_evidence():
    result = RequirementResolver().resolve(
        document_schema=_schema(_field("pcb_revision", "PCB Revision")),
        project_context={"fields": {"pcb_revision": "A4"}},
        evidence=[],
    )

    assert result.unresolved_requirements == []
    assert result.resolved_fields == {"pcb_revision": "A4"}


def test_existing_evidence_object_and_content_anchor_provide_single_candidate():
    result = RequirementResolver().resolve(
        document_schema=_schema(
            _field("pcb_revision", "PCB Revision", query_terms=["revision"]),
        ),
        evidence=[
            Evidence(
                id="e-a3",
                content="PCB Revision: A3",
                source_name="release-a.pdf",
            ),
        ],
    )

    assert result.unresolved_requirements == []
    assert result.resolved_fields == {"pcb_revision": "A3"}


def test_evidence_matrix_row_is_compatible_with_field_contract():
    matrix_row = EvidenceMatrixRow(
        field_id="pcb_revision",
        requirement=InformationRequirement(
            requirement_id="req-1",
            semantic_unit_id="field:pcb_revision",
            claim_type="attribute",
            subject="PCB Revision",
        ),
        evidence_ids=["e-a3"],
        coverage_status="supported",
        normalized_value="A3",
        display_value="A3",
    )

    result = RequirementResolver().resolve(
        document_schema=_schema(_field("pcb_revision", "PCB Revision")),
        evidence=[matrix_row],
    )

    assert result.unresolved_requirements == []
    assert result.resolved_fields == {"pcb_revision": "A3"}


def test_context_and_evidence_conflict_is_explicit_and_json_safe():
    result = RequirementResolver().resolve(
        document_schema=_schema(_field("pcb_revision", "PCB Revision")),
        project_context={"field_values": {"pcb_revision": "A3"}},
        evidence=[
            {"id": "e-a4", "field_id": "pcb_revision", "value": "A4"},
        ],
    )

    requirement = result.unresolved_requirements[0]
    assert requirement.reason == "context_evidence_conflict"
    assert "context_evidence_conflict" in requirement.reason_codes
    assert requirement.evidence_coverage.status == "conflicting"
    assert requirement.candidate_values == ("A3", "A4")
    assert requirement.requires_clarification is True

    serialized = result.to_dict()
    assert serialized["unresolved_requirements"][0]["field"] == "pcb_revision"
    assert serialized["unresolved_requirements"][0]["evidence_coverage"]["status"] == "conflicting"
    json.dumps(serialized, ensure_ascii=False, sort_keys=True)


def test_optional_missing_field_is_reported_but_does_not_require_user_input():
    result = RequirementResolver().resolve(
        document_schema=_schema(
            _field(
                "review_note",
                "Review Note",
                required=False,
                missing_policy="optional",
            ),
        ),
        evidence=[],
    )

    requirement = result.unresolved_requirements[0]
    assert requirement.required is False
    assert requirement.default_policy == "optional"
    assert requirement.allowed_default_strategy == "keep_blank"
    assert requirement.requires_clarification is False
    assert requirement.risk_level == "low"

