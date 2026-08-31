"""LLM-assisted structure inference for external conversations.

Only used for marker-less text (no 用户:/assistant:/Q:/A: markers found by the
deterministic parser). Strictly fail-open: any error, garbage output or
disabled setting returns ``None`` and the caller keeps the plain-blocks
fallback. One LLM call per upload, bounded by a character cap.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

import src.settings
from src.external_conversations.models import ConversationTurn

# Single-worker pool: LLM sockets cannot be force-killed on timeout, but the
# caller stops waiting after EXTERNAL_CONVERSATION_LLM_TIMEOUT_SECONDS and the
# orphaned call is discarded (bounded by the client's own AGENT_TIMEOUT).
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ext-conv-llm")

_PROMPT = """你是对话记录结构化解析器。下面是一段没有明确角色标记的文本，请推断其中的对话轮次。

规则：
- role 只能是 "user"（提问/求助的一方）或 "assistant"（回答/解释的一方）。
- 按原文顺序切分，不要合并跨主题的内容，也不要编造原文中不存在的句子。
- 无法判断归属的行并入相邻轮次；确实与对话无关的独立内容可以省略。
- title：给这段对话起一个不超过 20 字的中文标题。
- 只输出 JSON 对象，格式：
{{"title": "...", "turns": [{{"role": "user", "content": "..."}}, {{"role": "assistant", "content": "..."}}]}}
不要输出任何解释、markdown 代码块或多余文本。

文本：
{body}"""

_MAX_TURNS = 200


def llm_structure_enabled() -> bool:
    return bool(getattr(src.settings, "EXTERNAL_CONVERSATION_LLM_STRUCTURE", False))


def llm_summary_enabled() -> bool:
    return bool(getattr(src.settings, "EXTERNAL_CONVERSATION_LLM_SUMMARY", False))


def _chat_with_timeout(llm_client, prompt: str) -> str | None:
    """One LLM round-trip bounded by EXTERNAL_CONVERSATION_LLM_TIMEOUT_SECONDS.

    Returns raw text or None on timeout/error."""
    timeout = float(getattr(src.settings, "EXTERNAL_CONVERSATION_LLM_TIMEOUT_SECONDS", 60))
    try:
        future = _EXECUTOR.submit(
            llm_client.chat,
            [{"role": "user", "content": prompt}],
            usage_stage="external_conversation_llm",
        )
        return future.result(timeout=timeout)
    except FuturesTimeout:
        return None
    except Exception:
        return None


def _strip_fences(raw: str) -> str:
    text = str(raw or "").strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def _extract_json_object(raw: str) -> dict | None:
    text = _strip_fences(raw)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def infer_structure(text: str, llm_client=None) -> dict | None:
    """Return ``{"title": str|None, "turns": [ConversationTurn]}`` or None.

    Offsets are unknown for inferred turns and left at -1 on purpose — they
    point into an LLM-normalized view, not the raw file.
    """
    if not llm_structure_enabled():
        return None
    body = str(text or "").strip()
    if len(body) < 80:
        return None  # too short to be worth a model call; blocks fallback is fine
    body = body[: int(getattr(src.settings, "EXTERNAL_CONVERSATION_LLM_MAX_CHARS", 12000))]

    if llm_client is None:
        try:
            from src.core.llm_client import LLMClient

            llm_client = LLMClient()
        except Exception:
            return None

    raw = _chat_with_timeout(llm_client, _PROMPT.format(body=body))
    if raw is None:
        return None

    payload = _extract_json_object(raw)
    if not payload:
        return None

    turns: list[ConversationTurn] = []
    for item in (payload.get("turns") or [])[:_MAX_TURNS]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        turns.append(ConversationTurn(role=role, content=content, ts="", start_offset=-1, end_offset=-1))
    if not turns:
        return None

    title = str(payload.get("title") or "").strip()[:40] or None
    return {"title": title, "turns": turns}


_SUMMARY_PROMPT = """你是硬件知识库的资料提炼助手。下面是一段外部对话/记录内容，请做提取和总结。

规则：
- summary：2-4 句中文摘要，说清这段内容讨论了什么、得出了哪些结论。
- key_points：3-6 条要点，优先保留具体参数（电压/电流/型号/料号/阈值）、决策结论和注意事项；没有的维度不要硬凑。
- 只输出 JSON 对象，格式：
{{"summary": "...", "key_points": ["...", "..."]}}
不要输出任何解释、markdown 代码块或多余文本。

内容：
{body}"""


def summarize_content(text: str, llm_client=None, min_chars: int = 10) -> dict | None:
    """Return ``{"summary": str, "key_points": [str]}`` or None (fail-open)."""
    if not llm_summary_enabled():
        return None
    body = str(text or "").strip()
    if len(body) < min_chars:
        return None
    body = body[: int(getattr(src.settings, "EXTERNAL_CONVERSATION_LLM_MAX_CHARS", 12000))]

    if llm_client is None:
        try:
            from src.core.llm_client import LLMClient

            llm_client = LLMClient()
        except Exception:
            return None

    raw = _chat_with_timeout(llm_client, _SUMMARY_PROMPT.format(body=body))
    if raw is None:
        return None

    payload = _extract_json_object(raw)
    if not payload:
        return None
    summary = str(payload.get("summary") or "").strip()
    key_points = [
        str(k).strip() for k in (payload.get("key_points") or []) if isinstance(k, (str, int, float)) and str(k).strip()
    ][:8]
    if not summary and not key_points:
        return None
    return {"summary": summary[:600], "key_points": key_points}
