"""LLM-assisted structure inference for external conversations.

Only used for marker-less text (no 用户:/assistant:/Q:/A: markers found by the
deterministic parser). Strictly fail-open: any error, garbage output or
disabled setting returns ``None`` and the caller keeps the plain-blocks
fallback. One LLM call per operation, bounded by a character cap and an outer
timeout.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

import src.settings
from src.core.chat_model_runtime import ChatModelLike, invoke_structured
from src.core.model_factory import create_chat_model
from src.external_conversations.models import ConversationTurn

# Two independent background operations may be waiting on the provider. A
# timed-out call cannot be force-killed, so callers stop waiting and discard
# its result; the profile timeout is shorter than the outer wait window.
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


class ConversationTurnPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12000)


class ConversationStructurePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=40)
    turns: list[ConversationTurnPayload] = Field(
        default_factory=list, max_length=_MAX_TURNS,
    )


class ConversationSummaryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", max_length=600)
    key_points: list[str] = Field(default_factory=list, max_length=8)


def llm_structure_enabled() -> bool:
    return bool(getattr(src.settings, "EXTERNAL_CONVERSATION_LLM_STRUCTURE", False))


def llm_summary_enabled() -> bool:
    return bool(getattr(src.settings, "EXTERNAL_CONVERSATION_LLM_SUMMARY", False))


def _invoke_with_timeout(
    chat_model: ChatModelLike,
    schema: type[BaseModel],
    prompt: str,
    *,
    operation: str,
    text_fallback: Callable[[str], BaseModel | Mapping[str, Any]],
) -> Any | None:
    """Run one runtime invocation while preserving the external fail-open window."""

    timeout = float(getattr(src.settings, "EXTERNAL_CONVERSATION_LLM_TIMEOUT_SECONDS", 60))
    try:
        future = _EXECUTOR.submit(
            invoke_structured,
            chat_model,
            schema,
            [{"role": "user", "content": prompt}],
            operation=operation,
            profile="external_conversation",
            text_fallback=text_fallback,
        )
        return future.result(timeout=timeout)
    except FuturesTimeout:
        return None
    except Exception:
        return None


def _resolve_model(
    chat_model: ChatModelLike | None,
    model_factory: Callable[[], ChatModelLike] | None,
) -> ChatModelLike | None:
    if chat_model is not None:
        return chat_model
    try:
        return model_factory() if model_factory is not None else create_chat_model(
            profile="external_conversation"
        )
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


def _parse_text_structure(response: str) -> ConversationStructurePayload:
    """Parse the explicit compatibility JSON path and validate its shape."""

    payload = _extract_json_object(response)
    if payload is None:
        raise ValueError("structure response is not a JSON object")

    # Keep the historical fail-open filtering for malformed individual turns;
    # the resulting bounded payload still goes through the strict schema.
    raw_turns = payload.get("turns")
    if not isinstance(raw_turns, list):
        raise ValueError("structure response turns is not a list")
    valid_turns: list[dict[str, str]] = []
    for item in raw_turns[:_MAX_TURNS]:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "").strip().casefold()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        valid_turns.append({"role": role, "content": content})
    normalized = dict(payload)
    normalized["title"] = str(payload.get("title") or "").strip()[:40]
    normalized["turns"] = valid_turns
    try:
        return ConversationStructurePayload.model_validate(normalized)
    except Exception as exc:
        raise ValueError(str(exc)) from exc


def _parse_text_summary(response: str) -> ConversationSummaryPayload:
    payload = _extract_json_object(response)
    if payload is None:
        raise ValueError("summary response is not a JSON object")
    raw_points = payload.get("key_points")
    if raw_points is None:
        raw_points = []
    if not isinstance(raw_points, list):
        raise ValueError("summary response key_points is not a list")
    # Preserve the old compatibility parser's useful primitive coercion while
    # making the final object pass through the strict Pydantic schema.
    normalized = dict(payload)
    normalized["summary"] = str(payload.get("summary") or "").strip()[:600]
    normalized["key_points"] = [
        str(item).strip()
        for item in raw_points[:8]
        if isinstance(item, (str, int, float)) and str(item).strip()
    ]
    try:
        return ConversationSummaryPayload.model_validate(normalized)
    except Exception as exc:
        raise ValueError(str(exc)) from exc


def infer_structure(
    text: str,
    chat_model: ChatModelLike | None = None,
    *,
    model_factory: Callable[[], ChatModelLike] | None = None,
) -> dict | None:
    """Return ``{"title": str|None, "turns": [ConversationTurn]}`` or None.

    Offsets are unknown for inferred turns and left at -1 on purpose — they
    point into an LLM-normalized view, not the raw file.
    """

    if not llm_structure_enabled():
        return None
    body = str(text or "").strip()
    if len(body) < 80:
        return None
    body = body[: int(getattr(src.settings, "EXTERNAL_CONVERSATION_LLM_MAX_CHARS", 12000))]
    model = _resolve_model(chat_model, model_factory)
    if model is None:
        return None

    result = _invoke_with_timeout(
        model,
        ConversationStructurePayload,
        _PROMPT.format(body=body),
        operation="external_conversation_llm",
        text_fallback=_parse_text_structure,
    )
    if result is None or not result.value.turns:
        return None

    turns = [
        ConversationTurn(
            role=item.role,
            content=item.content.strip(),
            ts="",
            start_offset=-1,
            end_offset=-1,
        )
        for item in result.value.turns[:_MAX_TURNS]
    ]
    if not turns:
        return None
    title = result.value.title.strip()[:40] or None
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


def summarize_content(
    text: str,
    chat_model: ChatModelLike | None = None,
    *,
    model_factory: Callable[[], ChatModelLike] | None = None,
    min_chars: int = 10,
) -> dict | None:
    """Return ``{"summary": str, "key_points": [str]}`` or None (fail-open)."""

    if not llm_summary_enabled():
        return None
    body = str(text or "").strip()
    if len(body) < min_chars:
        return None
    body = body[: int(getattr(src.settings, "EXTERNAL_CONVERSATION_LLM_MAX_CHARS", 12000))]
    model = _resolve_model(chat_model, model_factory)
    if model is None:
        return None

    result = _invoke_with_timeout(
        model,
        ConversationSummaryPayload,
        _SUMMARY_PROMPT.format(body=body),
        operation="external_conversation_llm",
        text_fallback=_parse_text_summary,
    )
    if result is None:
        return None
    summary = result.value.summary.strip()[:600]
    key_points = [str(item).strip() for item in result.value.key_points if str(item).strip()][:8]
    if not summary and not key_points:
        return None
    return {"summary": summary, "key_points": key_points}
