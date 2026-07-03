import os
from dataclasses import dataclass, field
from typing import Iterable


DATASET_TABLE = "table"
CONTENT_KIND_DOCUMENT = "document_text"
CONTENT_KIND_SPREADSHEET = "spreadsheet_table"
PROCESSOR_KIND_RAGFLOW = "ragflow"
PROCESSOR_KIND_SPREADSHEET = "spreadsheet_table"

PIPELINE_STAGE_RETRIEVAL = "retrieval"
PIPELINE_STAGE_STRUCTURED = "structured"


@dataclass(frozen=True)
class PipelineSpec:
    key: str
    label: str
    processor_kind: str
    content_kind: str
    supported_extensions: frozenset[str] = field(default_factory=frozenset)
    rejected_extensions: frozenset[str] = field(default_factory=frozenset)
    stage: str = PIPELINE_STAGE_RETRIEVAL
    dataset_kind: str | None = None
    description: str = ""

    def supports_extension(self, extension: str) -> bool:
        return normalize_extension(extension) in self.supported_extensions

    def rejects_extension(self, extension: str) -> bool:
        return normalize_extension(extension) in self.rejected_extensions


@dataclass(frozen=True)
class PipelineRoute:
    extension: str
    spec: PipelineSpec | None = None
    rejected_by: PipelineSpec | None = None
    reason: str = ""

    @property
    def supported(self) -> bool:
        return self.spec is not None

    @property
    def rejected(self) -> bool:
        return self.rejected_by is not None


class PipelineRegistry:
    def __init__(self, specs: Iterable[PipelineSpec] = ()):
        self._specs: dict[str, PipelineSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: PipelineSpec):
        if spec.key in self._specs:
            raise ValueError(f"Duplicate pipeline key: {spec.key}")
        for existing in self._specs.values():
            overlap = spec.supported_extensions & existing.supported_extensions
            if overlap:
                raise ValueError(
                    f"Pipeline extension conflict for {sorted(overlap)}: "
                    f"{existing.key} and {spec.key}"
                )
        self._specs[spec.key] = spec

    def all(self) -> list[PipelineSpec]:
        return list(self._specs.values())

    def get(self, key: str) -> PipelineSpec | None:
        return self._specs.get(key)

    def by_processor_kind(self, processor_kind: str) -> PipelineSpec | None:
        for spec in self._specs.values():
            if spec.processor_kind == processor_kind:
                return spec
        return None

    def route_file(self, file_path: str) -> PipelineRoute:
        extension = normalize_extension(os.path.splitext(file_path)[1])
        for spec in self._specs.values():
            if spec.supports_extension(extension):
                return PipelineRoute(extension=extension, spec=spec)
        for spec in self._specs.values():
            if spec.rejects_extension(extension):
                return PipelineRoute(
                    extension=extension,
                    rejected_by=spec,
                    reason=f"{extension or '(no extension)'} is explicitly rejected by {spec.key}",
                )
        return PipelineRoute(extension=extension, reason="no configured pipeline")

    def configured_extensions(self) -> dict[str, list[str]]:
        return {
            spec.processor_kind: sorted(spec.supported_extensions)
            for spec in self._specs.values()
        }

    def rejected_extensions(self) -> dict[str, list[str]]:
        return {
            spec.processor_kind: sorted(spec.rejected_extensions)
            for spec in self._specs.values()
            if spec.rejected_extensions
        }


def normalize_extension(extension: str) -> str:
    value = str(extension or "").strip().lower()
    if not value:
        return ""
    return value if value.startswith(".") else f".{value}"


PIPELINE_REGISTRY = PipelineRegistry([
    PipelineSpec(
        key="document_rag",
        label="Document RAG",
        processor_kind=PROCESSOR_KIND_RAGFLOW,
        content_kind=CONTENT_KIND_DOCUMENT,
        supported_extensions=frozenset({".doc", ".docx", ".pdf"}),
        stage=PIPELINE_STAGE_RETRIEVAL,
        dataset_kind=None,
        description="Text documents parsed by the configured RAG backend.",
    ),
    PipelineSpec(
        key="spreadsheet",
        label="Spreadsheet",
        processor_kind=PROCESSOR_KIND_SPREADSHEET,
        content_kind=CONTENT_KIND_SPREADSHEET,
        supported_extensions=frozenset({".xlsx"}),
        rejected_extensions=frozenset({".xls"}),
        stage=PIPELINE_STAGE_STRUCTURED,
        dataset_kind=DATASET_TABLE,
        description="Excel workbooks parsed into department-scoped table indexes.",
    ),
])


def route_file(file_path: str) -> PipelineRoute:
    return PIPELINE_REGISTRY.route_file(file_path)


def pipeline_for_file(file_path: str) -> PipelineSpec | None:
    return route_file(file_path).spec


def pipeline_for_processor_kind(processor_kind: str) -> PipelineSpec | None:
    return PIPELINE_REGISTRY.by_processor_kind(processor_kind)
