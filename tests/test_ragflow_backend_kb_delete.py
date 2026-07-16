import unittest

from src.pipelines.document_rag.ragflow_backend import RAGFlowAPIError, RAGFlowBackend
from src.pipelines.document_rag.schemas import BackendResult, RequestContext
from src.pipelines.document_store import PipelineDocumentRecord
from src.pipelines.ingestion import HandlerDeleteResult, HandlerProcessResult
from src.pipelines.registry import PROCESSOR_KIND_RAGFLOW, PROCESSOR_KIND_SPREADSHEET
from src.pipelines.runtime import PipelineRuntime


class _ForbiddenClient:
    def delete_documents(self, dataset_id, document_ids):
        raise AssertionError("KB deletion must dispatch through pipeline handlers")

    def stop_parse_documents(self, dataset_id, document_ids):
        raise AssertionError("Failed parse task cleanup must not require stopping remote parsing")


class _RetrieveClient:
    def __init__(self, chunks):
        self.chunks = chunks
        self.retrieve_calls = []

    def retrieve(self, question, dataset_ids, top_k, metadata_condition=None):
        self.retrieve_calls.append((question, dataset_ids, top_k, metadata_condition))
        return self.chunks


class _SequentialRetrieveClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.retrieve_calls = []

    def retrieve(self, question, dataset_ids, top_k, metadata_condition=None):
        self.retrieve_calls.append((question, dataset_ids, top_k, metadata_condition))
        return self.responses.pop(0) if self.responses else []


class _FailingListClient:
    def list_documents(self, dataset_id, document_id):
        raise RuntimeError("ragflow unavailable")


class _NotOwnerListClient:
    def list_documents(self, dataset_id, document_id):
        raise RAGFlowAPIError({"code": 102, "message": "You don't own the document remote-1."})


class _ForbiddenSpreadsheetIndexes:
    def delete_record(self, record):
        raise AssertionError("KB deletion must dispatch through pipeline handlers")

    def get_document_profile(self, record):
        return None


class _Archive:
    def __init__(self):
        self.removed = []

    def remove_record_archive(self, record):
        self.removed.append(record.id)

    def inspect_record_archive(self, record):
        return {}


class _Store:
    def __init__(self, records):
        self.records = records
        self.list_calls = []
        self.delete_kb_calls = []
        self.deleted_ids = []

    def list_documents(self, kb_name, department_id=None):
        self.list_calls.append((kb_name, department_id))
        return [
            record
            for record in self.records
            if record.kb_name == kb_name and (department_id is None or record.department_id == department_id)
        ]

    def get_document_by_id_scoped(self, record_id, department_id=None):
        for record in self.records:
            if record.id == record_id and (
                department_id in (None, "") or record.department_id == department_id
            ):
                return record
        return None

    def delete_documents_by_kb(self, kb_name, department_id=None):
        self.delete_kb_calls.append((kb_name, department_id))

    def delete_document_by_id(self, record_id):
        self.deleted_ids.append(record_id)


class _Handler:
    def __init__(self, processor_kind, errors=None):
        self.processor_kind = processor_kind
        self.errors = errors or []
        self.ok = True
        self.deleted = []
        self.processed = []
        self.cleaned_failed = []

    def delete_record(self, record, archive):
        self.deleted.append((record, archive))
        return HandlerDeleteResult(
            ok=self.ok,
            message=f"deleted {record.document_name}",
            errors=list(self.errors),
            audit_action=f"{self.processor_kind}_delete_document",
        )

    def process_record(self, record, archive):
        self.processed.append((record, archive))
        return HandlerProcessResult(
            ok=True,
            status="indexed",
            message=f"processed {record.document_name}",
            audit_action=f"{self.processor_kind}_processed",
            audit_metadata={"store_id": record.id, "processor_kind": record.processor_kind},
        )

    def cleanup_failed_process(self, record):
        self.cleaned_failed.append(record)


class _Ingestion:
    def __init__(self, handlers):
        self.handlers = handlers


