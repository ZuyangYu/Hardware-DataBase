from __future__ import annotations

from pathlib import Path

import pytest
from langgraph.graph import END, START, StateGraph

from src.agents.claim_evidence import RetrievalOutcome
from src.document_authoring.harness.checkpointer import (
    StaleFencingToken,
    build_checkpointer,
)
from src.document_authoring.harness.graph import AuthoringGraph
from src.document_authoring.harness.langgraph_state import (
    initial_authoring_state,
    serialize_authoring_state,
)
from src.document_authoring.harness.policy import HarnessToolPolicy
from src.document_authoring.models import (
    AuthoringRunManifest,
    DocumentFieldSchema,
    DocumentSchema,
    HarnessPolicy,
    HarnessRun,
    KnowledgeBaseSourceSnapshot,
    TemplateVersion,
)
from src.document_authoring.writers.managed import DeterministicEvidenceWriter, ManagedWriter
from src.pipelines.document_rag.schemas import Evidence


def _objects(field_ids: list[str]):
    policy = HarnessPolicy(
        harness_policy_id="policy", version="1", status="approved",
        writer_provider_id="deterministic_evidence_writer", max_parallel_units=1,
        max_retrieval_rounds=10,
    )
    schema = DocumentSchema(
        document_schema_id="schema", version="1", document_type="checklist",
        status="approved", execution_mode="internal_harness",
        fields=[DocumentFieldSchema(
            field_id=field_id, label=field_id, query_terms=[field_id],
            retrieval_policy_id=field_id, verification_policy_id=field_id,
        ) for field_id in field_ids],
    )
    order = __import__("src.document_authoring.models", fromlist=["DocumentWorkOrder"]).DocumentWorkOrder(
        work_order_id="order", scope_type="knowledge_base", knowledge_base_name="ADAS",
        project_id=None, baseline_id=None, baseline_content_hash="",
        source_set_snapshot_id="snapshot", template_version_id="template",
        document_schema_id="schema", document_schema_version="1",
        template_schema_id="schema", template_schema_version="1",
        retrieval_policy_version="1", renderer_policy_version="1", target_format="xlsx",
        execution_mode="internal_harness", harness_policy_id="policy",
        harness_policy_version="1", created_by="tester",
    )
    snapshot = KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id="snapshot", tenant_id="default", knowledge_base_name="ADAS",
        source_names=["design.pdf"], created_by="tester",
    )
    template = TemplateVersion(
        template_version_id="template", template_id="template", format="xlsx",
        content_hash="template", template_schema_id="schema",
        template_schema_version="1", renderer_policy_id="renderer",
    )
    run = HarnessRun(
        harness_run_id="run", work_order_id="order", run_manifest_id="manifest",
    )
    manifest = AuthoringRunManifest(
        run_manifest_id="manifest", work_order_id="order", harness_policy_id="policy",
        harness_policy_version="1", writer_provider_id="deterministic_evidence_writer",
        prompt_version="1", source_set_snapshot_id="snapshot",
        input_fingerprint=order.input_fingerprint,
    )
    return policy, schema, order, snapshot, template, run, manifest


def _retrieve(requirement, attempt, query_override=None, **kwargs):
    return RetrievalOutcome(
        requirement_id=requirement.requirement_id,
        status="success_with_hits",
        evidences=[Evidence(
            id=requirement.semantic_unit_id,
            content=f"{requirement.subject}: value",
            source_name="design.pdf",
            metadata={"knowledge_base_name": "ADAS"},
        )],
        query_fingerprint=requirement.requirement_id,
        applied_source_set_snapshot_id="snapshot",
    )


def test_authoring_graph_runs_through_named_compiled_langgraph():
    policy, schema, order, snapshot, _template, run, manifest = _objects(["a"])
    graph = AuthoringGraph(
        HarnessToolPolicy(policy), ManagedWriter(DeterministicEvidenceWriter()),
    )

    compiled = graph.build_compiled_graph(build_checkpointer("memory"))
    assert {
        "load_context", "plan_units", "retrieve_evidence", "generate_draft",
        "validate_draft", "persist_draft", "route_next_unit", "await_human", "finalize",
    } <= set(compiled.get_graph().nodes)

    result = graph.run(
        work_order=order, harness_run=run, run_manifest=manifest, schema=schema,
        snapshot=snapshot, legacy_claims=[], retrieve=_retrieve,
    )

    assert [draft.unit_id for draft in result.drafts] == ["field:a"]
    assert graph._compiled_graph is not None


def test_single_field_entry_uses_same_pipeline_contract():
    policy, schema, order, snapshot, _template, run, manifest = _objects(["a", "b"])
    graph = AuthoringGraph(
        HarnessToolPolicy(policy), ManagedWriter(DeterministicEvidenceWriter()),
    )

    result = graph.run_field(
        "a", work_order=order, harness_run=run, run_manifest=manifest,
        schema=schema, snapshot=snapshot, legacy_claims=[], retrieve=_retrieve,
    )

    assert [draft.unit_id for draft in result.drafts] == ["field:a"]
    assert result.drafts[0].content == "a: value"


def test_state_contract_rejects_non_json_objects_and_oversize_payload():
    state = initial_authoring_state(
        work_order_id="wo", harness_run_id="run", run_manifest_id="manifest",
        source_set_snapshot_id="snapshot", input_fingerprint="fp",
        schema_version="1", unit_ids=["field:a"],
    )
    with pytest.raises(ValueError):
        serialize_authoring_state({**state, "raw": object()})
    with pytest.raises(ValueError, match="graph_state_size_exceeded"):
        serialize_authoring_state({**state, "issues": [{"x": "x" * (1024 * 1024)}]})
    with pytest.raises(ValueError, match="graph_state_version_incompatible"):
        serialize_authoring_state({**state, "graph_state_version": 2})


def test_sqlite_checkpointer_persists_state_and_rejects_stale_token(tmp_path: Path):
    current = {"run": 1}
    saver = build_checkpointer(
        "sqlite", sqlite_path=tmp_path / "checkpoints.db",
        fencing_token_provider=lambda _thread: current["run"],
    )

    class State(dict):
        pass

    # Use the real authoring state schema so the wrapper's version/size gate is
    # exercised by LangGraph itself.
    from src.document_authoring.harness.langgraph_state import DocumentAuthoringState
    graph = StateGraph(DocumentAuthoringState)
    graph.add_node("n", lambda state: {"current_node": "n"})
    graph.add_edge(START, "n")
    graph.add_edge("n", END)
    compiled = graph.compile(checkpointer=saver)
    state = initial_authoring_state(
        work_order_id="wo", harness_run_id="run", run_manifest_id="manifest",
        source_set_snapshot_id="snapshot", input_fingerprint="fp",
        schema_version="1", unit_ids=[],
    )
    config = {"configurable": {"thread_id": "run", "fencing_token": 1}}
    compiled.invoke(state, config)
    assert saver.get_tuple(config) is not None
    current["run"] = 2
    with pytest.raises(StaleFencingToken):
        compiled.invoke(state, config)
