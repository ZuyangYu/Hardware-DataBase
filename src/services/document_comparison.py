"""Whole-document parsing, alignment and exact/semantic diff.

The agent never receives both full documents.  This service freezes the two
authorized sources, canonicalizes their parsed parts, aligns sections/tables
and emits only changed facts to the model.  Original text, hashes, versions and
locators remain on both sides so every conclusion can be read back.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import src.settings as settings
from src.agents.schemas import Evidence
from src.services.document_comparison_models import (
    ADDED,
    MODIFIED,
    MOVED,
    REMOVED,
    TABLE_ADDED,
    TABLE_MODIFIED,
    TABLE_REMOVED,
    UNCHANGED,
    UNCERTAIN,
    AlignedSection,
    CanonicalDocument,
    CanonicalSection,
    CanonicalTable,
    ComparisonItem,
    ComparisonReport,
    ComparisonSourceSnapshot,
    report_hash,
)
from src.services.evidence_sources import (
    ATTACHMENT,
    COMPARISON,
    content_hash,
    with_source_metadata,
)

# Similarity is only an alignment aid.  It never decides that two different
# versions are the same source; hashes/versions remain on the report.
_ALIGNMENT_THRESHOLD = 0.58
_HEADING_NUMBER_RE = re.compile(r"^\s*(?:第\s*)?(\d+(?:\.\d+)*)\s*(?:[章节部分篇]|[\s、.．:：-])?")
_BUSINESS_KEY_RE = re.compile(r"\b([A-Z]{1,4}\d{2,6}(?:[-_][A-Z0-9]{1,8})?)\b", re.IGNORECASE)


class ComparisonError(Exception):
    """A typed comparison failure that must not look like a normal diff."""

    def __init__(self, error_code: str, detail: str = "", status: str = "blocked"):
        super().__init__(detail or error_code)
        self.error_code = str(error_code)
        self.detail = str(detail or "")
        self.status = str(status or "blocked")

    def as_dict(self) -> dict[str, Any]:
        return {"error_code": self.error_code, "detail": self.detail, "status": self.status}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(*parts: Any) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part or "").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def normalize_text(text: str) -> str:
    """NFKC + line/whitespace normalization used only for comparison."""

    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in value.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _section_number(heading: str) -> str:
    match = _HEADING_NUMBER_RE.match(str(heading or ""))
    return match.group(1) if match else ""


def section_key(heading: str, content: str = "") -> str:
    """Return the strongest deterministic alignment key for a section."""

    number = _section_number(heading)
    if number:
        return f"num:{number}"
    text = normalize_text(heading)
    if text:
        return f"title:{text.casefold()}"
    business = _BUSINESS_KEY_RE.search(normalize_text(content)[:500])
    if business:
        return f"key:{business.group(1).casefold()}"
    return f"content:{_sha(normalize_text(content))[:16]}"


def _json_table_rows(text: str) -> list[list[str]]:
    try:
        payload = json.loads(str(text or ""))
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    rows: list[list[str]] = []
    for row in payload:
        if isinstance(row, list):
            rows.append([str(cell or "") for cell in row])
        elif isinstance(row, dict):
            rows.append([str(value or "") for value in row.values()])
    return rows


def _table_from_parts(
    table_id: str,
    parts: Sequence[Any],
    *,
    locator: dict[str, Any],
) -> CanonicalTable:
    rows: list[list[str]] = []
    headers: tuple[str, ...] = ()
    for part in parts:
        rows.extend(_json_table_rows(str(getattr(part, "text_content", "") or "")))
        if not headers:
            header_context = dict(getattr(part, "metadata", None) or {}).get("header_context")
            if isinstance(header_context, list):
                headers = tuple(str(cell or "") for cell in header_context)
    if not headers and rows:
        headers = tuple(rows[0])
        rows = rows[1:]
    elif headers and rows and tuple(rows[0]) == tuple(headers):
        rows = rows[1:]
    serialized = json.dumps([list(headers), *rows], ensure_ascii=False, separators=(",", ":"))
    return CanonicalTable(
        table_id=table_id,
        headers=headers,
        rows=tuple(tuple(row) for row in rows),
        locator=dict(locator),
        content_hash=content_hash(serialized),
    )


def _part_path(part: Any) -> tuple[str, ...]:
    locator = dict(getattr(part, "locator", None) or {})
    heading_path = locator.get("heading_path")
    if isinstance(heading_path, (list, tuple)):
        return tuple(str(item).strip() for item in heading_path if str(item).strip())
    page = locator.get("page") or locator.get("page_number")
    if page not in (None, ""):
        return (f"page {page}",)
    line_start = locator.get("line_start")
    if line_start not in (None, ""):
        return (f"lines {line_start}",)
    return ("body",)


def canonicalize_parts(
    parts: Iterable[Any],
    *,
    source_type: str,
    source_id: str,
    source_name: str,
    document_version: str = "",
    source_content_hash: str = "",
    parser_version: str = "",
    owner: dict[str, Any] | None = None,
    permissions: dict[str, Any] | None = None,
    status: str = "complete",
    warnings: Sequence[str] = (),
    metadata: dict[str, Any] | None = None,
    frozen_at: str = "",
) -> CanonicalDocument:
    """Build a canonical document from parsed parts (attachment or KB)."""

    ordered = sorted(parts, key=lambda item: int(getattr(item, "ordinal", 0) or 0))
    groups: list[dict[str, Any]] = []
    current_key: tuple[str, ...] | None = None
    current: dict[str, Any] | None = None
    for part in ordered:
        path = _part_path(part)
        part_type = str(getattr(part, "part_type", "") or "text")
        part_locator = dict(getattr(part, "locator", None) or {})
        part_metadata = dict(getattr(part, "metadata", None) or {})
        table_id = ""
        if part_type == "table":
            table_id = str(part_metadata.get("table_id") or part_locator.get("table_id")
                           or f"table-{part_locator.get('table') or part_locator.get('table_index') or len(groups)}")
        # Consecutive parts under one heading path form one section.  Tables
        # are grouped by their stable table id inside that section.
        if current is None or path != current_key:
            current = {"path": path, "parts": [], "tables": {}, "locator": {}}
            groups.append(current)
            current_key = path
        current["parts"].append(part)
        if not current["locator"]:
            current["locator"] = dict(part_locator)
        if part_type == "table":
            current["tables"].setdefault(table_id, []).append(part)

    sections: list[CanonicalSection] = []
    all_warnings = list(warnings)
    for index, group in enumerate(groups):
        path = tuple(group["path"])
        heading = path[-1] if path else "正文"
        level = len(path) or 1
        text_parts = [
            str(getattr(part, "text_content", "") or "")
            for part in group["parts"]
            if str(getattr(part, "part_type", "") or "text") != "table"
        ]
        content = "\n\n".join(part for part in text_parts if part.strip())
        tables = tuple(
            _table_from_parts(
                table_id,
                table_parts,
                locator={**group["locator"], "table_id": table_id},
            )
            for table_id, table_parts in group["tables"].items()
        )
        table_payload = json.dumps(
            [[table.headers, table.rows] for table in tables],
            ensure_ascii=False, sort_keys=True, default=str,
        )
        section_hash = _sha(normalize_text(content), table_payload)
        sections.append(CanonicalSection(
            section_id=f"sec-{_sha(source_id, path, index)[:16]}",
            heading=heading,
            level=level,
            path=path,
            order=index,
            content=content,
            tables=tables,
            locator={**group["locator"], "heading_path": list(path)},
            content_hash=section_hash,
            metadata={"has_tables": bool(tables)},
        ))
    if not sections:
        all_warnings.append("document_contains_no_parseable_sections")
    snapshot = ComparisonSourceSnapshot(
        source_type=source_type,
        source_id=source_id,
        source_name=source_name,
        document_version=str(document_version or ""),
        content_hash=str(source_content_hash or ""),
        parser_version=str(parser_version or ""),
        frozen_at=frozen_at or _utc_now(),
        owner=dict(owner or {}),
        permissions=dict(permissions or {}),
        status=status,
        warnings=tuple(dict.fromkeys(all_warnings)),
        metadata=dict(metadata or {}),
    )
    normalized_hash = _sha(
        *[f"{section.section_id}:{section.content_hash}" for section in sections]
    )
    return CanonicalDocument(
        snapshot=snapshot,
        sections=tuple(sections),
        normalized_hash=normalized_hash,
        warnings=tuple(all_warnings),
    )


def canonicalize_attachment(
    ref: Any,
    parts: Sequence[Any],
    *,
    owner: dict[str, Any] | None = None,
    permissions: dict[str, Any] | None = None,
) -> CanonicalDocument:
    """Freeze one authorized attachment and canonicalize its stored parts."""

    attachment_id = str(getattr(ref, "attachment_id", "") or "")
    if not attachment_id:
        raise ComparisonError("source_identity_missing", "attachment_id is required")
    parser_version = next(
        (str(getattr(part, "parser_version", "") or "") for part in parts if part),
        str(getattr(ref, "parser_version", "") or ""),
    )
    return canonicalize_parts(
        parts,
        source_type=ATTACHMENT,
        source_id=f"attachment:{attachment_id}",
        source_name=str(getattr(ref, "filename", "") or "attachment"),
        document_version=str(getattr(ref, "sha256", "") or "")[:16],
        source_content_hash=str(getattr(ref, "sha256", "") or ""),
        parser_version=parser_version,
        owner=owner,
        permissions=permissions,
        status="complete" if parts else "degraded",
        warnings=() if parts else ("attachment_has_no_readable_parts",),
        metadata={
            "attachment_id": attachment_id,
            "asset_id": str(getattr(ref, "asset_id", "") or ""),
            "extension": str(getattr(ref, "extension", "") or ""),
        },
    )


def _spreadsheet_parts(source_path: str, *, source_id: str) -> list[Any]:
    """Read a bounded XLSX/XLSM into table parts using the existing parser."""

    from types import SimpleNamespace

    from src.pipelines.spreadsheet.xlsx_parser import XlsxParseLimits, parse_xlsx

    limits = XlsxParseLimits(
        max_rows=max(1, int(getattr(settings, "CHAT_ATTACHMENT_MAX_ROWS", 20000))),
        max_cells=max(1, int(getattr(settings, "CHAT_ATTACHMENT_MAX_CELLS", 200000))),
        max_sheets=max(1, int(getattr(settings, "CHAT_ATTACHMENT_MAX_SHEETS", 50))),
        max_shared_strings=max(1, int(getattr(settings, "CHAT_ATTACHMENT_MAX_SHARED_STRINGS", 100000))),
        max_cell_text_length=max(1, int(getattr(settings, "CHAT_ATTACHMENT_MAX_CELL_TEXT_LENGTH", 4000))),
        max_uncompressed_bytes=max(1, int(getattr(settings, "CHAT_ATTACHMENT_MAX_UNCOMPRESSED_BYTES", 50 * 1024 * 1024))),
    )
    workbook = parse_xlsx(source_path, limits=limits)
    parts: list[Any] = []
    ordinal = 0
    max_cell_text = limits.max_cell_text_length
    for sheet in workbook.sheets:
        rows = [
            [str(cell or "")[:max_cell_text] for cell in row]
            for row in sheet.rows
        ]
        if not rows:
            continue
        headers = rows[0]
        payload = rows[1:]
        parts.append(SimpleNamespace(
            part_id=f"{source_id}:sheet:{sheet.name}",
            ordinal=ordinal,
            part_type="table",
            text_content=json.dumps(payload, ensure_ascii=False),
            locator={"heading_path": [str(sheet.name)], "sheet_name": str(sheet.name)},
            metadata={
                "table_id": f"sheet:{sheet.name}",
                "header_context": headers,
                "format": "xlsx",
            },
            parser_version="xlsx-parser",
        ))
        ordinal += 1
    return parts


def canonicalize_archive_record(
    record: Any,
    source_path: str,
    *,
    owner: dict[str, Any] | None = None,
    permissions: dict[str, Any] | None = None,
    parser: Any | None = None,
) -> CanonicalDocument:
    """Freeze one ACL-scoped archived knowledge-base document.

    ``record`` must already have been resolved through the department-scoped
    document store; this function never accepts an arbitrary path from a model
    or client.
    """

    path = str(source_path or "")
    if not path or not os.path.isfile(path):
        raise ComparisonError("source_unavailable", "archived source file is not readable")
    extension = os.path.splitext(path)[1].lower()
    try:
        file_hash = hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError as exc:
        raise ComparisonError("source_unavailable", str(exc)[:200]) from exc
    record_hash = str(getattr(record, "content_hash", "") or "")
    if record_hash and file_hash != record_hash:
        raise ComparisonError(
            "source_changed",
            "archived file hash differs from the authorized record version",
        )
    if extension in {".xlsx", ".xlsm"}:
        parts = _spreadsheet_parts(path, source_id=str(getattr(record, "id", "") or ""))
        parser_version = "openpyxl-readonly"
        status = "complete" if parts else "degraded"
        warnings = () if parts else ("spreadsheet_has_no_readable_cells",)
    else:
        from types import SimpleNamespace

        from src.attachments.local_document_parser import LocalDocumentParser

        asset = SimpleNamespace(
            asset_id=str(getattr(record, "document_id", "") or f"record-{getattr(record, 'id', '')}"),
            extension=extension,
        )
        outcome = (parser or LocalDocumentParser()).parse(asset, path)
        if not outcome.ok:
            raise ComparisonError(
                "parse_failed",
                str(outcome.error_message or outcome.error_code or "parse_failed"),
                status="failed",
            )
        parts = list(outcome.parts)
        parser_version = next(
            (str(getattr(part, "parser_version", "") or "") for part in parts),
            str(getattr(record, "processor_version", "") or ""),
        )
        status = "complete" if outcome.parse_status == "ready" else "degraded"
        warnings = tuple(outcome.degraded_reasons or ())
    return canonicalize_parts(
        parts,
        source_type="knowledge_base",
        source_id=f"kb:{getattr(record, 'kb_id', '')}:record:{getattr(record, 'id', '')}",
        source_name=str(getattr(record, "document_name", "") or "document"),
        document_version=str(
            getattr(record, "revision", "")
            or getattr(record, "source_version_id", "")
            or file_hash[:16]
        ),
        source_content_hash=record_hash or file_hash,
        parser_version=parser_version,
        owner=owner,
        permissions=permissions,
        status=status,
        warnings=warnings,
        metadata={
            "kb_id": int(getattr(record, "kb_id", 0) or 0),
            "record_id": int(getattr(record, "id", 0) or 0),
            "processor_kind": str(getattr(record, "processor_kind", "") or ""),
            "department_id": str(getattr(record, "department_id", "") or ""),
        },
    )


def _similarity(left: CanonicalSection, right: CanonicalSection) -> float:
    left_text = normalize_text(f"{left.heading}\n{left.content}")
    right_text = normalize_text(f"{right.heading}\n{right.content}")
    if not left_text or not right_text:
        return 1.0 if left_text == right_text else 0.0
    return difflib.SequenceMatcher(None, left_text, right_text, autojunk=False).ratio()


def align_documents(
    left: CanonicalDocument,
    right: CanonicalDocument,
) -> tuple[list[AlignedSection], list[CanonicalSection], list[CanonicalSection]]:
    """Align by section number/title, exact content, then similarity."""

    left_sections = list(left.sections)
    right_sections = list(right.sections)
    used_left: set[str] = set()
    used_right: set[str] = set()
    alignments: list[AlignedSection] = []

    def add(left_section: CanonicalSection, right_section: CanonicalSection,
            method: str, key: str, confidence: float) -> None:
        used_left.add(left_section.section_id)
        used_right.add(right_section.section_id)
        alignments.append(AlignedSection(
            alignment_id=f"align-{_sha(left_section.section_id, right_section.section_id)[:16]}",
            left_section_id=left_section.section_id,
            right_section_id=right_section.section_id,
            method=method,
            section_key=key,
            confidence=round(confidence, 4),
        ))

    # 1. Section number/title.
    right_by_key: dict[str, list[CanonicalSection]] = {}
    for section in right_sections:
        right_by_key.setdefault(section_key(section.heading, section.content), []).append(section)
    for left_section in left_sections:
        key = section_key(left_section.heading, left_section.content)
        candidates = right_by_key.get(key) or []
        match = next((item for item in candidates if item.section_id not in used_right), None)
        if match is not None:
            add(left_section, match, "section_key", key, 1.0)

    # 2. Identical normalized content moves/renames.
    right_by_hash: dict[str, list[CanonicalSection]] = {}
    for section in right_sections:
        if section.section_id not in used_right:
            right_by_hash.setdefault(section.content_hash, []).append(section)
    for left_section in left_sections:
        if left_section.section_id in used_left:
            continue
        match = next(
            (item for item in right_by_hash.get(left_section.content_hash, [])
             if item.section_id not in used_right),
            None,
        )
        if match is not None:
            add(left_section, match, "content_identity", "content:" + left_section.content_hash[:16], 1.0)

    # 3. Similarity, only between unmatched sections of the same level.
    for left_section in left_sections:
        if left_section.section_id in used_left:
            continue
        best: tuple[float, CanonicalSection | None] = (0.0, None)
        for right_section in right_sections:
            if right_section.section_id in used_right:
                continue
            if left_section.level != right_section.level:
                continue
            score = _similarity(left_section, right_section)
            if score > best[0]:
                best = (score, right_section)
        if best[1] is not None and best[0] >= _ALIGNMENT_THRESHOLD:
            add(
                left_section,
                best[1],
                "similarity",
                section_key(left_section.heading, left_section.content),
                best[0],
            )

    unmatched_left = [item for item in left_sections if item.section_id not in used_left]
    unmatched_right = [item for item in right_sections if item.section_id not in used_right]
    return alignments, unmatched_left, unmatched_right


def _item(
    report_seed: str,
    kind: str,
    *,
    key: str,
    left_doc: CanonicalDocument,
    right_doc: CanonicalDocument,
    left_section: CanonicalSection | None = None,
    right_section: CanonicalSection | None = None,
    left_text: str = "",
    right_text: str = "",
    summary: str = "",
    semantic_judgement: str = "exact",
    confidence: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> ComparisonItem:
    item_id = f"cmp-{_sha(report_seed, kind, key, summary, left_text, right_text)[:16]}"
    left_locator = dict(left_section.locator or {}) if left_section else {}
    right_locator = dict(right_section.locator or {}) if right_section else {}
    if (metadata or {}).get("table_id"):
        left_locator.setdefault("table_id", str(metadata["table_id"]))
        right_locator.setdefault("table_id", str(metadata["table_id"]))
    return ComparisonItem(
        item_id=item_id,
        kind=kind,
        section_key=key,
        left_section_id=left_section.section_id if left_section else "",
        right_section_id=right_section.section_id if right_section else "",
        left_ref=f"{left_doc.snapshot.source_id}#{left_section.section_id}" if left_section else "",
        right_ref=f"{right_doc.snapshot.source_id}#{right_section.section_id}" if right_section else "",
        left_locator=left_locator,
        right_locator=right_locator,
        left_text=left_text,
        right_text=right_text,
        summary=summary,
        semantic_judgement=semantic_judgement,
        confidence=confidence,
        evidence_refs=tuple(
            item for item in (
                f"{left_doc.snapshot.source_id}#{left_section.section_id}" if left_section else "",
                f"{right_doc.snapshot.source_id}#{right_section.section_id}" if right_section else "",
            ) if item
        ),
        metadata=dict(metadata or {}),
    )


def _inline_character_diff(old: str, new: str, *, limit: int = 1600) -> str:
    """Compact character-level diff for short changed paragraphs."""

    old = normalize_text(old)
    new = normalize_text(new)
    if not old and not new:
        return ""
    if len(old) + len(new) > 20_000:
        return ""
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    parts: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            parts.append(old[i1:i2])
        elif tag == "delete":
            parts.append(f"[-{old[i1:i2]}-]")
        elif tag == "insert":
            parts.append(f"{{+{new[j1:j2]}+}}")
        else:
            parts.append(f"[-{old[i1:i2]}-]{{+{new[j1:j2]}+}}")
        if sum(len(part) for part in parts) > limit:
            parts.append("…")
            break
    return "".join(parts)


def _text_diff_items(
    *,
    report_seed: str,
    key: str,
    left_doc: CanonicalDocument,
    right_doc: CanonicalDocument,
    left_section: CanonicalSection | None,
    right_section: CanonicalSection | None,
    left_text: str,
    right_text: str,
) -> list[ComparisonItem]:
    if normalize_text(left_text) == normalize_text(right_text):
        return []
    left_lines = normalize_text(left_text).split("\n") if normalize_text(left_text) else []
    right_lines = normalize_text(right_text).split("\n") if normalize_text(right_text) else []
    matcher = difflib.SequenceMatcher(None, left_lines, right_lines, autojunk=False)
    items: list[ComparisonItem] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old = "\n".join(left_lines[i1:i2])
        new = "\n".join(right_lines[j1:j2])
        if tag == "replace":
            kind = MODIFIED
            summary = f"章节「{left_section.heading if left_section else key}」正文发生变化"
        elif tag == "delete":
            kind = REMOVED
            summary = f"章节「{left_section.heading if left_section else key}」删除 {i2 - i1} 行"
        else:
            kind = ADDED
            summary = f"章节「{right_section.heading if right_section else key}」新增 {j2 - j1} 行"
        metadata: dict[str, Any] = {"line_span": [i1, i2, j1, j2]}
        if kind == MODIFIED:
            inline = _inline_character_diff(old, new)
            if inline:
                metadata["inline_diff"] = inline
        items.append(_item(
            report_seed, kind, key=key, left_doc=left_doc, right_doc=right_doc,
            left_section=left_section, right_section=right_section,
            left_text=old, right_text=new, summary=summary,
            metadata=metadata,
        ))
    return items


def _table_diff_items(
    *,
    report_seed: str,
    key: str,
    left_doc: CanonicalDocument,
    right_doc: CanonicalDocument,
    left_section: CanonicalSection | None,
    right_section: CanonicalSection | None,
    left_tables: Sequence[CanonicalTable],
    right_tables: Sequence[CanonicalTable],
) -> list[ComparisonItem]:
    items: list[ComparisonItem] = []
    by_id_left = {table.table_id: table for table in left_tables}
    by_id_right = {table.table_id: table for table in right_tables}
    # Pair identical ids first, then remaining tables by order.
    pairs: list[tuple[CanonicalTable | None, CanonicalTable | None]] = []
    for table_id, table in by_id_left.items():
        pairs.append((table, by_id_right.get(table_id)))
    for table in right_tables:
        if table.table_id not in by_id_left:
            pairs.append((None, table))
    for left_table, right_table in pairs:
        if left_table is None and right_table is not None:
            items.append(_item(
                report_seed, TABLE_ADDED, key=f"{key}:{right_table.table_id}",
                left_doc=left_doc, right_doc=right_doc,
                right_section=right_section,
                right_text=_render_table(right_table, limit=24),
                summary=f"新增表格「{right_table.table_id}」",
                metadata={"table_id": right_table.table_id},
            ))
            continue
        if right_table is None and left_table is not None:
            items.append(_item(
                report_seed, TABLE_REMOVED, key=f"{key}:{left_table.table_id}",
                left_doc=left_doc, right_doc=right_doc,
                left_section=left_section,
                left_text=_render_table(left_table, limit=24),
                summary=f"删除表格「{left_table.table_id}」",
                metadata={"table_id": left_table.table_id},
            ))
            continue
        if left_table is None or right_table is None:
            continue
        if left_table.content_hash == right_table.content_hash:
            continue
        changed_rows = _changed_table_rows(left_table, right_table)
        items.append(_item(
            report_seed, TABLE_MODIFIED, key=f"{key}:{left_table.table_id}",
            left_doc=left_doc, right_doc=right_doc,
            left_section=left_section, right_section=right_section,
            left_text=_render_table(left_table, rows=changed_rows[0]),
            right_text=_render_table(right_table, rows=changed_rows[1]),
            summary=f"表格「{left_table.table_id}」有 {len(changed_rows[0])} 行变化",
            metadata={
                "table_id": left_table.table_id,
                "changed_left_rows": changed_rows[2],
                "changed_right_rows": changed_rows[3],
            },
        ))
    return items


def _changed_table_rows(
    left: CanonicalTable,
    right: CanonicalTable,
) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]], list[int], list[int]]:
    matcher = difflib.SequenceMatcher(None, left.rows, right.rows, autojunk=False)
    left_rows: list[tuple[str, ...]] = []
    right_rows: list[tuple[str, ...]] = []
    left_indexes: list[int] = []
    right_indexes: list[int] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        left_rows.extend(left.rows[i1:i2])
        right_rows.extend(right.rows[j1:j2])
        left_indexes.extend(range(i1, i2))
        right_indexes.extend(range(j1, j2))
    return left_rows, right_rows, left_indexes, right_indexes


def _render_table(table: CanonicalTable, *, rows: Sequence[Sequence[str]] | None = None,
                  limit: int = 12) -> str:
    lines = [" | ".join(table.headers)] if table.headers else []
    for row in list(rows if rows is not None else table.rows)[:limit]:
        lines.append(" | ".join(str(cell or "") for cell in row))
    if len(list(rows if rows is not None else table.rows)) > limit:
        lines.append("...")
    return "\n".join(lines)


def compare_documents(
    left: CanonicalDocument,
    right: CanonicalDocument,
    *,
    created_at: str | None = None,
) -> ComparisonReport:
    """Compare two frozen canonical documents and return an immutable report."""

    if left.snapshot.status == "failed" or right.snapshot.status == "failed":
        raise ComparisonError("source_parse_failed", "one side could not be parsed", status="failed")
    report_seed = _sha(
        left.snapshot.source_id, left.snapshot.content_hash, left.normalized_hash,
        right.snapshot.source_id, right.snapshot.content_hash, right.normalized_hash,
    )
    alignments, unmatched_left, unmatched_right = align_documents(left, right)
    by_left = {section.section_id: section for section in left.sections}
    by_right = {section.section_id: section for section in right.sections}
    items: list[ComparisonItem] = []
    warnings: list[str] = list(dict.fromkeys([*left.warnings, *right.warnings]))

    for alignment in alignments:
        left_section = by_left.get(alignment.left_section_id)
        right_section = by_right.get(alignment.right_section_id)
        if left_section is None or right_section is None:
            continue
        key = alignment.section_key
        if left_section.content_hash == right_section.content_hash and _tables_equal(
            left_section.tables, right_section.tables,
        ):
            if left_section.heading != right_section.heading:
                kind = MOVED
                items.append(_item(
                    report_seed, kind, key=key, left_doc=left, right_doc=right,
                    left_section=left_section, right_section=right_section,
                    left_text=left_section.heading, right_text=right_section.heading,
                    summary="章节正文相同但标题/位置不同",
                    metadata={"left_heading": left_section.heading,
                              "right_heading": right_section.heading,
                              "change_scope": "heading"},
                ))
            else:
                items.append(_item(
                    report_seed, UNCHANGED, key=key, left_doc=left, right_doc=right,
                    left_section=left_section, right_section=right_section,
                    left_text=left_section.content, right_text=right_section.content,
                    summary="章节内容未变化",
                ))
            continue
        if left_section.content_hash == right_section.content_hash and alignment.method == "content_identity":
            # Same content, different heading: one moved fact, not two edits.
            items.append(_item(
                report_seed, MOVED, key=key, left_doc=left, right_doc=right,
                left_section=left_section, right_section=right_section,
                left_text=left_section.heading, right_text=right_section.heading,
                summary="章节正文相同但标题/位置不同",
                metadata={"left_heading": left_section.heading,
                          "right_heading": right_section.heading},
            ))
            continue
        # Heading-only change is a real modification; keep exact text as the
        # primary evidence and mark semantic judgement uncertain.
        if left_section.heading != right_section.heading:
            items.append(_item(
                report_seed, MODIFIED, key=key, left_doc=left, right_doc=right,
                left_section=left_section, right_section=right_section,
                left_text=left_section.heading, right_text=right_section.heading,
                summary="章节标题发生变化",
                semantic_judgement="uncertain",
                confidence=alignment.confidence,
                metadata={"change_scope": "heading"},
            ))
        items.extend(_text_diff_items(
            report_seed=report_seed, key=key, left_doc=left, right_doc=right,
            left_section=left_section, right_section=right_section,
            left_text=left_section.content, right_text=right_section.content,
        ))
        items.extend(_table_diff_items(
            report_seed=report_seed, key=key, left_doc=left, right_doc=right,
            left_section=left_section, right_section=right_section,
            left_tables=left_section.tables, right_tables=right_section.tables,
        ))

    for section in unmatched_left:
        items.extend(_text_diff_items(
            report_seed=report_seed, key=section_key(section.heading, section.content),
            left_doc=left, right_doc=right, left_section=section, right_section=None,
            left_text=section.content, right_text="",
        ))
        if not section.content.strip():
            items.append(_item(
                report_seed, REMOVED, key=section_key(section.heading, section.content),
                left_doc=left, right_doc=right, left_section=section,
                left_text=section.heading, summary=f"删除章节「{section.heading}」",
            ))
        for table in section.tables:
            items.append(_item(
                report_seed, TABLE_REMOVED, key=f"{section.section_id}:{table.table_id}",
                left_doc=left, right_doc=right, left_section=section,
                left_text=_render_table(table, limit=24),
                summary=f"删除表格「{table.table_id}」",
                metadata={"table_id": table.table_id},
            ))
    for section in unmatched_right:
        items.extend(_text_diff_items(
            report_seed=report_seed, key=section_key(section.heading, section.content),
            left_doc=left, right_doc=right, left_section=None, right_section=section,
            left_text="", right_text=section.content,
        ))
        if not section.content.strip():
            items.append(_item(
                report_seed, ADDED, key=section_key(section.heading, section.content),
                left_doc=left, right_doc=right, right_section=section,
                right_text=section.heading, summary=f"新增章节「{section.heading}」",
            ))
        for table in section.tables:
            items.append(_item(
                report_seed, TABLE_ADDED, key=f"{section.section_id}:{table.table_id}",
                left_doc=left, right_doc=right, right_section=section,
                right_text=_render_table(table, limit=24),
                summary=f"新增表格「{table.table_id}」",
                metadata={"table_id": table.table_id},
            ))

    # A section can change only in its table set; make sure a section-level
    # modification is still visible when text diff is empty.
    changed_section_ids = {item.left_section_id for item in items if item.left_section_id}
    for alignment in alignments:
        left_section = by_left.get(alignment.left_section_id)
        right_section = by_right.get(alignment.right_section_id)
        if left_section is None or right_section is None:
            continue
        if left_section.section_id in changed_section_ids:
            continue
        if left_section.content_hash == right_section.content_hash and _tables_equal(
            left_section.tables, right_section.tables,
        ):
            continue
        items.append(_item(
            report_seed, MODIFIED, key=alignment.section_key,
            left_doc=left, right_doc=right,
            left_section=left_section, right_section=right_section,
            left_text=left_section.content, right_text=right_section.content,
            summary="章节内容或表格结构发生变化",
            semantic_judgement="uncertain",
            confidence=alignment.confidence,
        ))

    counts = {kind: 0 for kind in (
        ADDED, REMOVED, MODIFIED, MOVED,
        TABLE_ADDED, TABLE_REMOVED, TABLE_MODIFIED, UNCHANGED, UNCERTAIN,
    )}
    for item in items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    if not items:
        items.append(_item(
            report_seed, UNCHANGED, key="document",
            left_doc=left, right_doc=right,
            left_text="", right_text="", summary="两份来源未发现差异",
        ))
        counts[UNCHANGED] = 1
    status = "complete"
    if left.snapshot.status != "complete" or right.snapshot.status != "complete":
        status = "degraded"
    if any(item.semantic_judgement == "uncertain" for item in items):
        warnings.append("semantic_judgement_uncertain_present")
    report = ComparisonReport(
        report_id=f"cmp-{report_seed[:16]}",
        created_at=created_at or _utc_now(),
        left=left.snapshot,
        right=right.snapshot,
        alignments=tuple(alignments),
        items=tuple(items),
        counts=counts,
        status=status,
        warnings=tuple(dict.fromkeys(warnings)),
    )
    payload = report.to_dict()
    return replace(report, immutable_hash=report_hash(payload))


def _tables_equal(left: Sequence[CanonicalTable], right: Sequence[CanonicalTable]) -> bool:
    return [table.content_hash for table in left] == [table.content_hash for table in right]


def _focus_terms(focus: str) -> list[str]:
    text = normalize_text(focus).casefold()
    if not text:
        return []
    terms: list[str] = []
    for word in re.findall(r"[a-z0-9_./-]{2,}", text):
        if word not in terms:
            terms.append(word)
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        if run not in terms:
            terms.append(run)
        # Bigrams let "信号与引脚" match a section that contains only "信号"
        # or "引脚"; the focus is an expansion hint, not an exact phrase.
        for index in range(len(run) - 1):
            gram = run[index:index + 2]
            if gram not in terms:
                terms.append(gram)
    # "第 3 节" / "4.1 信号" become compact tokens so whitespace differences
    # between a heading and a focus phrase do not hide a match.
    compact = re.sub(r"\s+", "", text)
    for run in re.findall(r"[\u4e00-\u9fff0-9]{2,}", compact):
        if run not in terms:
            terms.append(run)
    # Section keys use ``num:3`` / ``num:4.1`` while users commonly say
    # “第 3 节” or “4.1 信号”.  Add the canonical numeric key so a focus can
    # select a chapter even when its body contains no chapter number.
    for number in re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)*(?![A-Za-z])", text):
        key = f"num:{number}"
        if key not in terms:
            terms.append(key)
    return terms


def _focus_matches(item: ComparisonItem, terms: list[str]) -> bool:
    if not terms:
        return True
    haystack = normalize_text(" ".join([
        item.section_key, item.summary, item.left_text, item.right_text,
        str(item.metadata.get("left_heading") or ""),
        str(item.metadata.get("right_heading") or ""),
    ])).casefold()
    compact = re.sub(r"\s+", "", haystack)
    return any(term in haystack or term in compact for term in terms)


def _render_comparison_items(
    report: ComparisonReport,
    selected: Sequence[ComparisonItem],
    *,
    byte_limit: int,
    item_limit: int,
) -> list[Evidence]:
    evidence: list[Evidence] = []
    used = 0
    side_types = [
        report.left.source_type,
        report.right.source_type,
    ]
    for item in selected[:item_limit]:
        left_label = report.left.source_name or report.left.source_id
        right_label = report.right.source_name or report.right.source_id
        content = (
            f"比较项 {item.item_id}（{item.kind}）：{item.summary}\n"
            f"[{report.left.source_type}] {left_label}: {item.left_text[:1200] or '（无）'}\n"
            f"[{report.right.source_type}] {right_label}: {item.right_text[:1200] or '（无）'}"
        )
        inline_diff = str(item.metadata.get("inline_diff") or "")
        if not inline_diff and item.kind == MODIFIED:
            inline_diff = _inline_character_diff(item.left_text, item.right_text)
        if inline_diff:
            content += f"\n逐字差异（[-左侧-] / {{+右侧+}}）：{inline_diff[:1800]}"
        encoded = len(content.encode("utf-8")) + 300
        if used + encoded > byte_limit and evidence:
            break
        metadata = with_source_metadata({
            "source_type": COMPARISON,
            "source_id": report.report_id,
            "evidence_role": "comparison",
            "comparison_report_id": report.report_id,
            "comparison_item_id": item.item_id,
            "comparison_sources": side_types,
            "content_hash": report.immutable_hash,
            "document_version": f"{report.left.document_version}|{report.right.document_version}",
            "left_ref": item.left_ref,
            "right_ref": item.right_ref,
            "semantic_judgement": item.semantic_judgement,
        }, content=content, locator={
            "comparison_report_id": report.report_id,
            "comparison_item_id": item.item_id,
            "left": item.left_locator,
            "right": item.right_locator,
        })
        item_key = _sha(
            item.item_id, item.kind, item.section_key,
            item.left_text, item.right_text,
        )[:16]
        evidence.append(Evidence(
            id=f"comparison:{report.report_id}:{item_key}",
            content=content,
            source_name=f"{left_label} ↔ {right_label}",
            content_kind=f"comparison_{item.kind}",
            processor_kind="document_comparison",
            score=1.0,
            locator={
                "comparison_report_id": report.report_id,
                "comparison_item_id": item.item_id,
                "left": item.left_locator,
                "right": item.right_locator,
            },
            metadata=metadata,
        ))
        used += encoded
    return evidence


def render_comparison_evidence(
    report: ComparisonReport,
    *,
    max_bytes: int | None = None,
    max_items: int | None = None,
    focus: str = "",
) -> list[Evidence]:
    """Convert changed comparison facts into bounded, citable evidence.

    Full documents stay in the comparison store.  Unchanged sections are not
    sent unless there are no changes at all; even then the single summary row
    keeps both side references.
    """

    byte_limit = max(
        400,
        int(max_bytes if max_bytes is not None
            else getattr(settings, "DOCUMENT_COMPARISON_MAX_MODEL_BYTES", 12000)),
    )
    item_limit = max(
        1,
        int(max_items if max_items is not None
            else getattr(settings, "DOCUMENT_COMPARISON_MAX_ITEMS", 40)),
    )
    changed = report.changed_items()
    terms = _focus_terms(focus)
    if terms and changed:
        changed = [item for item in changed if _focus_matches(item, terms)]
    selected = changed or ([] if terms else list(report.items))
    return _render_comparison_items(
        report, selected, byte_limit=byte_limit, item_limit=item_limit,
    )


@dataclass(frozen=True)
class ComparisonPage:
    """One deterministic page of a frozen comparison report."""

    report_id: str
    scope_key: str
    items: tuple[ComparisonItem, ...]
    evidence: tuple[Evidence, ...]
    offset: int
    total_count: int
    rendered_count: int
    remaining_count: int
    next_cursor: str
    report_status: str
    warnings: tuple[str, ...]
    truncated: bool
    scope_found: bool = True
    available_keys: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "scope_key": self.scope_key,
            "target_item_ids": [item.item_id for item in self.items],
            "offset": self.offset,
            "total_count": self.total_count,
            "rendered_count": self.rendered_count,
            "remaining_count": self.remaining_count,
            "next_cursor": self.next_cursor,
            "report_status": self.report_status,
            "warnings": list(self.warnings),
            "truncated": self.truncated,
            "scope_found": self.scope_found,
            "available_keys": list(self.available_keys),
        }


def render_comparison_page(
    report: ComparisonReport,
    *,
    chapter_keys: Sequence[str] = (),
    section_keys: Sequence[str] = (),
    item_ids: Sequence[str] = (),
    table_keys: Sequence[str] = (),
    offset: int = 0,
    limit: int = 0,
    cursor: str = "",
    max_bytes: int | None = None,
    max_items: int | None = None,
    focus: str = "",
) -> ComparisonPage:
    """Render a stable, bounded page selected from the frozen report.

    ``cursor`` is the last item id from the previous page.  ``offset`` is
    accepted for callers that already have a numeric position.  Selection is
    exact/prefix based; fuzzy ``focus`` is only a compatibility fallback.
    """

    from src.services.comparison_scope import (
        chapter_key_for_item,
        normalize_section_key,
        select_scope_items,
        table_key_for_item,
    )
    from src.services.task_planning import DeliveryScope

    scope = DeliveryScope(
        scope_kind=(
            "chapter_detail" if chapter_keys
            else "item_detail" if (section_keys or item_ids or table_keys or focus)
            else "full_report"
        ),
        chapter_keys=tuple(str(value) for value in chapter_keys if str(value)),
        section_keys=tuple(str(value) for value in section_keys if str(value)),
        item_ids=tuple(str(value) for value in item_ids if str(value)),
        table_keys=tuple(str(value) for value in table_keys if str(value)),
    )
    if focus and not (chapter_keys or section_keys or item_ids or table_keys):
        terms = _focus_terms(focus)
        selected = tuple(
            item for item in report.changed_items()
            if not terms or _focus_matches(item, terms)
        )
    else:
        selected = select_scope_items(report, scope)
    total = len(selected)
    start = max(0, int(offset or 0))
    cursor_text = str(cursor or "").strip()
    if cursor_text and start == 0:
        for index, item in enumerate(selected):
            if item.item_id == cursor_text:
                start = index + 1
                break
        else:
            if cursor_text.isdigit():
                start = int(cursor_text)
    page_size = max(
        1,
        int(limit or max_items or getattr(settings, "DOCUMENT_COMPARISON_MAX_ITEMS", 40) or 40),
    )
    byte_limit = max(
        400,
        int(max_bytes if max_bytes is not None
            else getattr(settings, "DOCUMENT_COMPARISON_MAX_MODEL_BYTES", 12000)),
    )
    page_items = selected[start:start + page_size]
    evidence = _render_comparison_items(
        report,
        page_items,
        byte_limit=byte_limit,
        item_limit=max(1, len(page_items) or page_size),
    )
    rendered = len(evidence)
    consumed = page_items[:rendered]
    remaining = max(0, total - start - rendered)
    next_cursor = consumed[-1].item_id if remaining and consumed else ""
    available_keys = tuple(
        dict.fromkeys(str(item.section_key) for item in report.items if item.section_key)
    )
    scope_found = True
    if chapter_keys:
        wanted = {normalize_section_key(value) for value in chapter_keys}
        scope_found = any(
            chapter_key_for_item(item) in wanted
            or any(
                str(item.section_key or "").startswith(key + ".")
                for key in wanted
            )
            for item in report.items
        )
    elif section_keys:
        wanted = {normalize_section_key(value) for value in section_keys}
        scope_found = any(
            str(item.section_key or "").split(":table-")[0] in wanted
            or any(
                str(item.section_key or "").startswith(key + ".")
                for key in wanted
            )
            for item in report.items
        )
    elif table_keys:
        wanted_tables = {str(value).removeprefix("table-") for value in table_keys}
        scope_found = any(
            table_key_for_item(item).removeprefix("table-") in wanted_tables
            for item in report.items
        )
    elif item_ids:
        scope_found = any(item.item_id in set(item_ids) for item in report.items)
    return ComparisonPage(
        report_id=report.report_id,
        scope_key=scope.scope_key,
        items=tuple(consumed),
        evidence=tuple(evidence),
        offset=start,
        total_count=total,
        rendered_count=rendered,
        remaining_count=remaining,
        next_cursor=next_cursor,
        report_status=str(report.status or ""),
        warnings=tuple(str(item) for item in report.warnings or ()),
        truncated=remaining > 0,
        scope_found=scope_found,
        available_keys=available_keys,
    )


def dump_report(report: ComparisonReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)


__all__ = [
    "ComparisonError",
    "ComparisonPage",
    "align_documents",
    "canonicalize_archive_record",
    "canonicalize_attachment",
    "canonicalize_parts",
    "compare_documents",
    "dump_report",
    "normalize_text",
    "render_comparison_evidence",
    "render_comparison_page",
    "section_key",
]
