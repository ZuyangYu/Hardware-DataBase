# src/core/rag_pipeline.py
import os
import shutil
import traceback
from typing import Generator, List, Tuple

import config.settings
from src.core.auth import AuthService
from src.core.hybrid_retriever import invalidate_bm25_cache
from src.core.logger import error, log, warn
from src.core.resource_manager import resource_manager
from src.ingestion.data_loader import get_kb_path, list_knowledge_bases
from src.ingestion.index_builder import get_or_build_index, invalidate_index_cache
from src.ingestion.kb_paths import InvalidKnowledgeBaseName, validate_kb_name
from src.rag_backends.factory import create_rag_backend
from src.rag_backends.schemas import RequestContext


class RAGPipeline:
    """RAG 核心业务流程管理。"""

    def __init__(self):
        try:
            os.makedirs(config.settings.DATA_ROOT, exist_ok=True)
            self.backend = create_rag_backend()
        except Exception as e:
            error(f"RAGPipeline 初始化异常: {e}")
            raise

    def get_index(self, kb_name: str):
        """获取索引实例，内部处理双轨持久化加载逻辑。"""
        return get_or_build_index(kb_name, resource_manager.chroma_client, use_cache=True)

    def list_knowledge_bases(self, ctx: RequestContext | None = None) -> List[str]:
        """列出当前用户可访问的知识库名称。"""
        kbs = list_knowledge_bases()
        if ctx is None:
            return kbs
        user = AuthService().get_user_by_username(ctx.user_id)
        if user is None:
            return []
        return AuthService().list_accessible_kbs(user, kbs)

    def list_all_knowledge_bases_for_admin(self, ctx: RequestContext | None = None) -> List[str]:
        """列出全部知识库，仅用于系统级管理界面，不授予内容读写权限。"""
        if ctx is None or not ctx.is_system_admin():
            return []
        return list_knowledge_bases()

    def query(
        self,
        msg: str,
        kb_name: str,
        history: List[Tuple[str, str]],
        ctx: RequestContext | None = None,
    ) -> Generator[str, None, None]:
        """问答入口，支持流式响应。"""
        if not msg.strip():
            yield "请输入有效问题"
            return
        if not kb_name:
            yield "未选择知识库"
            return
        try:
            yield from self.backend.stream_answer(kb_name, msg, history, ctx=ctx)
        except Exception as e:
            error(f"查询出错: {e}")
            traceback.print_exc()
            yield f"系统错误: {str(e)}"

    def upload_files(
        self,
        files,
        target_kb: str,
        ctx: RequestContext | None = None,
        source_group: str | None = None,
    ) -> str:
        """批量上传并自动提交后台解析任务。"""
        if not files:
            return "未选择文件"
        if not target_kb:
            return "未选择目标知识库"

        file_paths = [file if isinstance(file, str) else file.name for file in files]
        if hasattr(self.backend, "submit_parse_tasks"):
            tasks = self.backend.submit_parse_tasks(target_kb, file_paths, source_group=source_group, ctx=ctx)
            return f"已创建 {len(tasks)} 个解析任务，系统将自动开始解析"
        return self.backend.ingest(target_kb, file_paths, ctx=ctx, source_group=source_group).to_message()

    def add_document(self, temp_file_path: str, kb_name: str) -> Tuple[bool, str]:
        """单文件索引逻辑，返回是否成功和描述信息。"""
        result = self.backend.ingest(kb_name, [temp_file_path])
        message = result.messages[0] if result.messages else ""
        return result.success_count == 1, message

    def delete_document(self, filename: str, kb_name: str, ctx: RequestContext | None = None) -> str:
        """删除知识库中的文档。"""
        return self.backend.delete_document(kb_name, filename, ctx=ctx).message

    def create_kb(self, name: str, ctx: RequestContext | None = None) -> Tuple[bool, str]:
        """创建新知识库，索引会在首次查询时自动构建。"""
        try:
            name = validate_kb_name(name.strip().replace(" ", "_"))
            if not name:
                return False, "名称不能为空"
            if ctx is not None and "anonymous" in ctx.roles:
                return False, "权限不足：请先登录再创建知识库。"
            if ctx is not None and ctx.is_system_admin():
                return False, "系统管理员不能创建内容知识库，请由部门管理员创建。"
            path = get_kb_path(name)
            if os.path.exists(path):
                return False, "知识库已存在"

            os.makedirs(path, exist_ok=True)
            if ctx and ctx.user_id:
                auth_service = AuthService()
                owner = auth_service.get_user_by_username(ctx.user_id)
                auth_service.register_knowledge_base(name, owner=owner)
            log(f"知识库 '{name}' 创建成功，索引将在首次查询时自动初始化")
            return True, f"知识库 '{name}' 创建成功"
        except Exception as e:
            error(f"创建知识库失败: {e}")
            return False, str(e)

    def delete_knowledge_base(self, kb_name: str, ctx: RequestContext | None = None) -> Tuple[bool, str]:
        """彻底删除知识库，包括源文件、向量库、元数据和缓存。"""
        try:
            kb_name = validate_kb_name(kb_name)
        except InvalidKnowledgeBaseName as exc:
            return False, str(exc)

        if ctx is not None and not ctx.has_kb_permission(kb_name, "admin"):
            return False, "权限不足：删除知识库需要 admin 权限。"

        lock = resource_manager.get_kb_lock(kb_name)
        with lock:
            log(f"准备彻底删除知识库: {kb_name}")
            errors = []

            try:
                try:
                    resource_manager.chroma_client.delete_collection(name=f"kb_{kb_name}")
                    log("已删除 Chroma Collection")
                except Exception as e:
                    errors.append(f"Chroma: {e}")

                kb_data_path = get_kb_path(kb_name)
                if os.path.exists(kb_data_path):
                    try:
                        shutil.rmtree(kb_data_path)
                        log("已删除源文件目录")
                    except Exception as e:
                        errors.append(f"源文件: {e}")

                persist_dir = config.settings.get_kb_storage_path(kb_name)
                if os.path.exists(persist_dir):
                    try:
                        shutil.rmtree(persist_dir)
                        log("已删除 Docstore 目录")
                    except Exception as e:
                        errors.append(f"Docstore: {e}")

                invalidate_index_cache(kb_name)
                invalidate_bm25_cache(kb_name)
                AuthService().delete_knowledge_base_record(kb_name)

                if errors:
                    warn(f"删除过程中出现部分错误: {'; '.join(errors)}")
                    return True, f"知识库 '{kb_name}' 已删除，部分清理失败。"

                return True, f"知识库 '{kb_name}' 已被彻底删除"

            except Exception as e:
                error(f"删除知识库失败: {e}")
                return False, f"删除失败: {str(e)}"

    def list_files(self, kb_name: str, ctx: RequestContext | None = None) -> List[str]:
        """列出知识库内的源文件。"""
        return [doc.name for doc in self.backend.list_documents(kb_name, ctx=ctx)]

    def list_file_infos(self, kb_name: str, ctx: RequestContext | None = None):
        return self.backend.list_documents(kb_name, ctx=ctx)

    def list_parse_tasks(self, kb_name: str | None = None, ctx: RequestContext | None = None):
        if hasattr(self.backend, "list_parse_tasks"):
            return self.backend.list_parse_tasks(kb_name, ctx=ctx)
        return []

    def pause_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> str:
        if hasattr(self.backend, "pause_parse_task"):
            return self.backend.pause_parse_task(task_id, ctx=ctx).message
        return "当前后端不支持暂停解析任务"

    def resume_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> str:
        if hasattr(self.backend, "resume_parse_task"):
            return self.backend.resume_parse_task(task_id, ctx=ctx).message
        return "当前后端不支持启动解析任务"

    def delete_parse_task(self, task_id: str, ctx: RequestContext | None = None) -> str:
        if hasattr(self.backend, "delete_parse_task"):
            return self.backend.delete_parse_task(task_id, ctx=ctx).message
        return "当前后端不支持删除解析任务"

    def clear_finished_parse_tasks(self, kb_name: str | None = None, ctx: RequestContext | None = None):
        if hasattr(self.backend, "clear_finished_parse_tasks"):
            self.backend.clear_finished_parse_tasks(kb_name, ctx=ctx)

    def get_parse_result(self, kb_name: str, document_id: str, ctx: RequestContext | None = None):
        return self.backend.get_parse_result(kb_name, document_id, ctx=ctx)
