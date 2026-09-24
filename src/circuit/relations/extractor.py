from __future__ import annotations

from src.circuit.models import CircuitDesign
from src.circuit.parsers.edf_power import classify_net_name

from .models import RelationEdge, RelationGraph


class RelationExtractor:
    """Extract direct, source-backed connectivity facts from a CircuitDesign."""

    def extract(self, design: CircuitDesign) -> RelationGraph:
        graph = RelationGraph()

        for net in design.nets:
            role = net.net_type if net.net_type in {"power", "ground"} else classify_net_name(net.name)
            net_tags = tuple(tag for tag in (role,) if tag in {"power", "ground"})
            net_node = graph.node("net", net.name, tags=net_tags)
            for conn in net.connections:
                inst_node = graph.node("instance", conn.refdes)
                pin_id = f"{conn.refdes}.{conn.pin or '?'}"
                pin_node = graph.node("pin", pin_id, label=pin_id)
                graph.add_edge(
                    RelationEdge(
                        id=f"pin_on_net:{pin_id}->{net.name}",
                        relation_type="connectivity.pin_on_net",
                        source=pin_node,
                        target=net_node,
                        via=[inst_node],
                        evidence=[f"{pin_id} -> {net.name}"],
                        certainty="direct",
                        tags=["connectivity"],
                    )
                )
                graph.add_edge(
                    RelationEdge(
                        id=f"instance_uses_net:{conn.refdes}->{net.name}",
                        relation_type="connectivity.instance_uses_net",
                        source=inst_node,
                        target=net_node,
                        evidence=[f"{conn.refdes}.{conn.pin or '?'} -> {net.name}"],
                        certainty="direct",
                        tags=["connectivity"],
                    )
                )

        for module in design.modules:
            module_node = graph.node("module", module.module_id, label=module.name)
            for refdes in module.instances:
                inst_node = graph.node("instance", refdes)
                graph.add_edge(
                    RelationEdge(
                        id=f"module_contains:{module.module_id}->{refdes}",
                        relation_type="module.contains_instance",
                        source=module_node,
                        target=inst_node,
                        evidence=[f"module {module.name} contains {refdes}"],
                        certainty="direct",
                        tags=["module"],
                    )
                )
            for net_name in module.nets:
                net_node = graph.node("net", net_name)
                graph.add_edge(
                    RelationEdge(
                        id=f"module_uses_net:{module.module_id}->{net_name}",
                        relation_type="module.uses_net",
                        source=module_node,
                        target=net_node,
                        evidence=[f"module {module.name} uses {net_name}"],
                        certainty="direct",
                        tags=["module"],
                    )
                )

        return graph
