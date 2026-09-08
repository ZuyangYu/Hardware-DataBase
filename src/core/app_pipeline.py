# src/core/app_pipeline.py
import os
import hashlib
import re
import shutil
import tempfile
import traceback
import uuid
from typing import Any, Callable, Generator, List, Tuple

import src.settings
from src.agents.runner import MultiSourceAgentRunner, forget_thread
from src.agents.schemas import Evidence
from src.agents.tools.pipeline_catalog import scan_kb_sources as scan_pipeline_kb_sources
from src.agents.tools.circuit_tools import CircuitQueryTool
from src.agents.tools.spreadsheet_tools import SpreadsheetSemanticTool
from src.core.auth import AuthService
from src.core.cancellation import QueryCancelled
from src.core.conversation import ConversationService, GENERAL_CHAT_KB_NAME
from src.core.logger import error, log, warn
from src.ingestion.kb_paths import InvalidKnowledgeBaseName, validate_kb_name
from src.pipelines.document_rag.factory import create_rag_backend
from src.pipelines.document_rag.schemas import EvidenceEnvelope, IngestResult, RequestContext
from src.projects.service import ProjectService
from src.projects.retrieval import ProjectEvidenceRetrievalService, SourceUnavailableError
from src.document_authoring.service import DocumentGenerationService
from src.document_authoring.evidence import (
    AttachmentEvidenceProvider,
    CompositeDocumentEvidenceProvider,
    KnowledgeBaseEvidenceProvider,
)
from src.document_authoring.job_store import DocumentAuthoringJobStore
from src.document_authoring.circuit_capabilities import enrich_circuit_capabilities
from src.document_authoring.icd_scope_decision import (
    build_icd_scope_decision,
    build_unknown_connector_scope_decision,
    effective_frozen_pin_mappings,
    supported_connector_refdes,
)
from src.document_authoring.icd_generation import connector_refdes_from_front_view_template
from src.document_authoring.icd_profile import classify_icd_template
from src.document_authoring.template_progress import TemplateProgressCallback
from src.document_authoring.generation_sessions import GenerationBrief
from src.document_authoring.models import content_hash
from src.document_authoring.models import DocumentFieldSchema, DocumentSchema
from src.document_authoring.planning.models import OutputSpec
from src.document_authoring.planning.intake import OutputSpecIntakeService
from src.document_authoring.requirement_clarifier import RequirementClarifier
from src.document_authoring.requirement_resolver import RequirementResolver
from src.document_authoring.reviews import resolve_review_decision_status
from src.document_authoring.retriever_registry import (
    CrossUnitEvidenceCache,
    RetrieverRegistry,
    apply_role_boost,
    dedup_by_content,
)
from src.services.document_manager import DocumentManager
from src.services.kb_scope import kb_scope_from_context
from src.attachments.models import (
    resolve_source_scope,
    scope_allows_kb,
)
from src.attachments.retrieval import AttachmentRetrievalService


_ICD_PIN_TERMS = (
    "pin", "pinout", "pin definition", "connector", "接插件", "连接器",
    "引脚", "管脚", "针脚",
)
_PIN_FIELD_TERMS = ("pin", "pinout", "pin definition", "引脚", "管脚", "针脚")
_EXPLICIT_REFDES = re.compile(
    r"(?:connector|refdes|reference\s+designator|接插件|连接器|位号)\s*[:#]?\s*"
    r"([a-z]{1,12}\d+[a-z0-9_.-]*)",
    re.IGNORECASE,
)
_COMMON_CONNECTOR_REFDES = re.compile(r"\b(?:x|j|p|cn|con)\d+[a-z0-9_.-]*\b", re.IGNORECASE)


def _is_file_like(obj) -> bool:
    """True for uploaded file objects exposing a binary buffer."""
    return hasattr(obj, "getbuffer") or (hasattr(obj, "read") and not isinstance(obj, (str, bytes)))


def _materialize_to_temp(file_obj) -> str:
    """Persist a file-like upload to a temp path and return the path."""
    raw_name = getattr(file_obj, "name", None) or "upload"
    original_name = os.path.basename(str(raw_name)) or "upload"
    tmp_dir = tempfile.mkdtemp(prefix="hrag_upload_")
    temp_path = os.path.join(tmp_dir, original_name)
    try:
        with open(temp_path, "wb") as wb:
            buffer = file_obj.getbuffer() if hasattr(file_obj, "getbuffer") else file_obj.read()
            try:
                wb.write(buffer)
            except TypeError as exc:
                raise TypeError(f"Unsupported file-like payload: {type(buffer).__name__}") from exc
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return temp_path


def _connector_refdes_from_schema(schema: Any) -> list[str]:
    """Extract explicitly declared connector designators from ICD pin fields."""
    candidates: list[str] = []
    for field in getattr(schema, "fields", []) or []:
        values = [
            getattr(field, "label", ""),
            getattr(field, "description", ""),
            *(getattr(field, "query_terms", []) or []),
            *(getattr(field, "subject_aliases", []) or []),
            *_string_values(getattr(field, "value_schema", {}) or {}),
        ]
        field_text = " ".join(str(value) for value in values if str(value).strip())
        normalized = field_text.casefold()
        if not any(term in normalized for term in _ICD_PIN_TERMS):
            continue
        candidates.extend(match.group(1) for match in _EXPLICIT_REFDES.finditer(field_text))
        candidates.extend(match.group(0) for match in _COMMON_CONNECTOR_REFDES.finditer(field_text))
    return list(dict.fromkeys(value.upper() for value in candidates if value.strip()))


def _schema_has_icd_pin_field(schema: Any) -> bool:
    for field in getattr(schema, "fields", []) or []:
        text = " ".join(
            str(value)
            for value in (
                getattr(field, "label", ""),
                getattr(field, "description", ""),
                *(getattr(field, "query_terms", []) or []),
            )
            if str(value).strip()
        ).casefold()
        if any(term in text for term in _PIN_FIELD_TERMS):
            return True
    return False


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _string_values(child)]
    if isinstance(value, (list, tuple, set)):
        return [item for child in value for item in _string_values(child)]
    return []


def _template_free_allowlisted(value: Any, configured: Any) -> bool:
    """Require an exact, non-wildcard template-free canary allowlist match."""
    if isinstance(configured, str):
        values = configured.split(",")
    elif isinstance(configured, (list, tuple, set, frozenset)):
        values = configured
    else:
        values = ()
    allowed = {
        str(item).strip().casefold()
        for item in values
        if str(item).strip()
    }
    normalized = str(value or "").strip().casefold()
    return bool(allowed) and normalized in allowed


