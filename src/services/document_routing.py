import os

from src.pipelines.registry import (
    PIPELINE_REGISTRY,
    PROCESSOR_KIND_RAGFLOW,
    PROCESSOR_KIND_SPREADSHEET,
    PipelineRoute,
    PipelineSpec,
    pipeline_for_file,
    route_file,
)

RAGFLOW_PARSE_START_DELAY_SECONDS = 2.0
RAGFLOW_STATUS_FAILED = "failed"
RAGFLOW_STATUS_DELETED = "deleted"
RAGFLOW_STATUS_PARSING = "parsing"
TABLE_STATUS_ARCHIVED = "archived"
TABLE_STATUS_INDEXED = "indexed"
TABLE_STATUS_PROCESSING = "processing"

RAGFLOW_DOCUMENT_EXTENSIONS = set(
    PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_RAGFLOW).supported_extensions
)
SPREADSHEET_EXTENSIONS = set(
    PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_SPREADSHEET).supported_extensions
)
UNSUPPORTED_LEGACY_SPREADSHEET_EXTENSIONS = set(
    PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_SPREADSHEET).rejected_extensions
)


def document_extension(file_path: str) -> str:
    return os.path.splitext(file_path.lower())[1]


def supported_pipeline_for_file(file_path: str) -> str | None:
    spec = pipeline_for_file(file_path)
    return spec.processor_kind if spec else None


def route_pipeline_for_file(file_path: str) -> PipelineRoute:
    return route_file(file_path)


def pipeline_spec_for_file(file_path: str) -> PipelineSpec | None:
    return pipeline_for_file(file_path)


def configured_pipeline_extensions() -> dict[str, list[str]]:
    return PIPELINE_REGISTRY.configured_extensions()
