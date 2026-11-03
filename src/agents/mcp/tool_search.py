"""Lazy discovery for turns that expose many external MCP tools.

Small MCP setups keep the previous eager behaviour: every visible tool is bound
and listed in the prompt.  Above a threshold, binding every schema would crowd
out the domain tools, so the turn exposes one local ``mcp_tool_search`` tool and
the middleware reveals only the tools the model asked for.

Search is metadata-only: it never calls the external service, never consumes
the MCP call quota and never enters the evidence chain.
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

from langchain_core.tools import StructuredTool

from src.agents.mcp.policy import (
    MCP_PROMPT_RULE,
    MCP_WRITE_PROMPT_RULE,
    McpToolBinding,
)

SEARCH_TOOL_NAME = "mcp_tool_search"
DEFAULT_SEARCH_LIMIT = 8
MAX_SEARCH_LIMIT = 20
MAX_SEARCH_CALLS = 8
MIN_TOOLS_FOR_SEARCH = 12

_ASCII_TERM_RE = re.compile(r"[a-z0-9_]+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def should_use_tool_search(
    bindings: Iterable[McpToolBinding],
    *,
    min_tools: int = MIN_TOOLS_FOR_SEARCH,
) -> bool:
    """Use search only when eager binding would add too many schemas."""

    return len(list(bindings)) >= max(1, int(min_tools))


def _query_terms(query: str) -> set[str]:
    text = str(query or "").lower()
    terms = set(_ASCII_TERM_RE.findall(text))
    for run in _CJK_RUN_RE.findall(text):
        if len(run) <= 2:
            terms.add(run)
        else:
            terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def _description(binding: McpToolBinding) -> str:
    raw = getattr(binding.raw_tool, "description", "") or ""
    return " ".join(str(raw).split())[:240]


def _score(binding: McpToolBinding, query: str, terms: set[str]) -> int:
    tool_name = binding.tool_name.lower()
    safe_name = binding.safe_name.lower()
    description = _description(binding).lower()
    server = f"{binding.server.name} {binding.server.slug}".lower()
    score = 0
    if query:
        if query in tool_name or query in safe_name:
            score += 20
        if query in description:
            score += 8
    for term in terms:
        if term in tool_name:
            score += 6
        if term in safe_name:
            score += 4
        if term in description:
            score += 3
        if term in server:
            score += 2
    return score


def search_bindings(
    bindings: Iterable[McpToolBinding],
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> list[McpToolBinding]:
    """Rank visible bindings by tool name/description; empty query browses."""

    capped = max(1, min(int(limit or DEFAULT_SEARCH_LIMIT), MAX_SEARCH_LIMIT))
    rows = list(bindings)
    normalized = " ".join(str(query or "").split()).lower()
    terms = _query_terms(normalized)
    if normalized or terms:
        scored = [
            (_score(binding, normalized, terms), binding)
            for binding in rows
        ]
        hits = [(score, binding) for score, binding in scored if score > 0]
        hits.sort(key=lambda item: (-item[0], item[1].safe_name))
        return [binding for _score_value, binding in hits[:capped]]
    return sorted(rows, key=lambda binding: binding.safe_name)[:capped]


def format_search_result(
    hits: list[McpToolBinding],
    *,
    bindings: Iterable[McpToolBinding],
    total: int | None = None,
) -> str:
    """Render a bounded, explicit tool catalogue for the next model call."""

    rows = list(bindings)
    total = len(rows) if total is None else total
    lines = [
        MCP_PROMPT_RULE,
        "",
        "外部插件工具搜索结果（只是工具目录，不是知识库证据）：",
    ]
    if hits:
        for binding in hits:
            kind = "写工具（仅创建候选产物提案）" if binding.write else "只读"
            lines.append(
                f"- {binding.safe_name} | {binding.server.name}"
                f"（{binding.server.slug}） | {kind} | {_description(binding)}"
            )
        lines.append("")
        lines.append(
            "以上工具已在本轮可用；下一次模型调用起可直接按名称调用。"
            "需要更多工具时继续搜索，不要臆造未列出的工具名。"
        )
    else:
        sample = ", ".join(
            binding.safe_name
            for binding in sorted(rows, key=lambda item: item.safe_name)[:8]
        )
        lines.append(
            f"未找到匹配工具（本轮可见 {total} 个）。"
            "可尝试更短的关键词或英文工具名。"
        )
        if sample:
            lines.append(f"可见工具示例：{sample}")
    return "\n".join(lines)


def make_mcp_tool_search(
    rt: Any,
    bindings: Iterable[McpToolBinding],
):
    """Build the local search tool bound to this turn's visible bindings."""

    rows = list(bindings)

    def mcp_tool_search(query: str = "", limit: int = DEFAULT_SEARCH_LIMIT) -> str:
        """搜索本轮可见的外部插件工具，选择后即可直接调用。

        只在需要外部插件能力时调用。query 用简短关键词（工具名/能力），
        留空会按名称浏览。返回的是工具目录元数据，不是知识库证据。
        """
        rt.check_cancel()
        with rt._lock:  # noqa: SLF001 - runtime owns the turn-level counter
            if rt.mcp_search_count >= MAX_SEARCH_CALLS:
                return (
                    "本轮外部插件工具搜索次数已达上限，请直接调用已选工具或"
                    "基于现有结果继续；不要继续搜索。"
                )
            rt.mcp_search_count += 1
        started = time.monotonic()
        hits = search_bindings(rows, query, limit=limit)
        selected = rt.selected_mcp_tools
        selected.update(binding.safe_name for binding in hits)
        call_id = rt.next_invocation_id()
        rt.tool_started(
            call_id=call_id,
            tool_name=SEARCH_TOOL_NAME,
            tool_kind="mcp",
            query=str(query or ""),
        )
        rt.record_mcp_diagnostic(
            {
                "tool_name": SEARCH_TOOL_NAME,
                "hit_count": len(hits),
                "visible": len(rows),
                "status": "done",
                "latency_ms": int((time.monotonic() - started) * 1000),
                "filters": {"query": str(query or "")[:120], "limit": int(limit)},
            }
        )
        rt.tool_result(
            call_id=call_id,
            tool_name=SEARCH_TOOL_NAME,
            status="done",
            tool_kind="mcp",
            hit_count=len(hits),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return format_search_result(hits, bindings=rows, total=len(rows))

    return StructuredTool.from_function(
        func=mcp_tool_search,
        name=SEARCH_TOOL_NAME,
        description=(
            "搜索本轮可见的外部插件工具目录。工具较多时先搜索，"
            "搜索结果中的工具在下一次模型调用起可直接调用。"
            "返回目录元数据，不是知识库证据。"
        ),
    )


def append_mcp_search_prompt(
    system_prompt: str,
    bindings: Iterable[McpToolBinding],
) -> str:
    """Append the untrusted-content rule and the hidden-catalogue notice."""

    rows = list(bindings)
    if not rows:
        return system_prompt
    parts = [MCP_PROMPT_RULE]
    parts.append(
        f"本轮外部插件工具较多（共 {len(rows)} 个），未直接展开工具清单。"
        f"先调用 {SEARCH_TOOL_NAME} 搜索；搜索结果中的工具从下一次模型调用起可用，"
        "随后直接按名称调用。"
    )
    servers = sorted(
        {
            f"- {binding.server.name}（{binding.server.slug}）"
            for binding in rows
        }
    )
    parts.append("本轮可见外部服务：\n" + "\n".join(servers))
    if any(binding.write for binding in rows):
        parts.append(MCP_WRITE_PROMPT_RULE)
    block = "\n\n".join(parts)
    if not system_prompt:
        return block
    return f"{system_prompt}\n\n{block}"


__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "MAX_SEARCH_CALLS",
    "MAX_SEARCH_LIMIT",
    "MIN_TOOLS_FOR_SEARCH",
    "SEARCH_TOOL_NAME",
    "append_mcp_search_prompt",
    "format_search_result",
    "make_mcp_tool_search",
    "search_bindings",
    "should_use_tool_search",
]
