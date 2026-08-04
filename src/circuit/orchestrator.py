from __future__ import annotations

import os
import shutil
from pathlib import Path

from src.circuit.analyzers.image_cropper import ImageCropper
from src.circuit.analyzers.module_analyzer import enrich_module_descriptions
from src.circuit.graph_store import GraphStore
from src.circuit.image_cache import ImageCache
from src.circuit.index_lock import circuit_index_write_lock
from src.circuit.index_service import CircuitIndexService
from src.circuit.models import CircuitDesign, CircuitStatus, DesignFile
from src.circuit.parsers.cross_reference import CrossReferenceEngine
from src.circuit.parsers.region_mapper import estimate_regions_from_modules, estimate_regions_from_pages
from src.circuit.store import CircuitStore, make_design_id
from src.ingestion.source_groups import NETLIST_GROUP, SCHEMATIC_GROUP


class CircuitOrchestrator:
    """Incrementally merges EDF and schematic PDF parse results per knowledge base."""

    def __init__(
        self,
        store: CircuitStore | None = None,
        graph_store: GraphStore | None = None,
        image_cropper: ImageCropper | None = None,
        image_cache: ImageCache | None = None,
        index_service: CircuitIndexService | None = None,
    ):
        self.store = store or CircuitStore()
        self.graph_store = graph_store or GraphStore()
        self.image_cropper = image_cropper or ImageCropper(self.store)
        self.image_cache = image_cache or ImageCache(self.store)
        self.index_service = index_service or CircuitIndexService(
            store=self.store,
            graph_store=self.graph_store,
        )
        if os.path.realpath(self.index_service.store.root) != os.path.realpath(self.store.root):
            raise ValueError("Circuit orchestrator and index service must use the same storage root.")

    def apply_edf_parse(
        self,
        kb_name: str,
        filename: str,
        file_path: str,
        instances,
        nets,
        modules,
        warnings,
        *,
        record_id: int | None = None,
        department_id: str | None = None,
        uploaded_by: str = "",
    ) -> CircuitDesign:
        with circuit_index_write_lock(self.store.root):
            design = self._load_target_design(kb_name, filename, preferred_status=CircuitStatus.PARTIAL_PDF)
            design.design_id = design.design_id or make_design_id(filename)
            design.kb_name = kb_name
            design.instances = instances
            design.nets = nets
            design.modules = enrich_module_descriptions(modules, instances, nets)
            design.files = [file for file in design.files if file.file_type != "edf"]
            design.files.append(DesignFile(filename, "edf", NETLIST_GROUP, str(Path(file_path))))
            design.parse_warnings = sorted(set(design.parse_warnings + list(warnings)))
            self._fuse_if_possible(design)
            metadata = self._publication_metadata(
                design,
                filename,
                file_path,
                record_id=record_id,
                department_id=department_id,
                uploaded_by=uploaded_by,
            )
            self.index_service._publish_design_unlocked(
                design,
                metadata,
            )
            self._replace_module_screenshots(design)
            return design

    def apply_pdf_parse(
        self,
        kb_name: str,
        filename: str,
        file_path: str,
        pages,
        warnings,
        *,
        record_id: int | None = None,
        department_id: str | None = None,
        uploaded_by: str = "",
    ) -> CircuitDesign:
        with circuit_index_write_lock(self.store.root):
            design = self._load_target_design(kb_name, filename, preferred_status=CircuitStatus.PARTIAL_EDF)
            design.design_id = design.design_id or make_design_id(filename)
            design.kb_name = kb_name
            design.schematic_pages = pages
            design.files = [file for file in design.files if file.file_type != "pdf_schematic"]
            design.files.append(DesignFile(filename, "pdf_schematic", SCHEMATIC_GROUP, str(Path(file_path))))
            design.parse_warnings = sorted(set(design.parse_warnings + list(warnings)))
            self._fuse_if_possible(design)
            metadata = self._publication_metadata(
                design,
                filename,
                file_path,
                record_id=record_id,
                department_id=department_id,
                uploaded_by=uploaded_by,
            )
            self.index_service._publish_design_unlocked(
                design,
                metadata,
            )
            try:
                self.image_cache.replace_pdf(kb_name, design.design_id, file_path)
            except Exception as exc:
                self._remove_visual_artifacts(design)
                design.parse_warnings = sorted(
                    set(design.parse_warnings + [f"pdf_cache: {exc}"])
                )
            else:
                self._replace_module_screenshots(design)
            return design

    def _publication_metadata(
        self,
        design: CircuitDesign,
        filename: str,
        file_path: str,
        *,
        record_id: int | None,
        department_id: str | None,
        uploaded_by: str,
    ) -> dict:
        existing = self.index_service._read_metadata(design.kb_name, design.design_id)
        existing_department = str(existing.get("department_id") or "")
        requested_department = str(department_id or "")
        if existing_department and requested_department and existing_department != requested_department:
            raise PermissionError("Legacy circuit publication cannot change an existing department.")
        resolved_department = existing_department or requested_department
        resolved_record_id = existing.get("record_id")
        if resolved_record_id is None:
            resolved_record_id = record_id
        if not resolved_department or resolved_record_id is None:
            raise PermissionError(
                "First-time legacy circuit publication requires record_id and department_id."
            )
        return {
            **existing,
            "record_id": resolved_record_id,
            "department_id": resolved_department,
            "uploaded_by": str(existing.get("uploaded_by") or uploaded_by or ""),
            "original_name": str(existing.get("original_name") or filename),
            "file_path": file_path,
        }

    def _crop_module_screenshots(self, design: CircuitDesign):
        """Best-effort: drop ``module_screenshots/*.png`` next to the design.

        When PyMuPDF / the PDF cache is unavailable the cropper writes a JSON
        sidecar describing the deferred region instead — no warnings are
        surfaced for that path because it's the expected steady state when only
        an EDF has been uploaded.
        """
        if not design.module_regions:
            return
        try:
            self.image_cropper.crop_modules(design)
        except Exception as exc:  # pragma: no cover - cropping is best-effort
            design.parse_warnings = sorted(set(design.parse_warnings + [f"image_cropper: {exc}"]))

    def _replace_module_screenshots(self, design: CircuitDesign) -> None:
        screenshot_dir = self.store.module_screenshot_dir(
            design.kb_name,
            design.design_id,
        )
        if os.path.isdir(screenshot_dir):
            shutil.rmtree(screenshot_dir)
        self._crop_module_screenshots(design)

    def _remove_visual_artifacts(self, design: CircuitDesign) -> None:
        for directory in (
            self.store.pdf_cache_dir(design.kb_name, design.design_id),
            self.store.module_screenshot_dir(design.kb_name, design.design_id),
        ):
            if os.path.isdir(directory):
                shutil.rmtree(directory)

    def _load_target_design(
        self,
        kb_name: str,
        filename: str,
        preferred_status: CircuitStatus,
    ) -> CircuitDesign:
        design_id = make_design_id(filename)
        exact = self.store.load(kb_name, design_id)
        if exact:
            return exact
        candidates = [design for design in self.store.list_designs(kb_name) if design.status == preferred_status]
        if len(candidates) == 1:
            return candidates[0]
        return CircuitDesign(design_id=design_id, kb_name=kb_name)

    def _fuse_if_possible(self, design: CircuitDesign):
        has_edf = bool(design.instances and design.nets)
        has_pdf = bool(design.schematic_pages)
        if has_edf and has_pdf:
            design.cross_references = CrossReferenceEngine().match(design.instances, design.schematic_pages)
            design.module_regions = estimate_regions_from_modules(design.modules, design.schematic_pages)
            design.status = CircuitStatus.COMPLETE
        elif has_edf:
            design.status = CircuitStatus.PARTIAL_EDF
        elif has_pdf:
            design.module_regions = estimate_regions_from_pages(design.schematic_pages)
            design.status = CircuitStatus.PARTIAL_PDF
        else:
            design.status = CircuitStatus.EMPTY
