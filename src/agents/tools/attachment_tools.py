"""Agent tools for chat attachments (design §11.2).

Thin adapters only: scope comes from the server-resolved ``ToolRuntime``
(``attachment_refs``), ACL is re-derived on every call from the frozen refs,
and results are converted to the shared ``Evidence`` model. Tools never
accept local paths, never see other sessions' assets, and cannot widen the
attachment scope the turn was created with.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.agents.schemas import Evidence
from src.agents.tools.runtime import (
    ToolRuntime,
    empty_result_message,
    format_tool_result,
    timed_tool_call,
)
from src.attachments.audit import begin_attachment_audit, finish_attachment_audit
from src.attachments.evidence import (
    candidate_preview_from_chunk,
    evidence_from_chunk,
    evidence_from_part,
)
from src.attachments.models import AttachmentError
from src.attachments.retrieval import AttachmentRetrievalService
from src.attachments.service import AttachmentService, revision_change_summary
from src.attachments.store import AttachmentStore
from src.services.evidence_sources import ATTACHMENT, normalize_source_type

_TOOL_SOURCE = "chat_attachment"

_RELEASE_STATE_LABELS = {
    "not_requested": "未申请发布",
    "pending": "发布审核中",
    "approved": "已发布（已生成独立正式文件）",
    "rejected": "发布申请被拒绝",
}

_REVISION_ID_PREFIX = "attachment-revision-"
_LATEST_CANDIDATE_SELECTORS = {
    "latest",
    "latest_candidate",
    "最新",
    "最新候选",
    "最新版本",
    "上一版",
}
_FOLLOW_UP_VERSION_PATTERN = re.compile(
    r"继续|最新候选|最新版本|上一版|当前候选|latest", re.IGNORECASE
)
_REPLACE_INTENT_PATTERN = re.compile(
    r"替换|改成|改为|修改|replace|change", re.IGNORECASE
)
_QUOTED_SEGMENT_RE = re.compile(r"[「“\"']([^」”\"']{1,200})[」”\"']")
_EMBEDDED_REMOVAL_INTENT_PATTERN = re.compile(
    r"(?:"
    r"(?:删除|删掉|移除|去掉|清除|不要|不需要|丢弃)[^。；\n]{0,12}"
    r"(?:图片?|插图|图表?|表格?|表|嵌入|对象|附件|公式|流程图|示意图)"
    r"|"
    r"(?:图片?|插图|图表?|表格?|表|嵌入|对象|附件|公式|流程图|示意图)[^。；\n]{0,12}"
    r"(?:删除|删掉|移除|去掉|清除|不要|不需要)"
    r")",
    re.IGNORECASE,
)


def _lineage_revisions(rt: ToolRuntime, refs: list[Any]) -> list[Any]:
    """All review candidates descending from the authorized refs, newest first.

    The attachment scope frozen on a turn contains the files the client sent;
    a follow-up such as “继续修改最新候选” must still bind to the candidate
    produced by a previous turn, so the tool walks the revision lineage of the
    authorized attachments inside the same session/owner boundary.
    """

    service = _service(rt)
    session_id = int(getattr(rt, "attachment_session_id", 0) or 0)
    user_id = int(getattr(rt, "attachment_user_id", 0) or 0)
    if not session_id or not user_id:
        return []
    try:
        revisions = service.store.list_attachment_revisions(
            session_id=session_id, user_id=user_id, limit=200
        )
    except Exception:  # noqa: BLE001 - lineage lookup must stay fail-soft
        return []
    by_parent: dict[str, list[Any]] = {}
    for revision in revisions:
        if revision.status != "review_candidate":
            continue
        by_parent.setdefault(str(revision.parent_attachment_id or ""), []).append(
            revision
        )
    found: list[Any] = []
    seen: set[str] = set()
    frontier = [str(getattr(ref, "attachment_id", "") or "") for ref in refs]
    while frontier:
        parent = frontier.pop(0)
        for revision in by_parent.get(parent, []):
            if revision.revision_id in seen:
                continue
            seen.add(revision.revision_id)
            found.append(revision)
            frontier.append(str(revision.child_attachment_id))
    found.sort(
        key=lambda item: (str(item.created_at or ""), item.revision_id),
        reverse=True,
    )
    return found


def _resolve_revision_target(rt: ToolRuntime, refs: list[Any], attachment_id: str):
    """Resolve an edit/release target to one authorized child attachment.

    Accepts an explicit attachment id, a revision id, or one of the
    "latest candidate" selectors.  With no explicit value and multiple
    authorized attachments the newest revision candidate wins, so continued
    edits bind to the current version instead of the original upload.  Returns
    ``(attachment_id, revision, error_payload_or_None)``.
    """

    service = _service(rt)
    session_id = int(getattr(rt, "attachment_session_id", 0) or 0)
    user_id = int(getattr(rt, "attachment_user_id", 0) or 0)
    authorized_ids = [
        str(getattr(ref, "attachment_id", "") or "").strip() for ref in refs
    ]
    lineage = _lineage_revisions(rt, refs)
    allowed_ids = set(authorized_ids) | {
        str(revision.child_attachment_id) for revision in lineage
    }
    latest_candidate = (
        str(lineage[0].child_attachment_id) if lineage else ""
    )

    def _revision_for(attachment: str):
        return service.get_attachment_revision_by_child(
            child_attachment_id=attachment,
            session_id=session_id,
            user_id=user_id,
        )

    normalized = str(attachment_id or "").strip()
    if normalized.startswith(_REVISION_ID_PREFIX):
        revision = service.get_attachment_revision(
            revision_id=normalized, session_id=session_id, user_id=user_id
        )
        if revision is None or revision.child_attachment_id not in allowed_ids:
            return "", None, {
                "error_code": "attachment_scope_denied",
                "message": "该修订版本不在本轮已授权范围内。",
            }
        return revision.child_attachment_id, revision, None

    if normalized and normalized not in _LATEST_CANDIDATE_SELECTORS:
        # Models often echo the filename (or the asset id) instead of the
        # opaque attachment id.  Accept an unambiguous alias so a valid,
        # authorized target is not rejected on a cosmetic mismatch.
        if normalized not in allowed_ids:
            aliases = [
                str(getattr(ref, "attachment_id", "") or "").strip()
                for ref in refs
                if normalized
                in {
                    str(getattr(ref, "filename", "") or "").strip(),
                    str(getattr(ref, "asset_id", "") or "").strip(),
                }
            ]
            unique_aliases = list(dict.fromkeys(alias for alias in aliases if alias))
            if len(unique_aliases) != 1:
                return "", None, {
                    "error_code": "attachment_scope_denied",
                    "message": (
                        "该附件不在本轮已授权范围内；"
                        f"可用 attachment_id: {', '.join(authorized_ids)}"
                    ),
                }
            normalized = unique_aliases[0]
        question = str(getattr(rt, "question", "") or "")
        if (
            _FOLLOW_UP_VERSION_PATTERN.search(question)
            and latest_candidate
            and normalized != latest_candidate
        ):
            return "", None, {
                "error_code": "attachment_version_mismatch",
                "message": (
                    "用户要求继续修改最新修订候选；请省略 attachment_id "
                    f"（系统会自动选择最新候选）或使用 {latest_candidate}。"
                ),
            }
        return normalized, _revision_for(normalized), None

    if latest_candidate:
        latest = lineage[0]
        return latest_candidate, latest, None
    if len(authorized_ids) == 1:
        only = authorized_ids[0]
        return only, _revision_for(only), None
    return "", None, {
        "error_code": "attachment_target_unresolved",
        "message": (
            "编辑附件需要明确 attachment_id；当前附件数量不是一份，且没有修订候选。"
            f"可选附件: {', '.join(authorized_ids)}"
        ),
    }


def _service(rt: ToolRuntime) -> AttachmentService:
    configured = getattr(rt, "attachment_service", None)
    if configured is None:
        return AttachmentService()
    # ``AttachmentService`` is imported for the normal runtime path, but test
    # and embedding callers may temporarily replace the module attribute with
    # a factory.  A cached factory is not a valid second argument to
    # ``isinstance``; prefer the small service protocol and only use the type
    # check when the imported value is actually a class.
    if (
        hasattr(configured, "get_attachment")
        and hasattr(configured, "build_refs")
    ):
        return configured
    if isinstance(AttachmentService, type) and isinstance(configured, AttachmentService):
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
        """返回当前轮已授权附件的文件名、解析状态与发布状态。"""
        refs = _authorized_refs(rt)
        if not refs:
            return "本轮会话没有挂载任何附件。"
        store = _store(rt)
        session_id = int(getattr(rt, "attachment_session_id", 0) or 0)
        user_id = int(getattr(rt, "attachment_user_id", 0) or 0)
        lines = ["本轮可用附件："]
        for index, ref in enumerate(refs, start=1):
            state = str(getattr(ref, "parse_status", "") or "queued")
            note = str(getattr(ref, "degraded_reason", "") or "")
            suffix = f"（{note}）" if note and state != "ready" else ""
            attachment_id = str(getattr(ref, "attachment_id", "") or "")
            revision_note = ""
            try:
                revision = store.get_attachment_revision_by_child(
                    attachment_id, session_id=session_id, user_id=user_id
                )
            except Exception:  # noqa: BLE001 - list must stay fail-soft
                revision = None
            if revision is not None:
                label = _RELEASE_STATE_LABELS.get(
                    revision.release_state, revision.release_state
                )
                revision_note = (
                    f"；修订候选，发布状态: {label}"
                    f"（revision_id: {revision.revision_id}）"
                )
            lines.append(
                f"{index}. {getattr(ref, 'filename', '')} — 解析状态: {state}"
                f"{suffix}{revision_note}（attachment_id: {attachment_id}）"
            )
        lines.append(
            "使用 attachment_search 检索附件内容，attachment_read 读取指定段落，"
            "attachment_visual_analyze 分析指定 PDF 页面。"
        )
        return "\n".join(lines)

    return attachment_list


def _attachment_retriever(rt: ToolRuntime, retrieval: AttachmentRetrievalService | None):
    retriever = retrieval or getattr(rt, "attachment_retrieval", None)
    return retriever or AttachmentRetrievalService()


def _preview_attachment_candidates(
    rt: ToolRuntime,
    *,
    query: str,
    top_k: int = 6,
    retrieval: AttachmentRetrievalService | None = None,
) -> list[dict[str, Any]]:
    """Return bounded attachment candidates without formal admission.

    This helper is the only prefetch path used before the model's first turn.
    It performs the same ACL re-check and retriever call as
    ``attachment_search`` but never allocates a citation number, never appends
    to ``ToolRuntime.evidence`` and never consumes the formal evidence budget.
    """

    retriever = _attachment_retriever(rt, retrieval)
    refs = _authorized_refs(rt)
    asset_ids = _asset_ids(refs)
    if not asset_ids:
        return []
    result = retriever.search(
        str(query or ""),
        asset_ids=asset_ids,
        limit=max(1, min(int(top_k or 6), 8)),
        # Preview must stay smaller than a formal tool result.  The hard cap
        # is supplied by the caller's settings elsewhere; this keeps prefetch
        # from depending on attachment retrieval defaults.
        context_token_budget=1200,
    )
    candidates: list[dict[str, Any]] = []
    for chunk in result.chunks:
        ref = _ref_for_asset(refs, chunk.asset_id)
        if ref is None:
            continue
        candidates.append(candidate_preview_from_chunk(ref=ref, chunk=chunk))
    for reason in result.degraded_reasons:
        rt.emit("degraded", {"stage": "attachment_prefetch", "reason": reason})
    return candidates


def format_attachment_preview(candidates: list[dict[str, Any]], *, limit_bytes: int = 4000) -> str:
    """Render preview candidates as non-citable untrusted context."""

    if not candidates:
        return ""
    blocks: list[str] = []
    used = 0
    for candidate in candidates:
        text = str(candidate.get("content") or "")[:420]
        locator = candidate.get("locator") or {}
        locator_text = ", ".join(
            f"{key}={value}" for key, value in locator.items() if value not in (None, "", [], {})
        )
        block = (
            f'<attachment_candidate id="{candidate.get("candidate_id", "")}" '
            f'attachment_id="{candidate.get("attachment_id", "")}" '
            f'filename="{candidate.get("filename", "")}">\n'
            f"定位：{locator_text or '未提供'}\n{text}\n</attachment_candidate>"
        )
        encoded = len(block.encode("utf-8"))
        if used + encoded > max(400, int(limit_bytes)):
            break
        blocks.append(block)
        used += encoded
    return "\n\n".join(blocks)


def make_attachment_search(
    rt: ToolRuntime,
    retrieval: AttachmentRetrievalService | None = None,
    *,
    candidate_mode: bool = False,
):
    """在当前轮已授权附件内做本地检索，返回带定位的证据。

    ``candidate_mode`` is a deliberate non-admission mode for server-side
    preflight.  The normal tool (and every explicit model call) keeps the
    historical formal-evidence path.
    """

    retriever = _attachment_retriever(rt, retrieval)

    def attachment_search_preview(query: str, top_k: int = 6) -> str:
        return format_attachment_preview(
            _preview_attachment_candidates(rt, query=query, top_k=top_k, retrieval=retriever)
        )

    def attachment_search(query: str, top_k: int = 6) -> str:
        """按查询词检索当前轮附件内容，并返回带定位的证据。"""
        from src.attachments.models import PARSER_VERSION  # noqa: F401

        if candidate_mode:
            preview = attachment_search_preview(query, top_k)
            return preview or "附件中没有找到可用的候选预览。"
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
            return empty_result_message(
                rt,
                "附件中没有找到相关内容。可尝试型号、位号、网络名或换一种问法。",
                budget_blocked=bool(getattr(items, "budget_blocked", False)),
                items=items,
            )
        return format_tool_result(rt, adds_nothing, items)

    # The runner uses this server-side capability for the no-admission
    # preflight.  Keeping it on the callable preserves the legacy tool shape
    # for prompt construction and tests.
    attachment_search.preview = attachment_search_preview  # type: ignore[attr-defined]
    attachment_search.candidate_mode = candidate_mode  # type: ignore[attr-defined]
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

        state: dict[str, int] = {"total": 0, "readable_count": 0, "readable_max": -1}

        def _run() -> list[Evidence]:
            start = max(0, int(ordinal_from or 0))
            bounded = max(1, min(int(limit or 4), 10))
            evidence: list[Evidence] = []
            total = 0
            readable_count = 0
            readable_max = -1
            for asset_id in asset_ids:
                ref = _ref_for_asset(refs, asset_id)
                if ref is None:
                    continue
                parts = store.list_parts(asset_id)
                total = max(total, len(parts))
                # Image/embedded parts carry no text; reading them would only
                # pollute the evidence budget with empty rows.
                readable = [
                    part for part in parts if str(part.text_content or "").strip()
                ]
                readable_count = max(readable_count, len(readable))
                if readable:
                    readable_max = max(readable_max, int(readable[-1].ordinal))
                selected = [part for part in readable if part.ordinal >= start][:bounded]
                for part in selected:
                    evidence.append(evidence_from_part(ref=ref, part=part))
            state["total"] = total
            state["readable_count"] = readable_count
            state["readable_max"] = readable_max
            return evidence[: bounded * max(1, len(asset_ids))]

        query_text = str(query or f"read from {ordinal_from or 0}")
        items, adds_nothing = timed_tool_call(rt, "attachment_read", query_text, None, _run)
        from src.observability.metrics import record_attachment

        record_attachment("read", status="ok" if items else "empty")
        if not items:
            blocked = empty_result_message(
                rt,
                "",
                budget_blocked=bool(getattr(items, "budget_blocked", False)),
                items=items,
            )
            if blocked:
                return blocked
            start = max(0, int(ordinal_from or 0))
            readable_max = int(state["readable_max"])
            if readable_max < 0:
                return "附件中没有可读取的文本片段。"
            if start > readable_max:
                return (
                    f"读取起始序号 {start} 超出附件可读文本范围：当前附件共 "
                    f"{state['total']} 个解析片段，其中可读文本片段 {state['readable_count']} 个，"
                    f"最大有效序号 {readable_max}。请不要再递增序号翻页；"
                    "改用 attachment_search 按章节标题、型号或关键词定位，"
                    f"或读取 0–{readable_max} 范围内的序号。"
                )
            return (
                f"序号 {start} 起没有可读取的文本片段（当前附件共 {state['total']} 个解析片段，"
                f"可读文本 {state['readable_count']} 个，最大有效序号 {readable_max}）。"
                "可尝试更小的起始序号，或使用 attachment_search 按关键词定位。"
            )
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
                                metadata={"source_type": ATTACHMENT},
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
                                metadata={"source_type": ATTACHMENT},
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
                            metadata={"source_type": ATTACHMENT},
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
                                metadata={"source_type": ATTACHMENT},
                            )
                        )
            return evidence

        items, adds_nothing = timed_tool_call(rt, tool_name, query_text, None, _run)
        if not items:
            return empty_result_message(
                rt,
                "本轮附件中没有可查询的 Excel 表格（仅支持 .xlsx/.xlsm）。",
                budget_blocked=bool(getattr(items, "budget_blocked", False)),
                items=items,
            )
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
                        "source_type": ATTACHMENT,
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
            return empty_result_message(
                rt,
                "附件中没有找到电路设计或相关网络/器件信息。",
                budget_blocked=bool(getattr(items, "budget_blocked", False)),
                items=items,
            )
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
            return empty_result_message(
                rt,
                "未返回视觉分析证据；远程视觉能力可能未启用、页面不可用或供应商失败。",
                budget_blocked=bool(getattr(items, "budget_blocked", False)),
                items=items,
            )
        return format_tool_result(rt, adds_nothing, items)

    return attachment_visual_analyze


def make_attachment_edit(rt: ToolRuntime):
    """Create an immutable candidate from one exact attachment edit.

    The attachment scope is rechecked before the service reads the source,
    and the original attachment is never overwritten.  The response is JSON
    so the governed action adapter can retain the revision, audit details and
    child attachment references.
    """

    def attachment_edit(
        before: str = "",
        after: str = "",
        attachment_id: str = "",
        client_request_id: str = "",
        operation: str = "replace_text",
        heading_path: list[str] | None = None,
        sheet: str = "",
        cell: str = "",
        after_row: int = 0,
        row: int = 0,
        count: int = 1,
        values: list[list[str]] | None = None,
        preserve_embedded: bool = True,
    ) -> str:
        """按精确文本替换、按标题替换整节或按单元格修改，创建附件候选副本，原附件保持不变。

        operation 取值：
        - replace_text（默认）：把唯一出现的 before 文本替换为 after；
          支持 .txt/.md/.docx（DOCX 段落与表格单元格，保留原有格式）。
        - replace_section：把 heading_path 指定标题下的整节正文替换为 after
          （after 中每个换行生成一个段落）；仅 .docx，标题本身与其余章节保持不变。
          新段落自动沿用原标题格式与原正文样式/段落格式（缩进、间距）/首个 run 的
          字体格式（字号、字体、加粗等）；默认 preserve_embedded=true，把原节中的
          表格、图片、嵌入对象按原顺序移到新节末尾而不是丢弃。需要把对象放回指定
          位置时，用单独一行的占位符 [[embed]]（按原顺序取下一个对象）或
          [[embed:N]]（N 为原节内表格/嵌入对象按出现顺序的 1 起编号）；未放置的
          对象仍移到节末。若用户明确要求删除这些对象，才传
          preserve_embedded=false（否则会被拒绝并要求先向用户确认）。
        - append_paragraphs：在 heading_path 指定标题章节的末尾追加 after 段落
          （每个换行一段）；不传 heading_path 则追加到文档末尾。仅 .docx，
          其余内容保持不变；新段落沿用相邻段落的样式与格式。
        - set_cell：把 sheet 工作表的 cell（如 B3）设为 after 文本；仅 .xlsx/.xlsm，
          保留工作簿其余部件与单元格样式。
        - replace_cell_text：在 sheet 内把唯一出现的 before 单元格文本替换为 after；
          仅 .xlsx/.xlsm。
        - insert_rows：在 sheet 的 after_row 行之后插入 count 行（可带 values 数据，
          每行一个字符串数组）；下方行、合并区域与简单 A1 公式同步平移，跨表/INDIRECT/
          OFFSET 公式会拒绝而不是猜测；仅 .xlsx/.xlsm。
        - delete_rows：删除 sheet 第 row 行起的 count 行；引用被删行的公式会拒绝；
          仅 .xlsx/.xlsm。
        - annotate_text：在 PDF 中定位唯一 before 文本并添加替换建议批注；仅 .pdf，
          原件内容不变（不做原位改写）。.edf/.edif 走 replace_text 且必须通过解析校验。
        - reflow_to_docx：把 PDF 文本层重排为可编辑 DOCX 副本（仅保留文本，版式/
          图片/表格不保留，必须人工复核）；扫描件会被明确拒绝。

        目标缺失或重复出现时返回 blocked，不会猜测修改。候选以新附件形式返回，
        可下载复核，不会覆盖原附件。
        """
        def _blocked(payload: dict) -> str:
            # Execution records must show blocked writes, not only successful
            # candidates: the stage event is persisted with the turn.
            rt.emit("stage", {
                "key": "attachment_edit",
                "label": "附件修订",
                "status": "blocked",
                "detail": str(payload.get("message") or ""),
                "error_code": str(payload.get("error_code") or ""),
            })
            return json.dumps({"status": "blocked", **payload}, ensure_ascii=False)

        # Fail closed when the user quoted the exact text to change: the model
        # must not silently broaden the target (for example replacing a whole
        # repeated block when the user only pointed at one occurrence).
        operation_name = str(operation or "replace_text").strip().lower()
        if operation_name in {"replace_text", "replace_cell_text"}:
            question_text = str(getattr(rt, "question", "") or "")
            if _REPLACE_INTENT_PATTERN.search(question_text):
                quoted = [
                    item.strip()
                    for item in _QUOTED_SEGMENT_RE.findall(question_text)
                    if item.strip()
                ]
                if quoted and str(before or "").strip() not in quoted:
                    return _blocked({
                        "error_code": "attachment_target_not_explicit",
                        "message": (
                            "before 必须与用户明确引用的文本一致；"
                            "请直接引用原文，或让用户确认要替换的片段。"
                        ),
                    })

        # Dropping tables/pictures/OLE objects from a section must be an
        # explicit user request: the default is to preserve them, so a model
        # that sets preserve_embedded=false on its own is sent back to ask.
        if operation_name == "replace_section" and not bool(preserve_embedded):
            question_text = str(getattr(rt, "question", "") or "")
            if not _EMBEDDED_REMOVAL_INTENT_PATTERN.search(question_text):
                return _blocked({
                    "error_code": "attachment_embedded_removal_unconfirmed",
                    "message": (
                        "原节可能包含表格、图片或嵌入对象，而用户未明确要求删除它们。"
                        "请保留这些对象（preserve_embedded=true，默认），"
                        "或先向用户确认要删除哪些对象后再调用。"
                    ),
                })

        if not bool(getattr(rt, "attachment_edit_enabled", True)):
            return _blocked({
                "error_code": "attachment_edit_disabled",
                "message": "当前对话未授权文档写入，不能修改附件。",
            })
        refs = _authorized_refs(rt)
        if not refs:
            return _blocked({
                "error_code": "attachment_scope_denied",
                "message": "本轮没有可用附件。",
            })
        normalized_id, _source_revision, resolve_error = _resolve_revision_target(
            rt, refs, attachment_id
        )
        if resolve_error is not None or not normalized_id:
            return _blocked(resolve_error or {
                "error_code": "attachment_target_unresolved",
                "message": "无法确定要修改的附件版本。",
            })
        session_id = int(getattr(rt, "attachment_session_id", 0) or 0)
        user_id = int(getattr(rt, "attachment_user_id", 0) or 0)
        attempt = begin_attachment_audit(
            action="attachment.revision_create",
            target_type="attachment_revision",
            target_id=normalized_id,
            actor_user_id=user_id,
            kb_name=str(getattr(rt, "kb_name", "") or ""),
            metadata={
                "session_id": session_id,
                "parent_attachment_id": normalized_id,
                "operation": str(operation or "replace_text")[:100],
            },
        )
        try:
            revision = _service(rt).create_attachment_revision(
                parent_attachment_id=normalized_id,
                session_id=session_id,
                user_id=user_id,
                before=before,
                after=after,
                client_request_id=client_request_id,
                operation=operation,
                heading_path=heading_path,
                sheet=sheet,
                cell=cell,
                after_row=after_row,
                row=row,
                count=count,
                values=values,
                preserve_embedded=bool(preserve_embedded),
            )
        except (AttachmentError, ValueError) as exc:
            finish_attachment_audit(
                attempt,
                success=False,
                error_message=str(exc),
                metadata={
                    "session_id": session_id,
                    "parent_attachment_id": normalized_id,
                    "operation": str(operation or "replace_text")[:100],
                },
            )
            return _blocked({
                "error_code": "attachment_revision_rejected",
                "message": str(exc)[:500],
            })
        change_summary = revision_change_summary(revision)
        finish_attachment_audit(
            attempt,
            success=True,
            target_id=revision.revision_id,
            metadata={
                "session_id": session_id,
                "parent_attachment_id": revision.parent_attachment_id,
                "child_attachment_id": revision.child_attachment_id,
                "operation": revision.operation,
                "change_summary": change_summary,
            },
        )
        if revision.operation == "replace_section":
            message = (
                f"已替换章节正文并创建候选（变更：{change_summary}）；"
                "标题及未指定的其他章节保持不变，新正文沿用原节样式与字体格式，"
                "可下载候选文件复核。"
            )
        elif revision.operation == "annotate_text":
            message = (
                f"PDF 原件内容未修改，已生成批注候选（变更：{change_summary}）；"
                "可下载候选按批注复核建议替换。"
            )
        else:
            message = (
                f"已创建附件修订候选（变更：{change_summary}）；"
                "原附件未修改，可下载候选文件复核。"
            )
        download_url = (
            f"/api/v1/conversations/{session_id}/attachments/"
            f"{revision.child_attachment_id}/download"
        )
        payload = {
            "status": "completed",
            "revision_id": revision.revision_id,
            "parent_attachment_id": revision.parent_attachment_id,
            "child_attachment_id": revision.child_attachment_id,
            "parent_content_hash": revision.parent_content_hash,
            "child_content_hash": revision.child_content_hash,
            "operation": revision.operation,
            "stage": revision.status,
            "release_state": revision.release_state,
            "change_summary": change_summary,
            "details": dict(revision.details or {}),
            "download_url": download_url,
            "message": message,
        }
        rt.emit("attachment_revision", {
            key: value for key, value in payload.items()
            if key not in {"message"}
        })
        # Reuse the durable document-card channel so the candidate and its
        # download URL are visible in the chat, not only in the model context.
        rt.emit("document_card", {
            "card": {
                "kind": "attachment_revision",
                "status": revision.status,
                "next_actions": [],
                "kb_name": str(getattr(rt, "kb_name", "") or ""),
                "content": message,
                "candidate_state": "available",
                "release_state": revision.release_state,
                "artifacts": [{
                    "artifact_id": revision.child_attachment_id,
                    "stage": revision.status,
                    "output_format": str((revision.details or {}).get("format") or ""),
                    "download_url": download_url,
                }],
            },
        })
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    return attachment_edit


def make_attachment_release(rt: ToolRuntime):
    """Request formal release review for an attachment revision candidate.

    The request never releases anything by itself: the candidate stays
    immutable and ``release_state`` only moves to ``pending`` until an
    authorized reviewer approves or rejects it.
    """

    def request_attachment_release(
        revision_id: str = "",
        attachment_id: str = "",
        comment: str = "",
    ) -> str:
        """申请附件修订候选的正式发布审核；候选与正式发布严格分离，不会直接发布。

        revision_id 或 attachment_id 至少给出一个：attachment_id 为候选附件的 id
        （attachment_edit 返回的 child_attachment_id）。未指定时自动选择本轮附件中
        最新的修订候选。
        """
        refs = _authorized_refs(rt)
        if not refs:
            return json.dumps({
                "status": "blocked",
                "error_code": "attachment_scope_denied",
                "message": "本轮没有可用附件。",
            }, ensure_ascii=False)
        service = _service(rt)
        session_id = int(getattr(rt, "attachment_session_id", 0) or 0)
        user_id = int(getattr(rt, "attachment_user_id", 0) or 0)

        requested = str(revision_id or "").strip() or str(attachment_id or "").strip()
        _child, target, resolve_error = _resolve_revision_target(rt, refs, requested)
        if target is None:
            return json.dumps({
                "status": "blocked",
                **(resolve_error or {
                    "error_code": "attachment_revision_target_unresolved",
                    "message": (
                        "没有找到可申请发布的附件修订候选；"
                        "请先通过 attachment_edit 生成候选，或在对话中指明候选附件。"
                    ),
                }),
            }, ensure_ascii=False)

        attempt = begin_attachment_audit(
            action="attachment.release_request",
            target_type="attachment_revision",
            target_id=target.revision_id,
            actor_user_id=user_id,
            kb_name=str(getattr(rt, "kb_name", "") or ""),
            metadata={
                "session_id": session_id,
                "source": "chat_tool",
                "comment_length": len(comment or ""),
            },
        )
        try:
            revision = service.request_attachment_release(
                revision_id=target.revision_id,
                session_id=session_id,
                user_id=user_id,
                comment=comment,
            )
        except (AttachmentError, ValueError) as exc:
            finish_attachment_audit(
                attempt,
                success=False,
                error_message=str(exc),
                metadata={
                    "session_id": session_id,
                    "source": "chat_tool",
                    "comment_length": len(comment or ""),
                },
            )
            return json.dumps({
                "status": "blocked",
                "error_code": "attachment_release_rejected",
                "message": str(exc)[:500],
            }, ensure_ascii=False)

        finish_attachment_audit(
            attempt,
            success=True,
            target_id=revision.revision_id,
            metadata={
                "session_id": session_id,
                "source": "chat_tool",
                "operation": revision.operation,
                "release_state": revision.release_state,
                "comment_length": len(comment or ""),
            },
        )

        session_id_for_url = int(getattr(rt, "attachment_session_id", 0) or 0)
        download_url = (
            f"/api/v1/conversations/{session_id_for_url}/attachments/"
            f"{revision.child_attachment_id}/download"
        )
        message = (
            "已提交正式发布审核申请，等待授权审核人处理；"
            "候选附件保持不变，审核通过后会生成独立的正式文件。"
        )
        payload = {
            "status": "completed",
            "revision_id": revision.revision_id,
            "parent_attachment_id": revision.parent_attachment_id,
            "child_attachment_id": revision.child_attachment_id,
            "operation": revision.operation,
            "stage": revision.status,
            "release_state": revision.release_state,
            "release_comment": revision.release_comment,
            "download_url": download_url,
            "message": message,
        }
        rt.emit("attachment_release", {
            key: value for key, value in payload.items() if key != "message"
        })
        rt.emit("document_card", {
            "card": {
                "kind": "attachment_revision",
                "status": revision.status,
                "next_actions": [],
                "kb_name": str(getattr(rt, "kb_name", "") or ""),
                "content": message,
                "candidate_state": "available",
                "release_state": revision.release_state,
                "artifacts": [{
                    "artifact_id": revision.child_attachment_id,
                    "stage": revision.status,
                    "output_format": str((revision.details or {}).get("format") or ""),
                    "download_url": download_url,
                }],
            },
        })
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    return request_attachment_release


def make_attachment_revision_history(rt: ToolRuntime):
    """Expose the attachment revision lineage, change summary and release state."""

    def attachment_revision_history(attachment_id: str = "") -> str:
        """查看附件修订版本链：操作、变更摘要、发布状态与正式文件。

        attachment_id 可传候选附件 id 或修订 id（attachment-revision-...）；
        不传时汇总本轮所有附件相关的修订记录。
        """
        refs = _authorized_refs(rt)
        if not refs:
            return "本轮没有可用附件。"
        service = _service(rt)
        session_id = int(getattr(rt, "attachment_session_id", 0) or 0)
        user_id = int(getattr(rt, "attachment_user_id", 0) or 0)
        authorized_ids = [
            str(getattr(ref, "attachment_id", "") or "").strip() for ref in refs
        ]
        normalized = str(attachment_id or "").strip()
        scoped_ids = authorized_ids
        if normalized.startswith(_REVISION_ID_PREFIX):
            revision = service.get_attachment_revision(
                revision_id=normalized, session_id=session_id, user_id=user_id
            )
            if revision is None or revision.child_attachment_id not in authorized_ids:
                return "该修订版本不在本轮已授权范围内。"
            scoped_ids = [revision.child_attachment_id]
        elif normalized:
            if normalized not in authorized_ids:
                return "该附件不在本轮已授权范围内。"
            scoped_ids = [normalized]

        revisions = []
        seen: set[str] = set()
        for candidate in scoped_ids:
            revision = service.get_attachment_revision_by_child(
                child_attachment_id=candidate,
                session_id=session_id,
                user_id=user_id,
            )
            if revision is None or revision.revision_id in seen:
                continue
            seen.add(revision.revision_id)
            revisions.append(revision)
        if not revisions:
            return "附件的修订历史为空：当前没有可用的修订候选。"

        def _name(attachment_id_value: str) -> str:
            try:
                return service.get_attachment(
                    attachment_id=attachment_id_value,
                    session_id=session_id,
                    user_id=user_id,
                ).filename
            except Exception:  # noqa: BLE001 - history must stay fail-soft
                return attachment_id_value

        revisions.sort(
            key=lambda item: (str(item.created_at or ""), item.revision_id),
            reverse=True,
        )
        lines = [f"附件修订历史（{len(revisions)} 条，倒序）："]
        for index, revision in enumerate(revisions, start=1):
            label = _RELEASE_STATE_LABELS.get(
                revision.release_state, revision.release_state
            )
            lines.append(
                f"{index}. {revision.revision_id} | 操作: {revision.operation} | "
                f"候选: {_name(revision.child_attachment_id)} | "
                f"基于: {_name(revision.parent_attachment_id)} | "
                f"发布状态: {label} | 变更: {revision_change_summary(revision)} | "
                f"{revision.created_at}"
            )
            if revision.released_attachment_id:
                lines.append(
                    f"   正式文件: {_name(revision.released_attachment_id)}"
                )
            if revision.review_comment:
                lines.append(f"   审核意见: {revision.review_comment}")
        return "\n".join(lines)

    return attachment_revision_history


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
        if normalize_source_type(item.metadata.get("source_type")) != ATTACHMENT:
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
    tools = [
        make_attachment_list(rt),
        make_attachment_search(rt),
        make_attachment_read(rt),
        make_attachment_table_query(rt),
        make_attachment_circuit_search(rt),
        make_attachment_visual_analyze(rt),
    ]
    if bool(getattr(rt, "attachment_edit_enabled", True)):
        tools.append(make_attachment_edit(rt))
    tools.append(make_attachment_release(rt))
    tools.append(make_attachment_revision_history(rt))
    return tools
