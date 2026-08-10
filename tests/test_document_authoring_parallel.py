from __future__ import annotations

import threading
import time
from unittest.mock import Mock

import pytest

from src.agents.claim_evidence import RetrievalOutcome
from src.document_authoring.harness.graph import AuthoringGraph, _use_deterministic_evidence_writer
from src.document_authoring.harness.policy import HarnessToolPolicy
from src.document_authoring.harness.runtime import InternalDocumentHarnessRuntime
from src.document_authoring.models import (
    AuthoringRunManifest,
    DocumentFieldSchema,
    DocumentSchema,
    DocumentUnitDraft,
    DocumentWorkOrder,
    HarnessPolicy,
    HarnessRun,
    KnowledgeBaseSourceSnapshot,
    TemplateVersion,
)
from src.document_authoring.writers.managed import DeterministicEvidenceWriter, ManagedWriter
from src.pipelines.document_rag.schemas import Evidence


def test_harness_policy_defaults_to_three_parallel_units():
    policy = HarnessPolicy(harness_policy_id="parallel", version="1")

    assert policy.max_parallel_units == 3


def test_harness_policy_allows_eight_parallel_units_after_capacity_probe():
    policy = HarnessPolicy(harness_policy_id="parallel", version="1", max_parallel_units=8)

    assert policy.max_parallel_units == 8


@pytest.mark.parametrize("parallel_units", [0, 9])
def test_harness_policy_rejects_parallelism_outside_measured_limit(parallel_units: int):
    with pytest.raises(ValueError, match="max_parallel_units"):
        HarnessPolicy(
            harness_policy_id="parallel",
            version="1",
            max_parallel_units=parallel_units,
        )


def test_run_manifest_freezes_policy_parallelism():
    policy = HarnessPolicy(harness_policy_id="parallel", version="1", max_parallel_units=4)
    order = DocumentWorkOrder(
        work_order_id="order", scope_type="knowledge_base", knowledge_base_name="ADAS",
        project_id=None, baseline_id=None, baseline_content_hash="", source_set_snapshot_id="snapshot",
        template_version_id="template", document_schema_id="schema", document_schema_version="1",
        template_schema_id="schema", template_schema_version="1", retrieval_policy_version="1",
        renderer_policy_version="1", target_format="xlsx", execution_mode="internal_harness",
        harness_policy_id=policy.harness_policy_id, harness_policy_version=policy.version, created_by="tester",
    )
    snapshot = KnowledgeBaseSourceSnapshot.create(
        tenant_id="default", knowledge_base_name="ADAS", source_names=["design.pdf"], created_by="tester",
    )
    template = TemplateVersion(
        template_version_id="template", template_id="template", format="xlsx", content_hash="template-hash",
        template_schema_id="schema", template_schema_version="1", renderer_policy_id="renderer",
    )
    schema = DocumentSchema(document_schema_id="schema", version="1", document_type="checklist", status="approved")

    manifest = InternalDocumentHarnessRuntime.build_manifest(order, policy, snapshot, template, schema)

    assert manifest.max_parallel_units == 4


