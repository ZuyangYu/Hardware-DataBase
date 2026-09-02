"""Offline golden cases for scalar and repeating-table authoring paths.

The provider tests at the bottom are deliberately opt-in.  CI and local
regression runs use the deterministic fake model/tool loop; a publish job can
enable each real provider explicitly with its own credentials and endpoint.
"""

from __future__ import annotations

import io
import os
import zipfile
from xml.etree import ElementTree as ET

import pytest

from src.agents.claim_evidence import RetrievalOutcome
from src.document_authoring.harness.agent_loop import AgentFieldHarness
from src.document_authoring.models import (
    DocumentFieldSchema,
    DocumentSchema,
    DocumentUnitDraft,
    DocumentWorkOrder,
    DraftAssertion,
    HarnessPolicy,
    HarnessRun,
    KnowledgeBaseSourceSnapshot,
    LegacyTemplateClaim,
    RendererPolicy,
    WorkbookFill,
    WorkbookFillPlan,
    WorkbookRegionSchema,
    WorkbookTableColumnSchema,
    WorkbookTableFill,
    WorkbookTableSchema,
)
from src.document_authoring.renderers.xlsm import NS, XlsmRenderer
from src.document_authoring.template_analysis import workbook_value_hash
from src.document_authoring.validator import DocumentValidator
from src.pipelines.document_rag.schemas import Evidence


def _agent_fixtures() -> tuple[HarnessPolicy, DocumentSchema, DocumentWorkOrder, HarnessRun, KnowledgeBaseSourceSnapshot]:
    policy = HarnessPolicy(
        harness_policy_id="smoke-policy",
        version="1",
        status="approved",
        agent_tools=[
            "read_field_brief",
            "retrieve_evidence",
            "propose_field_value",
            "mark_missing",
        ],
        max_agent_tool_calls=20,
        max_proposal_retries_per_field=2,
        min_agent_confidence=0.7,
    )
    schema = DocumentSchema(
        document_schema_id="smoke-schema",
        version="1",
        document_type="hardware-review",
        status="approved",
        execution_mode="external_agent",
        fields=[
            DocumentFieldSchema(
                field_id="controller",
                label="Controller",
                value_type="text",
                retrieval_policy_id="retrieve-controller",
                verification_policy_id="verify-controller",
                authoring_policy="external_agent_draft",
            ),
            DocumentFieldSchema(
                field_id="interfaces",
                label="Interfaces",
                value_type="array",
                retrieval_policy_id="retrieve-interfaces",
                verification_policy_id="verify-interfaces",
                authoring_policy="external_agent_draft",
            ),
        ],
    )
    order = DocumentWorkOrder(
        work_order_id="smoke-order",
        tenant_id="tenant-a",
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        knowledge_base_id="kb-hardware",
        project_id=None,
        baseline_id=None,
        baseline_content_hash="",
        source_set_snapshot_id="smoke-snapshot",
        template_version_id="template-a",
        document_schema_id="smoke-schema",
        document_schema_version="1",
        template_schema_id="template-schema-a",
        template_schema_version="1",
        retrieval_policy_version="1",
        renderer_policy_version="1",
        target_format="xlsx",
        execution_mode="external_agent",
        requested_executor="external_agent",
        harness_policy_id="smoke-policy",
        harness_policy_version="1",
        created_by="user-a",
    )
    run = HarnessRun(
        harness_run_id="smoke-run",
        work_order_id=order.work_order_id,
        run_manifest_id="smoke-manifest",
        tenant_id=order.tenant_id,
        source_set_snapshot_id=order.source_set_snapshot_id,
        requested_executor="external_agent",
    )
    snapshot = KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id=order.source_set_snapshot_id,
        tenant_id=order.tenant_id,
        knowledge_base_name=order.knowledge_base_name,
        source_names=["hardware-spec.pdf"],
        created_by="user-a",
    )
    return policy, schema, order, run, snapshot


def _smoke_retriever(requirement, _attempt, _query=None):
    field_id = requirement.semantic_unit_id
    if "controller" in field_id:
        evidence_id = "evidence-controller"
        content = "The controller is STM32H743"
    else:
        evidence_id = "evidence-interfaces"
        content = "The design exposes CAN and LIN interfaces."
    return RetrievalOutcome(
        requirement_id=requirement.requirement_id,
        status="success_with_hits",
        evidences=[Evidence(
            id=evidence_id,
            content=content,
            source_name="hardware-spec.pdf",
            score=0.99,
        )],
        query_fingerprint=requirement.requirement_id,
        applied_source_set_snapshot_id="smoke-snapshot",
    )


def _fake_agent_runner(harness: AgentFieldHarness, _context):
    for field_id, value, value_type in (
        ("controller", "STM32H743", "text"),
        ("interfaces", ["CAN", "LIN"], "array"),
    ):
        harness.read_field_brief(field_id)
        retrieved = harness.retrieve_evidence(field_id, f"{field_id} value")
        evidence_id = retrieved.evidence_refs[0].evidence_id
        harness.propose_field_value(
            field_id,
            value,
            value_type,
            [evidence_id],
            note=f"verified {field_id}",
            confidence=0.98,
        )
    return None


