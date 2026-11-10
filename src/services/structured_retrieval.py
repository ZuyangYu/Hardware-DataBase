"""Strict, single-KB structured adapters consumed by controlled fanout.

The ledger and the processing product must agree before and after a query.
No adapter may emulate a frozen filter by retrieving an entire KB and then
discarding records: tools receive the exact record identity before ranking.
"""

from __future__ import annotations

import sqlite3
from hashlib import sha256
from contextlib import closing
from dataclasses import asdict
from typing import Callable
import inspect

from src.agents.schemas import Evidence as AgentEvidence
from src.agents.tools.circuit_tools import ALLOWED_QUERY_OPERATIONS, CircuitQueryTool
from src.agents.tools.spreadsheet_tools import SpreadsheetCellTool, SpreadsheetSemanticTool
import src.settings as settings
from src.pipelines.document_rag.schemas import (
    DocumentRetrievalContract, Evidence, ScopedDocumentRetrievalOutcome,
    ScopedRetrievalError, TASK_STATUS_COMPLETED, normalize_parse_status,
)
from src.services.kb_scope import kb_scope_from_context
from src.services.evidence_fusion import EvidenceIdentityIncomplete, evidence_identity
from src.services.pipeline_directories import document_relative_path


def _check_time(should_cancel, remaining_seconds):
    if should_cancel is not None and should_cancel():
        raise ScopedRetrievalError("cancelled")
    if remaining_seconds is not None and remaining_seconds() <= 0:
        raise ScopedRetrievalError("timeout")


