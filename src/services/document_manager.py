from dataclasses import dataclass, field
from typing import Callable

from src.core.logger import warn
from src.pipelines.document_rag.base import RAGBackend
from src.pipelines.document_rag.schemas import BackendResult, IngestResult, RequestContext
from src.services.document_routing import supported_pipeline_for_file
from src.services.pipeline_asset_cleanup import PipelineAssetCleanupService


@dataclass
class DocumentCleanupResult:
    ok: bool
    message: str
    partial: bool = False
    errors: list[str] = field(default_factory=list)


class DocumentManager:
    """Document lifecycle facade above concrete processing pipelines."""

    def __init__(self, processing_backend: RAGBackend):
        self.processing_backend = processing_backend

    def upload_files(
        self,
        files,
        target_kb: str,
        ctx: RequestContext | None = None,
        source_group: str | None = None,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> IngestResult:
        backend_name = getattr(self.processing_backend, "name", "unknown")
        if not files:
            return IngestResult(success_count=0, total_count=0, messages=["未选择文件"], backend=backend_name)

        file_paths = [file if isinstance(file, str) else file.name for file in files]
        if not target_kb:
            return IngestResult(
                success_count=0,
                total_count=len(file_paths),
                messages=["未选择目标知识库"],
                backend=backend_name,
                failed_count=len(file_paths),
            )

        return self.processing_backend.upload_files(
            target_kb,
            file_paths,
            ctx=ctx,
            source_group=source_group,
            progress_callback=progress_callback,
        )

    def processing_pipeline_for_file(self, file_path: str) -> str | None:
        return supported_pipeline_for_file(file_path)

    def delete_document(self, document_id: str, kb_name: str, ctx: RequestContext | None = None) -> BackendResult:
        """Return the full BackendResult so callers can branch on ``ok``
        rather than sniffing error prefixes out of ``message``."""
        return self.processing_backend.delete_document(kb_name, document_id, ctx=ctx)

    def delete_knowledge_base_documents(
        self,
        kb_name: str,
        ctx: RequestContext | None = None,
    ) -> DocumentCleanupResult:
        errors: list[str] = []

        result = self.processing_backend.delete_knowledge_base(kb_name, ctx=ctx)
        if result is not None:
            if not result.ok:
                return DocumentCleanupResult(ok=False, message=result.message)

            department_id = None
            if ctx is not None:
                department_id = ctx.metadata.get("resource_department_id") or ctx.metadata.get("department_id")
            cleanup_result = PipelineAssetCleanupService().cleanup_knowledge_base(kb_name, department_id)
            errors.extend(cleanup_result.errors)

            if errors:
                warn(f"删除文档资产过程中出现部分错误: {'; '.join(errors)}")
                return DocumentCleanupResult(
                    ok=True,
                    partial=True,
                    errors=errors,
                    message=f"知识库 '{kb_name}' 已删除，部分归档资产清理失败。",
                )
            return DocumentCleanupResult(ok=True, message=f"知识库 '{kb_name}' 已被彻底删除")

        return DocumentCleanupResult(
            ok=False,
            message="当前文档后端不支持知识库级删除。",
        )

    def list_files(self, kb_name: str, ctx: RequestContext | None = None) -> list[str]:
        return [doc.name for doc in self.processing_backend.list_documents(kb_name, ctx=ctx)]

    def list_file_infos(self, kb_name: str, ctx: RequestContext | None = None):
        return self.processing_backend.list_documents(kb_name, ctx=ctx)

    def list_parse_tasks(self, kb_name: str | None = None, ctx: RequestContext | None = None):
        return self.processing_backend.list_parse_tasks(kb_name, ctx=ctx)

    def requeue_dead_letter_parse_tasks(self, kb_name: str, ctx: RequestContext | None = None) -> list[str]:
        return self.processing_backend.requeue_dead_letter_parse_tasks(kb_name, ctx=ctx)

    def pause_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> str:
        return self.processing_backend.pause_parse_task(task_id, ctx=ctx).message

    def resume_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> str:
        return self.processing_backend.resume_parse_task(task_id, ctx=ctx).message

    def delete_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> str:
        return self.processing_backend.delete_parse_task(task_id, ctx=ctx).message

    def clear_finished_parse_tasks(self, kb_name: str | None = None, ctx: RequestContext | None = None):
        self.processing_backend.clear_finished_parse_tasks(kb_name, ctx=ctx)

    def get_parse_result(self, kb_name: str, document_id: str, ctx: RequestContext | None = None):
        return self.processing_backend.get_parse_result(kb_name, document_id, ctx=ctx)
