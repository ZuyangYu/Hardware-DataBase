"""Agent tools for chat attachments (design §11.2).

Thin adapters only: scope comes from the server-resolved ``ToolRuntime``
(``attachment_refs``), ACL is re-derived on every call from the frozen refs,
and results are converted to the shared ``Evidence`` model. Tools never
accept local paths, never see other sessions' assets, and cannot widen the
attachment scope the turn was created with.
"""

from __future__ import annotations

import re
from typing import Any

from src.agents.schemas import Evidence
from src.agents.tools.runtime import ToolRuntime, format_tool_result, timed_tool_call
from src.attachments.evidence import evidence_from_chunk, evidence_from_part
from src.attachments.retrieval import AttachmentRetrievalService
from src.attachments.service import AttachmentService
from src.attachments.store import AttachmentStore

_TOOL_SOURCE = "chat_attachment"


def _service(rt: ToolRuntime) -> AttachmentService:
    configured = getattr(rt, "attachment_service", None)
    if configured is None:
        return AttachmentService()
    if isinstance(configured, AttachmentService):
        return configured
    # Keep compatibility with callers that injected the raw store before the
    # per-call ACL boundary was introduced.
    return AttachmentService(store=configured)


def _authorized_refs(rt: ToolRuntime) -> list[Any]:
    """Re-check every frozen ref against the current owner/session row.

    A turn's frozen refs constrain the maximum scope, while the live lookup
    handles deletion, expiry, and ownership changes that happen after the
    turn starts.  Missing identity or a mismatched asset id fails closed.
    """
    from src.attachments.models import scope_allows_attachments

    if not scope_allows_attachments(str(getattr(rt, "source_scope", "") or "")):
        return []
    user_id = getattr(rt, "attachment_user_id", None)
    session_id = getattr(rt, "attachment_session_id", None)
    if user_id is None or session_id is None:
        return []
    service = _service(rt)
    authorized: list[Any] = []
    seen: set[str] = set()
    for ref in getattr(rt, "attachment_refs", None) or []:
        attachment_id = str(getattr(ref, "attachment_id", "") or "").strip()
        expected_asset_id = str(getattr(ref, "asset_id", "") or "").strip()
        if not attachment_id or not expected_asset_id or attachment_id in seen:
            continue
        seen.add(attachment_id)
        try:
            record = service.get_attachment(
                attachment_id=attachment_id,
                session_id=int(session_id),
                user_id=int(user_id),
            )
        except Exception:
            continue
        if record.asset_id != expected_asset_id or record.session_id != int(session_id):
            continue
        # A parse failure is not a readable capability even if the attachment
        # row itself is still active and can be retried from the UI.
        from src.attachments.models import PARSE_STATUS_FAILED

        if record.parse_status == PARSE_STATUS_FAILED:
            continue
        authorized.extend(service.build_refs([record]))
    return authorized


def _asset_ids(refs: list[Any]) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []
    for ref in refs:
        asset_id = str(getattr(ref, "asset_id", "") or "")
        if asset_id and asset_id not in seen:
            seen.add(asset_id)
            ids.append(asset_id)
    return ids


def _ref_for_asset(refs: list[Any], asset_id: str):
    for ref in refs:
        if str(getattr(ref, "asset_id", "") or "") == asset_id:
            return ref
    return None


def _store(rt: ToolRuntime) -> AttachmentStore:
    return _service(rt).store


def make_attachment_list(rt: ToolRuntime):
    """列出本轮已授权附件及其解析状态。"""

    def attachment_list() -> str:
        """返回当前轮已授权附件的文件名与解析状态。"""
        refs = _authorized_refs(rt)
        if not refs:
            return "本轮会话没有挂载任何附件。"
        lines = ["本轮可用附件："]
        for index, ref in enumerate(refs, start=1):
            state = str(getattr(ref, "parse_status", "") or "queued")
            note = str(getattr(ref, "degraded_reason", "") or "")
            suffix = f"（{note}）" if note and state != "ready" else ""
            lines.append(
                f"{index}. {getattr(ref, 'filename', '')} — 解析状态: {state}{suffix}"
            )
        lines.append(
            "使用 attachment_search 检索附件内容，attachment_read 读取指定段落，"
            "attachment_visual_analyze 分析指定 PDF 页面。"
        )
        return "\n".join(lines)

    return attachment_list