class RecordScopedAdapter:
    def __init__(self, *, document_store, processor_kind: str, query: Callable, validate_index: Callable,
                 query_batch: Callable | None = None, validate_batch: Callable | None = None):
        self.document_store = document_store
        self.processor_kind = processor_kind
        self.query = query
        self.validate_index = validate_index
        self.query_batch = query_batch
        self.validate_batch = validate_batch
        try:
            parameters = inspect.signature(query).parameters.values()
            self._query_accepts_locators = any(
                parameter.name == "locators" or parameter.kind == inspect.Parameter.VAR_POSITIONAL
                for parameter in parameters
            )
            self._validate_accepts_batch = any(
                parameter.name == "expected_stamps" or parameter.kind == inspect.Parameter.VAR_POSITIONAL
                for parameter in inspect.signature(validate_index).parameters.values()
            )
        except (TypeError, ValueError):
            self._query_accepts_locators = False
            self._validate_accepts_batch = False

    def _records(self, kb_name, ctx, contract, *, validate=True):
        scope = kb_scope_from_context(kb_name, ctx).require_department("retrieve structured evidence from")
        if not ctx.has_kb_permission(kb_name, "read"):
            raise PermissionError("read permission required")
        if scope.kb_id is None or contract.mode not in {"chat", "frozen"}:
            raise ScopedRetrievalError("source_unavailable", "source_identity_missing")
        if contract.processor_kind not in (None, self.processor_kind):
            raise ScopedRetrievalError("source_unavailable", "processor_kind_mismatch")
        allowed = contract.allowed_record_ids
        if contract.mode == "frozen" and (not allowed or set(contract.expected_source_stamps) != set(allowed)):
            raise ScopedRetrievalError("source_unavailable", "incomplete_frozen_source_set")
        all_records = {
            record.id: record for record in self.document_store.list_documents(kb_name, scope.department_id)
            if allowed is None or record.id in allowed
        }
        if allowed is not None and set(all_records) != set(allowed):
            raise ScopedRetrievalError("source_unavailable", "source_record_missing")
        records = {
            record.id: record for record in self.document_store.list_documents(kb_name, scope.department_id)
            if record.processor_kind == self.processor_kind
            and normalize_parse_status(record.status, record.processor_kind) == TASK_STATUS_COMPLETED
            and (allowed is None or record.id in allowed)
        }
        selected_processor_ids = {
            record_id for record_id, record in all_records.items()
            if record.processor_kind == self.processor_kind
        }
        if allowed is not None and not contract.processor_kind and set(records) != set(allowed):
            raise ScopedRetrievalError("source_unavailable", "source_record_missing")
        if contract.processor_kind and selected_processor_ids - set(records):
            raise ScopedRetrievalError("source_unavailable", "source_record_missing")
        for record_id, record in records.items():
            if record.kb_id not in (0, None, scope.kb_id):
                raise ScopedRetrievalError("source_unavailable", "source_identity_mismatch")
            stamp = contract.expected_source_stamps.get(record_id)
            if stamp is not None:
                if stamp.kb_id != scope.kb_id or stamp.local_record_id != record_id:
                    raise ScopedRetrievalError("source_unavailable", "source_identity_mismatch")
                expected = {
                    "content_hash": stamp.source_content_hash, "document_id": stamp.remote_document_id,
                    "index_revision": stamp.index_revision, "source_version_id": stamp.source_version_id,
                    "revision": stamp.revision, "processing_artifact_id": stamp.processing_artifact_id,
                    "processor_version": stamp.processor_version,
                }
                if any(str(getattr(record, key, 0 if key == "index_revision" else "") or "") != str(value or "") for key, value in expected.items()):
                    raise ScopedRetrievalError("source_changed")
            if validate:
                self.validate_index(record, scope)
        return scope, records

    def _validate_many(self, records, scope, contract, *, validate=True):
        if not validate:
            return
        if self.validate_batch is not None:
            self.validate_batch(list(records.values()), scope, contract.expected_source_stamps)
            return
        for record in records.values():
            self.validate_index(record, scope)

    def _evidence_for_item(self, item, record, scope, ctx):
        if str(item.locator.get("record_id") or "") != str(record.id):
            raise ScopedRetrievalError("source_unavailable", "source_identity_mismatch")
        metadata = dict(item.metadata)
        metadata.update({
            "tenant_key": str(getattr(ctx, "tenant_id", None) or "default"),
            "kb_id": scope.kb_id, "kb_name": scope.kb_name,
            "department_id": scope.department_id, "local_record_id": record.id,
            "source_content_hash": record.content_hash,
            "remote_document_id": record.document_id, "source_version_id": record.source_version_id,
            "revision": record.revision, "approval_status": record.approval_status,
            "project_id": record.project_id, "processor_kind": self.processor_kind,
            "content_kind": item.content_kind, "chunk_or_structured_locator": dict(item.locator),
            "locator": dict(item.locator),
            "directory_id": int(getattr(record, "directory_id", 0) or 0),
            "relative_path": document_relative_path(self.document_store, record),
            "index_revision": int(getattr(record, "index_revision", 0) or 0),
            "processing_artifact_id": str(getattr(record, "processing_artifact_id", "") or ""),
            "processor_version": str(getattr(record, "processor_version", "") or ""),
            "evidence_text_hash": sha256(item.content.encode("utf-8")).hexdigest(),
        })
        try:
            identity = evidence_identity(metadata)
        except EvidenceIdentityIncomplete as exc:
            raise ScopedRetrievalError("source_unavailable", "evidence_identity_incomplete") from exc
        return Evidence(id=identity, content=item.content,
                        source_name=document_relative_path(self.document_store, record),
                        score=item.score, metadata=metadata, backend="local_structured",
                        retriever=str(item.metadata.get("tool") or self.processor_kind))

    def retrieve_batch_with_scope(self, kb_name, query, *, ctx, contract: DocumentRetrievalContract,
                                  top_k=8, should_cancel=None, before_request=None, remaining_seconds=None,
                                  locators_by_record=None):
        """One budgeted call and one validation pair per KB batch."""
        try:
            _check_time(should_cancel, remaining_seconds)
            scope, records = self._records(kb_name, ctx, contract, validate=False)
            if self.query_batch is None:
                raise ScopedRetrievalError("filter_unsupported", "batch_query_unsupported")
            per_record = max(1, int(getattr(settings, "MULTI_KB_PER_RECORD_CANDIDATES", top_k)))
            per_source = max(1, int(getattr(settings, "MULTI_KB_PER_SOURCE_CANDIDATES", top_k)))
            output_limit = max(1, min(int(top_k), 20))
            self._validate_many(records, scope, contract)
            if before_request is not None:
                before_request()
            items = self.query_batch(
                list(records.values()), query, ctx,
                dict(locators_by_record or {}), min(200, max(output_limit, per_record * len(records))),
            )
            evidence = [self._evidence_for_item(item, records[int(item.locator["record_id"])], scope, ctx)
                        for item in items]
            _check_time(should_cancel, remaining_seconds)
            _, current = self._records(kb_name, ctx, contract, validate=False)
            if {key: asdict(value) for key, value in current.items()} != {key: asdict(value) for key, value in records.items()}:
                raise ScopedRetrievalError("source_changed")
            self._validate_many(current, scope, contract)
            evidence.sort(key=lambda item: (-item.score, item.id))
            candidate_count = len(evidence)
            evidence = evidence[:per_source]
            source_kept = len(evidence)
            evidence = evidence[:output_limit]
            return ScopedDocumentRetrievalOutcome(
                "success_with_hits" if evidence else "success_empty", evidence,
                diagnostics={"candidate_queries": 1, "validation_queries": 2, "records": len(records),
                             "per_record_limit": per_record, "per_source_limit": per_source,
                             "candidates": candidate_count, "source_kept": source_kept,
                             "dropped_by_source_limit": max(0, candidate_count - source_kept),
                             "dropped_by_output_limit": max(0, source_kept - len(evidence))},
            )
        except ScopedRetrievalError as exc:
            return ScopedDocumentRetrievalOutcome(exc.status, error_code=exc.error_code,
                                                  diagnostics={"candidate_queries": 0, "validation_queries": 0,
                                                               "records": 0})
        except PermissionError:
            return ScopedDocumentRetrievalOutcome("access_denied", error_code="access_denied")
        except (TimeoutError,):
            return ScopedDocumentRetrievalOutcome("timeout", error_code="timeout")
        except Exception as exc:
            return ScopedDocumentRetrievalOutcome("retrieval_failed", error_code="retrieval_failed",
                                                  diagnostics={"error_type": type(exc).__name__, "error": str(exc)[:300]})

    def retrieve_with_scope(self, kb_name, query, *, ctx, contract: DocumentRetrievalContract,
                            top_k=8, should_cancel=None, before_request=None, remaining_seconds=None,
                            locators_by_record=None):
        try:
            _check_time(should_cancel, remaining_seconds)
            scope, records = self._records(kb_name, ctx, contract)
            per_record = max(1, int(getattr(settings, "MULTI_KB_PER_RECORD_CANDIDATES", top_k)))
            per_source = max(1, int(getattr(settings, "MULTI_KB_PER_SOURCE_CANDIDATES", top_k)))
            output_limit = max(1, min(int(top_k), 20))
            evidence = []
            for record in records.values():
                _check_time(should_cancel, remaining_seconds)
                if before_request is not None:
                    before_request()
                locators = tuple((locators_by_record or {}).get(record.id) or ())
                query_kwargs = {"locators": locators} if self._query_accepts_locators else {}
                try:
                    items = self.query(record, query, ctx, per_record, **query_kwargs)
                except TypeError as exc:
                    # A test or legacy caller may replace ``query`` after
                    # construction with the old four-argument signature.
                    if query_kwargs and "locators" in str(exc):
                        items = self.query(record, query, ctx, per_record)
                    else:
                        raise
                for item in items:
                    if str(item.locator.get("record_id") or "") != str(record.id):
                        raise ScopedRetrievalError("source_unavailable", "source_identity_mismatch")
                    metadata = dict(item.metadata)
                    metadata.update({
                        "tenant_key": str(ctx.tenant_id or "default"),
                        "kb_id": scope.kb_id, "kb_name": scope.kb_name,
                        "department_id": scope.department_id, "local_record_id": record.id,
                        "source_content_hash": record.content_hash,
                        "remote_document_id": record.document_id, "source_version_id": record.source_version_id,
                        "revision": record.revision, "approval_status": record.approval_status,
                        "project_id": record.project_id, "processor_kind": self.processor_kind,
                        "content_kind": item.content_kind, "chunk_or_structured_locator": dict(item.locator),
                        "locator": dict(item.locator),
                        "directory_id": int(getattr(record, "directory_id", 0) or 0),
                        "relative_path": document_relative_path(self.document_store, record),
                        "index_revision": int(getattr(record, "index_revision", 0) or 0),
                        "processing_artifact_id": str(getattr(record, "processing_artifact_id", "") or ""),
                        "processor_version": str(getattr(record, "processor_version", "") or ""),
                        "evidence_text_hash": sha256(item.content.encode("utf-8")).hexdigest(),
                    })
                    try:
                        identity = evidence_identity(metadata)
                    except EvidenceIdentityIncomplete as exc:
                        raise ScopedRetrievalError("source_unavailable", "evidence_identity_incomplete") from exc
                    evidence.append(
                        Evidence(
                            id=identity,
                            content=item.content,
                            source_name=document_relative_path(self.document_store, record),
                            score=item.score,
                            metadata=metadata,
                            backend="local_structured",
                            retriever=str(item.metadata.get("tool") or self.processor_kind),
                        )
                    )
            _check_time(should_cancel, remaining_seconds)
            _, current = self._records(kb_name, ctx, contract)
            if {key: asdict(value) for key, value in current.items()} != {key: asdict(value) for key, value in records.items()}:
                raise ScopedRetrievalError("source_changed")
            evidence.sort(key=lambda item: (-item.score, item.id))
            candidate_count = len(evidence)
            evidence = evidence[:per_source]
            source_kept = len(evidence)
            evidence = evidence[:output_limit]
            return ScopedDocumentRetrievalOutcome(
                "success_with_hits" if evidence else "success_empty", evidence,
                diagnostics={"per_record_limit": per_record, "per_source_limit": per_source,
                             "candidates": candidate_count, "source_kept": source_kept,
                             "dropped_by_source_limit": max(0, candidate_count - source_kept),
                             "dropped_by_output_limit": max(0, source_kept - len(evidence))},
            )
        except ScopedRetrievalError as exc:
            return ScopedDocumentRetrievalOutcome(exc.status, error_code=exc.error_code)
        except PermissionError:
            return ScopedDocumentRetrievalOutcome("access_denied", error_code="access_denied")
        except (TimeoutError,):
            return ScopedDocumentRetrievalOutcome("timeout", error_code="timeout")
        except Exception as exc:
            return ScopedDocumentRetrievalOutcome("retrieval_failed", error_code="retrieval_failed", diagnostics={"error_type": type(exc).__name__, "error": str(exc)[:300]})


