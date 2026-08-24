from __future__ import annotations

from typing import Any

from src.agents.state import Evidence


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
        summary_kinds = {"circuit_overview", "module_list", "component_identity", "resolution_status"}
        evidence_metadata = {
            "kb_name": metadata.get("kb_name", ""),
            "department_id": metadata.get("department_id", ""),
            "source_group": "circuit_design",
            "evidence_kind": "circuit_summary" if kind in summary_kinds else "derived_topology" if kind in {"topology", "power_topology"} else "circuit_fact",
            "fact_type": "relationship"
            if kind in {"net", "module_connection", "module_power", "pin_mapping", "power_topology"}
            else "entity"
            if kind in {"instance", "module"}
            else "topology",
            "part_numbers": [str(row["part_number"])] if row.get("part_number") else [],
            "certainty": row.get("certainty", "direct"),
            "capability_candidate": bool(row.get("capability_candidate")),
        }
        if kind == "pin_mapping":
            evidence_metadata["pin_mappings"] = self._pin_mappings(row)
        if kind in summary_kinds:
            evidence_metadata.pop("certainty", None)
            evidence_metadata["fact_type"] = "summary"
            if row.get("source_kind") is not None:
                evidence_metadata["source_kind"] = str(row["source_kind"])
            if row.get("confidence") is not None:
                evidence_metadata["confidence"] = float(row["confidence"])
            if row.get("resolution_status") is not None:
                evidence_metadata["resolution_status"] = str(row["resolution_status"])
                evidence_metadata["candidate_count"] = int(row.get("candidate_count") or 0)
            if row.get("coverage") is not None:
                evidence_metadata["coverage"] = row["coverage"]
            if row.get("role_term") is not None:
                evidence_metadata["role_term"] = str(row["role_term"])
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
                **({"refdes": str(row["refdes"])} if row.get("refdes") else {}),
            },
            metadata=evidence_metadata,
        )

    @staticmethod
    def _pin_mappings(row: dict[str, Any]) -> list[dict[str, Any]]:
        mappings: list[dict[str, Any]] = []
        for pin in row.get("pins") or []:
            raw_pin_name = str(pin.get("name") or "").strip()
            if not raw_pin_name:
                continue
            net_name = str(pin.get("net_name") or "").strip() or None
            mappings.append(
                {
                    "raw_pin_name": raw_pin_name,
                    "pin_name": raw_pin_name.removeprefix("&"),
                    "net_name": net_name,
                    "connection_state": "connected" if net_name else "unconnected",
                }
            )
        return mappings

    def _render(self, kind: str, row: dict[str, Any]) -> tuple[str, str]:
        if kind == "circuit_overview":
            design_id = str(row.get("design_id") or "design")
            coverage = row.get("coverage") or {}
            label = {"available": "具备", "unavailable": "不具备", "unknown": "未知"}
            strategy = str(coverage.get("module_partition_strategy") or "none")
            parts = [
                f"设计 {design_id} 结构：实例 {row.get('instance_count', 0)}、网络 {row.get('net_count', 0)}、模块 {row.get('module_count', 0)} 个"
            ]
            if strategy == "refdes_page_heuristic":
                parts.append("模块划分策略 refdes_page（启发式分组，不代表视觉页面）")
            elif strategy == "source_page":
                parts.append("模块划分策略 source_page（源页归属）")
            else:
                parts.append("无模块划分")
            availability_text = "；".join(
                f"{label_zh}{label.get(str(coverage.get(field)) or 'unknown')}"
                for label_zh, field in (
                    ("页面", "schematic_pages"),
                    ("标题栏", "title_block"),
                    ("坐标", "coordinates"),
                    ("视觉布局", "visual_layout"),
                )
            )
            parts.append(availability_text)
            return f"{design_id}:overview", f"{parts[0]}；{parts[1]}；{parts[2]}。"

        if kind == "module_list":
            design_id = str(row.get("design_id") or "design")
            modules = row.get("modules") or []
            if modules:
                listing = "; ".join(
                    f"{item.get('name') or item.get('module_id')}[策略 {item.get('strategy')}, 实例 {item.get('instance_count', 0)}]"
                    for item in modules[:12]
                )
                content = f"设计 {design_id} 共 {row.get('module_count', len(modules))} 个模块：{listing}。"
            else:
                content = f"设计 {design_id} 当前解析结果中没有模块划分。"
            return f"{design_id}:modules", content

        if kind == "component_identity":
            refdes = str(row.get("refdes") or "component")
            roles = row.get("roles") or []
            role_texts = []
            for role in roles:
                locator = role.get("source_locator") or {}
                basis = ", ".join(f"{key}={value}" for key, value in locator.items() if value)
                role_texts.append(
                    f"角色 {str(role.get('display_name') or role.get('role_id')).upper()}"
                    f"（依据 {basis or '受控目录'}，来源 {role.get('source_kind')}，置信度 {float(role.get('confidence') or 0):.2f}）"
                )
            identifiers = row.get("identifiers") or []
            identifier_texts = [
                f"{item.get('namespace')}={item.get('raw_value')}" for item in identifiers[:6]
            ]
            segments = [f"{refdes} 身份解析（{row.get('resolution_status')}，匹配方式 {row.get('matched_by')}）"]
            if role_texts:
                segments.append("；".join(role_texts))
            if identifier_texts:
                segments.append(f"标识符 {', '.join(identifier_texts)}")
            return refdes, "；".join(segments) + "。"

        if kind == "resolution_status":
            term = str(row.get("term") or "")
            status = str(row.get("resolution_status") or "no_evidence")
            candidates = row.get("candidates") or []
            if status == "ambiguous":
                listing = "、".join(
                    f"{item.get('refdes')}（{'/'.join(str(r) for r in (item.get('roles') or [])) or '未标注角色'}）"
                    for item in candidates[:8]
                )
                content = (
                    f"“{term}”匹配到多个候选器件：{listing}。请指定位号或型号后再展开连接关系。"
                )
            else:
                content = (
                    f"资料中没有可验证的“{term}”角色证据；请补充 BOM、器件型号或位号后再查询连接关系。"
                )
            entity_id = f"{row.get('circuit_id') or 'kb'}:{status}:{term}"
            return entity_id, content

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
                f"{pin['pin_name']} -> {pin['net_name'] or 'NC（源文件未声明网络连接）'}"
                for pin in self._pin_mappings(row)
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
