from __future__ import annotations

from typing import Any

from src.agents.schemas import Evidence


class CircuitEvidenceMapper:
    """Convert structured circuit-query rows into grounded agent evidence."""

    def build(
        self,
        *,
        kind: str,
        row: dict[str, Any],
        metadata: dict[str, Any],
        source_name: str,
        score: float,
    ) -> Evidence:
        design_id = str(row.get("design_id") or row.get("circuit_id") or "")
        entity_id, content = self._render(kind, row)
        record_id = metadata.get("record_id")
        return Evidence(
            id=f"circuit:{record_id or design_id}:{kind}:{entity_id}",
            content=content,
            source_name=source_name,
            content_kind="circuit_design",
            processor_kind="circuit_design",
            score=score,
            locator={
                "record_id": record_id,
                "circuit_id": design_id,
                "entity_type": kind,
                "entity_id": entity_id,
            },
            metadata={
                "kb_name": metadata.get("kb_name", ""),
                "department_id": metadata.get("department_id", ""),
                "source_group": "circuit_design",
                "evidence_kind": "derived_topology" if kind in {"topology", "power_topology"} else "circuit_fact",
                "fact_type": "relationship"
                if kind in {"net", "module_connection", "module_power", "pin_mapping", "power_topology"}
                else "entity"
                if kind in {"instance", "module"}
                else "topology",
                "part_numbers": [str(row["part_number"])] if row.get("part_number") else [],
                "certainty": row.get("certainty", "direct"),
                "capability_candidate": bool(row.get("capability_candidate")),
            },
        )

    def _render(self, kind: str, row: dict[str, Any]) -> tuple[str, str]:
        if kind == "power_topology":
            edge_text = []
            for edge in row.get("conversion_edges") or []:
                source = str(edge.get("from_net") or "")
                target = str(edge.get("to_net") or "")
                refdes = str(edge.get("via_refdes") or "")
                if not (source and target and refdes):
                    continue
                label = str(edge.get("via_label") or "")
                controls = ", ".join(str(item) for item in edge.get("control_nets") or [] if item)
                via = f"{refdes} ({label})" if label else refdes
                if controls:
                    via = f"{via}, controls: {controls}"
                edge_text.append(f"{source} -> {via} -> {target}")
            content = "; ".join(edge_text)
            return "power_path", f"Power conversion path: {content}." if content else "Power conversion path is unavailable."

        if kind == "net":
            net_name = str(row.get("net_name") or row.get("name") or "net")
            endpoints = ", ".join(
                str(item.get("endpoint") or item.get("refdes") or "")
                for item in row.get("connections") or []
                if item
            )
            content = f"Net {net_name} connects {endpoints}." if endpoints else f"Net {net_name} is present."
            return net_name, content

        if kind == "instance":
            refdes = str(row.get("refdes") or "instance")
            details = [row.get("library_cell"), row.get("part_number"), row.get("value"), row.get("footprint")]
            detail_text = ", ".join(str(item) for item in details if item)
            return refdes, f"Instance {refdes}: {detail_text}." if detail_text else f"Instance {refdes} is present."

        if kind == "pin_mapping":
            refdes = str(row.get("refdes") or "instance")
            pin_pairs = ", ".join(
                f"{pin.get('name')} -> {pin.get('net_name')}"
                for pin in row.get("pins") or []
                if pin.get("name") and pin.get("net_name")
            )
            content = f"Pin mapping for {refdes}: {pin_pairs}." if pin_pairs else f"Pin mapping for {refdes} is unavailable."
            return refdes, content

        if kind == "module":
            module_id = str(row.get("module_id") or row.get("name") or "module")
            name = str(row.get("name") or module_id)
            instances = ", ".join(str(item) for item in (row.get("instances") or [])[:12])
            content = f"Module {name} contains {instances}." if instances else f"Module {name} is present."
            return module_id, content

        if kind == "module_connection":
            left = str(row.get("from_module") or row.get("module") or "module")
            right = str(row.get("to_module") or "")
            net = str(row.get("net") or "")
            entity_id = ":".join(part for part in (left, right, net) if part)
            if right:
                content = f"Module {left} connects to {right} through net {net}."
            else:
                content = f"Module {left} uses net {net}."
            return entity_id or left, content

        if kind == "module_power":
            module_id = str(row.get("module_id") or row.get("name") or "module")
            name = str(row.get("name") or module_id)
            power = ", ".join(str(item.get("name") or "") for item in row.get("power_nets") or [] if item)
            ground = ", ".join(str(item.get("name") or "") for item in row.get("ground_nets") or [] if item)
            facts = []
            if power:
                facts.append(f"power nets {power}")
            if ground:
                facts.append(f"ground nets {ground}")
            return module_id, f"Module {name} has {'; '.join(facts)}." if facts else f"Module {name} has no classified power nets."

        if kind == "topology":
            topology = str(row.get("topology") or "topology")
            refdes = str(row.get("refdes") or "component")
            entity_id = f"{topology}:{refdes}"
            if topology in {"pull_up", "pull_down"}:
                content = (
                    f"Observed {topology.replace('_', '-')} resistor {refdes} ({row.get('value') or 'value unknown'}) "
                    f"connects signal net {row.get('signal_net')} to {row.get('rail_net')}."
                )
            elif topology == "power_control_candidate":
                inputs = ", ".join(str(item) for item in row.get("input_nets") or []) or "no input power net classified"
                outputs = ", ".join(str(item) for item in row.get("protected_nets") or []) or "no output power net classified"
                content = (
                    f"Observed power-control candidate {refdes} connects input power nets {inputs} to output power nets {outputs}; "
                    "a matching datasheet capability clause is required before claiming protection."
                )
            else:
                protected = ", ".join(str(item) for item in row.get("protected_nets") or []) or "no signal net classified"
                ground = ", ".join(str(item) for item in row.get("ground_nets") or []) or "no ground net classified"
                content = (
                    f"Observed {topology.replace('_', ' ')} component {refdes} connects protected nets {protected} "
                    f"and ground nets {ground}; this topology does not confirm short-circuit protection capability."
                )
            return entity_id, content

        raise ValueError(f"Unsupported circuit evidence kind: {kind}")
