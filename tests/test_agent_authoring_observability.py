"""Fake-agent contracts for typed proposals, evidence scope and audit facts."""

from __future__ import annotations

from collections import Counter

from src.agents.claim_evidence import RetrievalOutcome
from src.document_authoring.harness.agent_contracts import FieldProposalResult
from src.document_authoring.harness.agent_loop import AgentFieldHarness
from src.document_authoring.models import (
    DocumentFieldSchema,
    DocumentSchema,
    DocumentWorkOrder,
    HarnessPolicy,
    HarnessRun,
    KnowledgeBaseSourceSnapshot,
)
from src.pipelines.document_rag.schemas import Evidence


def _fixtures(value_type: str = "text"):
    policy = HarnessPolicy(
        harness_policy_id="agent-policy",
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
        document_schema_id="agent-schema",
        version="1",
        document_type="hardware-review",
        status="approved",
        execution_mode="external_agent",
        fields=[
            DocumentFieldSchema(
                field_id="controller",
                label="主控型号",
                description="正式设计中的主控器件型号",
                value_type=value_type,
                query_terms=["controller", "主控"],
                retrieval_policy_id="retrieve-controller",
                verification_policy_id="verify-controller",
                authoring_policy="external_agent_draft",
            )
        ],
    )
    order = DocumentWorkOrder(
        work_order_id="agent-order",
        tenant_id="tenant-a",
        scope_type="knowledge_base",
        knowledge_base_name="hardware",
        knowledge_base_id="kb-hardware",
        project_id=None,
        baseline_id=None,
        baseline_content_hash="",
        source_set_snapshot_id="snapshot-a",
        template_version_id="template-a",
        document_schema_id="agent-schema",
        document_schema_version="1",
        template_schema_id="template-schema-a",
        template_schema_version="1",
        retrieval_policy_version="1",
        renderer_policy_version="1",
        target_format="xlsx",
        execution_mode="external_agent",
        requested_executor="external_agent",
        harness_policy_id="agent-policy",
        harness_policy_version="1",
        created_by="user-a",
    )
    run = HarnessRun(
        harness_run_id="agent-run",
        work_order_id=order.work_order_id,
        run_manifest_id="agent-manifest",
        tenant_id=order.tenant_id,
        source_set_snapshot_id=order.source_set_snapshot_id,
        requested_executor="external_agent",
    )
    snapshot = KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id="snapshot-a",
        tenant_id="tenant-a",
        knowledge_base_name="hardware",
        source_names=["controller-spec.pdf"],
        created_by="user-a",
    )
    return policy, schema, order, run, snapshot


def _retriever(snapshot_id: str = "snapshot-a"):
    def retrieve(requirement, _attempt, query_override=None):
        del query_override
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id,
            status="success_with_hits",
            evidences=[
                Evidence(
                    id="evidence-controller-1",
                    content="正式设计主控型号为 STM32H743。",
                    source_name="controller-spec.pdf",
                    source_type="document",
                    score=0.98,
                )
            ],
            source_outcomes=[],
            query_fingerprint=requirement.requirement_id,
            applied_source_set_snapshot_id=snapshot_id,
        )

    return retrieve


def _run_with_runner(runner_factory, *, value_type: str = "text"):
    policy, schema, order, run, snapshot = _fixtures(value_type)
    events = []
    harness = None

    def runner(context):
        return runner_factory(harness, context)

    harness = AgentFieldHarness(
        policy=policy,
        agent_tools_implemented=True,
        agent_runner=runner,
    )
    result = harness.run(
        work_order=order,
        harness_run=run,
        schema=schema,
        policy=policy,
        snapshot=snapshot,
        retrieve=_retriever(),
        append_execution_event=events.append,
    )
    return result, harness, run, events


def test_fake_agent_accepts_typed_proposal_only_after_brief_and_evidence():
    def runner(harness, _context):
        brief = harness.read_field_brief("controller")
        assert brief.field_contract["value_type"] == "text"
        retrieved = harness.retrieve_evidence("controller", "正式主控型号")
        assert retrieved.evidence_refs[0].evidence_id == "evidence-controller-1"
        return harness.propose_field_value(
            "controller",
            "STM32H743",
            "text",
            [retrieved.evidence_refs[0].evidence_id],
            note="正式设计主控型号：STM32H743",
            confidence=0.95,
        )

    result, harness, run, events = _run_with_runner(runner)

    assert result.drafts[0].generated_by == "external_agent"
    assert result.drafts[0].run_id == run.harness_run_id
    assert result.drafts[0].typed_value.kind == "scalar"
    assert result.unit_statuses["field:controller"] == "ready_to_render"
    assert run.agent_token_usage["usage_returned"] is False
    counts = Counter(event.event_type for event in events)
    assert counts["tool_called"] == 3
    assert counts["tool_succeeded"] == 3
    assert counts["proposal_submitted"] == 1
    assert counts["proposal_accepted"] == 1
    assert counts["draft_persisted"] == 1
    assert harness.last_execution.agent_token_usage["call_count"] == 0


def test_first_bad_proposal_returns_typed_issue_and_second_proposal_succeeds():
    def runner(harness, _context):
        retrieved = harness.retrieve_evidence("controller", "主控型号")
        evidence_id = retrieved.evidence_refs[0].evidence_id
        rejected = harness.propose_field_value(
            "controller", "STM32H743", "array", [evidence_id], confidence=0.95,
        )
        assert isinstance(rejected, FieldProposalResult)
        assert rejected.status == "rejected"
        assert rejected.error_code == "value_type_mismatch"
        return harness.propose_field_value(
            "controller", "STM32H743", "text", [evidence_id],
            note="主控型号为 STM32H743", confidence=0.95,
        )

    result, _harness, _run, events = _run_with_runner(runner)

    assert result.drafts[0].typed_value.display_value == "STM32H743"
    rejected = [event for event in events if event.event_type == "proposal_rejected"]
    assert len(rejected) == 1
    assert rejected[0].error_code == "value_type_mismatch"


def test_unregistered_evidence_is_rejected_and_agent_must_mark_missing():
    def runner(harness, _context):
        rejected = harness.propose_field_value(
            "controller", "STM32H743", "text", ["not-in-registry"], confidence=0.95,
        )
        assert rejected.status == "rejected"
        assert rejected.error_code == "evidence_unavailable"
        return harness.mark_missing("controller", "冻结来源中没有可验证的证据")

    result, _harness, run, events = _run_with_runner(runner)

    assert result.drafts == []
    assert result.unit_statuses["field:controller"] == "tbd"
    assert run.status != "completed"
    assert any(event.event_type == "proposal_rejected" for event in events)
    assert any(event.event_type == "missing_marked" for event in events)


def test_low_confidence_proposal_persists_human_gate_without_accepting_draft():
    def runner(harness, _context):
        retrieved = harness.retrieve_evidence("controller", "主控型号")
        return harness.propose_field_value(
            "controller", "STM32H743", "text",
            [retrieved.evidence_refs[0].evidence_id], confidence=0.2,
        )

    result, _harness, run, events = _run_with_runner(runner)

    assert result.drafts[0].validation_status == "requires_human"
    assert result.unit_statuses["field:controller"] == "requires_human"
    assert run.status == "waiting_human"
    assert run.pending_human_event["proposal_hash"]
    assert run.pending_human_event["event_id"].startswith("pending-agent-proposal-")
    assert any(event.event_type == "human_waiting" for event in events)
    assert not any(event.event_type == "proposal_accepted" for event in events)
