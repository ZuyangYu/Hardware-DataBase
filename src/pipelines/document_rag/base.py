from abc import ABC, abstractmethod
from typing import Callable

from src.pipelines.document_rag.schemas import BackendHealth, BackendResult, DocumentInfo, Evidence, IngestResult, ParseResult, RequestContext


class RAGBackend(ABC):
    name: str

    def list_knowledge_bases(self) -> list[str]:
        """枚举该后端管理的全部知识库名称（不含权限过滤）。

        权限过滤由 AppPipeline 基于 RequestContext 完成，后端只负责枚举。
        """
        return []

    def create_kb_storage(self, kb_name: str, ctx: RequestContext | None = None) -> None:
        """为新建知识库创建后端所需的物理/逻辑存储。

        默认无操作；RAGFlow 这类远端后端通常无需预处理。
        """
        return None

    @abstractmethod
    def upload_files(
        self,
        kb_name: str,
        files: list[str],
        ctx: RequestContext | None = None,
        source_group: str | None = None,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> IngestResult:
        """Application upload entrypoint for backend-specific document flows."""

    @abstractmethod
    def retrieve(
        self,
        kb_name: str,
        query: str,
        top_k: int | None = None,
        ctx: RequestContext | None = None,
        filters: dict | None = None,
    ) -> list[Evidence]:
        """Retrieve evidence for a query."""

    @abstractmethod
    def delete_document(self, kb_name: str, document_id: str, ctx: RequestContext | None = None) -> BackendResult:
        """Delete a document from a knowledge base."""

    @abstractmethod
    def list_documents(self, kb_name: str, ctx: RequestContext | None = None) -> list[DocumentInfo]:
        """List documents in a knowledge base."""

    def get_parse_result(self, kb_name: str, document_id: str, ctx: RequestContext | None = None) -> ParseResult | None:
        """Return parsed chunks for a document when the backend can expose them."""
        return None

    def delete_knowledge_base(self, kb_name: str, ctx: RequestContext | None = None) -> BackendResult | None:
        """Delete a whole knowledge base when the backend owns external assets."""
        return None

    def list_parse_tasks(self, kb_name: str | None = None, ctx: RequestContext | None = None):
        """Return backend parse tasks when supported."""
        return []

    def pause_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> BackendResult:
        """Pause a parse task when supported."""
        return BackendResult(ok=False, message="当前后端不支持暂停解析任务", backend=self.name)

    def resume_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> BackendResult:
        """Resume a parse task when supported."""
        return BackendResult(ok=False, message="当前后端不支持启动解析任务", backend=self.name)

    def delete_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> BackendResult:
        """Delete or cancel a parse task when supported."""
        return BackendResult(ok=False, message="当前后端不支持删除解析任务", backend=self.name)

    def clear_finished_parse_tasks(self, kb_name: str | None = None, ctx: RequestContext | None = None):
        """Clear terminal parse tasks when supported."""
        return None

    @abstractmethod
    def health_check(self) -> BackendHealth:
        """Return backend health."""