def test_parallel_graph_limits_in_flight_units_and_merges_schema_order():
    policy = HarnessPolicy(
        harness_policy_id="parallel", version="1", status="approved",
        max_parallel_units=3, max_steps=30, max_retrieval_rounds=10,
    )
    schema = DocumentSchema(
        document_schema_id="schema", version="1", document_type="checklist",
        status="approved", execution_mode="internal_harness",
        fields=[
            DocumentFieldSchema(
                field_id=name, label=name, retrieval_policy_id=name,
                verification_policy_id=name, query_terms=[name],
            )
            for name in ("first", "second", "third")
        ],
    )
    order = DocumentWorkOrder(
        work_order_id="order", scope_type="knowledge_base", knowledge_base_name="ADAS",
        project_id=None, baseline_id=None, baseline_content_hash="", source_set_snapshot_id="snapshot",
        template_version_id="template", document_schema_id="schema", document_schema_version="1",
        template_schema_id="schema", template_schema_version="1", retrieval_policy_version="1",
        renderer_policy_version="1", target_format="xlsx", execution_mode="internal_harness",
        harness_policy_id=policy.harness_policy_id, harness_policy_version=policy.version, created_by="tester",
    )
    snapshot = KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id="snapshot", tenant_id="default", knowledge_base_name="ADAS",
        source_names=["design.pdf"], created_by="tester",
    )
    run = HarnessRun(harness_run_id="run", work_order_id="order", run_manifest_id="manifest")
    manifest = AuthoringRunManifest(
        run_manifest_id="manifest", work_order_id="order", harness_policy_id="parallel",
        harness_policy_version="1", writer_provider_id="deterministic_evidence_writer",
        prompt_version="1", source_set_snapshot_id="snapshot", input_fingerprint=order.input_fingerprint,
    )
    active = 0
    max_active = 0
    lock = threading.Lock()

    def retrieve(requirement, attempt, query_override=None, **_):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id, status="success_with_hits",
            evidences=[Evidence(
                id=requirement.semantic_unit_id, content=f"{requirement.subject}: value",
                source_name="design.pdf", metadata={"knowledge_base_name": "ADAS"},
            )], query_fingerprint=requirement.requirement_id,
            applied_source_set_snapshot_id="snapshot",
        )

    result = AuthoringGraph(
        HarnessToolPolicy(policy), ManagedWriter(DeterministicEvidenceWriter()),
    ).run(
        work_order=order, harness_run=run, run_manifest=manifest, schema=schema,
        snapshot=snapshot, legacy_claims=[], retrieve=retrieve,
    )

    assert max_active == 3
    assert [draft.unit_id for draft in result.drafts] == ["field:first", "field:second", "field:third"]


def test_structured_pin_fact_bypasses_reranker_and_configured_writer():
    policy = HarnessPolicy(
        harness_policy_id="parallel", version="1", status="approved",
        max_steps=20, max_retrieval_rounds=4,
    )
    schema = DocumentSchema(
        document_schema_id="schema", version="1", document_type="checklist",
        status="approved", execution_mode="internal_harness",
        fields=[DocumentFieldSchema(
            field_id="u1_pa0", label="MCU U1 管脚定义", description="U1-PA0 的网络功能",
            retrieval_policy_id="pin", verification_policy_id="pin", query_terms=["U1-PA0"],
        )],
    )
    order = DocumentWorkOrder(
        work_order_id="order", scope_type="knowledge_base", knowledge_base_name="ADAS",
        project_id=None, baseline_id=None, baseline_content_hash="", source_set_snapshot_id="snapshot",
        template_version_id="template", document_schema_id="schema", document_schema_version="1",
        template_schema_id="schema", template_schema_version="1", retrieval_policy_version="1",
        renderer_policy_version="1", target_format="xlsx", execution_mode="internal_harness",
        harness_policy_id=policy.harness_policy_id, harness_policy_version=policy.version, created_by="tester",
    )
    snapshot = KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id="snapshot", tenant_id="default", knowledge_base_name="ADAS",
        source_names=["design.edf"], created_by="tester",
    )
    run = HarnessRun(harness_run_id="run", work_order_id="order", run_manifest_id="manifest")
    manifest = AuthoringRunManifest(
        run_manifest_id="manifest", work_order_id="order", harness_policy_id="parallel",
        harness_policy_version="1", writer_provider_id="llm_managed_writer",
        prompt_version="1", source_set_snapshot_id="snapshot", input_fingerprint=order.input_fingerprint,
    )
    reranker = Mock()
    reranker.rerank.side_effect = lambda requirement, evidence: list(evidence)
    writer_calls: list = []

    def configured_writer(request):
        writer_calls.append(request)
        return DocumentUnitDraft(
            unit_id=request.unit_id, run_id=request.run_id, generated_by="managed_writer",
        )

    def retrieve(requirement, attempt, query_override=None, **_):
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id, status="success_with_hits",
            evidences=[Evidence(
                id="edf-u1-pa0", content="U1-PA0: CAN_RX", source_name="design.edf",
                metadata={"knowledge_base_name": "ADAS", "source_group": "circuit_design", "pin_mappings": ["U1-PA0"]},
            )], query_fingerprint=requirement.requirement_id,
            applied_source_set_snapshot_id="snapshot",
        )

    result = AuthoringGraph(
        HarnessToolPolicy(policy), ManagedWriter(DeterministicEvidenceWriter()),
        draft_provider=configured_writer, reranker=reranker,
    ).run(
        work_order=order, harness_run=run, run_manifest=manifest, schema=schema,
        snapshot=snapshot, legacy_claims=[], retrieve=retrieve,
    )

    assert reranker.rerank.call_count == 0
    assert writer_calls == []
    assert result.drafts[0].content == "U1-PA0: CAN_RX"


