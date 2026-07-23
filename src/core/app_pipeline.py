# src/core/app_pipeline.py
import os
import shutil
import tempfile
import traceback
from typing import Callable, Generator, List, Tuple

import config.settings
from src.agents.runner import MultiSourceAgentRunner
from src.core.auth import AuthService
from src.core.logger import error, log, warn
from src.ingestion.kb_paths import InvalidKnowledgeBaseName, validate_kb_name
from src.pipelines.document_rag.factory import create_rag_backend
from src.pipelines.document_rag.schemas import IngestResult, RequestContext
from src.projects.service import ProjectService
from src.document_authoring.service import DocumentGenerationService
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
            # Authoring is deliberately a sibling of the query agent.  It
            # shares source/evidence services but never stores WorkOrder state
            # in a chat session or AgentState.
            self.projects = ProjectService()
            self.document_generation = DocumentGenerationService(self.projects)
        except Exception as exc:
            error(f"AppPipeline 初始化失败: {exc}")
            raise

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
            )
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
            return self.documents.upload_files(
                file_paths,
                target_kb,
                ctx=ctx,
                source_group=source_group,
                progress_callback=progress_callback,
            )
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
        return self.documents.delete_document(filename, kb_name, ctx=ctx)

    def create_kb(self, name: str, ctx: RequestContext | None = None) -> Tuple[bool, str]:
        try:
            name = validate_kb_name(name.strip().replace(" ", "_"))
            if not name:
                return False, "名称不能为空"
            if ctx is not None and "anonymous" in ctx.roles:
                return False, "权限不足：请先登录再创建知识库。"
            if ctx is not None and ctx.is_system_admin():
                return False, "系统管理员不能创建内容知识库，请由部门管理员创建。"
            auth_service = AuthService()
            scope = kb_scope_from_context(name, ctx).require_department("create")
            if auth_service.knowledge_base_exists(scope.kb_name, department_id=scope.department_id):
                return False, "知识库已存在"

            self.backend.create_kb_storage(scope.kb_name, ctx=ctx)

            if ctx and ctx.user_id:
                owner = auth_service.get_user_by_username(ctx.user_id)
                auth_service.register_knowledge_base(scope.kb_name, owner=owner)
            log(f"知识库 '{name}' 创建成功（后端 {self.backend.name}）")
            return True, f"知识库 '{name}' 创建成功"
        except Exception as exc:
            error(f"创建知识库失败: {exc}")
            return False, str(exc)

    def delete_knowledge_base(self, kb_name: str, ctx: RequestContext | None = None) -> Tuple[bool, str]:
        try:
            kb_name = validate_kb_name(kb_name)
        except InvalidKnowledgeBaseName as exc:
            return False, str(exc)

        if ctx is None or not ctx.has_kb_permission(kb_name, "admin"):
            return False, "权限不足：删除知识库需要 admin 权限。"

        log(f"准备彻底删除知识库: {kb_name}")
        try:
            result = self.documents.delete_knowledge_base_documents(kb_name, ctx=ctx)
            if not result.ok:
                return False, result.message
            scope = kb_scope_from_context(kb_name, ctx)
            AuthService().delete_knowledge_base_record(scope.kb_name, department_id=scope.department_id, kb_id=scope.kb_id)
            return True, result.message
        except Exception as exc:
            error(f"删除知识库失败: {exc}")
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
        return self.documents.delete_parse_task(task_id, ctx=ctx)

    def clear_finished_parse_tasks(self, kb_name: str | None = None, ctx: RequestContext | None = None):
        self.documents.clear_finished_parse_tasks(kb_name, ctx=ctx)

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

    def list_document_work_orders(self, ctx: RequestContext, *, project_id: str):
        """List durable work-order summaries visible to the current project user."""
        self.projects.access.require(ctx, project_id, "view_project")
        return self.document_generation.store.list_work_orders(ctx.tenant_id or "default", project_id)

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

    def get_document_template_sanitization_summary(self, ctx: RequestContext, template_version_id: str):
        return self.document_generation.get_template_sanitization_summary(ctx, template_version_id)

    def confirm_document_template(self, ctx: RequestContext, *, analysis_id: str, display_name: str):
        return self.document_generation.confirm_template_analysis(
            ctx, analysis_id=analysis_id, display_name=display_name,
        )

    def create_document_work_order(self, ctx: RequestContext, **kwargs):
        return self.document_generation.create_document_work_order(ctx, **kwargs)

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
        if ctx is not None:
            self.projects.access.require(ctx, order.project_id, "view_project")
        status = {
            "work_order_id": order.work_order_id,
            "status": order.status,
            "project_id": order.project_id,
            "target_format": order.target_format,
            "unit_statuses": dict(order.unit_statuses),
            "validation_report_id": order.validation_report_id,
        }
        if order.run_manifest_id:
            status["run_manifest_id"] = order.run_manifest_id
        harness_runs = self.document_generation.store.list_harness_runs(order.work_order_id)
        if harness_runs:
            latest_run = harness_runs[-1]
            status["harness_run"] = {
                "run_id": latest_run.harness_run_id,
                "status": latest_run.status,
                "current_node": latest_run.current_node,
                "step_count": latest_run.step_count,
                "retrieval_round_count": latest_run.retrieval_round_count,
                "retry_count": latest_run.retry_count,
                "checkpoint_id": latest_run.checkpoint_id,
                "fencing_token": latest_run.fencing_token,
                "error": latest_run.error,
            }
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

    def approve_document_artifact(self, ctx: RequestContext, artifact_id: str, *, comment: str = ""):
        return self.document_generation.approve_document_artifact(ctx, artifact_id, comment=comment)

    def download_document_artifact(self, ctx: RequestContext, artifact_id: str) -> bytes:
        return self.document_generation.download_document_artifact(ctx, artifact_id)
