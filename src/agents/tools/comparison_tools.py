"""Agent tool for whole-document comparison.

The model selects two already-authorized sources by identity.  The backend
freezes both source versions, parses them outside the model context, computes
the diff and admits only changed fragments as formal evidence.
"""

from __future__ import annotations

import json
from typing import Any

from src.agents.tools.runtime import (
    ToolRuntime,
    empty_result_message,
    format_tool_result,
    timed_tool_call,
)
from src.services.document_comparison import (
    ComparisonError,
    ComparisonPage,
    _focus_matches,
    _focus_terms,
    canonicalize_archive_record,
    canonicalize_attachment,
    compare_documents,
    render_comparison_page,
)
from src.services.document_comparison_models import comparison_report_from_dict
from src.services.comparison_scope import normalize_section_key
from src.services.task_planning import scope_from_query
from src.services.task_runtime import record_comparison_page
from src.storage.document_comparison_store import DocumentComparisonStore


def _owner(rt: ToolRuntime) -> dict[str, Any]:
    return {
        "user_id": getattr(rt, "attachment_user_id", None),
        "session_id": getattr(rt, "attachment_session_id", None),
        "chat_session_id": str(getattr(rt, "chat_session_id", "") or ""),
    }


def _permissions(rt: ToolRuntime) -> dict[str, Any]:
    return {
        "source_scope": str(getattr(rt, "source_scope", "") or ""),
        "attachment_refs": len(getattr(rt, "attachment_refs", []) or []),
        "knowledge_scope": (
            dict(getattr(rt.knowledge_scope, "model_dump", lambda: {})())
            if getattr(rt, "knowledge_scope", None) is not None else {}
        ),
    }


def _resolve_attachment_ref(rt: ToolRuntime, value: str):
    from src.agents.tools.attachment_tools import _authorized_refs

    refs = _authorized_refs(rt)
    normalized = str(value or "").strip()
    if normalized.startswith("attachment:"):
        normalized = normalized.split(":", 1)[1]
    for ref in refs:
        if normalized in {
            str(getattr(ref, "attachment_id", "") or ""),
            str(getattr(ref, "asset_id", "") or ""),
            str(getattr(ref, "filename", "") or ""),
        }:
            return ref
    return None


def _name_matches(hint: str, *names: Any) -> bool:
    target = str(hint or "").strip().casefold()
    if not target:
        return False
    for name in names:
        candidate = str(name or "").strip().casefold()
        if not candidate:
            continue
        if target in candidate or candidate in target:
            return True
        stem = candidate.rsplit(".", 1)[0]
        if target == stem or target in stem:
            return True
    return False


def _resolve_kb_record(rt: ToolRuntime, value: str, *, hint: str = ""):
    from src.pipelines.document_store import PipelineDocumentStore

    normalized = str(value or "").strip()
    if normalized.startswith("kb:"):
        normalized = normalized.split(":", 1)[1].strip()
    scope = getattr(rt, "knowledge_scope", None)
    store = getattr(rt, "document_store", None)
    if scope is None or not isinstance(store, PipelineDocumentStore):
        raise ComparisonError("source_unavailable", "knowledge base comparison is not configured")
    metadata = dict(getattr(getattr(rt, "ctx", None), "metadata", {}) or {})
    default_department = str(
        metadata.get("resource_department_id") or metadata.get("department_id") or ""
    )
    allowed = {int(source.kb_id) for source in scope.sources}
    try:
        record_id = int(normalized)
    except (TypeError, ValueError):
        record_id = 0
    if record_id:
        record = None
        departments = [
            str(getattr(source, "department_id", "") or default_department)
            for source in scope.sources
        ]
        for department_id in dict.fromkeys(item for item in departments if item):
            record = store.get_document_by_id_scoped(record_id, department_id)
            if record is not None:
                break
        if record is None:
            raise ComparisonError("source_acl_denied", "record is not available in this scope")
        if int(getattr(record, "kb_id", 0) or 0) not in allowed:
            raise ComparisonError("source_acl_denied", "record is outside the selected KB scope")
        return record

    candidates = []
    for source in scope.sources:
        department_id = str(getattr(source, "department_id", "") or default_department)
        if not department_id:
            continue
        try:
            records = store.list_documents(source.kb_name, department_id)
        except Exception:  # noqa: BLE001 - one source must not hide the others
            continue
        candidates.extend(
            record for record in records
            if int(getattr(record, "kb_id", 0) or 0) in allowed
        )
    if not candidates:
        raise ComparisonError("source_unavailable", "no readable document in the selected KB scope")
    matches = [
        record for record in candidates
        if _name_matches(normalized, getattr(record, "document_name", ""),
                         getattr(record, "original_file_name", ""))
    ]
    if not matches and not normalized:
        matches = candidates
    if not matches and hint:
        matches = [
            record for record in candidates
            if _name_matches(hint, getattr(record, "document_name", ""),
                             getattr(record, "original_file_name", ""))
        ]
    unique = {int(getattr(record, "id", 0) or 0): record for record in matches}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if not unique:
        raise ComparisonError(
            "source_not_found",
            f"no KB document matches {value or hint!r}; pass kb:<record_id> explicitly",
        )
    names = "、".join(str(getattr(record, "document_name", "")) for record in list(unique.values())[:5])
    raise ComparisonError(
        "ambiguous_source_reference",
        f"multiple KB documents match {value or hint!r}: {names}; pass kb:<record_id>",
    )