def test_direct_datasheet_assignment_uses_deterministic_fast_path():
    schema = DocumentSchema(
        document_schema_id="schema", version="1", document_type="checklist", status="approved",
        fields=[DocumentFieldSchema(
            field_id="can_model", label="CAN 收发器型号", description="U2 型号",
            retrieval_policy_id="part", verification_policy_id="part", query_terms=["U2"],
        )],
    )
    requirement = type("Requirement", (), {
        "retrieval_query_terms": ["U2", "CAN 收发器型号"],
    })()

    assert _use_deterministic_evidence_writer(
        "field:can_model", schema, requirement,
        [{"id": "datasheet-u2", "content": "U2: TJA1042", "metadata": {}}],
    ) is True


def test_parallel_graph_reports_completed_unit_before_slowest_future_finishes():
    policy = HarnessPolicy(
        harness_policy_id="parallel", version="1", status="approved",
        max_parallel_units=2, max_steps=30, max_retrieval_rounds=10,
    )
    schema = DocumentSchema(
        document_schema_id="schema", version="1", document_type="checklist",
        status="approved", execution_mode="internal_harness",
        fields=[
            DocumentFieldSchema(field_id="fast", label="fast", retrieval_policy_id="fast", verification_policy_id="fast", query_terms=["fast"]),
            DocumentFieldSchema(field_id="slow", label="slow", retrieval_policy_id="slow", verification_policy_id="slow", query_terms=["slow"]),
        ],
    )
    order = DocumentWorkOrder(
        work_order_id="order", scope_type="knowledge_base", knowledge_base_name="ADAS",
        project_id=None, baseline_id=None, baseline_content_hash="", source_set_snapshot_id="snapshot",
        template_version_id="template", document_schema_id="schema", document_schema_version="1",
        template_schema_id="schema", template_schema_version="1", retrieval_policy_version="1",
        renderer_policy_version="1", target_format="xlsx", execution_mode="internal_harness",
        harness_policy_id=policy.harness_policy_id, harness_policy_version=policy.version, created_by="tester",
    )
    snapshot = KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id="snapshot", tenant_id="default", knowledge_base_name="ADAS",
        source_names=["design.pdf"], created_by="tester",
    )
    run = HarnessRun(harness_run_id="run", work_order_id="order", run_manifest_id="manifest")
    manifest = AuthoringRunManifest(
        run_manifest_id="manifest", work_order_id="order", harness_policy_id="parallel",
        harness_policy_version="1", writer_provider_id="deterministic_evidence_writer",
        prompt_version="1", source_set_snapshot_id="snapshot", input_fingerprint=order.input_fingerprint,
    )
    release_slow = threading.Event()
    observed_one_complete = threading.Event()
    observed: list[dict] = []
    finished: dict[str, object] = {}

    def retrieve(requirement, attempt, query_override=None, **_):
        if requirement.semantic_unit_id == "field:slow":
            release_slow.wait(timeout=2)
        return RetrievalOutcome(
            requirement_id=requirement.requirement_id, status="success_with_hits",
            evidences=[Evidence(
                id=requirement.semantic_unit_id, content=f"{requirement.subject}: value",
                source_name="design.pdf", metadata={"knowledge_base_name": "ADAS"},
            )], query_fingerprint=requirement.requirement_id,
            applied_source_set_snapshot_id="snapshot",
        )

    def on_progress(state):
        if state["current_node"] == "parallel_units":
            snapshot_state = dict(state)
            observed.append(snapshot_state)
            if snapshot_state.get("completed_units") == 1:
                observed_one_complete.set()

    graph = AuthoringGraph(
        HarnessToolPolicy(policy), ManagedWriter(DeterministicEvidenceWriter()), on_progress=on_progress,
    )
    worker = threading.Thread(
        target=lambda: finished.setdefault("result", graph.run(
            work_order=order, harness_run=run, run_manifest=manifest, schema=schema,
            snapshot=snapshot, legacy_claims=[], retrieve=retrieve,
        )),
    )
    worker.start()
    try:
        assert observed_one_complete.wait(timeout=0.5)
    finally:
        release_slow.set()
        worker.join(timeout=3)

    assert not worker.is_alive()
    assert [draft.unit_id for draft in finished["result"].drafts] == ["field:fast", "field:slow"]
    assert observed[0]["total_units"] == 2
