# src/core/rag_pipeline.py
import os
import shutil
import time
import traceback
from typing import List, Tuple, Generator
import config.settings
from src.ingestion.index_builder import get_or_build_index, invalidate_index_cache
from src.ingestion.data_loader import get_kb_path, list_knowledge_bases
from src.ingestion.docling_parser import parse_file
from src.core.hybrid_retriever import invalidate_bm25_cache
from src.core.logger import log, error, warn
from src.core.custom_rag_chat import CustomRAGChat
from src.core.resource_manager import resource_manager


class RAGPipeline:
    """
    RAG 核心业务流程管理
    """

    def __init__(self):
        try:
            # 初始化全局资源（模型、Chroma客户端等）
            if not resource_manager.initialize():
                raise RuntimeError("资源管理器初始化失败")
        except Exception as e:
            error(f"❌ RAGPipeline 初始化异常: {e}")
            raise
        os.makedirs(config.settings.DATA_ROOT, exist_ok=True)

    def get_index(self, kb_name: str):
        """获取索引实例，内部处理了双轨持久化加载逻辑"""
        return get_or_build_index(kb_name, resource_manager.chroma_client, use_cache=True)

    def list_knowledge_bases(self) -> List[str]:
        """列出所有知识库名称"""
        return list_knowledge_bases()

    def query(self, msg: str, kb_name: str, history: List[Tuple[str, str]]) -> Generator[str, None, None]:
        """
        问答入口 - 支持流式响应
        """
        if not msg.strip():
            yield "请输入有效问题"
            return
        if not kb_name:
            yield "❌ 未选择知识库"
            return
        try:
            # 获取索引并创建聊天引擎
            index = self.get_index(kb_name)
            chat_engine = CustomRAGChat(kb_name, index)

            # 使用 yield from 转发子生成器的流式输出
            yield from chat_engine.chat(msg, history)

        except Exception as e:
            error(f"查询出错: {e}")
            traceback.print_exc()
            yield f"❌ 系统错误: {str(e)}"

    def upload_files(self, files, target_kb: str) -> str:
        """批量上传并索引文件"""
        if not files: return "未选择文件"
        if not target_kb: return "❌ 未选择目标知识库"

        results, success_count = [], 0
        for file in files:
            file_path = file if isinstance(file, str) else file.name
            try:
                success, msg = self.add_document(file_path, target_kb)
                results.append(msg)
                if success:
                    success_count += 1
            except Exception as e:
                error(f"上传文件失败 {file_path}: {e}")
                results.append(f"❌ {os.path.basename(file_path)}: {str(e)}")

        return f"✅ 成功处理 {success_count}/{len(files)} 个文件\n" + "\n".join(results)

    def add_document(self, temp_file_path: str, kb_name: str) -> Tuple[bool, str]:
        """
        单文件索引逻辑：
        1. 物理存盘
        2. 统一 doc_id 为文件名并入库
        3. 同步持久化 Docstore
        Returns:
            Tuple[bool, str]: (是否成功, 描述信息)
        """
        lock = resource_manager.get_kb_lock(kb_name)
        with lock:
            try:
                if not os.path.exists(temp_file_path):
                    return False, "❌ 临时文件不存在"

                filename = os.path.basename(temp_file_path)
                target_dir = get_kb_path(kb_name)
                os.makedirs(target_dir, exist_ok=True)
                target_path = os.path.join(target_dir, filename)

                # 处理物理文件重名逻辑
                if os.path.exists(target_path):
                    base, ext = os.path.splitext(filename)
                    filename = f"{base}_{int(time.time())}{ext}"
                    target_path = os.path.join(target_dir, filename)

                shutil.copy2(temp_file_path, target_path)

                log(f"开始解析文件: {filename}")

                # 获取索引实例
                index = self.get_index(kb_name)

                # 使用 Docling 进行布局感知解析和语义分块
                nodes = parse_file(target_path, filename, kb_name)

                if not nodes:
                    raise ValueError("文件解析后未生成任何有效节点")

                log(f"解析成功，准备写入 {len(nodes)} 个节点到 {kb_name}")

                # 显式写入 docstore
                index.docstore.add_documents(nodes)

                # 写入向量库(Chroma)
                index.insert_nodes(nodes)

                persist_dir = config.settings.get_kb_storage_path(kb_name)  # 获取路径,持久化的是文档
                index.storage_context.persist(persist_dir=persist_dir)  # 文档持久化

                # 清理相关缓存
                invalidate_bm25_cache(kb_name)
                invalidate_index_cache(kb_name)

                log(f"✅ 索引及同步持久化成功: {filename} (KB: {kb_name})")
                return True, f"✅ 索引成功: {filename}"
            except Exception as e:
                error(f"❌ 上传文档处理失败: {e}")
                if 'target_path' in locals() and os.path.exists(target_path):
                    try:
                        os.remove(target_path)
                    except Exception as cleanup_error:
                        error(f"清理临时文件失败: {target_path}, 错误: {cleanup_error}")
                raise e

    def delete_document(self, filename: str, kb_name: str) -> str:
        """
        删除知识库中的文档
        """
        lock = resource_manager.get_kb_lock(kb_name)
        with lock:
            if not filename:
                return "❌ 文件名不能为空"

            try:
                index = self.get_index(kb_name)

                # 查找所有属于该文件的节点的 ref_doc_id
                # 一个文件通常对应一个 ref_doc_id
                all_nodes = list(index.docstore.docs.values())
                target_ref_doc_ids = set()  # 使用 set 来自动去重
                for node in all_nodes:
                    if node.metadata.get("file_name") == filename:
                        if node.ref_doc_id:  # 确保 ref_doc_id 存在
                            target_ref_doc_ids.add(node.ref_doc_id)

                if not target_ref_doc_ids:
                    warn(f"在索引中未找到与文件 '{filename}' 关联的文档。可能已被删除或从未索引。")
                    # 即使索引中没有，也尝试删除物理文件
                    file_path = os.path.join(get_kb_path(kb_name), filename)
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        return f"✅ 索引中无记录，已删除物理文件: {filename}"
                    return "索引和物理路径中均未找到该文件。"

                # 批量删除节点
                for ref_doc_id in target_ref_doc_ids:
                    index.delete_ref_doc(ref_doc_id, delete_from_store=True)
                    log(f"从索引中删除 ref_doc_id: {ref_doc_id}")

                # 持久化变更
                persist_dir = config.settings.get_kb_storage_path(kb_name)
                index.storage_context.persist(persist_dir=persist_dir)

                # 删除物理文件
                file_path = os.path.join(get_kb_path(kb_name), filename)
                if os.path.exists(file_path):
                    os.remove(file_path)

                # 清理缓存
                invalidate_index_cache(kb_name)
                invalidate_bm25_cache(kb_name)

                log(f"✅ 文档已从物理磁盘和索引库中同步移除: {filename}")
                return f"✅ 已成功删除文档: {filename}"

            except Exception as e:
                error(f"❌ 删除文档失败: {e}")
                traceback.print_exc()
                return f"❌ 删除失败: {str(e)}"

    def create_kb(self, name: str) -> Tuple[bool, str]:
        """
        创建新知识库
        注意：索引会在首次查询时自动构建
        """
        try:
            name = name.strip().replace(" ", "_")
            if not name:
                return False, "❌ 名称不能为空"
            path = get_kb_path(name)
            if os.path.exists(path):
                return False, "❌ 知识库已存在"

            os.makedirs(path, exist_ok=True)
            log(f"✅ 知识库 '{name}' 创建成功（索引将在首次查询时自动初始化）")
            return True, f"✅ 知识库 '{name}' 创建成功"
        except Exception as e:
            error(f"创建知识库失败: {e}")
            return False, str(e)

    def delete_knowledge_base(self, kb_name: str) -> Tuple[bool, str]:
        """
        彻底删除知识库（含物理文件、向量库、元数据、缓存）
        修复要点：增加完整性校验
        """
        if kb_name == config.settings.DEFAULT_KB_NAME:
            return False, "❌ 不可删除默认知识库"

        lock = resource_manager.get_kb_lock(kb_name)
        with lock:
            log(f"准备彻底删除知识库: {kb_name}")
            errors = []

            try:
                # === 1. 删除 Chroma Collection ===
                try:
                    resource_manager.chroma_client.delete_collection(name=f"kb_{kb_name}")
                    log(f"✅ 已删除 Chroma Collection")
                except Exception as e:
                    errors.append(f"Chroma: {e}")

                # === 2. 删除源文件目录 ===
                kb_data_path = get_kb_path(kb_name)
                if os.path.exists(kb_data_path):
                    try:
                        shutil.rmtree(kb_data_path)
                        log(f"✅ 已删除源文件目录")
                    except Exception as e:
                        errors.append(f"源文件: {e}")

                # === 3. 删除持久化元数据目录 ===
                persist_dir = config.settings.get_kb_storage_path(kb_name)
                if os.path.exists(persist_dir):
                    try:
                        shutil.rmtree(persist_dir)
                        log(f"✅ 已删除 Docstore 目录")
                    except Exception as e:
                        errors.append(f"Docstore: {e}")

                # === 4. 清理缓存 ===
                invalidate_index_cache(kb_name)
                invalidate_bm25_cache(kb_name)

                if errors:
                    warn(f"删除过程中出现部分错误: {'; '.join(errors)}")
                    return True, f"⚠️ 知识库 '{kb_name}' 已删除（部分清理失败）"

                return True, f"✅ 知识库 '{kb_name}' 已被彻底删除"

            except Exception as e:
                error(f"删除知识库失败: {e}")
                return False, f"❌ 删除失败: {str(e)}"

    def list_files(self, kb_name: str) -> List[str]:
        """列出知识库内的源文件"""
        if not kb_name: return []
        kb_path = get_kb_path(kb_name)
        if not os.path.exists(kb_path): return []
        return sorted([f for f in os.listdir(kb_path) if os.path.isfile(os.path.join(kb_path, f))])
