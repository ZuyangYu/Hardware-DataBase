import json
import os
import shutil
import time
import traceback
from typing import Callable, Generator

import config.settings
from src.core.custom_rag_chat import CustomRAGChat
from src.core.routed_retriever import routed_retrieve
from src.core.hybrid_retriever import invalidate_bm25_cache
from src.core.logger import error, log, warn
from src.core.resource_manager import resource_manager
from src.ingestion.data_loader import get_kb_path
from src.ingestion.index_builder import get_or_build_index, invalidate_index_cache
from src.ingestion.parse_strategies import parse_by_source_group
from src.ingestion.parse_tasks import ParseTaskManager
from src.ingestion.source_groups import SourceGroupClassification, classify_source_group, safe_source_group
from src.rag_backends.base import RAGBackend
from src.rag_backends.schemas import BackendHealth, BackendResult, DocumentInfo, Evidence, IngestResult, ParsedChunk, ParseResult, RequestContext


_MAX_METADATA_VALUE_LENGTH = 2000


def _sanitize_chroma_metadata(metadata: dict | None) -> tuple[dict, list[str]]:
    clean = {}
    converted_keys = []
    for key, value in (metadata or {}).items():
        if isinstance(value, bool):
            clean[key] = int(value)
            continue
        if isinstance(value, (str, int, float)) or value is None:
            clean[key] = value
            continue

        converted_keys.append(key)
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        if len(serialized) > _MAX_METADATA_VALUE_LENGTH:
            serialized = f"{serialized[:_MAX_METADATA_VALUE_LENGTH]}..."
        clean[key] = serialized
    return clean, converted_keys


