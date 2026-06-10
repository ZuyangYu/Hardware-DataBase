# src/core/custom_rag_chat.py
from typing import List, Tuple, Generator
import hashlib
import re
import traceback
from llama_index.core import Settings
from llama_index.core.llms import ChatMessage, MessageRole
from src.core.routed_retriever import routed_retrieve
from src.core.logger import log, error
import config.settings


_context_cache: dict[str, tuple[str, str]] = {}
_CONTEXT_CACHE_LIMIT = 20


def _get_query_hash(query: str) -> str:
    return hashlib.md5(query.strip().lower().encode()).hexdigest()


class CustomRAGChat:
    """
    自定义 RAG 聊天实现
    """

    def __init__(self, kb_name: str, index):
        self.kb_name = kb_name
        self.index = index

    def retrieve_context(self, query: str, top_k: int = 5) -> Tuple[str, str]:
        """
        检索相关上下文（带缓存）
        Returns:
            Tuple[str, str]: (用于Prompt的纯文本上下文, 用于UI显示的带格式上下文)
        """
        cache_key = f"{self.kb_name}:{_get_query_hash(query)}"
        if cache_key in _context_cache:
            log(f"⚡ 使用缓存的上下文: {query[:30]}...")
            return _context_cache[cache_key]

        retrieved_nodes = routed_retrieve(query, self.index, self.kb_name, top_k)

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
        _context_cache[cache_key] = result
        if len(_context_cache) > _CONTEXT_CACHE_LIMIT:
            oldest_key = next(iter(_context_cache))
            del _context_cache[oldest_key]
        return result

    def chat(self, user_input: str, history: List[Tuple[str, str]], max_history: int = 5) -> Generator[str, None, None]:
        """主聊天方法 - 返回流式响应的生成器"""
        if not user_input.strip():
            yield "请输入有效的问题"
            return

        context, display_context_str = self.retrieve_context(user_input)

        if not context:
            log("⚠️ 未检索到相关内容，将仅基于模型知识回答")
            context = config.settings.NO_CONTEXT_PROMPT

        system_content = (
            f"{config.settings.SYSTEM_PROMPT}\n\n"
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
            traceback.print_exc()
            yield "抱歉，生成响应时出现错误，请稍后重试。"
