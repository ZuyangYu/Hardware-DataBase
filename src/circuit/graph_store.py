"""Persist a linear component-pin-net connectivity graph for a circuit design.

The persisted graph deliberately models each connection as two edges:
``component -> pin`` and ``pin -> net``.  A high-fanout net therefore grows
linearly with its number of pin connections rather than producing a
component-component clique.
"""

from __future__ import annotations

import os
import pickle
from dataclasses import dataclass
from typing import Any

from src.circuit.models import CircuitDesign, ComponentInstance


_GPICKLE_NAME = "connectivity_graph.gpickle"


@dataclass(frozen=True)
class GraphIndexResult:
    """Summary of a graph artifact written by :meth:`GraphStore.save`."""

    path: str
    node_count: int
    edge_count: int


def _try_import_networkx():
    try:
        import networkx as nx  # type: ignore

        return nx
    except Exception:  # pragma: no cover - optional dep
        return None


class GraphStore:
    """Persist and reload a component-pin-net graph for a circuit design.

    NetworkX is used when available.  Minimal environments receive a plain
    dictionary representation with the same node IDs, attributes, and edge
    projection so retrieval code does not need to know which representation
    was persisted.
    """

    def build_graph(self, design: CircuitDesign) -> Any:
        """Build a graph using stable component, pin, and net node IDs."""

        nodes, edges, graph_metadata = self._build_projection(design)
        nx = _try_import_networkx()
        if nx is None:
            return self._build_fallback(nodes, edges, graph_metadata)

        graph = nx.Graph()
        graph.graph.update(graph_metadata)
        for node_id, attrs in nodes.items():
            graph.add_node(node_id, **attrs)
        for edge in edges:
            graph.add_edge(edge["src"], edge["dst"], relation=edge["relation"])
        return graph

    def save(self, design: CircuitDesign, design_dir: str) -> GraphIndexResult:
        """Persist a graph and return its path and exact node/edge counts."""

        os.makedirs(design_dir, exist_ok=True)
        graph = self.build_graph(design)
        target = os.path.join(design_dir, _GPICKLE_NAME)
        with open(target, "wb") as f:
            pickle.dump(graph, f)
            f.flush()
            os.fsync(f.fileno())
        node_count, edge_count = self._graph_counts(graph)
        return GraphIndexResult(path=target, node_count=node_count, edge_count=edge_count)

    def load(self, design_dir: str):
        target = os.path.join(design_dir, _GPICKLE_NAME)
        if not os.path.exists(target):
            return None
        with open(target, "rb") as f:
            return pickle.load(f)

    @staticmethod
    def connected_entities(
        graph: Any,
        *,
        refdes: str = "",
        net_name: str = "",
        pin: str = "",
    ) -> list[dict[str, Any]]:
        """Return entities connected to a component, pin, or net.

        ``graph`` may be either the NetworkX graph or the dictionary fallback
        emitted by :meth:`build_graph`.  Component lookups expand through
        their pins to include the attached nets; net lookups expand through
        their pins to include the attached components.  Pin lookups return
        their component and net neighbors.  Results are sorted by stable node
        ID, making both graph representations equivalent for retrieval.
        """

        nodes = GraphStore._iter_nodes(graph)
        edges = GraphStore._iter_edges(graph)
        if not nodes:
            return []

        adjacency: dict[str, set[str]] = {node_id: set() for node_id in nodes}
        for src, dst, _attrs in edges:
            if src not in adjacency:
                adjacency[src] = set()
            if dst not in adjacency:
                adjacency[dst] = set()
            adjacency[src].add(dst)
            adjacency[dst].add(src)

        target_ids = GraphStore._query_targets(nodes, refdes=refdes, net_name=net_name, pin=pin)
        if not target_ids:
            return []

        result_ids: set[str] = set()
        for target_id in target_ids:
            target_kind = nodes.get(target_id, {}).get("kind")
            direct_neighbors = adjacency.get(target_id, set())
            result_ids.update(direct_neighbors)
            if target_kind == "component":
                for neighbor_id in direct_neighbors:
                    if nodes.get(neighbor_id, {}).get("kind") != "pin":
                        continue
                    result_ids.update(adjacency.get(neighbor_id, set()))
            elif target_kind == "net":
                for neighbor_id in direct_neighbors:
                    if nodes.get(neighbor_id, {}).get("kind") != "pin":
                        continue
                    result_ids.update(adjacency.get(neighbor_id, set()))

        result_ids.difference_update(target_ids)
        return [
            {"id": node_id, **dict(nodes[node_id])}
            for node_id in sorted(result_ids)
            if node_id in nodes
        ]

    # ── shared projection ──

    @staticmethod
    def _build_projection(
        design: CircuitDesign,
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]], dict[str, str]]:
        source_name = GraphStore._source_name(design)
        graph_metadata = {"design_id": design.design_id, "source_name": source_name}
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, str]] = []
        edge_keys: set[tuple[str, str, str]] = set()
        instances_by_refdes: dict[str, ComponentInstance] = {}

        def add_edge(src: str, dst: str, relation: str) -> None:
            key = (src, dst, relation)
            if key in edge_keys:
                return
            edge_keys.add(key)
            edges.append({"src": src, "dst": dst, "relation": relation})

        def component_attrs(refdes: str, instance: ComponentInstance | None) -> dict[str, Any]:
            return {
                "kind": "component",
                "design_id": design.design_id,
                "source_name": source_name,
                "refdes": refdes,
                "library_cell": instance.library_cell if instance else None,
                "part_number": instance.part_number if instance else None,
                "footprint": instance.footprint if instance else None,
                "value": instance.value if instance else None,
                "erp_number": instance.erp_number if instance else None,
                "properties": dict(instance.properties) if instance else {},
            }

        for instance in design.instances:
            refdes = str(instance.refdes or "").strip()
            if not refdes or refdes in instances_by_refdes:
                continue
            instances_by_refdes[refdes] = instance
            nodes[f"component:{refdes}"] = component_attrs(refdes, instance)

        for net in design.nets:
            net_name = str(net.name or "")
            net_id = f"net:{net_name}"
            nodes.setdefault(
                net_id,
                {
                    "kind": "net",
                    "design_id": design.design_id,
                    "source_name": source_name,
                    "name": net_name,
                    "net_name": net_name,
                    "net_type": net.net_type,
                },
            )
            for connection in net.connections:
                refdes = str(connection.refdes or "").strip()
                if not refdes:
                    continue
                pin_name = str(connection.pin or "?").strip() or "?"
                component_id = f"component:{refdes}"
                if component_id not in nodes:
                    nodes[component_id] = component_attrs(refdes, instances_by_refdes.get(refdes))

                pin_id = f"pin:{refdes}.{pin_name}"
                nodes.setdefault(
                    pin_id,
                    {
                        "kind": "pin",
                        "design_id": design.design_id,
                        "source_name": source_name,
                        "refdes": refdes,
                        "pin": pin_name,
                        "pin_name": pin_name,
                    },
                )
                add_edge(component_id, pin_id, "contains")
                add_edge(pin_id, net_id, "on_net")

        return nodes, edges, graph_metadata

    @staticmethod
    def _build_fallback(
        nodes: dict[str, dict[str, Any]],
        edges: list[dict[str, str]],
        graph_metadata: dict[str, str],
    ) -> dict[str, Any]:
        return {
            "type": "component_pin_net",
            **graph_metadata,
            "nodes": nodes,
            "edges": edges,
        }

    @staticmethod
    def _source_name(design: CircuitDesign) -> str:
        if not design.files:
            return ""
        for source in design.files:
            file_type = str(source.file_type or "").casefold()
            filename = str(source.file_name or "")
            extension = os.path.splitext(filename)[1].casefold()
            if file_type in {"edf", "edif", "netlist"} or extension in {".edf", ".edif"}:
                return filename
        return str(design.files[0].file_name or "")

    @staticmethod
    def _graph_counts(graph: Any) -> tuple[int, int]:
        if hasattr(graph, "number_of_nodes"):
            return graph.number_of_nodes(), graph.number_of_edges()
        return len(graph.get("nodes", {})), len(graph.get("edges", []))

    @staticmethod
    def _iter_nodes(graph: Any) -> dict[str, dict[str, Any]]:
        if hasattr(graph, "nodes"):
            return {node_id: dict(attrs) for node_id, attrs in graph.nodes(data=True)}
        return {node_id: dict(attrs) for node_id, attrs in graph.get("nodes", {}).items()}

    @staticmethod
    def _iter_edges(graph: Any) -> list[tuple[str, str, dict[str, Any]]]:
        if hasattr(graph, "edges"):
            return [(src, dst, dict(attrs)) for src, dst, attrs in graph.edges(data=True)]
        return [
            (edge.get("src", ""), edge.get("dst", ""), dict(edge))
            for edge in graph.get("edges", [])
        ]

    @staticmethod
    def _query_targets(
        nodes: dict[str, dict[str, Any]],
        *,
        refdes: str,
        net_name: str,
        pin: str,
    ) -> set[str]:
        targets: set[str] = set()
        clean_refdes = str(refdes or "").strip()
        clean_net_name = str(net_name or "").strip()
        clean_pin = str(pin or "").strip()

        if clean_refdes:
            target_id = clean_refdes.removeprefix("component:")
            target_id = f"component:{target_id}"
            if target_id in nodes:
                targets.add(target_id)
        if clean_net_name:
            target_id = clean_net_name.removeprefix("net:")
            target_id = f"net:{target_id}"
            if target_id in nodes:
                targets.add(target_id)
        if clean_pin:
            target_pin = clean_pin.removeprefix("pin:")
            if "." not in target_pin and clean_refdes:
                target_pin = f"{clean_refdes.removeprefix('component:')}.{target_pin}"
            if "." in target_pin:
                target_id = f"pin:{target_pin}"
                if target_id in nodes:
                    targets.add(target_id)
            else:
                targets.update(
                    node_id
                    for node_id, attrs in nodes.items()
                    if attrs.get("kind") == "pin" and attrs.get("pin") == target_pin
                )
        return targets
