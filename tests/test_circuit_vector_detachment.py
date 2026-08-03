from __future__ import annotations

from unittest.mock import Mock

from src.circuit.models import CircuitDesign
from src.circuit.store import CircuitStore
from src.circuit.vector_index import CircuitVectorIndex, CircuitVectorIndexStatus, default_circuit_vector_index


def _design() -> CircuitDesign:
    return CircuitDesign(design_id="board", kb_name="kb_hw")


def test_save_does_not_invoke_legacy_local_vector_index(tmp_path, monkeypatch):
    reindex = Mock()
    monkeypatch.setattr(default_circuit_vector_index, "reindex_design", reindex)

    CircuitStore(root=str(tmp_path / "circuits")).save(_design())

    reindex.assert_not_called()


def test_delete_does_not_invoke_legacy_local_vector_index(tmp_path, monkeypatch):
    store = CircuitStore(root=str(tmp_path / "circuits"))
    monkeypatch.setattr(default_circuit_vector_index, "reindex_design", Mock())
    store.save(_design())
    delete = Mock()
    monkeypatch.setattr(default_circuit_vector_index, "_delete_design", delete)

    store.delete_design("kb_hw", "board")

    delete.assert_not_called()


def test_delete_kb_does_not_invoke_legacy_local_vector_index(tmp_path, monkeypatch):
    store = CircuitStore(root=str(tmp_path / "circuits"))
    monkeypatch.setattr(default_circuit_vector_index, "reindex_design", Mock())
    store.save(_design())
    drop_kb = Mock()
    monkeypatch.setattr(default_circuit_vector_index, "drop_kb", drop_kb)

    store.delete_kb("kb_hw")

    drop_kb.assert_not_called()


def test_reindex_design_compatibility_wrapper_returns_status_count(monkeypatch):
    index = CircuitVectorIndex()
    expected = CircuitVectorIndexStatus(available=True, indexed_count=3)
    reindex_with_status = Mock(return_value=expected)
    monkeypatch.setattr(index, "reindex_design_with_status", reindex_with_status)

    result = index.reindex_design(_design())

    assert result == 3
    reindex_with_status.assert_called_once()


def test_reindex_status_reports_delete_failure_for_empty_design(monkeypatch):
    index = CircuitVectorIndex()
    monkeypatch.setattr(index, "_embed_model", Mock(return_value=object()))
    monkeypatch.setattr(index, "_delete_design", Mock(return_value="delete failed"))

    result = index.reindex_design_with_status(_design())

    assert result == CircuitVectorIndexStatus(available=True, indexed_count=0, error="delete failed")
