"""Agent closures over strict adapters; no anchor-KB fallback is available."""

from __future__ import annotations

from src.agents.schemas import Evidence
from src.agents.tools.circuit_tools import ALLOWED_QUERY_OPERATIONS


def make_scoped_structured_tools(rt, *, wiki_assist=False):
    def search(kind, name, query, top_k, source_ref=None):
        from src.agents.tools.runtime import format_tool_result, timed_tool_call
        from src.agents.tools.document_rag_tool import ScopedEvidenceBatch, _scope_statuses

        service, budget = rt.multi_kb_retrieval, rt.retrieval_budget
        if service is None or budget is None or rt.knowledge_scope is None:
            return "filter_unsupported: multi_kb_retrieval_not_configured"
        failures = []

        def run():
            outcome = service.search(snapshot=rt.knowledge_scope, queries=[query], retriever_kind=kind,
                per_source_top_k=max(1, min(int(top_k), 20)), budget=budget, source_ref=source_ref)
            failures.extend(f"kb:{item.kb_id} {item.status}: {item.error_code or item.status}"
                for item in outcome.source_outcomes if item.status not in {"success_with_hits", "success_empty"})
            raw_evidence = list(outcome.evidence)
            fusion_summary = {}
            if outcome.source_outcomes:
                import src.settings as settings
                from src.services.evidence_fusion import fuse_evidence

                fused = fuse_evidence(
                    outcome,
                    purpose=str(getattr(rt.knowledge_scope, "purpose", "answer")),
                    max_items=max(1, min(int(top_k), int(settings.MULTI_KB_EVIDENCE_MAX_ITEMS))),
                    token_budget=max(0, int(settings.MULTI_KB_CONTEXT_MAX_TOKENS)),
                )
                raw_evidence = list(fused.evidence)
                fusion_summary = {
                    "selection_hash": str(getattr(outcome, "selection_hash", "") or ""),
                    "coverage": dict(fused.coverage), "complete": bool(fused.complete),
                    "token_count": int(fused.token_count), "candidate_refs": list(fused.candidate_refs),
                    "discarded": dict(fused.discarded), "query_coverage": list(fused.query_coverage),
                    "source_representatives": list(fused.source_representatives),
                }
            result = ScopedEvidenceBatch(Evidence(id=f"structured:{item.metadata['kb_id']}:{item.id}", content=item.content,
                source_name=item.source_name, content_kind=str(item.metadata.get("content_kind") or "structured"),
                processor_kind=str(item.metadata.get("processor_kind") or kind), score=item.score,
                locator=dict(item.metadata.get("chunk_or_structured_locator") or {}),
                metadata={**item.metadata, "tool": name}) for item in raw_evidence)
            result.source_statuses = _scope_statuses(outcome)
            result.fusion_summary = fusion_summary
            return result

        items, adds_nothing = timed_tool_call(rt, name, query, None, run)
        result = format_tool_result(rt, adds_nothing, items)
        return result + ("\n来源检索未完成：\n" + "\n".join(failures) if failures else "")

    def spreadsheet_row_search(query: str, top_k: int = rt.top_k, source_ref: str | None = None) -> str:
        """在本轮已选知识库中检索表格行。source_ref 可用 kb:数字 限定某个已选库。"""
        return search("spreadsheet_semantic", "spreadsheet_row_search", query, top_k, source_ref)

    def spreadsheet_cell_lookup(query: str, top_k: int = rt.top_k, source_ref: str | None = None) -> str:
        """在本轮已选知识库中按字段或值定位表格单元格，保留实际文件、工作表与单元格身份。"""
        return search("spreadsheet_cell", "spreadsheet_cell_lookup", query, top_k, source_ref)

    def circuit_search(query: str, top_k: int = rt.top_k, query_operation: str = "auto", source_ref: str | None = None) -> str:
        """检索已选知识库的电路；operation 可选 auto/structure_overview/module_list/resolve_identity/resolved_connections。"""
        if query_operation not in ALLOWED_QUERY_OPERATIONS:
            return "filter_unsupported: circuit_query_operation_invalid"
        kind = "circuit" if query_operation == "auto" else f"circuit_{query_operation}"
        return search(kind, "circuit_search", query, top_k, source_ref)

    def conversation_search(query: str, top_k: int = rt.top_k, source_ref: str | None = None) -> str:
        """在本轮已选知识库的外部对话原始消息中检索，保留会话与轮次定位。"""
        return search("conversation", "conversation_search", query, top_k, source_ref)

    def spreadsheet_schema_lookup(query: str = "", top_k: int = 8) -> str:
        """多库表格 SQL 暂不支持冻结表身份；返回明确诊断，请改用表格行或单元格检索。"""
        return "filter_unsupported: multi_kb_sql_schema_unsupported；请使用 spreadsheet_row_search 或 spreadsheet_cell_lookup。"

    def spreadsheet_sql_query(sql: str) -> str:
        """拒绝无法绑定实际 KB 与冻结记录的 SQL 查询。"""
        return "filter_unsupported: multi_kb_sql_query_unsupported；请使用 spreadsheet_row_search 或 spreadsheet_cell_lookup。"

    def wiki_search(query: str, top_k: int = rt.top_k, include_linked: bool = False, source_ref: str | None = None) -> str:
        """查询已选知识库的 Wiki 摘要；冻结原始记录检索不支持 Wiki，不扩展未冻结链接。"""
        return search("wiki", "wiki_search", query, top_k, source_ref)

    tools = [spreadsheet_row_search, spreadsheet_cell_lookup, circuit_search, conversation_search,
             spreadsheet_schema_lookup, spreadsheet_sql_query]
    if wiki_assist:
        tools.append(wiki_search)
    return tools
