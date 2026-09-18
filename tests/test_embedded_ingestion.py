import hashlib
import io
import os
import shutil
import tempfile
import unittest
import zipfile

from src.pipelines.document_store import PipelineDocumentRecord
from src.pipelines.ingestion import (
    HandlerResult,
    IngestionOrchestrator,
    IngestionScope,
    PipelineHandler,
)
from src.pipelines.registry import (
    PIPELINE_REGISTRY,
    PROCESSOR_KIND_RAGFLOW,
    PROCESSOR_KIND_SPREADSHEET,
)


def _docx_bytes(*paragraphs: str) -> bytes:
    body = "".join(
        "<w:p><w:r><w:t>"
        + "</w:t></w:r><w:r><w:t>".join(segment for segment in paragraph)
        + "</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def _xlsx_bytes(marker: str = "sheet") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", f"<workbook>{marker}</workbook>")
    return buffer.getvalue()


class _Store:
    def __init__(self):
        self.documents: list[dict] = []
        self.embedding_meta: list[tuple] = []
        self.parent_refs: list[tuple] = []

    def find_by_hash(self, *args, **kwargs):
        return None

    def upsert_document(self, **kwargs):
        self.documents.append(kwargs)

    def get_document(self, *args, **kwargs):
        return type("Record", (), {"id": 501})()

    def update_embedding_meta(self, record_id, parent_document, embed_depth):
        self.embedding_meta.append((record_id, parent_document, embed_depth))

    def add_parent_reference(self, record_id, parent_document):
        self.parent_refs.append((record_id, parent_document))

    def list_embedded_children(self, *args, **kwargs):
        return []

    def delete_document_by_id(self, record_id):
        pass


class _Archive:
    def __init__(self, root):
        self.root = root

    def archive_source_file(self, kb_name, file_path, source_group, department_id=None):
        os.makedirs(self.root, exist_ok=True)
        archived_path = os.path.join(self.root, os.path.basename(file_path))
        shutil.copy2(file_path, archived_path)
        return archived_path, os.path.basename(file_path), source_group or "design data"

    def kb_path(self, kb_name, department_id=None):
        return self.root

    def record_archive_exists(self, record):
        return True


class _RecordingHandler(PipelineHandler):
    def __init__(self, spec):
        self.spec = spec
        self.submissions: list[dict] = []

    def existing_record_dataset_kind(self, default_dataset_kind):
        return self.spec.dataset_kind or default_dataset_kind

    def submit(self, scope, archived, default_dataset_kind, default_dataset_id, progress_callback=None):
        self.submissions.append(
            {
                "filename": archived.filename,
                "embed_depth": scope.embed_depth,
                "parent_document": scope.parent_document,
            }
        )
        return HandlerResult(
            success=True,
            message=f"ok {archived.filename}",
            record_id=len(self.submissions) + 100,
        )


