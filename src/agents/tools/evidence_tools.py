"""Two-stage evidence reading for candidate-based retrieval.

``document_search`` and the other search tools now keep most hits in a
non-citable candidate index.  Only ``evidence_read`` promotes selected
candidates into the formal evidence ledger, so a broad search does not consume
the per-turn evidence/citation budget.
"""

from __future__ import annotations

import re
import time
from typing import Any

from src.agents.tools.runtime import format_evidence_for_llm


_THRESHOLD_TERMS = ("门限", "阈值", "过压", "欠压", "上限", "下限")
_THRESHOLD_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?\s*(?:V|mV|A|mA|℃|°C|%)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def _missing_entity_hints(rt: Any) -> str:
    """Turn entities named in the question but absent from evidence into next steps."""

    query = str(getattr(rt, "question", "") or "")
    if not query:
        return ""
    from src.agents.turn_meta import _hardware_profile

    evidence_text = " ".join(str(item.content or "") for item in rt.evidence).lower()
    hints: list[str] = []
    refs = [
        ref for ref in _hardware_profile(query)["refdes"]
        if ref.lower() not in evidence_text
    ]
    if refs:
        hints.append(
            f"问题中的 {'、'.join(refs[:6])} 尚未出现在已登记证据；"
            "可用 kb_grep 一次传多个位号，或 circuit_search 查网表，再决定是否声明缺口"
        )
    if any(term in query for term in _THRESHOLD_TERMS) and not _THRESHOLD_VALUE_RE.search(
        evidence_text
    ):
        hints.append(
            "问题涉及门限/阈值，但已登记证据还没有具体数值；"
            "先用 spreadsheet_row_search / spreadsheet_sql_query 查规格或 WCCA 表，"
            "或换关键词检索，再决定是否声明缺口"
        )
    if not hints:
        return ""
    return "\n\n（缺口提示：" + "；".join(hints) + "。）"


CANDIDATE_PROMPT = (
    "检索工具默认返回候选证据（id/来源/位置/摘要），候选不会获得引用编号，也不占用证据预算。"
    "先判断候选是否直接回答问题，再用 evidence_read 读取 2-4 条最相关候选；"
    "候选 id 只能传给 evidence_read，不能写进最终回答；引用编号只能来自 evidence_read 返回的 [n]。"
    "如果某个目标或来源只有候选、还没有编号证据，优先用 evidence_read 读取该来源的候选，"
    "不要用相同或近似的查询重复检索。型号、位号、信号名、表号等精确关键词可用 kb_grep 一次传多个模式。"
)


def make_evidence_read(rt: Any):
    """Return the model-facing candidate promotion tool."""

    def evidence_read(candidate_ids: list[str]) -> str:
        """读取候选证据的完整正文并登记为正式证据，返回可引用的 [n] 编号。

        候选来自 document_search / circuit_search / spreadsheet_row_search 等检索结果。
        单次最多读取 4 条与问题直接相关的候选；一旦登记，会占用本轮证据预算。
        """
        ids = [
            str(value or "").strip()
            for value in (candidate_ids or [])
            if str(value or "").strip()
        ][:4]
        if not ids:
            return "（未提供候选 id）"
        call_id = rt.next_invocation_id()
        started = time.monotonic()
        rt.tool_started(
            call_id=call_id,
            tool_name="evidence_read",
            tool_kind="retrieval",
            query=", ".join(ids),
        )
        rt.check_cancel()
        selected, missing = rt.read_candidates(ids)
        if not selected:
            rt.tool_result(
                call_id=call_id,
                tool_name="evidence_read",
                status="done",
                hit_count=0,
                evidence_count=0,
                empty_reason="no_match",
                latency_ms=int((time.monotonic() - started) * 1000),
                error="候选不存在或已失效",
            )
            return "（候选不存在或已失效；请重新调用检索工具。）"
        epoch = rt.begin_tool_call()
        try:
            numbers = rt.add_evidence(selected, epoch=epoch)
        finally:
            rt.end_tool_call(epoch)
        admitted = []
        citation_numbers = []
        for item, number in zip(selected, numbers):
            if number is None:
                continue
            item.metadata = {**item.metadata, "citation_number": number}
            admitted.append(item)
            citation_numbers.append(int(number))
        # A promoted candidate is now formal evidence; keep it out of the
        # candidate index so a repeated read cannot dump the same body twice.
        rt.drop_candidates(item.id for item in admitted)
        sources = list(
            dict.fromkeys(
                str(item.source_name or "").strip()
                for item in admitted
                if str(item.source_name or "").strip()
            )
        )[:3]
        rt.tool_result(
            call_id=call_id,
            tool_name="evidence_read",
            status="done",
            hit_count=len(admitted),
            evidence_count=len(admitted),
            sources=sources,
            citation_numbers=citation_numbers,
            empty_reason="" if admitted else "budget_blocked",
            latency_ms=int((time.monotonic() - started) * 1000),
            error="" if admitted else "本轮证据预算已满",
        )
        if not admitted:
            return (
                "（候选正文未能登记：本轮证据预算已满。"
                "请基于已有证据作答，不要继续读取候选。）"
            )
        rendered = format_evidence_for_llm(admitted)
        if missing:
            rendered += f"\n\n（以下候选不存在或已失效：{', '.join(missing[:8])}）"
        pending = len(rt.candidate_evidence)
        if pending:
            rendered += (
                f"\n\n（仍有 {pending} 条候选未读。若回答还缺少问题中的具体数值、型号或连接关系，"
                "先读取相关候选或换关键词/来源检索，再决定是否声明缺口。）"
            )
        rendered += _missing_entity_hints(rt)
        return rendered

    return evidence_read


__all__ = ["CANDIDATE_PROMPT", "make_evidence_read"]