def _attachment_document(rt: ToolRuntime, value: str):
    ref = _resolve_attachment_ref(rt, value)
    if ref is None:
        raise ComparisonError("source_acl_denied", f"attachment is not authorized: {value}")
    store = getattr(rt, "attachment_service", None)
    if store is None:
        from src.attachments.service import AttachmentService

        store = AttachmentService()
    attachment_store = getattr(store, "store", store)
    parts = attachment_store.list_parts(str(getattr(ref, "asset_id", "") or ""))
    return canonicalize_attachment(
        ref,
        parts,
        owner=_owner(rt),
        permissions=_permissions(rt),
    )


def _archive_document(rt: ToolRuntime, value: str, *, hint: str = ""):
    from src.services.document_archive import DocumentArchiveManager

    record = _resolve_kb_record(rt, value, hint=hint)
    archive = DocumentArchiveManager()
    path = archive.resolve_record_path(record)
    return canonicalize_archive_record(
        record,
        path,
        owner=_owner(rt),
        permissions=_permissions(rt),
    )


def _auto_attachment_ref(rt: ToolRuntime, hint: str = ""):
    from src.agents.tools.attachment_tools import _authorized_refs

    refs = _authorized_refs(rt)
    if len(refs) == 1:
        return refs[0]
    matches = [
        ref for ref in refs
        if _name_matches(hint, getattr(ref, "filename", ""))
    ]
    unique = {
        str(getattr(ref, "attachment_id", "")): ref for ref in matches
    }
    if len(unique) == 1:
        return next(iter(unique.values()))
    return None


def _canonical_source(
    rt: ToolRuntime,
    value: str,
    *,
    hint: str = "",
    prefer: str = "attachment",
):
    normalized = str(value or "").strip()
    if normalized in {"attachment:auto", "auto"}:
        normalized = ""
    if not normalized:
        if prefer == "knowledge_base":
            return _archive_document(rt, "", hint=hint)
        ref = _auto_attachment_ref(rt, hint)
        if ref is None:
            raise ComparisonError(
                "ambiguous_source_reference",
                "multiple attachments match; pass attachment:<attachment_id> explicitly",
            )
        return _attachment_document(rt, str(getattr(ref, "attachment_id", "")))
    if normalized.startswith("attachment:") or _resolve_attachment_ref(rt, normalized) is not None:
        return _attachment_document(rt, normalized)
    return _archive_document(rt, normalized, hint=hint)


def _known_comparison_report_ids(rt: ToolRuntime) -> list[str]:
    """Return frozen comparison ids already admitted in this turn.

    A chapter follow-up is a read of an immutable report.  Reconstructing the
    two sources just to apply a focus can race with parsing/index updates and
    needlessly hit the report idempotency guard.  The evidence registry is the
    request-scoped record of reports that have already passed ACL checks.
    """

    report_ids: list[str] = []
    for item in reversed(list(getattr(rt, "evidence", []) or [])):
        metadata = dict(getattr(item, "metadata", {}) or {})
        if metadata.get("evidence_role") != "comparison":
            continue
        report_id = str(
            metadata.get("comparison_report_id")
            or metadata.get("source_id")
            or ""
        ).strip()
        if report_id and report_id not in report_ids:
            report_ids.append(report_id)
    return report_ids


