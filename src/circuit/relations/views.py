from __future__ import annotations

from .models import RelationEdge, RelationGraph


def power_relation_to_edge(edge: RelationEdge) -> dict:
    metadata = edge.metadata or {}
    return {
        "from_net": edge.source.id,
        "to_net": edge.target.id,
        "via_refdes": metadata.get("via_refdes") or (edge.via[0].id if edge.via else None),
        "via_type": metadata.get("via_type") or edge.relation_type,
        "via_label": metadata.get("label") or metadata.get("via_refdes") or edge.relation_type,
        "control_nets": list(metadata.get("control_nets") or []),
        "confidence": edge.confidence,
        "certainty": edge.certainty,
        "relation_type": edge.relation_type,
        "evidence": list(edge.evidence),
    }


def build_power_tree_view(graph: RelationGraph) -> dict:
    direct_types = {"power.input_to_output", "power.series_filter"}
    inferred_types = {"power.inferred_conversion"}
    direct_edges = [edge for edge in graph.edges if edge.relation_type in direct_types]
    inferred_edges = [edge for edge in graph.edges if edge.relation_type in inferred_types]

    return {
        "direct_edges": [power_relation_to_edge(edge) for edge in direct_edges],
        "derived_edges": [power_relation_to_edge(edge) for edge in direct_edges if edge.certainty == "derived"],
        "inferred_edges": [power_relation_to_edge(edge) for edge in inferred_edges],
        "ambiguous_edges": [],
        "control_edges": [],
        "relation_summary": {
            "direct_or_derived_count": len(direct_edges),
            "inferred_count": len(inferred_edges),
            "ambiguous_count": 0,
        },
    }