def make_attachment_search(rt: ToolRuntime, retrieval: AttachmentRetrievalService | None = None):
    """在当前轮已授权附件内做本地检索，返回带定位的证据。"""

    def attachment_search(query: str, top_k: int = 6) -> str:
        """按查询词检索当前轮附件内容，并返回带定位的证据。"""
        from src.attachments.models import PARSER_VERSION  # noqa: F401

        retriever = retrieval or getattr(rt, "attachment_retrieval", None)
        if retriever is None:
            retriever = AttachmentRetrievalService()
        refs = _authorized_refs(rt)
        asset_ids = _asset_ids(refs)
        if not asset_ids:
            return "本轮没有可用附件。"
        from src.observability.metrics import record_attachment

        def _run() -> list[Evidence]:
            result = retriever.search(
                str(query or ""),
                asset_ids=asset_ids,
                limit=max(1, min(int(top_k or 6), 12)),
            )
            evidence: list[Evidence] = []
            for chunk in result.chunks:
                ref = _ref_for_asset(refs, chunk.asset_id)
                if ref is None:
                    continue  # never emit evidence outside the frozen scope
                evidence.append(evidence_from_chunk(ref=ref, chunk=chunk))
            record_attachment("search", status="ok" if evidence else "empty")
            for reason in result.degraded_reasons:
                rt.emit("degraded", {"stage": "attachment_search", "reason": reason})
            return evidence

        items, adds_nothing = timed_tool_call(rt, "attachment_search", str(query or ""), None, _run)
        if not items:
            return "附件中没有找到相关内容。可尝试型号、位号、网络名或换一种问法。"
        return format_tool_result(rt, adds_nothing, items)

    return attachment_search


def make_attachment_read(rt: ToolRuntime):
    """按序号/定位读取附件片段（小范围精读，补充检索）。"""

    def attachment_read(query: str = "", ordinal_from: int = 0, limit: int = 4) -> str:
        """读取当前轮附件中指定序号起始的少量解析片段。"""
        store = _store(rt)
        refs = _authorized_refs(rt)
        asset_ids = _asset_ids(refs)
        if not asset_ids:
            return "本轮没有可用附件。"

        def _run() -> list[Evidence]:
            start = max(0, int(ordinal_from or 0))
            bounded = max(1, min(int(limit or 4), 10))
            evidence: list[Evidence] = []
            for asset_id in asset_ids:
                ref = _ref_for_asset(refs, asset_id)
                if ref is None:
                    continue
                parts = store.list_parts(asset_id)
                selected = [part for part in parts if part.ordinal >= start][:bounded]
                for part in selected:
                    evidence.append(evidence_from_part(ref=ref, part=part))
            return evidence[: bounded * max(1, len(asset_ids))]

        query_text = str(query or f"read from {ordinal_from or 0}")
        items, adds_nothing = timed_tool_call(rt, "attachment_read", query_text, None, _run)
        from src.observability.metrics import record_attachment

        record_attachment("read", status="ok" if items else "empty")
        if not items:
            return "没有读取到附件内容（可能超出片段范围）。"
        return format_tool_result(rt, adds_nothing, items)

    return attachment_read


