"""Background parse job execution (design §6/§7/§9/§10).

``AttachmentParseExecutor`` routes each asset through
``AttachmentProcessorRouter`` to a deterministic local processor:

- ``LocalDocumentProcessor``      pdf / docx / txt / md -> canonical text parts
- ``SpreadsheetProcessor``        xlsx -> existing ``SpreadsheetPipeline`` with an
                                  attachment-scoped ``TableIndexStore`` (no parser copy)
- ``CircuitProcessor``            edf/edif -> existing ``EdfParser`` + a dedicated
                                  attachment-root ``CircuitStore`` (never a fake kb_name)

Only ``job_type=parse`` is accepted; unknown types fail closed.
"""

from __future__ import annotations

import hashlib
import os
import re
import time

import src.settings
from src.attachments import storage
from src.attachments.local_document_parser import LocalDocumentParser
from src.attachments.models import (
    AttachmentAsset,
    AttachmentJob,
    PARSE_STATUS_FAILED,
    PARSE_STATUS_READY,
    PART_TYPE_TEXT,
)
from src.attachments.router import AttachmentProcessorRouter, ParseOutcome
from src.attachments.store import AttachmentStore
from src.pipelines.spreadsheet.xlsx_parser import XlsxParseLimits


def _attachment_kb_name(asset: AttachmentAsset) -> str:
    """A safe, flat KB label used only inside attachment-scoped stores."""
    return f"att_{re.sub(r'[^A-Za-z0-9]', '', asset.asset_id)[:16]}"


def _attachment_record_id(asset: AttachmentAsset) -> int:
    """Stable integer record id for xlsx index tables (sha-derived, no registry)."""
    return int(hashlib.sha256(asset.asset_id.encode()).hexdigest()[:12], 16)


def _attachment_department(asset: AttachmentAsset) -> str:
    return f"session_{asset.session_id}"


class LocalDocumentProcessor:
    kind = "local_document"

    def __init__(self):
        self._parser = LocalDocumentParser()

    def parse(self, asset: AttachmentAsset, source_path: str) -> ParseOutcome:
        return self._parser.parse(asset, source_path)


class SpreadsheetProcessor:
    """Reuse the KB xlsx parser + index with an attachment-scoped store."""

    kind = "spreadsheet"

    def parse(self, asset: AttachmentAsset, source_path: str) -> ParseOutcome:
        from src.pipelines.spreadsheet.pipeline import SpreadsheetIndexRequest, SpreadsheetPipeline
        from src.pipelines.spreadsheet.table_store import TableIndexStore

        index_dir = storage.asset_table_index_dir(asset.session_id, asset.asset_id, create=True)
        db_path = os.path.join(index_dir, "table_indexes.db")
        pipeline = SpreadsheetPipeline(TableIndexStore(db_path=db_path))
        request = SpreadsheetIndexRequest(
            record_id=_attachment_record_id(asset),
            kb_name=_attachment_kb_name(asset),
            department_id=_attachment_department(asset),
            document_name=f"asset{asset.extension}",
            source_group="chat_attachment",
            file_path=source_path,
            local_path=source_path,
            content_hash=asset.sha256,
            parse_limits=XlsxParseLimits(
                max_rows=src.settings.CHAT_ATTACHMENT_MAX_ROWS,
                max_cells=src.settings.CHAT_ATTACHMENT_MAX_CELLS,
                max_sheets=src.settings.CHAT_ATTACHMENT_MAX_SHEETS,
                max_shared_strings=src.settings.CHAT_ATTACHMENT_MAX_SHARED_STRINGS,
                max_cell_text_length=src.settings.CHAT_ATTACHMENT_MAX_CELL_TEXT_LENGTH,
                max_uncompressed_bytes=src.settings.CHAT_ATTACHMENT_MAX_UNCOMPRESSED_BYTES,
            ),
        )
        result = pipeline.parse_and_index(request)
        text_parts = self._document_text_parts(asset, db_path)
        index_key = storage.to_storage_key(db_path)
        if not result.ok:
            return ParseOutcome(
                ok=False,
                parse_status=PARSE_STATUS_FAILED,
                parts=[],
                manifest={"index_db_path": index_key},
                error_code="spreadsheet_index_failed",
                error_message=result.message,
            )
        return ParseOutcome(
            ok=True,
            parse_status=PARSE_STATUS_READY,
            parts=text_parts,
            manifest={
                "format": "xlsx",
                "message": result.message,
                "index_db_path": index_key,
                "record_id": request.record_id,
                "kb_name": request.kb_name,
                "department_id": request.department_id,
                "warnings": list(result.warnings or []),
                "stats": {
                    "sheet_count": getattr(result.stats, "sheet_count", 0),
                    "row_count": getattr(result.stats, "row_count", 0),
                    "cell_count": getattr(result.stats, "cell_count", 0),
                },
            },
        )

    def _document_text_parts(self, asset: AttachmentAsset, db_path: str) -> list:
        """Small lexical preview so sparse search can hit spreadsheets too."""
        from src.attachments.models import AttachmentPart
        from src.pipelines.spreadsheet.table_store import TableIndexStore

        parts: list[AttachmentPart] = []
        try:
            store = TableIndexStore(db_path=db_path)
            profile = store.get_document_profile(_attachment_record_id(asset))
        except Exception:
            profile = None
        summary_lines: list[str] = []
        if isinstance(profile, dict):
            name = str(profile.get("document_name") or "")
            if name:
                summary_lines.append(f"文档: {name}")
            sheets = profile.get("sheets") or profile.get("sheet_names") or []
            if isinstance(sheets, list) and sheets:
                summary_lines.append("工作表: " + ", ".join(str(s) for s in sheets[:20]))
        if summary_lines:
            parts.append(
                AttachmentPart(
                    part_id="",
                    asset_id=asset.asset_id,
                    ordinal=0,
                    part_type=PART_TYPE_TEXT,
                    text_content="\n".join(summary_lines),
                    locator={"sheet": 0},
                    metadata={"format": "xlsx", "kind": "preview"},
                    content_hash=hashlib.sha256("\n".join(summary_lines).encode()).hexdigest(),
                )
            )
        return parts