class WikiScopedAdapter:
    def __init__(self, service):
        self.service = service

    def retrieve_with_scope(self, kb_name, query, *, ctx, contract, top_k=8,
                            should_cancel=None, before_request=None, remaining_seconds=None,
                            locators_by_record=None):
        # Wiki revisions do not freeze the raw documents from which they were
        # distilled. Treating a page revision as a frozen original is unsafe.
        if contract.mode == "frozen" or contract.allowed_record_ids is not None:
            return ScopedDocumentRetrievalOutcome("filter_unsupported", error_code="wiki_frozen_provenance_unsupported")
        try:
            _check_time(should_cancel, remaining_seconds)
            scope = kb_scope_from_context(kb_name, ctx).require_department()
            if scope.kb_id is None or not ctx.has_kb_permission(kb_name, "read"):
                raise PermissionError()
            # Page versions and doc-id/name references cannot bind the raw
            # source hash or processing generation required by evidence fusion.
            # Refuse explicitly instead of reporting hits that fusion discards.
            return ScopedDocumentRetrievalOutcome(
                "filter_unsupported", error_code="wiki_source_provenance_unsupported",
            )
        except ScopedRetrievalError as exc:
            return ScopedDocumentRetrievalOutcome(exc.status, error_code=exc.error_code)
        except PermissionError:
            return ScopedDocumentRetrievalOutcome("access_denied", error_code="access_denied")
        except Exception as exc:
            return ScopedDocumentRetrievalOutcome("retrieval_failed", error_code="retrieval_failed", diagnostics={"error_type": type(exc).__name__, "error": str(exc)[:300]})


