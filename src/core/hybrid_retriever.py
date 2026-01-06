from typing import List, Optional, Tuple
import jieba
import hashlib
from rank_bm25 import BM25Okapi
from llama_index.core import VectorStoreIndex, Settings
from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode
from llama_index.vector_stores.chroma import ChromaVectorStore
from config.settings import VECTOR_TOP_K, BM25_TOP_K, RRF_K, FINAL_TOP_K
from src.core.bm25_cache import BM25Cache
from src.core.logger import log, error, warn


def aggressive_tokenize(text: str) -> List[str]:
    if not text: return []
    text = text.lower()
    tokens = jieba.lcut(text)
    return [t for t in tokens if t.strip()]


def _compute_content_hash(texts: List[str]) -> str:
    """
    计算文档内容的哈希值，用于检测内容是否变化

    Args:
        texts: 文档文本列表

    Returns:
        MD5 哈希值
    """
    combined = "".join(sorted(texts))  # 排序后拼接，确保顺序无关
    return hashlib.md5(combined.encode('utf-8')).hexdigest()


def build_bm25_index(kb_name: str, index: VectorStoreIndex, force_rebuild: bool = False) -> Optional[
    Tuple[BM25Okapi, List[str]]]:
    """
    直接从 Chroma 构建 BM25 索引

    修复要点：
    1. 使用内容哈希检测变化，而非仅数量
    2. 缓存数据结构：(bm25, ids, content_hash)
    """
    cache = BM25Cache()
    vector_store = index._vector_store

    if not isinstance(vector_store, ChromaVectorStore):
        return None

    collection = vector_store._collection
    current_doc_count = collection.count()

    if current_doc_count == 0:
        return None

    # === 1. 缓存校验（使用内容哈希） ===
    if not force_rebuild:
        cached_data = cache.get(kb_name)
        if cached_data is not None:
            # 新的缓存格式：(bm25, cached_ids, content_hash)
            if len(cached_data) == 3:
                bm25, cached_ids, cached_hash = cached_data

                # 快速检查：数量不同直接重建
                if len(cached_ids) != current_doc_count:
                    log(f"文档数量变更 (缓存:{len(cached_ids)} vs DB:{current_doc_count}) -> 触发重建")
                else:
                    # ✅ 内容哈希校验
                    log(f"检查 BM25 缓存完整性: {kb_name}")
                    results = collection.get(limit=None, include=["documents"])
                    current_hash = _compute_content_hash(results.get("documents", []))

                    if current_hash == cached_hash:
                        try:
                            sample_ids = cached_ids[:min(10, len(cached_ids))]
                            collection.get(ids=sample_ids)
                            log(f"⚡ BM25 缓存有效")
                            return bm25, cached_ids
                        except Exception as e:
                            warn(f"缓存 ID 验证失败: {e}，触发重建")
                    else:
                        log(f"内容已变更 (哈希不匹配) -> 触发重建")
            else:
                # 旧格式缓存，直接重建
                log(f"检测到旧版缓存格式，触发重建")

    # === 2. 构建索引（直接从 Chroma 拉取） ===
    try:
        log(f"构建 BM25 索引: {kb_name} (Total: {current_doc_count} docs)")
        valid_docs_tokens = []
        valid_ids = []

        # 拉取全量数据
        results = collection.get(
            limit=None,
            include=["documents"]
        )

        docs_text = results.get("documents", [])
        ids = results.get("ids", [])

        # 分词处理
        for i, text in enumerate(docs_text):
            try:
                tokens = aggressive_tokenize(text)
                if tokens:
                    valid_docs_tokens.append(tokens)
                    valid_ids.append(ids[i])
            except Exception as e:
                warn(f"分词失败 (跳过): {e}")
                continue

        if not valid_ids:
            warn(f"知识库 {kb_name} 未能构建有效索引")
            return None

        # 构建 BM25
        bm25 = BM25Okapi(valid_docs_tokens)

        # ✅ 计算内容哈希
        content_hash = _compute_content_hash(docs_text)

        # 保存缓存（新格式）
        cache.set(kb_name, (bm25, valid_ids, content_hash))

        log(f"✅ BM25 索引构建完成，有效文档数: {len(valid_ids)}")
        log(f"   内容哈希: {content_hash[:8]}...")

        return bm25, valid_ids

    except Exception as e:
        error(f"❌ 构建 BM25 索引失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def hybrid_retrieve(
        query: str,
        index: VectorStoreIndex,
        kb_name: str,
        top_k: int = 20,
        vector_weight: float = 0.5,
        bm25_weight: float = 0.5
) -> List[NodeWithScore]:
    """混合检索"""

    # 1. 向量检索
    log(f"向量检索: {query[:20]}...")
    try:
        vector_retriever = index.as_retriever(similarity_top_k=VECTOR_TOP_K)
        vector_nodes = vector_retriever.retrieve(query)
        log(f"   └─ 向量检索返回: {len(vector_nodes)} 个结果")
    except Exception as e:
        error(f"向量检索失败: {e}")
        vector_nodes = []

    # 2. BM25 检索
    bm25_nodes = []
    bm25_data = build_bm25_index(kb_name, index)

    if bm25_data:
        # ✅ 适配新的返回格式
        if len(bm25_data) == 3:
            bm25, node_ids, _ = bm25_data
        else:
            bm25, node_ids = bm25_data

        try:
            query_tokens = aggressive_tokenize(query)
            if query_tokens:
                bm25_scores = bm25.get_scores(query_tokens)
                top_indices = sorted(range(len(bm25_scores)), key=lambda i: bm25_scores[i], reverse=True)[:BM25_TOP_K]

                # 批量获取需要的节点内容
                target_ids = [node_ids[i] for i in top_indices if bm25_scores[i] > 0.0]

                if target_ids:
                    collection = index._vector_store._collection
                    try:
                        results = collection.get(ids=target_ids, include=["documents", "metadatas"])
                    except Exception as get_err:
                        warn(f"部分 BM25 ID 无效，尝试逐个验证: {get_err}")
                        # 逐个验证 ID
                        valid_ids = []
                        for tid in target_ids:
                            try:
                                collection.get(ids=[tid])
                                valid_ids.append(tid)
                            except:
                                pass

                        if not valid_ids:
                            log("所有 BM25 ID 均无效，跳过 BM25 检索")
                            bm25_nodes = []
                            results = None
                        else:
                            results = collection.get(ids=valid_ids, include=["documents", "metadatas"])

                    # 组装 NodeWithScore
                    id_to_data = {
                        id_: (doc, meta)
                        for id_, doc, meta in zip(results['ids'], results['documents'], results['metadatas'])
                    }

                    for i in top_indices:
                        if i >= len(node_ids): continue
                        score = float(bm25_scores[i])
                        if score <= 0.0: continue

                        node_id = node_ids[i]
                        if node_id in id_to_data:
                            text, meta = id_to_data[node_id]
                            node = TextNode(text=text, id_=node_id, metadata=meta)
                            bm25_nodes.append(NodeWithScore(node=node, score=score))

                log(f"   └─ BM25 检索返回: {len(bm25_nodes)} 个结果")
        except Exception as e:
            error(f"BM25 计算出错: {e}")
            import traceback
            traceback.print_exc()

    # 3. RRF 融合
    if bm25_nodes:
        fused_nodes = rrf_fusion(vector_nodes, bm25_nodes, top_k, vector_weight, bm25_weight)
    else:
        fused_nodes = vector_nodes[:top_k]

    # 4. Reranker
    if Settings.node_postprocessors:
        try:
            query_bundle = QueryBundle(query_str=query)
            reranked_nodes = fused_nodes
            for processor in Settings.node_postprocessors:
                reranked_nodes = processor.postprocess_nodes(reranked_nodes, query_bundle=query_bundle)
            return reranked_nodes
        except Exception as e:
            error(f"Reranker 失败: {e}")
            return fused_nodes[:FINAL_TOP_K]

    return fused_nodes[:FINAL_TOP_K]


def rrf_fusion(vector_nodes, bm25_nodes, top_k, k=RRF_K, vector_weight=0.5, bm25_weight=0.5):
    scores = {}
    node_map = {}

    for rank, node in enumerate(vector_nodes, 1):
        node_id = node.node.node_id
        scores[node_id] = vector_weight / (k + rank)
        node_map[node_id] = node

    for rank, node in enumerate(bm25_nodes, 1):
        node_id = node.node.node_id
        if node_id in scores:
            scores[node_id] += bm25_weight / (k + rank)
        else:
            scores[node_id] = bm25_weight / (k + rank)
            node_map[node_id] = node

    sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)[:top_k]
    return [NodeWithScore(node=node_map[nid].node, score=scores[nid]) for nid in sorted_ids]


def invalidate_bm25_cache(kb_name: str) -> bool:
    cache = BM25Cache()
    return cache.delete(kb_name)