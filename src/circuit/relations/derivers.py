from __future__ import annotations

from collections import defaultdict

from src.circuit.models import CircuitDesign, ComponentInstance, Pin
from src.circuit.parsers.edf_power import classify_net_name

from .models import RelationEdge, RelationGraph, RelationNode


INPUT_PIN_KEYS = {"VIN", "IN", "PVIN", "AVIN", "VBAT", "BATT", "BAT", "VBIAS", "VCCIN", "VDDIN"}
OUTPUT_PIN_KEYS = {"VOUT", "OUT", "OUTPUT", "VREG", "VLDO", "LDO", "BUCK", "VCCOUT", "VDDOUT", "QOD"}
SWITCH_PIN_KEYS = {"SW", "LX", "PH", "BOOTSW"}
ENABLE_PIN_KEYS = {"EN", "ENABLE", "ON", "CE", "SHDN", "RUN", "ENSYNC"}
POWER_GOOD_PIN_KEYS = {"PG", "PGOOD", "PWRGD", "POWERGOOD", "POK", "RESETB"}
FEEDBACK_PIN_PREFIXES = ("FB", "VSNS", "VOSNS", "SENSE")
SERIES_LIB_TERMS = ("FERRITE", "FILTER", "INDUCTOR", "INDUCT")
OSCILLATOR_LIB_TERMS = ("CRYSTAL", "OSC", "XTAL")


def pin_key(name: str | None) -> str:
    import re

    return re.sub(r"[^A-Z0-9]+", "", str(name or "").upper().lstrip("&"))


def is_power_net(name: str | None) -> bool:
    return classify_net_name(name or "") == "power"


def is_ground_net(name: str | None) -> bool:
    return classify_net_name(name or "") == "ground"


def is_series_passive(inst: ComponentInstance) -> bool:
    refdes = (inst.refdes or "").upper()
    descriptor = " ".join(str(value or "").upper() for value in (inst.library_cell, inst.part_number, inst.value))
    return refdes.startswith(("L", "FB", "F")) or any(term in descriptor for term in SERIES_LIB_TERMS)


def is_probable_power_ic(inst: ComponentInstance) -> bool:
    refdes = (inst.refdes or "").upper()
    descriptor = " ".join(str(value or "").upper() for value in (inst.library_cell, inst.part_number, inst.value))
    keys = {pin_key(pin.name) for pin in inst.pins}
    if refdes.startswith(("Y", "X")) or any(term in descriptor for term in OSCILLATOR_LIB_TERMS):
        return False
    if not refdes.startswith("U"):
        return False
    if keys & INPUT_PIN_KEYS and (keys & OUTPUT_PIN_KEYS or keys & SWITCH_PIN_KEYS or any(key.startswith(FEEDBACK_PIN_PREFIXES) for key in keys)):
        return True
    return any(term in descriptor for term in ("PMIC", "BUCK", "DCDC", "DC-DC", "LDO", "REGULATOR", "LOADSW", "TPS", "LP877", "SA230"))