def _split_values(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value or "").strip()
    if not text:
        return ()
    return tuple(
        item.strip() for item in text.replace("，", ",").replace("；", ",").split(",")
        if item.strip()
    )


def _scope_filters(focus: str) -> dict[str, tuple[str, ...]]:
    """Turn a user-facing focus into exact chapter/section/table filters."""

    scope = scope_from_query(focus, task_kind="compare")
    if scope.scope_kind == "chapter_detail":
        if scope.section_keys:
            return {"section_keys": scope.section_keys}
        if scope.chapter_keys:
            return {"chapter_keys": scope.chapter_keys}
    if scope.scope_kind == "item_detail" and scope.table_keys:
        return {"table_keys": scope.table_keys}
    return {}


def _scope_only_focus(focus: str) -> bool:
    """Whether a focus is a delivery-scope phrase rather than a term filter."""

    scope = scope_from_query(focus, task_kind="compare")
    return scope.scope_kind in {"chapter_summary", "full_report"}


def _load_frozen_page(
    rt: ToolRuntime,
    *,
    report_id: str,
    focus: str = "",
    chapter_keys: tuple[str, ...] = (),
    section_keys: tuple[str, ...] = (),
    item_ids: tuple[str, ...] = (),
    table_keys: tuple[str, ...] = (),
    offset: int = 0,
    limit: int = 0,
    cursor: str = "",
) -> tuple[Any, ComparisonPage]:
    """Load one frozen report and render an exact, bounded page."""

    store = DocumentComparisonStore()
    payload = store.get(
        str(report_id),
        owner_user_id=getattr(rt, "attachment_user_id", None),
    )
    report = comparison_report_from_dict(payload)
    if not (chapter_keys or section_keys or item_ids or table_keys):
        filters = _scope_filters(focus)
        chapter_keys = filters.get("chapter_keys", ())
        section_keys = filters.get("section_keys", ())
        table_keys = filters.get("table_keys", ())
    page_focus = "" if (
        chapter_keys or section_keys or item_ids or table_keys or _scope_only_focus(focus)
    ) else focus
    page = render_comparison_page(
        report,
        chapter_keys=chapter_keys,
        section_keys=section_keys,
        item_ids=item_ids,
        table_keys=table_keys,
        offset=offset,
        limit=limit,
        cursor=cursor,
        focus=page_focus,
    )
    return report, page


def _page_note(page: ComparisonPage) -> str:
    parts = [
        f"report_id={page.report_id}",
        f"当前展示 {page.rendered_count}/{page.total_count} 项",
    ]
    if page.remaining_count:
        parts.append(f"剩余 {page.remaining_count} 项")
    if page.next_cursor:
        parts.append(f"next_cursor={page.next_cursor}")
    if page.truncated:
        parts.append("本轮未展开全部变化项")
    if page.warnings:
        parts.append("警告：" + "；".join(page.warnings[:5]))
    return "比较分页：" + "；".join(parts) + "。"


def comparison_prompt() -> str:
    return (
        "\n\n## 整篇比较\n"
        "本轮是整篇文档比较。必须调用 document_compare，"
        "left/right 优先使用 attachment:<attachment_id> 与 kb:<record_id>；"
        "每侧只有一个可判定来源时可省略身份，也可传 kb:<文件名>。"
        "后端会冻结两侧版本、解析并对齐章节/表格，只返回变化片段；"
        "如果用户问的是某一章/某一节/某个表格，必须用 comparison_expand，"
        "传 report_id 和 chapter_keys（如 num:4）、section_keys（如 num:4.2）或 table_keys；"
        "只有 report_id 不存在时才重新调用 document_compare；"
        "分页结果必须保留 next_cursor 和 remaining_count，不能把当前页说成全部差异；"
        "逐项正文/差异必须保留对应引用编号；"
        "不要把整篇附件或整篇知识库文档当作普通检索结果自行比较。"
        "任一侧解析失败、版本变化、权限不足或预算阻塞时，"
        "必须明确说明该侧未完成，禁止报告为“无差异”或完整比较结论。"
    )