def _record(record_id, name, processor_kind, department_id="dept_a", kb_name="kb"):
    return PipelineDocumentRecord(
        id=record_id,
        kb_name=kb_name,
        document_name=name,
        original_file_name=name,
        dataset_kind="table" if processor_kind == PROCESSOR_KIND_SPREADSHEET else "design",
        dataset_id="dataset-1",
        document_id=f"remote-{record_id}",
        source_group="docs",
        department_id=department_id,
        uploaded_by="user",
        status="indexed" if processor_kind == PROCESSOR_KIND_SPREADSHEET else "parsed",
        processor_kind=processor_kind,
    )


def _backend(records, handlers):
    backend = object.__new__(RAGFlowBackend)
    backend.name = "ragflow"
    backend.store = _Store(records)
    backend.archive = _Archive()
    backend.client = _ForbiddenClient()
    backend.spreadsheet_indexes = _ForbiddenSpreadsheetIndexes()
    backend.ingestion = _Ingestion(handlers)
    backend._audit = lambda *args, **kwargs: None
    backend.runtime = PipelineRuntime(
        store=backend.store,
        archive=backend.archive,
        ingestion=backend.ingestion,
        audit_callback=backend._audit,
    )
    backend._check_kb_access = lambda kb_name, ctx, required="read": None
    return backend


def _retrieve_backend(chunks, records=None):
    backend = object.__new__(RAGFlowBackend)
    backend.name = "ragflow"
    backend.client = _RetrieveClient(chunks)
    backend.store = _Store(records or [])
    backend._dataset_ids = {"governance": "dataset-g", "design": "dataset-d"}
    backend._check_kb_access = lambda kb_name, ctx, required="read": None
    return backend


