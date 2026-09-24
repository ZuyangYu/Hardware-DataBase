from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RelationNode:
    """A node in the reusable circuit relation graph."""

    id: str
    type: str
    label: str | None = None
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "tags": list(self.tags),
        }


@dataclass
class RelationEdge:
    """A direct, derived or inferred relationship between circuit entities."""

    id: str
    relation_type: str
    source: RelationNode
    target: RelationNode
    via: list[RelationNode] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    confidence: float = 1.0
    certainty: str = "direct"
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "relation_type": self.relation_type,
            "source": self.source.to_dict(),
            "target": self.target.to_dict(),
            "via": [item.to_dict() for item in self.via],
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "certainty": self.certainty,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
        }


@dataclass
class RelationGraph:
    """Container plus small indexes for circuit relations."""

    nodes: dict[tuple[str, str], RelationNode] = field(default_factory=dict)
    edges: list[RelationEdge] = field(default_factory=list)

    def node(self, node_type: str, node_id: str, *, label: str | None = None, tags: tuple[str, ...] = ()) -> RelationNode:
        key = (node_type, node_id)
        existing = self.nodes.get(key)
        if existing:
            return existing
        created = RelationNode(id=node_id, type=node_type, label=label or node_id, tags=tags)
        self.nodes[key] = created
        return created

    def add_edge(self, edge: RelationEdge) -> None:
        if any(existing.id == edge.id for existing in self.edges):
            return
        self.edges.append(edge)

    def edges_by_type(self, relation_type: str) -> list[RelationEdge]:
        return [edge for edge in self.edges if edge.relation_type == relation_type]

    def edges_with_tag(self, tag: str) -> list[RelationEdge]:
        return [edge for edge in self.edges if tag in edge.tags]

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes.values()],
            "edges": [edge.to_dict() for edge in self.edges],
        }