def make_document_comparison_tool(rt: ToolRuntime):
    """Compare two authorized sources and return only changed evidence."""

    def document_compare(
        left: str = "",
        right: str = "",
        focus: str = "",
        include_unchanged: bool = False,
        report_id: str = "",
        chapter_keys: str = "",
        section_keys: str = "",
        item_ids: str = "",
        table_keys: str = "",
        offset: int = 0,
        limit: int = 0,
        cursor: str = "",
    ) -> str:
        """整篇比较两个已授权来源；left/right 使用 attachment:<id> 或 kb:<record_id 或文件名>。

        每侧只有一个可判定来源时可省略 left/right。后端完整解析并冻结两个版本，
        模型只接收变化片段。解析失败、版本变化或权限不足会明确返回错误，
        不会报告成“无差异”。章节展开时应复用已有 ``report_id``，传
        chapter_keys="num:4" 或 section_keys="num:4.2"；只读取冻结报告，
        不要为同一份报告重新发起整篇比较。
        """

        state: dict[str, Any] = {"error": None, "report": None, "summary": "", "page": None}
        hint = str(focus or getattr(rt, "question", "") or "").strip()
        chapter_filter = _split_values(chapter_keys)
        section_filter = _split_values(section_keys)
        item_filter = _split_values(item_ids)
        table_filter = _split_values(table_keys)

        def _run():
            try:
                frozen_report_id = str(report_id or "").strip()
                if not frozen_report_id and focus:
                    known_ids = _known_comparison_report_ids(rt)
                    frozen_report_id = known_ids[0] if known_ids else ""
                if frozen_report_id:
                    report, page = _load_frozen_page(
                        rt,
                        report_id=frozen_report_id,
                        focus=focus,
                        chapter_keys=tuple(normalize_section_key(v) for v in chapter_filter),
                        section_keys=tuple(normalize_section_key(v) for v in section_filter),
                        item_ids=item_filter,
                        table_keys=table_filter,
                        offset=offset,
                        limit=limit,
                        cursor=cursor,
                    )
                    state["report"] = report
                    state["page"] = page
                    state["summary"] = report.summary_text()
                    return list(page.evidence)
                left_doc = _canonical_source(rt, left, hint=hint, prefer="attachment")
                right_doc = _canonical_source(
                    rt, right, hint=hint, prefer="knowledge_base",
                )
                if left_doc.snapshot.source_id == right_doc.snapshot.source_id:
                    raise ComparisonError(
                        "same_source", "left and right resolve to the same source identity",
                    )
                report = compare_documents(left_doc, right_doc)
                store = DocumentComparisonStore()
                owner_user_id = getattr(rt, "attachment_user_id", None)
                try:
                    stored = store.save(report, owner_user_id=owner_user_id)
                except ComparisonError as exc:
                    if exc.error_code != "comparison_report_conflict":
                        raise
                    stored = store.find_matching(report, owner_user_id=owner_user_id)
                    if stored is None:
                        raise
                # Always render the authoritative stored report: a repeated
                # run may have produced a new in-memory object while the store
                # correctly returned the existing frozen report.
                report = comparison_report_from_dict(stored)
                state["report"] = report
                state["summary"] = report.summary_text()
                filters = _scope_filters(focus) if not (
                    chapter_filter or section_filter or item_filter or table_filter
                ) else {}
                page = render_comparison_page(
                    report,
                    chapter_keys=tuple(normalize_section_key(v) for v in chapter_filter)
                    or filters.get("chapter_keys", ()),
                    section_keys=tuple(normalize_section_key(v) for v in section_filter)
                    or filters.get("section_keys", ()),
                    item_ids=item_filter,
                    table_keys=table_filter or filters.get("table_keys", ()),
                    offset=offset,
                    limit=limit,
                    cursor=cursor,
                    focus="" if (
                        chapter_filter or section_filter or item_filter or table_filter
                        or filters or _scope_only_focus(focus)
                    ) else focus,
                )
                state["page"] = page
                return list(page.evidence)
            except ComparisonError as exc:
                state["error"] = exc
                return []
            except Exception as exc:  # noqa: BLE001 - typed failure at tool boundary
                state["error"] = ComparisonError(type(exc).__name__, str(exc)[:300])
                return []

        query_text = (
            f"compare {left} with {right} {focus} "
            f"{chapter_filter}|{section_filter}|{item_filter}|{table_filter}|{offset}|{cursor}"
        ).strip()
        items, adds_nothing = timed_tool_call(rt, "document_compare", query_text, None, _run)
        error = state.get("error")
        if isinstance(error, ComparisonError):
            return json.dumps(
                {
                    "status": error.status,
                    "error_code": error.error_code,
                    "message": (
                        "整篇比较未完成，不能据此判断两份文档没有差异。"
                        f"原因：{error.detail or error.error_code}"
                    ),
                },
                ensure_ascii=False,
            )
        report = state.get("report")
        page = state.get("page")
        if not items:
            blocked = empty_result_message(
                rt,
                "",
                budget_blocked=bool(getattr(items, "budget_blocked", False)),
                items=items,
            )
            if report is None:
                return blocked or "整篇比较未产生可引用结果。"
            if page is not None and not page.scope_found:
                note = (
                    _page_note(page)
                    + "\n未匹配到指定章节/节/表格。可用章节键："
                    + "；".join(page.available_keys[:40])[:1600]
                    + "。请使用这些键重新展开，不能据此判断整篇无差异。"
                )
            else:
                note = _report_completion_note(report, page=page)
            if focus and report.changed_items():
                terms = _focus_terms(focus)
                unchanged_hits = [
                    item for item in report.items
                    if item.kind == "unchanged" and _focus_matches(item, terms)
                ]
                if unchanged_hits:
                    note += (
                        f"\n\n本轮焦点“{focus}”匹配的章节在报告中均未变化："
                        + "；".join(item.section_key for item in unchanged_hits[:8])
                        + "。这不代表整篇无差异。"
                    )
                else:
                    keys = "；".join(
                        dict.fromkeys(item.section_key for item in report.changed_items())
                    )
                    note += (
                        f"\n\n本轮焦点“{focus}”没有匹配到变化项。"
                        f"可用变化章节键：{keys[:1200]}。"
                        "请从这些章节键中选择一个重新调用，不要重复整篇比较。"
                    )
            if blocked:
                note += "\n\n" + blocked
            return note
        rendered = format_tool_result(rt, adds_nothing, items)
        if report is not None:
            if page is not None:
                record_comparison_page(rt, report, page, admitted_items=items)
            rendered += "\n\n" + _report_completion_note(report, page=page)
        return rendered

    return document_compare