class RAGFlowBackendKnowledgeBaseDeleteTests(unittest.TestCase):
    def test_remote_document_exists_fails_closed(self):
        backend = object.__new__(RAGFlowBackend)
        backend.client = _FailingListClient()
        record = _record(9, "stale.pdf", PROCESSOR_KIND_RAGFLOW)

        self.assertFalse(backend._remote_document_exists(record))

    def test_remote_document_exists_keeps_mapping_when_status_is_not_readable(self):
        backend = object.__new__(RAGFlowBackend)
        backend.client = _NotOwnerListClient()
        record = _record(9, "submitted.pdf", PROCESSOR_KIND_RAGFLOW)

        self.assertTrue(backend._remote_document_exists(record))

    def test_list_documents_hides_not_owner_status_as_non_error_note(self):
        record = _record(9, "submitted.pdf", PROCESSOR_KIND_RAGFLOW)
        backend = _backend([record], {PROCESSOR_KIND_RAGFLOW: _Handler(PROCESSOR_KIND_RAGFLOW)})
        backend.client = _NotOwnerListClient()
        ctx = RequestContext(metadata={"department_id": "dept_a"})

        documents = backend.list_documents("kb", ctx=ctx)

        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].metadata["ragflow_error"], "")
        self.assertIn("realtime progress is not readable", documents[0].metadata["ragflow_status_note"])

    def test_retrieve_requires_department_context_before_remote_call(self):
        backend = _retrieve_backend([])

        with self.assertRaises(PermissionError):
            backend.retrieve("kb", "query", ctx=None)

        self.assertEqual(backend.client.retrieve_calls, [])

    def test_retrieve_filters_by_department_scope(self):
        dept_a_record = _record(1, "a.pdf", PROCESSOR_KIND_RAGFLOW, department_id="dept_a")
        dept_b_record = _record(2, "b.pdf", PROCESSOR_KIND_RAGFLOW, department_id="dept_b")
        backend = _retrieve_backend([
            {
                "id": "chunk-a",
                "document_id": dept_a_record.document_id,
                "content": "dept a content",
                "similarity": 0.9,
                "metadata": {
                    "kb_name": "kb",
                    "department_id": "dept_a",
                    "source_group": "docs",
                    "original_file_name": "a.pdf",
                },
            },
            {
                "id": "chunk-b",
                "document_id": dept_b_record.document_id,
                "content": "dept b content",
                "similarity": 0.8,
                "metadata": {
                    "kb_name": "kb",
                    "department_id": "dept_b",
                    "source_group": "docs",
                    "original_file_name": "b.pdf",
                },
            },
        ], [dept_a_record, dept_b_record])
        ctx = RequestContext(metadata={"department_id": "dept_a"})

        evidences = backend.retrieve("kb", "query", top_k=5, ctx=ctx)

        self.assertEqual([item.content for item in evidences], ["dept a content"])
        metadata_condition = backend.client.retrieve_calls[0][3]
        self.assertIn(
            {"name": "department_id", "comparison_operator": "=", "value": "dept_a"},
            metadata_condition["conditions"],
        )

    def test_retrieve_passes_through_chunks_without_department_metadata(self):
        # RAGFlow does not always echo document-level meta_fields (department_id)
        # on retrieved chunks. Such chunks must pass through to the caller instead
        # of being dropped by the defense-in-depth department check — otherwise
        # every hit is silently filtered out and the answer comes back empty.
        record = _record(3, "c.pdf", PROCESSOR_KIND_RAGFLOW)
        backend = _retrieve_backend([
            {
                "id": "chunk-nodept",
                "document_id": record.document_id,
                "content": "content without department metadata",
                "similarity": 0.9,
                "metadata": {
                    "kb_name": "kb",
                    "source_group": "docs",
                    "original_file_name": "c.pdf",
                },
            },
        ], [record])
        ctx = RequestContext(metadata={"department_id": "dept_a"})

        evidences = backend.retrieve("kb", "query", top_k=5, ctx=ctx)

        self.assertEqual(
            [item.content for item in evidences],
            ["content without department metadata"],
        )

    def test_retrieve_retries_scoped_source_name_without_remote_filename_filter(self):
        record = _record(4, "600608964_ADAS_HSI.docx", PROCESSOR_KIND_RAGFLOW)
        record.source_group = "design"
        chunk = {
            "id": "chunk-hsi",
            "document_id": record.document_id,
            "document_name": "600608964_ADAS_HSI.docx",
            "content": "HSI interface evidence",
            "similarity": 0.91,
            "metadata": {
                "kb_name": "kb",
                "department_id": "dept_a",
                "source_group": "design",
            },
        }
        backend = _retrieve_backend([], [record])
        backend.client = _SequentialRetrieveClient([[], [chunk]])
        ctx = RequestContext(metadata={"department_id": "dept_a"})

        evidences = backend.retrieve(
            "kb",
            "HSI interface",
            top_k=5,
            ctx=ctx,
            filters={"source_name": "600608964_ADAS_HSI.docx"},
        )

        self.assertEqual([item.id for item in evidences], ["chunk-hsi"])
        self.assertTrue(evidences[0].metadata["ragflow_source_name_fallback"])
        self.assertEqual(len(backend.client.retrieve_calls), 2)
        first_condition = backend.client.retrieve_calls[0][3]
        second_condition = backend.client.retrieve_calls[1][3]
        self.assertIn(
            {"name": "original_file_name", "comparison_operator": "=", "value": "600608964_ADAS_HSI.docx"},
            first_condition["conditions"],
        )
        self.assertNotIn(
            {"name": "original_file_name", "comparison_operator": "=", "value": "600608964_ADAS_HSI.docx"},
            second_condition["conditions"],
        )

    def test_retrieve_fallback_keeps_only_requested_local_document(self):
        target = _record(5, "target.pdf", PROCESSOR_KIND_RAGFLOW)
        other = _record(6, "other.pdf", PROCESSOR_KIND_RAGFLOW)
        target.source_group = other.source_group = "design"
        target_chunk = {
            "id": "chunk-target",
            "document_id": target.document_id,
            "document_keyword": "target(1).pdf",
            "content": "target evidence",
            "similarity": 0.91,
        }
        other_chunk = {
            "id": "chunk-other",
            "document_id": other.document_id,
            "document_keyword": "other.pdf",
            "content": "other evidence",
            "similarity": 0.9,
        }
        backend = _retrieve_backend([], [target, other])
        backend.client = _SequentialRetrieveClient([[], [other_chunk, target_chunk]])
        ctx = RequestContext(metadata={"department_id": "dept_a"})

        evidences = backend.retrieve(
            "kb",
            "HSI interface",
            top_k=5,
            ctx=ctx,
            filters={"source_name": "target.pdf"},
        )

        self.assertEqual([item.id for item in evidences], ["chunk-target"])
        self.assertTrue(evidences[0].metadata["ragflow_source_name_fallback"])

    def test_retrieve_retries_when_local_filename_validation_empties_remote_hits(self):
        target = _record(7, "600608964_ADAS_HSI_0506_1952_shoulin.wang.docx", PROCESSOR_KIND_RAGFLOW)
        other = _record(8, "other.docx", PROCESSOR_KIND_RAGFLOW)
        target.source_group = other.source_group = "design"
        mismatched_chunk = {
            "id": "chunk-mismatched-name",
            "document_id": other.document_id,
            "document_name": "ragflow-internal-name.docx",
            "content": "candidate returned with an internal name",
            "similarity": 0.9,
            "metadata": {
                "kb_name": "kb",
                "department_id": "dept_a",
                "source_group": "design",
            },
        }
        fallback_chunk = {
            "id": "chunk-fallback",
            "document_id": target.document_id,
            "document_name": "600608964_ADAS_HSI_0506_1952_shoulin.wang.docx",
            "content": "HSI interface evidence",
            "similarity": 0.91,
            "metadata": {
                "kb_name": "kb",
                "department_id": "dept_a",
                "source_group": "design",
            },
        }
        backend = _retrieve_backend([], [target, other])
        backend.client = _SequentialRetrieveClient([[mismatched_chunk], [fallback_chunk]])
        ctx = RequestContext(metadata={"department_id": "dept_a"})

        evidences = backend.retrieve(
            "kb",
            "HSI interface",
            top_k=5,
            ctx=ctx,
            filters={"source_name": "600608964_ADAS_HSI_0506_1952_shoulin.wang.docx"},
        )

        self.assertEqual([item.id for item in evidences], ["chunk-fallback"])
        self.assertTrue(evidences[0].metadata["ragflow_source_name_fallback"])
        self.assertEqual(len(backend.client.retrieve_calls), 2)
        self.assertNotIn(
            {
                "name": "original_file_name",
                "comparison_operator": "=",
                "value": "600608964_ADAS_HSI_0506_1952_shoulin.wang.docx",
            },
            backend.client.retrieve_calls[1][3]["conditions"],
        )

    def test_retrieve_enriches_metadata_free_chunk_from_scoped_local_record(self):
        record = _record(9, "600608964_ADAS_HSI.docx", PROCESSOR_KIND_RAGFLOW)
        chunk = {
            "id": "chunk-hsi",
            "document_id": record.document_id,
            "document_keyword": "600608964_ADAS_HSI(1).docx",
            "content": "HSI interface evidence",
            "similarity": 0.91,
        }
        backend = _retrieve_backend([chunk], [record])
        ctx = RequestContext(metadata={"department_id": "dept_a"})

        evidences = backend.retrieve("kb", "balanced query", top_k=5, ctx=ctx)

        self.assertEqual([item.id for item in evidences], ["chunk-hsi"])
        self.assertEqual(evidences[0].source_name, "600608964_ADAS_HSI.docx")
        self.assertEqual(evidences[0].metadata["kb_name"], "kb")
        self.assertEqual(evidences[0].metadata["department_id"], "dept_a")
        self.assertEqual(evidences[0].metadata["source_group"], "文档资料")
        self.assertEqual(evidences[0].metadata["original_file_name"], "600608964_ADAS_HSI.docx")

    def test_retrieve_rejects_chunk_without_scoped_local_document(self):
        allowed = _record(10, "allowed.docx", PROCESSOR_KIND_RAGFLOW)
        chunk = {
            "id": "chunk-stale",
            "document_id": "remote-stale",
            "document_keyword": "stale.docx",
            "content": "unscoped content",
            "similarity": 0.9,
        }
        backend = _retrieve_backend([chunk], [allowed])
        ctx = RequestContext(metadata={"department_id": "dept_a"})

        self.assertEqual(backend.retrieve("kb", "balanced query", ctx=ctx), [])

    def test_retrieve_rejects_explicit_metadata_conflicting_with_local_record(self):
        record = _record(11, "allowed.docx", PROCESSOR_KIND_RAGFLOW)
        chunk = {
            "id": "chunk-conflict",
            "document_id": record.document_id,
            "content": "wrong department content",
            "similarity": 0.9,
            "metadata": {
                "kb_name": "kb",
                "department_id": "dept_b",
                "source_group": "docs",
            },
        }
        backend = _retrieve_backend([chunk], [record])
        ctx = RequestContext(metadata={"department_id": "dept_a"})

        self.assertEqual(backend.retrieve("kb", "balanced query", ctx=ctx), [])

    def test_kb_delete_dispatches_to_pipeline_handlers(self):
        rag_handler = _Handler(PROCESSOR_KIND_RAGFLOW)
        spreadsheet_handler = _Handler(PROCESSOR_KIND_SPREADSHEET)
        records = [
            _record(1, "design.pdf", PROCESSOR_KIND_RAGFLOW),
            _record(2, "hardware.xlsx", PROCESSOR_KIND_SPREADSHEET),
        ]
        backend = _backend(
            records,
            {
                PROCESSOR_KIND_RAGFLOW: rag_handler,
                PROCESSOR_KIND_SPREADSHEET: spreadsheet_handler,
            },
        )
        ctx = RequestContext(metadata={"department_id": "dept_a"})

        result = backend.delete_knowledge_base("kb", ctx=ctx)

        self.assertIsInstance(result, BackendResult)
        self.assertTrue(result.ok)
        self.assertEqual([item[0].document_name for item in rag_handler.deleted], ["design.pdf"])
        self.assertEqual([item[0].document_name for item in spreadsheet_handler.deleted], ["hardware.xlsx"])
        self.assertEqual(backend.store.delete_kb_calls, [("kb", "dept_a")])

    def test_kb_delete_is_department_scoped(self):
        rag_handler = _Handler(PROCESSOR_KIND_RAGFLOW)
        spreadsheet_handler = _Handler(PROCESSOR_KIND_SPREADSHEET)
        records = [
            _record(1, "dept-a.pdf", PROCESSOR_KIND_RAGFLOW, department_id="dept_a"),
            _record(2, "dept-b.xlsx", PROCESSOR_KIND_SPREADSHEET, department_id="dept_b"),
        ]
        backend = _backend(
            records,
            {
                PROCESSOR_KIND_RAGFLOW: rag_handler,
                PROCESSOR_KIND_SPREADSHEET: spreadsheet_handler,
            },
        )
        ctx = RequestContext(metadata={"department_id": "dept_a"})

        result = backend.delete_knowledge_base("kb", ctx=ctx)

        self.assertTrue(result.ok)
        self.assertEqual(backend.store.list_calls, [("kb", "dept_a")])
        self.assertEqual([item[0].document_name for item in rag_handler.deleted], ["dept-a.pdf"])
        self.assertEqual(spreadsheet_handler.deleted, [])
        self.assertEqual(backend.store.delete_kb_calls, [("kb", "dept_a")])

    def test_kb_delete_keeps_kb_metadata_when_one_record_fails(self):
        rag_handler = _Handler(PROCESSOR_KIND_RAGFLOW)
        failing_handler = _Handler(PROCESSOR_KIND_SPREADSHEET)
        failing_handler.ok = False
        records = [
            _record(1, "ok.pdf", PROCESSOR_KIND_RAGFLOW),
            _record(2, "failed.xlsx", PROCESSOR_KIND_SPREADSHEET),
        ]
        backend = _backend(
            records,
            {
                PROCESSOR_KIND_RAGFLOW: rag_handler,
                PROCESSOR_KIND_SPREADSHEET: failing_handler,
            },
        )
        ctx = RequestContext(metadata={"department_id": "dept_a"})

        result = backend.delete_knowledge_base("kb", ctx=ctx)

        self.assertFalse(result.ok)
        self.assertEqual(backend.store.deleted_ids, [1])
        self.assertEqual(backend.store.delete_kb_calls, [])
        self.assertEqual([item[0].document_name for item in rag_handler.deleted], ["ok.pdf"])
        self.assertEqual([item[0].document_name for item in failing_handler.deleted], ["failed.xlsx"])

    def test_parse_worker_dispatches_to_pipeline_handler(self):
        spreadsheet_handler = _Handler(PROCESSOR_KIND_SPREADSHEET)
        record = _record(3, "hardware.xlsx", PROCESSOR_KIND_SPREADSHEET)
        backend = _backend(
            [record],
            {PROCESSOR_KIND_SPREADSHEET: spreadsheet_handler},
        )
        audits = []
        backend._audit = lambda *args, **kwargs: audits.append((args, kwargs))
        backend.runtime.audit_callback = backend._audit

        backend._process_parse_record(record)

        self.assertEqual([item[0].document_name for item in spreadsheet_handler.processed], ["hardware.xlsx"])
        self.assertEqual(audits[0][0][0], f"{PROCESSOR_KIND_SPREADSHEET}_processed")
        self.assertEqual(audits[0][1]["metadata"]["store_id"], 3)

    def test_runtime_skips_audit_when_callback_is_not_callable(self):
        spreadsheet_handler = _Handler(PROCESSOR_KIND_SPREADSHEET)
        record = _record(4, "hardware.xlsx", PROCESSOR_KIND_SPREADSHEET)
        backend = _backend(
            [record],
            {PROCESSOR_KIND_SPREADSHEET: spreadsheet_handler},
        )
        runtime = PipelineRuntime(
            store=backend.store,
            archive=backend.archive,
            ingestion=backend.ingestion,
            audit_callback=None,
        )

        runtime.process_record(record)

        self.assertEqual([item[0].document_name for item in spreadsheet_handler.processed], ["hardware.xlsx"])

    def test_delete_parse_task_propagates_handler_failure(self):
        # Regression for #4: delete_parse_task previously hardcoded ok=True for the
        # spreadsheet branch, masking a handler ok=False (e.g. index/archive cleanup
        # failed). It must now surface the handler's real outcome.
        failing_handler = _Handler(PROCESSOR_KIND_SPREADSHEET)
        failing_handler.ok = False
        failing_handler.errors = ["spreadsheet index: boom"]
        record = _record(2, "hardware.xlsx", PROCESSOR_KIND_SPREADSHEET)
        backend = _backend(
            [record],
            {PROCESSOR_KIND_SPREADSHEET: failing_handler},
        )
        ctx = RequestContext(metadata={"department_id": "dept_a"})

        result = backend.delete_parse_task("ragflow-2", ctx=ctx)

        self.assertFalse(result.ok)
        self.assertIn("boom", result.message)
        self.assertEqual([item[0].document_name for item in failing_handler.deleted], ["hardware.xlsx"])

    def test_delete_parse_task_reports_ok_when_handler_succeeds(self):
        # Positive control for #4: a successful cleanup still reports ok=True.
        handler = _Handler(PROCESSOR_KIND_SPREADSHEET)
        record = _record(2, "hardware.xlsx", PROCESSOR_KIND_SPREADSHEET)
        backend = _backend([record], {PROCESSOR_KIND_SPREADSHEET: handler})
        ctx = RequestContext(metadata={"department_id": "dept_a"})

        result = backend.delete_parse_task("ragflow-2", ctx=ctx)

        self.assertTrue(result.ok)

    def test_delete_failed_ragflow_parse_task_cleans_local_state_without_stop(self):
        record = _record(5, "broken.pdf", PROCESSOR_KIND_RAGFLOW)
        record.status = "failed"
        backend = _backend([record], {})
        ctx = RequestContext(metadata={"department_id": "dept_a"})

        result = backend.delete_parse_task("ragflow-5", ctx=ctx)

        self.assertTrue(result.ok)
        self.assertEqual(backend.archive.removed, [5])
        self.assertEqual(backend.store.deleted_ids, [5])


if __name__ == "__main__":
    unittest.main()
