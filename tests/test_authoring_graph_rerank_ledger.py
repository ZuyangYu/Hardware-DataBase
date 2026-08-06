from __future__ import annotations

from unittest.mock import Mock

import pytest

from src.agents.claim_evidence import RetrievalOutcome, RetrievalSourceOutcome
from src.agents.state import Evidence
from src.document_authoring.harness.graph import AuthoringGraph
from src.document_authoring.harness.policy import HarnessToolPolicy
from src.document_authoring.models import (
    AuthoringRunManifest,
    DocumentFieldSchema,
    DocumentSchema,
    DocumentUnitDraft,
    DraftAssertion,
    DocumentWorkOrder,
    HarnessPolicy,
    HarnessRun,
    KnowledgeBaseSourceSnapshot,
)
from src.document_authoring.validator import DocumentValidator


KB = "kb1"
SOURCE = "doc.pdf"
SNAP_ID = "snap-1"


def _policy(*, allowed_tools=None, max_steps: int = 40) -> HarnessPolicy:
    return HarnessPolicy(
        harness_policy_id="hp", version="1", status="approved",
        max_steps=max_steps, max_retrieval_rounds=4, max_retrieval_attempts_per_unit=2,
        allowed_tools=allowed_tools if allowed_tools is not None else [
            "retrieve_evidence", "draft_ready_unit", "validate_unit_draft",
            "detect_template_contamination", "validate_cross_unit",
            "rewrite_query", "rerank_evidence",
        ],
    )


def _evidence(
    eid: str,
    content: str,
    *,
    fallback: bool = False,
    score: float = 0.5,
    preferred_role: bool = False,
) -> Evidence:
    metadata = {"knowledge_base_name": KB}
    if fallback:
        metadata["ragflow_source_name_fallback"] = True
    if preferred_role:
        metadata["preferred_source_role_match"] = "released_design"
    return Evidence(
        id=eid, content=content, source_name=SOURCE,
        content_kind="document_text", processor_kind="ragflow",
        score=score, metadata=metadata,
    )


def _outcome(evidences, source_outcomes=None, *, status="success_with_hits") -> RetrievalOutcome:
    return RetrievalOutcome(
        requirement_id="req-1", status=status, evidences=evidences,
        source_outcomes=source_outcomes or [], query_fingerprint="fp",
        applied_source_set_snapshot_id=SNAP_ID, applied_region_policy_versions={},
    )


def _setup():
    snapshot = KnowledgeBaseSourceSnapshot(
        source_set_snapshot_id=SNAP_ID, tenant_id="t", knowledge_base_name=KB,
        source_names=[SOURCE], created_by="alice",
    )
    work_order = DocumentWorkOrder(
        work_order_id="wo-1", tenant_id="t", scope_type="knowledge_base",
        knowledge_base_name=KB, project_id=None, baseline_id=None,
        baseline_content_hash="", source_set_snapshot_id=SNAP_ID,
        template_version_id="tv", document_schema_id="ds", document_schema_version="1",
        template_schema_id="ts", template_schema_version="1",
        retrieval_policy_version="1", renderer_policy_version="1",
        target_format="xlsx", execution_mode="internal_harness",
        harness_policy_id="hp", harness_policy_version="1", created_by="alice",
    )
    schema = DocumentSchema(
        document_schema_id="ds", version="1", document_type="spec",
        execution_mode="internal_harness",
        fields=[DocumentFieldSchema(
            field_id="f1", label="额定电流", description="电源拓扑的额定电流",
            retrieval_policy_id="rp", verification_policy_id="vp",
            required_capabilities=["entity_lookup"], authoring_policy="managed_writer",
        )],
    )
    harness_run = HarnessRun(
        harness_run_id="hr-1", work_order_id="wo-1", run_manifest_id="rm-1", status="running",
    )
    manifest = AuthoringRunManifest(
        run_manifest_id="rm-1", work_order_id="wo-1", harness_policy_id="hp",
        harness_policy_version="1", writer_provider_id="managed", prompt_version="1",
        source_set_snapshot_id=SNAP_ID, input_fingerprint=work_order.input_fingerprint,
    )
    return work_order, snapshot, schema, harness_run, manifest