def make_attachment_table_query(rt: ToolRuntime):
    """对附件 Excel 做只读 SQL/schema 查询（复用 KB 的 SQL 安全栈）。"""

    def attachment_table_query(query: str = "", sql: str = "", top_k: int = 8) -> str:
        """查询当前轮 XLSX/XLSM 附件的结构或执行只读 SELECT。"""
        from src.agents.tools.spreadsheet_tools import (
            _execute_readonly_sql,
            _format_sql_result,
            _format_schema_entry,
            _load_sql_registry,
            _tokens,
            _validate_readonly_sql,
        )
        from src.attachments.storage import resolve_storage_key

        store = _store(rt)
        refs = _authorized_refs(rt)
        asset_ids = _asset_ids(refs)
        tool_name = "attachment_table_query"
        query_text = str(sql or query or "spreadsheet")

        def _run() -> list[Evidence]:
            from src.agents.scopes import AttachmentSpreadsheetScopeResolver

            evidence: list[Evidence] = []
            for asset_id in asset_ids:
                ref = _ref_for_asset(refs, asset_id)
                if ref is None:
                    continue
                asset = store.get_asset(asset_id)
                if asset is None or asset.extension not in {".xlsx", ".xlsm"}:
                    continue
                scope = AttachmentSpreadsheetScopeResolver(store).resolve(refs=[ref])
                if not scope.db_path or not scope.allowed_record_ids:
                    continue
                index_db = scope.db_path
                record_id = next(iter(scope.allowed_record_ids))
                try:
                    db_path = str(resolve_storage_key(index_db))
                except Exception:
                    continue
                registry = [
                    entry
                    for entry in _load_sql_registry(db_path)
                    if entry.get("record_id") == record_id
                ]
                if str(sql or "").strip():
                    allowed = {entry["table_name"]: entry for entry in registry}
                    ast, error = _validate_readonly_sql(str(sql), set(allowed))
                    if error:
                        evidence.append(
                            Evidence(
                                id=f"att-sql:{asset_id[:8]}:invalid",
                                content=f"SQL 校验未通过: {error}",
                                source_name=ref.filename,
                                content_kind="spreadsheet_sql_result",
                                processor_kind=_TOOL_SOURCE,
                                score=0.0,
                                locator={"attachment_id": ref.attachment_id},
                                metadata={"source_type": "chat_attachment"},
                            )
                        )
                        continue
                    records, exec_error = _execute_readonly_sql(db_path, ast.sql(dialect="sqlite"))
                    if exec_error:
                        evidence.append(
                            Evidence(
                                id=f"att-sql:{asset_id[:8]}:error",
                                content=f"SQL 执行失败: {exec_error}",
                                source_name=ref.filename,
                                content_kind="spreadsheet_sql_result",
                                processor_kind=_TOOL_SOURCE,
                                score=0.0,
                                locator={"attachment_id": ref.attachment_id},
                                metadata={"source_type": "chat_attachment"},
                            )
                        )
                        continue
                    evidence.append(
                        Evidence(
                            id=f"att-sql:{asset_id[:8]}:result",
                            content=_format_sql_result(records)[:6000],
                            source_name=ref.filename,
                            content_kind="spreadsheet_sql_result",
                            processor_kind=_TOOL_SOURCE,
                            score=1.0,
                            locator={"attachment_id": ref.attachment_id, "sql": "executed"},
                            metadata={"source_type": "chat_attachment"},
                        )
                    )
                else:
                    tokens = _tokens(str(query or ""))
                    scored = [
                        entry
                        for entry in registry
                        if not tokens
                        or any(token in f"{entry['document_name']} {entry['sheet_name']}".casefold() for token in tokens)
                    ]
                    for entry in scored[: max(1, min(int(top_k or 8), 20))]:
                        evidence.append(
                            Evidence(
                                id=f"att-schema:{entry['table_name']}",
                                content=_format_schema_entry(entry),
                                source_name=ref.filename,
                                content_kind="spreadsheet_schema",
                                processor_kind=_TOOL_SOURCE,
                                score=1.0,
                                locator={"attachment_id": ref.attachment_id, "table_name": entry["table_name"]},
                                metadata={"source_type": "chat_attachment"},
                            )
                        )
            return evidence

        items, adds_nothing = timed_tool_call(rt, tool_name, query_text, None, _run)
        if not items:
            return "本轮附件中没有可查询的 Excel 表格（仅支持 .xlsx/.xlsm）。"
        return format_tool_result(rt, adds_nothing, items)

    return attachment_table_query


def make_attachment_circuit_search(rt: ToolRuntime):
    """对附件 EDF/EDIF 做电路查询（复用 CircuitIndexService 核心引擎）。"""

    def attachment_circuit_search(query: str, top_k: int = 5) -> str:
        """检索当前轮 EDF/EDIF 附件中的电路结构与连接信息。"""
        from src.attachments.storage import resolve_storage_key
        from src.circuit.index_service import CircuitIndexService
        from src.circuit.store import CircuitStore

        store = _store(rt)
        refs = _authorized_refs(rt)
        asset_ids = _asset_ids(refs)

        def _run() -> list[Evidence]:
            from src.agents.scopes import AttachmentCircuitScopeResolver

            evidence: list[Evidence] = []
            for asset_id in asset_ids:
                ref = _ref_for_asset(refs, asset_id)
                if ref is None:
                    continue
                asset = store.get_asset(asset_id)
                if asset is None or asset.extension not in {".edf", ".edif"}:
                    continue
                scope = AttachmentCircuitScopeResolver(store).resolve(refs=[ref])
                if not scope.store_root:
                    continue
                circuit_root = scope.store_root
                kb_name = str((asset.manifest or {}).get("kb_name") or "")
                if not kb_name:
                    continue
                try:
                    root = str(resolve_storage_key(circuit_root))
                except Exception:
                    continue
                service = CircuitIndexService(store=CircuitStore(root=root))
                # ctx=None -> no department filter; the store root itself is
                # attachment-private and already scoped to this session.
                hits = service.query(
                    kb_name=kb_name,
                    query=str(query or ""),
                    ctx=None,
                    top_k=max(1, min(int(top_k or 5), 10)),
                )
                for hit in hits:
                    hit.metadata = {
                        **(hit.metadata or {}),
                        "source_type": "chat_attachment",
                        "attachment_id": ref.attachment_id,
                        "asset_id": asset_id,
                        "filename": ref.filename,
                    }
                    hit.source_name = hit.source_name or ref.filename
                evidence.extend(hits)
            return evidence

        items, adds_nothing = timed_tool_call(
            rt, "attachment_circuit_search", str(query or ""), None, _run
        )
        if not items:
            return "附件中没有找到电路设计或相关网络/器件信息。"
        return format_tool_result(rt, adds_nothing, items)

    return attachment_circuit_search


