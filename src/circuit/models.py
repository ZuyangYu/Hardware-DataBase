from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class CircuitStatus(StrEnum):
    EMPTY = "empty"
    PARTIAL_EDF = "partial_edf"
    PARTIAL_PDF = "partial_pdf"
    COMPLETE = "complete"


@dataclass
class FieldProvenance:
    source_file: str
    parser: str
    confidence: float = 1.0


@dataclass
class DesignFile:
    file_name: str
    file_type: str
    source_group: str
    path: str


@dataclass
class PinRef:
    refdes: str
    pin: str | None = None


@dataclass
class Pin:
    name: str
    net: str | None = None


@dataclass
class SchematicLabel:
    text: str
    page_number: int
    kind: str = "text"
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class SchematicPage:
    page_number: int
    width: float | None = None
    height: float | None = None
    text: str = ""
    labels: list[SchematicLabel] = field(default_factory=list)


@dataclass
class ComponentInstance:
    refdes: str
    library_cell: str | None = None
    part_number: str | None = None
    footprint: str | None = None
    value: str | None = None
    erp_number: str | None = None
    pins: list[Pin] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, FieldProvenance] = field(default_factory=dict)


@dataclass
class Net:
    name: str
    connections: list[PinRef] = field(default_factory=list)
    net_type: str = "signal"


@dataclass
class CircuitModule:
    module_id: str
    name: str
    strategy: str
    instances: list[str] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)
    connectivity_description: str | None = None
    visual_description: str | None = None
    merged_description: str | None = None


@dataclass
class ModuleRegion:
    module_id: str
    page_number: int
    bbox: tuple[float, float, float, float] | None = None
    confidence: float = 0.0
    strategy: str = "text_cluster"


@dataclass
class CrossReference:
    edf_refdes: str
    pdf_label: str
    page_number: int | None = None
    confidence: float = 0.0
    strategy: str = "exact"


@dataclass
class CircuitDesign:
    design_id: str
    kb_name: str
    status: CircuitStatus = CircuitStatus.EMPTY
    files: list[DesignFile] = field(default_factory=list)
    instances: list[ComponentInstance] = field(default_factory=list)
    nets: list[Net] = field(default_factory=list)
    modules: list[CircuitModule] = field(default_factory=list)
    schematic_pages: list[SchematicPage] = field(default_factory=list)
    module_regions: list[ModuleRegion] = field(default_factory=list)
    cross_references: list[CrossReference] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = str(self.status)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CircuitDesign":
        files = [DesignFile(**item) for item in data.get("files", [])]
        instances = []
        for item in data.get("instances", []):
            item = dict(item)
            item["pins"] = [Pin(**pin) for pin in item.get("pins", [])]
            item["provenance"] = {
                key: FieldProvenance(**value)
                for key, value in item.get("provenance", {}).items()
                if isinstance(value, dict)
            }
            instances.append(ComponentInstance(**item))
        nets = []
        for item in data.get("nets", []):
            item = dict(item)
            item["connections"] = [PinRef(**conn) for conn in item.get("connections", [])]
            nets.append(Net(**item))
        modules = [CircuitModule(**item) for item in data.get("modules", [])]
        schematic_pages = []
        for item in data.get("schematic_pages", []):
            item = dict(item)
            labels = []
            for label in item.get("labels", []):
                label = dict(label)
                if label.get("bbox") is not None:
                    label["bbox"] = tuple(label["bbox"])
                labels.append(SchematicLabel(**label))
            item["labels"] = labels
            schematic_pages.append(SchematicPage(**item))
        module_regions = []
        for item in data.get("module_regions", []):
            item = dict(item)
            if item.get("bbox") is not None:
                item["bbox"] = tuple(item["bbox"])
            module_regions.append(ModuleRegion(**item))
        cross_references = [CrossReference(**item) for item in data.get("cross_references", [])]
        return cls(
            design_id=data["design_id"],
            kb_name=data["kb_name"],
            status=CircuitStatus(data.get("status", CircuitStatus.EMPTY)),
            files=files,
            instances=instances,
            nets=nets,
            modules=modules,
            schematic_pages=schematic_pages,
            module_regions=module_regions,
            cross_references=cross_references,
            parse_warnings=list(data.get("parse_warnings", [])),
        )
