# src/core/app_pipeline.py
import os
import shutil
import tempfile
import traceback
from typing import Any, Callable, Generator, List, Tuple

import src.settings
from src.agents.runner import MultiSourceAgentRunner, forget_thread
from src.agents.tools.pipeline_catalog import scan_kb_sources as scan_pipeline_kb_sources
from src.core.auth import AuthService
from src.core.cancellation import QueryCancelled
from src.core.conversation import GENERAL_CHAT_KB_NAME
from src.core.logger import error, log, warn
from src.ingestion.kb_paths import InvalidKnowledgeBaseName, validate_kb_name
from src.pipelines.document_rag.factory import create_rag_backend
from src.pipelines.document_rag.schemas import IngestResult, RequestContext
from src.services.document_manager import DocumentManager
from src.services.kb_scope import kb_scope_from_context




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
                document_manager=self.documents,
            )
        except Exception as exc:
            error(f"AppPipeline 初始化失败: {exc}")
            raise

    @staticmethod
    def _resolve_actor_user_id(ctx: RequestContext | None, auth_service: "AuthService") -> int | None:
        """把 ctx.user_id(用户名或数字)解析为 users.id; 无法解析返回 None。"""
        raw = str(getattr(ctx, "user_id", "") or "").strip()
        if not raw:
            return None
        if raw.isdigit():
            return int(raw)
        user = auth_service.get_user_by_username(raw)
        return int(user.id) if user is not None else None

    @staticmethod
    def _kb_sharing_audit_metadata(kb_name: str, ctx: RequestContext | None) -> dict[str, Any]:
        """危险操作审计补充: 库是否部门共享、操作者是否为所有者。

        共享库的删除等操作会影响全部门, 审计记录必须能区分"所有者自删"与
        "部门成员删除他人共享库"。
        """
        try:
            auth_service = AuthService()
            scope = kb_scope_from_context(kb_name, ctx)
            sharing = auth_service.get_knowledge_base_sharing(
                scope.kb_name, department_id=scope.department_id, kb_id=scope.kb_id
            )
            if sharing is None:
                return {}
            owner_id, is_shared = sharing
            actor_id = AppPipeline._resolve_actor_user_id(ctx, auth_service)
            return {
                "is_shared": is_shared,
                "owner_user_id": owner_id,
                "actor_is_owner": owner_id is not None and owner_id == actor_id,
            }
        except Exception:  # noqa: BLE001 - 审计补充信息失败不影响主流程
            return {}

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

    def _audit_requested(
        self,
        action: str,
        ctx: RequestContext | None,
        target_type: str = "",
        target_id: str = "",
        kb_name: str = "",
        metadata: dict | None = None,
    ) -> None:
        """Write the pre-mutation intent row for a destructive/write operation.

        The outcome row still uses the original action.  If the process dies
        between the mutation and that row, the ``*.requested`` event remains
        visible instead of the operation disappearing from the audit trail.
        """

        self._audit(
            f"{action}.requested",
            ctx,
            target_type=target_type,
            target_id=target_id,
            kb_name=kb_name,
            success=True,
            metadata={**(metadata or {}), "phase": "requested"},
        )

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
        wiki_assist: bool = False,
        attachments: list[Any] | None = None,
        source_scope: str = "auto",
        propagate_errors: bool = False,
        attachment_user_id: int | None = None,
        conversation_session_id: int | None = None,
        action_scope: Any | None = None,
        knowledge_scope: Any | None = None,
        knowledge_scope_resolver: Any | None = None,
        workspace_id: str = "",
        turn_id: str = "",
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
                wiki_assist=wiki_assist,
                attachments=attachments,
                source_scope=source_scope,
                attachment_user_id=attachment_user_id,
                conversation_session_id=conversation_session_id,
                action_scope=action_scope,
                knowledge_scope=knowledge_scope,
                knowledge_scope_resolver=knowledge_scope_resolver,
                workspace_id=workspace_id,
                turn_id=turn_id,
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

    def get_last_agent_answer(self) -> str:
        return self.agent.get_last_agent_answer()

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
        directory_id: int = 0,
        file_metadata: list[dict] | None = None,
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
            self._audit_requested(
                "upload_document",
                ctx,
                target_type="document",
                target_id=", ".join(os.path.basename(p) for p in file_paths),
                kb_name=target_kb,
                metadata={"file_count": len(file_paths), "source_group": source_group},
            )
            result = self.documents.upload_files(
                file_paths,
                target_kb,
                ctx=ctx,
                source_group=source_group,
                progress_callback=progress_callback,
                directory_id=directory_id,
                file_metadata=file_metadata,
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
            from src.pipelines.document_rag.ragflow_backend import RAGFlowAPIError, RAGFlowClient

            client = RAGFlowClient(
                base_url=base_url.rstrip("/"),
                api_key=api_key,
                timeout=timeout,
            )
            datasets = client.list_datasets()
            existing_names = {item.get("name") for item in datasets if isinstance(item, dict)}
            missing: List[str] = []
            seen: set[str] = set()
            for name in dataset_names:
                if not name or name in seen or name in existing_names:
                    continue
                seen.add(name)
                # Some RAGFlow deployments hide shared datasets from the
                # unfiltered listing; probe the exact name before reporting it
                # missing (mirrors what operators verify by hand with the API).
                try:
                    hits = client.list_datasets(name=name)
                except RAGFlowAPIError:
                    hits = []
                if any(isinstance(item, dict) and item.get("name") == name for item in hits):
                    continue
                missing.append(name)
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
            src.settings.validate_provider_configuration(new_settings)
            src.settings.save_settings_to_env(new_settings)
            src.settings.reload_settings()
            # create_chat_model 是 lru_cache 的:不清缓存的话,管理页改完
            # AGENT_LLM_* 后旧模型实例会一直用到进程重启。
            from src.core.model_factory import invalidate_chat_model_cache

            invalidate_chat_model_cache()
            from src.agents.tools.retrieval_executor import initialize_retrieval_executor

            try:
                initialize_retrieval_executor()
            except Exception as exc:
                error(f"Retrieval executor unavailable after settings reload: {type(exc).__name__}")

    def delete_document(self, filename: str, kb_name: str, ctx: RequestContext | None = None, *, directory_id: int | None = None) -> str:
        try:
            self._audit_requested(
                "delete_document",
                ctx,
                target_type="document",
                target_id=filename,
                kb_name=kb_name,
                metadata=self._kb_sharing_audit_metadata(kb_name, ctx),
            )
            result = self.documents.delete_document(filename, kb_name, ctx=ctx, directory_id=directory_id)
            # DocumentManager now returns the full BackendResult; branch on .ok
            # instead of sniffing error prefixes out of the message string.
            delete_ok = bool(getattr(result, "ok", False))
            message = getattr(result, "message", str(result))
            if delete_ok:
                self._invalidate_quality_source(kb_name, ctx, filename=filename)
            self._audit(
                "delete_document",
                ctx,
                target_type="document",
                target_id=filename,
                kb_name=kb_name,
                success=delete_ok,
                error_message="" if delete_ok else message,
                metadata=self._kb_sharing_audit_metadata(kb_name, ctx),
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

    def delete_directory_recursive(
        self,
        kb_name: str,
        directory_id: int,
        *,
        ctx: RequestContext | None = None,
    ) -> dict:
        """Delete all files and directories below one directory."""
        from src.services.pipeline_directories import delete_kb_directory_tree

        return delete_kb_directory_tree(self, kb_name, directory_id, ctx=ctx)

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
                return False, "系统管理员不能创建内容知识库，请用员工账号创建。"
            auth_service = AuthService()
            scope = kb_scope_from_context(name, ctx).require_department("create")
            if auth_service.knowledge_base_exists(scope.kb_name, department_id=scope.department_id):
                self._audit("create_kb", ctx, target_type="knowledge_base", target_id=name, kb_name=name, success=False, error_message="知识库已存在")
                return False, "知识库已存在"


            self._audit_requested(
                "create_kb",
                ctx,
                target_type="knowledge_base",
                target_id=name,
                kb_name=name,
            )
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

        # Owner 保护: 部门内员工对 KB 隐式 admin, 但删除只允许创建者,
        # 防止同事互删各自建立的知识库(历史无 owner 的库保持可删, 兼容旧数据)。
        # 注意 RequestContext.user_id 存的是用户名, 必须解析成 users.id 再比较。
        try:
            scope = kb_scope_from_context(kb_name, ctx)
            auth_service = AuthService()
            owner_id = auth_service.get_knowledge_base_owner(
                kb_name, department_id=scope.department_id, kb_id=scope.kb_id
            )
            actor_id = self._resolve_actor_user_id(ctx, auth_service)
        except Exception:  # noqa: BLE001 - 解析失败时回退到原有 admin 校验
            owner_id = None
            actor_id = None
        if owner_id is not None and actor_id != owner_id:
            self._audit("delete_kb", ctx, target_type="knowledge_base", target_id=kb_name, kb_name=kb_name, success=False, error_message="仅知识库创建者可删除。")
            return False, "仅知识库创建者可删除该知识库。"

        log(f"准备彻底删除知识库: {kb_name}")
        try:
            self._audit_requested(
                "delete_kb",
                ctx,
                target_type="knowledge_base",
                target_id=kb_name,
                kb_name=kb_name,
            )
            result = self.documents.delete_knowledge_base_documents(kb_name, ctx=ctx)
            if not result.ok:
                self._audit("delete_kb", ctx, target_type="knowledge_base", target_id=kb_name, kb_name=kb_name, success=False, error_message=result.message)
                return False, result.message
            scope = kb_scope_from_context(kb_name, ctx)
            self._invalidate_quality_source(kb_name, ctx)
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

    @staticmethod
    def _invalidate_quality_source(kb_name: str, ctx: RequestContext | None, *, filename: str | None = None) -> None:
        if ctx is None or not getattr(src.settings, "QUALITY_ENABLED", True):
            return
        scope = kb_scope_from_context(kb_name, ctx)
        if scope.department_id is None:
            return
        from src.quality.store import QualityStore

        # A source deletion also revokes review copies derived from it.
        # Initialize local conversation tables when this is the first operation.
        if src.settings.CONTROL_PLANE_BACKEND == "sqlite":
            from src.core.conversation import ConversationService

            ConversationService()
        QualityStore().invalidate_source(kb_name=kb_name, department_id=scope.department_id, kb_id=scope.kb_id, filename=filename)

    def list_files(self, kb_name: str, ctx: RequestContext | None = None) -> List[str]:
        return self.documents.list_files(kb_name, ctx=ctx)

    def list_file_infos(
        self,
        kb_name: str,
        ctx: RequestContext | None = None,
        *,
        directory_id: int | None = None,
    ):
        if directory_id is None:
            return self.documents.list_file_infos(kb_name, ctx=ctx)
        return self.documents.list_file_infos(kb_name, ctx=ctx, directory_id=directory_id)

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

    def requeue_dead_letter_parse_tasks(self, kb_name: str, ctx: RequestContext | None = None) -> list[str]:
        try:
            self._audit_requested(
                "requeue_dead_letter_parse_tasks",
                ctx,
                target_type="parse_task",
                target_id=kb_name,
                kb_name=kb_name,
            )
            names = self.documents.requeue_dead_letter_parse_tasks(kb_name, ctx=ctx)
            self._audit(
                "requeue_dead_letter_parse_tasks",
                ctx,
                target_type="parse_task",
                target_id=kb_name,
                kb_name=kb_name,
                success=True,
                metadata={"requeued": names},
            )
            return names
        except Exception as exc:
            self._audit(
                "requeue_dead_letter_parse_tasks",
                ctx,
                target_type="parse_task",
                target_id=kb_name,
                kb_name=kb_name,
                success=False,
                error_message=str(exc),
            )
            raise

    def delete_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> str:
        # Stopping a parse task deletes the remote RAGFlow document + local
        # archive; audit it so the governance log can trace who cancelled what.
        try:
            self._audit_requested(
                "delete_parse_task",
                ctx,
                target_type="parse_task",
                target_id=str(task_id),
            )
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
            self._audit_requested(
                "clear_parse_tasks",
                ctx,
                target_type="parse_task",
                target_id=kb_name or "",
                kb_name=kb_name or "",
            )
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

    def get_parse_result(self, kb_name: str, document_id: str, ctx: RequestContext | None = None,
                         *, deadline: float | None = None, should_cancel=None):
        return self.documents.get_parse_result(
            kb_name, document_id, ctx=ctx, deadline=deadline, should_cancel=should_cancel,
        )

    # Project/document-authoring domain entry points.  REST, UI and future MCP
    # adapters must use these service methods instead of reaching into SQLite,
    # pipeline archives or renderer file paths directly.
