# src/core/app_pipeline.py
import os
import shutil
import tempfile
import traceback
from typing import Any, Callable, Generator, List, Tuple

import config.settings
from src.agents.runner import MultiSourceAgentRunner
from src.agents.state import Evidence
from src.agents.tools.circuit_tools import CircuitQueryTool
from src.agents.tools.spreadsheet_tools import SpreadsheetSemanticTool
from src.core.auth import AuthService
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
    effective_frozen_pin_mappings,
)
from src.document_authoring.template_progress import TemplateProgressCallback
from src.document_authoring.retriever_registry import (
    CrossUnitEvidenceCache,
    RetrieverRegistry,
    apply_role_boost,
    dedup_by_content,
)
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
            self.project_retrieval = ProjectEvidenceRetrievalService(self.projects)
            self.document_generation = DocumentGenerationService(self.projects)
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

    def auto_generate_knowledge_base_document(
        self,
        ctx: RequestContext,
        *,
        knowledge_base_name: str,
        **kwargs,
    ):
        """Create and run a KB work order using only its frozen source snapshot."""
        order = self.create_knowledge_base_document_work_order(
            ctx,
            knowledge_base_name=knowledge_base_name,
            **kwargs,
        )
        snapshot = self.document_generation.resolve_source_snapshot(order)
        scope_review = None
        if self._schema_has_relationship_lookup(order):
            circuit_evidences = (
                self.circuit_service.list_pin_mapping_evidence(
                    knowledge_base_name,
                    list(snapshot.source_names),
                    ctx,
                )
                if getattr(self, "circuit_service", None) is not None
                else []
            )
            supporting_evidences = self.backend.retrieve(
                knowledge_base_name,
                "ICD connector pin mapping",
                top_k=config.settings.FINAL_TOP_K,
                ctx=ctx,
                filters={"source_names": list(snapshot.source_names)},
            )
            decision = build_icd_scope_decision(
                circuit_evidences,
                supporting_evidences,
            )
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
        candidate = self.document_generation.run_internal_harness(
            ctx,
            order.work_order_id,
            retrieve=retrieve,
        )
        return candidate

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
            query = query_override or " ".join(
                value
                for value in (
                    requirement.subject,
                    requirement.predicate,
                    requirement.object_hint,
                )
                if value
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

    @staticmethod
    def _frozen_icd_pin_evidence(
        kb_name: str,
        source_names: list[str],
        review: Any | None,
    ) -> list[Evidence]:
        mappings = effective_frozen_pin_mappings(review)
        if not mappings or not source_names:
            return []
        normalized_mappings = [
            {
                "refdes": str(mapping.get("refdes") or "").strip(),
                "pin_name": str(mapping.get("pin_name") or "").strip(),
                "net_name": str(mapping.get("net_name") or "").strip() or "NC",
            }
            for mapping in mappings
            if isinstance(mapping, dict)
            and str(mapping.get("refdes") or "").strip()
            and str(mapping.get("pin_name") or "").strip()
        ]
        if not normalized_mappings:
            return []
        source_name = source_names[0]
        pin_text = "; ".join(
            f"{mapping['refdes']}-{mapping['pin_name']} -> {mapping['net_name']}"
            for mapping in normalized_mappings
        )
        return [Evidence(
            id="frozen-icd-pin-set:" + "|".join(
                f"{mapping['refdes']}:{mapping['pin_name']}"
                for mapping in normalized_mappings
            ),
            content=f"Frozen ICD pin mappings: {pin_text}.",
            source_name=source_name,
            content_kind="circuit_design",
            processor_kind="icd_scope_review",
            score=1.0,
            metadata={
                "kb_name": kb_name,
                "source_group": "circuit_design",
                "pin_mappings": normalized_mappings,
                "frozen_icd_scope": True,
            },
        )]

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
                query = query_override or " ".join(
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
                return result

            outcome = self.project_retrieval.retrieve(ctx, requirement, snapshot_id, retrieve_one)
            query = query_override or " ".join(
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
            "scope_type": order.scope_type,
            "knowledge_base_name": order.knowledge_base_name,
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