def make_comparison_expand_tool(rt: ToolRuntime):
    """Read one exact, paged slice of an already frozen comparison report."""

    def comparison_expand(
        report_id: str = "",
        focus: str = "",
        chapter_keys: str = "",
        section_keys: str = "",
        item_ids: str = "",
        table_keys: str = "",
        offset: int = 0,
        limit: int = 0,
        cursor: str = "",
    ) -> str:
        """按章节/节/表格分页读取冻结比较报告，不重新比较整篇文档。

        report_id 必须来自此前的 document_compare；chapter_keys 示例
        "num:4" 或 "4"；section_keys 示例 "num:4.2"；table_keys 示例
        "table-10"。返回当前页、剩余项和 next_cursor。
        """

        state: dict[str, Any] = {"error": None, "report": None, "page": None}
        chapter_filter = tuple(normalize_section_key(v) for v in _split_values(chapter_keys))
        section_filter = tuple(normalize_section_key(v) for v in _split_values(section_keys))
        item_filter = _split_values(item_ids)
        table_filter = _split_values(table_keys)

        def _run():
            try:
                if not str(report_id or "").strip():
                    raise ComparisonError(
                        "comparison_report_required",
                        "comparison_expand requires report_id from document_compare",
                    )
                report, page = _load_frozen_page(
                    rt,
                    report_id=str(report_id),
                    focus=focus,
                    chapter_keys=chapter_filter,
                    section_keys=section_filter,
                    item_ids=item_filter,
                    table_keys=table_filter,
                    offset=offset,
                    limit=limit,
                    cursor=cursor,
                )
                state["report"] = report
                state["page"] = page
                return list(page.evidence)
            except ComparisonError as exc:
                state["error"] = exc
                return []
            except Exception as exc:  # noqa: BLE001 - typed failure at tool boundary
                state["error"] = ComparisonError(type(exc).__name__, str(exc)[:300])
                return []

        query_text = (
            f"expand {report_id} {focus} {chapter_filter}|{section_filter}|"
            f"{item_filter}|{table_filter}|{offset}|{cursor}"
        ).strip()
        items, adds_nothing = timed_tool_call(rt, "comparison_expand", query_text, None, _run)
        error = state.get("error")
        if isinstance(error, ComparisonError):
            return json.dumps(
                {
                    "status": error.status,
                    "error_code": error.error_code,
                    "message": (
                        "比较章节展开未完成，不能据此判断该范围没有差异。"
                        f"原因：{error.detail or error.error_code}"
                    ),
                },
                ensure_ascii=False,
            )
        page = state.get("page")
        report = state.get("report")
        if page is None:
            blocked = empty_result_message(
                rt,
                "",
                budget_blocked=bool(getattr(items, "budget_blocked", False)),
                items=items,
            )
            return blocked or "比较章节展开未产生可引用结果。"
        if not items:
            note = _page_note(page)
            if not page.scope_found:
                note += (
                    "\n未匹配到指定章节/节/表格。可用章节键："
                    + "；".join(page.available_keys[:40])[:1600]
                    + "。请使用这些键重新展开，不要重新整篇比较。"
                )
            elif page.total_count == 0:
                note += "\n该范围在冻结报告中没有可解析变化项。"
            elif page.remaining_count:
                note += "\n本页没有新增证据；请使用 next_cursor 或调整范围。"
            blocked = empty_result_message(
                rt,
                "",
                budget_blocked=bool(getattr(items, "budget_blocked", False)),
                items=items,
            )
            if blocked:
                note += "\n\n" + blocked
            return note
        rendered = format_tool_result(rt, adds_nothing, items)
        if report is not None:
            record_comparison_page(rt, report, page, admitted_items=items)
            rendered += "\n\n" + _report_completion_note(report, page=page)
        return rendered

    return comparison_expand


