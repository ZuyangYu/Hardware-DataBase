"""Build an export envelope from the canonical completed chat turn."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.core.conversation import ChatTurn, GENERAL_CHAT_KB_NAME
from src.result_exports.models import ResultEnvelope


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return str(value)
    return str(value)


def _table_from_candidate(name: str, candidate: Any) -> dict[str, Any] | None:
    if isinstance(candidate, dict):
        if "tables" in candidate and isinstance(candidate["tables"], list):
            return None
        rows = candidate.get("rows")
        columns = candidate.get("columns") or candidate.get("headers")
        if isinstance(rows, list):
            candidate = rows
        elif columns is not None:
            candidate = [candidate]
        else:
            return None

    if not isinstance(candidate, list) or not candidate:
        return None

    rows: list[list[Any]] = []
    if all(isinstance(row, dict) for row in candidate):
        columns: list[str] = []
        for row in candidate:
            for key in row:
                if str(key) not in columns:
                    columns.append(str(key))
        rows = [[row.get(column, "") for column in columns] for row in candidate]
    else:
        columns = []
        for row in candidate:
            if isinstance(row, (list, tuple)):
                rows.append(list(row))
            else:
                rows.append([row])
        width = max((len(row) for row in rows), default=0)
        columns = [f"列{index + 1}" for index in range(width)]

    if not rows:
        return None
    return {
        "name": str(name or "检索结果")[:80],
        "columns": columns,
        "rows": rows,
    }


def _extract_tables(summary: dict[str, Any]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    candidates: list[tuple[str, Any]] = []
    raw_tables = summary.get("tables")
    if isinstance(raw_tables, list):
        candidates.extend((str(item.get("name") or f"结果{index + 1}"), item) for index, item in enumerate(raw_tables))
    for key in ("structured_result", "structured_rows", "result_rows", "rows"):
        if key in summary:
            candidates.append((key, summary.get(key)))
    for name, candidate in candidates:
        if isinstance(candidate, dict) and isinstance(candidate.get("tables"), list):
            for index, nested in enumerate(candidate["tables"]):
                table = _table_from_candidate(str(nested.get("name") or f"结果{index + 1}"), nested)
                if table:
                    tables.append(table)
            continue
        table = _table_from_candidate(name, candidate)
        if table:
            tables.append(table)
    return tables[:20]


def _citation_locator(item: dict[str, Any]) -> str:
    locator = item.get("locator")
    if isinstance(locator, str):
        return locator
    if not isinstance(locator, dict):
        locator = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    sheet = str(locator.get("sheet_name") or "")
    cell = str(locator.get("cell_ref") or "")
    if sheet and cell:
        return f"{sheet}!{cell}"
    if sheet and locator.get("row_index") is not None:
        return f"{sheet} · 第 {int(locator['row_index']) + 1} 行"
    page = locator.get("page") or locator.get("page_number")
    if page:
        return f"第 {page} 页"
    chunk = locator.get("chunk_id") or item.get("chunk_id")
    return str(chunk or "")


def _extract_citations(summary: dict[str, Any]) -> list[dict[str, Any]]:
    raw = summary.get("evidence")
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, dict)):
        return []
    citations: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        title = item.get("file_name") or item.get("source_name") or item.get("document_id") or "参考来源"
        excerpt = item.get("text_preview") or item.get("text") or item.get("content") or ""
        citation = {
            "index": index,
            "title": str(title)[:300],
            "locator": _citation_locator(item),
            "excerpt": str(excerpt)[:2000],
            "source_type": str(item.get("content_kind") or item.get("source_type") or ""),
        }
        evidence_id = item.get("evidence_id") or item.get("id") or item.get("source_id")
        if evidence_id not in (None, ""):
            citation["evidence_id"] = str(evidence_id)[:200]
        citations.append(citation)
    return citations[:200]


def envelope_from_turn(turn: ChatTurn, *, title: str | None = None, include_citations: bool = True) -> ResultEnvelope:
    summary = turn.summary if isinstance(turn.summary, dict) else {}
    return ResultEnvelope(
        title=(title or "对话结果").strip()[:160] or "对话结果",
        query=turn.query,
        answer=turn.answer,
        footer=turn.footer,
        tables=_extract_tables(summary),
        citations=_extract_citations(summary) if include_citations else [],
        metadata={
            "knowledge_base": "" if turn.kb_name == GENERAL_CHAT_KB_NAME else turn.kb_name,
            "session_id": str(turn.session_id),
            "turn_id": turn.id,
            "query_mode": turn.query_mode,
        },
    )