class CircuitProcessor:
    """Reuse EdfParser + CircuitIndexService with an attachment-rooted store."""

    kind = "circuit"

    def parse(self, asset: AttachmentAsset, source_path: str) -> ParseOutcome:
        from src.circuit.index_service import CircuitIndexService
        from src.circuit.store import CircuitStore

        circuit_root = storage.asset_circuit_root(asset.session_id)
        service = CircuitIndexService(store=CircuitStore(root=circuit_root))
        result = service.index_file(
            kb_name=_attachment_kb_name(asset),
            record_id=None,
            file_path=source_path,
            original_name=f"asset{asset.extension}",
        )
        manifest = {
            "format": asset.extension.lstrip("."),
            "design_id": result.design_id,
            "kb_name": _attachment_kb_name(asset),
            "circuit_root": storage.to_storage_key(circuit_root),
            "message": result.message,
            "warnings": list(result.warnings or []),
        }
        if not result.ok:
            return ParseOutcome(
                ok=False,
                parse_status=PARSE_STATUS_FAILED,
                parts=[],
                manifest=manifest,
                error_code="circuit_index_failed",
                error_message=result.message,
            )
        return ParseOutcome(
            ok=True,
            parse_status=PARSE_STATUS_READY,
            parts=[],
            manifest=manifest,
        )


def default_router() -> AttachmentProcessorRouter:
    """Extension routing table (design §6). XLS is rejected fail-closed."""
    return AttachmentProcessorRouter(
        processors={
            ".pdf": LocalDocumentProcessor(),
            ".docx": LocalDocumentProcessor(),
            ".txt": LocalDocumentProcessor(),
            ".md": LocalDocumentProcessor(),
            ".xlsx": SpreadsheetProcessor(),
            ".xlsm": SpreadsheetProcessor(),
            ".edf": CircuitProcessor(),
            ".edif": CircuitProcessor(),
        }
    )


