"""Connectivity graph persistence for circuit designs.

Plan section 3.4 prescribes a `connectivity_graph.gpickle` next to each
`circuit.json` so that NetworkX queries (path tracing, community detection)
can resume without re-parsing the EDF.  This module materializes that file
from `CircuitDesign.instances + .nets` and loads it back lazily.

The graph is undirected: nodes are component refdes, edges are nets carrying
that connection.  Each edge gets a `net` attribute, each node carries the
`library_cell`/`part_number` for downstream filtering.
"""

from __future__ import annotations

import os
import pickle
from typing import Any

from src.circuit.models import CircuitDesign


_GPICKLE_NAME = "connectivity_graph.gpickle"


def _try_import_networkx():
    try:
        import networkx as nx  # type: ignore

        return nx
    except Exception:  # pragma: no cover - optional dep
        return None


class GraphStore:
    """Persist & reload a NetworkX connectivity graph for a circuit design.

    Falls back to a plain dict-of-lists if `networkx` is not installed so the
    rest of the pipeline keeps working in minimal environments.
    """

    def build_graph(self, design: CircuitDesign) -> Any:
        nx = _try_import_networkx()
        if nx is None:
            return self._build_fallback(design)

        graph = nx.Graph()
        for instance in design.instances:
            graph.add_node(
                instance.refdes,
                library_cell=instance.library_cell,
                part_number=instance.part_number,
            )
        for net in design.nets:
            refs = [conn.refdes for conn in net.connections if conn.refdes]
            for i, src in enumerate(refs):
                for dst in refs[i + 1 :]:
                    if src == dst:
                        continue
                    graph.add_edge(src, dst, net=net.name, net_type=net.net_type)
        return graph

    def save(self, design: CircuitDesign, design_dir: str) -> str:
        os.makedirs(design_dir, exist_ok=True)
        graph = self.build_graph(design)
        target = os.path.join(design_dir, _GPICKLE_NAME)
        with open(target, "wb") as f:
            pickle.dump(graph, f)
            f.flush()
            os.fsync(f.fileno())
        return target

    def load(self, design_dir: str):
        target = os.path.join(design_dir, _GPICKLE_NAME)
        if not os.path.exists(target):
            return None
        with open(target, "rb") as f:
            return pickle.load(f)

    # ── fallback (no networkx installed) ──

    def _build_fallback(self, design: CircuitDesign) -> dict[str, Any]:
        adjacency: dict[str, set[str]] = {inst.refdes: set() for inst in design.instances}
        edges: list[dict[str, Any]] = []
        for net in design.nets:
            refs = [conn.refdes for conn in net.connections if conn.refdes]
            for i, src in enumerate(refs):
                for dst in refs[i + 1 :]:
                    if src == dst:
                        continue
                    adjacency.setdefault(src, set()).add(dst)
                    adjacency.setdefault(dst, set()).add(src)
                    edges.append({"src": src, "dst": dst, "net": net.name, "net_type": net.net_type})
        return {
            "type": "fallback_adjacency",
            "nodes": {
                inst.refdes: {
                    "library_cell": inst.library_cell,
                    "part_number": inst.part_number,
                }
                for inst in design.instances
            },
            "adjacency": {k: sorted(v) for k, v in adjacency.items()},
            "edges": edges,
        }
