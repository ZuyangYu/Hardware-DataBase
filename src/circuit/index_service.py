from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.agents.state import Evidence
from src.circuit.models import CircuitDesign, CircuitStatus, DesignFile
from src.circuit.parsers.edf_parser import EdfParser
from src.circuit.store import CircuitStore, make_design_id
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
    ):
        self.store = store or CircuitStore(root=storage_root)
        self.parser_factory = parser_factory or EdfParser

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
        stats = {
            "instance_count": len(design.instances),
            "net_count": len(design.nets),
            "module_count": len(design.modules),
        }
        return CircuitIndexResult(
            ok=True,
            status="indexed",
            message=f"Indexed circuit design {original_name}",
            warnings=design.parse_warnings,
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
        hits: list[Evidence] = []
        for design in self.store.list_designs(kb_name):
            meta = self._read_metadata(kb_name, design.design_id)
            if department_id and meta.get("department_id") and meta.get("department_id") != department_id:
                continue
            source_name = str(meta.get("original_name") or (design.files[0].file_name if design.files else design.design_id))
            if not _matches_filters(filters, source_name, meta):
                continue
            hits.extend(self._net_evidence(design, meta, source_name, needles))
            hits.extend(self._instance_evidence(design, meta, source_name, needles))
        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[:top_k]

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
