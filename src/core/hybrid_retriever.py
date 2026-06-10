from typing import List, Optional, Tuple
import traceback
import jieba
from rank_bm25 import BM25Okapi
from llama_index.core import VectorStoreIndex, Settings
from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode
from llama_index.vector_stores.chroma import ChromaVectorStore
import config.settings
from src.core.bm25_cache import BM25Cache
from src.core.logger import log, error, warn


def _get_chroma_collection(index: VectorStoreIndex):
    """安全获取 ChromaDB collection 对象，收敛私有属性访问"""
    vector_store = index._vector_store
    if not isinstance(vector_store, ChromaVectorStore):
        return None
    return vector_store._collection


def aggressive_tokenize(text: str) -> List[str]:
    if not text: return []
    text = text.lower()
    tokens = jieba.lcut(text)
    return [t for t in tokens if t.strip()]


def build_bm25_index(kb_name: str, index: VectorStoreIndex, force_rebuild: bool = False) -> Optional[
    Tuple[BM25Okapi, List[str]]]:
    """
    从 Chroma 构建 BM25 索引，带轻量级缓存校验
    缓存格式：(bm25, ids, doc_count)
    校验策略：数量比对 + 抽样 ID 验证（避免全量拉取文档计算哈希）
    """
    cache = BM25Cache()
    collection = _get_chroma_collection(index)
    if collection is None:
        return None
    current_doc_count = collection.count()

    if current_doc_count == 0:
        return None

    # === 1. 缓存校验（数量 + ID 集合比对） ===
    if not force_rebuild:
        cached_data = cache.get(kb_name)
        if cached_data is not None and len(cached_data) == 3:
            bm25, cached_ids, cached_count = cached_data

            if cached_count != current_doc_count:
                log(f"文档数量变更 (缓存:{cached_count} vs DB:{current_doc_count}) -> 触发重建")
            else:
                # 比对缓存 ID 集合与数据库实际 ID 集合
                try:
                    results = collection.get(limit=None, include=[])
                    current_ids = set(results.get("ids", []))
                    if set(cached_ids) == current_ids:
                        log(f"⚡ BM25 缓存有效")
                        return bm25, cached_ids
                    else:
                        log(f"文档 ID 集合变更 -> 触发重建")
                except Exception as e:
                    warn(f"缓存校验失败: {e}，触发重建")
        elif cached_data is not None:
            log(f"检测到旧版缓存格式，触发重建")

    # === 2. 构建索引 ===
    try:
        log(f"构建 BM25 索引: {kb_name} (Total: {current_doc_count} docs)")
        valid_docs_tokens = []
        valid_ids = []

        results = collection.get(limit=None, include=["documents", "metadatas"])
        docs_text = results.get("documents", [])
        metadatas = results.get("metadatas", [])
        ids = results.get("ids", [])

        for i, text in enumerate(docs_text):
            try:
                metadata = metadatas[i] if i < len(metadatas) and metadatas[i] else {}
                searchable_text = f"{text}\n{metadata.get('source_group', '')}\n{metadata.get('file_name', '')}"
                tokens = aggressive_tokenize(searchable_text)
                if tokens:
                    valid_docs_tokens.append(tokens)
                    valid_ids.append(ids[i])
            except Exception as e:
                warn(f"分词失败 (跳过): {e}")

        if not valid_ids:
            warn(f"知识库 {kb_name} 未能构建有效索引")
            return None

        bm25 = BM25Okapi(valid_docs_tokens)
        cache.set(kb_name, (bm25, valid_ids, current_doc_count))

        log(f"✅ BM25 索引构建完成，有效文档数: {len(valid_ids)}")
        return bm25, valid_ids

    except Exception as e:
        error(f"❌ 构建 BM25 索引失败: {e}")
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
        vector_retriever = index.as_retriever(similarity_top_k=config.settings.VECTOR_TOP_K)
        vector_nodes = vector_retriever.retrieve(query)
        log(f"   └─ 向量检索返回: {len(vector_nodes)} 个结果")
    except Exception as e:
        error(f"向量检索失败: {e}")
        vector_nodes = []

    # 2. BM25 检索
    bm25_nodes = []
    bm25_data = build_bm25_index(kb_name, index)

    if bm25_data:
        bm25, node_ids = bm25_data[0], bm25_data[1]

        try:
            query_tokens = aggressive_tokenize(query)
            if query_tokens:
                bm25_scores = bm25.get_scores(query_tokens)
                top_indices = sorted(range(len(bm25_scores)), key=lambda i: bm25_scores[i], reverse=True)[:config.settings.BM25_TOP_K]

                # 批量获取需要的节点内容
                target_ids = [node_ids[i] for i in top_indices if bm25_scores[i] > 0.0]

                if target_ids:
                    collection = _get_chroma_collection(index)
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
                            except Exception:
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
            traceback.print_exc()

    # 3. RRF 融合
    if bm25_nodes:
        fused_nodes = rrf_fusion(
            vector_nodes,
            bm25_nodes,
            top_k,
            vector_weight=vector_weight,
            bm25_weight=bm25_weight,
        )
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
            return fused_nodes[:top_k]

    return fused_nodes[:top_k]


def rrf_fusion(vector_nodes, bm25_nodes, top_k, k=None, vector_weight=0.5, bm25_weight=0.5):
    if k is None:
        k = config.settings.RRF_K
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
