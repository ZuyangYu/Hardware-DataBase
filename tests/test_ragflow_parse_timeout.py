import time
import unittest
from types import SimpleNamespace

from src.pipelines.document_rag import ragflow_backend as backend_module
from src.pipelines.document_rag.ragflow_backend import (
    RAGFlowBackend,
    _ragflow_parse_timed_out,
)
from src.pipelines.document_rag.schemas import RequestContext, TASK_STATUS_FAILED
from src.pipelines.document_store import PipelineDocumentRecord
from src.pipelines.registry import PROCESSOR_KIND_RAGFLOW


def _fmt_ts(ts: float) -> str:
    # parse_started_at is stored in UTC (SQLite CURRENT_TIMESTAMP), so format
    # the epoch as UTC to match production semantics.
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))


def _timeout_record(
    *,
    status: str = "parsing",
    parse_started_at: str | None = "set-by-caller",
    record_id: int = 1,
    department_id: str = "dept_a",
    kb_name: str = "kb",
    processor_kind: str = PROCESSOR_KIND_RAGFLOW,
) -> PipelineDocumentRecord:
    return PipelineDocumentRecord(
        id=record_id,
        kb_name=kb_name,
        document_name="stale.pdf",
        original_file_name="stale.pdf",
        dataset_kind="design",
        dataset_id="dataset-1",
        document_id="remote-1",
        source_group="docs",
        department_id=department_id,
        uploaded_by="user",
        status=status,
        processor_kind=processor_kind,
        parse_started_at=parse_started_at or "",
    )


class _CleanupClient:
    """RAGFlow client stub that records delete calls and never fails."""

    def __init__(self):
        self.deleted = []

    def delete_documents(self, dataset_id, document_ids):
        self.deleted.append((dataset_id, list(document_ids)))


class _Store:
    """Lightweight store mock recording status updates."""

    def __init__(self, records):
        self.records = records
        self.status_updates = []  # (record_id, status, message)

    def list_documents(self, kb_name, department_id=None):
        return [
            record
            for record in self.records
            if record.kb_name == kb_name
            and (department_id in (None, "") or record.department_id == department_id)
        ]

    def update_document_status_by_id(self, record_id, status, error_message=""):
        self.status_updates.append((record_id, status, error_message))

    def update_document_status(self, dataset_id, document_id, status, error_message=""):
        # Used by the non-timeout refresh branch; record for completeness.
        self.status_updates.append((dataset_id, status, error_message))


def _timeout_backend(records, *, client=None):
    backend = object.__new__(RAGFlowBackend)
    backend.name = "ragflow"
    backend.store = _Store(records)
    backend.archive = None
    backend.client = client if client is not None else _CleanupClient()
    backend._check_kb_access = lambda kb_name, ctx, required="read": None
    return backend


class RagflowParseTimedOutHelperTests(unittest.TestCase):
    def test_parsing_record_past_threshold_times_out(self):
        started = _fmt_ts(time.time() - 2 * 3600)  # 2 hours ago
        record = _timeout_record(parse_started_at=started)
        self.assertTrue(_ragflow_parse_timed_out(record))

    def test_parsing_record_within_threshold_not_timed_out(self):
        started = _fmt_ts(time.time() - 5 * 60)  # 5 minutes ago
        record = _timeout_record(parse_started_at=started)
        self.assertFalse(_ragflow_parse_timed_out(record))

    def test_now_ts_override_controls_judgment(self):
        started = _fmt_ts(0)  # 1970 epoch
        record = _timeout_record(parse_started_at=started)
        # now just after start + threshold
        self.assertFalse(_ragflow_parse_timed_out(record, now_ts=10))
        # now well past threshold
        self.assertTrue(
            _ragflow_parse_timed_out(
                record,
                now_ts=10 + backend_module.RAGFLOW_PARSE_PROGRESS_TIMEOUT_SECONDS + 1,
            )
        )

    def test_non_running_status_never_times_out(self):
        started = _fmt_ts(time.time() - 2 * 3600)
        for terminal_status in ("parsed", "failed", "deleted", "cancelled"):
            record = _timeout_record(status=terminal_status, parse_started_at=started)
            self.assertFalse(_ragflow_parse_timed_out(record), msg=terminal_status)

    def test_empty_or_malformed_start_at_is_conservatively_skipped(self):
        for started in ("", "not-a-date"):
            record = _timeout_record(parse_started_at=started)
            self.assertFalse(_ragflow_parse_timed_out(record), msg=started)


