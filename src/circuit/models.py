from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from typing import Any


class CircuitStatus(StrEnum):
    EMPTY = "empty"
    PARTIAL_EDF = "partial_edf"
    PARTIAL_PDF = "partial_pdf"
    COMPLETE = "complete"


class Availability(StrEnum):
    """Explicit tri-state so ``0 pages`` is never misread as "no pages exist"."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


IDENTIFIER_NAMESPACES = (
    "refdes",
    "internal_part_number",
    "manufacturer_part_number",
    "erp_number",
    "library_cell",
    "value",
    "curated_alias",
)

IDENTIFIER_SOURCE_KINDS = ("edf_property", "bom_field", "curated_catalog")

ROLE_SOURCE_KINDS = (
    "edf_property",
    "bom_field",
    "datasheet_cross_reference",
    "curated_catalog",
)

ASSERTION_MODES = ("explicit", "catalog_match")

ALIAS_ORIGIN_KINDS = ("identifier", "curated_catalog")

# Logical partition strategies (``source_partition_strategy`` keeps the raw
# parser value; the logical one must never claim a visual page exists).
PARTITION_STRATEGIES = ("refdes_page_heuristic", "source_page", "none")


def _require_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value


def _require_choice(name: str, value: str, allowed: tuple[str, ...]) -> str:
    _require_text(name, value)
    if value not in allowed:
        raise ValueError(f"{name} 取值不受支持: {value!r}")
    return value


def _require_json_mapping(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是对象")
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须可 JSON 序列化") from exc
    return value


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


def _coerce_availability(value: Any) -> Availability:
    try:
        return Availability(value)
    except ValueError as exc:
        raise ValueError(f"availability 取值不受支持: {value!r}") from exc


@dataclass
class ComponentIdentifier:
    """Namespaced, source-traceable identifier for one component."""

    namespace: str
    raw_value: str
    normalized_value: str
    source_kind: str
    source_locator: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        _require_choice("namespace", self.namespace, IDENTIFIER_NAMESPACES)
        _require_text("raw_value", self.raw_value)
        _require_text("normalized_value", self.normalized_value)
        _require_choice("source_kind", self.source_kind, IDENTIFIER_SOURCE_KINDS)
        _require_json_mapping("source_locator", self.source_locator)


@dataclass
class ComponentAlias:
    """Alias whose provenance resolves to an identifier or a curated entry."""

    value: str
    origin_kind: str
    origin_key: str

    def __post_init__(self):
        _require_text("value", self.value)
        _require_choice("origin_kind", self.origin_kind, ALIAS_ORIGIN_KINDS)
        _require_text("origin_key", self.origin_key)


@dataclass
class ComponentRoleAssertion:
    """Evidence-backed role claim; LLM guesses may never be written here."""

    role_id: str
    display_name: str
    source_kind: str
    source_file: str = ""
    source_locator: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    assertion_mode: str = "explicit"

    def __post_init__(self):
        _require_text("role_id", self.role_id)
        _require_text("display_name", self.display_name)
        _require_choice("source_kind", self.source_kind, ROLE_SOURCE_KINDS)
        if not isinstance(self.source_file, str):
            raise ValueError("source_file 必须是字符串")
        _require_json_mapping("source_locator", self.source_locator)
        self.confidence = float(self.confidence)
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence 必须在 [0, 1] 区间内")
        _require_choice("assertion_mode", self.assertion_mode, ASSERTION_MODES)


@dataclass
class ComponentIdentity:
    refdes: str
    identifiers: list[ComponentIdentifier] = field(default_factory=list)
    aliases: list[ComponentAlias] = field(default_factory=list)
    roles: list[ComponentRoleAssertion] = field(default_factory=list)

    def __post_init__(self):
        _require_text("refdes", self.refdes)
        identifier_keys = {item.name for item in fields(ComponentIdentifier)}
        alias_keys = {item.name for item in fields(ComponentAlias)}
        role_keys = {item.name for item in fields(ComponentRoleAssertion)}
        identifiers = []
        for item in self.identifiers:
            if isinstance(item, ComponentIdentifier):
                identifiers.append(item)
            elif isinstance(item, dict):
                identifiers.append(
                    ComponentIdentifier(**{key: value for key, value in item.items() if key in identifier_keys})
                )
            else:
                raise ValueError("identifier 必须是对象")
        self.identifiers = identifiers
        aliases = []
        for item in self.aliases:
            if isinstance(item, ComponentAlias):
                aliases.append(item)
            elif isinstance(item, dict):
                aliases.append(ComponentAlias(**{key: value for key, value in item.items() if key in alias_keys}))
            else:
                raise ValueError("alias 必须是对象")
        self.aliases = aliases
        roles = []
        for item in self.roles:
            if isinstance(item, ComponentRoleAssertion):
                roles.append(item)
            elif isinstance(item, dict):
                roles.append(ComponentRoleAssertion(**{key: value for key, value in item.items() if key in role_keys}))
            else:
                raise ValueError("role 必须是对象")
        self.roles = roles

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComponentIdentity":
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class CircuitStructureCoverage:
    """What the parsed sources can honestly answer about design structure.

    ``schematic_pages``/``title_block``/``coordinates``/``visual_layout`` use
    :class:`Availability` so legacy states (never computed) stay ``unknown``
    instead of being reported as "definitively absent".
    """

    netlist_connectivity: Availability = Availability.UNKNOWN
    module_partition_strategy: str = "none"
    source_partition_strategy: str = "none"
    schematic_pages: Availability = Availability.UNKNOWN
    schematic_page_count: int = 0
    title_block: Availability = Availability.UNKNOWN
    coordinates: Availability = Availability.UNKNOWN
    visual_layout: Availability = Availability.UNKNOWN
    notes: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.netlist_connectivity = _coerce_availability(self.netlist_connectivity)
        self.schematic_pages = _coerce_availability(self.schematic_pages)
        self.title_block = _coerce_availability(self.title_block)
        self.coordinates = _coerce_availability(self.coordinates)
        self.visual_layout = _coerce_availability(self.visual_layout)
        _require_choice(
            "module_partition_strategy",
            self.module_partition_strategy,
            PARTITION_STRATEGIES,
        )
        # Raw parser strategies are preserved verbatim; new parsers may add
        # values without touching this model.
        _require_text("source_partition_strategy", self.source_partition_strategy)
        self.schematic_page_count = int(self.schematic_page_count or 0)
        if not isinstance(self.notes, list) or not all(isinstance(item, str) for item in self.notes):
            raise ValueError("notes 必须是字符串列表")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CircuitStructureCoverage":
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


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
    # Derived, fully recomputable projections (never replace the raw EDF data).
    component_identities: list[ComponentIdentity] = field(default_factory=list)
    structure_coverage: CircuitStructureCoverage = field(default_factory=CircuitStructureCoverage)

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
        component_identities = [
            ComponentIdentity.from_dict(item)
            for item in data.get("component_identities", [])
            if isinstance(item, dict)
        ]
        coverage_data = data.get("structure_coverage")
        structure_coverage = (
            CircuitStructureCoverage.from_dict(coverage_data)
            if isinstance(coverage_data, dict)
            else CircuitStructureCoverage()
        )
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
            component_identities=component_identities,
            structure_coverage=structure_coverage,
        )
