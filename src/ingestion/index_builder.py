# src/ingestion/index_builder.py
import os
import threading
from typing import Optional, Dict
from llama_index.core import VectorStoreIndex, StorageContext, load_index_from_storage
from llama_index.core.schema import TextNode
from llama_index.vector_stores.chroma import ChromaVectorStore
from src.core.logger import log, error, warn
from src.core.resource_manager import resource_manager
from config.settings import get_kb_storage_path
from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.core.storage.index_store import SimpleIndexStore

class _IndexCache:
    def __init__(self):
        self._cache: Dict[str, VectorStoreIndex] = {}
        self._lock = threading.RLock()

    def get(self, kb_name: str) -> Optional[VectorStoreIndex]:
        with self._lock:
            return self._cache.get(kb_name)

    def set(self, kb_name: str, index: VectorStoreIndex):
        with self._lock:
            self._cache[kb_name] = index

    def invalidate(self, kb_name: str):
        with self._lock:
            if kb_name in self._cache:
                del self._cache[kb_name]


_index_cache = _IndexCache()


def _rebuild_docstore_from_chroma(collection, vector_store) -> VectorStoreIndex:
    """
    从 Chroma 重建 Docstore

    这是一个独立的辅助函数，只在 docstore 损坏/丢失且 Chroma 有数据时调用

    Args:
        collection: Chroma collection 对象
        vector_store: ChromaVectorStore 实例

    Returns:
        重建后的索引
    """
    chroma_count = collection.count()

    # 1. 批量拉取所有数据
    results = collection.get(limit=None, include=["documents", "metadatas", "embeddings"])
    # 2. 重建节点
    nodes = []

    for i, (doc_id, text, meta, embedding) in enumerate(zip(
            results["ids"],
            results["documents"],
            results["metadatas"],
            results.get("embeddings", [None] * len(results["ids"]))
    )):
        node = TextNode(
            text=text,
            id_=doc_id,
            metadata=meta or {},
            embedding=embedding  # 保留嵌入信息
        )
        nodes.append(node)

        if (i + 1) % 100 == 0:
            log(f"   进度: {i + 1}/{chroma_count} 个节点")

    # 3. 创建索引（会正确初始化 docstore）
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    # 1. 创建空索引（连接到现有的 vector_store）
    index = VectorStoreIndex.from_vector_store(
        vector_store=vector_store,
        storage_context=storage_context
    )

    # 2. 手动填充 docstore（不插入向量，因为 Chroma 已有）
    for node in nodes:
        index.docstore.add_documents([node], allow_update=True)

    log(f"✅ Docstore 重建完成: {len(index.docstore.docs)} 个节点")

    return index


def get_or_build_index(kb_name: str, chroma_client, use_cache: bool = True) -> VectorStoreIndex:
    """
    获取或构建索引：支持 Chroma + Docstore 双轨持久化

    修复要点：
    1. 当 docstore 丢失时，从 Chroma 完整重建
    2. 确保 docstore 和向量库的强一致性
    3. 空库使用标准构造函数，避免 docstore 初始化不完整
    """
    if use_cache:
        cached_index = _index_cache.get(kb_name)
        if cached_index is not None:
            return cached_index

    lock = resource_manager.get_kb_lock(kb_name)
    with lock:
        # 双重检查
        if use_cache:
            cached_index = _index_cache.get(kb_name)
            if cached_index is not None:
                return cached_index

        try:
            persist_dir = get_kb_storage_path(kb_name)
            coll_name = f"kb_{kb_name}"
            collection = chroma_client.get_or_create_collection(coll_name)
            vector_store = ChromaVectorStore(chroma_collection=collection)

            docstore_path = os.path.join(persist_dir, "docstore.json")

            # === 场景1: Docstore 存在 - 直接加载 ===
            if os.path.exists(docstore_path):
                try:
                    log(f"📂 加载已有索引: {kb_name}")
                    storage_context = StorageContext.from_defaults(
                        vector_store=vector_store,
                        persist_dir=persist_dir
                    )
                    index = load_index_from_storage(storage_context)

                    # ✅ 校验一致性
                    docstore_count = len(index.docstore.docs)
                    chroma_count = collection.count()

                    if docstore_count == chroma_count:
                        log(f"✅ 索引加载成功，共 {docstore_count} 个节点")
                        if use_cache:
                            _index_cache.set(kb_name, index)
                        return index
                    else:
                        # 数据不一致，需要重建
                        warn(f"数据不一致! Docstore:{docstore_count} vs Chroma:{chroma_count}")
                        warn(f"触发完整重建...")
                        raise ValueError("数据不一致")

                except Exception as e:
                    error(f"❌ 加载索引失败: {e}")
                    # 继续执行下面的重建逻辑
                    pass

            # === 场景2: Docstore 不存在 - 根据 Chroma 状态决定策略 ===
            chroma_count = collection.count()
            if chroma_count == 0:
                # ✅ 情况A: 空库 - 直接创建标准空索引
                log(f"✨ 创建新知识库索引: {kb_name}")

                # 关键：使用 VectorStoreIndex
                # 构建索引
                storage_context = StorageContext.from_defaults(
                    vector_store=vector_store,
                    docstore=SimpleDocumentStore(),
                    index_store=SimpleIndexStore()
                )

                # 使用标准构造函数，传入空列表 [] 而不是 from_vector_store
                index = VectorStoreIndex(
                    nodes=[],
                    storage_context=storage_context
                )

                # 立即执行一次持久化，强制生成非空的初始 JSON 结构
                index.storage_context.persist(persist_dir=persist_dir)

                log(f"✅ 空索引创建完成")
                log(f"   └─ Docstore 初始化: {len(index.docstore.docs)} 个节点")
            else:
                # ✅ 情况B: Chroma 有数据但 docstore 丢失 - 重建
                log(f"🔧 检测到 Chroma 有 {chroma_count} 个节点，但 docstore 丢失")
                log(f"开始从 Chroma 重建...")
                index = _rebuild_docstore_from_chroma(collection, vector_store)
                log(f"✅ Docstore 重建完成")

            # === 持久化到磁盘 ===
            try:
                index.storage_context.persist(persist_dir=persist_dir)
                log(f"💾 索引已持久化: {persist_dir}")
            except Exception as e:
                error(f"❌ 持久化失败: {e}")
                # 不抛出异常，允许继续使用内存中的索引

            if use_cache:
                _index_cache.set(kb_name, index)
            return index

        except Exception as e:
            error(f"❌ 索引加载失败: {kb_name} - {e}")
            raise


def invalidate_index_cache(kb_name: str):
    """清除索引缓存"""
    _index_cache.invalidate(kb_name)