class AttachmentParseExecutor:
    """Claim -> route -> parse -> persist parts -> index -> resolve waiters."""

    def __init__(
        self,
        store: AttachmentStore | None = None,
        router: AttachmentProcessorRouter | None = None,
    ):
        self.store = store or AttachmentStore()
        self.router = router or default_router()

    def process_asset(
        self,
        asset_id: str,
        *,
        parser_version: str | None = None,
        deadline: float | None = None,
    ) -> dict:
        """Parse one asset end-to-end; returns the outcome summary."""
        def timed_out() -> bool:
            return deadline is not None and time.monotonic() >= deadline

        asset = self.store.get_asset(asset_id)
        if asset is None:
            return {"ok": False, "error_code": "asset_not_found", "error_message": "asset not found"}
        if timed_out():
            return {
                "ok": False,
                "parse_status": PARSE_STATUS_FAILED,
                "error_code": "parse_timeout",
                "error_message": "attachment parse deadline exceeded",
            }
        try:
            processor = self.router.resolve(asset.extension)
        except Exception as exc:
            self.store.update_asset_status(
                asset_id,
                parse_status=PARSE_STATUS_FAILED,
                parser_version=parser_version or "",
                error_code="unsupported_attachment_type",
                error_message=str(exc)[:500],
            )
            return {"ok": False, "error_code": "unsupported_attachment_type", "error_message": str(exc)}

        version = parser_version or _current_parser_version(processor)
        try:
            source_path = storage.resolve_storage_key(asset.storage_key)
        except Exception as exc:  # noqa: BLE001 - persist storage failures
            message = str(exc)[:500] or "attachment source is unavailable"
            self.store.update_asset_status(
                asset_id,
                parse_status=PARSE_STATUS_FAILED,
                parser_version=version,
                error_code="source_unavailable",
                error_message=message,
            )
            return {
                "ok": False,
                "parse_status": PARSE_STATUS_FAILED,
                "error_code": "source_unavailable",
                "error_message": message,
            }
        try:
            outcome = processor.parse(asset, source_path)
        except Exception as exc:  # noqa: BLE001 - persist parser failures
            message = str(exc)[:500] or "attachment parser failed"
            self.store.update_asset_status(
                asset_id,
                parse_status=PARSE_STATUS_FAILED,
                parser_version=version,
                error_code="parse_failed",
                error_message=message,
            )
            return {
                "ok": False,
                "parse_status": PARSE_STATUS_FAILED,
                "error_code": "parse_failed",
                "error_message": message,
            }
        if timed_out():
            return {
                "ok": False,
                "parse_status": PARSE_STATUS_FAILED,
                "error_code": "parse_timeout",
                "error_message": "attachment parse deadline exceeded",
            }
        if not outcome.ok:
            self.store.update_asset_status(
                asset_id,
                parse_status=PARSE_STATUS_FAILED,
                parser_version=version,
                manifest=outcome.manifest,
                error_code=outcome.error_code or "parse_failed",
                error_message=outcome.error_message or "parse failed",
            )
            return {
                "ok": False,
                "parse_status": PARSE_STATUS_FAILED,
                "error_code": outcome.error_code,
                "error_message": outcome.error_message,
            }

        manifest = dict(outcome.manifest or {})
        if outcome.degraded_reasons:
            manifest["degraded_reasons"] = list(outcome.degraded_reasons)
        if outcome.parts:
            self.store.replace_parts(asset_id, outcome.parts)
            # ``replace_parts`` assigns durable part ids in SQLite; re-read
            # them before populating FTS so trigram joins retain locators and
            # metadata instead of indexing transient empty ids.
            indexed_parts = self.store.list_parts(asset_id)
        else:
            indexed_parts = []
        self._reindex_asset(asset_id, indexed_parts)
        self.store.update_asset_status(
            asset_id,
            parse_status=outcome.parse_status,
            parser_version=version,
            manifest=manifest,
        )
        return {
            "ok": True,
            "parse_status": outcome.parse_status,
            "part_count": len(outcome.parts),
            "degraded_reasons": outcome.degraded_reasons,
            "manifest": manifest,
        }

    def _reindex_asset(self, asset_id: str, parts: list) -> None:
        from src.attachments.index import AttachmentIndex

        try:
            AttachmentIndex().replace_asset(asset_id, parts)
        except Exception:
            # Index failures degrade retrieval but must not corrupt parse state.
            pass


def _current_parser_version(processor) -> str:
    from src.attachments.models import PARSER_VERSION

    return f"{PARSER_VERSION}:{processor.kind}"


def run_job(job: AttachmentJob, executor: AttachmentParseExecutor | None = None) -> dict:
    """Execute one claimed parse job (worker-facing entry point)."""
    executor = executor or AttachmentParseExecutor()
    return executor.process_asset(job.asset_id)
