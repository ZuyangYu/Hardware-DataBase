from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.agents.state import Evidence
from src.circuit.evidence_mapper import CircuitEvidenceMapper
from src.circuit.graph_store import GraphStore
from src.circuit.models import CircuitDesign, CircuitStatus, DesignFile
from src.circuit.parsers.edf_parser import EdfParser
from src.circuit.question_analysis import analyze_question
from src.circuit.query_engine import CircuitQueryEngine
from src.circuit.store import CircuitStore, make_design_id
from src.circuit.vector_index import KIND_INSTANCE, KIND_MODULE, KIND_NET, CircuitVectorIndex, default_circuit_vector_index
from src.pipelines.document_rag.schemas import RequestContext


META_FILE = "pipeline_metadata.json"


@dataclass
class CircuitIndexResult:
    ok: bool
    status: str
    message: str
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    design_id: str = ""


class CircuitIndexService:
    def __init__(
        self,
        *,
        store: CircuitStore | None = None,
        storage_root: str | None = None,
        parser_factory: Callable[..., Any] | None = None,
        query_engine: CircuitQueryEngine | None = None,
        graph_store: GraphStore | None = None,
        vector_index: CircuitVectorIndex | None = None,
    ):
        self.store = store or CircuitStore(root=storage_root)
        self.parser_factory = parser_factory or EdfParser
        self.query_engine = query_engine or CircuitQueryEngine(self.store)
        self.graph_store = graph_store or GraphStore()
        self.vector_index = vector_index or default_circuit_vector_index
        self.evidence_mapper = CircuitEvidenceMapper()

    def index_file(
        self,
        *,
        kb_name: str,
        record_id: int | None,
        file_path: str,
        original_name: str,
        department_id: str | None = None,
        uploaded_by: str = "",
    ) -> CircuitIndexResult:
        design_id = make_design_id(original_name)
        parser = self.parser_factory(file_path)
        parsed = parser.parse()
        if len(parsed) == 2:
            instances, nets = parsed
            modules = []
        else:
            instances, nets, modules = parsed
        design = CircuitDesign(
            design_id=design_id,
            kb_name=kb_name,
            status=CircuitStatus.COMPLETE if instances or nets else CircuitStatus.EMPTY,
            files=[
                DesignFile(
                    file_name=original_name,
                    file_type=Path(original_name).suffix.lower().lstrip(".") or "circuit",
                    source_group="circuit_design",
                    path=file_path,
                )
            ],
            instances=list(instances or []),
            nets=list(nets or []),
            modules=list(modules or []),
            parse_warnings=list(getattr(parser, "warnings", []) or []),
        )
        self.store.save(design)
        self._write_metadata(
            kb_name,
            design_id,
            {
                "record_id": record_id,
                "department_id": str(department_id or ""),
                "uploaded_by": uploaded_by,
                "original_name": original_name,
                "file_path": file_path,
            },
        )
        warnings = list(design.parse_warnings)
        graph_node_count = 0
        graph_edge_count = 0
        try:
            graph_result = self.graph_store.save(
                design,
                self.store.design_dir(kb_name, design_id, create=True),
            )
            graph_node_count = graph_result.node_count
            graph_edge_count = graph_result.edge_count
        except Exception:
            warnings.append("Graph index persistence failed.")

        vector_document_count = 0
        try:
            vector_status = self.vector_index.reindex_design_with_status(design)
            vector_document_count = vector_status.indexed_count
            if vector_status.available and vector_status.error:
                warnings.append("Vector index persistence failed.")
        except Exception:
            warnings.append("Vector index persistence failed.")
        stats = {
            "instance_count": len(design.instances),
            "net_count": len(design.nets),
            "module_count": len(design.modules),
            "graph_node_count": graph_node_count,
            "graph_edge_count": graph_edge_count,
            "vector_document_count": vector_document_count,
        }
        return CircuitIndexResult(
            ok=True,
            status="degraded" if len(warnings) > len(design.parse_warnings) else "indexed",
            message=f"Indexed circuit design {original_name}",
            warnings=warnings,
            stats=stats,
            design_id=design_id,
        )

    def query(
        self,
        *,
        kb_name: str,
        query: str,
        ctx: RequestContext | None,
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[Evidence]:
        filters = filters or {}
        top_k = max(1, int(top_k or 5))
        needles = _query_terms(query)
        department_id = _ctx_department_id(ctx)
        allowed_designs: dict[str, tuple[dict[str, Any], str]] = {}
        for design in self.store.list_designs(kb_name):
            meta = self._read_metadata(kb_name, design.design_id)
            if department_id and str(meta.get("department_id") or "") != department_id:
                continue
            source_name = str(meta.get("original_name") or (design.files[0].file_name if design.files else design.design_id))
            if not _matches_filters(filters, source_name, meta):
                continue
            meta = {**meta, "kb_name": design.kb_name}
            allowed_designs[design.design_id] = (meta, source_name)

        if not allowed_designs:
            return []

        # Retrieval is deliberately staged.  Structured facts are authoritative;
        # graph expansion adds topology context without replacing them; semantic
        # recall is used only when neither has grounded an answer.
        structured_hits = self._structured_evidence(kb_name, query, allowed_designs, top_k)
        graph_hits = self._graph_evidence(kb_name, query, allowed_designs)
        hits = self._deduplicate([*structured_hits, *graph_hits])
        if not hits:
            hits.extend(self._semantic_evidence(kb_name, query, allowed_designs, top_k))
        for design in self.store.list_designs(kb_name):
            allowed = allowed_designs.get(design.design_id)
            if allowed is None:
                continue
            meta, source_name = allowed
            hits.extend(self._net_evidence(design, meta, source_name, needles))
            hits.extend(self._instance_evidence(design, meta, source_name, needles))
        return self._deduplicate(hits)[:top_k]

    @staticmethod
    def _deduplicate(hits: list[Evidence]) -> list[Evidence]:
        by_id: dict[str, Evidence] = {}
        for hit in hits:
            current = by_id.get(hit.id)
            if current is None or hit.score > current.score:
                by_id[hit.id] = hit
        return sorted(by_id.values(), key=lambda item: (-item.score, item.id))

    def _graph_evidence(
        self,
        kb_name: str,
        query: str,
        allowed_designs: dict[str, tuple[dict[str, Any], str]],
    ) -> list[Evidence]:
        load = getattr(self.graph_store, "load", None)
        if not callable(load):
            return []
        refdes_values = list(dict.fromkeys(re.findall(r"(?<![A-Za-z0-9])([A-Za-z]{1,4}\d+)(?![A-Za-z0-9])", query)))
        if not refdes_values:
            return []
        results: list[Evidence] = []
        for design_id, (metadata, source_name) in allowed_designs.items():
            try:
                graph = load(self.store.design_dir(kb_name, design_id))
            except Exception:
                continue
            if graph is None:
                continue
            for refdes in refdes_values:
                neighbors = self.graph_store.connected_entities(graph, refdes=refdes)
                net_names = [str(item.get("net_name") or item.get("name") or "") for item in neighbors if item.get("kind") == "net"]
                for net_name in net_names:
                    related = self.graph_store.connected_entities(graph, net_name=net_name)
                    endpoints = [item for item in related if item.get("kind") == "pin"]
                    components = sorted({str(item.get("refdes") or "") for item in related if item.get("kind") == "component" and item.get("refdes")})
                    for endpoint in endpoints:
                        pin = str(endpoint.get("pin") or endpoint.get("pin_name") or "")
                        endpoint_refdes = str(endpoint.get("refdes") or "")
                        if not pin or not endpoint_refdes:
                            continue
                        content = f"Graph net {net_name}: {refdes} is related through {endpoint_refdes}.{pin}; components: {', '.join(components)}."
                        record_id = metadata.get("record_id")
                        results.append(Evidence(
                            id=f"circuit:{record_id or design_id}:graph_relationship:{refdes}:{endpoint_refdes}.{pin}:{net_name}",
                            content=content,
                            source_name=source_name,
                            content_kind="circuit_design",
                            processor_kind="circuit_design",
                            score=0.88,
                            locator={"record_id": record_id, "circuit_id": design_id, "entity_type": "graph_relationship", "entity_id": refdes, "pin": pin, "net": net_name},
                            metadata={"kb_name": kb_name, "department_id": metadata.get("department_id", ""), "source_group": "circuit_design", "evidence_kind": "graph_relationship"},
                        ))
        return self._deduplicate(results)

    def _semantic_evidence(
        self,
        kb_name: str,
        query: str,
        allowed_designs: dict[str, tuple[dict[str, Any], str]],
        top_k: int,
    ) -> list[Evidence]:
        search = getattr(self.vector_index, "semantic_search", None)
        if not callable(search):
            return []
        try:
            vector_hits = search(kb_name, query, top_k=top_k * 2, kinds=(KIND_INSTANCE, KIND_NET, KIND_MODULE))
        except Exception:
            return []
        results: list[Evidence] = []
        for hit in vector_hits:
            context = allowed_designs.get(str(getattr(hit, "design_id", "")))
            if context is None:
                continue
            metadata, source_name = context
            kind = str(getattr(hit, "kind", "") or "instance")
            entity_id = str(getattr(hit, "natural_id", "") or "semantic")
            record_id = metadata.get("record_id")
            # Semantic scores must never outrank direct EDF facts.
            score = min(0.69, max(0.0, float(getattr(hit, "score", 0.0))))
            results.append(Evidence(
                id=f"circuit:{record_id or hit.design_id}:semantic_{kind}:{entity_id}",
                content=str(getattr(hit, "document", "") or f"Semantic circuit match: {entity_id}."),
                source_name=source_name,
                content_kind="circuit_design",
                processor_kind="circuit_design",
                score=score,
                locator={"record_id": record_id, "circuit_id": hit.design_id, "entity_type": f"semantic_{kind}", "entity_id": entity_id},
                metadata={"kb_name": kb_name, "department_id": metadata.get("department_id", ""), "source_group": "circuit_design", "evidence_kind": "semantic"},
            ))
        return self._deduplicate(results)

    def list_pin_mapping_evidence(
        self,
        kb_name: str,
        source_names: list[str],
        ctx: RequestContext | None,
        *,
        refdes: list[str] | None = None,
    ) -> list[Evidence]:
        """Enumerate selected pin mappings from frozen, authorized EDF sources."""
        frozen_source_names = {
            str(source_name).strip()
            for source_name in source_names
            if str(source_name).strip()
        }
        if not frozen_source_names:
            return []
        requested_refdes = {
            str(value).strip().casefold()
            for value in (refdes or [])
            if str(value).strip()
        }
        department_id = _ctx_department_id(ctx)
        evidences: list[Evidence] = []
        for design in self.store.list_designs(kb_name):
            metadata = self._read_metadata(kb_name, design.design_id)
            if department_id and str(metadata.get("department_id") or "") != department_id:
                continue
            source_name = str(
                metadata.get("original_name")
                or (design.files[0].file_name if design.files else design.design_id)
            )
            if source_name not in frozen_source_names:
                continue
            evidence_metadata = {**metadata, "kb_name": design.kb_name}
            for instance in design.instances:
                if not instance.refdes or not instance.pins:
                    continue
                if requested_refdes and instance.refdes.casefold() not in requested_refdes:
                    continue
                evidences.append(self.evidence_mapper.build(
                    kind="pin_mapping",
                    row={
                        "design_id": design.design_id,
                        "refdes": instance.refdes,
                        "pins": [
                            {"name": pin.name, "net_name": pin.net}
                            for pin in instance.pins
                        ],
                    },
                    metadata=evidence_metadata,
                    source_name=source_name,
                    score=1.0,
                ))
        return sorted(evidences, key=lambda evidence: evidence.id)

    def _structured_evidence(
        self,
        kb_name: str,
        query: str,
        allowed_designs: dict[str, tuple[dict[str, Any], str]],
        top_k: int,
    ) -> list[Evidence]:
        plan = analyze_question(query)
        candidates = [
            ("net", 0.96, self.query_engine.search_net_connections(kb_name, query, limit=top_k * 3)),
            ("instance", 0.92, self.query_engine.search_instances(kb_name, query, limit=top_k * 3)),
            ("module", 0.80, self.query_engine.search_modules(kb_name, query, limit=top_k * 2)),
            ("module_connection", 0.84, self.query_engine.search_module_connections(kb_name, query, limit=top_k * 2)),
            ("module_power", 0.82, self.query_engine.search_module_power_nets(kb_name, query, limit=top_k * 2)),
        ]
        if "power_switch" in plan.operations:
            switch_rows = self.query_engine.search_instances(kb_name, query, limit=top_k * 3)
            pin_mapping_rows: list[dict[str, Any]] = []
            for row in switch_rows:
                design_id = str(row.get("design_id") or row.get("circuit_id") or "")
                refdes = str(row.get("refdes") or "")
                if design_id not in allowed_designs or not refdes:
                    continue
                detail = self.query_engine.get_instance_detail(kb_name, design_id, refdes)
                if detail and detail.get("pins"):
                    pin_mapping_rows.append(detail)
            candidates = [("pin_mapping", 0.98, pin_mapping_rows), ("instance", 0.92, switch_rows)]
        if "power_path" in plan.operations:
            power_topology_rows = [
                topology
                for design_id in allowed_designs
                if (topology := self.query_engine.build_power_topology(kb_name, design_id))
            ]
            candidates.append(("power_topology", 0.99, power_topology_rows))
        if "mcu" in query.casefold():
            candidates.append(
                ("instance", 0.97, self.query_engine.search_instances(kb_name, "TC3", limit=top_k * 2))
            )
        refdes_matches = re.findall(r"(?<![A-Za-z0-9])([A-Za-z]{1,4}\d+)(?![A-Za-z0-9])", query)
        if "connection" in plan.operations and refdes_matches:
            pin_mapping_rows: list[dict[str, Any]] = []
            refdes_values = list(dict.fromkeys(refdes_matches))[:3]
            for design_id in allowed_designs:
                for refdes in refdes_values:
                    detail = self.query_engine.get_instance_detail(kb_name, design_id, refdes)
                    if detail and detail.get("pins"):
                        pin_mapping_rows.append(detail)
            if pin_mapping_rows:
                candidates.append(("pin_mapping", 0.98, pin_mapping_rows))
        if "bias" in plan.operations:
            bias_rows = self.query_engine.search_bias_topologies(kb_name, limit=top_k * 3)
            lowered = query.casefold()
            if "上拉" in lowered or "pull-up" in lowered or "pullup" in lowered:
                bias_rows = [row for row in bias_rows if row.get("topology") == "pull_up"]
            elif "下拉" in lowered or "pull-down" in lowered or "pulldown" in lowered:
                bias_rows = [row for row in bias_rows if row.get("topology") == "pull_down"]
            candidates.append(("topology", 0.94, bias_rows))
        if "protection" in plan.operations:
            candidates.append(("topology", 0.90, self.query_engine.search_protection_topologies(kb_name, limit=top_k * 3)))
        if "power_path" in plan.operations and "protection" in plan.operations:
            candidates.append(("topology", 0.91, self.query_engine.search_power_protection_candidates(kb_name, limit=top_k * 3)))
        evidence_by_id: dict[str, Evidence] = {}
        for kind, score, rows in candidates:
            for row in rows:
                design_id = str(row.get("design_id") or row.get("circuit_id") or "")
                context = allowed_designs.get(design_id)
                if context is None:
                    continue
                metadata, source_name = context
                evidence = self.evidence_mapper.build(
                    kind=kind,
                    row=row,
                    metadata=metadata,
                    source_name=source_name,
                    score=score,
                )
                evidence_by_id.setdefault(evidence.id, evidence)
        return sorted(evidence_by_id.values(), key=lambda item: (-item.score, item.id))

    def delete_record(self, record: Any) -> None:
        kb_name = getattr(record, "kb_name", "")
        if not kb_name:
            return
        record_id = getattr(record, "id", None)
        for design in list(self.store.list_designs(kb_name)):
            meta = self._read_metadata(kb_name, design.design_id)
            if record_id is not None and meta.get("record_id") == record_id:
                self.store.delete_design(kb_name, design.design_id)
                continue
            names = {getattr(record, "document_name", ""), getattr(record, "original_file_name", "")}
            if any(name and make_design_id(name) == design.design_id for name in names):
                self.store.delete_design(kb_name, design.design_id)

    def _net_evidence(
        self,
        design: CircuitDesign,
        meta: dict[str, Any],
        source_name: str,
        needles: list[str],
    ) -> list[Evidence]:
        results: list[Evidence] = []
        for net in design.nets:
            connection_text = ", ".join(
                f"{conn.refdes}.{conn.pin}" if conn.pin else conn.refdes for conn in net.connections
            )
            content = f"Net {net.name} connects {connection_text}." if connection_text else f"Net {net.name} is present."
            haystack = f"{net.name} {connection_text}"
            if not _matches_terms(haystack, needles):
                continue
            results.append(
                self._evidence(
                    design=design,
                    meta=meta,
                    source_name=source_name,
                    entity_type="net",
                    entity_id=net.name,
                    content=content,
                    score=0.9 if needles else 0.65,
                )
            )
        return results

    def _instance_evidence(
        self,
        design: CircuitDesign,
        meta: dict[str, Any],
        source_name: str,
        needles: list[str],
    ) -> list[Evidence]:
        results: list[Evidence] = []
        for inst in design.instances:
            pin_text = ", ".join(
                f"{pin.name}->{pin.net}" if pin.net else pin.name for pin in inst.pins
            )
            descriptors = [inst.library_cell, inst.part_number, inst.value, inst.footprint, pin_text]
            content = f"Instance {inst.refdes}"
            detail = ", ".join(str(item) for item in descriptors if item)
            if detail:
                content = f"{content}: {detail}."
            haystack = f"{inst.refdes} {detail}"
            if not _matches_terms(haystack, needles):
                continue
            results.append(
                self._evidence(
                    design=design,
                    meta=meta,
                    source_name=source_name,
                    entity_type="instance",
                    entity_id=inst.refdes,
                    content=content,
                    score=0.78 if needles else 0.55,
                )
            )
        return results

    def _evidence(
        self,
        *,
        design: CircuitDesign,
        meta: dict[str, Any],
        source_name: str,
        entity_type: str,
        entity_id: str,
        content: str,
        score: float,
    ) -> Evidence:
        record_id = meta.get("record_id")
        return Evidence(
            id=f"circuit:{record_id or design.design_id}:{entity_type}:{entity_id}",
            content=content,
            source_name=source_name,
            content_kind="circuit_design",
            processor_kind="circuit_design",
            score=score,
            locator={
                "record_id": record_id,
                "circuit_id": design.design_id,
                "entity_type": entity_type,
                "entity_id": entity_id,
            },
            metadata={
                "kb_name": design.kb_name,
                "department_id": meta.get("department_id", ""),
                "source_group": "circuit_design",
            },
        )

    def _metadata_path(self, kb_name: str, design_id: str) -> str:
        return os.path.join(self.store.design_dir(kb_name, design_id, create=True), META_FILE)

    def _write_metadata(self, kb_name: str, design_id: str, metadata: dict[str, Any]) -> None:
        with open(self._metadata_path(kb_name, design_id), "w", encoding="utf-8") as fh:
            json.dump(metadata, fh, ensure_ascii=False, indent=2)

    def _read_metadata(self, kb_name: str, design_id: str) -> dict[str, Any]:
        try:
            with open(self._metadata_path(kb_name, design_id), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except OSError:
            return {}


def _query_terms(query: str) -> list[str]:
    terms = []
    for token in re.findall(r"[A-Za-z0-9_+.-]+|[\u4e00-\u9fff]+", query or ""):
        token = token.strip()
        if len(token) >= 2:
            terms.append(token)
    return terms


def _matches_terms(haystack: str, terms: list[str]) -> bool:
    if not terms:
        return True
    upper = haystack.upper()
    return any(term.upper() in upper for term in terms)


def _matches_filters(filters: dict, source_name: str, metadata: dict[str, Any]) -> bool:
    source_filter = filters.get("source_name") or filters.get("document_name")
    if source_filter and str(source_filter) != source_name:
        return False
    record_filter = filters.get("record_id")
    if record_filter not in (None, "") and str(record_filter) != str(metadata.get("record_id")):
        return False
    return True


def _ctx_department_id(ctx: RequestContext | None) -> str:
    if ctx is None:
        return ""
    metadata = getattr(ctx, "metadata", {}) or {}
    return str(metadata.get("resource_department_id") or metadata.get("department_id") or "")
