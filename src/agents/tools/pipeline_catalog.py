"""KB catalog scan tool: lets the model see what sources exist before retrieving."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from src.agents.schemas import CatalogSource
from src.circuit.index_service import CircuitIndexService
from src.pipelines.document_store import PipelineDocumentStore
from src.pipelines.document_rag.base import RAGBackend
from src.services.kb_scope import kb_scope_from_context
from src.services.spreadsheet_index_service import SpreadsheetIndexService


def scan_kb_sources(
    kb_name: str,
    ctx,
    query: str,
    *,
    document_store: PipelineDocumentStore,
    spreadsheet_service: SpreadsheetIndexService | None = None,
    circuit_service: CircuitIndexService | None = None,
    rag_backend: RAGBackend | None = None,
) -> dict[str, Any]:
    spreadsheet_service = spreadsheet_service or SpreadsheetIndexService()
    scope = kb_scope_from_context(kb_name, ctx)
    sources: list[CatalogSource] = []
    if scope.department_id:
        records = document_store.list_documents(scope.kb_name, department_id=scope.department_id)
    else:
        records = []

    spreadsheet_matches: dict[int, dict] = {}
    circuit_matches: dict[int, dict] = {}
    if query and scope.department_id and spreadsheet_service is not None:
        try:
            spreadsheet_matches = spreadsheet_service.rank_document_matches(scope.kb_name, scope.department_id, query)
        except Exception:
            spreadsheet_matches = {}
        if circuit_service is not None:
            try:
                circuit_matches = circuit_service.rank_document_matches(scope.kb_name, ctx, query)
            except Exception:
                circuit_matches = {}

    seen_names: set[str] = set()
    for record in records:
        seen_names.add(record.document_name)
        profile = {}
        if record.processor_kind == "spreadsheet_table" and spreadsheet_service is not None:
            try:
                profile = spreadsheet_service.get_document_profile(record) or {}
            except Exception as exc:
                profile = {"profile_error": str(exc)}
        routing = (
            spreadsheet_matches.get(record.id, {})
            if record.processor_kind == "spreadsheet_table"
            else circuit_matches.get(record.id, {})
            if record.processor_kind == "circuit_design"
            else {}
        )
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
                metadata={**asdict(record), "routing": routing},
            )
        )

    errors: list[str] = []
    if rag_backend is not None:
        try:
            for info in rag_backend.list_documents(kb_name, ctx=ctx):
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
            errors.append(f"RAG backend document listing failed: {exc}")

    return {
        "sources": [source.model_dump() for source in sources],
        "summary": {
            "total": len(sources),
            "documents": sum(1 for item in sources if item.content_kind == "document_text"),
            "spreadsheets": sum(1 for item in sources if item.processor_kind == "spreadsheet_table"),
            "circuits": sum(1 for item in sources if item.processor_kind == "circuit_design"),
            "conversations": sum(1 for item in sources if item.processor_kind == "external_conversation"),
        },
        "errors": errors,
    }


def make_catalog_tool(rt, *, document_store: PipelineDocumentStore, spreadsheet_service, circuit_service, rag_backend):
    def list_kb_sources(query: str = "") -> str:
        """列出当前知识库中所有可用资料源（文档/表格/电路），含各资料的类型与状态。回答前先用它了解有哪些资料可查。"""
        from src.agents.tools.runtime import timed_tool_call

        def run(_: str, __: int):
            result = scan_kb_sources(
                rt.kb_name,
                rt.ctx,
                query,
                document_store=document_store,
                spreadsheet_service=spreadsheet_service,
                circuit_service=circuit_service,
                rag_backend=rag_backend,
            )

            from src.agents.schemas import Evidence

            summary = result.get("summary") or {}
            lines = [
                f"共 {summary.get('total', 0)} 个资料源：文档 {summary.get('documents', 0)}、"
                f"表格 {summary.get('spreadsheets', 0)}、电路 {summary.get('circuits', 0)}"
            ]
            for source in (result.get("sources") or [])[:50]:
                name = source.get("document_name") or ""
                kind = source.get("processor_kind") or ""
                status = source.get("status") or ""
                group = source.get("source_group") or ""
                record_id = source.get("record_id") or ""
                lines.append(f"- {name} | 类型:{kind} | 状态:{status} | 来源组:{group} | record_id:{record_id}")
            for error in result.get("errors") or []:
                lines.append(f"- 错误: {error}")
            text = "\n".join(lines)

            return [
                Evidence(
                    id="catalog:scan",
                    content=text,
                    source_name="知识库目录",
                    content_kind="catalog",
                    processor_kind="pipeline_catalog",
                    score=1.0,
                    locator={},
                    metadata={"tool": "list_kb_sources"},
                )
            ]

        items = timed_tool_call(rt, "list_kb_sources", query, None, lambda: run("", 1))
        from src.agents.tools.runtime import format_evidence_for_llm

        return format_evidence_for_llm(items)

    return list_kb_sources