def make_attachment_visual_analyze(rt: ToolRuntime, analyzer: Any | None = None):
    """Analyze selected PDF pages through the optional remote visual gateway."""

    def attachment_visual_analyze(
        question: str,
        page: int = 0,
        pages: str = "",
    ) -> str:
        """分析当前轮 PDF 附件指定页面的视觉内容并返回证据。"""
        refs = _authorized_refs(rt)
        if not refs:
            return "本轮没有可用附件。"
        selected_pages = _visual_pages(rt, page=page, pages=pages)
        visual_analyzer = analyzer or getattr(rt, "attachment_visual_analyzer", None)
        if visual_analyzer is None:
            from src.attachments.visual import AttachmentVisualAnalyzer

            visual_analyzer = AttachmentVisualAnalyzer(store=_store(rt))
        state: dict[str, list[str]] = {"degraded_reasons": []}

        def _run() -> list[Evidence]:
            outcome = visual_analyzer.analyze(
                refs=refs,
                question=str(question or ""),
                page_numbers=selected_pages,
            )
            state["degraded_reasons"] = list(outcome.degraded_reasons)
            return list(outcome.evidence)

        query_text = str(question or "分析附件页面")
        items, adds_nothing = timed_tool_call(
            rt,
            "attachment_visual_analyze",
            query_text,
            {"pages": ",".join(str(value) for value in selected_pages)},
            _run,
        )
        from src.observability.metrics import record_attachment

        record_attachment("visual", status="ok" if items else "degraded")
        for reason in state["degraded_reasons"]:
            rt.emit("degraded", {"stage": "attachment_visual", "reason": reason})
        if any(
            reason in {"visual_failed", "visual_unavailable", "visual_source_unavailable"}
            for reason in state["degraded_reasons"]
        ):
            record_attachment("visual_failure", status="degraded")
        if not items:
            return "未返回视觉分析证据；远程视觉能力可能未启用、页面不可用或供应商失败。"
        return format_tool_result(rt, adds_nothing, items)

    return attachment_visual_analyze


def _visual_pages(rt: ToolRuntime, *, page: int, pages: str) -> list[int]:
    """Parse explicit pages, or reuse page locators from earlier evidence."""

    values: list[int] = []

    def add(value: Any) -> None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return
        if parsed not in values:
            values.append(parsed)

    if int(page or 0) != 0:
        add(page)
    for token in re.split(r"[,，\s]+", str(pages or "").strip()):
        if not token:
            continue
        match = re.fullmatch(r"(\d+)\s*[\-~至]\s*(\d+)", token)
        if match:
            start, end = (int(match.group(1)), int(match.group(2)))
            if end < start:
                start, end = end, start
            for value in range(start, min(end, start + 19) + 1):
                add(value)
        else:
            add(token)
    if values:
        return values
    for item in getattr(rt, "evidence", []) or []:
        if str(item.metadata.get("source_type") or "") != "chat_attachment":
            continue
        try:
            add(item.locator.get("page"))
        except AttributeError:
            continue
    return values or [1]


def build_attachment_tools(rt: ToolRuntime) -> list[Any]:
    """Scope-based attachment toolset (design §11.3)."""
    from src.attachments.models import scope_allows_attachments

    if (
        not scope_allows_attachments(str(getattr(rt, "source_scope", "") or ""))
        or not getattr(rt, "attachment_refs", None)
    ):
        return []
    return [
        make_attachment_list(rt),
        make_attachment_search(rt),
        make_attachment_read(rt),
        make_attachment_table_query(rt),
        make_attachment_circuit_search(rt),
        make_attachment_visual_analyze(rt),
    ]
