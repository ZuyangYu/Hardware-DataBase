from __future__ import annotations

from types import SimpleNamespace

from src.core.app_pipeline import AppPipeline
from src.document_authoring.models import DocumentFieldSchema, DocumentSchema
from src.document_authoring.requirement_clarifier import RequirementClarifier
from src.pipelines.document_rag.schemas import RequestContext


def _ctx() -> RequestContext:
    return RequestContext(
        user_id="user-a",
        tenant_id="tenant-a",
        kb_permissions={"1:hardware": "write"},
        metadata={"resource_department_id": 1, "kb_id": 1},
    )


def test_app_pipeline_exposes_evidence_first_requirement_resolution():
    schema = DocumentSchema(
        document_schema_id="schema-a",
        version="1",
        document_type="hardware-review",
        fields=[DocumentFieldSchema(
            field_id="pcb_revision",
            label="PCB Revision",
            required=True,
            missing_policy="block_section",
            retrieval_policy_id="retrieval-v1",
            verification_policy_id="verification-v1",
        )],
        status="approved",
    )
    pipeline = object.__new__(AppPipeline)
    pipeline.document_generation = SimpleNamespace(
        store=SimpleNamespace(
            get_document_schema=lambda _schema_id, _version: schema,
        ),
    )

    result = pipeline.resolve_document_requirements(
        _ctx(),
        document_schema_id="schema-a",
        document_schema_version="1",
        evidence=[{"id": "e-a4", "field_id": "pcb_revision", "value": "A4"}],
    )

    assert result.resolved_fields == {"pcb_revision": "A4"}
    assert result.unresolved_requirements == []


def test_requirement_clarifier_can_turn_dynamic_unresolved_field_into_question():
    brief = {
        "purpose": "review",
        "scope": {"revision": "当前发布版本"},
        "missing_data_policy": "mark_tbd",
        "inference_policy": "forbid",
        "unresolved_requirements": [{
            "field": "pcb_revision",
            "field_label": "PCB Revision",
            "candidate_values": ["A3", "A4"],
            "requires_clarification": True,
            "reason": "multiple_candidates",
        }],
    }
    from src.document_authoring.generation_sessions import GenerationBrief
    generation_brief = GenerationBrief.model_validate(brief)

    message = RequirementClarifier().next_message({}, generation_brief)

    assert message.question_id == "field:pcb_revision"
    assert message.options == ["A3", "A4"]
