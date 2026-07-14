from __future__ import annotations

from src.circuit.query_context import CircuitSourceStatus, QueryEvidence, SourceAvailability
from src.circuit.query_engine import CircuitQueryEngine


class QueryEvidenceBuilder:
    def __init__(self, engine: CircuitQueryEngine):
        self.engine = engine

    def source_status(
        self,
        kb_name: str,
        circuit_ids: list[str],
        used_sources: list[str] | None = None,
        scope_type: str = "single_circuit",
    ) -> CircuitSourceStatus:
        used = used_sources or []
        sources: list[SourceAvailability] = []
        warnings: list[str] = []
        for circuit_id in circuit_ids:
            design = self.engine.store.load(kb_name, circuit_id)
            if design is None:
                sources.append(
                    SourceAvailability(
                        "circuit_state",
                        "missing",
                        circuit_id=circuit_id,
                        reason="circuit_state not found",
                    )
                )
                continue
            source_files = [file.file_name for file in design.files]
            warnings.extend(str(item) for item in design.parse_warnings)
            counts = {
                "instances": len(design.instances),
                "nets": len(design.nets),
                "modules": len(design.modules),
                "schematic_pages": len(design.schematic_pages),
                "module_regions": len(design.module_regions),
                "cross_references": len(design.cross_references),
                "module_screenshots": len(self.engine.store.list_module_screenshots(kb_name, circuit_id)),
                "pdf_cache_files": len(self.engine.store.list_pdf_cache(kb_name, circuit_id)),
            }
            sources.append(
                SourceAvailability(
                    "circuit_state",
                    "available",
                    circuit_id=circuit_id,
                    source_files=source_files,
                    counts=counts,
                    warnings=list(design.parse_warnings),
                )
            )
            sources.append(
                SourceAvailability(
                    "edf_netlist",
                    "available" if design.instances or design.nets else "missing",
                    circuit_id=circuit_id,
                    source_files=[name for name in source_files if name.lower().endswith((".edf", ".edif"))],
                    counts={"instances": len(design.instances), "nets": len(design.nets), "modules": len(design.modules)},
                    warnings=list(design.parse_warnings),
                )
            )
            pdf_files = [name for name in source_files if name.lower().endswith(".pdf")]
            pdf_status = "available" if design.schematic_pages or pdf_files else "missing"
            if "pdf_schematic" not in used:
                pdf_status = "not_used" if pdf_status == "available" else "missing"
            sources.append(
                SourceAvailability(
                    "pdf_schematic",
                    pdf_status,
                    circuit_id=circuit_id,
                    source_files=pdf_files,
                    counts={"schematic_pages": len(design.schematic_pages), "module_regions": len(design.module_regions)},
                )
            )
            sources.append(
                SourceAvailability(
                    "module_screenshots",
                    "not_used",
                    circuit_id=circuit_id,
                    counts={"module_screenshots": counts["module_screenshots"]},
                )
            )
            sources.append(
                SourceAvailability(
                    "connectivity_graph",
                    "not_used",
                    circuit_id=circuit_id,
                )
            )
        return CircuitSourceStatus(
            kb_name=kb_name,
            circuit_ids=list(circuit_ids),
            scope_type=scope_type,
            used_sources=used,
            sources=sources,
            warnings=warnings,
        )

    def evidence(
        self,
        result: dict,
        entity_type: str,
        entity_id: str | None,
        field_path: str | None = None,
        source_type: str = "edf_netlist",
    ) -> list[dict]:
        files = result.get("source_files") or []
        source_file = files[0] if files else None
        return [
            QueryEvidence(
                source_type=source_type,
                circuit_id=result.get("circuit_id") or result.get("design_id"),
                source_file=source_file,
                entity_type=entity_type,
                entity_id=entity_id,
                field_path=field_path,
                confidence=float(result.get("confidence", 1.0) or 1.0),
                metadata={"source_files": files, "kb_name": result.get("kb_name")},
            ).to_dict()
        ]
