import json
import os
import re
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
from src.ingestion.kb_paths import safe_child_path, validate_kb_name
from src.ingestion.parse_strategies import parse_by_source_group
from src.ingestion.parse_tasks import ParseTaskManager
from src.ingestion.source_groups import SourceGroupClassification, classify_source_group, safe_source_group
from src.rag_backends.base import RAGBackend
from src.rag_backends.schemas import BackendHealth, BackendResult, DocumentInfo, Evidence, IngestResult, ParsedChunk, ParseResult, RequestContext


_MAX_METADATA_VALUE_LENGTH = 2000
_TEMP_UPLOAD_PREFIX_RE = re.compile(r"^(?:[0-9a-f]{32}_)+", re.IGNORECASE)
_INSERT_NODES_BATCH_SIZE = 32


def _display_filename(path_or_name: str) -> str:
    name = os.path.basename(str(path_or_name).replace("\\", os.sep).replace("/", os.sep))
    return _TEMP_UPLOAD_PREFIX_RE.sub("", name)


def _normalize_document_key(path_or_name: str | None) -> str:
    if not path_or_name:
        return ""
    return _display_filename(path_or_name).casefold()


def _normalize_relative_path(path_or_name: str | None) -> str:
    if not path_or_name:
        return ""
    return str(path_or_name).replace("\\", "/").casefold()


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


def _delete_node_ids_from_index(index, node_ids: set[str]) -> int:
    """Delete nodes from both Chroma and the persisted docstore."""
    if not node_ids:
        return 0

    ordered_node_ids = sorted(node_ids)
    index.vector_store.delete_nodes(ordered_node_ids)

    for node_id in ordered_node_ids:
        try:
            index.index_struct.delete(node_id)
        except KeyError:
            pass
        index.docstore.delete_document(node_id, raise_error=False)

    index.storage_context.index_store.add_index_struct(index.index_struct)
    return len(ordered_node_ids)


def _rollback_inserted_nodes(index, node_ids: set[str], kb_name: str):
    if not node_ids:
        return
    try:
        deleted_count = _delete_node_ids_from_index(index, node_ids)
        index.storage_context.persist(persist_dir=config.settings.get_kb_storage_path(kb_name))
        invalidate_bm25_cache(kb_name)
        invalidate_index_cache(kb_name)
        warn(f"Rolled back {deleted_count} partially inserted nodes for {kb_name}.")
    except Exception as rollback_error:
        error(f"Failed to roll back partial ingest for {kb_name}: {rollback_error}")