def _graph(*, reranker=None, rewriter=None, policy=None, draft_recorder=None,
           draft_provider=None, validator=None) -> tuple[AuthoringGraph, list]:
    draft_calls: list = []

    def default_draft_provider(request):
        draft_calls.append(request)
        return DocumentUnitDraft(
            unit_id=request.unit_id, run_id=request.run_id,
            generated_by="managed_writer", validation_status="supported",
        )

    if validator is None:
        validator = Mock()
        validator.validate_unit_draft.side_effect = lambda draft, ev_by_id: draft
        validator.validate_typed_field_draft.side_effect = lambda draft, ev_by_id, **kwargs: draft
        validator.detect_template_contamination.return_value = []
        validator.validate_cross_unit_consistency.return_value = []
    graph = AuthoringGraph(
        HarnessToolPolicy(policy or _policy()), Mock(), validator=validator,
        draft_provider=draft_provider or default_draft_provider, rewriter=rewriter,
        reranker=reranker,
    )
    recorder = draft_recorder if draft_recorder is not None else draft_calls
    return graph, recorder


def _run(graph, retrieve):
    work_order, snapshot, schema, harness_run, manifest = _setup()
    return graph.run(
        work_order=work_order, harness_run=harness_run, run_manifest=manifest,
        schema=schema, snapshot=snapshot, legacy_claims=[], retrieve=retrieve,
    )


def test_rerank_applied_when_reranker_injected():
    evidences = [_evidence("a", "alpha"), _evidence("b", "bravo")]

    def retrieve(req, attempt, query_override=None):
        return _outcome(evidences)

    reranker = Mock()
    reranker.rerank.side_effect = lambda req, ev, top_k=None: list(reversed(ev))
    graph, draft_calls = _graph(reranker=reranker)

    result = _run(graph, retrieve)

    # Evidence sent to the writer is reversed (post-rerank).
    assert [e["id"] for e in draft_calls[0].evidence] == ["b", "a"]
    # matrix_row.evidence_ids reflect post-rerank order.
    assert result.matrix_rows[0]["evidence_ids"] == ["b", "a"]


def test_rerank_skipped_when_reranker_none():
    evidences = [_evidence("a", "alpha"), _evidence("b", "bravo")]

    def retrieve(req, attempt, query_override=None):
        return _outcome(evidences)

    graph_with, draft_with = _graph(reranker=Mock())  # not called -> passthrough identity
    graph_with.reranker = None
    # Use a graph built without reranker for a clean step-count comparison.
    graph_none, draft_none = _graph(reranker=None)

    # Both produce original order; the reranker-injected one has an extra step.
    res_none = _run(graph_none, retrieve)
    assert [e["id"] for e in draft_none[0].evidence] == ["a", "b"]
    assert res_none.matrix_rows[0]["evidence_ids"] == ["a", "b"]


def test_rerank_step_gated_by_require_tool():
    """A policy without rerank_evidence must reject an (mis)injected reranker."""
    evidences = [_evidence("a", "alpha"), _evidence("b", "bravo")]

    def retrieve(req, attempt, query_override=None):
        return _outcome(evidences)

    policy = _policy(allowed_tools=[
        "retrieve_evidence", "draft_ready_unit", "validate_unit_draft",
        "detect_template_contamination", "validate_cross_unit", "rewrite_query",
    ])  # no rerank_evidence
    reranker = Mock()
    reranker.rerank.side_effect = lambda req, ev, top_k=None: list(reversed(ev))
    graph, _ = _graph(reranker=reranker, policy=policy)

    with pytest.raises(PermissionError):
        _run(graph, retrieve)


def test_ledger_records_query_rewrites_per_source_fallback():
    # attempt 1: success_empty -> triggers rewrite; attempt 2: hits with fallback metadata.
    hit_evidences = [_evidence("a", "alpha", fallback=True), _evidence("b", "bravo", fallback=True)]
    source_outcomes = [
        RetrievalSourceOutcome(source_version_id=SOURCE, status="success_with_hits",
                              evidence_ids=["a", "b"]),
        RetrievalSourceOutcome(source_version_id="other.pdf", status="success_empty",
                              evidence_ids=[]),
    ]

    def retrieve(req, attempt, query_override=None):
        if attempt == 1:
            return _outcome([], status="success_empty")
        return _outcome(hit_evidences, source_outcomes)

    rewriter = Mock()
    rewriter.rewrite.return_value = "rewritten-query"
    graph, _ = _graph(rewriter=rewriter)

    result = _run(graph, retrieve)

    row = result.retrieval_ledger[0]
    assert row["unit_id"] == "field:f1"
    assert "额定电流" in row["original_query"]
    assert row["rewrites"] == ["rewritten-query"]
    assert len(row["per_source"]) == 2
    assert row["per_source"][0] == {"source": SOURCE, "status": "success_with_hits", "hit_count": 2}
    assert row["per_source"][1] == {"source": "other.pdf", "status": "success_empty", "hit_count": 0}
    assert row["fallback_triggered"] is True
    assert row["final_evidence_ids"] == ["a", "b"]


