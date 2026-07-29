# src/core/app_pipeline.py
import os
import shutil
import tempfile
import traceback
from typing import Callable, Generator, List, Tuple

import config.settings
from src.agents.runner import MultiSourceAgentRunner
from src.core.auth import AuthService
from src.core.cancellation import QueryCancelled
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
            )
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
        progress_callback: Callable[[str, str, str, str], None] | None = None,
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
                progress_callback=progress_callback,
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
        config.settings.save_settings_to_env(new_settings)
        config.settings.reload_settings()

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
