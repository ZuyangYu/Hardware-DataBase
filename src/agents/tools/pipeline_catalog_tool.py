from __future__ import annotations

from dataclasses import asdict
from typing import Any

from src.agents.state import CatalogSource
from src.pipelines.document_store import PipelineDocumentStore
from src.pipelines.document_rag.base import RAGBackend
from src.pipelines.document_rag.schemas import RequestContext
from src.services.kb_scope import kb_scope_from_context
from src.services.spreadsheet_index_service import SpreadsheetIndexService


class PipelineCatalogTool:
    name = "pipeline_catalog"
    description = "Scan mounted knowledge-base files and structured pipeline assets."

    def __init__(
        self,
        *,
        document_store: PipelineDocumentStore,
        spreadsheet_service: SpreadsheetIndexService,
        rag_backend: RAGBackend | None = None,
    ):
        self.document_store = document_store
        self.spreadsheet_service = spreadsheet_service
        self.rag_backend = rag_backend

    def scan(self, kb_name: str, ctx: RequestContext | None) -> dict[str, Any]:
        scope = kb_scope_from_context(kb_name, ctx)
        sources: list[CatalogSource] = []
        if scope.department_id:
            records = self.document_store.list_documents(scope.kb_name, department_id=scope.department_id)
        else:
            records = []

        seen_names: set[str] = set()
        for record in records:
            seen_names.add(record.document_name)
            profile = {}
            if record.processor_kind == "spreadsheet_table":
                try:
                    profile = self.spreadsheet_service.get_document_profile(record) or {}
                except Exception as exc:
                    profile = {"profile_error": str(exc)}
            sources.append(
                CatalogSource(
                    record_id=record.id,
                    document_name=record.document_name,
                    original_file_name=record.original_file_name,
                    processor_kind=record.processor_kind,
                    content_kind=record.content_kind,
                    dataset_kind=record.dataset_kind,
                    source_group=record.source_group,
                    status=record.status,
                    local_path=record.local_path,
                    file_size=record.file_size,
                    profile=profile,
                    metadata=asdict(record),
                )
            )

        if self.rag_backend is not None:
            try:
                for info in self.rag_backend.list_documents(kb_name, ctx=ctx):
                    if info.name in seen_names:
                        continue
                    sources.append(
                        CatalogSource(
                            record_id=int(info.metadata.get("store_id") or 0) or None,
                            document_name=info.name,
                            processor_kind=info.processor_kind or info.metadata.get("processor_kind", ""),
                            content_kind=info.metadata.get("content_kind", "document_text"),
                            dataset_kind=info.dataset_kind,
                            source_group=info.metadata.get("source_group", ""),
                            status=info.status or info.metadata.get("status", ""),
                            local_path=info.local_path,
                            profile=info.spreadsheet_profile or info.metadata.get("spreadsheet_profile") or {},
                            metadata=dict(info.metadata or {}),
                        )
                    )
            except Exception as exc:
                return {
                    "sources": [source.model_dump() for source in sources],
                    "errors": [f"RAG backend document listing failed: {exc}"],
                }

        return {
            "sources": [source.model_dump() for source in sources],
            "summary": {
                "total": len(sources),
                "documents": sum(1 for item in sources if item.content_kind == "document_text"),
                "spreadsheets": sum(1 for item in sources if item.processor_kind == "spreadsheet_table"),
            },
            "errors": [],
        }

