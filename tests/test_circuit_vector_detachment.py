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
    # chroma 栈在测试环境未安装；桩掉客户端解析以到达删除失败分支。
    monkeypatch.setattr(index, "_chroma_client", Mock(return_value=object()))
    monkeypatch.setattr(index, "_delete_design", Mock(return_value="delete failed"))

    result = index.reindex_design_with_status(_design())

    assert result == CircuitVectorIndexStatus(available=True, indexed_count=0, error="delete failed")


def test_semantic_search_filters_allowed_designs_in_chroma_before_limit(monkeypatch):
    index = CircuitVectorIndex()
    monkeypatch.setattr(index, "_chroma_client", Mock(return_value=object()))
    collection = Mock()
    collection.query.return_value = {
        "ids": [["instance:z_allowed:U1"]],
        "documents": [["allowed semantic evidence"]],
        "metadatas": [[{
            "kind": "instance",
            "design_id": "z_allowed",
            "natural_id": "U1",
        }]],
        "distances": [[0.1]],
    }
    monkeypatch.setattr(index, "_embed_model", Mock(return_value=object()))
    monkeypatch.setattr(index, "_embed_batch", Mock(return_value=[[0.5, 0.5]]))
    monkeypatch.setattr(index, "_chroma_collection", Mock(return_value=collection))

    hits = index.semantic_search(
        "kb_hw",
        "semantic only question",
        top_k=1,
        kinds=("instance", "net"),
        allowed_design_ids={"z_allowed"},
        allowed_generations={"z_allowed": "gen-1"},
    )

    assert [hit.design_id for hit in hits] == ["z_allowed"]
    assert collection.query.call_args.kwargs["where"] == {
        "$and": [
            {"kind": {"$in": ["instance", "net"]}},
            {"design_id": {"$in": ["z_allowed"]}},
            {"generation_key": {"$in": ["z_allowed:gen-1"]}},
        ]
    }