def test_ledger_survived_in_result():
    """The retrieval_ledger must be surfaced on the result (not dropped)."""
    evidences = [_evidence("a", "alpha")]

    def retrieve(req, attempt, query_override=None):
        return _outcome(evidences)

    graph, _ = _graph()

    result = _run(graph, retrieve)

    assert len(result.retrieval_ledger) == 1
    assert result.retrieval_ledger[0]["unit_id"] == "field:f1"


def test_ledger_for_empty_unit():
    def retrieve(req, attempt, query_override=None):
        return _outcome([], status="success_empty")

    # rewriter returns None -> no rewrite, stays empty.
    graph, _ = _graph(rewriter=Mock(rewrite=Mock(return_value=None)))

    result = _run(graph, retrieve)

    assert result.matrix_rows  # empty unit still gets a matrix row
    row = result.retrieval_ledger[0]
    assert row["final_evidence_ids"] == []
    assert row["rewrites"] == []
    assert row["fallback_triggered"] is False


def test_matrix_row_embeds_ledger():
    evidences = [_evidence("a", "alpha"), _evidence("b", "bravo")]

    def retrieve(req, attempt, query_override=None):
        return _outcome(evidences)

    graph, _ = _graph()

    result = _run(graph, retrieve)

    assert result.matrix_rows[0]["retrieval_ledger"] == result.retrieval_ledger[0]


def test_writer_receives_bounded_deduplicated_evidence_and_ledger_keeps_discards():
    evidences = [
        _evidence("a", "alpha"),
        _evidence("b", "bravo"),
        _evidence("c", "charlie"),
        _evidence("d", "delta"),
        _evidence("e", "echo"),
        _evidence("f", "foxtrot"),
        _evidence("duplicate", "alpha"),
    ]
    def retrieve(req, attempt, query_override=None):
        return _outcome(evidences)
    graph, draft_calls = _graph()

    result = _run(graph, retrieve)

    assert [item["id"] for item in draft_calls[0].evidence] == ["a", "b", "c", "d", "e"]
    assert result.retrieval_ledger[0]["discarded_evidence_ids"] == ["duplicate", "f"]


def test_non_reranked_evidence_uses_preferred_role_then_score_then_id_order():
    evidences = [
        _evidence("high-score", "high", score=0.9),
        _evidence("preferred", "preferred", score=0.1, preferred_role=True),
        _evidence("middle-score", "middle", score=0.5),
    ]

    def retrieve(req, attempt, query_override=None):
        return _outcome(evidences)

    graph, draft_calls = _graph()
    _run(graph, retrieve)

    assert [item["id"] for item in draft_calls[0].evidence] == [
        "preferred", "high-score", "middle-score",
    ]


def test_graph_blocks_a_supported_prose_draft_without_a_typed_value():
    def draft_provider(request):
        return DocumentUnitDraft(
            unit_id=request.unit_id,
            run_id=request.run_id,
            generated_by="managed_writer",
            content="额定电流为 10A",
            proposed_value="额定电流为 10A",
            evidence_ids=["a"],
            assertions=[DraftAssertion(
                assertion_id="assertion-a",
                text="额定电流为 10A",
                claim_id="claim-f1",
                evidence_ids=["a"],
            )],
        )

    def retrieve(req, attempt, query_override=None):
        return _outcome([_evidence("a", "额定电流为 10A")])

    graph, _ = _graph(
        validator=DocumentValidator(),
        draft_provider=draft_provider,
    )
    result = _run(graph, retrieve)

    assert result.unit_statuses["field:f1"] == "requires_human"
    assert result.drafts[0].validation_status == "unsupported"
    assert "draft has no typed field value" in result.drafts[0].validation_notes