class LocalRAGBackend(RAGBackend):
    """Current in-process RAG implementation backed by Docling, LlamaIndex, Chroma and BM25."""

    name = "local"

    def __init__(self):
        if not resource_manager.initialize():
            raise RuntimeError("璧勬簮绠＄悊鍣ㄥ垵濮嬪寲澶辫触")
        self.parse_tasks = ParseTaskManager(self._run_parse_task)

    def get_index(self, kb_name: str):
        return get_or_build_index(kb_name, resource_manager.chroma_client, use_cache=True)

    def _check_kb_access(self, kb_name: str, ctx: RequestContext | None, required: str = "read"):
        validate_kb_name(kb_name)
        if ctx is not None and not ctx.has_kb_permission(kb_name, required):
            raise PermissionError(f"User {ctx.user_id} lacks {required} permission for knowledge base {kb_name}")

    def ingest(
        self,
        kb_name: str,
        files: list[str],
        ctx: RequestContext | None = None,
        source_group: str | None = None,
    ) -> IngestResult:
        self._check_kb_access(kb_name, ctx, "write")
        if not files:
            return IngestResult(success_count=0, total_count=0, messages=["No files selected"], backend=self.name)
        if not kb_name:
            return IngestResult(success_count=0, total_count=len(files), messages=["No target knowledge base selected"], backend=self.name)

        messages = []
        success_count = 0
        for file_path in files:
            try:
                result = self.add_document(kb_name, file_path, source_group=source_group)
                messages.append(result.message)
                if result.ok:
                    success_count += 1
            except Exception as exc:
                error(f"Upload failed {file_path}: {exc}")
                messages.append(f"Failed {os.path.basename(file_path)}: {exc}")

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
                    return BackendResult(ok=False, message="Temporary file does not exist", backend=self.name)

                staged_filename = os.path.basename(temp_file_path)
                original_filename = _display_filename(staged_filename)
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

                report(10, "Saving source file")
                shutil.copy2(temp_file_path, target_path)

                check()
                log(f"Start parsing file: {filename}")
                report(25, "Loading knowledge base index")
                index = self.get_index(kb_name)
                check()
                report(40, "Parsing document content")
                nodes = parse_by_source_group(
                    target_path,
                    filename,
                    kb_name,
                    source_group=source_group,
                    progress_callback=report,
                )

                if not nodes:
                    raise ValueError("Document parsing produced no valid nodes")

                check()
                log(f"Parsed successfully, preparing to write {len(nodes)} nodes to {kb_name}")
                report(70, f"Writing index nodes ({len(nodes)} chunks)")
                relative_path = os.path.join(source_group, filename)
                for chunk_index, node in enumerate(nodes):
                    node.metadata["file_name"] = filename
                    node.metadata["original_file_name"] = original_filename
                    node.metadata["staged_file_name"] = staged_filename
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

                inserted_node_ids = {node.node_id for node in nodes}
                index.docstore.add_documents(nodes)
                total_nodes = len(nodes)
                for start in range(0, total_nodes, _INSERT_NODES_BATCH_SIZE):
                    end = min(start + _INSERT_NODES_BATCH_SIZE, total_nodes)
                    report(
                        70 + int((start / total_nodes) * 18),
                        f"Embedding and indexing nodes ({start + 1}-{end}/{total_nodes} chunks)",
                    )
                    index.insert_nodes(nodes[start:end])
                    report(
                        70 + int((end / total_nodes) * 18),
                        f"Indexed nodes ({end}/{total_nodes} chunks)",
                    )

                report(90, "Persisting index")
                persist_dir = config.settings.get_kb_storage_path(kb_name)
                index.storage_context.persist(persist_dir=persist_dir)

                invalidate_bm25_cache(kb_name)
                invalidate_index_cache(kb_name)

                log(f"Index persisted successfully: {filename} (KB: {kb_name})")
                report(100, "Parsing completed")
                return BackendResult(ok=True, message=f"Index succeeded: {filename}", backend=self.name)
            except Exception as exc:
                error(f"Document upload processing failed: {exc}")
                if "index" in locals() and "inserted_node_ids" in locals():
                    _rollback_inserted_nodes(index, inserted_node_ids, kb_name)
                if "target_path" in locals() and os.path.exists(target_path):
                    try:
                        os.remove(target_path)
                    except Exception as cleanup_error:
                        error(f"Failed to clean staged file: {target_path}, error: {cleanup_error}")
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
        self._check_kb_access(kb_name, ctx, "write")
        created_by = ctx.user_id if ctx else ""
        task_source_group = safe_source_group(source_group) if source_group else ""
        return self.parse_tasks.submit(kb_name, files, task_source_group, created_by=created_by)

    def list_parse_tasks(self, kb_name: str | None = None, ctx: RequestContext | None = None):
        if kb_name:
            self._check_kb_access(kb_name, ctx, "read")
        return self.parse_tasks.list_tasks(kb_name)

    def pause_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> BackendResult:
        task = self.parse_tasks.get_task(task_id)
        if not task:
            return BackendResult(ok=False, message="Parse task not found", backend=self.name)
        self._check_kb_access(task.kb_name, ctx, "write")
        ok = self.parse_tasks.pause(task_id)
        return BackendResult(ok=ok, message="Parse task paused" if ok else "Parse task cannot be paused in current state", backend=self.name)

    def resume_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> BackendResult:
        task = self.parse_tasks.get_task(task_id)
        if not task:
            return BackendResult(ok=False, message="Parse task not found", backend=self.name)
        self._check_kb_access(task.kb_name, ctx, "write")
        ok = self.parse_tasks.resume(task_id)
        return BackendResult(ok=ok, message="Parse task resumed" if ok else "Parse task cannot be resumed in current state", backend=self.name)

    def delete_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> BackendResult:
        task = self.parse_tasks.get_task(task_id)
        if not task:
            return BackendResult(ok=False, message="Parse task not found", backend=self.name)
        self._check_kb_access(task.kb_name, ctx, "write")
        is_running = task.status == "running"
        ok = self.parse_tasks.delete(task_id)
        message = "Parse task cancellation requested" if is_running else "Parse task deleted"
        return BackendResult(ok=ok, message=message if ok else "Failed to delete parse task", backend=self.name)

    def clear_finished_parse_tasks(self, kb_name: str | None = None, ctx: RequestContext | None = None):
        if kb_name:
            self._check_kb_access(kb_name, ctx, "write")
        self.parse_tasks.clear_finished(kb_name)

    def retrieve(self, kb_name: str, query: str, top_k: int | None = None, ctx: RequestContext | None = None) -> list[Evidence]:
        self._check_kb_access(kb_name, ctx, "read")
        index = self.get_index(kb_name)
        nodes = routed_retrieve(query, index, kb_name, top_k or config.settings.FINAL_TOP_K)
        evidences = []
        for node in nodes:
            metadata = dict(node.node.metadata or {})
            evidences.append(
                Evidence(
                    id=node.node.node_id,
                    content=node.node.get_content(),
                    source_name=metadata.get("file_name", "unknown"),
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
        self._check_kb_access(kb_name, ctx, "read")
        index = self.get_index(kb_name)
        chat_engine = CustomRAGChat(kb_name, index)
        yield from chat_engine.chat(query, history)

    def delete_document(self, kb_name: str, document_id: str, ctx: RequestContext | None = None) -> BackendResult:
        self._check_kb_access(kb_name, ctx, "write")
        lock = resource_manager.get_kb_lock(kb_name)
        with lock:
            if not document_id:
                return BackendResult(ok=False, message="Document name cannot be empty", backend=self.name)

            try:
                index = self.get_index(kb_name)
                all_nodes = list(index.docstore.docs.values())
                requested_key = _normalize_document_key(document_id)
                requested_relative_path = _normalize_relative_path(document_id)
                target_ref_doc_ids = set()
                target_node_ids = set()
                for node in all_nodes:
                    node_file_name = node.metadata.get("file_name", "")
                    node_original_file_name = node.metadata.get("original_file_name", "")
                    node_staged_file_name = node.metadata.get("staged_file_name", "")
                    node_relative_path = node.metadata.get("relative_path", "")
                    node_keys = {
                        _normalize_document_key(node_file_name),
                        _normalize_document_key(node_original_file_name),
                        _normalize_document_key(node_staged_file_name),
                        _normalize_document_key(node_relative_path),
                    }
                    matched = requested_relative_path == _normalize_relative_path(node_relative_path) or requested_key in node_keys
                    if matched:
                        target_node_ids.add(node.node_id)
                        if node.ref_doc_id:
                            target_ref_doc_ids.add(node.ref_doc_id)

                try:
                    file_path = safe_child_path(get_kb_path(kb_name), document_id)
                except ValueError as exc:
                    return BackendResult(ok=False, message=f"Invalid document path: {exc}", backend=self.name)
                if not target_ref_doc_ids and not target_node_ids:
                    warn(f"No index records found for document {document_id}. It may already be deleted or was never indexed.")
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        return BackendResult(ok=True, message=f"No index record found; deleted source file: {document_id}", backend=self.name)
                    return BackendResult(ok=False, message="Document was not found in the index or source path.", backend=self.name)

                for ref_doc_id in target_ref_doc_ids:
                    ref_doc_info = index.docstore.get_ref_doc_info(ref_doc_id)
                    if ref_doc_info is not None:
                        target_node_ids.update(ref_doc_info.node_ids)

                deleted_node_count = _delete_node_ids_from_index(index, target_node_ids)
                log(f"Deleted {deleted_node_count} nodes from index")

                persist_dir = config.settings.get_kb_storage_path(kb_name)
                index.storage_context.persist(persist_dir=persist_dir)

                if os.path.exists(file_path):
                    os.remove(file_path)

                invalidate_index_cache(kb_name)
                invalidate_bm25_cache(kb_name)

                log(f"Document removed from source files and index: {document_id}")
                return BackendResult(ok=True, message=f"Deleted document: {document_id}", backend=self.name)
            except Exception as exc:
                error(f"Delete document failed: {exc}")
                traceback.print_exc()
                return BackendResult(ok=False, message=f"Delete failed: {exc}", backend=self.name)

    def list_documents(self, kb_name: str, ctx: RequestContext | None = None) -> list[DocumentInfo]:
        self._check_kb_access(kb_name, ctx, "read")
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
        self._check_kb_access(kb_name, ctx, "read")
        if not document_id:
            return None
        index = self.get_index(kb_name)
        requested_key = _normalize_document_key(document_id)
        requested_relative_path = _normalize_relative_path(document_id)
        chunks = []
        file_name = document_id
        for node in index.docstore.docs.values():
            metadata = dict(node.metadata or {})
            node_file_name = metadata.get("file_name", "")
            node_original_file_name = metadata.get("original_file_name", "")
            node_staged_file_name = metadata.get("staged_file_name", "")
            node_relative_path = metadata.get("relative_path", "")
            node_keys = {
                _normalize_document_key(node_file_name),
                _normalize_document_key(node_original_file_name),
                _normalize_document_key(node_staged_file_name),
                _normalize_document_key(node_relative_path),
            }
            if requested_relative_path == _normalize_relative_path(node_relative_path) or requested_key in node_keys:
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
