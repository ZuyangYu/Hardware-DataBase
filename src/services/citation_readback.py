"""Read an exact citation from its current, authorized processing product."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
from collections.abc import Mapping

from src.pipelines.document_rag.schemas import ScopedRetrievalError


def validate_citation_processing_identity(record, expected: Mapping) -> None:
    """Require original processing stamps; legacy citations must be retrieved again."""
    revision = expected.get("index_revision")
    if type(revision) is not int or revision != int(getattr(record, "index_revision", 0) or 0):
        raise ScopedRetrievalError("source_changed")
    for key in ("processing_artifact_id", "processor_version"):
        if not isinstance(expected.get(key), str) or expected[key] != str(getattr(record, key, "") or ""):
            raise ScopedRetrievalError("source_changed")
    if not expected.get("evidence_text_hash"):
        raise ScopedRetrievalError("source_changed")


def read_citation_content(pipeline, record, ctx, locator: dict, *, expected_evidence_text_hash: str | None = None) -> str | None:
    content = _read_citation_content(pipeline, record, ctx, locator)
    if content is not None and expected_evidence_text_hash is not None:
        if sha256(content.encode("utf-8")).hexdigest() != expected_evidence_text_hash:
            raise ScopedRetrievalError("source_changed")
    return content


def _read_citation_content(pipeline, record, ctx, locator: dict) -> str | None:
    if record.processor_kind == "ragflow":
        result = pipeline.get_parse_result(record.kb_name, f"ragflow:{record.id}", ctx=ctx)
        chunk_id = str(locator.get("chunk_id") or "")
        if result is None or not chunk_id:
            return None
        return next((chunk.content for chunk in result.chunks
                     if str(chunk.metadata.get("ragflow_chunk_id") or chunk.metadata.get("chunk_id") or "") == chunk_id), None)
    if record.processor_kind == "spreadsheet_table":
        service = getattr(pipeline, "spreadsheet_service", None)
        if service is None:
            raise ScopedRetrievalError("filter_unsupported")
        path = service.db_path(record.department_id, record.kb_name, create=False)
        sheet = locator.get("sheet_name")
        if not isinstance(sheet, str) or not sheet:
            return None
        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                meta = connection.execute("SELECT content_hash FROM table_documents WHERE record_id = ?", (record.id,)).fetchone()
                if meta is None:
                    return None
                if meta["content_hash"] != record.content_hash:
                    raise ScopedRetrievalError("source_changed")
                cell = locator.get("cell_ref")
                if isinstance(cell, str) and cell:
                    row = connection.execute("SELECT header, value FROM table_cells WHERE record_id = ? AND sheet_name = ? AND cell_ref = ?",
                                             (record.id, sheet, cell)).fetchone()
                    return f"{row['header'] or 'Cell'}: {row['value']}" if row else None
                row_index = locator.get("row_index")
                if isinstance(row_index, bool) or not isinstance(row_index, int):
                    return None
                row = connection.execute("SELECT semantic_text, raw_text FROM table_semantic_rows WHERE record_id = ? AND sheet_name = ? AND row_index = ?",
                                         (record.id, sheet, row_index)).fetchone()
                return str(row["semantic_text"] or row["raw_text"]) if row else None
        except sqlite3.Error as exc:
            raise ScopedRetrievalError("source_unavailable") from exc
    if record.processor_kind == "circuit_design":
        service = getattr(pipeline, "circuit_service", None)
        circuit_id = locator.get("circuit_id") or locator.get("design_id")
        if service is None or not isinstance(circuit_id, str) or not circuit_id:
            return None
        meta = service._read_metadata(record.kb_name, circuit_id)
        if str(meta.get("record_id") or "") != str(record.id) or str(meta.get("department_id") or "") != record.department_id:
            return None
        if not meta.get("source_content_hash"):
            raise ScopedRetrievalError("filter_unsupported")
        if meta["source_content_hash"] != record.content_hash:
            raise ScopedRetrievalError("source_changed")
        design = service.store.load(record.kb_name, circuit_id)
        if design is None:
            return None
        entity = str(locator.get("entity_id") or "")
        entities = [*design.instances, *design.nets, *design.modules]
        matches = [item for item in entities if entity in {
            str(getattr(item, "refdes", "")), str(getattr(item, "name", "")), str(getattr(item, "module_id", "")),
        }]
        if not entity or not matches:
            return None
        return "\n".join(json.dumps(asdict(item), ensure_ascii=False) for item in matches)
    if record.processor_kind == "external_conversation":
        conversation_id = locator.get("conversation_id")
        if str(conversation_id or "") != record.document_id:
            return None
        detail = pipeline.get_external_conversation(record.kb_name, record.document_id, ctx=ctx)
        if detail is None:
            return None
        if detail.get("content_hash") != record.content_hash:
            raise ScopedRetrievalError("source_changed")
        turn_index = locator.get("turn_index")
        turns = detail.get("turns") or []
        if isinstance(turn_index, bool) or not isinstance(turn_index, int) or not 0 <= turn_index < len(turns):
            return None
        return str(turns[turn_index].get("content") or "")
    raise ScopedRetrievalError("filter_unsupported")
