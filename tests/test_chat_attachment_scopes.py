"""Tests for the analyze-from-attachment bridge and attachment scope resolvers."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
import zipfile

import src.settings
from src.agents.scopes import (
    AttachmentCircuitScopeResolver,
    AttachmentSpreadsheetScopeResolver,
    KnowledgeBaseSpreadsheetScopeResolver,
)
from src.attachments.service import AttachmentService
from src.attachments.store import AttachmentStore


def _valid_office_stub() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    return output.getvalue()


class ScopeResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._patch("CHAT_ATTACHMENTS_ENABLED", True)
        self._patch("CHAT_ATTACHMENT_INDEX_DB_PATH", os.path.join(self.tmp.name, "att.db"))
        self._patch("CHAT_ATTACHMENT_STORAGE_DIR", os.path.join(self.tmp.name, "att-storage"))
        self.store = AttachmentStore(db_path=os.path.join(self.tmp.name, "att.db"))
        self.service = AttachmentService(store=self.store)

    def _patch(self, name, value):
        old = getattr(src.settings, name)
        setattr(src.settings, name, value)
        self.addCleanup(setattr, src.settings, name, old)

    def test_kb_spreadsheet_scope_requires_department(self):
        resolver = KnowledgeBaseSpreadsheetScopeResolver(
            lambda department_id, kb_name: "/tmp/unused.db"
        )
        with self.assertRaises(PermissionError):
            resolver.resolve(kb_name="shared", department_id=None)
        scope = resolver.resolve(kb_name="shared", department_id="3")
        self.assertEqual(scope.source_type, "knowledge_base")
        self.assertEqual(scope.department_id, "3")

    def test_attachment_spreadsheet_scope_from_manifest(self):
        conv = None  # scope resolvers only need asset rows + refs
        del conv
        record = self.service.upload(
            session_id=1, user_id=1, filename="t.xlsx",
            stream=io.BytesIO(_valid_office_stub()),
        )
        self.store.update_asset_status(
            record.asset_id, parse_status="ready",
            manifest={"index_db_path": "table_indexes/1/x/table_indexes.db", "record_id": 42},
        )
        ref = self.service.build_refs([record])[0]
        scope = AttachmentSpreadsheetScopeResolver(self.store).resolve(refs=[ref])
        self.assertEqual(scope.source_type, "chat_attachment")
        self.assertEqual(scope.allowed_record_ids, frozenset({42}))
        self.assertEqual(scope.db_path, "table_indexes/1/x/table_indexes.db")

    def test_attachment_circuit_scope_from_manifest(self):
        record = self.service.upload(
            session_id=1, user_id=1, filename="board.edf",
            stream=io.BytesIO(b"(edif board)"),
        )
        self.store.update_asset_status(
            record.asset_id, parse_status="ready",
            manifest={"circuit_root": "/tmp/att-root", "kb_name": "att_x"},
        )
        ref = self.service.build_refs([record])[0]
        scope = AttachmentCircuitScopeResolver(self.store).resolve(refs=[ref])
        self.assertEqual(scope.source_type, "chat_attachment")
        self.assertEqual(scope.store_root, "/tmp/att-root")
        self.assertIn(ref.attachment_id, scope.attachment_ids)



if __name__ == "__main__":
    unittest.main()
