import unittest
from unittest.mock import patch

from src.pipelines.document_rag.ragflow_backend import RAGFlowBackend
from src.pipelines.document_store import PipelineDocumentRecord
from src.pipelines.ingestion import HandlerDeleteResult
from src.pipelines.registry import PROCESSOR_KIND_RAGFLOW


def _record(record_id: int, name: str) -> PipelineDocumentRecord:
    return PipelineDocumentRecord(
        id=record_id,
        kb_name="kb_hw",
        document_name=name,
        original_file_name=name,
        dataset_kind="design",
        dataset_id="ds-1",
        document_id=f"remote-{record_id}",
        source_group="design",
        department_id="dept_hw",
        uploaded_by="alice",
        status="parsed",
        processor_kind=PROCESSOR_KIND_RAGFLOW,
    )


class _Store:
    def __init__(self, children: list[PipelineDocumentRecord], parents: dict[int, list[str]]):
        self.children = children
        self.parents = parents

    def list_embedded_children(self, kb_name, department_id, parent_document):
        return [
            child
            for child in self.children
            if parent_document in self.parents.get(child.id, [])
        ]

    def remove_parent_reference(self, record_id, parent_document):
        self.parents[record_id] = [
            item for item in self.parents.get(record_id, []) if item != parent_document
        ]
        return not self.parents[record_id]


class _Runtime:
    def __init__(self):
        self.deleted: list[str] = []

    def delete_record(self, record):
        self.deleted.append(record.document_name)
        return HandlerDeleteResult(ok=True, message=f"deleted {record.document_name}")


class EmbeddedCascadeDeleteTests(unittest.TestCase):
    def _backend(self, children, parents):
        backend = object.__new__(RAGFlowBackend)
        backend.name = "ragflow"
        backend.store = _Store(children, parents)
        backend.runtime = _Runtime()
        backend._audit = lambda *args, **kwargs: None
        return backend

    def test_orphaned_children_are_deleted_and_shared_children_are_kept(self):
        shared = _record(11, "架构说明书 [嵌入] MCU 系统方案设计.docx")
        orphan = _record(12, "架构说明书 [嵌入] 5.3 摄像头模块.docx")
        backend = self._backend(
            [shared, orphan],
            {11: ["架构说明书.xlsx", "第二份架构说明书.xlsx"], 12: ["架构说明书.xlsx"]},
        )

        with patch("src.core.doc_assets.sync_file_deleted", return_value=None):
            errors = backend._delete_embedded_children(
                _record(1, "架构说明书.xlsx")
            )

        self.assertEqual(errors, [])
        # 共享子文档只移除引用；独享子文档走完整删除。
        self.assertEqual(backend.runtime.deleted, ["架构说明书 [嵌入] 5.3 摄像头模块.docx"])
        self.assertEqual(backend.store.parents[11], ["第二份架构说明书.xlsx"])
        self.assertEqual(backend.store.parents[12], [])

    def test_child_cleanup_failures_are_reported_but_do_not_raise(self):
        child = _record(12, "架构说明书 [嵌入] 5.3 摄像头模块.docx")
        backend = self._backend([child], {12: ["架构说明书.xlsx"]})

        def _boom(record):
            raise RuntimeError("remote delete failed")

        backend.runtime.delete_record = _boom

        errors = backend._delete_embedded_children(_record(1, "架构说明书.xlsx"))

        self.assertEqual(len(errors), 1)
        self.assertIn("remote delete failed", errors[0])


if __name__ == "__main__":
    unittest.main()
