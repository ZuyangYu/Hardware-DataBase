from __future__ import annotations

from pathlib import Path

from src.circuit.analyzers.image_cropper import ImageCropper
from src.circuit.analyzers.module_analyzer import enrich_module_descriptions
from src.circuit.graph_store import GraphStore
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
    ):
        self.store = store or CircuitStore()
        self.graph_store = graph_store or GraphStore()
        self.image_cropper = image_cropper or ImageCropper(self.store)

    def apply_edf_parse(self, kb_name: str, filename: str, file_path: str, instances, nets, modules, warnings) -> CircuitDesign:
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
        self.store.save(design)
        self._persist_graph(design)
        self._crop_module_screenshots(design)
        return design

    def apply_pdf_parse(self, kb_name: str, filename: str, file_path: str, pages, warnings) -> CircuitDesign:
        design = self._load_target_design(kb_name, filename, preferred_status=CircuitStatus.PARTIAL_EDF)
        design.design_id = design.design_id or make_design_id(filename)
        design.kb_name = kb_name
        design.schematic_pages = pages
        design.files = [file for file in design.files if file.file_type != "pdf_schematic"]
        design.files.append(DesignFile(filename, "pdf_schematic", SCHEMATIC_GROUP, str(Path(file_path))))
        design.parse_warnings = sorted(set(design.parse_warnings + list(warnings)))
        self._fuse_if_possible(design)
        self.store.save(design)
        self._persist_graph(design)
        self._crop_module_screenshots(design)
        return design

    def _persist_graph(self, design: CircuitDesign):
        if not design.instances or not design.nets:
            return
        try:
            design_dir = self.store.design_dir(design.kb_name, design.design_id, create=True)
            self.graph_store.save(design, design_dir)
        except Exception as exc:  # graph is a best-effort artifact
            design.parse_warnings = sorted(set(design.parse_warnings + [f"graph_store: {exc}"]))

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