class RelationDeriver:
    """Derive semantic relationships from direct connectivity facts."""

    def derive(self, design: CircuitDesign, graph: RelationGraph) -> RelationGraph:
        net_connections = self._net_connections(design)
        instance_by_refdes = {inst.refdes: inst for inst in design.instances}

        for inst in design.instances:
            if is_series_passive(inst):
                self._derive_series_filter(inst, graph)
            if is_probable_power_ic(inst):
                self._derive_power_ic(inst, graph, net_connections, instance_by_refdes)

        return graph

    @staticmethod
    def _net_connections(design: CircuitDesign) -> dict[str, list[tuple[ComponentInstance, Pin]]]:
        instance_by_refdes = {inst.refdes: inst for inst in design.instances}
        by_net: dict[str, list[tuple[ComponentInstance, Pin]]] = defaultdict(list)
        for inst in design.instances:
            for pin in inst.pins:
                if pin.net:
                    by_net[pin.net].append((inst, pin))
        for net in design.nets:
            for conn in net.connections:
                inst = instance_by_refdes.get(conn.refdes)
                if not inst:
                    continue
                if any(existing_inst.refdes == conn.refdes and existing_pin.name == conn.pin for existing_inst, existing_pin in by_net[net.name]):
                    continue
                by_net[net.name].append((inst, Pin(conn.pin or "?", net.name)))
        return by_net

    @staticmethod
    def _pin_rows(inst: ComponentInstance) -> list[dict]:
        rows = []
        for pin in inst.pins:
            if pin.net and not is_ground_net(pin.net):
                rows.append({"pin": pin.name, "key": pin_key(pin.name), "net": pin.net})
        return rows

    def _derive_series_filter(self, inst: ComponentInstance, graph: RelationGraph) -> None:
        rows = self._pin_rows(inst)
        if len(rows) != 2:
            return
        left, right = rows
        if not (is_power_net(left["net"]) and is_power_net(right["net"])):
            return
        source_net = graph.node("net", left["net"])
        target_net = graph.node("net", right["net"])
        via = graph.node("instance", inst.refdes)
        graph.add_edge(
            RelationEdge(
                id=f"power.series_filter:{left['net']}->{right['net']}:{inst.refdes}",
                relation_type="power.series_filter",
                source=source_net,
                target=target_net,
                via=[via],
                evidence=[f"{inst.refdes}.{left['pin']} -> {left['net']}", f"{inst.refdes}.{right['pin']} -> {right['net']}"],
                confidence=0.66,
                certainty="derived",
                tags=["power", "series_passive"],
                metadata={"via_refdes": inst.refdes, "via_type": "series_passive", "label": inst.part_number or inst.library_cell or inst.value or inst.refdes},
            )
        )

    def _derive_power_ic(
        self,
        inst: ComponentInstance,
        graph: RelationGraph,
        net_connections: dict[str, list[tuple[ComponentInstance, Pin]]],
        instance_by_refdes: dict[str, ComponentInstance],
    ) -> None:
        rows = self._pin_rows(inst)
        inputs = [row for row in rows if row["key"] in INPUT_PIN_KEYS or row["key"].startswith("VIN") or row["key"].endswith("VIN")]
        outputs = [row for row in rows if row["key"] in OUTPUT_PIN_KEYS or row["key"].startswith("VOUT") or row["key"].endswith("OUT")]
        switches = [row for row in rows if row["key"] in SWITCH_PIN_KEYS or row["key"].startswith("SW")]
        feedbacks = [row for row in rows if row["key"].startswith(FEEDBACK_PIN_PREFIXES)]
        controls = [row for row in rows if row["key"] in ENABLE_PIN_KEYS or row["key"].endswith("EN") or row["key"] in POWER_GOOD_PIN_KEYS]

        input_nets = sorted({row["net"] for row in inputs})
        direct_output_nets = sorted({row["net"] for row in outputs if is_power_net(row["net"])})
        inferred_output_nets = sorted({row["net"] for row in feedbacks if is_power_net(row["net"])})
        switch_paths = self._switch_inductor_paths(inst, switches, net_connections)
        for item in switch_paths:
            inferred_output_nets.append(item["output_net"])
        inferred_output_nets = sorted(set(inferred_output_nets) - set(direct_output_nets))

        for source in input_nets:
            for target in direct_output_nets:
                self._add_power_edge(graph, inst, source, target, controls, "power.input_to_output", "derived", 0.78)
            for target in inferred_output_nets:
                path = next((item for item in switch_paths if item["output_net"] == target), None)
                evidence = []
                if path:
                    evidence = path["evidence"]
                self._add_power_edge(graph, inst, source, target, controls, "power.inferred_conversion", "inferred", 0.74, extra_evidence=evidence)

    @staticmethod
    def _switch_inductor_paths(inst: ComponentInstance, switches: list[dict], net_connections: dict[str, list[tuple[ComponentInstance, Pin]]]) -> list[dict]:
        paths = []
        for switch in switches:
            sw_net = switch["net"]
            for peer_inst, peer_pin in net_connections.get(sw_net, []):
                if peer_inst.refdes == inst.refdes or not is_series_passive(peer_inst):
                    continue
                other_pins = [pin for pin in peer_inst.pins if pin.net and pin.name != peer_pin.name and not is_ground_net(pin.net)]
                if len(other_pins) != 1:
                    continue
                out_net = other_pins[0].net
                if not is_power_net(out_net):
                    continue
                paths.append(
                    {
                        "switch_net": sw_net,
                        "inductor": peer_inst.refdes,
                        "output_net": out_net,
                        "evidence": [
                            f"{inst.refdes}.{switch['pin']} -> {sw_net}",
                            f"{peer_inst.refdes}.{peer_pin.name} -> {sw_net}",
                            f"{peer_inst.refdes}.{other_pins[0].name} -> {out_net}",
                        ],
                    }
                )
        return paths

    def _add_power_edge(
        self,
        graph: RelationGraph,
        inst: ComponentInstance,
        source: str,
        target: str,
        controls: list[dict],
        relation_type: str,
        certainty: str,
        confidence: float,
        *,
        extra_evidence: list[str] | None = None,
    ) -> None:
        if source == target:
            return
        source_node = graph.node("net", source)
        target_node = graph.node("net", target)
        via_nodes: list[RelationNode] = [graph.node("instance", inst.refdes)]
        evidence = [f"{inst.refdes} input -> {source}", f"{inst.refdes} output -> {target}"]
        evidence.extend(extra_evidence or [])
        control_nets = sorted({row["net"] for row in controls})
        for control in control_nets:
            evidence.append(f"{inst.refdes} control -> {control}")
        graph.add_edge(
            RelationEdge(
                id=f"{relation_type}:{source}->{target}:{inst.refdes}",
                relation_type=relation_type,
                source=source_node,
                target=target_node,
                via=via_nodes,
                evidence=evidence,
                confidence=confidence,
                certainty=certainty,
                tags=["power", "conversion"],
                metadata={
                    "via_refdes": inst.refdes,
                    "via_type": "power_device",
                    "label": inst.part_number or inst.library_cell or inst.value or inst.refdes,
                    "control_nets": control_nets,
                },
            )
        )