def _locator_filters(locator) -> dict:
    if locator is None:
        return {}
    return {
        key: value for key, value in {
            "sheet_name": getattr(locator, "sheet_name", None),
            "row_index": getattr(locator, "row_index", None),
            "cell_ref": getattr(locator, "cell_ref", None),
            "pin_value": getattr(locator, "pin_value", None),
            "pin_field": getattr(locator, "pin_field", None),
        }.items() if value not in (None, "")
    }


def _query_spreadsheet(tool, record, query, ctx, top_k, locators=()):
    merged = {}
    for locator in (tuple(locators) or (None,)):
        filters = {"record_id": record.id, **_locator_filters(locator)}
        for item in tool.run(query, record.kb_name, ctx, top_k, filters=filters):
            merged[item.id] = item
    return sorted(merged.values(), key=lambda item: (-float(item.score or 0.0), item.id))[:top_k]


def build_structured_retrieval_adapters(*, document_store, spreadsheet_service=None,
                                      circuit_service=None, conversation_service=None, wiki_service=None):
    adapters = {}
    if spreadsheet_service is not None:
        def validate_spreadsheet(record, scope):
            path = spreadsheet_service.db_path(scope.department_id, scope.kb_name, create=False)
            try:
                with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
                    row = connection.execute("SELECT kb_id, kb_name, department_id, content_hash FROM table_documents WHERE record_id = ?", (record.id,)).fetchone()
            except sqlite3.Error as exc:
                raise ScopedRetrievalError("source_unavailable", "structured_index_missing") from exc
            if row is None:
                raise ScopedRetrievalError("source_unavailable", "structured_record_missing")
            if row[0] not in (0, None, scope.kb_id) or row[1] != scope.kb_name or str(row[2]) != scope.department_id:
                raise ScopedRetrievalError("source_unavailable", "source_identity_mismatch")
            if not row[3] or row[3] != record.content_hash:
                raise ScopedRetrievalError("source_changed")

        def validate_spreadsheet_batch(records, scope, expected_stamps=None):
            ids = sorted({int(record.id) for record in records})
            if not ids:
                raise ScopedRetrievalError("source_unavailable", "structured_record_missing")
            path = spreadsheet_service.db_path(scope.department_id, scope.kb_name, create=False)
            placeholders = ",".join("?" for _ in ids)
            try:
                with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
                    rows = connection.execute(
                        f"SELECT record_id, kb_id, kb_name, department_id, content_hash "
                        f"FROM table_documents WHERE record_id IN ({placeholders})",
                        ids,
                    ).fetchall()
            except sqlite3.Error as exc:
                raise ScopedRetrievalError("source_unavailable", "structured_index_missing") from exc
            indexed = {int(row[0]): row for row in rows}
            if set(indexed) != set(ids):
                raise ScopedRetrievalError("source_unavailable", "structured_record_missing")
            for record in records:
                row = indexed[int(record.id)]
                if row[1] not in (0, None, scope.kb_id) or row[2] != scope.kb_name or str(row[3]) != scope.department_id:
                    raise ScopedRetrievalError("source_unavailable", "source_identity_mismatch")
                if not row[4] or row[4] != record.content_hash:
                    raise ScopedRetrievalError("source_changed")
                stamp = (expected_stamps or {}).get(record.id)
                if stamp is not None and stamp.source_content_hash != record.content_hash:
                    raise ScopedRetrievalError("source_changed")

        for key, tool in (("spreadsheet_semantic", SpreadsheetSemanticTool(spreadsheet_service)),
                          ("spreadsheet_cell", SpreadsheetCellTool(spreadsheet_service))):
            adapters[key] = RecordScopedAdapter(
                document_store=document_store, processor_kind="spreadsheet_table",
                query=lambda record, query, ctx, top_k, locators=(), tool=tool: _query_spreadsheet(
                    tool, record, query, ctx, top_k, locators),
                validate_index=validate_spreadsheet,
                query_batch=(lambda records, query, ctx, locators, limit, tool=tool:
                             tool.run_batch(query, records[0].kb_name, ctx,
                                            [record.id for record in records], locators, limit)),
                validate_batch=validate_spreadsheet_batch,
            )
    if circuit_service is not None:
        def validate_circuit(record, scope):
            matches = [circuit_service._read_metadata(scope.kb_name, design.design_id)
                       for design in circuit_service.store.list_designs(scope.kb_name)
                       if circuit_service._read_metadata(scope.kb_name, design.design_id).get("record_id") == record.id]
            if len(matches) != 1 or str(matches[0].get("department_id") or "") != scope.department_id:
                raise ScopedRetrievalError("source_unavailable", "structured_record_missing")
            digest = matches[0].get("source_content_hash")
            if not digest:
                raise ScopedRetrievalError("filter_unsupported", "circuit_source_hash_missing")
            if digest != record.content_hash:
                raise ScopedRetrievalError("source_changed")

        circuit_tool = CircuitQueryTool(circuit_service)
        for operation in ALLOWED_QUERY_OPERATIONS:
            key = "circuit" if operation == "auto" else f"circuit_{operation}"
            adapters[key] = RecordScopedAdapter(document_store=document_store, processor_kind="circuit_design",
                query=lambda record, query, ctx, top_k, locators=(), operation=operation: circuit_tool.run(query, record.kb_name, ctx, top_k,
                    filters={"record_id": record.id, "query_operation": operation}), validate_index=validate_circuit)
    if conversation_service is not None:
        def validate_conversation(record, scope):
            meta = conversation_service.get_conversation(scope.department_id, scope.kb_name, record.document_id)
            if meta is None:
                raise ScopedRetrievalError("source_unavailable", "structured_record_missing")
            if meta.get("content_hash") != record.content_hash:
                raise ScopedRetrievalError("source_changed")

        def query_conversation(record, query, ctx, top_k, locators=()):
            scope = kb_scope_from_context(record.kb_name, ctx)
            rows = conversation_service.search_by_scope(scope.department_id, scope.kb_name, query,
                                                       top_k=top_k, conversation_ids=(record.document_id,))
            return [AgentEvidence(id=f"conv:{row['conversation_id']}:{row['message_id']}", content=row["content"],
                    source_name=record.document_name, processor_kind="external_conversation", content_kind="external_conversation",
                    score=1.0, locator={"record_id": record.id, "conversation_id": row["conversation_id"],
                    "message_id": row["message_id"], "turn_index": row["turn_index"], "start_offset": row["start_offset"]}) for row in rows]

        adapters["conversation"] = RecordScopedAdapter(document_store=document_store, processor_kind="external_conversation",
            query=query_conversation, validate_index=validate_conversation)
    if wiki_service is not None:
        adapters["wiki"] = WikiScopedAdapter(wiki_service)
    return adapters