class RagflowMarkTimedOutTests(unittest.TestCase):
    def test_marks_failed_and_cleans_remote(self):
        record = _timeout_record(record_id=7)
        backend = _timeout_backend([record])

        msg = backend._mark_ragflow_parse_timed_out(record)

        self.assertIn("超时", msg)
        self.assertEqual(backend.client.deleted, [("dataset-1", ["remote-1"])])
        self.assertEqual(
            backend.store.status_updates,
            [(7, "failed", msg)],
        )

    def test_swallows_cleanup_failure_and_still_marks_failed(self):
        record = _timeout_record(record_id=8)
        backend = _timeout_backend([record])

        def boom(dataset_id, document_id):
            raise RuntimeError("ragflow unreachable")

        backend._cleanup_remote_document = boom

        msg = backend._mark_ragflow_parse_timed_out(record)

        self.assertIn("超时", msg)
        self.assertEqual(
            backend.store.status_updates,
            [(8, "failed", msg)],
        )


class RagflowListParseTasksTimeoutTests(unittest.TestCase):
    def _fresh_parsing_record(self, record_id=1):
        # Started 5 minutes ago: within threshold, must NOT time out.
        started = _fmt_ts(time.time() - 5 * 60)
        record = _timeout_record(record_id=record_id, parse_started_at=started)
        return record

    def _stale_parsing_record(self, record_id=1):
        # Started 2 hours ago: past threshold, MUST time out.
        started = _fmt_ts(time.time() - 2 * 3600)
        record = _timeout_record(record_id=record_id, parse_started_at=started)
        return record

    def test_marks_stale_parsing_record_as_failed_task(self):
        record = self._stale_parsing_record(record_id=3)
        backend = _timeout_backend([record])

        tasks = backend.list_parse_tasks("kb", ctx=RequestContext(metadata={"department_id": "dept_a"}))

        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task.status, TASK_STATUS_FAILED)
        self.assertEqual(task.progress, 100)
        self.assertEqual(task.stage, "解析超时")
        self.assertIn("超时", task.message)
        # Remote doc deleted + store marked failed.
        self.assertEqual(backend.client.deleted, [("dataset-1", ["remote-1"])])
        self.assertEqual(
            backend.store.status_updates,
            [(3, "failed", task.message)],
        )

    def test_fresh_parsing_record_does_not_time_out(self):
        record = self._fresh_parsing_record(record_id=4)
        # Use a client whose list_documents returns "parsing" so the normal
        # refresh branch runs instead of the timeout branch.
        client = SimpleNamespace(
            list_documents=lambda dataset_id, document_id: [{"run": "1"}],
            delete_documents=lambda dataset_id, document_ids: None,
        )
        backend = _timeout_backend([record], client=client)

        tasks = backend.list_parse_tasks("kb", ctx=RequestContext(metadata={"department_id": "dept_a"}))

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].status, "running")
        # Timeout path must not have marked failed.
        self.assertNotIn(("4", "failed"), [(str(u[0]), u[1]) for u in backend.store.status_updates])
        # No cleanup client here; just assert no failed status was written for record 4.
        self.assertFalse(
            any(u[0] == 4 and u[1] == "failed" for u in backend.store.status_updates),
            "fresh parsing record must not be marked failed",
        )


if __name__ == "__main__":
    unittest.main()
