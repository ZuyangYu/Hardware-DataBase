"""Retention and cleanup guarantees for session-private attachments."""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import src.settings
from src.attachments.models import AttachmentNotFound, AttachmentPart
from src.attachments.service import AttachmentService
from src.attachments.store import AttachmentStore
from src.attachments.worker import AttachmentWorker
from src.core.conversation import ConversationService
from src.core.session_cleanup import SessionCleanupCoordinator


class AttachmentRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old: dict[str, object] = {}
        self._patch("CHAT_ATTACHMENTS_ENABLED", True)
        self._patch("CHAT_ATTACHMENT_INDEX_DB_PATH", os.path.join(self.tmp.name, "att.db"))
        self._patch("CHAT_ATTACHMENT_STORAGE_DIR", os.path.join(self.tmp.name, "storage"))
        self._patch("CHAT_ATTACHMENT_RETENTION_SECONDS", 3600)
        self._patch("AUTH_DB_PATH", os.path.join(self.tmp.name, "auth.db"))
        self.store = AttachmentStore(db_path=src.settings.CHAT_ATTACHMENT_INDEX_DB_PATH)
        self.service = AttachmentService(store=self.store)
        self._seed_user()
        self.session = ConversationService(src.settings.AUTH_DB_PATH).create_session(1, "__general__")

    def _patch(self, name: str, value: object):
        self._old[name] = getattr(src.settings, name, None)
        setattr(src.settings, name, value)

    def tearDown(self):
        for name, value in self._old.items():
            setattr(src.settings, name, value)

    def _seed_user(self):
        with closing(sqlite3.connect(src.settings.AUTH_DB_PATH)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, department_id INTEGER)"
            )
            conn.execute(
                "INSERT INTO users (id, username, department_id) VALUES (1, 'user1', 1)"
            )
            conn.commit()

    def _upload(self):
        return self.service.upload(
            session_id=self.session.id,
            user_id=1,
            filename="notes.txt",
            stream=io.BytesIO(b"retained attachment"),
        )

    def test_upload_sets_retention_deadline(self):
        record = self._upload()

        self.assertIsNotNone(record.expires_at)
        deadline = datetime.fromisoformat(record.expires_at)
        self.assertGreater(deadline, datetime.now(timezone.utc))

    def test_expired_attachment_is_marked_unavailable_and_asset_is_reclaimed(self):
        record = self._upload()
        self.store.replace_parts(
            record.asset_id,
            [AttachmentPart(part_id="part-1", asset_id=record.asset_id, ordinal=0, part_type="text", text_content="body")],
        )
        self.store.save_embeddings(
            asset_id=record.asset_id,
            provider="fake",
            model="model",
            embeddings=[("part-1", "hash", [1.0, 0.0])],
        )
        expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with closing(self.store._connect()) as conn:
            conn.execute(
                "UPDATE chat_attachments SET expires_at = ? WHERE attachment_id = ?",
                (expired_at, record.attachment_id),
            )

        expired = self.service.expire_due_attachments()

        self.assertEqual(expired, 1)
        with self.assertRaises(AttachmentNotFound):
            self.service.get_attachment(
                attachment_id=record.attachment_id,
                session_id=self.session.id,
                user_id=1,
            )
        stored = self.store.get_attachment(
            attachment_id=record.attachment_id,
            include_deleted=True,
        )
        self.assertEqual(stored.status, "expired")
        self.assertIsNone(self.store.get_asset(record.asset_id))
        with closing(self.store._connect()) as conn:
            embedding_count = conn.execute(
                "SELECT COUNT(*) FROM chat_attachment_embeddings WHERE asset_id = ?",
                (record.asset_id,),
            ).fetchone()[0]
        self.assertEqual(embedding_count, 0)

    def test_cleanup_failure_is_requeued_and_emits_cleanup_retry_metric(self):
        self.store.enqueue_session_cleanup(session_id=self.session.id, user_id=1)
        result_store = Mock()
        document_jobs = Mock()
        document_store = Mock()
        coordinator = SessionCleanupCoordinator(
            store=self.store,
            result_store=result_store,
            document_job_store=document_jobs,
            document_store=document_store,
            worker_id="cleanup-test",
        )

        with patch("src.agents.runner.forget_thread", return_value=False):
            with patch("src.core.session_cleanup.record_attachment") as metric:
                self.assertTrue(coordinator.run_once())

        pending = self.store.list_pending_cleanups()
        self.assertEqual(pending, [])
        with closing(self.store._connect()) as conn:
            row = conn.execute(
                "SELECT status, retry_count, last_error FROM session_cleanup_outbox"
            ).fetchone()
        self.assertEqual(row[0], "retrying")
        self.assertEqual(row[1], 1)
        self.assertIn("checkpoint cleanup failed", row[2])
        self.assertTrue(
            any(call.args[0] == "cleanup_retry" for call in metric.call_args_list)
        )

    def test_session_delete_does_not_commit_when_cleanup_outbox_is_unavailable(self):
        """A cleanup enqueue failure must not leave a deleted, uncleanable session."""
        conversation = ConversationService(src.settings.AUTH_DB_PATH)

        with patch(
            "src.attachments.store.AttachmentStore.enqueue_session_cleanup",
            side_effect=RuntimeError("attachment database unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                conversation.delete_session(1, self.session.id)

        self.assertIsNotNone(conversation.get_session(1, self.session.id))

    def test_attachment_worker_sweeps_expired_rows_before_parsing(self):
        record = self._upload()
        expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with closing(self.store._connect()) as conn:
            conn.execute(
                "UPDATE chat_attachments SET expires_at = ? WHERE attachment_id = ?",
                (expired_at, record.attachment_id),
            )

        worker = AttachmentWorker(store=self.store, worker_id="retention-worker")
        self.assertTrue(worker.run_once())

        expired = self.store.get_attachment(
            attachment_id=record.attachment_id,
            include_deleted=True,
        )
        self.assertEqual(expired.status, "expired")
        self.assertIsNone(self.store.get_asset(record.asset_id))
        self.assertEqual(self.store.list_pending_jobs(), [])

    def test_attachment_worker_times_out_parse_and_heartbeats_lease(self):
        self._patch("CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS", 0.05)
        self._upload()
        job = self.store.list_pending_jobs()[0]

        class SlowExecutor:
            def process_asset(self, asset_id, parser_version=None, deadline=None):
                time.sleep(0.2)
                return {"ok": True, "parse_status": "ready"}

        with patch.object(self.store, "heartbeat_job", wraps=self.store.heartbeat_job) as heartbeat:
            worker = AttachmentWorker(
                store=self.store,
                executor=SlowExecutor(),
                worker_id="timeout-worker",
            )
            self.assertTrue(worker.run_once())

        stored_job = self.store.get_job(job.job_id)
        self.assertGreaterEqual(heartbeat.call_count, 1)
        self.assertEqual(stored_job.error_code, "parse_timeout")
        self.assertIn("timed out", stored_job.error_message)

    def test_asset_reuse_emits_low_cardinality_metric(self):
        self._upload()
        with patch("src.observability.metrics.record_attachment") as metric:
            self.service.upload(
                session_id=self.session.id,
                user_id=1,
                filename="renamed.txt",
                stream=io.BytesIO(b"retained attachment"),
            )

        self.assertTrue(
            any(call.args[0] == "asset_reuse" for call in metric.call_args_list)
        )

    def test_waiting_turn_settlement_emits_waiting_turn_metric(self):
        from src.attachments.coordinator import AttachmentTurnCoordinator

        record = self._upload()
        turn = ConversationService(src.settings.AUTH_DB_PATH).create_turn(
            1,
            self.session.id,
            "分析附件",
            attachment_ids=[record.attachment_id],
        )
        self.store.update_asset_status(record.asset_id, parse_status="ready")
        with patch("src.attachments.coordinator.record_attachment") as metric:
            AttachmentTurnCoordinator(store=self.store).notify_asset_settled(record.asset_id)

        self.assertEqual(turn.status, "waiting_for_attachments")
        self.assertTrue(
            any(call.args[0] == "waiting_turn" for call in metric.call_args_list)
        )

    def test_attachment_read_emits_read_metric(self):
        from src.agents.tools.attachment_tools import make_attachment_read
        from src.agents.tools.runtime import ToolRuntime

        record = self._upload()
        self.store.update_asset_status(record.asset_id, parse_status="ready")
        self.store.replace_parts(
            record.asset_id,
            [AttachmentPart(part_id="part-read", asset_id=record.asset_id, ordinal=0, part_type="text", text_content="body")],
        )
        ref = self.service.build_refs([record])[0]
        runtime = ToolRuntime(
            kb_name="",
            ctx=None,
            attachment_refs=[ref],
            source_scope="attachment_only",
            attachment_service=self.service,
            attachment_user_id=1,
            attachment_session_id=self.session.id,
        )
        with patch("src.observability.metrics.record_attachment") as metric:
            output = make_attachment_read(runtime)(limit=1)

        self.assertIn("body", output)
        self.assertTrue(any(call.args[0] == "read" for call in metric.call_args_list))

    def test_attachment_worker_records_ocr_metric_for_ocr_parse(self):
        self._upload()

        class FakeExecutor:
            def process_asset(self, asset_id, parser_version=None):
                self.asset_id = asset_id
                return {
                    "ok": True,
                    "parse_status": "degraded",
                    "manifest": {"ocr_attempted_pages": [1], "ocr_failed_pages": []},
                }

        with patch("src.attachments.worker.record_attachment") as metric:
            worker = AttachmentWorker(
                store=self.store,
                executor=FakeExecutor(),
                worker_id="ocr-metric-worker",
            )
            self.assertTrue(worker.run_once())

        self.assertTrue(any(call.args[0] == "ocr" for call in metric.call_args_list))


if __name__ == "__main__":
    unittest.main()
