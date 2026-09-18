import os
import tempfile
import unittest

from src.pipelines.document_store import PipelineDocumentStore


class EmbeddedStoreMetadataTests(unittest.TestCase):
    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = PipelineDocumentStore(db_path=os.path.join(tmp.name, "pipeline.db"))
        store.upsert_document(
            kb_name="kb_hw",
            document_name="架构说明书 [嵌入] 5.3 摄像头模块.docx",
            dataset_kind="design",
            dataset_id="ds-1",
            document_id="remote-1",
            source_group="design",
            department_id="dept_hw",
            uploaded_by="alice",
        )
        record = store.get_document(
            "kb_hw",
            "架构说明书 [嵌入] 5.3 摄像头模块.docx",
            "design",
            department_id="dept_hw",
        )
        return store, record

    def test_embedding_meta_and_parent_reference_refcount(self):
        store, record = self._store()

        store.update_embedding_meta(record.id, "架构说明书.xlsx", 1)
        child = store.get_document_by_id(record.id)
        self.assertEqual(child.embed_depth, 1)
        self.assertEqual(store._parse_parent_documents(child.parent_document), ["架构说明书.xlsx"])
        self.assertEqual(
            [item.id for item in store.list_embedded_children("kb_hw", "dept_hw", "架构说明书.xlsx")],
            [record.id],
        )
        self.assertEqual(store.list_embedded_children("kb_hw", "dept_hw", "其他.xlsx"), [])

        store.add_parent_reference(record.id, "第二份架构说明书.xlsx")
        self.assertEqual(
            len(store.list_embedded_children("kb_hw", "dept_hw", "第二份架构说明书.xlsx")),
            1,
        )
        # 仍有其他父引用时，移除一个不应判定为孤儿。
        self.assertFalse(store.remove_parent_reference(record.id, "第二份架构说明书.xlsx"))
        self.assertTrue(store.remove_parent_reference(record.id, "架构说明书.xlsx"))

    def test_non_embedded_documents_are_not_listed_as_children(self):
        store, record = self._store()

        self.assertEqual(store.list_embedded_children("kb_hw", "dept_hw", "架构说明书.xlsx"), [])


if __name__ == "__main__":
    unittest.main()