class EmbeddedIngestionTests(unittest.TestCase):
    def _orchestrator(self, tmp):
        table_spec = PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_SPREADSHEET)
        rag_spec = PIPELINE_REGISTRY.by_processor_kind(PROCESSOR_KIND_RAGFLOW)
        self.table_handler = _RecordingHandler(table_spec)
        self.rag_handler = _RecordingHandler(rag_spec)
        self.store = _Store()
        return IngestionOrchestrator(
            backend_name="test",
            store=self.store,
            archive=_Archive(os.path.join(tmp, "archive")),
            handlers={
                PROCESSOR_KIND_SPREADSHEET: self.table_handler,
                PROCESSOR_KIND_RAGFLOW: self.rag_handler,
            },
            audit_callback=lambda *args, **kwargs: None,
            content_hash_callback=lambda path: hashlib.sha256(open(path, "rb").read()).hexdigest(),
            remote_document_exists_callback=lambda record: False,
        )

    def test_container_upload_expands_children_into_matching_pipelines(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = os.path.join(tmp, "架构说明书.xlsx")
            with zipfile.ZipFile(parent, "w") as archive:
                archive.writestr(
                    "xl/embeddings/Microsoft_Word_Document.docx",
                    _docx_bytes(("5.", "3", " ", "摄像头模块")),
                )
                archive.writestr(
                    "xl/embeddings/Microsoft_Excel_Worksheet.xlsx",
                    _xlsx_bytes(),
                )
            orchestrator = self._orchestrator(tmp)

            result = orchestrator.upload_files(
                [parent],
                IngestionScope(kb_name="kb_hw", department_id="dept_hw", uploaded_by="alice"),
                default_dataset_kind="design",
                default_dataset_id="dataset-1",
            )

        self.assertEqual(result.failed_count, 0)
        # docx child -> RAGFlow pipeline
        self.assertEqual(len(self.rag_handler.submissions), 1)
        child = self.rag_handler.submissions[0]
        self.assertEqual(child["filename"], "架构说明书 [嵌入] 5.3 摄像头模块.docx")
        self.assertEqual(child["embed_depth"], 1)
        self.assertEqual(child["parent_document"], "架构说明书.xlsx")
        # xlsx child -> spreadsheet pipeline, in addition to the parent itself
        table_names = [item["filename"] for item in self.table_handler.submissions]
        self.assertIn("架构说明书.xlsx", table_names)
        self.assertIn("架构说明书 [嵌入] Microsoft_Excel_Worksheet.xlsx", table_names)
        # Both children carry embedding metadata for cascade bookkeeping.
        depths = {depth for _, _, depth in self.store.embedding_meta}
        self.assertEqual(depths, {1})
        self.assertTrue(all(parent_name == "架构说明书.xlsx" for _, parent_name, _ in self.store.embedding_meta))
        joined = "\n".join(result.messages)
        self.assertIn("已展开 2 个嵌入对象", joined)
        self.assertIn("5.3 摄像头模块", joined)

    def test_reupload_of_existing_parent_backfills_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = os.path.join(tmp, "架构说明书.xlsx")
            with zipfile.ZipFile(parent, "w") as archive:
                archive.writestr(
                    "xl/embeddings/Microsoft_Word_Document.docx",
                    _docx_bytes(("MCU 系统方案设计",)),
                )
            parent_hash = hashlib.sha256(open(parent, "rb").read()).hexdigest()
            orchestrator = self._orchestrator(tmp)
            existing_parent = PipelineDocumentRecord(
                id=88,
                kb_name="kb_hw",
                document_name="架构说明书.xlsx",
                original_file_name="架构说明书.xlsx",
                dataset_kind="table",
                dataset_id="ds-1",
                document_id="remote-88",
                source_group="design",
                department_id="dept_hw",
                uploaded_by="alice",
                status="indexed",
                processor_kind=PROCESSOR_KIND_SPREADSHEET,
            )
            self.store.find_by_hash = lambda kb_name, dataset_kind, content_hash, department_id=None: (
                existing_parent if content_hash == parent_hash else None
            )

            result = orchestrator.upload_files(
                [parent],
                IngestionScope(kb_name="kb_hw", department_id="dept_hw", uploaded_by="alice"),
                default_dataset_kind="design",
                default_dataset_id="dataset-1",
            )

        # 父文档走复用分支，同时把此前未展开的嵌入子文档补齐。
        self.assertFalse(self.table_handler.submissions)
        self.assertEqual(len(self.rag_handler.submissions), 1)
        self.assertEqual(
            self.rag_handler.submissions[0]["filename"],
            "架构说明书 [嵌入] MCU 系统方案设计.docx",
        )
        self.assertEqual(self.store.embedding_meta[0][1], "架构说明书.xlsx")
        self.assertIn("已展开 1 个嵌入对象", "\n".join(result.messages))

    def test_embedded_child_is_not_recursively_expanded(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The embedded xlsx itself embeds another docx; depth guard must stop.
            nested = _xlsx_bytes_with_child()
            parent = os.path.join(tmp, "parent.xlsx")
            with zipfile.ZipFile(parent, "w") as archive:
                archive.writestr("xl/embeddings/nested.xlsx", nested)
            orchestrator = self._orchestrator(tmp)

            orchestrator.upload_files(
                [parent],
                IngestionScope(kb_name="kb_hw", department_id="dept_hw", uploaded_by="alice"),
                default_dataset_kind="design",
                default_dataset_id="dataset-1",
            )

        table_names = [item["filename"] for item in self.table_handler.submissions]
        self.assertEqual(len(table_names), 2)
        self.assertTrue(any(name.endswith("nested.xlsx") for name in table_names))
        self.assertFalse(any("grandchild" in name for name in table_names))


def _xlsx_bytes_with_child() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook>nested</workbook>")
        archive.writestr(
            "xl/embeddings/grandchild.docx",
            _docx_bytes(("不该被展开",)),
        )
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
