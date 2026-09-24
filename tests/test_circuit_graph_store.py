from __future__ import annotations

from pathlib import Path

import pytest

from src.circuit.graph_store import GraphStore
from src.circuit.models import CircuitDesign, ComponentInstance, DesignFile, Net, Pin, PinRef


def _minimal_design() -> CircuitDesign:
    return CircuitDesign(
        design_id="board-1",
        kb_name="kb-hardware",
        files=[DesignFile("board.edf", "edf", "netlist", "/tmp/board.edf")],
        instances=[
            ComponentInstance(
                "R1",
                library_cell="RES",
                part_number="R-100",
                value="10K",
                pins=[Pin("1", "SHARED")],
            ),
            ComponentInstance(
                "C1",
                library_cell="CAP",
                part_number="C-100",
                value="100nF",
                pins=[Pin("1", "SHARED")],
            ),
            ComponentInstance(
                "U1",
                library_cell="IC",
                part_number="U-100",
                pins=[Pin("1", "SECOND")],
            ),
        ],
        nets=[
            Net("SHARED", [PinRef("R1", "1"), PinRef("C1", "1")]),
            Net("SECOND", [PinRef("U1", "1")], net_type="power"),
        ],
    )


def _nodes(graph) -> dict[str, dict]:
    if hasattr(graph, "nodes"):
        return {node_id: dict(attrs) for node_id, attrs in graph.nodes(data=True)}
    return {node_id: dict(attrs) for node_id, attrs in graph["nodes"].items()}


def _edges(graph) -> list[tuple[str, str, dict]]:
    if hasattr(graph, "edges"):
        return [(src, dst, dict(attrs)) for src, dst, attrs in graph.edges(data=True)]
    return [(edge["src"], edge["dst"], dict(edge)) for edge in graph["edges"]]


def _assert_component_pin_net_shape(graph, *, expected_nodes: set[str], expected_edges: int) -> None:
    nodes = _nodes(graph)
    edges = _edges(graph)
    assert set(nodes) == expected_nodes
    assert len(edges) == expected_edges

    component_ids = {node_id for node_id in nodes if node_id.startswith("component:")}
    for src, dst, attrs in edges:
        assert attrs["relation"] in {"contains", "on_net"}
        assert not ({src, dst} <= component_ids)


def test_graph_uses_component_pin_net_nodes_without_component_clique():
    graph = GraphStore().build_graph(_minimal_design())

    _assert_component_pin_net_shape(
        graph,
        expected_nodes={
            "component:R1",
            "component:C1",
            "component:U1",
            "pin:R1.1",
            "pin:C1.1",
            "pin:U1.1",
            "net:SHARED",
            "net:SECOND",
        },
        expected_edges=6,
    )
    nodes = _nodes(graph)
    assert nodes["component:R1"]["kind"] == "component"
    assert nodes["component:R1"]["design_id"] == "board-1"
    assert nodes["component:R1"]["source_name"] == "board.edf"
    assert nodes["component:R1"]["part_number"] == "R-100"
    assert nodes["component:R1"]["value"] == "10K"
    assert nodes["net:SECOND"]["net_type"] == "power"
    assert nodes["net:SECOND"]["source_name"] == "board.edf"


def test_high_fanout_net_graph_size_is_linear():
    design = CircuitDesign(
        design_id="fanout",
        kb_name="kb-hardware",
        instances=[ComponentInstance(f"U{i}") for i in range(100)],
        nets=[Net("FANOUT", [PinRef(f"U{i}", "1") for i in range(100)])],
    )

    graph = GraphStore().build_graph(design)
    nodes = _nodes(graph)
    edges = _edges(graph)

    assert len(nodes) == 100 + 100 + 1
    assert len(edges) == 200
    assert not any(
        src.startswith("component:") and dst.startswith("component:")
        for src, dst, _attrs in edges
    )


def test_graph_preserves_multiple_nets_between_the_same_components():
    design = CircuitDesign(
        design_id="multi-net",
        kb_name="kb-hardware",
        instances=[ComponentInstance("R1"), ComponentInstance("R2")],
        nets=[
            Net("NET_A", [PinRef("R1", "1"), PinRef("R2", "1")]),
            Net("NET_B", [PinRef("R1", "2"), PinRef("R2", "2")]),
        ],
    )

    graph = GraphStore().build_graph(design)
    nodes = _nodes(graph)
    edges = _edges(graph)

    assert {"net:NET_A", "net:NET_B"} <= set(nodes)
    assert len(edges) == 8
    assert sum(attrs["relation"] == "on_net" for _src, _dst, attrs in edges) == 4


def test_save_returns_index_result_with_persisted_counts(tmp_path: Path):
    result = GraphStore().save(_minimal_design(), str(tmp_path))

    assert result.path == str(tmp_path / "connectivity_graph.gpickle")
    assert result.node_count == 8
    assert result.edge_count == 6
    assert Path(result.path).exists()


def test_fallback_graph_has_equivalent_component_pin_net_semantics(monkeypatch):
    monkeypatch.setattr("src.circuit.graph_store._try_import_networkx", lambda: None)

    graph = GraphStore().build_graph(_minimal_design())

    _assert_component_pin_net_shape(
        graph,
        expected_nodes={
            "component:R1",
            "component:C1",
            "component:U1",
            "pin:R1.1",
            "pin:C1.1",
            "pin:U1.1",
            "net:SHARED",
            "net:SECOND",
        },
        expected_edges=6,
    )
    assert graph["nodes"]["component:R1"]["part_number"] == "R-100"
    assert graph["nodes"]["net:SECOND"]["net_type"] == "power"


@pytest.mark.parametrize("fallback", [False, True])
def test_connected_entities_by_refdes_pin_and_net(monkeypatch, fallback: bool):
    if fallback:
        monkeypatch.setattr("src.circuit.graph_store._try_import_networkx", lambda: None)
    graph = GraphStore().build_graph(_minimal_design())

    by_refdes = GraphStore.connected_entities(graph, refdes="R1")
    by_pin = GraphStore.connected_entities(graph, pin="R1.1")
    by_net = GraphStore.connected_entities(graph, net_name="SHARED")

    assert {item["id"] for item in by_refdes} == {"pin:R1.1", "net:SHARED"}
    assert {item["id"] for item in by_pin} == {"component:R1", "net:SHARED"}
    assert {item["id"] for item in by_net} == {
        "component:R1",
        "component:C1",
        "pin:R1.1",
        "pin:C1.1",
    }
