# src/core/custom_rag_chat.py
from typing import List, Tuple, Generator
from llama_index.core import Settings
from llama_index.core.llms import ChatMessage, MessageRole
from src.core.hybrid_retriever import hybrid_retrieve
from src.core.logger import log, error
import hashlib
import re


class CustomRAGChat:
    """
    自定义 RAG 聊天实现
    """

    def __init__(self, kb_name: str, index):
        self.kb_name = kb_name
        self.index = index
        self._context_cache = {}  # 上下文缓存：{query_hash: context}
        self._cache_size_limit = 10  # 最多缓存 10 条

    def _get_query_hash(self, query: str) -> str:
        """生成查询的哈希值（用于缓存）"""
        return hashlib.md5(query.strip().lower().encode()).hexdigest()

    def retrieve_context(self, query: str, top_k: int = 5) -> Tuple[str, str]:
        """
        检索相关上下文（带缓存）
        Returns:
            Tuple[str, str]: (用于Prompt的纯文本上下文, 用于UI显示的带格式上下文)
        """
        query_hash = self._get_query_hash(query)
        if query_hash in self._context_cache:
            log(f"⚡ 使用缓存的上下文: {query[:30]}...")
            return self._context_cache[query_hash]

        retrieved_nodes = hybrid_retrieve(query, self.index, self.kb_name, top_k)

        if not retrieved_nodes:
            return "", ""

        context_parts, display_parts = [], []
        for i, node in enumerate(retrieved_nodes, 1):
            content = node.node.get_content().strip()
            file_name = node.node.metadata.get('file_name', '未知来源')
            score = node.score if node.score else 0.0
            context_parts.append(f"【来源: {file_name}】\n{content}")
            safe_content = content[:200].replace('\n', ' ')
            display_parts.append(f"【来源 {i}: {file_name} | 分数: {score:.4f}】\n{safe_content}...")

        context, display_context = "\n\n".join(context_parts), "\n\n".join(display_parts)
        log("=" * 50)
        log(f"🔍 [RAG 检索详情] Query: {query}")
        log(f"📄 检索到 {len(retrieved_nodes)} 个片段")
        log("=" * 50)

        result = (context, display_context)
        self._context_cache[query_hash] = result
        if len(self._context_cache) > self._cache_size_limit:
            oldest_key = next(iter(self._context_cache))
            del self._context_cache[oldest_key]
        return result

    def chat(self, user_input: str, history: List[Tuple[str, str]], max_history: int = 5) -> Generator[str, None, None]:
        """主聊天方法 - 返回流式响应的生成器"""
        if not user_input.strip():
            yield "请输入有效的问题"
            return

        context, display_context_str = self.retrieve_context(user_input)

        if not context:
            log("⚠️ 未检索到相关内容，将仅基于模型知识回答")
            context = "（知识库中没有找到相关上下文，请基于你自己的知识回答，并告知用户知识库中无相关信息）"

        system_content = (
            "你是一个专业的硬件技术助手,你的名字叫小智。请严格基于下方的【参考资料】回答用户问题。\n"
            "规则：\n"
            "1. 如果【参考资料】包含答案，请详细回答。\n"
            "2. 如果【参考资料】内容不足或无关，请明确说明'知识库中未找到相关信息'，不要编造。\n"
            "3. 回答必须使用中文。\n\n"
            f"### 参考资料 ###\n{context}"
        )
        messages = [ChatMessage(role=MessageRole.SYSTEM, content=system_content)]
        for user_msg, bot_msg in history[-max_history:]:
            clean_bot_msg = re.split(r'\n\n---\n\n\*\*🔍 检索到的上下文:\*\*', bot_msg)[0]
            clean_bot_msg = re.sub(r'<[^>]+>', '', clean_bot_msg)
            messages.append(ChatMessage(role=MessageRole.USER, content=user_msg))
            messages.append(ChatMessage(role=MessageRole.ASSISTANT, content=clean_bot_msg))
        messages.append(ChatMessage(role=MessageRole.USER, content=user_input))

        try:
            response_stream = Settings.llm.stream_chat(messages)

            llm_response_content = []
            for chunk in response_stream:
                content_delta = chunk.delta or ""
                llm_response_content.append(content_delta)
                yield content_delta

            content = "".join(llm_response_content)
            log("=" * 50)
            log(f"🤖 [LLM 生成详情]\n{content}")
            log("=" * 50)

            if display_context_str and "知识库中未找到相关信息" not in content:
                final_response_suffix = f"\n\n---\n\n**🔍 检索到的上下文:**\n{display_context_str}"
                yield final_response_suffix

        except Exception as e:
            error(f"LLM生成响应失败: {e}")
            import traceback
            traceback.print_exc()
            yield "抱歉，生成响应时出现错误，请稍后重试。"
