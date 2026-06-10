from llama_index.core.schema import NodeWithScore

import config.settings
from src.core.hybrid_retriever import hybrid_retrieve
from src.core.logger import log
from src.core.source_group_router import route_source_groups
from src.ingestion.source_groups import UNKNOWN_GROUP, classify_source_group, safe_source_group


def routed_retrieve(query: str, index, kb_name: str, top_k: int = 5) -> list[NodeWithScore]:
    """Retrieve with a light source-group router on top of the existing hybrid retriever."""
    route = route_source_groups(query)
    candidate_k = max(top_k * 4, config.settings.FINAL_TOP_K * 4, 12)
    candidates = hybrid_retrieve(query, index, kb_name, top_k=candidate_k)

    if not candidates:
        return []

    routed_nodes: list[NodeWithScore] = []
    for item in candidates:
        metadata = item.node.metadata or {}
        source_group = safe_source_group(metadata.get("source_group"))
        if source_group == UNKNOWN_GROUP and metadata.get("file_name"):
            source_group = classify_source_group(metadata["file_name"]).group
        weight = route.weights.get(source_group, route.weights.get(UNKNOWN_GROUP, 1.0))
        base_score = float(item.score or 0.0)
        routed_score = base_score * weight if base_score else weight
        routed_nodes.append(NodeWithScore(node=item.node, score=routed_score))

    routed_nodes.sort(key=lambda node: float(node.score or 0.0), reverse=True)
    log(f"Source group route: {route.reason}, weights={route.weights}")
    return routed_nodes[:top_k]
