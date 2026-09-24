import os
import sqlite3
import tempfile
import unittest

from src.pipelines.document_rag.ragflow_backend import (
    RAGFLOW_STATUS_PARSED,
    RAGFLOW_STATUS_PARSING,
    RAGFlowBackend,
)
from src.pipelines.document_store import PipelineDocumentStore
from src.pipelines.registry import PROCESSOR_KIND_RAGFLOW


def _upsert(store, name, status, document_id="remote-1"):
    store.upsert_document(
        kb_name="kb_hw",
        document_name=name,
        dataset_kind="design",
        dataset_id="ds-1",
        document_id=document_id,
        source_group="design",
        department_id="dept_hw",
        uploaded_by="alice",
        status=status,
        processor_kind=PROCESSOR_KIND_RAGFLOW,
    )
    return store.get_document("kb_hw", name, "design", department_id="dept_hw")


class ParseStatusRefreshStoreTests(unittest.TestCase):
    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = PipelineDocumentStore(db_path=os.path.join(tmp.name, "pipeline.db"))
        parsing = _upsert(store, "parsing.docx", RAGFLOW_STATUS_PARSING)
        _upsert(store, "done.docx", RAGFLOW_STATUS_PARSED, document_id="remote-2")
        return store, parsing

    def test_candidates_exclude_completed_and_honor_throttle(self):
        store, parsing = self._store()

        candidates = store.list_status_refresh_candidates(
            [PROCESSOR_KIND_RAGFLOW], [RAGFLOW_STATUS_PARSING, "uploaded"],
            limit=10, min_interval_seconds=0,
        )
        self.assertEqual([item.id for item in candidates], [parsing.id])

        store.mark_status_checked(parsing.id)
        throttled = store.list_status_refresh_candidates(
            [PROCESSOR_KIND_RAGFLOW], [RAGFLOW_STATUS_PARSING, "uploaded"],
            limit=10, min_interval_seconds=3600,
        )
        self.assertEqual(throttled, [])

        # 把检查时间拨旧两小时 → 重新成为候选。
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE pipeline_documents SET last_status_checked_at = datetime('now', '-2 hours') WHERE id = ?",
                (parsing.id,),
            )

        fresh = store.list_status_refresh_candidates(
            [PROCESSOR_KIND_RAGFLOW], [RAGFLOW_STATUS_PARSING, "uploaded"],
            limit=10, min_interval_seconds=3600,
        )
        self.assertEqual([item.id for item in fresh], [parsing.id])


class _FakeStore:
    def __init__(self, records):
        self.records = records
        self.checked: list[int] = []
        self.updates: list[dict] = []

    def list_status_refresh_candidates(self, processor_kinds, statuses, limit, min_interval_seconds):
        return list(self.records)

    def mark_status_checked(self, record_id):
        self.checked.append(record_id)

    def update_document_progress_by_id(self, record_id, progress, stage, status=None, error_message=None):
        self.updates.append(
            {
                "record_id": record_id,
                "progress": progress,
                "stage": stage,
                "status": status,
                "error": error_message,
            }
        )


class _FakeClient:
    def __init__(self, responses):
        self.responses = responses

    def list_documents(self, dataset_id, document_id):
        result = self.responses[document_id]
        if isinstance(result, Exception):
            raise result
        return result


def _record(record_id, document_id):
    return type(
        "Record",
        (),
        {"id": record_id, "dataset_id": "ds-1", "document_id": document_id, "parse_progress": 5},
    )()


class ParseStatusRefreshBackendTests(unittest.TestCase):
    def _backend(self, records, responses):
        backend = object.__new__(RAGFlowBackend)
        backend.store = _FakeStore(records)
        backend._client = lambda: _FakeClient(responses)
        return backend

    def test_remote_done_and_failed_states_are_synced(self):
        records = [_record(1, "remote-done"), _record(2, "remote-fail"), _record(3, "remote-down")]
        backend = self._backend(
            records,
            {
                "remote-done": [{"run": "2", "progress": 1.0}],
                "remote-fail": [{"run": "3", "error": "parser boom"}],
                "remote-down": RuntimeError("ragflow unavailable"),
            },
        )

        updated = backend.refresh_parse_statuses(limit=5, min_interval_seconds=0)

        self.assertEqual(updated, 2)
        self.assertEqual(backend.store.checked, [1, 2, 3])
        by_id = {item["record_id"]: item for item in backend.store.updates}
        self.assertEqual(by_id[1]["status"], RAGFLOW_STATUS_PARSED)
        self.assertEqual(by_id[1]["progress"], 100)
        self.assertEqual(by_id[2]["status"], "failed")
        self.assertIn("parser boom", by_id[2]["error"])
        # 远端不可读时保持原状态，仅记录检查时间，不写入状态更新。
        self.assertNotIn(3, by_id)

    def test_still_parsing_updates_progress_without_status_flip(self):
        backend = self._backend(
            [_record(1, "remote-running")],
            {"remote-running": [{"run": "1", "progress": 42}]},
        )

        updated = backend.refresh_parse_statuses(limit=5, min_interval_seconds=0)

        self.assertEqual(updated, 0)
        self.assertEqual(backend.store.updates[0]["progress"], 42)
        self.assertIsNone(backend.store.updates[0]["status"])


if __name__ == "__main__":
    unittest.main()
