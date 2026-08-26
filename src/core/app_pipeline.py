# src/core/app_pipeline.py
import os
import re
import shutil
import tempfile
import traceback
from typing import Any, Callable, Generator, List, Tuple

import config.settings
from src.agents.runner import MultiSourceAgentRunner
from src.agents.schemas import Evidence
from src.agents.tools.circuit_tools import CircuitQueryTool
from src.agents.tools.spreadsheet_tools import SpreadsheetSemanticTool
from src.core.auth import AuthService
from src.core.cancellation import QueryCancelled
from src.core.logger import error, log, warn
from src.ingestion.kb_paths import InvalidKnowledgeBaseName, validate_kb_name
from src.pipelines.document_rag.factory import create_rag_backend
from src.pipelines.document_rag.schemas import EvidenceEnvelope, IngestResult, RequestContext
from src.projects.service import ProjectService
from src.projects.retrieval import ProjectEvidenceRetrievalService, SourceUnavailableError
from src.document_authoring.service import DocumentGenerationService
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
from src.document_authoring.requirement_clarifier import RequirementClarifier
from src.document_authoring.retriever_registry import (
    CrossUnitEvidenceCache,
    RetrieverRegistry,
    apply_role_boost,
    dedup_by_content,
)
from src.services.document_manager import DocumentManager
from src.services.kb_scope import kb_scope_from_context


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
            self.requirement_clarifier = RequirementClarifier()
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
    ) -> Generator[str, None, None]:
        self.clear_last_token_usage_summary()
        if not msg.strip():
            yield "请输入有效问题"
            return
        if not kb_name:
            yield "未选择知识库"
            return
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
            )
        except QueryCancelled:
            return
        except Exception as exc:
            error(f"查询出错: {exc}")
            traceback.print_exc()
            yield f"系统错误: {str(exc)}"

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
        with config.settings._ENV_WRITE_LOCK:
            config.settings.validate_settings_values(new_settings)
            config.settings.save_settings_to_env(new_settings)
            config.settings.reload_settings()
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
        if not ctx.has_kb_permission(knowledge_base_name, "read"):
            raise PermissionError("knowledge base read permission is required")
        return self.document_generation.store.list_work_orders_for_knowledge_base(
            ctx.tenant_id or "default",
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

    def analyze_document_template(self, ctx: RequestContext, *, filename: str, content: bytes, template_name: str):
        return self.document_generation.analyze_uploaded_template(
            ctx, filename=filename, content=content, template_name=template_name,
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

    def confirm_document_template(self, ctx: RequestContext, *, analysis_id: str, display_name: str):
        return self.document_generation.confirm_template_analysis(
            ctx, analysis_id=analysis_id, display_name=display_name,
        )

    def create_document_generation_session(
        self,
        ctx: RequestContext,
        *,
        knowledge_base_name: str,
        template_version_id: str,
        purpose: str = "",
        output_policy: dict[str, Any] | None = None,
    ):
        template = self.document_generation.store.get_template(template_version_id)
        if template is None:
            raise KeyError("template not found")
        self.document_generation._require_template_kb_scope(ctx, template, "read")
        analysis = self.document_generation.store.get_template_analysis(template_version_id)
        if analysis is None:
            raise KeyError("template analysis not found")
        policy = dict(output_policy or {})
        policy.setdefault("format", analysis.format)
        brief = GenerationBrief(purpose=purpose.strip(), output_policy=policy)
        session = self.document_generation.store.generation_sessions.create_session(
            tenant_id=ctx.tenant_id or "default",
            user_id=ctx.user_id,
            knowledge_base_name=knowledge_base_name,
            template_version_id=template_version_id,
            brief=brief,
        )
        message = self.requirement_clarifier.next_message(
            analysis.model_dump(mode="json"),
            brief,
        )
        self.document_generation.store.generation_sessions.append_message(
            session.session_id,
            role=message.role,
            content=message.content,
            question_id=message.question_id,
            options=message.options,
            reason=message.reason,
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

    def answer_document_generation_session(
        self,
        ctx: RequestContext,
        session_id: str,
        *,
        question_id: str,
        answer: str,
    ):
        session = self.get_document_generation_session(ctx, session_id)
        if session.status != "needs_clarification":
            raise ValueError("generation session is not accepting clarification answers")
        pending = next(
            (
                message
                for message in reversed(session.messages)
                if message.role == "assistant" and message.question_id
            ),
            None,
        )
        if pending is None or pending.question_id != question_id:
            raise ValueError("clarification answer does not match the current question")
        brief = self.requirement_clarifier.apply_answer(
            session.brief,
            question_id=question_id,
            answer=answer,
        )
        sessions = self.document_generation.store.generation_sessions
        sessions.append_message(
            session_id,
            role="user",
            content=answer.strip(),
            question_id=question_id,
            answer=answer.strip(),
        )
        sessions.update_brief(session_id, brief.model_dump())
        analysis = self.document_generation.store.get_template_analysis(
            session.template_version_id,
        )
        if analysis is None:
            raise KeyError("template analysis not found")
        message = self.requirement_clarifier.next_message(
            analysis.model_dump(mode="json"),
            brief,
        )
        sessions.append_message(
            session_id,
            role=message.role,
            content=message.content,
            question_id=message.question_id,
            options=message.options,
            reason=message.reason,
        )
        return self.get_document_generation_session(ctx, session_id)

    def confirm_document_generation_session(self, ctx: RequestContext, session_id: str):
        session = self.get_document_generation_session(ctx, session_id)
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
        return self.document_generation.store.generation_sessions.confirm(session_id)

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
        if not ctx.has_kb_permission(knowledge_base_name, "read"):
            raise PermissionError("knowledge base read permission is required")
        source_names = []
        for document in self.list_file_infos(knowledge_base_name, ctx):
            name = (
                document.get("name", "")
                if isinstance(document, dict)
                else getattr(document, "name", "")
            )
            normalized = str(name).strip()
            if normalized and normalized not in source_names:
                source_names.append(normalized)
        if not source_names:
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
        profile = self._icd_template_profile(order)
        if profile is not None and profile.kind == "icd_sample":
            return {
                "stage": "template_contract_review_required",
                "work_order_id": order.work_order_id,
                "issues": [{
                    "code": "icd_formal_template_required",
                    "severity": "blocking",
                    "message": "ICD 示例模板缺少正式连接器定义；请上传含连接器编号、板端型号和管脚定义表的正式 ICD 模板。",
                }, *profile.issues],
            }
        scope_review = None
        icd_schema = self._icd_connector_scope_schema(order)
        if icd_schema is not None:
            supporting_evidences = self.backend.retrieve(
                knowledge_base_name,
                "ICD connector pin mapping",
                top_k=config.settings.FINAL_TOP_K,
                ctx=ctx,
                filters={"source_names": list(snapshot.source_names)},
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
                    if getattr(self, "circuit_service", None) is not None
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
                return {
                    "stage": "scope_review_required",
                    "work_order_id": order.work_order_id,
                    "exceptions": [
                        exception.model_dump()
                        if hasattr(exception, "model_dump")
                        else dict(vars(exception))
                        for exception in scope_review.exceptions
                    ],
                }
        return {"stage": "ready", "work_order_id": order.work_order_id}

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
        retrieve = self._knowledge_base_retriever(
            ctx,
            order.knowledge_base_name,
            list(snapshot.source_names),
            source_set_snapshot_id=snapshot.source_set_snapshot_id,
            icd_scope_review=scope_review,
        )
        return self.document_generation.run_internal_harness(
            ctx,
            work_order_id,
            retrieve=retrieve,
        )

    def submit_knowledge_base_document_generation(
        self,
        ctx: RequestContext,
        work_order_id: str,
    ) -> str:
        """Run an existing KB work order's harness on the background worker."""
        return self.document_generation.worker.submit(
            work_order_id,
            lambda: self.continue_knowledge_base_document_generation(ctx, work_order_id),
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
        snapshot = self.document_generation.resolve_source_snapshot(order)
        scope_review = self.document_generation.get_icd_scope_review(ctx, work_order_id)
        retrieve = self._knowledge_base_retriever(
            ctx,
            order.knowledge_base_name,
            list(snapshot.source_names),
            source_set_snapshot_id=snapshot.source_set_snapshot_id,
            icd_scope_review=scope_review,
        )
        return self.document_generation.worker.submit(
            work_order_id,
            lambda: self.document_generation.resume_internal_harness(
                ctx,
                paused_run.harness_run_id,
                retrieve=retrieve,
            ),
        )

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
    ):
        if not ctx.has_kb_permission(kb_name, "read"):
            raise PermissionError("knowledge base read permission is required")
        frozen_source_names = list(dict.fromkeys(source_names))
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
            filters = {"source_names": frozen_source_names}
            if balanced_route:
                filters["balanced_route"] = True
            return list(
                self.backend.retrieve(
                    kb_name,
                    query,
                    top_k=config.settings.FINAL_TOP_K,
                    ctx=ctx,
                    filters=filters,
                )
            )

        specialized: dict[str, Callable[[str, Any], list]] = {}
        if spreadsheet_tool is not None:
            def _tabular_retriever(query, requirement):
                sp_evidences = spreadsheet_tool.run(
                    query,
                    kb_name,
                    ctx,
                    top_k=config.settings.FINAL_TOP_K,
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
        if circuit_tool is not None:
            def _circuit_retriever(query, requirement):
                circuit_evidences = circuit_tool.run(
                    query,
                    kb_name,
                    ctx,
                    top_k=config.settings.FINAL_TOP_K,
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
        if circuit_tool is not None or frozen_pin_evidence:
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
                        top_k=config.settings.FINAL_TOP_K,
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
                        top_k=config.settings.FINAL_TOP_K,
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
                        top_k=config.settings.FINAL_TOP_K,
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
        status = {
            "work_order_id": order.work_order_id,
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
            "validation_report_id": order.validation_report_id,
            "clarification_session_id": getattr(order, "generation_session_id", None),
            "generation_brief": dict(getattr(order, "generation_brief", {}) or {}),
            "error_code": getattr(order, "error_code", None),
            "error_message": getattr(order, "error_message", None),
            "retryable": getattr(order, "retryable", None),
            "next_actions": list(getattr(order, "next_actions", []) or []),
        }
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
        if order.validation_report_id:
            report = self.document_generation.store.get_validation_report(order.validation_report_id)
            if report is not None:
                status["validation"] = {"status": report.status, "issues": list(report.issues)}
        status["artifacts"] = [
            {
                "artifact_id": artifact.artifact_id,
                "stage": artifact.stage,
                "validation_report_id": artifact.validation_report_id,
                "validity_status": artifact.validity_status,
                "policy_status": artifact.policy_status,
            }
            for artifact in self.document_generation.store.list_artifacts(order.work_order_id)
        ]
        return status

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