class AppPipeline:
    """Application orchestration for KB governance, document assets and Q&A."""

    def __init__(self):
        try:
            self.backend = create_rag_backend()
            self.documents = DocumentManager(self.backend)
            self.agent = MultiSourceAgentRunner(
                rag_backend=self.backend,
                document_store=getattr(self.backend, "store", None),
                spreadsheet_service=getattr(self.backend, "spreadsheet_indexes", None),
                circuit_service=getattr(self.backend, "circuit_indexes", None),
                conversation_service=getattr(self.backend, "conversation_indexes", None),
            )
            # Authoring is deliberately a sibling of the query agent.  It
            # shares source/evidence services but never stores WorkOrder state
            # in a chat session or AgentState.
            self.projects = ProjectService()
            self.project_retrieval = ProjectEvidenceRetrievalService(self.projects)
            self.document_generation = DocumentGenerationService(self.projects)
            # Chat-created authoring work orders are durable jobs.  Keep one
            # repository instance on the application pipeline so the HTTP
            # tool adapter and the separated worker share the same database
            # and idempotency boundary.
            self.document_job_store = DocumentAuthoringJobStore()
            # The chat runner is constructed before the document service, so
            # bind the completed pipeline after initialization.  The runner
            # still exposes the tools only when the explicit setting is on.
            self.agent.document_authoring_pipeline = self
            self.agent.document_job_store = self.document_job_store
            self.requirement_clarifier = RequirementClarifier()
            self.requirement_resolver = RequirementResolver()
            self.output_spec_intake = OutputSpecIntakeService()
            # Spreadsheet structured index (xlsx TableIndexStore). Shared with
            # the query agent; the KB authoring retriever also needs it so
            # frozen .xlsx sources can produce tabular evidence.
            self.spreadsheet_service = getattr(self.backend, "spreadsheet_indexes", None)
            # Circuit structured index (EDF/EDIF CircuitStore). Shared with
            # the query agent; the authoring retrievers also need it so
            # frozen .edf/.edif sources can produce pin/connectivity evidence.
            self.circuit_service = getattr(self.backend, "circuit_indexes", None)
        except Exception as exc:
            error(f"AppPipeline 初始化失败: {exc}")
            raise

    def _audit(
        self,
        action: str,
        ctx: RequestContext | None,
        target_type: str = "",
        target_id: str = "",
        kb_name: str = "",
        success: bool = True,
        error_message: str = "",
        metadata: dict | None = None,
    ) -> None:
        """Record an audit event, fail-soft. Actor is resolved from ctx.user_id
        (same pattern as RAGFlowBackend._audit). Centralizing write-op audits
        here covers Streamlit, the API layer, and any future client uniformly."""
        try:
            from src.core.app_logs import AppLogService

            actor = AuthService().get_user_by_username(ctx.user_id) if ctx and ctx.user_id else None
            AppLogService().record_audit(
                action=action,
                actor=actor,
                target_type=target_type,
                target_id=target_id,
                kb_name=kb_name,
                success=success,
                error_message=error_message,
                metadata=metadata,
            )
        except Exception as audit_error:
            warn(f"AppPipeline audit failed: {audit_error}")

    def list_knowledge_bases(self, ctx: RequestContext | None = None) -> List[str]:
        auth_service = AuthService()
        kbs = self.backend.list_knowledge_bases()
        if ctx is None:
            return kbs
        user = auth_service.get_user_by_username(ctx.user_id)
        if user is None:
            return []
        return auth_service.list_accessible_kbs(user, kbs)

    def list_all_knowledge_bases_for_admin(self, ctx: RequestContext | None = None) -> List[str]:
        if ctx is None or not ctx.is_system_admin():
            return []
        return self.backend.list_knowledge_bases()

    def scan_kb_sources(
        self,
        kb_name: str,
        ctx: RequestContext | None = None,
        query: str = "",
    ) -> dict[str, Any]:
        """List scoped KB sources through the application layer.

        The agent-facing catalog is a per-request tool closure.  Evaluation
        preflight must not reach into that closure, so expose the underlying
        read-only scanner as a stable application boundary instead.
        """
        document_store = getattr(self.backend, "store", None)
        if document_store is None:
            raise RuntimeError("pipeline document store is not configured")
        if ctx is not None and not ctx.has_kb_permission(kb_name, "read"):
            raise PermissionError(
                f"User {ctx.user_id} lacks read permission for knowledge base {kb_name}"
            )
        return scan_pipeline_kb_sources(
            kb_name,
            ctx,
            query,
            document_store=document_store,
            spreadsheet_service=getattr(self, "spreadsheet_service", None),
            circuit_service=getattr(self, "circuit_service", None),
            rag_backend=self.backend,
        )

    def query(
        self,
        msg: str,
        kb_name: str,
        history: List[Tuple[str, str]],
        ctx: RequestContext | None = None,
        agent_thread_id: str = "",
        event_callback: Callable[[dict], None] | None = None,
        query_mode: str = "deep",
        should_cancel: Callable[[], bool] | None = None,
        persist_thread: bool = False,
        document_context: Any | None = None,
        document_flow: bool | None = None,
        attachments: list[Any] | None = None,
        source_scope: str = "auto",
        propagate_errors: bool = False,
        attachment_user_id: int | None = None,
    ) -> Generator[str, None, None]:
        self.clear_last_token_usage_summary()
        if not msg.strip():
            yield "请输入有效问题"
            return
        if kb_name == GENERAL_CHAT_KB_NAME:
            kb_name = ""
        try:
            yield from self.agent.stream(
                query=msg,
                kb_name=kb_name,
                history=history,
                ctx=ctx,
                thread_id=agent_thread_id,
                event_callback=event_callback,
                query_mode=query_mode,
                should_cancel=should_cancel,
                persist_thread=persist_thread,
                document_context=document_context,
                document_flow=document_flow,
                attachments=attachments,
                source_scope=source_scope,
                attachment_user_id=attachment_user_id,
            )
        except QueryCancelled:
            return
        except Exception as exc:
            error(f"查询出错: {exc}")
            traceback.print_exc()
            from src.core.error_friendly import friendly_error_message
            if propagate_errors:
                raise
            yield friendly_error_message(exc)

    def forget_agent_thread(self, thread_id: str) -> None:
        """Drop the persisted agent thread state (session cleared/deleted)."""
        forget_thread(thread_id)

    def get_last_agent_footer(self) -> str:
        return self.agent.get_last_footer()

    def get_last_retrieval_summary(self) -> dict:
        return self.agent.get_last_retrieval_summary()

    def get_last_token_usage_summary(self):
        return self.agent.get_last_token_usage_summary()

    def clear_last_token_usage_summary(self) -> None:
        clear = getattr(self.agent, "clear_last_token_usage_summary", None)
        if callable(clear):
            clear()

    def upload_files(
        self,
        files,
        target_kb: str,
        ctx: RequestContext | None = None,
        source_group: str | None = None,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> IngestResult:
        temp_paths: list[str] = []
        file_paths: list[str] = []
        try:
            for file_obj in files:
                if isinstance(file_obj, str):
                    file_paths.append(file_obj)
                elif _is_file_like(file_obj):
                    temp_path = _materialize_to_temp(file_obj)
                    temp_paths.append(temp_path)
                    file_paths.append(temp_path)
                else:
                    raise TypeError(f"Unsupported file argument: {type(file_obj).__name__}")
            result = self.documents.upload_files(
                file_paths,
                target_kb,
                ctx=ctx,
                source_group=source_group,
                progress_callback=progress_callback,
            )
            self._audit(
                "upload_document",
                ctx,
                target_type="document",
                target_id=", ".join(os.path.basename(p) for p in file_paths),
                kb_name=target_kb,
                success=result.success_count > 0,
                error_message="" if result.success_count > 0 else "; ".join(result.messages),
                metadata={
                    "file_count": len(file_paths),
                    "source_group": source_group,
                    "success_count": result.success_count,
                    "failed_count": result.failed_count,
                    "skipped_count": result.skipped_count,
                },
            )
            return result
        except Exception as exc:
            self._audit(
                "upload_document",
                ctx,
                target_type="document",
                target_id=", ".join(os.path.basename(p) for p in file_paths),
                kb_name=target_kb,
                success=False,
                error_message=str(exc),
                metadata={"file_count": len(file_paths), "source_group": source_group},
            )
            raise
        finally:
            for temp_path in temp_paths:
                try:
                    shutil.rmtree(os.path.dirname(temp_path), ignore_errors=True)
                except OSError as cleanup_error:
                    warn(f"Failed to clean temp upload {temp_path}: {cleanup_error}")

    def upload_files_message(
        self,
        files,
        target_kb: str,
        ctx: RequestContext | None = None,
        source_group: str | None = None,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> str:
        return self.upload_files(
            files,
            target_kb,
            ctx=ctx,
            source_group=source_group,
            progress_callback=progress_callback,
        ).to_message()

    @staticmethod
    def governance_stats(ctx: RequestContext | None = None) -> dict:
        try:
            from src.pipelines.document_store import PipelineDocumentStore

            if ctx is not None and ctx.is_system_admin():
                return PipelineDocumentStore().document_stats_by_kb_identity()
            department_id = ""
            if ctx is not None:
                department_id = str(ctx.metadata.get("resource_department_id") or ctx.metadata.get("department_id") or "")
            return PipelineDocumentStore().document_stats_by_kb(department_id=department_id or None)
        except Exception as exc:
            warn(f"RAGFlow 台账读取失败: {exc}")
            return {}

    @staticmethod
    def check_ragflow_connection(
        base_url: str,
        api_key: str,
        dataset_names: List[str],
        timeout: int = 120,
    ) -> Tuple[bool, str, List[str]]:
        if not base_url or not api_key:
            return False, "请先填写 RAGFlow Base URL 和 API Key", []
        try:
            from src.pipelines.document_rag.ragflow_backend import RAGFlowClient

            client = RAGFlowClient(
                base_url=base_url.rstrip("/"),
                api_key=api_key,
                timeout=timeout,
            )
            datasets = client.list_datasets()
            existing_names = {item.get("name") for item in datasets if isinstance(item, dict)}
            missing = [name for name in dataset_names if name and name not in existing_names]
            if missing:
                return True, f"RAGFlow 可连接，但以下 Dataset 暂未找到: {', '.join(missing)}", missing
            return True, f"RAGFlow 连接正常，已找到 {len(dataset_names)} 个配置 Dataset", []
        except Exception as exc:
            return False, f"RAGFlow 连接检查失败: {exc}", []

    @staticmethod
    def apply_settings(new_settings: dict) -> None:
        # Validate BEFORE persisting and hold the .env write lock across the
        # whole validate -> save -> reload cycle: reload_settings (and module
        # import) parse numeric/enum settings eagerly, so a bad value written
        # to .env first would brick the server on the next boot. Concurrent
        # PUT /config requests serialise here instead of losing updates.
        with src.settings._ENV_WRITE_LOCK:
            src.settings.validate_settings_values(new_settings)
            src.settings.save_settings_to_env(new_settings)
            src.settings.reload_settings()
            # create_chat_model 是 lru_cache 的:不清缓存的话,管理页改完
            # AGENT_LLM_* 后旧模型实例会一直用到进程重启。
            from src.core.model_factory import create_chat_model

            create_chat_model.cache_clear()

    def delete_document(self, filename: str, kb_name: str, ctx: RequestContext | None = None) -> str:
        try:
            result = self.documents.delete_document(filename, kb_name, ctx=ctx)
            # DocumentManager now returns the full BackendResult; branch on .ok
            # instead of sniffing error prefixes out of the message string.
            delete_ok = bool(getattr(result, "ok", False))
            message = getattr(result, "message", str(result))
            self._audit(
                "delete_document",
                ctx,
                target_type="document",
                target_id=filename,
                kb_name=kb_name,
                success=delete_ok,
                error_message="" if delete_ok else message,
            )
            return message
        except Exception as exc:
            self._audit(
                "delete_document",
                ctx,
                target_type="document",
                target_id=filename,
                kb_name=kb_name,
                success=False,
                error_message=str(exc),
            )
            raise

    def create_kb(self, name: str, ctx: RequestContext | None = None) -> Tuple[bool, str]:
        try:
            name = validate_kb_name(name.strip().replace(" ", "_"))
            if not name:
                self._audit("create_kb", ctx, target_type="knowledge_base", target_id=name, kb_name=name, success=False, error_message="名称不能为空")
                return False, "名称不能为空"
            if ctx is not None and "anonymous" in ctx.roles:
                self._audit("create_kb", ctx, target_type="knowledge_base", target_id=name, kb_name=name, success=False, error_message="权限不足：请先登录再创建知识库。")
                return False, "权限不足：请先登录再创建知识库。"
            if ctx is not None and ctx.is_system_admin():
                self._audit("create_kb", ctx, target_type="knowledge_base", target_id=name, kb_name=name, success=False, error_message="系统管理员不能创建内容知识库")
                return False, "系统管理员不能创建内容知识库，请由部门管理员创建。"
            auth_service = AuthService()
            scope = kb_scope_from_context(name, ctx).require_department("create")
            if auth_service.knowledge_base_exists(scope.kb_name, department_id=scope.department_id):
                self._audit("create_kb", ctx, target_type="knowledge_base", target_id=name, kb_name=name, success=False, error_message="知识库已存在")
                return False, "知识库已存在"

            self.backend.create_kb_storage(scope.kb_name, ctx=ctx)

            if ctx and ctx.user_id:
                owner = auth_service.get_user_by_username(ctx.user_id)
                auth_service.register_knowledge_base(scope.kb_name, owner=owner)
            log(f"知识库 '{name}' 创建成功（后端 {self.backend.name}）")
            self._audit("create_kb", ctx, target_type="knowledge_base", target_id=name, kb_name=name, success=True)
            return True, f"知识库 '{name}' 创建成功"
        except Exception as exc:
            error(f"创建知识库失败: {exc}")
            self._audit("create_kb", ctx, target_type="knowledge_base", target_id=name, kb_name=name, success=False, error_message=str(exc))
            return False, str(exc)

    def delete_knowledge_base(self, kb_name: str, ctx: RequestContext | None = None) -> Tuple[bool, str]:
        try:
            kb_name = validate_kb_name(kb_name)
        except InvalidKnowledgeBaseName as exc:
            self._audit("delete_kb", ctx, target_type="knowledge_base", target_id=kb_name, kb_name=kb_name, success=False, error_message=str(exc))
            return False, str(exc)

        if ctx is None or not ctx.has_kb_permission(kb_name, "admin"):
            self._audit("delete_kb", ctx, target_type="knowledge_base", target_id=kb_name, kb_name=kb_name, success=False, error_message="权限不足：删除知识库需要 admin 权限。")
            return False, "权限不足：删除知识库需要 admin 权限。"

        log(f"准备彻底删除知识库: {kb_name}")
        try:
            result = self.documents.delete_knowledge_base_documents(kb_name, ctx=ctx)
            if not result.ok:
                self._audit("delete_kb", ctx, target_type="knowledge_base", target_id=kb_name, kb_name=kb_name, success=False, error_message=result.message)
                return False, result.message
            scope = kb_scope_from_context(kb_name, ctx)
            AuthService().delete_knowledge_base_record(scope.kb_name, department_id=scope.department_id, kb_id=scope.kb_id)
            # Surface partial cleanup (some archive assets failed to delete) in
            # audit metadata so the governance log shows it wasn't fully clean.
            partial = bool(getattr(result, "partial", False))
            cleanup_errors = list(getattr(result, "errors", []) or [])
            self._audit(
                "delete_kb",
                ctx,
                target_type="knowledge_base",
                target_id=kb_name,
                kb_name=kb_name,
                success=True,
                metadata={"partial": partial, "cleanup_errors": cleanup_errors},
            )
            return True, result.message
        except Exception as exc:
            error(f"删除知识库失败: {exc}")
            self._audit("delete_kb", ctx, target_type="knowledge_base", target_id=kb_name, kb_name=kb_name, success=False, error_message=str(exc))
            return False, f"删除失败: {str(exc)}"

    def list_files(self, kb_name: str, ctx: RequestContext | None = None) -> List[str]:
        return self.documents.list_files(kb_name, ctx=ctx)

    def list_file_infos(self, kb_name: str, ctx: RequestContext | None = None):
        return self.documents.list_file_infos(kb_name, ctx=ctx)

    # ---- external conversations (外部对话) ------------------------------
    def list_external_conversations(self, kb_name: str, ctx: RequestContext | None = None):
        scope = kb_scope_from_context(kb_name, ctx).require_department("list external conversations in")
        return self.backend.conversation_indexes.list_conversations(scope.department_id, kb_name)

    def get_external_conversation(self, kb_name: str, conversation_id: str, ctx: RequestContext | None = None):
        scope = kb_scope_from_context(kb_name, ctx).require_department("read an external conversation in")
        meta = self.backend.conversation_indexes.get_conversation(scope.department_id, kb_name, conversation_id)
        if meta is None:
            return None
        preview = ""
        conversation = self.backend.conversations.load(scope.department_id, kb_name, conversation_id)
        if conversation is not None:
            turns = [
                {
                    "role": t.role,
                    "content": t.content,
                    "ts": t.ts,
                    "start_offset": t.start_offset,
                    "end_offset": t.end_offset,
                }
                for t in conversation.turns
            ]
            blocks = [{"index": i, "content": block} for i, block in enumerate(conversation.blocks)]
        else:
            turns, blocks = [], []
            try:
                raw_path = os.path.join(
                    self.backend.conversations.conversation_dir(scope.department_id, kb_name, conversation_id),
                    "original.md",
                )
                with open(raw_path, "r", encoding="utf-8", errors="replace") as f:
                    preview = f.read(4000)
            except OSError:
                preview = ""
        return {**meta, "turns": turns, "blocks": blocks, "preview": preview}

    def delete_external_conversation(self, kb_name: str, conversation_id: str, ctx: RequestContext | None = None) -> bool:
        scope = kb_scope_from_context(kb_name, ctx).require_department("delete an external conversation in")
        removed_index = self.backend.conversation_indexes.delete_conversation(scope.department_id, kb_name, conversation_id)
        removed_store = self.backend.conversations.delete_conversation(scope.department_id, kb_name, conversation_id)
        return bool(removed_index or removed_store)

    def regenerate_external_conversation_summary(self, kb_name: str, conversation_id: str, ctx: RequestContext | None = None):
        """(Re)generate AI extraction for one conversation; returns updated detail or None."""
        from src.external_conversations import llm_structure
        from datetime import date

        scope = kb_scope_from_context(kb_name, ctx).require_department("summarize an external conversation in")
        conversation = self.backend.conversations.load(scope.department_id, kb_name, conversation_id)
        if conversation is None:
            return None
        body = "\n".join(t.content for t in conversation.turns) or "\n".join(conversation.blocks)
        result = llm_structure.summarize_content(body)
        if result is None:
            return None
        conversation.summary = result["summary"]
        conversation.key_points = result["key_points"]
        conversation.summary_generated_at = date.today().isoformat()
        self.backend.conversations.save(
            conversation,
            raw_bytes=None,
            raw_ext=os.path.splitext(conversation.source_file)[1] or ".md",
        )
        self.backend.conversation_indexes.update_summary(
            scope.department_id,
            kb_name,
            conversation_id,
            conversation.summary,
            conversation.key_points,
            conversation.summary_generated_at,
        )
        return self.get_external_conversation(kb_name, conversation_id, ctx=ctx)

    def list_parse_tasks(self, kb_name: str | None = None, ctx: RequestContext | None = None):
        return self.documents.list_parse_tasks(kb_name, ctx=ctx)

    def pause_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> str:
        return self.documents.pause_parse_task(task_id, ctx=ctx)

    def resume_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> str:
        return self.documents.resume_parse_task(task_id, ctx=ctx)

    def delete_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> str:
        # Stopping a parse task deletes the remote RAGFlow document + local
        # archive; audit it so the governance log can trace who cancelled what.
        try:
            result = self.documents.delete_parse_task(task_id, ctx=ctx)
            self._audit(
                "delete_parse_task",
                ctx,
                target_type="parse_task",
                target_id=str(task_id),
                success=True,
            )
            return result
        except Exception as exc:
            self._audit(
                "delete_parse_task",
                ctx,
                target_type="parse_task",
                target_id=str(task_id),
                success=False,
                error_message=str(exc),
            )
            raise

    def clear_finished_parse_tasks(self, kb_name: str | None = None, ctx: RequestContext | None = None):
        try:
            self.documents.clear_finished_parse_tasks(kb_name, ctx=ctx)
            self._audit(
                "clear_parse_tasks",
                ctx,
                target_type="parse_task",
                target_id=kb_name or "",
                kb_name=kb_name or "",
                success=True,
            )
        except Exception as exc:
            self._audit(
                "clear_parse_tasks",
                ctx,
                target_type="parse_task",
                target_id=kb_name or "",
                kb_name=kb_name or "",
                success=False,
                error_message=str(exc),
            )
            raise

    def get_parse_result(self, kb_name: str, document_id: str, ctx: RequestContext | None = None):
        return self.documents.get_parse_result(kb_name, document_id, ctx=ctx)

    # Project/document-authoring domain entry points.  REST, UI and future MCP
    # adapters must use these service methods instead of reaching into SQLite,
    # pipeline archives or renderer file paths directly.

    def create_project(self, project, ctx: RequestContext):
        return self.projects.create_project(ctx, project)

    def get_project_context(self, project_id: str, ctx: RequestContext):
        return self.projects.get_project_context(ctx, project_id)

    def list_accessible_projects(self, ctx: RequestContext):
        return self.projects.list_accessible_projects(ctx)

    def get_project_source_catalog(self, project_id: str, ctx: RequestContext):
        return self.projects.list_source_catalog(ctx, project_id)

    def list_document_generation_options(self, ctx: RequestContext, *, project_id: str):
        """Return only approved, project-scoped choices for a new work order."""
        self.projects.access.require(ctx, project_id, "view_project")
        tenant_id = ctx.tenant_id or "default"
        baselines = [
            baseline
            for baseline in self.projects.store.list_baselines(project_id, tenant_id, approved_only=True)
            if baseline.status in {"approved", "released"}
        ]
        return {
            "baselines": baselines,
            "templates": self.document_generation.store.list_templates(approved_only=True),
            "schemas": self.document_generation.store.list_document_schemas(approved_only=True),
            "harness_policies": self.document_generation.store.list_harness_policies(approved_only=True),
        }

    def list_knowledge_base_document_generation_options(
        self,
        ctx: RequestContext,
    ) -> dict[str, list]:
        """Return approved authoring choices plus only the caller's readable KBs."""
        return {
            "knowledge_bases": self.list_knowledge_bases(ctx),
            "templates": self.document_generation.store.list_templates(
                approved_only=True
            ),
            "schemas": self.document_generation.store.list_document_schemas(
                approved_only=True
            ),
        }

    def list_document_work_orders(self, ctx: RequestContext, *, project_id: str):
        """List durable work-order summaries visible to the current project user."""
        self.projects.access.require(ctx, project_id, "view_project")
        return self.document_generation.store.list_work_orders(ctx.tenant_id or "default", project_id)

    def list_knowledge_base_document_work_orders(
        self,
        ctx: RequestContext,
        knowledge_base_name: str,
    ):
        """List work orders only while the caller retains KB read permission."""
        return self.document_generation.list_knowledge_base_work_orders_for_context(
            ctx,
            knowledge_base_name,
        )

    def register_renderer_policy(self, policy):
        return self.document_generation.register_renderer_policy(policy)

    def register_document_schema(self, schema):
        return self.document_generation.register_document_schema(schema)

    def register_template(self, template, content: bytes, *, regions, bindings, legacy_claims=None):
        return self.document_generation.register_template(
            template,
            content,
            regions=regions,
            bindings=bindings,
            legacy_claims=legacy_claims,
        )

    def register_harness_policy(self, policy):
        return self.document_generation.register_harness_policy(policy)

    def approve_template_schema(self, template_version_id: str, actor_id: str):
        return self.document_generation.approve_template(template_version_id, actor_id)

    def analyze_document_template(
        self,
        ctx: RequestContext,
        *,
        filename: str,
        content: bytes,
        template_name: str,
        origin_source_type: str | None = None,
        origin_attachment_id: str | None = None,
        origin_session_id: int | None = None,
        origin_content_hash: str | None = None,
    ):
        return self.document_generation.analyze_uploaded_template(
            ctx,
            filename=filename,
            content=content,
            template_name=template_name,
            origin_source_type=origin_source_type,
            origin_attachment_id=origin_attachment_id,
            origin_session_id=origin_session_id,
            origin_content_hash=origin_content_hash,
        )

    def analyze_and_activate_document_template(
        self,
        ctx: RequestContext,
        *,
        filename: str,
        content: bytes,
        template_name: str,
        progress_callback: TemplateProgressCallback | None = None,
    ):
        return self.document_generation.analyze_and_activate_uploaded_template(
            ctx,
            filename=filename,
            content=content,
            template_name=template_name,
            progress_callback=progress_callback,
        )

    def get_document_template_sanitization_summary(self, ctx: RequestContext, template_version_id: str):
        return self.document_generation.get_template_sanitization_summary(ctx, template_version_id)

    def resolve_document_requirements(
        self,
        ctx: RequestContext,
        *,
        document_schema_id: str | None = None,
        document_schema_version: str | None = None,
        document_schema: Any | None = None,
        field_contracts: Any | None = None,
        evidence: Any | None = None,
        project_context: Any | None = None,
        available_sources: Any | None = None,
        generation_policy: Any | None = None,
    ):
        """Resolve field requirements before a writer is allowed to act."""
        schema = document_schema
        if schema is None and document_schema_id:
            getter = getattr(self.document_generation.store, "get_document_schema", None)
            if not callable(getter):
                raise KeyError("document schema store is unavailable")
            schema = getter(
                document_schema_id,
                document_schema_version or "1",
            )
        if schema is None and field_contracts is None:
            raise KeyError("document schema is required")
        resolver = getattr(self, "requirement_resolver", None) or RequirementResolver()
        metadata = getattr(ctx, "metadata", {})
        return resolver.resolve(
            document_schema=schema,
            field_contracts=field_contracts,
            evidence=evidence if evidence is not None else metadata.get("document_evidence"),
            project_context=(
                project_context
                if project_context is not None
                else metadata.get("document_project_context")
            ),
            available_sources=(
                available_sources
                if available_sources is not None
                else metadata.get("document_available_sources")
            ),
            generation_policy=(
                generation_policy
                if generation_policy is not None
                else metadata.get("document_generation_policy")
            ),
        )

    @staticmethod
    def _document_requirement_resolution_enabled(ctx: RequestContext) -> bool:
        metadata = getattr(ctx, "metadata", {})
        if isinstance(metadata, dict) and "document_requirement_resolution_enabled" in metadata:
            return bool(metadata["document_requirement_resolution_enabled"])
        return bool(getattr(src.settings, "DOCUMENT_REQUIREMENT_RESOLUTION_ENABLED", False))

    def _document_requirement_resolution_snapshot(
        self,
        ctx: RequestContext,
        *,
        document_schema_id: str,
        document_schema_version: str,
    ) -> dict[str, Any] | None:
        """Resolve requirements only for an explicitly enabled rollout.

        The snapshot is attached to the preflight/session projection.  It is
        never treated as writer authority; the generation worker still uses
        the frozen schema, evidence and policy captured by the WorkOrder.
        """
        if not self._document_requirement_resolution_enabled(ctx):
            return None
        result = self.resolve_document_requirements(
            ctx,
            document_schema_id=document_schema_id,
            document_schema_version=document_schema_version,
        )
        return result.to_dict()

    @staticmethod
    def _attach_requirement_resolution(
        payload: dict[str, Any],
        resolution: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if resolution is not None:
            payload["requirement_resolution"] = resolution
        return payload

    def get_document_template_analysis_for_review(
        self,
        ctx: RequestContext,
        *,
        analysis_id: str,
    ):
        return self.document_generation.get_template_analysis_for_review(
            ctx,
            analysis_id=analysis_id,
        )

    def correct_document_template_analysis(self, ctx: RequestContext, *, correction):
        return self.document_generation.correct_template_analysis(
            ctx,
            correction=correction,
        )

    def confirm_document_template(
        self,
        ctx: RequestContext,
        *,
        analysis_id: str,
        display_name: str,
        execution_mode: str | None = None,
    ):
        return self.document_generation.confirm_template_analysis(
            ctx,
            analysis_id=analysis_id,
            display_name=display_name,
            execution_mode=execution_mode,
        )

    def create_document_generation_session(
        self,
        ctx: RequestContext,
        *,
        knowledge_base_name: str,
        template_version_id: str,
        purpose: str = "",
        output_policy: dict[str, Any] | None = None,
        document_schema_id: str | None = None,
        document_schema_version: str | None = None,
        auto_confirm_recommended: bool = False,
        contract_version: str = "legacy_brief_v1",
        output_spec: dict[str, Any] | None = None,
    ):
        if contract_version == "output_spec_v1":
            return self._create_output_spec_generation_session(
                ctx,
                knowledge_base_name=knowledge_base_name,
                template_version_id=template_version_id,
                purpose=purpose,
                output_policy=output_policy,
                document_schema_id=document_schema_id,
                document_schema_version=document_schema_version,
                output_spec=output_spec,
                auto_confirm_recommended=auto_confirm_recommended,
            )
        if contract_version != "legacy_brief_v1":
            raise ValueError("unsupported generation session contract version")
        template = self.document_generation.store.get_template(template_version_id)
        if template is None:
            raise KeyError("template not found")
        if not ctx.has_kb_permission(knowledge_base_name, "write"):
            raise PermissionError("knowledge base write permission is required")
        self.document_generation._require_template_kb_scope(ctx, template, "write")
        if template.knowledge_base_name != knowledge_base_name:
            raise PermissionError("template does not belong to the selected knowledge base")
        analysis = self.document_generation.store.get_template_analysis(template_version_id)
        if analysis is None:
            raise KeyError("template analysis not found")
        policy = dict(output_policy or {})
        policy.setdefault("format", analysis.format)
        # A one-shot chat request may explicitly authorize the safe template
        # defaults (current published revision, mark missing data as TBD,
        # forbid unsupported inference).  This is a policy decision, not an
        # LLM guess, and remains opt-in for the original clarification API.
        if auto_confirm_recommended:
            brief = GenerationBrief(
                purpose=purpose.strip(),
                scope={"revision": "当前发布版本"},
                output_policy=policy,
                missing_data_policy="mark_tbd",
                inference_policy="forbid",
                confirmed=False,
                confidence=1.0,
            )
        else:
            brief = GenerationBrief(purpose=purpose.strip(), output_policy=policy)
        if document_schema_id:
            resolver_snapshot = self._document_requirement_resolution_snapshot(
                ctx,
                document_schema_id=document_schema_id,
                document_schema_version=document_schema_version or "1",
            )
            if resolver_snapshot is not None:
                brief = brief.model_copy(update={
                    "unresolved_requirements": resolver_snapshot["unresolved_requirements"],
                    "resolved_fields": resolver_snapshot["resolved_fields"],
                })
        session = self.document_generation.store.generation_sessions.create_session(
            tenant_id=ctx.tenant_id or "default",
            user_id=ctx.user_id,
            knowledge_base_name=knowledge_base_name,
            template_version_id=template_version_id,
            brief=brief,
            conversation_id=ctx.metadata.get("conversation_id"),
            initiating_turn_id=ctx.metadata.get("initiating_turn_id"),
        )
        sessions = self.document_generation.store.generation_sessions
        task = self.document_generation.ensure_document_task(
            ctx,
            template_version_id=template_version_id,
            generation_session_id=session.session_id,
            knowledge_base_name=knowledge_base_name,
            idempotency_key=(
                ctx.metadata.get("document_task_idempotency_key")
                or f"generation-session:{session.session_id}"
            ),
            status="needs_clarification",
        )
        if task is not None:
            session = sessions.bind_document_task(session.session_id, task.task_id)
            try:
                self.document_generation.task_service.store.update_status(
                    task.task_id, "needs_clarification"
                )
            except Exception:
                warn(f"failed to project clarification status for task {task.task_id}")
        if auto_confirm_recommended:
            sessions.append_message(
                session.session_id,
                role="system",
                content=(
                    "已按模板推荐值确认：使用当前发布版本；缺失字段标记为未提供；"
                    "禁止无证据推断。"
                ),
                reason="recommended_defaults_applied",
            )
            sessions.confirm(session.session_id)
            if task is not None:
                try:
                    self.document_generation.task_service.store.update_status(
                        task.task_id, "planned"
                    )
                except Exception:
                    warn(f"failed to project confirmed task {task.task_id}")
        else:
            message = self.requirement_clarifier.next_message(
                analysis.model_dump(mode="json"),
                brief,
            )
            sessions.append_message(
                session.session_id,
                role=message.role,
                content=message.content,
                question_id=message.question_id,
                options=message.options,
                reason=message.reason,
            )
        session = self.get_document_generation_session(ctx, session.session_id)
        if auto_confirm_recommended:
            self._project_generation_session_event(
                session,
                "document_clarification_ready",
                {"reason": "recommended_defaults_applied"},
            )
        else:
            pending = next(
                (
                    item for item in reversed(session.messages)
                    if item.role == "assistant" and item.question_id
                ),
                None,
            )
            if pending is not None:
                self._project_generation_session_event(
                    session,
                    "document_clarification_question",
                    {
                        "question_id": pending.question_id,
                        "options": list(pending.options),
                        "reason": pending.reason,
                        "content": pending.content,
                    },
                )
        return session

    def _create_output_spec_generation_session(
        self,
        ctx: RequestContext,
        *,
        knowledge_base_name: str,
        template_version_id: str | None,
        purpose: str = "",
        output_policy: dict[str, Any] | None = None,
        document_schema_id: str | None = None,
        document_schema_version: str | None = None,
        output_spec: dict[str, Any] | None = None,
        auto_confirm_recommended: bool = False,
    ):
        """Create a v2 OutputSpec draft without entering legacy execution."""
        if auto_confirm_recommended:
            raise ValueError("auto_confirm_recommended is not available for output_spec_v1")
        if not ctx.has_kb_permission(knowledge_base_name, "write"):
            raise PermissionError("knowledge base write permission is required")
        template = None
        if template_version_id:
            template = self.document_generation.store.get_template(template_version_id)
            if template is None:
                raise KeyError("template not found")
            self.document_generation._require_template_kb_scope(ctx, template, "write")
            if template.knowledge_base_name != knowledge_base_name:
                raise PermissionError("template does not belong to the selected knowledge base")

        raw = dict(output_spec or {})
        schema = None
        if document_schema_id:
            schema = self.document_generation.store.get_document_schema(
                document_schema_id, document_schema_version or "1",
            )
            if schema is None:
                raise KeyError("document schema not found")
        intake = getattr(self, "output_spec_intake", None) or OutputSpecIntakeService()
        allowed = {
            "purpose", "audience", "document_type", "deliverables", "artifact", "outline",
            "layout_source", "table_requirements", "source_scope", "target_identity", "language", "style",
            "missing_data_policy", "inference_policy", "approval_policy_id",
            "accepted_recommendations",
        }
        draft_kwargs = {key: raw[key] for key in allowed if key in raw}
        draft_kwargs.setdefault("purpose", purpose.strip() or raw.get("purpose"))
        if schema is not None:
            draft_kwargs.setdefault("document_type", schema.document_type)
        if template is not None:
            draft_kwargs["layout_source"] = {
                "mode": "provided_template",
                "template_version_id": template.template_version_id,
                "template_schema_id": template.template_schema_id,
                "template_schema_version": template.template_schema_version,
            }
            draft_kwargs.setdefault("document_type", schema.document_type if schema else template.template_id)
        elif "layout_source" in raw:
            draft_kwargs["layout_source"] = raw["layout_source"]
        # Template references are server-owned.  A client cannot smuggle a
        # provided-template layout into a template-free session.
        layout = draft_kwargs.get("layout_source")
        if isinstance(layout, dict) and layout.get("mode") == "provided_template" and template is None:
            raise ValueError("provided-template layout requires template_version_id")
        draft = intake.start_draft(
            output_spec_id=f"output-spec-{uuid.uuid4().hex}",
            template_version_id=template.template_version_id if template else None,
            template_schema_id=template.template_schema_id if template else None,
            template_schema_version=template.template_schema_version if template else None,
            **draft_kwargs,
        )
        sessions = self.document_generation.store.generation_sessions
        session = sessions.create_session(
            tenant_id=ctx.tenant_id or "default",
            user_id=ctx.user_id,
            knowledge_base_name=knowledge_base_name,
            template_version_id=template.template_version_id if template else None,
            contract_version="output_spec_v1",
            status="awaiting_plan",
            output_spec_id=draft["output_spec_id"],
            output_spec_version=draft["version"],
            output_spec_draft=draft,
            conversation_id=ctx.metadata.get("conversation_id"),
            initiating_turn_id=ctx.metadata.get("initiating_turn_id"),
        )
        task = self.document_generation.ensure_document_task(
            ctx,
            template_version_id=template.template_version_id if template else None,
            generation_session_id=session.session_id,
            knowledge_base_name=knowledge_base_name,
            idempotency_key=(
                ctx.metadata.get("document_task_idempotency_key")
                or f"generation-session:{session.session_id}"
            ),
            status="needs_clarification",
        )
        if task is not None:
            session = sessions.bind_document_task(session.session_id, task.task_id)
        question = intake.next_question(draft)
        if question is None:
            sessions.append_message(
                session.session_id,
                role="assistant",
                content="需求已经明确，可以生成文档计划提案。",
                reason="awaiting_plan",
            )
            if task is not None:
                self.document_generation.task_service.store.update_status(task.task_id, "planned")
        else:
            sessions.append_message(
                session.session_id,
                role="assistant",
                content=question.prompt,
                question_id=question.question_id,
                options=question.options,
                reason=question.reason,
            )
        return self.get_document_generation_session(ctx, session.session_id)

    def get_document_generation_session(self, ctx: RequestContext, session_id: str):
        session = self.document_generation.store.generation_sessions.get_session(
            session_id,
            tenant_id=ctx.tenant_id or "default",
            user_id=ctx.user_id,
        )
        requested_kb = ctx.metadata.get("document_template_kb_name")
        if requested_kb is not None and session.knowledge_base_name != requested_kb:
            raise PermissionError("generation session belongs to another knowledge base")
        return session

    @staticmethod
    def _document_plan_view(
        plan: Any,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        session_status: str | None = None,
        source_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Project a plan to chat/workbench-safe metadata only."""

        issues = list(getattr(plan, "issues", []) or [])
        blocker_items: list[dict[str, Any]] = []
        warning_items: list[str] = []
        for issue in issues:
            code = str(getattr(issue, "code", "plan_issue") or "plan_issue")
            severity = str(getattr(issue, "severity", "warning") or "warning")
            message = str(getattr(issue, "message", "") or "")
            path = str(getattr(issue, "path", "") or "")
            item = {"code": code, "severity": severity}
            if path:
                item["path"] = path
            if message:
                item["message"] = message
            if bool(getattr(issue, "blocking", False)):
                blocker_items.append(item)
            else:
                warning_items.append(message or code)
        layout = getattr(plan, "layout_contract", None)
        layout_summary = {
            "mode": "provided_template" if getattr(layout, "kind", "") == "template" else "generated_structure",
            "template_bound": bool(getattr(layout, "kind", "") == "template"),
        }
        render_spec = dict(getattr(plan, "render_spec", {}) or {})
        deliverable_formats = [
            str(value).strip()
            for value in (render_spec.get("deliverables") or [])
            if str(value).strip()
        ]
        if not deliverable_formats and render_spec.get("format"):
            deliverable_formats = [str(render_spec["format"]).strip()]
        deliverables = [
            {"format": value, "role": "primary" if index == 0 else "derivative"}
            for index, value in enumerate(deliverable_formats)
        ]
        summary = dict(getattr(plan, "output_spec_summary", {}) or {})
        policies = {
            "missing_data": summary.get("missing_data_policy"),
            "inference": summary.get("inference_policy"),
            "approval_policy_id": summary.get("approval_policy_id"),
        }
        status = str(session_status or getattr(plan, "status", "proposed") or "proposed")
        executable = bool(getattr(plan, "is_executable", False))
        return {
            "session_id": session_id,
            "task_id": task_id,
            "document_plan_id": str(getattr(plan, "document_plan_id", "")),
            "document_plan_version": int(getattr(plan, "version", 1) or 1),
            "plan_hash": getattr(plan, "plan_hash", None),
            "output_spec_id": getattr(plan, "output_spec_id", None),
            "output_spec_version": getattr(plan, "output_spec_version", None),
            "output_spec_hash": getattr(plan, "output_spec_hash", None),
            "status": status,
            "executable": executable,
            "deliverables": deliverables,
            "layout_summary": layout_summary,
            "outline_count": len(summary.get("outline_unit_ids") or []),
            "table_count": len(summary.get("table_unit_ids") or []),
            "source_summary": dict(source_summary or {"snapshot_bound": True}),
            "policies": policies,
            "warnings": warning_items,
            "blockers": blocker_items,
            "next_actions": (
                ["confirm_document_plan"]
                if executable
                else ["resolve_plan_blockers", "update_output_spec"]
            ),
        }

    def _planning_source_snapshot(
        self,
        ctx: RequestContext,
        *,
        knowledge_base_name: str,
    ) -> tuple[Any, dict[str, Any]]:
        """Freeze currently readable source identities for a proposal."""

        kb = str(knowledge_base_name or "").strip()
        if not kb or not ctx.has_kb_permission(kb, "read"):
            raise PermissionError("knowledge base read permission is required")
        source_names: list[str] = []
        for document in self.list_file_infos(kb, ctx):
            name = document.get("name", "") if isinstance(document, dict) else getattr(document, "name", "")
            normalized = str(name or "").strip()
            if normalized and normalized not in source_names:
                source_names.append(normalized)
        attachment_ids = [
            str(item).strip()
            for item in (ctx.metadata.get("document_authoring_attachment_ids", []) or [])
            if str(item).strip()
        ]
        snapshot = self.document_generation._create_knowledge_base_source_snapshot(
            ctx,
            kb,
            source_names,
        )
        source_hash = content_hash({
            # KnowledgeBaseSourceSnapshot includes created_at and therefore
            # its row hash is intentionally unique per freeze.  Proposal
            # retries need a semantic scope hash independent of that nonce.
            "knowledge_base_name": kb,
            "source_names": sorted(set(source_names)),
            "attachment_ids": sorted(set(attachment_ids)),
        })
        return snapshot, {
            "knowledge_base_count": 1,
            "attachment_count": len(set(attachment_ids)),
            "source_count": len(source_names),
            "snapshot_bound": True,
            "snapshot_hash": source_hash,
            "snapshot_content_hash": str(snapshot.content_hash),
        }

    @staticmethod
    def _synthetic_document_schema(spec: OutputSpec) -> DocumentSchema:
        """Give template-free Phase 1 proposals a schema for shadow compilation."""

        return DocumentSchema(
            document_schema_id=f"generated:{spec.output_spec_id}",
            version=str(spec.version),
            document_type=spec.document_type,
            status="approved",
            execution_mode="deterministic_only",
            fields=[
                DocumentFieldSchema(
                    field_id=unit.unit_id,
                    label=unit.title or unit.unit_id,
                    required=unit.required,
                    value_type="table" if unit.kind == "table" else "text",
                    retrieval_policy_id="generic-retrieval",
                    verification_policy_id="generic-verification",
                )
                for unit in spec.outline
                if unit.kind in {"field", "paragraph", "section", "table", "list"}
            ],
        )

    def create_document_plan_proposal(
        self,
        ctx: RequestContext,
        session_id: str,
        *,
        client_request_id: str | None = None,
        expected_output_spec_version: int = 1,
    ) -> dict[str, Any]:
        """Compile and persist one hash-bound proposal for a v2 session."""

        session = self.get_document_generation_session(ctx, session_id)
        if session.contract_version != "output_spec_v1":
            raise ValueError("plan proposals require output_spec_v1")
        if session.status not in {"awaiting_plan", "awaiting_plan_confirmation"}:
            raise ValueError("generation session is not awaiting a plan proposal")
        current_version = int(session.output_spec_version or 0)
        if current_version != int(expected_output_spec_version):
            raise ValueError(
                f"expected output spec version {expected_output_spec_version}, current version is {current_version}"
            )
        draft = session.output_spec_draft
        if not isinstance(draft, dict):
            raise ValueError("output spec draft is missing")
        intake = getattr(self, "output_spec_intake", None) or OutputSpecIntakeService()
        spec = intake.to_output_spec(draft).model_copy(update={"status": "proposed"})
        snapshot, source_summary = self._planning_source_snapshot(
            ctx,
            knowledge_base_name=session.knowledge_base_name,
        )
        template = None
        analysis = None
        bindings: list[Any] = []
        template_hash = ""
        template_id = session.template_version_id
        if not template_id and getattr(spec.layout_source, "mode", "") == "provided_template":
            template_id = spec.layout_source.template_version_id
        if template_id:
            template = self.document_generation.store.get_template(template_id)
            if template is None:
                raise KeyError("template not found")
            self.document_generation._require_template_kb_scope(ctx, template, "read")
            schema = self.document_generation.store.get_document_schema(
                template.template_schema_id,
                template.template_schema_version,
            )
            if schema is None:
                raise KeyError("document schema not found")
            analysis = self.document_generation.store.get_template_analysis(template_id)
            bindings = list(
                self.document_generation.store.list_unit_bindings(
                    template.template_schema_id,
                    template.template_schema_version,
                )
            )
            template_hash = str(template.content_hash or "")
        else:
            schema = self._synthetic_document_schema(spec)
        plan = self.document_generation.planning.compile(
            output_spec=spec,
            document_schema=schema,
            template_analysis=analysis,
            bindings=bindings,
            source_snapshot_id=str(snapshot.source_set_snapshot_id),
            source_snapshot_hash=str(source_summary["snapshot_content_hash"]),
        )
        summary = dict(plan.output_spec_summary)
        summary.update({
            "template_content_hash": template_hash,
            "source_scope_hash": source_summary["snapshot_hash"],
        })
        plan_payload = plan.model_dump(mode="json", exclude={"plan_hash"})
        plan_payload["output_spec_summary"] = summary
        plan = type(plan).model_validate(plan_payload)
        planning = self.document_generation.planning
        planning_store = planning.store
        task_id = str(session.document_task_id or session.session_id)
        latest = planning_store.get_latest_plan(
            task_id,
            tenant_id=ctx.tenant_id or "default",
            user_id=ctx.user_id,
        ) if planning_store is not None else None
        state_matches = bool(
            latest
            and latest.status != "stale"
            and latest.output_spec_hash == spec.content_hash
            and str((latest.output_spec_summary or {}).get("source_scope_hash") or "") == source_summary["snapshot_hash"]
            and str((latest.output_spec_summary or {}).get("template_content_hash") or "") == template_hash
        )
        if state_matches:
            return self._document_plan_view(
                latest,
                session_id=session.session_id,
                task_id=session.document_task_id,
                session_status=session.status,
                source_summary=source_summary,
            )
        if latest is not None and latest.status in {"proposed", "blocked"} and planning_store is not None:
            reason = "source_changed"
            if latest.output_spec_hash != spec.content_hash:
                reason = "output_spec_changed"
            elif str((latest.output_spec_summary or {}).get("template_content_hash") or "") != template_hash:
                reason = "template_changed"
            try:
                planning_store.mark_plan_stale(
                    latest.document_plan_id,
                    latest.version,
                    expected_plan_hash=latest.plan_hash,
                    reason_code=reason,
                    tenant_id=ctx.tenant_id or "default",
                    user_id=ctx.user_id,
                )
            except ValueError:
                pass
            digest = hashlib.sha256(
                f"{spec.content_hash}|{source_summary['snapshot_hash']}|{template_hash}".encode()
            ).hexdigest()[:16]
            plan_payload = plan.model_dump(mode="json", exclude={"plan_hash"})
            plan_payload["document_plan_id"] = f"{plan.document_plan_id}:proposal-{digest}"
            plan = type(plan).model_validate(plan_payload)
        elif latest is not None and not state_matches:
            digest = hashlib.sha256(
                f"{spec.content_hash}|{source_summary['snapshot_hash']}|{template_hash}".encode()
            ).hexdigest()[:16]
            plan_payload = plan.model_dump(mode="json", exclude={"plan_hash"})
            plan_payload["document_plan_id"] = f"{plan.document_plan_id}:proposal-{digest}"
            plan = type(plan).model_validate(plan_payload)
        if latest is not None and not state_matches:
            try:
                self.document_generation.store.generation_sessions.clear_plan(session.session_id)
            except (KeyError, ValueError):
                pass
            if session.document_task_id:
                try:
                    self.document_generation.task_service.store.clear_plan(session.document_task_id)
                except (KeyError, ValueError):
                    pass
        persisted = planning.persist_proposal(
            output_spec=spec,
            plan=plan,
            tenant_id=ctx.tenant_id or "default",
            user_id=ctx.user_id,
            task_id=task_id,
            idempotency_key=f"plan-proposal:{plan.document_plan_id}:v{plan.version}",
        )
        next_status = "awaiting_plan_confirmation" if persisted.is_executable else "awaiting_plan"
        sessions = self.document_generation.store.generation_sessions
        sessions.bind_plan(
            session.session_id,
            output_spec_id=spec.output_spec_id,
            output_spec_version=spec.version,
            document_plan_id=persisted.document_plan_id,
            document_plan_version=persisted.version,
            status=next_status,
        )
        if session.document_task_id:
            try:
                self.document_generation.task_service.store.bind_plan(
                    session.document_task_id,
                    output_spec_id=spec.output_spec_id,
                    output_spec_version=spec.version,
                    document_plan_id=persisted.document_plan_id,
                    document_plan_version=persisted.version,
                )
                self.document_generation.task_service.store.update_status(
                    session.document_task_id,
                    next_status,
                )
            except (KeyError, ValueError):
                pass
        self._project_generation_session_event(
            sessions.get_session(session.session_id),
            "document_plan_proposed",
            {
                "document_plan_id": persisted.document_plan_id,
                "document_plan_version": persisted.version,
                "output_spec_version": spec.version,
                "status": next_status,
            },
        )
        return self._document_plan_view(
            persisted,
            session_id=session.session_id,
            task_id=session.document_task_id,
            session_status=next_status,
            source_summary=source_summary,
        )

    def get_document_plan(
        self,
        ctx: RequestContext,
        document_plan_id: str,
        version: int,
    ) -> dict[str, Any] | None:
        planning_store = self.document_generation.planning.store
        if planning_store is None:
            return None
        plan = planning_store.get_plan(
            document_plan_id,
            int(version),
            tenant_id=ctx.tenant_id or "default",
            user_id=ctx.user_id,
        )
        if plan is None:
            return None
        source_summary = {
            "snapshot_bound": True,
            "snapshot_hash": str((plan.output_spec_summary or {}).get("source_scope_hash") or plan.source_snapshot_hash),
        }
        return self._document_plan_view(plan, source_summary=source_summary)

    def answer_document_generation_session(
        self,
        ctx: RequestContext,
        session_id: str,
        *,
        question_id: str,
        answer: str,
        client_request_id: str | None = None,
    ):
        session = self.get_document_generation_session(ctx, session_id)
        if not ctx.has_kb_permission(session.knowledge_base_name, "write"):
            raise PermissionError("knowledge base write permission is required")
        if ctx.metadata.get("document_template_kb_name") != session.knowledge_base_name:
            raise PermissionError("generation session belongs to another knowledge base")
        if session.contract_version == "output_spec_v1":
            return self._answer_output_spec_generation_session(
                ctx,
                session,
                question_id=question_id,
                answer=answer,
                client_request_id=client_request_id,
            )
        brief = self.requirement_clarifier.apply_answer(
            session.brief,
            question_id=question_id,
            answer=answer,
        )
        analysis = self.document_generation.store.get_template_analysis(
            session.template_version_id,
        )
        if analysis is None:
            raise KeyError("template analysis not found")
        message = self.requirement_clarifier.next_message(
            analysis.model_dump(mode="json"),
            brief,
        )
        sessions = self.document_generation.store.generation_sessions
        revised, applied = sessions.apply_clarification_answer(
            session_id,
            question_id=question_id,
            answer=answer,
            brief=brief,
            next_message=message,
            client_request_id=client_request_id,
        )
        if applied:
            task_id = revised.document_task_id
            if task_id:
                try:
                    self.document_generation.task_service.store.update_status(
                        task_id, "needs_clarification"
                    )
                except Exception:
                    warn(f"failed to project clarification answer for task {task_id}")
            self._project_generation_session_event(
                revised,
                "document_clarification_answer",
                {
                    "question_id": question_id,
                    "answer": answer.strip(),
                    "client_request_id": client_request_id,
                },
            )
            event_type = (
                "document_clarification_ready"
                if message.reason == "ready_to_generate"
                else "document_clarification_question"
            )
            self._project_generation_session_event(
                revised,
                event_type,
                {
                    "question_id": message.question_id,
                    "options": list(message.options),
                    "reason": message.reason,
                    "content": message.content,
                },
            )
        return self.get_document_generation_session(ctx, session_id)

    def _answer_output_spec_generation_session(
        self,
        ctx: RequestContext,
        session: Any,
        *,
        question_id: str,
        answer: str,
        client_request_id: str | None = None,
    ):
        intake = getattr(self, "output_spec_intake", None) or OutputSpecIntakeService()
        draft = session.output_spec_draft
        if not isinstance(draft, dict):
            raise ValueError("output spec draft is missing")
        expected_version = int(session.output_spec_version or draft.get("version") or 0)
        if answer.strip().casefold() in {"采用推荐方案", "use recommended", "accept recommendations"}:
            revised_draft = intake.accept_recommendation(
                draft,
                recommendation_id=OutputSpecIntakeService._RECOMMENDATION_ID,
                expected_version=expected_version,
            )
        else:
            revised_draft = intake.merge_answer(
                draft,
                expected_version=expected_version,
                question_id=question_id,
                answer=answer,
            )
        sessions = self.document_generation.store.generation_sessions
        revised = sessions.update_output_spec_draft(
            session.session_id,
            revised_draft,
            expected_version=expected_version,
            status="awaiting_plan",
        )
        try:
            sessions.append_message(
                session.session_id,
                role="user",
                content=answer.strip(),
                question_id=question_id,
                answer=answer.strip(),
                client_request_id=client_request_id,
            )
        except Exception:
            # The draft CAS has already made the answer durable.  A duplicate
            # browser request must not turn a successful draft update into a
            # failed authoring session.
            pass
        question = intake.next_question(revised_draft)
        if question is None:
            sessions.append_message(
                session.session_id,
                role="assistant",
                content="需求已经明确，可以生成文档计划提案。",
                reason="awaiting_plan",
            )
            task_status = "planned"
        else:
            sessions.append_message(
                session.session_id,
                role="assistant",
                content=question.prompt,
                question_id=question.question_id,
                options=question.options,
                reason=question.reason,
            )
            task_status = "needs_clarification"
        if revised.document_task_id:
            try:
                self.document_generation.task_service.store.update_status(
                    revised.document_task_id, task_status,
                )
            except Exception:
                warn("failed to project output spec clarification status")
        return self.get_document_generation_session(ctx, session.session_id)

    def confirm_document_generation_session(self, ctx: RequestContext, session_id: str):
        session = self.get_document_generation_session(ctx, session_id)
        if not ctx.has_kb_permission(session.knowledge_base_name, "write"):
            raise PermissionError("knowledge base write permission is required")
        if ctx.metadata.get("document_template_kb_name") != session.knowledge_base_name:
            raise PermissionError("generation session belongs to another knowledge base")
        analysis = self.document_generation.store.get_template_analysis(
            session.template_version_id,
        )
        if analysis is None:
            raise KeyError("template analysis not found")
        ready = self.requirement_clarifier.next_message(
            analysis.model_dump(mode="json"),
            session.brief,
        )
        if ready.reason != "ready_to_generate":
            raise ValueError("generation brief still has unanswered questions")
        confirmed = self.document_generation.store.generation_sessions.confirm(session_id)
        if confirmed.document_task_id:
            try:
                self.document_generation.task_service.store.update_status(
                    confirmed.document_task_id, "planned"
                )
            except Exception:
                warn(f"failed to project confirmed task {confirmed.document_task_id}")
        self._project_generation_session_event(
            confirmed,
            "document_clarification_ready",
            {"reason": "user_confirmed"},
        )
        return confirmed

    @staticmethod
    def _project_generation_session_event(
        session: Any,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Project safe clarification metadata into the owning Chat turn.

        GenerationSession remains the requirement-state source of truth.  The
        Conversation event is a durable, replayable view for Chat/Workbench
        timelines and intentionally carries identifiers and question metadata
        only, never source evidence or rendered document content.
        """
        turn_id = str(getattr(session, "initiating_turn_id", None) or "").strip()
        conversation_id = str(getattr(session, "conversation_id", None) or "").strip()
        if not turn_id or not conversation_id:
            return
        try:
            conversation = ConversationService()
            turn = conversation.get_turn_unscoped(turn_id)
            if turn is None or str(turn.session_id) != conversation_id:
                return
            projected = {
                "generation_session_id": session.session_id,
                "document_task_id": session.document_task_id,
                "conversation_id": conversation_id,
                "initiating_turn_id": turn_id,
                "clarification_revision": session.clarification_revision,
                **{
                    key: value
                    for key, value in payload.items()
                    if value is not None
                },
            }
            conversation.append_turn_event(turn_id, event_type, projected)
        except Exception as exc:
            # Projection is additive during migration.  The authoring state
            # was already committed and remains authoritative if the chat DB
            # is temporarily unavailable.
            warn(f"failed to project generation session event {event_type}: {exc}")

    def create_document_work_order(self, ctx: RequestContext, **kwargs):
        return self.document_generation.create_document_work_order(ctx, **kwargs)

    def get_icd_scope_review(self, ctx: RequestContext, work_order_id: str):
        return self.document_generation.get_icd_scope_review(ctx, work_order_id)

    def submit_icd_scope_resolution(
        self,
        ctx: RequestContext,
        work_order_id: str,
        *,
        resolutions: list[dict[str, str]],
        comment: str,
    ):
        return self.document_generation.submit_icd_scope_resolution(
            ctx,
            work_order_id,
            resolutions=resolutions,
            comment=comment,
        )

    def create_knowledge_base_document_work_order(
        self,
        ctx: RequestContext,
        *,
        knowledge_base_name: str,
        **kwargs,
    ):
        """Create a KB work order from the sources readable at creation time."""
        # Creation freezes the readable source snapshot.  The mutating
        # generation/approval operations below still require write access;
        # keeping creation read-scoped preserves the legacy API contract and
        # lets a read-only user prepare a reviewable work order without
        # authorizing document mutation at this boundary.  Chat/API callers
        # that create a job use their explicit write gate before reaching us.
        if not ctx.has_kb_permission(knowledge_base_name, "read"):
            raise PermissionError("knowledge base read permission is required")
        requested_scope = str(kwargs.get("source_scope") or "knowledge_base_only").strip()
        has_attachment_refs = bool(kwargs.get("attachment_refs"))
        effective_scope = resolve_source_scope(
            requested_scope=requested_scope,
            has_attachments=has_attachment_refs,
            is_general_chat=False,
        )
        source_names = []
        if effective_scope != "attachment_only":
            for document in self.list_file_infos(knowledge_base_name, ctx):
                name = (
                    document.get("name", "")
                    if isinstance(document, dict)
                    else getattr(document, "name", "")
                )
                normalized = str(name).strip()
                if normalized and normalized not in source_names:
                    source_names.append(normalized)
        if not source_names and effective_scope == "knowledge_base_only":
            raise ValueError(
                "knowledge base has no readable source documents; "
                "upload and parse a source before generating a document"
            )
        kwargs.pop("source_names", None)
        return self.document_generation.create_knowledge_base_work_order(
            ctx,
            knowledge_base_name=knowledge_base_name,
            source_names=source_names,
            **kwargs,
        )

    def restart_cancelled_knowledge_base_document_generation(
        self,
        ctx: RequestContext,
        work_order_id: str,
        *,
        max_parallel_units: int = 8,
    ):
        return self.document_generation.restart_cancelled_knowledge_base_work_order(
            ctx, work_order_id, max_parallel_units=max_parallel_units,
        )

    def prepare_knowledge_base_document_generation(
        self,
        ctx: RequestContext,
        *,
        knowledge_base_name: str,
        template_version_id: str,
        document_schema_id: str,
        document_schema_version: str,
        generation_session_id: str | None = None,
        **kwargs,
    ):
        """Create a KB work order and run the fast, non-harness preflight.

        Mirrors the synchronous pre-harness half of ``auto_generate_...``:
        create the order, resolve its frozen source snapshot, check the ICD
        template profile, and build the ICD connector-scope decision. Returns a
        stage dict. The slow harness half is submitted separately via
        ``submit_knowledge_base_document_generation``.
        """
        if generation_session_id:
            kwargs.update(self._generation_session_work_order_inputs(
                ctx,
                generation_session_id=generation_session_id,
                knowledge_base_name=knowledge_base_name,
                template_version_id=template_version_id,
            ))
        order = self.create_knowledge_base_document_work_order(
            ctx,
            knowledge_base_name=knowledge_base_name,
            template_version_id=template_version_id,
            document_schema_id=document_schema_id,
            document_schema_version=document_schema_version,
            **kwargs,
        )
        if generation_session_id:
            self.document_generation.store.generation_sessions.bind_work_order(
                generation_session_id,
                order.work_order_id,
            )
        snapshot = self.document_generation.resolve_source_snapshot(order)
        return self._kb_document_generation_stages(
            ctx,
            order,
            snapshot,
            knowledge_base_name=knowledge_base_name,
            document_schema_id=document_schema_id,
            document_schema_version=document_schema_version,
        )

    def _kb_document_generation_stages(
        self,
        ctx: RequestContext,
        order,
        snapshot,
        *,
        knowledge_base_name: str,
        document_schema_id: str,
        document_schema_version: str,
    ):
        """Run the shared fast preflight for a frozen KB document Work Order.

        Mirrors the synchronous pre-harness half of ``auto_generate_...``:
        check the ICD template profile and build the ICD connector-scope
        decision.  Used by both the legacy prepare path and the plan-submission
        dispatcher so a confirmed plan cannot skip a human gate.
        """
        requirement_resolution = self._document_requirement_resolution_snapshot(
            ctx,
            document_schema_id=document_schema_id,
            document_schema_version=document_schema_version,
        )
        profile = self._icd_template_profile(order)
        if profile is not None and profile.kind == "icd_sample":
            return self._attach_requirement_resolution(
                {
                    "stage": "template_contract_review_required",
                    "work_order_id": order.work_order_id,
                    "task_id": getattr(order, "task_id", None),
                    "issues": [{
                        "code": "icd_formal_template_required",
                        "severity": "blocking",
                        "message": "ICD 示例模板缺少正式连接器定义；请上传含连接器编号、板端型号和管脚定义表的正式 ICD 模板。",
                    }, *profile.issues],
                },
                requirement_resolution,
            )
        scope_review = None
        icd_schema = self._icd_connector_scope_schema(order)
        if icd_schema is not None:
            scope_kwargs = self._document_retriever_scope_kwargs(order)
            requested_scope = scope_kwargs.get("source_scope", "knowledge_base_only")
            attachment_refs = list(scope_kwargs.get("attachment_refs", []) or [])
            attachment_ids = [
                str(
                    ref.get("attachment_id", "")
                    if isinstance(ref, dict)
                    else getattr(ref, "attachment_id", "")
                ).strip()
                for ref in attachment_refs
            ]
            attachment_ids = list(dict.fromkeys(item for item in attachment_ids if item))
            effective_scope = resolve_source_scope(
                requested_scope=requested_scope,
                has_attachments=bool(attachment_refs),
                is_general_chat=False,
            )
            kb_provider = KnowledgeBaseEvidenceProvider(
                self.backend,
                top_k=src.settings.FINAL_TOP_K,
                source_names=list(snapshot.source_names),
            )
            providers = [kb_provider]
            if attachment_refs and effective_scope in {
                "attachment_only", "attachment_and_knowledge_base",
            }:
                providers.append(AttachmentEvidenceProvider(
                    retrieval=AttachmentRetrievalService(),
                    refs_snapshot=attachment_refs,
                    top_k=src.settings.FINAL_TOP_K,
                ))
            supporting_evidences = CompositeDocumentEvidenceProvider(providers).retrieve(
                query="ICD connector pin mapping",
                source_scope=effective_scope,
                attachment_ids=attachment_ids,
                kb_name=knowledge_base_name,
                ctx=ctx,
            )
            connector_refdes = (
                list(dict.fromkeys(
                    block.location_number
                    for block in profile.connector_blocks
                    if block.location_number
                ))
                if profile is not None and profile.kind == "icd"
                else list(dict.fromkeys([
                    *_connector_refdes_from_schema(icd_schema),
                    *supported_connector_refdes(supporting_evidences),
                    *self._icd_front_view_connector_refdes(order),
                ]))
            )
            if connector_refdes:
                circuit_evidences = (
                    self.circuit_service.list_pin_mapping_evidence(
                        knowledge_base_name,
                        list(snapshot.source_names),
                        ctx,
                        refdes=connector_refdes,
                    )
                    if (
                        getattr(self, "circuit_service", None) is not None
                        and scope_allows_kb(effective_scope)
                        and snapshot.source_names
                    )
                    else []
                )
                decision = build_icd_scope_decision(
                    circuit_evidences,
                    supporting_evidences,
                    connector_refdes=connector_refdes,
                )
            else:
                decision = build_unknown_connector_scope_decision()
            scope_review = self.document_generation.prepare_icd_scope_review(
                ctx,
                order.work_order_id,
                decision,
            )
            if scope_review.pending_count:
                return self._attach_requirement_resolution(
                    {
                        "stage": "scope_review_required",
                        "work_order_id": order.work_order_id,
                        "task_id": getattr(order, "task_id", None),
                        "exceptions": [
                            exception.model_dump()
                            if hasattr(exception, "model_dump")
                            else dict(vars(exception))
                            for exception in scope_review.exceptions
                        ],
                    },
                    requirement_resolution,
                )
        return self._attach_requirement_resolution(
            {
                "stage": "ready",
                "work_order_id": order.work_order_id,
                "task_id": getattr(order, "task_id", None),
            },
            requirement_resolution,
        )

    def dispatch_confirmed_document_plan(
        self,
        ctx: RequestContext,
        submission,
    ) -> dict[str, Any]:
        """Materialize one confirmed plan into its template-backed Work Order.

        Only immutable identifiers/hashes arrive in ``submission``.  The
        accepted plan, frozen snapshot and approved template are re-read from
        this database and re-authorized here; the Work Order carries the v3
        fingerprint binding the planning references.  A preflight human gate
        parks the submission instead of creating a generation job.
        """
        planning = getattr(self.document_generation, "planning", None)
        store = getattr(planning, "store", None)
        if store is None:
            raise RuntimeError("document planning store is not configured")
        plan = store.get_plan(
            submission.document_plan_id,
            submission.document_plan_version,
            tenant_id=submission.tenant_id,
            user_id=submission.user_id,
        )
        if plan is None or plan.status != "accepted" or plan.plan_hash != submission.document_plan_hash:
            raise ValueError("accepted document plan is missing or changed")
        if plan.output_spec_hash != submission.output_spec_hash:
            raise ValueError("accepted OutputSpec does not match the submission")
        output_spec = store.get_output_spec(
            submission.output_spec_id,
            submission.output_spec_version,
            tenant_id=submission.tenant_id,
            user_id=submission.user_id,
        )
        if (
            output_spec is None
            or output_spec.status != "accepted"
            or output_spec.content_hash != submission.output_spec_hash
        ):
            raise ValueError("accepted OutputSpec is missing or changed")

        is_template_free = getattr(plan.layout_contract, "kind", "") == "structure"
        if not is_template_free and getattr(plan.layout_contract, "kind", "") != "template":
            raise ValueError("document plan has an unsupported layout contract")
        recipe = None
        recipe_key = ""
        primary_format = ""
        if is_template_free:
            if not getattr(src.settings, "DOCUMENT_TEMPLATE_FREE_EXECUTION_ENABLED", False):
                raise ValueError("template-free execution is disabled")
            from src.document_authoring.planning.recipes import build_builtin_recipe_registry

            layout = plan.layout_contract
            recipe_id = str(
                (plan.render_spec or {}).get("recipe_id")
                or getattr(layout, "structure_profile_id", "")
            ).strip()
            recipe_version = str(
                (plan.render_spec or {}).get("recipe_version")
                or getattr(layout, "structure_profile_version", "")
            ).strip()
            recipe_key = f"{recipe_id}@{recipe_version}"
            recipe = build_builtin_recipe_registry().lookup(recipe_id, recipe_version)
            primary_deliverables = [
                item for item in output_spec.artifact.deliverables
                if item.role == "primary"
            ]
            if recipe is None or recipe.status != "available" or len(primary_deliverables) != 1:
                raise ValueError("template-free recipe or primary deliverable is unavailable")
            primary_format = primary_deliverables[0].format
            if primary_format not in recipe.supported_formats:
                raise ValueError("template-free recipe does not support the primary deliverable")
            if not all(
                _template_free_allowlisted(value, configured)
                for value, configured in (
                    (submission.tenant_id, getattr(src.settings, "DOCUMENT_TEMPLATE_FREE_ALLOWLIST_TENANTS", ())),
                    (output_spec.document_type, getattr(src.settings, "DOCUMENT_TEMPLATE_FREE_ALLOWLIST_DOCUMENT_TYPES", ())),
                    (primary_format, getattr(src.settings, "DOCUMENT_TEMPLATE_FREE_ALLOWLIST_FORMATS", ())),
                    (recipe_key, getattr(src.settings, "DOCUMENT_TEMPLATE_FREE_ALLOWLIST_RECIPES", ())),
                )
            ):
                raise ValueError("template-free execution scope is not present in the allowlist")

        knowledge_base_name = submission.knowledge_base_name
        if not ctx.has_kb_permission(knowledge_base_name, "read"):
            raise PermissionError("knowledge base read permission is required")
        snapshot = self.document_generation.store.get_knowledge_base_source_snapshot(
            submission.source_snapshot_id,
        )
        if (
            snapshot is None
            or snapshot.tenant_id != submission.tenant_id
            or snapshot.knowledge_base_name != knowledge_base_name
            or snapshot.content_hash != submission.source_snapshot_hash
        ):
            raise ValueError("frozen source snapshot is missing or changed")

        layout = plan.layout_contract
        template = None
        schema = None
        if not is_template_free:
            template = self.document_generation.store.get_template(layout.template_version_id)
            template_hash = str((plan.output_spec_summary or {}).get("template_content_hash") or "")
            if (
                template is None
                or template.status != "approved"
                or template.content_hash != template_hash
            ):
                raise ValueError("frozen template is missing or changed")
            schema = self.document_generation.store.get_document_schema(
                template.template_schema_id,
                template.template_schema_version,
            )
            if schema is None or schema.status != "approved":
                raise ValueError("frozen document schema is missing or not approved")

        idempotency_key = (
            f"document-plan:{plan.document_plan_id}:{plan.version}:{plan.plan_hash}"
        )
        order = self.document_generation._find_knowledge_base_work_order_by_idempotency(
            submission.tenant_id,
            knowledge_base_name,
            idempotency_key,
            department_id=self.document_generation._ctx_department_id(ctx),
        )
        if order is None:
            if is_template_free:
                order = self.document_generation._create_template_free_work_order(
                    ctx,
                    plan=plan,
                    output_spec=output_spec,
                    snapshot=snapshot,
                    knowledge_base_name=knowledge_base_name,
                    idempotency_key=idempotency_key,
                    generation_session_id=submission.session_id,
                    source_scope="knowledge_base_only",
                )
            else:
                order = self.document_generation._create_frozen_work_order(
                    ctx,
                    scope_type="knowledge_base",
                    snapshot=snapshot,
                    knowledge_base_name=knowledge_base_name,
                    template_version_id=template.template_version_id,
                    document_schema_id=schema.document_schema_id,
                    document_schema_version=schema.version,
                    idempotency_key=idempotency_key,
                    generation_session_id=submission.session_id,
                    source_scope="knowledge_base_only",
                    output_spec_id=submission.output_spec_id,
                    output_spec_version=submission.output_spec_version,
                    output_spec_hash=submission.output_spec_hash,
                    document_plan_id=submission.document_plan_id,
                    document_plan_version=submission.document_plan_version,
                    document_plan_hash=submission.document_plan_hash,
                )
            try:
                self.document_generation.store.generation_sessions.bind_work_order(
                    submission.session_id,
                    order.work_order_id,
                )
            except (KeyError, ValueError):
                pass

        if is_template_free:
            # Recipe plans have no uploaded template, ICD template profile or
            # template-specific connector scope. Their accepted plan and
            # server-owned recipe are the complete layout preflight.
            stage = {
                "stage": "ready",
                "work_order_id": order.work_order_id,
                "task_id": getattr(order, "task_id", None),
            }
        else:
            stage = self._kb_document_generation_stages(
                ctx,
                order,
                snapshot,
                knowledge_base_name=knowledge_base_name,
                document_schema_id=(schema.document_schema_id if schema is not None else order.document_schema_id),
                document_schema_version=(schema.version if schema is not None else order.document_schema_version),
            )
        if stage.get("stage") != "ready":
            result = {"status": "waiting_human", **stage}
            result.pop("job_id", None)
            return result
        job_id = self.submit_knowledge_base_document_generation(
            ctx, order.work_order_id,
        )
        return {
            "status": "dispatched",
            "stage": "ready",
            "work_order_id": order.work_order_id,
            "job_id": job_id,
        }

    def confirm_document_plan(
        self,
        ctx: RequestContext,
        session_id: str,
        *,
        expected_output_spec_hash: str,
        expected_plan_hash: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        """Gate 1: accept the proposed plan and enqueue its durable submission.

        The whole acceptance transition commits inside
        ``document_authoring.db``; the Work Order/job are materialized later
        by the outbox worker in the separate auth/job database.
        """
        session = self.get_document_generation_session(ctx, session_id)
        if session.contract_version != "output_spec_v1":
            raise ValueError("plan confirmation requires output_spec_v1")
        planning_store = self.document_generation.planning.store
        submission = planning_store.confirm_submission(
            session_id=session.session_id,
            tenant_id=ctx.tenant_id or "default",
            user_id=ctx.user_id,
            expected_output_spec_hash=expected_output_spec_hash,
            expected_plan_hash=expected_plan_hash,
            client_request_id=client_request_id,
        )
        return self._document_plan_submission_view(submission)

    def _document_plan_submission_view(self, submission) -> dict[str, Any]:
        next_actions = ["await_generation"]
        if submission.status in {"pending", "retrying", "running"}:
            next_actions = ["await_generation"]
        elif submission.status == "waiting_human":
            next_actions = ["open_document_workbench"]
        elif submission.status in {"failed", "dead_letter"}:
            next_actions = ["review_failure"]
        return {
            "submission_id": submission.submission_id,
            "status": submission.status,
            "session_id": submission.session_id,
            "task_id": submission.task_id,
            "document_plan_id": submission.document_plan_id,
            "document_plan_version": submission.document_plan_version,
            "plan_hash": submission.document_plan_hash,
            "work_order_id": submission.work_order_id,
            "job_id": submission.job_id,
            "next_actions": next_actions,
        }

    def _generation_session_work_order_inputs(
        self,
        ctx: RequestContext,
        *,
        generation_session_id: str,
        knowledge_base_name: str,
        template_version_id: str,
    ) -> dict[str, Any]:
        session = self.get_document_generation_session(ctx, generation_session_id)
        if session.status != "ready_to_generate" or not session.brief.confirmed:
            raise ValueError("generation session must be confirmed before creating a work order")
        if session.knowledge_base_name != knowledge_base_name:
            raise PermissionError("generation session belongs to another knowledge base")
        if session.template_version_id != template_version_id:
            raise ValueError("generation session template differs from the selected template")
        return {
            "generation_session_id": session.session_id,
            "generation_brief": session.brief.model_dump(mode="json"),
            "idempotency_key": f"generation-session:{session.session_id}",
        }

    def auto_generate_knowledge_base_document(
        self,
        ctx: RequestContext,
        *,
        knowledge_base_name: str,
        **kwargs,
    ):
        """Create and run a KB work order using only its frozen source snapshot."""
        prepared = self.prepare_knowledge_base_document_generation(
            ctx,
            knowledge_base_name=knowledge_base_name,
            **kwargs,
        )
        if prepared.get("stage") != "ready":
            return prepared
        work_order_id = prepared["work_order_id"]
        order = self.document_generation.store.get_work_order(work_order_id)
        snapshot = self.document_generation.resolve_source_snapshot(order)
        scope_review = self.document_generation.get_icd_scope_review(ctx, work_order_id)
        retriever_kwargs = {
            "source_set_snapshot_id": snapshot.source_set_snapshot_id,
        }
        if scope_review is not None:
            retriever_kwargs["icd_scope_review"] = scope_review
        retriever_kwargs.update(self._document_retriever_scope_kwargs(order))
        retrieve = self._knowledge_base_retriever(
            ctx,
            knowledge_base_name,
            list(snapshot.source_names),
            **retriever_kwargs,
        )
        return self.document_generation.run_internal_harness(
            ctx,
            work_order_id,
            retrieve=retrieve,
        )

    def continue_knowledge_base_document_generation(
        self,
        ctx: RequestContext,
        work_order_id: str,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ):
        """Run an existing KB work order after its frozen ICD scope is resolved."""
        order = self.document_generation.store.get_work_order(work_order_id)
        if order is None:
            raise ValueError("document work order was not found")
        self.document_generation.require_work_order_capability(
            ctx, order, "view_project",
        )
        if order.scope_type != "knowledge_base" or not order.knowledge_base_name:
            raise ValueError("work order is not a knowledge-base document generation")
        snapshot = self.document_generation.resolve_source_snapshot(order)
        scope_review = self.document_generation.get_icd_scope_review(ctx, work_order_id)
        retriever_kwargs = {
            "source_set_snapshot_id": snapshot.source_set_snapshot_id,
            "icd_scope_review": scope_review,
        }
        retriever_kwargs.update(self._document_retriever_scope_kwargs(order))
        retrieve = self._knowledge_base_retriever(
            ctx,
            order.knowledge_base_name,
            list(snapshot.source_names),
            **retriever_kwargs,
        )
        run_kwargs: dict[str, Any] = {"retrieve": retrieve}
        if should_cancel is not None:
            run_kwargs["should_cancel"] = should_cancel
        return self.document_generation.run_internal_harness(
            ctx, work_order_id, **run_kwargs,
        )

    def submit_knowledge_base_document_generation(
        self,
        ctx: RequestContext,
        work_order_id: str,
    ) -> str:
        """Queue an existing KB work order on the durable authoring worker.

        The old in-process ``DocumentGenerationWorker`` remains a compatibility
        path for small legacy callers that construct an ``AppPipeline`` without
        the shared job store.  A production pipeline always has
        ``document_job_store`` and therefore never serializes a request-bound
        closure or authentication context into a background queue.
        """
        job_store = getattr(self, "document_job_store", None)
        if callable(getattr(job_store, "create_job", None)):
            order = self.document_generation.store.get_work_order(work_order_id)
            if order is None:
                raise KeyError("document work order was not found")
            self.document_generation.require_work_order_capability(
                ctx, order, "run_deterministic_work_order",
            )
            if order.scope_type != "knowledge_base" or not order.knowledge_base_name:
                raise ValueError("work order is not a knowledge-base document generation")
            if not ctx.has_kb_permission(order.knowledge_base_name, "write"):
                raise PermissionError("knowledge base write permission is required")
            job = job_store.create_job(
                tenant_id=ctx.tenant_id or "default",
                user_id=ctx.user_id,
                session_id=ctx.session_id or f"document-generation:{work_order_id}",
                client_request_id=f"work-order:{work_order_id}:generate",
                operation="generate_work_order",
                work_order_id=work_order_id,
                task_id=getattr(order, "task_id", None),
                payload={
                    "work_order_id": work_order_id,
                    "knowledge_base_name": order.knowledge_base_name,
                },
            )
            mark_task_queued = getattr(self.document_generation, "mark_document_task_queued", None)
            if callable(mark_task_queued):
                mark_task_queued(getattr(order, "task_id", None))
            return job.job_id
        return self.document_generation.worker.submit(
            work_order_id,
            lambda: self.continue_knowledge_base_document_generation(ctx, work_order_id),
        )

    def submit_document_artifact_conversion(
        self,
        ctx: RequestContext,
        artifact_id: str,
        *,
        target_format: str,
        session_id: str | int | None = None,
        client_request_id: str | None = None,
    ):
        """Queue an immutable template-artifact conversion.

        Only the artifact id and requested format are serialized.  The worker
        re-reads the parent artifact and re-checks the work-order capability
        before conversion, so a permission revocation or source replacement
        cannot turn a stale browser request into a downloadable file.
        """
        job_store = getattr(self, "document_job_store", None)
        if not callable(getattr(job_store, "create_job", None)):
            raise RuntimeError("durable document authoring job store is not configured")
        artifact = self.document_generation.store.get_artifact(str(artifact_id))
        if artifact is None:
            raise KeyError("artifact not found")
        order = self.document_generation.store.get_work_order(artifact.work_order_id)
        if order is None:
            raise KeyError("work order not found")
        self.document_generation.require_work_order_capability(
            ctx, order, "run_deterministic_work_order",
        )
        if order.scope_type != "knowledge_base" or not order.knowledge_base_name:
            raise ValueError("artifact conversion requires a knowledge-base work order")
        if not ctx.has_kb_permission(order.knowledge_base_name, "write"):
            raise PermissionError("knowledge base write permission is required")
        normalized_target = str(target_format or "").strip().lower().lstrip(".")
        if normalized_target not in {"pdf", "pptx"}:
            raise ValueError("document artifact conversion supports only pdf or pptx")
        job = job_store.create_job(
            tenant_id=ctx.tenant_id or "default",
            user_id=ctx.user_id,
            session_id=session_id or ctx.session_id or f"document-generation:{order.work_order_id}",
            client_request_id=client_request_id or f"artifact-conversion:{artifact.artifact_id}:{normalized_target}",
            operation="convert_artifact",
            work_order_id=order.work_order_id,
            task_id=getattr(order, "task_id", None),
            payload={
                "work_order_id": order.work_order_id,
                "knowledge_base_name": order.knowledge_base_name,
                "source_artifact_id": artifact.artifact_id,
                "target_format": normalized_target,
            },
        )
        return job

    def resume_knowledge_base_document_generation_run(
        self,
        ctx: RequestContext,
        work_order_id: str,
        harness_run_id: str,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ):
        """Resume one already-selected HarnessRun without queueing it again.

        This is the worker dispatch target for ``resume_work_order`` jobs.  It
        deliberately rebuilds the retriever from the frozen snapshot at
        execution time, so a browser/request process cannot smuggle a closure
        or a mutable retrieval scope through the job payload.
        """
        order = self.document_generation.store.get_work_order(work_order_id)
        if order is None:
            raise KeyError("document work order was not found")
        self.document_generation.require_work_order_capability(
            ctx, order, "run_deterministic_work_order",
        )
        if order.scope_type != "knowledge_base" or not order.knowledge_base_name:
            raise ValueError("work order is not a knowledge-base document generation")
        if not ctx.has_kb_permission(order.knowledge_base_name, "write"):
            raise PermissionError("knowledge base write permission is required")
        snapshot = self.document_generation.resolve_source_snapshot(order)
        scope_review = self.document_generation.get_icd_scope_review(ctx, work_order_id)
        retriever_kwargs = {
            "source_set_snapshot_id": snapshot.source_set_snapshot_id,
            "icd_scope_review": scope_review,
        }
        retriever_kwargs.update(self._document_retriever_scope_kwargs(order))
        retrieve = self._knowledge_base_retriever(
            ctx,
            order.knowledge_base_name,
            list(snapshot.source_names),
            **retriever_kwargs,
        )
        return self.document_generation.resume_internal_harness(
            ctx,
            harness_run_id,
            retrieve=retrieve,
            should_cancel=should_cancel,
        )

    def resume_knowledge_base_document_generation(
        self,
        ctx: RequestContext,
        work_order_id: str,
    ) -> str:
        """Resume the latest paused KB Harness run using frozen sources."""
        order = self.document_generation.store.get_work_order(work_order_id)
        if order is None:
            raise KeyError("document work order was not found")
        self.document_generation.require_work_order_capability(
            ctx, order, "run_deterministic_work_order",
        )
        if order.scope_type != "knowledge_base" or not order.knowledge_base_name:
            raise ValueError("work order is not a knowledge-base document generation")
        paused_runs = [
            run for run in self.document_generation.store.list_harness_runs(work_order_id)
            if run.status == "paused"
        ]
        if not paused_runs:
            raise ValueError("work order has no paused Harness run")
        paused_run = paused_runs[-1]
        job_store = getattr(self, "document_job_store", None)
        if callable(getattr(job_store, "create_job", None)):
            job = self._queue_document_authoring_resume(
                ctx,
                order,
                paused_run.harness_run_id,
                client_request_id=(
                    f"work-order:{work_order_id}:resume:{paused_run.harness_run_id}"
                ),
            )
            return job.job_id
        snapshot = self.document_generation.resolve_source_snapshot(order)
        scope_review = self.document_generation.get_icd_scope_review(ctx, work_order_id)
        retriever_kwargs = {
            "source_set_snapshot_id": snapshot.source_set_snapshot_id,
            "icd_scope_review": scope_review,
        }
        retriever_kwargs.update(self._document_retriever_scope_kwargs(order))
        retrieve = self._knowledge_base_retriever(
            ctx,
            order.knowledge_base_name,
            list(snapshot.source_names),
            **retriever_kwargs,
        )
        return self.document_generation.worker.submit(
            work_order_id,
            lambda: self.document_generation.resume_internal_harness(
                ctx,
                paused_run.harness_run_id,
                retrieve=retrieve,
            ),
        )

    def _queue_document_authoring_resume(
        self,
        ctx: RequestContext,
        order,
        harness_run_id: str,
        *,
        client_request_id: str,
    ):
        """Persist a restartable Harness resume request without request state."""
        job_store = getattr(self, "document_job_store", None)
        if not callable(getattr(job_store, "create_job", None)):
            raise RuntimeError("durable document authoring job store is not configured")
        if order.scope_type != "knowledge_base" or not order.knowledge_base_name:
            raise ValueError("document authoring resume requires a knowledge-base work order")
        if not ctx.has_kb_permission(order.knowledge_base_name, "write"):
            raise PermissionError("knowledge base write permission is required")
        job = job_store.create_job(
            tenant_id=ctx.tenant_id or "default",
            user_id=ctx.user_id,
            session_id=ctx.session_id or f"document-generation:{order.work_order_id}",
            client_request_id=str(client_request_id),
            operation="resume_work_order",
            work_order_id=order.work_order_id,
            task_id=getattr(order, "task_id", None),
            payload={
                "work_order_id": order.work_order_id,
                "knowledge_base_name": order.knowledge_base_name,
                "harness_run_id": str(harness_run_id),
            },
        )
        mark_task_queued = getattr(self.document_generation, "mark_document_task_queued", None)
        if callable(mark_task_queued):
            mark_task_queued(getattr(order, "task_id", None))
        return job

    def delete_knowledge_base_document_work_order(
        self,
        ctx: RequestContext,
        work_order_id: str,
        *,
        reason: str = "",
    ):
        order = self.document_generation.store.get_work_order(work_order_id)
        if order is None:
            raise KeyError("document work order was not found")
        if order.scope_type != "knowledge_base" or not order.knowledge_base_name:
            raise ValueError("work order is not a knowledge-base document generation")
        return self.document_generation.delete_document_work_order(
            ctx,
            work_order_id,
            reason=reason,
        )

    def auto_generate_document(self, ctx: RequestContext, **kwargs):
        """Run a document using the frozen project source snapshot and return a candidate."""
        project_id = kwargs["project_id"]

        def retrieve_factory(order):
            return self._project_retriever(ctx, project_id, order.source_set_snapshot_id)

        return self.document_generation.auto_generate_document(
            ctx, retrieve_factory=retrieve_factory, **kwargs,
        )

    def _knowledge_base_retriever(
        self,
        ctx: RequestContext,
        kb_name: str,
        source_names: list[str],
        *,
        source_set_snapshot_id: str = "",
        icd_scope_review: Any | None = None,
        source_scope: str = "knowledge_base_only",
        attachment_refs: list[Any] | None = None,
        attachment_retrieval: Any | None = None,
    ):
        if not ctx.has_kb_permission(kb_name, "read"):
            raise PermissionError("knowledge base read permission is required")
        frozen_source_names = list(dict.fromkeys(source_names))
        frozen_attachment_refs = list(attachment_refs or [])
        effective_source_scope = resolve_source_scope(
            requested_scope=source_scope,
            has_attachments=bool(frozen_attachment_refs),
            is_general_chat=False,
        )
        attachment_ids = [
            str(
                ref.get("attachment_id", "")
                if isinstance(ref, dict)
                else getattr(ref, "attachment_id", "")
            ).strip()
            for ref in frozen_attachment_refs
        ]
        attachment_ids = list(dict.fromkeys(item for item in attachment_ids if item))
        kb_provider = KnowledgeBaseEvidenceProvider(
            self.backend,
            top_k=src.settings.FINAL_TOP_K,
            source_names=frozen_source_names,
        )
        attachment_provider = None
        if frozen_attachment_refs and effective_source_scope in {
            "attachment_only", "attachment_and_knowledge_base",
        }:
            attachment_provider = AttachmentEvidenceProvider(
                retrieval=attachment_retrieval or AttachmentRetrievalService(),
                refs_snapshot=frozen_attachment_refs,
                top_k=src.settings.FINAL_TOP_K,
            )
        evidence_providers = [kb_provider]
        if attachment_provider is not None:
            evidence_providers.append(attachment_provider)
        composite_provider = CompositeDocumentEvidenceProvider(evidence_providers)
        frozen_pin_evidence = self._frozen_icd_pin_evidence(
            kb_name,
            frozen_source_names,
            icd_scope_review,
        )

        # Spreadsheet structured index (xlsx TableIndexStore). Instantiated
        # once per retriever so multiple units reuse the same tool. The tool
        # does not honour source_names in `filters` (only record_id), so the
        # specialised retriever filters frozen-set membership itself before
        # merging; the downstream build_knowledge_base_retrieval_outcome
        # re-checks as a second guard.
        spreadsheet_tool = (
            SpreadsheetSemanticTool(self.spreadsheet_service)
            if self.spreadsheet_service is not None
            else None
        )

        # Circuit structured index (EDF CircuitStore). Same dispatch pattern
        # as the spreadsheet tool: the circuit query tool cannot filter by a
        # list of source names (only a single source_name), so the
        # specialised retriever filters frozen-set membership itself before
        # merging; build_knowledge_base_retrieval_outcome re-checks as a
        # second guard. getattr keeps object.__new__-built test doubles
        # without the attribute on the disabled path.
        circuit_service = getattr(self, "circuit_service", None)
        circuit_tool = (
            CircuitQueryTool(circuit_service)
            if circuit_service is not None
            else None
        )

        def default_retriever(query, requirement, *, balanced_route: bool = False):
            # Stage 5 adaptive recovery: balanced_route drops the source_group
            # hard filter so a mis-routed query can reach frozen sources. The
            # frozen source_names scope stays, so the result never widens
            # beyond the frozen source set.
            if balanced_route:
                balanced_kb_provider = KnowledgeBaseEvidenceProvider(
                    self.backend,
                    top_k=src.settings.FINAL_TOP_K,
                    source_names=frozen_source_names,
                    extra_filters={"balanced_route": True},
                )
                provider = CompositeDocumentEvidenceProvider(
                    [balanced_kb_provider]
                    + ([attachment_provider] if attachment_provider is not None else [])
                )
            else:
                provider = composite_provider
            return provider.retrieve(
                query=query,
                source_scope=effective_source_scope,
                attachment_ids=attachment_ids,
                kb_name=kb_name,
                ctx=ctx,
            )

        specialized: dict[str, Callable[[str, Any], list]] = {}
        if spreadsheet_tool is not None and scope_allows_kb(effective_source_scope):
            def _tabular_retriever(query, requirement):
                sp_evidences = spreadsheet_tool.run(
                    query,
                    kb_name,
                    ctx,
                    top_k=src.settings.FINAL_TOP_K,
                    filters=None,
                )
                # Drop anything outside the frozen source set before it reaches
                # the domain-binding step, which would otherwise raise
                # PermissionError and abort the whole run.
                return [
                    evidence
                    for evidence in sp_evidences
                    if evidence.source_name in frozen_source_names
                ]
            specialized["tabular_lookup"] = _tabular_retriever
        if circuit_tool is not None and scope_allows_kb(effective_source_scope):
            def _circuit_retriever(query, requirement):
                circuit_evidences = circuit_tool.run(
                    query,
                    kb_name,
                    ctx,
                    top_k=src.settings.FINAL_TOP_K,
                    filters=None,
                )
                # Same frozen-set guard as the tabular retriever: circuit
                # evidence carries the design's original file name as
                # source_name; anything outside the frozen snapshot is
                # dropped before the domain-binding step, which would
                # otherwise raise PermissionError and abort the whole run.
                return [
                    evidence
                    for evidence in circuit_evidences
                    if evidence.source_name in frozen_source_names
                ]
            specialized["entity_lookup"] = _circuit_retriever
        if (circuit_tool is not None or frozen_pin_evidence) and scope_allows_kb(effective_source_scope):
            def _relationship_retriever(query, requirement):
                circuit_evidences = (
                    _circuit_retriever(query, requirement)
                    if circuit_tool is not None
                    else []
                )
                return [*circuit_evidences, *frozen_pin_evidence]
            specialized["relationship_lookup"] = _relationship_retriever

        # The registry generalises the Stage 0 tabular_lookup dispatch: the
        # default (RAGFlow) retriever is always invoked, specialised retrievers
        # are additively invoked per declared capability, evidence is deduplicated
        # by content hash, preferred_source_roles are boosted (P7), and a
        # cross-unit cache reuses evidence on empty retrieval (P8). Closure-
        # internal, no policy gating (same precedent as Stage 0).
        registry = RetrieverRegistry(
            default_retriever=default_retriever,
            specialized=specialized,
            cross_unit_cache=CrossUnitEvidenceCache(),
        )

        def retrieve(requirement, _attempt, query_override=None, *, relaxed: bool = False):
            query = query_override or " ".join(requirement.retrieval_query_terms) or " ".join(
                value for value in (requirement.subject, requirement.predicate, requirement.object_hint) if value
            )
            evidences = registry.retrieve(requirement, query, balanced_route=relaxed)
            return self.document_generation.build_knowledge_base_retrieval_outcome(
                kb_name,
                frozen_source_names,
                evidences,
                requirement_id=requirement.requirement_id,
                source_set_snapshot_id=source_set_snapshot_id,
                attachment_ids=attachment_ids,
            )

        return retrieve

    def _schema_has_relationship_lookup(self, order: Any) -> bool:
        schema_id = getattr(order, "document_schema_id", "")
        schema_version = getattr(order, "document_schema_version", "")
        if not schema_id or not schema_version:
            return False
        schema = self.document_generation._schema(
            schema_id,
            schema_version,
        )
        return any(
            "relationship_lookup" in enrich_circuit_capabilities(
                field.required_capabilities,
                label=field.label,
                description=field.description,
                query_terms=field.query_terms,
            )
            for field in schema.fields
        )

    def _icd_connector_scope_schema(self, order: Any) -> Any | None:
        """Return explicit connector candidates for an ICD pin table only.

        A generic relationship field may need circuit evidence, but it must
        never turn every component in an EDF into an ICD review item.  The
        authoring schema is the stable, project-independent place to declare
        connector/refdes constraints (labels, aliases, query terms or value
        schema).  Without one, normal retrieval remains available and no
        unbounded pin scan is attempted.
        """
        schema_id = getattr(order, "document_schema_id", "")
        schema_version = getattr(order, "document_schema_version", "")
        if not schema_id or not schema_version:
            return None
        schema = self.document_generation._schema(schema_id, schema_version)
        if str(getattr(schema, "document_type", "")).casefold() != "icd":
            return None
        return schema if _schema_has_icd_pin_field(schema) else None

    def _icd_front_view_connector_refdes(self, order: Any) -> list[str]:
        """Use only explicit ICD front-view slots from the frozen template."""
        template_version_id = str(getattr(order, "template_version_id", "")).strip()
        if not template_version_id:
            return []
        try:
            content = self.document_generation.store.read_template_content(template_version_id)
        except (KeyError, OSError, ValueError):
            return []
        return connector_refdes_from_front_view_template(content)

    def _icd_template_profile(self, order: Any):
        """Classify the immutable template before an ICD run uses any evidence."""
        template_version_id = str(getattr(order, "template_version_id", "")).strip()
        if not template_version_id:
            return None
        try:
            content = self.document_generation.store.read_template_content(template_version_id)
        except (KeyError, OSError, ValueError):
            return None
        return classify_icd_template(
            content,
            str(getattr(order, "target_format", "xlsx")),
        )

    @staticmethod
    def _frozen_icd_pin_evidence(
        kb_name: str,
        source_names: list[str],
        review: Any | None,
    ) -> list[Evidence]:
        mappings = effective_frozen_pin_mappings(review)
        if not mappings or not source_names:
            return []
        normalized_mappings = []
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            refdes = str(mapping.get("refdes") or "").strip()
            pin_name = str(mapping.get("pin_name") or "").strip()
            if not (refdes and pin_name):
                continue
            normalized = {
                "refdes": refdes,
                "pin_name": pin_name,
                "net_name": str(mapping.get("net_name") or "").strip() or "NC",
            }
            source_name = str(mapping.get("source_name") or "").strip()
            if source_name:
                normalized["source_name"] = source_name
            normalized_mappings.append(normalized)
        if not normalized_mappings:
            return []
        grouped: dict[str, list[dict[str, str]]] = {}
        fallback_source_name = source_names[0]
        for mapping in normalized_mappings:
            source_name = mapping.get("source_name") or fallback_source_name
            if source_name not in source_names:
                continue
            grouped.setdefault(source_name, []).append(mapping)
        frozen: list[Evidence] = []
        for source_name, mappings_for_source in grouped.items():
            pin_text = "; ".join(
                f"{mapping['refdes']}-{mapping['pin_name']} -> {mapping['net_name']}"
                for mapping in mappings_for_source
            )
            frozen.append(Evidence(
                id="frozen-icd-pin-set:" + source_name + ":" + "|".join(
                    f"{mapping['refdes']}:{mapping['pin_name']}"
                    for mapping in mappings_for_source
                ),
                content=f"Frozen ICD pin mappings: {pin_text}.",
                source_name=source_name,
                content_kind="circuit_design",
                processor_kind="icd_scope_review",
                score=1.0,
                metadata={
                    "kb_name": kb_name,
                    "source_group": "circuit_design",
                    "pin_mappings": mappings_for_source,
                    "frozen_icd_scope": True,
                },
            ))
        return frozen

    @staticmethod
    def _document_retriever_scope_kwargs(order: Any) -> dict[str, Any]:
        """Return only persisted source-scope inputs for retriever rebuilds."""
        kwargs: dict[str, Any] = {}
        raw_scope = getattr(order, "source_scope_snapshot", "")
        source_scope = raw_scope.strip() if isinstance(raw_scope, str) else ""
        raw_refs = getattr(order, "attachment_refs_snapshot", None)
        attachment_refs = list(raw_refs) if isinstance(raw_refs, (list, tuple)) else []
        # Empty values identify pre-bridge work orders.  Omitting them keeps
        # compatibility with injected/legacy order objects and lets the
        # retriever use its knowledge-base-only default.
        if source_scope:
            kwargs["source_scope"] = source_scope
        if attachment_refs:
            kwargs["attachment_refs"] = attachment_refs
        return kwargs

    def _project_retriever(self, ctx: RequestContext, project_id: str, snapshot_id: str):
        tenant_id = ctx.tenant_id or "default"
        bindings = self.projects.store.list_knowledge_bindings(project_id, tenant_id)
        fallback_kb_names = [binding.kb_name_snapshot for binding in bindings if binding.kb_name_snapshot]
        # Cross-unit evidence reuse cache (P8): persists across units within one
        # run (the closure is reused per unit). Offer is only consulted when a
        # unit's fresh retrieval is empty, so reuse never adds noise to a hit.
        cross_unit_cache = CrossUnitEvidenceCache()

        def retrieve(requirement, _attempt, query_override=None, *, relaxed: bool = False):
            # Stage 5 adaptive recovery: relaxed drops the source_group hard
            # filter (balanced_route) on each version's RAGFlow retrieve. The
            # per-version source_names scope ([document.title]) stays, so the
            # result never widens beyond the frozen source set.
            balanced = relaxed

            def retrieve_one(version_id: str, artifact_ids: list[str], _region_policies: dict[str, str]):
                version = self.projects.store.get_source_version(version_id, tenant_id)
                if version is None:
                    raise SourceUnavailableError(f"source version not found: {version_id}")
                document = self.projects.store.get_logical_document(version.document_id, tenant_id)
                if document is None:
                    raise SourceUnavailableError(f"logical document not found: {version.document_id}")
                source_kb_name = str(document.metadata.get("kb_name") or "").strip()
                kb_names = [source_kb_name] if source_kb_name else list(fallback_kb_names)
                kb_names = [name for name in kb_names if name]
                if not kb_names:
                    raise SourceUnavailableError(f"knowledge base is not configured for source: {version_id}")
                query = query_override or " ".join(requirement.retrieval_query_terms) or " ".join(
                    value for value in (requirement.subject, requirement.predicate, requirement.object_hint) if value
                )
                result: list[EvidenceEnvelope] = []
                for kb_name in dict.fromkeys(kb_names):
                    version_filters = {"source_names": [document.title]}
                    if balanced:
                        version_filters["balanced_route"] = True
                    evidences = self.backend.retrieve(
                        kb_name,
                        query,
                        top_k=src.settings.FINAL_TOP_K,
                        ctx=ctx,
                        filters=version_filters,
                    )
                    for evidence in evidences:
                        result.append(EvidenceEnvelope(
                            id=evidence.id,
                            content=evidence.content,
                            source_name=evidence.source_name,
                            source_type=evidence.source_type,
                            score=evidence.score,
                            metadata=dict(evidence.metadata),
                            backend=evidence.backend,
                            retriever=evidence.retriever,
                            project_id=project_id,
                            source_version_id=version_id,
                            processing_artifact_id=artifact_ids[0] if len(artifact_ids) == 1 else None,
                            document_role=document.document_role,
                            revision=version.revision,
                            approval_status=version.approval_status,
                        ))
                # Capability-aware spreadsheet dispatch (closes the project-path
                # gap symmetrical to P1): tabular_lookup requirements also query
                # the spreadsheet structured index. The tool's `filters` only
                # honours record_id, so we filter by document.title (same scope
                # as the RAGFlow source_names filter) and bind each hit to the
                # current version + artifact so it passes the per-version scope
                # validation in ProjectEvidenceRetrievalService.retrieve.
                if (
                    self.spreadsheet_service is not None
                    and "tabular_lookup" in (requirement.required_capabilities or [])
                ):
                    sp_evidences = SpreadsheetSemanticTool(self.spreadsheet_service).run(
                        query,
                        source_kb_name or (kb_names[0] if kb_names else ""),
                        ctx,
                        top_k=src.settings.FINAL_TOP_K,
                        filters=None,
                    )
                    for evidence in sp_evidences:
                        if evidence.source_name != document.title:
                            continue
                        result.append(EvidenceEnvelope(
                            id=evidence.id,
                            content=evidence.content,
                            source_name=evidence.source_name,
                            source_type=getattr(evidence, "source_type", "spreadsheet"),
                            score=evidence.score,
                            metadata=dict(evidence.metadata),
                            backend=getattr(evidence, "backend", "spreadsheet"),
                            retriever=getattr(evidence, "retriever", "spreadsheet_semantic"),
                            project_id=project_id,
                            source_version_id=version_id,
                            processing_artifact_id=artifact_ids[0] if len(artifact_ids) == 1 else None,
                            document_role=document.document_role,
                            revision=version.revision,
                            approval_status=version.approval_status,
                        ))
                # Capability-aware circuit dispatch (symmetrical to the
                # spreadsheet dispatch above and to P1's entity/relationship
                # retrievers): entity_lookup / relationship_lookup requirements
                # also query the circuit structured index. Same frozen-source
                # guard (by document.title) and per-version EvidenceEnvelope
                # binding so the result passes the scope validation in
                # ProjectEvidenceRetrievalService.retrieve.
                if (
                    getattr(self, "circuit_service", None) is not None
                    and any(
                        cap in (requirement.required_capabilities or [])
                        for cap in ("entity_lookup", "relationship_lookup")
                    )
                ):
                    circuit_tool = CircuitQueryTool(self.circuit_service)
                    circuit_evidences = circuit_tool.run(
                        query,
                        source_kb_name or (kb_names[0] if kb_names else ""),
                        ctx,
                        top_k=src.settings.FINAL_TOP_K,
                        filters=None,
                    )
                    for evidence in circuit_evidences:
                        if evidence.source_name != document.title:
                            continue
                        result.append(EvidenceEnvelope(
                            id=evidence.id,
                            content=evidence.content,
                            source_name=evidence.source_name,
                            source_type=getattr(evidence, "source_type", "circuit_design"),
                            score=evidence.score,
                            metadata=dict(evidence.metadata),
                            backend=getattr(evidence, "backend", "circuit"),
                            retriever=getattr(evidence, "retriever", "circuit_query"),
                            project_id=project_id,
                            source_version_id=version_id,
                            processing_artifact_id=artifact_ids[0] if len(artifact_ids) == 1 else None,
                            document_role=document.document_role,
                            revision=version.revision,
                            approval_status=version.approval_status,
                        ))
                return result

            outcome = self.project_retrieval.retrieve(ctx, requirement, snapshot_id, retrieve_one)
            query = query_override or " ".join(requirement.retrieval_query_terms) or " ".join(
                value for value in (requirement.subject, requirement.predicate, requirement.object_hint) if value
            )
            # Post-process the validated evidence: dedup by content hash (P4
            # merge), boost preferred_source_roles (P7), and cross-unit reuse
            # on empty retrieval (P8). Only genuinely fresh evidence is ingested
            # so provenance is preserved; reused evidence is tagged for the
            # Stage 4 requirement_fit_check to scrutinise.
            fresh = dedup_by_content(list(outcome.evidences))
            fresh = apply_role_boost(fresh, requirement.preferred_source_roles)
            if fresh:
                outcome.evidences = fresh
                cross_unit_cache.ingest(fresh, requirement.semantic_unit_id)
            else:
                reused = cross_unit_cache.offer(requirement, query, requirement.semantic_unit_id)
                if reused:
                    outcome.evidences = reused
                    # Upgraded from success_empty: reused evidence makes the
                    # unit answerable (low-confidence, routed to human review).
                    outcome.status = "success_with_hits"
                else:
                    outcome.evidences = []
            return outcome

        return retrieve

    def start_document_generation(self, ctx: RequestContext, work_order_id: str, *, rule_inputs, retrieval_outcomes):
        return self.document_generation.start_document_generation(
            ctx, work_order_id, rule_inputs=rule_inputs, retrieval_outcomes=retrieval_outcomes,
        )

    def run_internal_document_harness(self, ctx: RequestContext, work_order_id: str, *, retrieve, writer=None):
        return self.document_generation.run_internal_harness(
            ctx,
            work_order_id,
            retrieve=retrieve,
            writer=writer,
        )

    def resume_internal_document_harness(self, ctx: RequestContext, harness_run_id: str, *, retrieve, writer=None):
        return self.document_generation.resume_internal_harness(
            ctx,
            harness_run_id,
            retrieve=retrieve,
            writer=writer,
        )

    def pause_harness_run(self, ctx: RequestContext, harness_run_id: str):
        return self.document_generation.pause_harness_run(ctx, harness_run_id)

    def cancel_harness_run(self, ctx: RequestContext, harness_run_id: str):
        return self.document_generation.cancel_harness_run(ctx, harness_run_id)

    def resolve_knowledge_base_harness_human_decision(
        self,
        ctx: RequestContext,
        harness_run_id: str,
        *,
        pending_event_id: str,
        proposal_hash: str,
        decision: str,
    ):
        """Apply an agent proposal decision and resume the same KB run."""
        run = self.document_generation.store.get_harness_run(harness_run_id)
        if run is None:
            raise KeyError("harness run not found")
        order = self.document_generation.store.get_work_order(run.work_order_id)
        if order is None or order.scope_type != "knowledge_base" or not order.knowledge_base_name:
            raise ValueError("human agent decisions require a knowledge-base work order")
        self.document_generation.require_work_order_capability(ctx, order, "run_deterministic_work_order")
        job_store = getattr(self, "document_job_store", None)
        if callable(getattr(job_store, "create_job", None)):
            # The request process only consumes the atomic decision transition.
            # Approval is resumed by the durable worker, which rebuilds the
            # retriever and authorization context after a restart.
            result = self.document_generation.resolve_agent_human_decision(
                ctx,
                harness_run_id,
                pending_event_id=pending_event_id,
                proposal_hash=proposal_hash,
                decision=decision,
                retrieve=None,
            )
            if result.get("decision") == "approve":
                job = self._queue_document_authoring_resume(
                    ctx,
                    order,
                    harness_run_id,
                    client_request_id=f"human-decision:{result['decision_key']}",
                )
                result["job_id"] = job.job_id
                result["job_status"] = job.status
                result["next_actions"] = ["poll_status"]
            return result
        snapshot = self.document_generation.resolve_source_snapshot(order)
        scope_review = self.document_generation.get_icd_scope_review(ctx, order.work_order_id)
        retriever_kwargs = {
            "source_set_snapshot_id": snapshot.source_set_snapshot_id,
            "icd_scope_review": scope_review,
        }
        retriever_kwargs.update(self._document_retriever_scope_kwargs(order))
        retrieve = self._knowledge_base_retriever(
            ctx,
            order.knowledge_base_name,
            list(snapshot.source_names),
            **retriever_kwargs,
        )
        return self.document_generation.resolve_agent_human_decision(
            ctx,
            harness_run_id,
            pending_event_id=pending_event_id,
            proposal_hash=proposal_hash,
            decision=decision,
            retrieve=retrieve,
        )

    @staticmethod
    def _coverage_bucket(status: str) -> str:
        """Map one execution unit status to the coverage panel bucket.

        The panel speaks the user's language (已完成/缺证据/冲突), so the
        bucket mapping is the single place where internal unit-status strings
        become user-facing aggregates.
        """
        return {
            "ready_to_render": "covered",
            "passed": "covered",
            "tbd": "missing",
            "insufficient_evidence": "missing",
            "conflicting": "conflicting",
            "retrieval_failed": "failed",
            "failed": "failed",
            "blocked": "failed",
        }.get(str(status or "").strip(), "pending")

    def _document_coverage_block(self, order) -> dict[str, Any]:
        """Per-field coverage projection for the workbench coverage panel.

        Combines three stored facts -- frozen schema labels, execution unit
        statuses and the persisted evidence matrix. Every source degrades
        independently so a partial read never hides the panel; the execution
        statuses alone are always sufficient to render it.
        """
        store = self.document_generation.store
        schema = None
        try:
            schema = store.get_document_schema(order.document_schema_id, order.document_schema_version)
        except Exception:
            schema = None
        matrix_rows: list[Any] = []
        try:
            matrix_rows = store.get_evidence_matrix(order.work_order_id) or []
        except Exception:
            matrix_rows = []
        row_by_unit: dict[str, dict[str, Any]] = {}
        for row in matrix_rows:
            if not isinstance(row, dict):
                continue
            for key in ("field_id", "review_item_id", "semantic_unit_id"):
                unit_id = str(row.get(key) or "").strip()
                if unit_id:
                    row_by_unit.setdefault(unit_id, row)
                    break
        unit_statuses = dict(getattr(order, "unit_statuses", {}) or {})

        entries: list[dict[str, Any]] = []
        summary = {"covered": 0, "missing": 0, "conflicting": 0, "failed": 0, "pending": 0}

        def _entry(kind: str, unit_id: str, label: str, required: bool) -> dict[str, Any]:
            unit_key = f"{kind}:{unit_id}"
            status_value = (
                unit_statuses.get(unit_key)
                or unit_statuses.get(unit_id)
                or "planned"
            )
            row = row_by_unit.get(unit_id) or {}
            evidence_ids = row.get("evidence_ids") or []
            display_value = row.get("display_value")
            return {
                "kind": kind,
                "field_id": unit_id,
                "unit_id": unit_key,
                "label": label or unit_id,
                "required": required,
                "status": status_value,
                "coverage_status": str(row.get("coverage_status") or "") or None,
                "display_value": str(display_value) if display_value not in (None, "") else None,
                "evidence_count": len(evidence_ids) if isinstance(evidence_ids, list) else 0,
            }

        schema_unit_ids: set[str] = set()
        if schema is not None:
            for field in schema.fields:
                schema_unit_ids.add(field.field_id)
                entries.append(_entry("field", field.field_id, field.label, bool(field.required)))
            for item in schema.review_items:
                schema_unit_ids.add(item.review_item_id)
                entries.append(_entry("review", item.review_item_id, item.label, False))
        for key, _status_value in unit_statuses.items():
            kind = "review" if key.startswith("review:") else "field"
            unit_id = key.removeprefix("field:").removeprefix("review:")
            if unit_id in schema_unit_ids:
                continue
            schema_unit_ids.add(unit_id)
            entries.append(_entry(kind, unit_id, unit_id, False))
        for entry in entries:
            summary[self._coverage_bucket(entry["status"])] += 1
        return {"fields": entries, "summary": summary, "total": len(entries)}

    def get_document_run_status(self, work_order_id: str, ctx: RequestContext | None = None):
        order = self.document_generation.store.get_work_order(work_order_id)
        if order is None:
            return None
        if ctx is None and order.scope_type == "knowledge_base":
            raise PermissionError(
                "request context is required for knowledge base work order status"
            )
        if ctx is not None:
            self.document_generation.require_work_order_capability(
                ctx,
                order,
                "view_project",
            )
        task_projection = None
        task_service = getattr(self.document_generation, "task_service", None)
        if getattr(src.settings, "DOCUMENT_TASK_READ_ENABLED", True):
            if getattr(order, "task_id", None) and getattr(task_service, "store", None) is not None:
                try:
                    task_projection = task_service.store.get(order.task_id)
                except Exception:
                    # Task projection is an additive read model; a transient
                    # read failure must not hide the already-authorized WorkOrder.
                    task_projection = None
            elif callable(getattr(task_service, "legacy_view_for_work_order", None)):
                task_projection = task_service.legacy_view_for_work_order(order)
        status = {
            "work_order_id": order.work_order_id,
            "task_id": (
                getattr(order, "task_id", None)
                or getattr(task_projection, "task_id", None)
            ) if getattr(src.settings, "DOCUMENT_TASK_READ_ENABLED", True) else None,
            "status": order.status,
            "phase": {
                "waiting_human_input": "needs_review",
                "waiting_human_approval": "needs_review",
                "ready_to_draft": "generating",
                "drafting": "generating",
                "complete": "completed",
            }.get(order.status, order.status),
            "scope_type": order.scope_type,
            "knowledge_base_name": order.knowledge_base_name,
            "project_id": order.project_id,
            "target_format": order.target_format,
            "unit_statuses": dict(order.unit_statuses),
            "coverage": self._document_coverage_block(order),
            "validation_report_id": order.validation_report_id,
            "clarification_session_id": getattr(order, "generation_session_id", None),
            "generation_brief": dict(getattr(order, "generation_brief", {}) or {}),
            "error_code": getattr(order, "error_code", None),
            "error_message": getattr(order, "error_message", None),
            "retryable": getattr(order, "retryable", None),
            "next_actions": list(getattr(order, "next_actions", []) or []),
        }
        if task_projection is not None:
            status["task"] = task_projection.model_dump(mode="json")
        if order.run_manifest_id:
            status["run_manifest_id"] = order.run_manifest_id
        harness_runs = self.document_generation.store.list_harness_runs(order.work_order_id)
        latest_run = None
        if harness_runs:
            latest_run = harness_runs[-1]
            status["harness_run"] = {
                "run_id": latest_run.harness_run_id,
                "status": latest_run.status,
                "current_node": latest_run.current_node,
                "step_count": latest_run.step_count,
                "retrieval_round_count": latest_run.retrieval_round_count,
                "completed_units": latest_run.completed_units,
                "total_units": latest_run.total_units,
                "retry_count": latest_run.retry_count,
                "checkpoint_id": latest_run.checkpoint_id,
                "fencing_token": latest_run.fencing_token,
                "error": latest_run.error,
                "requested_executor": getattr(latest_run, "requested_executor", None),
                "effective_executor": getattr(latest_run, "effective_executor", None),
                "degraded_reasons": list(getattr(latest_run, "degraded_reasons", []) or []),
                "agent_thread_id": getattr(latest_run, "agent_thread_id", None),
                "pending_human_event": getattr(latest_run, "pending_human_event", None),
            }
        has_kb_permission = getattr(ctx, "has_kb_permission", None)
        can_write = bool(
            ctx is not None
            and order.scope_type == "knowledge_base"
            and order.knowledge_base_name
            and callable(has_kb_permission)
            and has_kb_permission(order.knowledge_base_name, "write")
        )
        active = latest_run is not None and latest_run.status in {"queued", "running", "retrying"}
        paused = order.status == "paused" and latest_run is not None and latest_run.status == "paused"
        terminal = order.status in {"cancelled", "blocked", "failed", "complete"}
        status.update({
            "can_pause": active and can_write,
            "can_resume": paused and can_write,
            "can_cancel": (active or paused) and can_write,
            "can_delete": terminal and can_write,
        })
        job_store = getattr(self, "document_job_store", None)
        get_job = getattr(job_store, "get_by_work_order", None)
        if callable(get_job):
            job = get_job(
                order.work_order_id,
                tenant_id=(ctx.tenant_id if ctx is not None else None),
                user_id=(ctx.user_id if ctx is not None else None),
            )
            if job is not None:
                job_status = {
                    "job_id": job.job_id,
                    "operation": job.operation,
                    "status": job.status,
                    "attempt": job.attempt,
                    "last_error": job.last_error,
                    "result": dict(job.result or {}),
                }
                if getattr(src.settings, "DOCUMENT_TASK_READ_ENABLED", True):
                    job_status["task_id"] = getattr(job, "task_id", None)
                status["job"] = job_status
        if order.validation_report_id:
            report = self.document_generation.store.get_validation_report(order.validation_report_id)
            if report is not None:
                status["validation"] = {"status": report.status, "issues": list(report.issues)}
        status["artifacts"] = [
            {
                "artifact_id": artifact.artifact_id,
                "stage": artifact.stage,
                "output_format": artifact.output_format or order.target_format,
                "parent_artifact_id": artifact.parent_artifact_id,
                "validation_report_id": artifact.validation_report_id,
                "validity_status": artifact.validity_status,
                "policy_status": artifact.policy_status,
            }
            for artifact in self.document_generation.store.list_artifacts(order.work_order_id)
        ]
        return status

    def get_document_task_projection(
        self,
        ctx: RequestContext,
        task_id: str,
    ) -> dict[str, Any] | None:
        """Return the user-facing aggregate projection for one DocumentTask.

        DocumentTask owns cross-entry-point identity; WorkOrder/Run/Artifact
        remain the execution stores.  This method composes their read models
        only after rechecking tenant, user and knowledge-base scope.
        """
        if not getattr(src.settings, "DOCUMENT_TASK_READ_ENABLED", True):
            return None
        task_service = getattr(self.document_generation, "task_service", None)
        task_store = getattr(task_service, "store", None)
        if task_store is None:
            return None
        task = task_store.get(str(task_id))
        if task is None:
            return None
        tenant_id = str(getattr(ctx, "tenant_id", None) or "default")
        user_id = str(getattr(ctx, "user_id", None) or "")
        if task.tenant_id != tenant_id or task.user_id != user_id:
            raise PermissionError("document task is outside the current owner scope")
        kb_name = str(task.knowledge_base_name or "").strip()
        requested_kb = str(getattr(ctx, "metadata", {}).get("document_template_kb_name") or "").strip()
        if requested_kb and kb_name and requested_kb != kb_name:
            raise PermissionError("document task belongs to another knowledge base")
        if kb_name and not ctx.has_kb_permission(kb_name, "read"):
            raise PermissionError("knowledge base read permission is required")

        session_projection = self._document_clarification_projection(ctx, task)
        order = None
        status: dict[str, Any] = {}
        if task.work_order_id:
            order = self.document_generation.store.get_work_order(task.work_order_id)
            if order is not None:
                if getattr(order, "task_id", None) not in {None, task.task_id}:
                    raise ValueError("document task/work-order association mismatch")
                status_reader = getattr(self, "get_document_run_status", None)
                if callable(status_reader):
                    status = status_reader(order.work_order_id, ctx) or {}

        artifacts: list[dict[str, Any]] = []
        if order is not None:
            list_artifacts = getattr(self.document_generation.store, "list_artifacts", None)
            if callable(list_artifacts):
                for artifact in list_artifacts(order.work_order_id) or []:
                    artifacts.append(self._safe_document_task_artifact(artifact, order))

        reviews = self._document_reviews_for_task(ctx, task, order)
        revisions: list[dict[str, Any]] = []
        revision_service = getattr(self.document_generation, "revision_service", None)
        if revision_service is not None:
            try:
                revisions = [
                    item.model_dump(mode="json")
                    for item in revision_service.list_revisions(ctx, task.task_id)
                ]
            except (PermissionError, KeyError, ValueError):
                revisions = []

        next_actions = list(status.get("next_actions") or [])
        if not next_actions:
            next_actions = self._document_task_next_actions(task, session_projection, order)
        # Phase 1 v2 lifecycle projection: the internal pre-proposal state
        # projects as a user-visible draft; confirmation and submission states
        # surface their own actionable next steps.
        projected_status = task.status
        is_v2_task = bool(task.output_spec_id or task.document_plan_id) or (
            (session_projection or {}).get("contract_version") == "output_spec_v1"
        )
        if is_v2_task and order is None:
            session_status = (session_projection or {}).get("status")
            if session_status == "awaiting_plan" or (
                session_status is None and task.status == "needs_clarification"
            ):
                projected_status = "draft"
                next_actions = ["answer_clarification", "propose_document_plan"]
            elif task.status == "awaiting_plan_confirmation":
                next_actions = ["propose_document_plan", "confirm_document_plan"]
            elif task.status == "planned":
                next_actions = ["await_generation", "get_document_task_status"]

        planning_state = None
        submission_state = None
        spec_ref = (
            str(task.output_spec_id or "").strip(),
            int(task.output_spec_version or 0),
        )
        plan_ref = (
            str(task.document_plan_id or "").strip(),
            int(task.document_plan_version or 0),
        )
        if not spec_ref[0] or not plan_ref[0]:
            # Proposal binding may live on the owning session while the task
            # projection is refreshed; fall back to the session pointers.
            sessions = getattr(
                getattr(self.document_generation, "store", None),
                "generation_sessions",
                None,
            )
            session_id = str(getattr(task, "generation_session_id", "") or "").strip()
            getter = getattr(sessions, "get_session", None)
            if session_id and callable(getter):
                try:
                    bound = getter(
                        session_id,
                        tenant_id=str(getattr(ctx, "tenant_id", None) or "default"),
                        user_id=str(getattr(ctx, "user_id", None) or ""),
                    )
                except (KeyError, PermissionError, ValueError):
                    bound = None
                if bound is not None:
                    spec_ref = (
                        spec_ref[0] or str(getattr(bound, "output_spec_id", "") or "").strip(),
                        spec_ref[1] or int(getattr(bound, "output_spec_version", 0) or 0),
                    )
                    plan_ref = (
                        plan_ref[0] or str(getattr(bound, "document_plan_id", "") or "").strip(),
                        plan_ref[1] or int(getattr(bound, "document_plan_version", 0) or 0),
                    )
        if is_v2_task and spec_ref[0] and plan_ref[0]:
            try:
                planning_state, submission_state = self._document_planning_projection(
                    ctx,
                    task,
                    spec_ref=spec_ref,
                    plan_ref=plan_ref,
                )
            except Exception as exc:  # noqa: BLE001 - additive/fail-soft
                warn(f"failed to project planning state for task {task.task_id}: {exc}")
        pending_review = status.get("pending_human_event")
        if pending_review is None:
            harness = status.get("harness_run") or {}
            pending_review = harness.get("pending_human_event")
        if pending_review is None:
            pending_review = next(
                (
                    {
                        "review_id": review.get("review_id"),
                        "review_kind": review.get("review_kind"),
                        "status": review.get("status"),
                    }
                    for review in reviews
                    if review.get("status") in {"pending", "changes_requested"}
                ),
                None,
            )
        if pending_review is None:
            pending_review = next(
                (
                    {
                        "revision_id": revision.get("revision_id"),
                        "status": revision.get("status"),
                        "impact_scope": revision.get("impact_scope"),
                    }
                    for revision in revisions
                    if revision.get("status") in {"planned", "waiting_human", "revalidated", "revalidation_required"}
                ),
                None,
            )
        if pending_review and pending_review.get("revision_id"):
            next_actions = ["review_revision", "open_document_workbench"]
        elif pending_review and pending_review.get("review_kind") not in {None, "icd_scope"}:
            next_actions = ["review_document", "open_document_workbench"]
        projection = {
            "task_id": task.task_id,
            "origin": task.origin,
            "status": projected_status,
            "conversation_refs": {
                key: value
                for key, value in {
                    "conversation_id": task.conversation_id,
                    "initiating_turn_id": task.initiating_turn_id,
                }.items()
                if value is not None
            },
            "generation_session_id": task.generation_session_id,
            "work_order_id": task.work_order_id,
            "current_run_id": task.current_run_id,
            "current_artifact_id": task.current_artifact_id,
            "artifact_ids": list(task.artifact_ids),
            "knowledge_base_name": task.knowledge_base_name,
            "template_version_id": task.template_version_id,
            "project_id": task.project_id,
            "clarification_state": session_projection,
            "work_order": (
                {
                    "work_order_id": order.work_order_id,
                    "status": status.get("status", getattr(order, "status", None)),
                    "phase": status.get("phase"),
                    "target_format": getattr(order, "target_format", None),
                }
                if order is not None
                else None
            ),
            "run": status.get("harness_run"),
            "artifacts": artifacts,
            "reviews": reviews,
            "revisions": revisions,
            "pending_review": pending_review,
            "next_actions": next_actions,
            "planning_state": planning_state,
            "submission": submission_state,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }
        return projection

    def _document_planning_projection(
        self,
        ctx: RequestContext,
        task: Any,
        *,
        spec_ref: tuple[str, int],
        plan_ref: tuple[str, int],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Project safe planning identity for a v2 task (fail-soft caller)."""
        planning = getattr(self.document_generation, "planning", None)
        store = getattr(planning, "store", None)
        if store is None:
            return None, None
        tenant_id = str(getattr(ctx, "tenant_id", None) or "default")
        user_id = str(getattr(ctx, "user_id", None) or "")
        spec = store.get_output_spec(
            spec_ref[0],
            spec_ref[1],
            tenant_id=tenant_id,
            user_id=user_id,
        )
        plan = store.get_plan(
            plan_ref[0],
            plan_ref[1],
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if spec is None or plan is None:
            return None, None
        planning_state = {
            "output_spec_id": spec.output_spec_id,
            "output_spec_version": spec.version,
            "output_spec_hash": spec.content_hash,
            "document_plan_id": plan.document_plan_id,
            "document_plan_version": plan.version,
            "plan_hash": plan.plan_hash,
            "proposal_status": plan.status,
        }
        submission = store.submissions.get_by_plan(
            plan.document_plan_id,
            plan.version,
            plan.plan_hash,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        submission_state = (
            {
                "submission_id": submission.submission_id,
                "status": submission.status,
                "work_order_id": submission.work_order_id,
                "job_id": submission.job_id,
            }
            if submission is not None
            else None
        )
        return planning_state, submission_state

    def list_document_reviews(
        self,
        ctx: RequestContext,
        task_id: str,
    ) -> list[dict[str, Any]]:
        task = self._document_task_for_context(ctx, task_id)
        order = None
        if task.work_order_id:
            order = self.document_generation.store.get_work_order(task.work_order_id)
            if order is not None and getattr(order, "task_id", None) not in {None, task.task_id}:
                raise ValueError("document task/work-order association mismatch")
        return self._document_reviews_for_task(ctx, task, order)

    def get_document_review(
        self,
        ctx: RequestContext,
        review_id: str,
    ) -> dict[str, Any] | None:
        review_store = getattr(self.document_generation, "review_store", None)
        if review_store is None:
            return None
        review = review_store.get(review_id)
        if review is None:
            return None
        self._document_task_for_context(ctx, review.task_id)
        return review.model_dump(mode="json")

    def submit_document_review_decision(
        self,
        ctx: RequestContext,
        review_id: str,
        *,
        subject_hash: str,
        decision: Any,
        client_request_id: str,
        status: str | None = None,
        decision_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        review_store = getattr(self.document_generation, "review_store", None)
        if review_store is None:
            raise KeyError("document review not found")
        review = review_store.get(review_id)
        if review is None:
            raise KeyError("document review not found")
        self._document_task_for_context(ctx, review.task_id, required_permission="write")
        if review.review_kind == "icd_scope":
            raise ValueError("ICD scope reviews must use the dedicated scope resolution endpoint")
        requested_status = resolve_review_decision_status(decision, status)
        is_artifact_approval = (
            str(review.review_kind).startswith("artifact_approval:")
            and bool(review.artifact_id)
        )
        if is_artifact_approval and requested_status == "approved":
            if review.subject_hash != str(subject_hash or "").strip():
                raise ValueError("review subject hash does not match current subject")
            # Release is a side effect outside the review database transaction.
            # Preflight it while the review is still pending so a stale or
            # otherwise unreleasable candidate cannot become permanently
            # recorded as approved without a corresponding release Artifact.
            if review.decision is None and review.decided_at is None:
                self._release_approved_document_review(ctx, review)
        decided = review_store.submit_decision(
            review_id,
            subject_hash=subject_hash,
            decision=decision,
            client_request_id=client_request_id,
            status=status,
            decision_metadata=decision_metadata,
        )
        # Review state is authoritative for the review itself; the task
        # projection is advanced only for an approval and only from the
        # human-waiting state.  This makes retries harmless and prevents a
        # late review response from regressing a task that already finished.
        if decided.status == "approved":
            if is_artifact_approval:
                self._release_approved_document_review(ctx, review)
                revision_service = getattr(self.document_generation, "revision_service", None)
                if revision_service is not None:
                    try:
                        revision_service.mark_released(ctx, review.artifact_id)
                    except Exception:
                        # Release is already guarded and durable.  A revision
                        # status projection can be reconciled from the child
                        # release on the next task read.
                        warn(f"failed to close artifact revision for {review.artifact_id}")
                task = self._document_task_for_context(ctx, review.task_id)
                task_service = getattr(
                    getattr(self.document_generation, "task_service", None),
                    "store",
                    None,
                )
                if task.status != "completed" and task_service is not None:
                    try:
                        task_service.update_status(task.task_id, "completed")
                    except Exception:
                        warn(f"failed to project released artifact for task {task.task_id}")
                return decided.model_dump(mode="json")
            task_service = getattr(
                getattr(self.document_generation, "task_service", None),
                "store",
                None,
            )
            task = self._document_task_for_context(ctx, review.task_id)
            if task.status == "waiting_human" and task_service is not None:
                try:
                    task_service.update_status(task.task_id, "planned")
                except Exception:
                    warn(f"failed to project approved review for task {task.task_id}")
        return decided.model_dump(mode="json")

    def _release_approved_document_review(self, ctx: RequestContext, review: Any) -> Any:
        """Apply an approved generic artifact review to the guarded release path."""
        artifact_id = str(getattr(review, "artifact_id", None) or "").strip()
        if not artifact_id:
            raise ValueError("artifact review is missing its artifact identity")
        list_artifacts = getattr(self.document_generation.store, "list_artifacts", None)
        if callable(list_artifacts):
            for artifact in list_artifacts(getattr(review, "work_order_id", None)) or []:
                if (
                    getattr(artifact, "stage", None) == "approved_release"
                    and getattr(artifact, "parent_artifact_id", None) == artifact_id
                ):
                    return artifact
        approve = getattr(self.document_generation, "approve_document_artifact", None)
        if not callable(approve):
            raise ValueError("artifact approval service is unavailable")
        decision = getattr(review, "decision", None)
        comment = ""
        if isinstance(decision, dict):
            comment = str(decision.get("comment") or "").strip()
        return approve(ctx, artifact_id, comment=comment)

    def list_document_revisions(
        self,
        ctx: RequestContext,
        task_id: str,
    ) -> list[dict[str, Any]]:
        revision_service = getattr(self.document_generation, "revision_service", None)
        if revision_service is None:
            raise KeyError("document revision service is unavailable")
        return [
            item.model_dump(mode="json")
            for item in revision_service.list_revisions(ctx, task_id)
        ]

    def get_document_revision(
        self,
        ctx: RequestContext,
        revision_id: str,
    ) -> dict[str, Any] | None:
        revision_service = getattr(self.document_generation, "revision_service", None)
        if revision_service is None:
            return None
        revision = revision_service.get_revision(ctx, revision_id)
        return revision.model_dump(mode="json") if revision is not None else None

    def create_document_revision(
        self,
        ctx: RequestContext,
        task_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Record a governed revision and queue its regeneration work order.

        Revision requests are execution orders, not sticky notes: after the
        revision snapshot is frozen, a revision-idempotent WorkOrder restarts
        generation from the parent's frozen inputs on the durable worker.  The
        returned ``regeneration`` block lets callers show progress without
        polling the revision record.
        """
        revision_service = getattr(self.document_generation, "revision_service", None)
        if revision_service is None:
            raise KeyError("document revision service is unavailable")
        revision = revision_service.create_revision(ctx, task_id=task_id, **kwargs)
        payload = revision.model_dump(mode="json")
        if not revision.child_artifact_id:
            order = self.document_generation.restart_work_order_for_revision(
                ctx, revision.work_order_id, revision_id=revision.revision_id,
            )
            run_id = self.submit_knowledge_base_document_generation(ctx, order.work_order_id)
            payload["regeneration"] = {
                "work_order_id": order.work_order_id,
                "run_id": run_id,
                "status": "queued",
            }
        return payload

    def complete_document_revision(
        self,
        ctx: RequestContext,
        revision_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        revision_service = getattr(self.document_generation, "revision_service", None)
        if revision_service is None:
            raise KeyError("document revision service is unavailable")
        revision = revision_service.complete_revision(ctx, revision_id, **kwargs)
        return revision.model_dump(mode="json")

    def resume_document_task(
        self,
        ctx: RequestContext,
        task_id: str,
    ) -> dict[str, Any]:
        """Resume a task through its durable identity.

        The endpoint is deliberately task-bound: callers cannot resume an
        arbitrary WorkOrder copied from a stale browser URL.  A pending review
        must be decided first; once the task is ready, the existing WorkOrder
        queue/resume paths retain their own idempotency and frozen-input
        checks.
        """
        task = self._document_task_for_context(ctx, task_id)
        work_order_id = str(getattr(task, "work_order_id", None) or "").strip()
        if not work_order_id:
            raise ValueError("document task has no work order to resume")
        order = self.document_generation.store.get_work_order(work_order_id)
        if order is None:
            raise KeyError("document task work order not found")
        if getattr(order, "task_id", None) not in {None, task.task_id}:
            raise ValueError("document task/work-order association mismatch")
        self.document_generation.require_work_order_capability(
            ctx, order, "run_deterministic_work_order",
        )
        if order.scope_type != "knowledge_base" or not order.knowledge_base_name:
            raise ValueError("task resume currently supports knowledge-base document work orders")
        if not ctx.has_kb_permission(order.knowledge_base_name, "write"):
            raise PermissionError("knowledge base write permission is required")

        reviews = self._document_reviews_for_task(ctx, task, order)
        if any(review.get("status") in {"pending", "changes_requested"} for review in reviews):
            raise ValueError("document task has pending human review")

        if order.status == "paused":
            run_id = self.resume_knowledge_base_document_generation(ctx, work_order_id)
        elif order.status in {"planned", "waiting_human_input"}:
            run_id = self.submit_knowledge_base_document_generation(ctx, work_order_id)
        else:
            raise ValueError(f"document task cannot resume from work order status {order.status}")
        return {
            "task_id": task.task_id,
            "work_order_id": work_order_id,
            "run_id": run_id,
            "status": "queued",
        }

    def _document_task_for_context(
        self,
        ctx: RequestContext,
        task_id: str,
        *,
        required_permission: str = "read",
    ) -> Any:
        task_store = getattr(getattr(self.document_generation, "task_service", None), "store", None)
        task = task_store.get(str(task_id)) if task_store is not None else None
        if task is None:
            raise KeyError("document task not found")
        tenant_id = str(getattr(ctx, "tenant_id", None) or "default")
        user_id = str(getattr(ctx, "user_id", None) or "")
        if task.tenant_id != tenant_id or task.user_id != user_id:
            raise PermissionError("document task is outside the current owner scope")
        kb_name = str(task.knowledge_base_name or "").strip()
        requested_kb = str(getattr(ctx, "metadata", {}).get("document_template_kb_name") or "").strip()
        if requested_kb and kb_name and requested_kb != kb_name:
            raise PermissionError("document task belongs to another knowledge base")
        if kb_name and not ctx.has_kb_permission(kb_name, required_permission):
            raise PermissionError(f"knowledge base {required_permission} permission is required")
        return task

    def _document_reviews_for_task(
        self,
        ctx: RequestContext,
        task: Any,
        order: Any | None,
    ) -> list[dict[str, Any]]:
        review_store = getattr(self.document_generation, "review_store", None)
        if review_store is None:
            return []
        if order is not None:
            self._materialize_document_reviews(task, order, review_store)
        return [review.model_dump(mode="json") for review in review_store.list_for_task(task.task_id)]

    def _materialize_document_reviews(self, task: Any, order: Any, review_store: Any) -> None:
        """Adapt legacy ICD/artifact gates into the generic review namespace."""
        schema_hash = content_hash({
            "template_version_id": getattr(order, "template_version_id", None),
            "document_schema_id": getattr(order, "document_schema_id", None),
            "document_schema_version": getattr(order, "document_schema_version", None),
        })
        source_hash = ""
        resolve_snapshot = getattr(self.document_generation, "resolve_source_snapshot", None)
        if callable(resolve_snapshot):
            try:
                snapshot = resolve_snapshot(order)
            except (KeyError, PermissionError, ValueError):
                # Do not create a review bound to a snapshot identifier (or a
                # synthetic fallback) when the immutable source cannot be
                # resolved.  A review without the exact content hash would be
                # unsafe to approve; the next projection can retry once the
                # source store is available.
                return
            if snapshot is not None:
                source_hash = str(getattr(snapshot, "content_hash", "") or "").strip()
            if not source_hash:
                return
        else:
            source_hash = str(getattr(order, "baseline_content_hash", "") or "").strip()
        if not source_hash:
            return
        existing_kinds = {
            str(item.review_kind)
            for item in review_store.list_for_task(task.task_id)
        }
        get_icd = getattr(self.document_generation.store, "get_icd_scope_review", None)
        icd_review = get_icd(order.work_order_id) if callable(get_icd) else None
        if icd_review is not None and "icd_scope" not in existing_kinds:
            subject_hash = str(getattr(icd_review, "decision_content_hash", "") or "").strip()
            if not subject_hash:
                subject_hash = content_hash(getattr(icd_review, "decision", {}))
            exceptions = list(getattr(icd_review, "exceptions", []) or [])
            review_store.create(
                task_id=task.task_id,
                work_order_id=order.work_order_id,
                review_kind="icd_scope",
                status="approved" if getattr(icd_review, "status", "pending") == "frozen" else "pending",
                subject_hash=subject_hash,
                source_snapshot_hash=str(getattr(icd_review, "source_snapshot_hash", "") or source_hash),
                schema_hash=schema_hash,
                metadata={
                    "exception_ids": [str(getattr(item, "exception_id", "")) for item in exceptions],
                    "pending_count": int(getattr(icd_review, "pending_count", 0) or 0),
                },
                client_request_id=f"legacy-adapter:icd-scope:{order.work_order_id}:{subject_hash}",
            )

        list_artifacts = getattr(self.document_generation.store, "list_artifacts", None)
        artifacts = list_artifacts(order.work_order_id) if callable(list_artifacts) else []
        for artifact in artifacts or []:
            if getattr(artifact, "stage", None) != "review_candidate":
                continue
            if any(
                getattr(released, "stage", None) == "approved_release"
                and getattr(released, "parent_artifact_id", None) == getattr(artifact, "artifact_id", None)
                for released in artifacts or []
            ):
                # The legacy artifact approval endpoint may have released the
                # candidate before the generic review adapter was projected.
                # Do not resurrect a stale pending review for an immutable
                # artifact that already has a release child.
                continue
            kind = f"artifact_approval:{artifact.artifact_id}"
            if kind in existing_kinds:
                continue
            artifact_content_hash = str(getattr(artifact, "content_hash", None) or "").strip()
            stored_subject_hash = str(getattr(artifact, "approval_subject_hash", None) or "").strip()
            report_hash = ""
            get_report = getattr(self.document_generation.store, "get_validation_report", None)
            if callable(get_report):
                report = get_report(getattr(artifact, "validation_report_id", None))
                report_hash = str(getattr(report, "content_hash", None) or "").strip()
            if artifact_content_hash and report_hash:
                subject_hash = content_hash({
                    "artifact_content_hash": artifact_content_hash,
                    "validation_report_hash": report_hash,
                    "source_set_snapshot_hash": source_hash,
                })
                if stored_subject_hash and stored_subject_hash != subject_hash:
                    continue
            else:
                subject_hash = stored_subject_hash or artifact_content_hash
            if not subject_hash:
                continue
            review_store.create(
                task_id=task.task_id,
                work_order_id=order.work_order_id,
                artifact_id=artifact.artifact_id,
                review_kind=kind,
                status="pending",
                subject_hash=subject_hash,
                source_snapshot_hash=source_hash,
                schema_hash=schema_hash,
                metadata={"stage": str(getattr(artifact, "stage", ""))},
                client_request_id=f"legacy-adapter:artifact:{artifact.artifact_id}:{subject_hash}",
            )

    def list_document_task_projections(
        self,
        ctx: RequestContext,
        *,
        knowledge_base_name: str | None = None,
        conversation_id: str | int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List task projections using the same authorization as single reads."""
        task_service = getattr(self.document_generation, "task_service", None)
        task_store = getattr(task_service, "store", None)
        if task_store is None or not getattr(src.settings, "DOCUMENT_TASK_READ_ENABLED", True):
            return []
        requested_kb = str(knowledge_base_name or "").strip() or None
        if requested_kb and not ctx.has_kb_permission(requested_kb, "read"):
            raise PermissionError("knowledge base read permission is required")
        requested_conversation = str(conversation_id or "").strip() or None
        projections: list[dict[str, Any]] = []
        for task in task_store.list_for_owner(
            tenant_id=str(getattr(ctx, "tenant_id", None) or "default"),
            user_id=str(getattr(ctx, "user_id", None) or ""),
            limit=limit,
        ):
            if requested_kb and task.knowledge_base_name != requested_kb:
                continue
            if requested_conversation and task.conversation_id != requested_conversation:
                continue
            try:
                projection = self.get_document_task_projection(ctx, task.task_id)
            except PermissionError:
                continue
            if projection is not None:
                projections.append(projection)
        return projections

    def _document_clarification_projection(
        self,
        ctx: RequestContext,
        task: Any,
    ) -> dict[str, Any] | None:
        session_id = str(getattr(task, "generation_session_id", None) or "").strip()
        if not session_id:
            return None
        sessions = getattr(getattr(self.document_generation, "store", None), "generation_sessions", None)
        if sessions is None:
            return None
        try:
            session = sessions.get_session(
                session_id,
                tenant_id=str(getattr(ctx, "tenant_id", None) or "default"),
                user_id=str(getattr(ctx, "user_id", None) or ""),
            )
        except (KeyError, PermissionError, ValueError):
            # The task projection is additive and must remain readable when a
            # legacy/partially migrated task points at a removed or unavailable
            # GenerationSession.  WorkOrder/Artifact state is still useful.
            return None
        if session is None:
            return None
        pending = next(
            (
                item for item in reversed(session.messages)
                if item.role == "assistant" and item.question_id
            ),
            None,
        )
        return {
            "session_id": session.session_id,
            "status": session.status,
            "contract_version": getattr(session, "contract_version", "legacy_brief_v1"),
            "last_question_id": session.last_question_id,
            "clarification_revision": session.clarification_revision,
            "pending_question": (
                {
                    "question_id": pending.question_id,
                    "content": pending.content,
                    "options": list(pending.options),
                    "reason": pending.reason,
                }
                if pending is not None
                else None
            ),
        }

    @staticmethod
    def _safe_document_task_artifact(artifact: Any, order: Any) -> dict[str, Any]:
        projection = {
            "artifact_id": str(getattr(artifact, "artifact_id", "") or ""),
            "run_id": str(getattr(artifact, "run_id", "") or ""),
            "stage": str(getattr(artifact, "stage", "") or ""),
            "output_format": getattr(artifact, "output_format", None) or getattr(order, "target_format", None),
            "parent_artifact_id": getattr(artifact, "parent_artifact_id", None),
            "validity_status": getattr(artifact, "validity_status", None),
            "policy_status": getattr(artifact, "policy_status", None),
            "validation_report_id": getattr(artifact, "validation_report_id", None),
        }
        revision_id = getattr(artifact, "revision_id", None)
        if revision_id:
            projection["revision_id"] = revision_id
        return projection

    @staticmethod
    def _document_task_next_actions(
        task: Any,
        clarification: dict[str, Any] | None,
        order: Any,
    ) -> list[str]:
        if clarification and clarification.get("status") == "needs_clarification":
            return ["answer_clarification"]
        if order is None and task.status == "planned":
            return ["create_document_work_order"]
        if order is not None and task.status == "planned":
            return ["resume_document_task"]
        if task.status in {"queued", "running"}:
            return ["poll_status"]
        if task.status == "waiting_human":
            return ["review_document"]
        if task.status == "completed":
            return ["view_result"]
        return []

    def submit_document_human_event(self, ctx: RequestContext, **kwargs):
        return self.document_generation.submit_document_human_event(ctx, **kwargs)

    def submit_document_feedback(self, ctx: RequestContext, artifact_id: str, *, comment: str):
        return self.document_generation.submit_document_feedback(
            ctx,
            artifact_id,
            comment=comment,
        )

    def approve_document_artifact(self, ctx: RequestContext, artifact_id: str, *, comment: str = ""):
        return self.document_generation.approve_document_artifact(ctx, artifact_id, comment=comment)

    def download_document_artifact(self, ctx: RequestContext, artifact_id: str) -> bytes:
        return self.document_generation.download_document_artifact(ctx, artifact_id)

    def preview_document_artifact(self, ctx: RequestContext, artifact_id: str) -> dict[str, Any]:
        return self.document_generation.preview_document_artifact(ctx, artifact_id)
