import os
import re
from typing import Any

import config.settings
from src.core.query_tokens import tokenize_hardware_query
from src.ingestion.kb_paths import safe_child_path, validate_kb_name
from src.pipelines.spreadsheet.pipeline import SpreadsheetIndexRequest, SpreadsheetIndexResult, SpreadsheetPipeline
from src.pipelines.spreadsheet.table_store import TableIndexStore


class SpreadsheetIndexService:
    """Department-scoped entry point for spreadsheet indexing.

    Spreadsheet data is a sibling processing product to RAG chunks. Keep its
    physical store scoped by department and KB so Excel evidence never lands in
    a shared table-index database.
    """

    def parse_and_index(
        self,
        request: SpreadsheetIndexRequest,
        progress_callback=None,
    ) -> SpreadsheetIndexResult:
        return self._pipeline(request.department_id, request.kb_name).parse_and_index(
            request,
            progress_callback=progress_callback,
        )

    def get_document_profile(self, record: Any) -> dict | None:
        return self._pipeline(record.department_id, record.kb_name).get_document_profile(record.id)

    def rank_document_matches(
        self,
        kb_name: str,
        department_id: str | int | None,
        query: str,
        limit: int = 20,
    ) -> dict[int, dict]:
        """Return local Excel record matches for fast source routing."""
        db_path = self.db_path(department_id, kb_name, create=False)
        if not os.path.exists(db_path):
            return {}
        terms = tokenize_hardware_query(query, max_tokens=8, include_cjk_ngrams=False)
        return TableIndexStore(db_path).rank_documents_by_terms(terms, limit=limit)

    def delete_record(self, record: Any):
        self._pipeline(record.department_id, record.kb_name).delete(record.id)

    def db_path(self, department_id: str | int | None, kb_name: str, create: bool = True) -> str:
        return os.path.join(self.kb_index_path(department_id, kb_name, create=create), "table_indexes.db")

    def kb_index_path(self, department_id: str | int | None, kb_name: str, create: bool = True) -> str:
        return safe_child_path(
            config.settings.STORAGE_DIR,
            "table_indexes",
            "departments",
            _safe_scope_part(department_id),
            "kbs",
            validate_kb_name(kb_name),
            create=create,
        )

    def _pipeline(self, department_id: str | int | None, kb_name: str) -> SpreadsheetPipeline:
        return SpreadsheetPipeline(TableIndexStore(db_path=self.db_path(department_id, kb_name)))


def _safe_scope_part(value: str | int | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip("._-")
    return cleaned[:80] or "unknown"
