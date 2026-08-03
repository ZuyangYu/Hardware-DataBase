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