def _xlsx_table_fixture() -> bytes:
    files = {
        "xl/workbook.xml": b'''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Review" sheetId="1" r:id="rId1"/></sheets></workbook>''',
        "xl/_rels/workbook.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>''',
        "xl/worksheets/sheet1.xml": b'''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData>
<row r="1"><c r="A1" t="inlineStr"><is><t>Project</t></is></c><c r="D1"/></row>
<row r="2" customFormat="1"><c r="A2" s="4" t="inlineStr"><is><t>sample-pin</t></is></c><c r="B2" s="4" t="inlineStr"><is><t>sample-signal</t></is></c></row>
</sheetData></worksheet>''',
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        for name, content in files.items():
            package.writestr(name, content)
    return output.getvalue()


def _cell_value(content: bytes, ref: str) -> str | None:
    with zipfile.ZipFile(io.BytesIO(content)) as package:
        root = ET.fromstring(package.read("xl/worksheets/sheet1.xml"))
    cell = next(
        candidate for candidate in root.findall(".//x:c", NS)
        if candidate.attrib.get("r") == ref
    )
    if cell.attrib.get("t") != "inlineStr":
        return None
    return "".join(text.text or "" for text in cell.findall(".//x:t", NS)) or None


def test_fake_agent_xlsx_scalar_and_repeating_table_golden(tmp_path):
    policy, schema, order, run, snapshot = _agent_fixtures()
    events = []
    harness = AgentFieldHarness(
        policy=policy,
        agent_tools_implemented=True,
        agent_runner=lambda context: _fake_agent_runner(harness, context),
    )
    result = harness.run(
        work_order=order,
        harness_run=run,
        schema=schema,
        policy=policy,
        snapshot=snapshot,
        retrieve=_smoke_retriever,
        append_execution_event=events.append,
    )
    drafts = {draft.unit_id: draft for draft in result.drafts}
    assert drafts["field:controller"].typed_value.display_value == "STM32H743"
    assert drafts["field:interfaces"].typed_value.normalized_values == ["CAN", "LIN"]
    assert all(
        result.unit_statuses[unit_id] == "ready_to_render"
        for unit_id in ("field:controller", "field:interfaces")
    )

    template = _xlsx_table_fixture()
    table_schema = WorkbookTableSchema(
        table_region_id="interfaces-table",
        semantic_unit_id="interfaces",
        sheet_name="Review",
        header_row=1,
        first_data_row=2,
        last_template_row=2,
        style_source_row=2,
        max_output_rows=4,
        columns=[
            WorkbookTableColumnSchema(column_id="pin", label="Pin", column_letter="A"),
            WorkbookTableColumnSchema(column_id="signal", label="Signal", column_letter="B"),
        ],
        expected_value_hashes={
            "A2": workbook_value_hash("sample-pin"),
            "B2": workbook_value_hash("sample-signal"),
        },
    )
    scalar_region = WorkbookRegionSchema(
        region_id="project-value",
        sheet_name="Review",
        locator={"cell": "D1"},
        role="evidence_derived",
        write_policy="validated_draft",
        expected_value_hash=workbook_value_hash(None),
    )
    fill_plan = WorkbookFillPlan(
        template_version_id="template-a",
        fills=[WorkbookFill(
            region_id="project-value",
            semantic_unit_id="project",
            value="hardware-golden",
        )],
        table_fills=[WorkbookTableFill(
            table_region_id="interfaces-table",
            semantic_unit_id="interfaces",
            rows=[
                {"pin": "J1-1", "signal": "CAN"},
                {"pin": "J1-2", "signal": "LIN"},
            ],
        )],
    )
    rendered = XlsmRenderer().render(
        template,
        [scalar_region],
        fill_plan,
        RendererPolicy(renderer_policy_id="smoke-renderer"),
        table_schemas=[table_schema],
    )
    assert _cell_value(rendered.content, "D1") == "hardware-golden"
    assert _cell_value(rendered.content, "A2") == "J1-1"
    assert _cell_value(rendered.content, "B2") == "CAN"
    assert _cell_value(rendered.content, "A3") == "J1-2"
    assert _cell_value(rendered.content, "B3") == "LIN"
    assert rendered.integrity_manifest["policy_violations"] == []
    assert any(event.event_type == "proposal_accepted" for event in events)

    # The same deterministic validator used by the coordinator must accept
    # the typed scalar and enumeration drafts, while legacy example text is a
    # separate contamination finding rather than evidence.
    validator = DocumentValidator()
    for draft in drafts.values():
        evidence_id = draft.evidence_ids[0]
        checked = validator.validate_typed_field_draft(
            draft,
            {evidence_id: {"content": "STM32H743 CAN LIN"}},
            expected_value_type="text" if draft.unit_id.endswith("controller") else "array",
        )
        assert checked.validation_status == "supported"
    contaminated = DocumentUnitDraft(
        unit_id="field:controller",
        run_id="smoke-run",
        generated_by="external_agent",
        content="sample-pin",
        assertions=[DraftAssertion(
            assertion_id="a1",
            text="sample-pin",
            claim_id="claim-1",
            evidence_ids=["e1"],
        )],
        evidence_ids=["e1"],
    )
    assert validator.detect_template_contamination(
        contaminated,
        [LegacyTemplateClaim(claim_id="legacy-1", text="sample-pin", locator={})],
    )


@pytest.mark.skipif(
    os.getenv("AGENT_REAL_SMOKE_OLLAMA") != "1",
    reason="real Ollama smoke is opt-in for publish validation",
)
def test_real_ollama_agent_smoke():
    _run_real_provider_smoke()


@pytest.mark.skipif(
    os.getenv("AGENT_REAL_SMOKE_OPENAI") != "1",
    reason="real OpenAI-compatible smoke is opt-in for publish validation",
)
def test_real_openai_compatible_agent_smoke():
    _run_real_provider_smoke()


def _run_real_provider_smoke() -> None:
    policy, schema, order, run, snapshot = _agent_fixtures()
    harness = AgentFieldHarness(policy=policy, agent_tools_implemented=True)
    result = harness.run(
        work_order=order,
        harness_run=run,
        schema=schema,
        policy=policy,
        snapshot=snapshot,
        retrieve=_smoke_retriever,
    )
    assert result is not None
    assert harness.last_execution is not None
