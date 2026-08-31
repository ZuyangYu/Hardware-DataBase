"""End-to-end lifecycle for the external-conversation domain.

Upload routing -> handler submit -> store/index/ledger -> API-shaped listing ->
agent-tool search -> delete -> KB cleanup, plus cross-department isolation for
same-named KBs. Runs entirely on temp roots; no RAGFlow access.
"""

import os
import shutil
import tempfile
import unittest

from src.ingestion.container_inspector import ContainerInspection
from src.ingestion.source_groups import DOCS_GROUP, EXTERNAL_GROUP
from src.pipelines.document_store import PipelineDocumentStore
from src.pipelines.ingestion import ArchivedFile, ExternalConversationHandler, IngestionScope
from src.pipelines.registry import (
    DATASET_CONVERSATION,
    PROCESSOR_KIND_EXTERNAL_CONVERSATION,
    PROCESSOR_KIND_RAGFLOW,
    route_file,
)
from src.agents.tools.external_conversation_tools import ExternalConversationSearchTool
from src.external_conversations.query_engine import ExternalConversationQueryEngine
from src.external_conversations.store import ExternalConversationStore
from src.pipelines.registry import PIPELINE_REGISTRY


class _FakeArchive:
    def __init__(self):
        self.removed = []

    def remove_record_archive(self, record):
        self.removed.append(record.id)


class ExternalConversationEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ext_conv_e2e_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        root = os.path.join(self.tmp, "convs")
        self.store = ExternalConversationStore(root=root)
        self.engine = ExternalConversationQueryEngine(root=self.tmp)
        self.ledger = PipelineDocumentStore(db_path=os.path.join(self.tmp, "ledger.db"))
        spec = PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_EXTERNAL_CONVERSATION)
        self.handler = ExternalConversationHandler(
            spec=spec,
            store=self.ledger,
            conversation_store=self.store,
            conversation_indexes=self.engine,
        )
        self.tool = ExternalConversationSearchTool(self.engine)

    def _write_upload(self, filename: str, content: str) -> str:
        path = os.path.join(self.tmp, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _submit(self, department_id: str, kb_name: str, path: str, filename: str):
        archived = ArchivedFile(
            original_path=path,
            archived_path=path,
            filename=filename,
            source_group=EXTERNAL_GROUP,
            relative_local_path=f"departments/{department_id}/kbs/{kb_name}/{filename}",
            file_size=os.path.getsize(path),
            content_hash=f"hash-{department_id}-{filename}-{os.path.getsize(path)}",
            inspection=ContainerInspection(file_name=filename),
        )
        scope = IngestionScope(
            kb_name=kb_name,
            department_id=department_id,
            kb_id=1,
            uploaded_by="tester",
            source_group=EXTERNAL_GROUP,
        )
        return self.handler.submit(
            scope=scope,
            archived=archived,
            default_dataset_kind="design",
            default_dataset_id="ds-1",
        )

    def test_full_lifecycle_upload_parse_browse_search_delete_cleanup(self):
        # 1. upload routing decision (what the orchestrator would do)
        upload_path = self._write_upload(
            "chat.md",
            "用户: LDO 的压差要求?\n助手: 最大 0.3V,注意热设计。\n用户: 静态电流?\n助手: 典型 12uA。\n",
        )
        route = route_file(upload_path, source_group=EXTERNAL_GROUP)
        self.assertEqual(route.spec.processor_kind, PROCESSOR_KIND_EXTERNAL_CONVERSATION)

        # 2. ingest via handler
        result = self._submit("dept_1", "kb_a", upload_path, "chat.md")
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.status, "indexed")

        # 3. workbench-style listing + detail
        rows = self.engine.list_conversations("dept_1", "kb_a")
        self.assertEqual(len(rows), 1)
        detail = self.store.load("dept_1", "kb_a", rows[0]["conversation_id"])
        self.assertIsNotNone(detail)
        self.assertEqual(len(detail.turns), 4)

        # 4. agent retrieval with evidence provenance
        evidences = self.tool.run("LDO 压差", "kb_a", type("C", (), {"metadata": {"department_id": "dept_1"}})(), top_k=3)
        self.assertTrue(evidences)
        self.assertEqual(evidences[0].processor_kind, PROCESSOR_KIND_EXTERNAL_CONVERSATION)
        self.assertIn(evidences[0].locator["conversation_id"], rows[0]["conversation_id"])

        # 5. ledger row exists and is deletable end-to-end
        record = self.ledger.get_document("kb_a", "chat.md", DATASET_CONVERSATION, department_id="dept_1")
        self.assertIsNotNone(record)
        fake_archive = _FakeArchive()
        deleted = self.handler.delete_record(record, fake_archive)
        self.assertTrue(deleted.ok)
        self.assertIsNone(self.ledger.get_document("kb_a", "chat.md", DATASET_CONVERSATION, department_id="dept_1"))
        self.assertEqual(self.store.list_conversations("dept_1", "kb_a"), [])
        self.assertEqual(self.tool.run("LDO 压差", "kb_a", type("C", (), {"metadata": {"department_id": "dept_1"}})()), [])

        # 6. cleanup removes remaining per-KB trees
        from src.services.pipeline_asset_cleanup import PipelineAssetCleanupService

        cleanup = PipelineAssetCleanupService(conversations=self.store).cleanup_knowledge_base("kb_a", "dept_1")
        self.assertTrue(cleanup.ok, cleanup.errors)
        self.assertFalse(os.path.exists(self.store.scope_dir("dept_1", "kb_a")))

    def test_two_departments_same_kb_name_stay_isolated_end_to_end(self):
        p1 = self._write_upload("same.md", "用户: 部门一的问题 A?")
        p2 = self._write_upload("same2.md", "用户: 部门二的问题 B?")
        r1 = self._submit("dept_1", "shared_kb", p1, "same.md")
        r2 = self._submit("dept_2", "shared_kb", p2, "same.md")
        self.assertTrue(r1.success and r2.success)

        ctx1 = type("C", (), {"metadata": {"department_id": "dept_1"}})()
        ctx2 = type("C", (), {"metadata": {"department_id": "dept_2"}})()

        hits1 = {e.locator["conversation_id"] for e in self.tool.run("部门一 问题", "shared_kb", ctx1)}
        hits2 = {e.locator["conversation_id"] for e in self.tool.run("部门二 问题", "shared_kb", ctx2)}
        self.assertEqual(len(hits1), 1)
        self.assertEqual(len(hits2), 1)
        self.assertNotEqual(hits1, hits2)

        listings = (
            self.engine.list_conversations("dept_1", "shared_kb"),
            self.engine.list_conversations("dept_2", "shared_kb"),
        )
        self.assertEqual([r["title"] for r in listings[0]], ["same"])
        self.assertEqual([r["title"] for r in listings[1]], ["same"])

        # deleting dept_1's conversation leaves dept_2 untouched
        record1 = self.ledger.get_document("shared_kb", "same.md", DATASET_CONVERSATION, department_id="dept_1")
        self.handler.delete_record(record1, _FakeArchive())
        self.assertEqual(self.engine.list_conversations("dept_1", "shared_kb"), [])
        self.assertEqual(len(self.engine.list_conversations("dept_2", "shared_kb")), 1)

    def test_other_groups_keep_document_routing(self):
        route = route_file("/tmp/notes.md", source_group=DOCS_GROUP)
        self.assertEqual(route.spec.processor_kind, PROCESSOR_KIND_RAGFLOW)


if __name__ == "__main__":
    unittest.main()
