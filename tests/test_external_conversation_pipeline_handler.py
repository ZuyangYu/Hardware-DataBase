import os
import shutil
import tempfile
import unittest

from src.ingestion.source_groups import DOCS_GROUP, EXTERNAL_GROUP
from src.pipelines.document_store import PipelineDocumentStore
from src.pipelines.ingestion import ExternalConversationHandler, IngestionScope
from src.pipelines.registry import (
    CONTENT_KIND_EXTERNAL_CONVERSATION,
    DATASET_CONVERSATION,
    PIPELINE_REGISTRY,
    PROCESSOR_KIND_CIRCUIT,
    PROCESSOR_KIND_EXTERNAL_CONVERSATION,
    PROCESSOR_KIND_RAGFLOW,
    PROCESSOR_KIND_SPREADSHEET,
    route_file,
)
from src.external_conversations.query_engine import ExternalConversationQueryEngine
from src.external_conversations.store import ExternalConversationStore


def _make_scope(tmp: str, source_group: str) -> IngestionScope:
    return IngestionScope(
        kb_name="kb_a",
        department_id="dept_1",
        kb_id=7,
        uploaded_by="tester",
        source_group=source_group,
    )


class ExternalConversationRoutingTests(unittest.TestCase):
    def test_txt_md_with_external_group_routes_to_conversation_pipeline(self):
        for ext in (".txt", ".md", ".markdown"):
            route = route_file(f"/tmp/chat{ext}", source_group=EXTERNAL_GROUP)
            self.assertTrue(route.supported, ext)
            self.assertEqual(route.spec.processor_kind, PROCESSOR_KIND_EXTERNAL_CONVERSATION)

    def test_txt_md_with_other_groups_route_to_document_rag(self):
        for group in (DOCS_GROUP, "测试数据"):
            route = route_file("/tmp/notes.md", source_group=group)
            self.assertTrue(route.supported)
            self.assertEqual(route.spec.processor_kind, PROCESSOR_KIND_RAGFLOW)

    def test_txt_md_without_group_defaults_to_document_rag(self):
        route = route_file("/tmp/notes.md")
        self.assertTrue(route.supported)
        self.assertEqual(route.spec.processor_kind, PROCESSOR_KIND_RAGFLOW)

    def test_existing_extensions_route_unchanged(self):
        expectations = {
            "/tmp/a.pdf": PROCESSOR_KIND_RAGFLOW,
            "/tmp/a.docx": PROCESSOR_KIND_RAGFLOW,
            "/tmp/a.xlsx": PROCESSOR_KIND_SPREADSHEET,
            "/tmp/a.edf": PROCESSOR_KIND_CIRCUIT,
        }
        for path, kind in expectations.items():
            route = route_file(path)
            self.assertTrue(route.supported, path)
            self.assertEqual(route.spec.processor_kind, kind)
            # and with a source group the non-text routes are untouched
            grouped = route_file(path, source_group=EXTERNAL_GROUP)
            self.assertEqual(grouped.spec.processor_kind, kind)

    def test_override_does_not_conflict_registered_extensions(self):
        # building the registry must not raise on overlapping text extensions
        specs = [PIPELINE_REGISTRY.by_processor_kind(kind) for kind in (
            PROCESSOR_KIND_RAGFLOW,
            PROCESSOR_KIND_SPREADSHEET,
            PROCESSOR_KIND_CIRCUIT,
            PROCESSOR_KIND_EXTERNAL_CONVERSATION,
        )]
        self.assertTrue(all(specs))


class ExternalConversationHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ext_conv_handler_")
        self.store = ExternalConversationStore(root=os.path.join(self.tmp, "conversations"))
        self.engine = ExternalConversationQueryEngine(root=self.tmp)  # index.db under tmp/departments/...
        ledger = PipelineDocumentStore(db_path=os.path.join(self.tmp, "ledger.db"))
        spec = PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_EXTERNAL_CONVERSATION)
        self.handler = ExternalConversationHandler(
            spec=spec,
            store=ledger,
            conversation_store=self.store,
            conversation_indexes=self.engine,
        )
        self.ledger = ledger
        # fake an archived file layout the orchestrator normally provides
        self.archive_dir = os.path.join(self.tmp, "archive")
        os.makedirs(self.archive_dir, exist_ok=True)
        with open(os.path.join(self.archive_dir, "chat.md"), "w", encoding="utf-8") as f:
            f.write("用户: LDO 压差?\n助手: 0.3V。\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _archived(self):
        from src.pipelines.ingestion import ArchivedFile
        from src.ingestion.container_inspector import ContainerInspection

        path = os.path.join(self.archive_dir, "chat.md")
        return ArchivedFile(
            original_path=path,
            archived_path=path,
            relative_local_path="departments/d/kbs/kb_a/chat.md",
            filename="chat.md",
            file_size=os.path.getsize(path),
            content_hash=f"hash-{os.path.getsize(path)}",
            source_group=EXTERNAL_GROUP,
            inspection=ContainerInspection(file_name="chat.md"),
        )

    def _submit(self):
        archived = self._archived()
        scope = _make_scope(self.tmp, EXTERNAL_GROUP)
        return self.handler.submit(
            scope=scope,
            archived=archived,
            default_dataset_kind="design",
            default_dataset_id="ds-1",
        )

    def test_handler_submit_writes_archive_json_index_ledger_in_order(self):
        result = self._submit()
        self.assertTrue(result.success)
        self.assertEqual(result.status, "indexed")

        convs = self.store.list_conversations("dept_1", "kb_a")
        self.assertEqual(len(convs), 1)
        self.assertEqual(len(convs[0].turns), 2)

        rows = self.engine.search_by_scope("dept_1", "kb_a", "LDO 压差")
        self.assertTrue(rows)

        record = self.ledger.get_document("kb_a", "chat.md", DATASET_CONVERSATION, department_id="dept_1")
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "indexed")
        self.assertEqual(record.processor_kind, PROCESSOR_KIND_EXTERNAL_CONVERSATION)
        self.assertEqual(record.content_kind, CONTENT_KIND_EXTERNAL_CONVERSATION)
        self.assertEqual(record.dataset_kind, DATASET_CONVERSATION)

    def test_handler_submit_failure_marks_record_failed(self):
        handler = ExternalConversationHandler(
            spec=self.handler.spec,
            store=self.ledger,
            conversation_store=self.store,
            conversation_indexes=self.engine,
            parser=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        archived = self._archived()
        scope = _make_scope(self.tmp, EXTERNAL_GROUP)
        result = handler.submit(scope=scope, archived=archived, default_dataset_kind="d", default_dataset_id="x")
        self.assertFalse(result.success)
        record = self.ledger.get_document("kb_a", "chat.md", DATASET_CONVERSATION, department_id="dept_1")
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "failed")

    def test_handler_llm_postprocess_fills_turns_and_summary(self):
        """Background post-process fills turns/title/summary for marker-less files."""
        import time

        markerless = os.path.join(self.archive_dir, "notes.md")
        with open(markerless, "w", encoding="utf-8") as f:
            f.write("今天讨论了 LDO 压差问题。\n结论是最大 0.3V,需要注意散热。\n" * 5)

        class FakeLLM:
            def invoke(self, messages, **kwargs):
                text = messages[0]["content"]
                if "资料提炼助手" in text:
                    return '{"summary": "讨论了LDO压差结论。", "key_points": ["最大压差0.3V", "注意散热"]}'
                return '{"title": "LDO 压差讨论", "turns": [{"role": "user", "content": "压差要求?"}, {"role": "assistant", "content": "最大 0.3V"}]}'

        handler = ExternalConversationHandler(
            spec=self.handler.spec,
            store=self.ledger,
            conversation_store=self.store,
            conversation_indexes=self.engine,
            chat_model=FakeLLM(),
        )
        archived = self._archived()
        archived.filename = "notes.md"
        archived.original_path = markerless
        archived.archived_path = markerless
        scope = _make_scope(self.tmp, EXTERNAL_GROUP)

        started = time.monotonic()
        result = handler.submit(scope=scope, archived=archived, default_dataset_kind="d", default_dataset_id="x")
        elapsed = time.monotonic() - started
        # submit must NOT block on LLM work
        self.assertLess(elapsed, 2.0)
        self.assertTrue(result.success)

        loaded = [c for c in self.store.list_conversations("dept_1", "kb_a") if c.source_file == "notes.md"][0]
        handler._postprocess_llm(loaded)  # run what the background thread runs

        target = [c for c in self.store.list_conversations("dept_1", "kb_a") if c.source_file == "notes.md"][0]
        self.assertEqual(len(target.turns), 2)
        self.assertEqual(target.title, "LDO 压差讨论")
        self.assertIn("LDO压差", target.summary)
        self.assertEqual(target.key_points, ["最大压差0.3V", "注意散热"])
        # keyword index refreshed with inferred turns
        rows = self.engine.search_by_scope("dept_1", "kb_a", "压差要求")
        self.assertTrue(rows)

    def test_handler_delete_record_cleans_all_artifacts_reverse_order(self):
        self._submit()
        record = self.ledger.get_document("kb_a", "chat.md", DATASET_CONVERSATION, department_id="dept_1")

        class FakeArchive:
            def __init__(self):
                self.removed = []

            def remove_record_archive(self, rec):
                self.removed.append(rec.id)

        fake_archive = FakeArchive()
        delete_result = self.handler.delete_record(record, fake_archive)
        self.assertTrue(delete_result.ok)
        self.assertEqual(fake_archive.removed, [record.id])
        self.assertIsNone(self.ledger.get_document("kb_a", "chat.md", DATASET_CONVERSATION, department_id="dept_1"))
        self.assertEqual(self.store.list_conversations("dept_1", "kb_a"), [])
        self.assertEqual(self.engine.list_conversations("dept_1", "kb_a"), [])


if __name__ == "__main__":
    unittest.main()