def _report_completion_note(report, page: ComparisonPage | None = None) -> str:
    """Describe a frozen report as completed, degraded or identical.

    Warnings about unsupported embedded legacy objects do not mean the parsed
    comparison failed.  The model must not turn them into "no evidence".
    """

    warnings = [str(item) for item in report.warnings if str(item)]
    warning_note = ""
    if warnings:
        warning_note = "；未索引项：" + "；".join(warnings[:5])
    counts = json.dumps(report.counts, ensure_ascii=False)
    display_status = "completed" if report.status == "complete" else "completed_with_warnings"
    if (
        report.left.content_hash
        and report.left.content_hash == report.right.content_hash
    ):
        verdict = (
            "两侧文件内容哈希相同，整篇未发现差异（无需逐章推断差异）"
            + warning_note
        )
    elif not report.changed_items():
        verdict = "整篇比较已完成，可解析范围内未发现差异" + warning_note
    else:
        total_changed = len(report.changed_items())
        if page is not None and page.remaining_count:
            verdict = (
                f"比较报告已完成，共发现 {total_changed} 项变化；"
                f"当前分页展示 {page.rendered_count} 项，"
                f"剩余 {page.remaining_count} 项未在本次回答展开"
                + warning_note
            )
        else:
            verdict = f"比较报告已完成，共发现 {total_changed} 项变化" + warning_note
    changed = report.changed_items()
    index_lines = [
        f"- {position}. {item.kind} | {item.section_key} | {item.summary}"
        for position, item in enumerate(changed[:40], start=1)
    ]
    index_note = (
        "\n\n变化索引（报告事实，可用于按章节展开；详细正文按章节键分页读取）：\n"
        + "\n".join(index_lines)
        if index_lines else ""
    )
    page_note = ("\n\n" + _page_note(page)) if page is not None else ""
    scope_hint = (
        "当前回答只展示该范围的一个分页，剩余项必须继续分页，不能声称已逐条交付全部差异。"
        if page is not None and page.remaining_count
        else "未变化章节未注入上下文；如需查看特定章节可要求按章节展开。"
    )
    return (
        f"比较报告已冻结：report_id={report.report_id}；"
        f"状态={display_status}；变化统计={counts}。\n"
        f"结论依据：{verdict}。"
        f"{scope_hint}"
        "这是后端基于冻结版本和哈希生成的报告，不得改写成“未完成”或“无证据”。"
        + index_note
        + page_note
    )


__all__ = ["make_comparison_expand_tool", "make_document_comparison_tool"]
