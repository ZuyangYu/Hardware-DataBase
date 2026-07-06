import os
from dataclasses import dataclass, field

from src.pipelines.spreadsheet.table_store import TableIndexStats, TableIndexStore
from src.services.document_routing import TABLE_STATUS_ARCHIVED, TABLE_STATUS_INDEXED


@dataclass
class SpreadsheetIndexRequest:
    record_id: int
    kb_name: str
    department_id: str
    document_name: str
    source_group: str
    file_path: str
    local_path: str
    content_hash: str
    kb_id: int = 0


@dataclass
class SpreadsheetIndexResult:
    ok: bool
    status: str
    message: str
    stats: TableIndexStats | None = None
    warnings: list[str] = field(default_factory=list)


class SpreadsheetPipeline:
    STATUS_ARCHIVED = TABLE_STATUS_ARCHIVED
    STATUS_INDEXED = TABLE_STATUS_INDEXED
    STATUS_UNSUPPORTED = "unsupported"

    def __init__(self, store: TableIndexStore):
        if store is None:
            raise ValueError("SpreadsheetPipeline requires a scoped TableIndexStore")
        self.store = store

    def parse_and_index(self, request: SpreadsheetIndexRequest, progress_callback=None) -> SpreadsheetIndexResult:
        extension = os.path.splitext(request.document_name.lower())[1]
        if extension != ".xlsx":
            return SpreadsheetIndexResult(
                ok=False,
                status=self.STATUS_UNSUPPORTED,
                message=f"{request.document_name}: unsupported spreadsheet format; please upload .xlsx.",
                warnings=["当前仅支持 .xlsx 结构化解析，请将 .xls 另存为 .xlsx 后重新上传。"],
            )

        if progress_callback:
            progress_callback(10, "准备解析 Excel")
        stats = self.store.index_xlsx(
            record_id=request.record_id,
            kb_id=request.kb_id,
            kb_name=request.kb_name,
            department_id=request.department_id,
            document_name=request.document_name,
            source_group=request.source_group,
            file_path=request.file_path,
            local_path=request.local_path,
            content_hash=request.content_hash,
            progress_callback=progress_callback,
        )
        warning_suffix = f" warnings={len(stats.warnings)}" if stats.warnings else ""
        return SpreadsheetIndexResult(
            ok=True,
            status=self.STATUS_INDEXED,
            message=(
                f"{request.document_name}: indexed "
                f"{stats.sheet_count} sheets, {stats.row_count} rows, "
                f"{stats.cell_count} cells, {stats.text_block_count} blocks, "
                f"{stats.semantic_row_count} semantic rows.{warning_suffix}"
            ),
            stats=stats,
            warnings=stats.warnings,
        )

    def get_document_profile(self, record_id: int) -> dict | None:
        return self.store.get_document_profile(record_id)

    def delete(self, record_id: int):
        self.store.delete_document(record_id)