class LocalRAGBackend(RAGBackend):
    """Current in-process RAG implementation backed by Docling, LlamaIndex, Chroma and BM25."""

    name = "local"

    def __init__(self):
        if not resource_manager.initialize():
            raise RuntimeError("资源管理器初始化失败")
        self.parse_tasks = ParseTaskManager(self._run_parse_task)

    def get_index(self, kb_name: str):
        return get_or_build_index(kb_name, resource_manager.chroma_client, use_cache=True)

    def _check_kb_access(self, kb_name: str, ctx: RequestContext | None):
        if ctx and not ctx.can_access_kb(kb_name):
            raise PermissionError(f"用户 {ctx.user_id} 无权访问知识库: {kb_name}")

    def ingest(
        self,
        kb_name: str,
        files: list[str],
        ctx: RequestContext | None = None,
        source_group: str | None = None,
    ) -> IngestResult:
        self._check_kb_access(kb_name, ctx)
        if not files:
            return IngestResult(success_count=0, total_count=0, messages=["未选择文件"], backend=self.name)
        if not kb_name:
            return IngestResult(success_count=0, total_count=len(files), messages=["❌ 未选择目标知识库"], backend=self.name)

        messages = []
        success_count = 0
        for file_path in files:
            try:
                result = self.add_document(kb_name, file_path, source_group=source_group)
                messages.append(result.message)
                if result.ok:
                    success_count += 1
            except Exception as exc:
                error(f"上传文件失败 {file_path}: {exc}")
                messages.append(f"❌ {os.path.basename(file_path)}: {exc}")

        return IngestResult(success_count=success_count, total_count=len(files), messages=messages, backend=self.name)

    def add_document(
        self,
        kb_name: str,
        temp_file_path: str,
        source_group: str | None = None,
        progress_callback: Callable[[int, str], None] | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> BackendResult:
        def report(progress: int, stage: str):
            if progress_callback:
                progress_callback(progress, stage)

        def check():
            if checkpoint:
                checkpoint()

        lock = resource_manager.get_kb_lock(kb_name)
        with lock:
            try:
                check()
                if not os.path.exists(temp_file_path):
                    return BackendResult(ok=False, message="❌ 临时文件不存在", backend=self.name)

                original_filename = os.path.basename(temp_file_path)
                if source_group:
                    source_group = safe_source_group(source_group)
                    classification = SourceGroupClassification(
                        source_group,
                        1.0,
                        "user-selected upload type",
                    )
                else:
                    classification = classify_source_group(original_filename)
                    source_group = safe_source_group(classification.group)
                filename = original_filename
                target_dir = os.path.join(get_kb_path(kb_name), source_group)
                os.makedirs(target_dir, exist_ok=True)
                target_path = os.path.join(target_dir, filename)

                if os.path.exists(target_path):
                    base, ext = os.path.splitext(filename)
                    filename = f"{base}_{int(time.time())}{ext}"
                    target_path = os.path.join(target_dir, filename)

                report(10, "保存源文件")
                shutil.copy2(temp_file_path, target_path)

                check()
                log(f"开始解析文件: {filename}")
                report(25, "加载知识库索引")
                index = self.get_index(kb_name)
                check()
                report(40, "解析文档内容")
                nodes = parse_by_source_group(target_path, filename, kb_name, source_group=source_group)

                if not nodes:
                    raise ValueError("文件解析后未生成任何有效节点")

                check()
                log(f"解析成功，准备写入 {len(nodes)} 个节点到 {kb_name}")
                report(70, f"写入索引节点（{len(nodes)} 个分块）")
                relative_path = os.path.join(source_group, filename)
                for chunk_index, node in enumerate(nodes):
                    node.metadata["file_name"] = filename
                    node.metadata["original_file_name"] = original_filename
                    node.metadata["relative_path"] = relative_path
                    node.metadata["source_group"] = source_group
                    node.metadata["source_group_confidence"] = classification.confidence
                    node.metadata["source_group_reason"] = classification.reason
                    node.metadata["chunk_index"] = chunk_index
                    clean_metadata, converted_keys = _sanitize_chroma_metadata(node.metadata)
                    node.metadata = clean_metadata
                    if converted_keys:
                        node.excluded_embed_metadata_keys = sorted(
                            set(getattr(node, "excluded_embed_metadata_keys", []) or []) | set(converted_keys)
                        )
                        node.excluded_llm_metadata_keys = sorted(
                            set(getattr(node, "excluded_llm_metadata_keys", []) or []) | set(converted_keys)
                        )

                index.docstore.add_documents(nodes)
                index.insert_nodes(nodes)

                report(90, "持久化索引")
                persist_dir = config.settings.get_kb_storage_path(kb_name)
                index.storage_context.persist(persist_dir=persist_dir)

                invalidate_bm25_cache(kb_name)
                invalidate_index_cache(kb_name)

                log(f"✅ 索引及同步持久化成功: {filename} (KB: {kb_name})")
                report(100, "解析完成")
                return BackendResult(ok=True, message=f"✅ 索引成功: {filename}", backend=self.name)
            except Exception as exc:
                error(f"❌ 上传文档处理失败: {exc}")
                if "target_path" in locals() and os.path.exists(target_path):
                    try:
                        os.remove(target_path)
                    except Exception as cleanup_error:
                        error(f"清理临时文件失败: {target_path}, 错误: {cleanup_error}")
                raise

    def _run_parse_task(
        self,
        kb_name: str,
        file_path: str,
        source_group: str,
        progress_callback: Callable[[int, str], None],
        checkpoint: Callable[[], None],
    ) -> str:
        result = self.add_document(
            kb_name,
            file_path,
            source_group,
            progress_callback=progress_callback,
            checkpoint=checkpoint,
        )
        if not result.ok:
            raise ValueError(result.message)
        return result.message

    def submit_parse_tasks(
        self,
        kb_name: str,
        files: list[str],
        source_group: str | None = None,
        ctx: RequestContext | None = None,
    ):
        self._check_kb_access(kb_name, ctx)
        created_by = ctx.user_id if ctx else ""
        task_source_group = safe_source_group(source_group) if source_group else ""
        return self.parse_tasks.submit(kb_name, files, task_source_group, created_by=created_by)

    def list_parse_tasks(self, kb_name: str | None = None, ctx: RequestContext | None = None):
        if kb_name:
            self._check_kb_access(kb_name, ctx)
        return self.parse_tasks.list_tasks(kb_name)

    def pause_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> BackendResult:
        task = self.parse_tasks.get_task(task_id)
        if not task:
            return BackendResult(ok=False, message="解析任务不存在", backend=self.name)
        self._check_kb_access(task.kb_name, ctx)
        ok = self.parse_tasks.pause(task_id)
        return BackendResult(ok=ok, message="已暂停解析任务" if ok else "当前状态不可暂停", backend=self.name)

    def resume_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> BackendResult:
        task = self.parse_tasks.get_task(task_id)
        if not task:
            return BackendResult(ok=False, message="解析任务不存在", backend=self.name)
        self._check_kb_access(task.kb_name, ctx)
        ok = self.parse_tasks.resume(task_id)
        return BackendResult(ok=ok, message="已启动解析任务" if ok else "当前状态不可启动", backend=self.name)

    def delete_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> BackendResult:
        task = self.parse_tasks.get_task(task_id)
        if not task:
            return BackendResult(ok=False, message="解析任务不存在", backend=self.name)
        self._check_kb_access(task.kb_name, ctx)
        is_running = task.status == "running"
        ok = self.parse_tasks.delete(task_id)
        message = "已请求取消解析任务" if is_running else "已删除解析任务"
        return BackendResult(ok=ok, message=message if ok else "解析任务删除失败", backend=self.name)

    def clear_finished_parse_tasks(self, kb_name: str | None = None, ctx: RequestContext | None = None):
        if kb_name:
            self._check_kb_access(kb_name, ctx)
        self.parse_tasks.clear_finished(kb_name)

    def retrieve(self, kb_name: str, query: str, top_k: int | None = None, ctx: RequestContext | None = None) -> list[Evidence]:
        self._check_kb_access(kb_name, ctx)
        index = self.get_index(kb_name)
        nodes = routed_retrieve(query, index, kb_name, top_k or config.settings.FINAL_TOP_K)
        evidences = []
        for node in nodes:
            metadata = dict(node.node.metadata or {})
            evidences.append(
                Evidence(
                    id=node.node.node_id,
                    content=node.node.get_content(),
                    source_name=metadata.get("file_name", "未知来源"),
                    source_type=metadata.get("source_type", "document"),
                    score=float(node.score or 0.0),
                    metadata=metadata,
                    backend=self.name,
                    retriever="routed_hybrid",
                )
            )
        return evidences

    def stream_answer(
        self,
        kb_name: str,
        query: str,
        history: list[tuple[str, str]],
        ctx: RequestContext | None = None,
    ) -> Generator[str, None, None]:
        self._check_kb_access(kb_name, ctx)
        index = self.get_index(kb_name)
        chat_engine = CustomRAGChat(kb_name, index)
        yield from chat_engine.chat(query, history)

    def delete_document(self, kb_name: str, document_id: str, ctx: RequestContext | None = None) -> BackendResult:
        self._check_kb_access(kb_name, ctx)
        lock = resource_manager.get_kb_lock(kb_name)
        with lock:
            if not document_id:
                return BackendResult(ok=False, message="❌ 文件名不能为空", backend=self.name)

            try:
                index = self.get_index(kb_name)
                all_nodes = list(index.docstore.docs.values())
                requested_name = os.path.basename(document_id)
                target_ref_doc_ids = set()
                for node in all_nodes:
                    node_file_name = node.metadata.get("file_name")
                    node_relative_path = node.metadata.get("relative_path")
                    if (
                        document_id in {node_file_name, node_relative_path}
                        or requested_name == node_file_name
                    ) and node.ref_doc_id:
                        target_ref_doc_ids.add(node.ref_doc_id)

                file_path = os.path.join(get_kb_path(kb_name), document_id)
                if not target_ref_doc_ids:
                    warn(f"在索引中未找到与文件 '{document_id}' 关联的文档。可能已被删除或从未索引。")
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        return BackendResult(ok=True, message=f"✅ 索引中无记录，已删除物理文件: {document_id}", backend=self.name)
                    return BackendResult(ok=False, message="索引和物理路径中均未找到该文件。", backend=self.name)

                for ref_doc_id in target_ref_doc_ids:
                    index.delete_ref_doc(ref_doc_id, delete_from_store=True)
                    log(f"从索引中删除 ref_doc_id: {ref_doc_id}")

                persist_dir = config.settings.get_kb_storage_path(kb_name)
                index.storage_context.persist(persist_dir=persist_dir)

                if os.path.exists(file_path):
                    os.remove(file_path)

                invalidate_index_cache(kb_name)
                invalidate_bm25_cache(kb_name)

                log(f"✅ 文档已从物理磁盘和索引库中同步移除: {document_id}")
                return BackendResult(ok=True, message=f"✅ 已成功删除文档: {document_id}", backend=self.name)
            except Exception as exc:
                error(f"❌ 删除文档失败: {exc}")
                traceback.print_exc()
                return BackendResult(ok=False, message=f"❌ 删除失败: {exc}", backend=self.name)

    def list_documents(self, kb_name: str, ctx: RequestContext | None = None) -> list[DocumentInfo]:
        self._check_kb_access(kb_name, ctx)
        if not kb_name:
            return []
        kb_path = get_kb_path(kb_name)
        if not os.path.exists(kb_path):
            return []
        documents = []
        for root, _, files in os.walk(kb_path):
            for name in files:
                full_path = os.path.join(root, name)
                relative_path = os.path.relpath(full_path, kb_path)
                source_group = os.path.dirname(relative_path).split(os.sep)[0] if os.path.dirname(relative_path) else ""
                documents.append(
                    DocumentInfo(
                        id=relative_path,
                        name=relative_path,
                        metadata={"source_group": source_group, "file_name": name},
                        backend=self.name,
                    )
                )
        return sorted(documents, key=lambda doc: doc.name)

    def get_parse_result(self, kb_name: str, document_id: str, ctx: RequestContext | None = None) -> ParseResult | None:
        self._check_kb_access(kb_name, ctx)
        if not document_id:
            return None
        index = self.get_index(kb_name)
        requested_name = os.path.basename(document_id)
        chunks = []
        file_name = document_id
        for node in index.docstore.docs.values():
            metadata = dict(node.metadata or {})
            node_file_name = metadata.get("file_name", "")
            node_relative_path = metadata.get("relative_path", "")
            if document_id in {node_file_name, node_relative_path} or requested_name == node_file_name:
                file_name = node_relative_path or node_file_name or file_name
                try:
                    chunk_index = int(metadata.get("chunk_index", len(chunks)))
                except (TypeError, ValueError):
                    chunk_index = len(chunks)
                chunks.append(
                    ParsedChunk(
                        index=chunk_index,
                        content=node.get_content(),
                        metadata=metadata,
                    )
                )
        chunks.sort(key=lambda chunk: chunk.index)
        if not chunks:
            return None
        return ParseResult(
            document_id=document_id,
            file_name=file_name,
            chunk_count=len(chunks),
            chunks=chunks,
            backend=self.name,
        )

    def health_check(self) -> BackendHealth:
        status = resource_manager.health_check()
        return BackendHealth(ok=bool(status.get("overall")), details=status, backend=self.name)
