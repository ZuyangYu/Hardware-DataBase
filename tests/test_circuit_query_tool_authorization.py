from __future__ import annotations

import pytest

import src.circuit.query_tool as query_tool
from src.circuit.index_service import CircuitIndexService
from src.circuit.models import CircuitDesign
from src.circuit.store import CircuitStore
from src.pipelines.document_rag.schemas import RequestContext


class _CapturingAgent:
    engines = []

    def __init__(self, *, engine, **kwargs):
        self.engine = engine
        self.engines.append(engine)

    def query(self, *, kb_name, **kwargs):
        return [row["design_id"] for row in self.engine.list_designs(kb_name)]


def _indexed_service(tmp_path):
    store = CircuitStore(root=str(tmp_path / "circuits"))
    service = CircuitIndexService(store=store)
    for design_id, department_id in (("allowed", "dept_a"), ("denied", "dept_b")):
        store.save(CircuitDesign(design_id=design_id, kb_name="kb_hw"))
        service._write_metadata(
            "kb_hw",
            design_id,
            {
                "department_id": department_id,
                "original_name": f"{design_id}.edf",
            },
        )
    return service


def test_legacy_query_tool_fails_closed_without_department_context(tmp_path, monkeypatch):
    service = _indexed_service(tmp_path)
    monkeypatch.setattr(query_tool, "CircuitQueryAgent", _CapturingAgent)

    with pytest.raises(PermissionError, match="department context"):
        query_tool.query_circuit_data(
            "list circuits",
            "kb_hw",
            "session-1",
            index_service=service,
            ctx=None,
        )


def test_legacy_query_tool_scopes_agent_store_to_authorized_designs(tmp_path, monkeypatch):
    service = _indexed_service(tmp_path)
    monkeypatch.setattr(query_tool, "CircuitQueryAgent", _CapturingAgent)

    result = query_tool.query_circuit_data(
        "list circuits",
        "kb_hw",
        "session-1",
        index_service=service,
        ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_a"}),
    )

    assert result == ["allowed"]


def test_document_rag_strict_path_requires_department_context(tmp_path):
    from src.agents.tools.document_rag_tool import DocumentRAGTool

    class _Backend:
        def retrieve(self, *args, **kwargs):
            return []

    tool = DocumentRAGTool(rag_backend=_Backend(), document_store=None)

    with pytest.raises(PermissionError):
        tool.run(
            "datasheet lookup",
            "kb_hw",
            None,
            filters={"allowed_record_ids": [1]},
        )


def test_derived_datasheet_calls_carry_allowed_record_ids():
    from src.agents.graph import _derived_datasheet_calls

    calls = _derived_datasheet_calls(
        "电源输出电路是否有短地保护？",
        [{"metadata": {"evidence_kind": "derived_topology", "capability_candidate": True, "part_numbers": ["TPS22919"]}}],
        verified_links=[{"refdes": "U1", "part_number": "TPS22919", "record_ids": [42]}],
    )

    assert calls and calls[0]["filters"] == {"allowed_record_ids": [42]}


def test_permission_denial_is_distinct_from_invalid_operation():
    # PermissionError (missing department context) must be a distinct failure
    # mode from ValueError (illegal typed operation), so callers and traces can
    # tell authorization gaps apart from contract violations.
    from src.agents.tools.circuit_tools import CircuitQueryTool
    from src.circuit.index_service import CircuitIndexService

    tool = CircuitQueryTool(index_service=CircuitIndexService(storage_root="/tmp/eval-circuit-t"))
    with pytest.raises(PermissionError):
        tool.run("SoC 的连接关系", "kb_hw", None)
    ctx = RequestContext(user_id="alice", metadata={"department_id": "dept_a"})
    with pytest.raises(ValueError):
        tool.run("SoC 的连接关系", "kb_hw", ctx, filters={"query_operation": "bogus"})
