from __future__ import annotations

import json
from typing import Generator

from src.agents.graph import (
    _chat_with_usage_stage,
    _stream_chat_with_usage_stage,
    _write_stream_event,
    analyze_question_with_llm,
    build_multi_source_graph,
    compose_direct_answer,
    judge_sufficiency,
    plan_next_retrieval,
    plan_source_selection_with_llm,
    retrieve_evidence,
    route_query,
    scan_kb_catalog,
)
from src.agents.prompts import ANSWER_SYSTEM_PROMPT
from src.agents.tools.circuit_tools import CircuitQueryTool
from src.agents.tools.document_rag_tool import DocumentRAGTool
from src.agents.tools.pipeline_catalog_tool import PipelineCatalogTool
from src.agents.tools.spreadsheet_tools import SpreadsheetCellTool, SpreadsheetProfileTool, SpreadsheetSemanticTool
from src.core.llm_client import LLMClient
from src.circuit.index_service import CircuitIndexService
from src.pipelines.document_rag.base import RAGBackend
from src.pipelines.document_rag.schemas import RequestContext
from src.pipelines.document_store import PipelineDocumentStore
from src.services.spreadsheet_index_service import SpreadsheetIndexService


_OBSERVABILITY_REDACTED_KEYS = {
    "query",
    "content",
    "raw_text",
    "raw_value",
    "raw_values",
    "values",
    "profile",
    "filters",
    "metadata",
}


def _select_claim_context(state: dict, *, limit: int = 20) -> list[dict]:
    """Retain evidence required by supported claims before optional high-score items."""

    evidence = list(state.get("merged_evidence") or [])
    evidence_by_id = {str(item.get("id") or ""): item for item in evidence}
    selected: list[dict] = []
    selected_ids: set[str] = set()
    for coverage in state.get("claim_coverage") or []:
        if coverage.get("status") not in {"supported", "partial", "conflicting"}:
            continue
        for evidence_id in coverage.get("evidence_ids") or []:
            item = evidence_by_id.get(str(evidence_id))
            if item is not None and str(evidence_id) not in selected_ids:
                selected.append(item)
                selected_ids.add(str(evidence_id))
                if len(selected) >= limit:
                    return selected
    for item in sorted(evidence, key=lambda candidate: float(candidate.get("score") or 0.0), reverse=True):
        evidence_id = str(item.get("id") or "")
        if evidence_id not in selected_ids:
            selected.append(item)
            selected_ids.add(evidence_id)
            if len(selected) >= limit:
                break
    return selected


class MultiSourceAgentRunner:
    def __init__(
        self,
        *,
        rag_backend: RAGBackend,
        document_store: PipelineDocumentStore | None = None,
        spreadsheet_service: SpreadsheetIndexService | None = None,
        circuit_service: CircuitIndexService | None = None,
        llm_client: LLMClient | None = None,
    ):
        self.rag_backend = rag_backend
        self.document_store = document_store or PipelineDocumentStore()
        self.spreadsheet_service = spreadsheet_service or SpreadsheetIndexService()
        self.circuit_service = circuit_service or CircuitIndexService()
        self.llm_client = llm_client or LLMClient()
        # Footer (observability/trace) from the most recent stream; exposed
        # separately from final_response so the frontend can collapse it.
        self._last_footer: str = ""
        # Retrieval summary from the most recent stream (rewritten queries,
        # retriever type, final evidence count, and the merged evidence list).
        # Consumed by the log layer to populate query_traces + retrieved_evidence
        # after the answer finishes streaming. Reset on every stream() entry.
        self._last_retrieval_summary: dict = {}
        self._last_token_usage_summary = None
        self.catalog_tool = PipelineCatalogTool(
            document_store=self.document_store,
            spreadsheet_service=self.spreadsheet_service,
            rag_backend=rag_backend,
        )
        self.tools = {
            "document_rag": DocumentRAGTool(rag_backend, self.document_store),
            "circuit_query": CircuitQueryTool(self.circuit_service),
            "spreadsheet_semantic": SpreadsheetSemanticTool(self.spreadsheet_service),
            "spreadsheet_cell": SpreadsheetCellTool(self.spreadsheet_service),
            "spreadsheet_profile": SpreadsheetProfileTool(self.spreadsheet_service),
        }
        self.graph = build_multi_source_graph(
            {
                "analyze_question": lambda state: analyze_question_with_llm(state, self.llm_client),
                "route_query": lambda state: route_query(state, self.llm_client),
                "compose_direct_answer": self._compose_direct_answer,
                "scan_kb_catalog": lambda state: scan_kb_catalog(state, self.catalog_tool),
                "plan_source_selection": lambda state: plan_source_selection_with_llm(state, self.llm_client),
                "retrieve_evidence": lambda state: retrieve_evidence(state, self.tools),
                "draft_intermediate_answer": self._draft_intermediate_answer,
                "judge_sufficiency": lambda state: judge_sufficiency(state, self.llm_client),
                "plan_next_retrieval": lambda state: plan_next_retrieval(state, self.llm_client, self.catalog_tool),
                "compose_answer": self._compose_answer,
                "verify_grounding": self._verify_grounding,
            }
        )

    def stream(
        self,
        *,
        query: str,
        kb_name: str,
        history: list[tuple[str, str]],
        ctx: RequestContext | None = None,
        thread_id: str = "",
    ) -> Generator[str, None, None]:
        """Run the compiled LangGraph agent and stream answer deltas."""
        self._last_footer = ""
        self._last_retrieval_summary = {}
        self._last_token_usage_summary = None
        self._reset_llm_usage()

        state = {
            "thread_id": thread_id or f"{ctx.session_id if ctx else 'anonymous'}:{kb_name}",
            "kb_name": kb_name,
            "user_query": query,
            "history": history,
            "ctx": _ctx_to_dict(ctx),
            "_ctx_obj": ctx,
            "retrieval_round": 0,
            "evidence": [],
            "trace": [],
        }
        # The graph owns routing, retrieval, multi-hop replanning, and answer generation.
        final_state = state
        yielded = False
        for mode, event in self.graph.stream(
            state,
            config={"configurable": {"thread_id": state["thread_id"]}},
            stream_mode=["custom", "values"],
        ):
            if mode == "custom" and isinstance(event, dict) and event.get("type") == "answer_delta":
                delta = str(event.get("delta") or "")
                if delta:
                    yielded = True
                    yield delta
            elif mode == "values" and isinstance(event, dict):
                final_state = event
        self._last_retrieval_summary = (
            self._build_retrieval_summary(final_state)
            if int(final_state.get("retrieval_round") or 0) > 0
            else {}
        )
        self._last_token_usage_summary = self._get_llm_usage_summary()
        if not yielded:
            yield final_state.get("final_response") or final_state.get("answer") or "未生成回答。"
        return

    def _draft_intermediate_answer(self, state):
        evidence = _select_claim_context(state, limit=12)
        if not evidence:
            draft = "当前没有可用于起草答案的证据。"
        else:
            snippets = "\n".join(
                f"[{index}] {item.get('source_name')} | {item.get('content_kind')}: "
                f"{str(item.get('content') or '')[:260]}"
                for index, item in enumerate(evidence[:12], start=1)
            )
            sub_questions = json.dumps(
                (state.get("question_analysis") or {}).get("sub_questions") or [],
                ensure_ascii=False,
                indent=2,
            )
            prompt = (
                f"用户问题：{state.get('user_query')}\n\n"
                f"子问题：\n{sub_questions}\n\n"
                f"证据片段：\n{snippets}\n\n"
                "请基于当前证据生成一份中间草稿。只写证据支持的内容，"
                "对缺失或冲突的信息明确标注“缺失”或“冲突”。"
            )
            try:
                draft = _chat_with_usage_stage(
                    self.llm_client,
                    [
                        {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "intermediate_draft",
                )
            except Exception as exc:
                draft = f"中间草稿生成失败：{exc}"
        return {
            **state,
            "intermediate_answer": draft,
            "trace": list(state.get("trace") or []) + [
                {
                    "node": "intermediate_draft",
                    "message": "Intermediate answer drafted for sufficient-context review",
                    "metadata": {"chars": len(draft)},
                }
            ],
        }

    def _compose_answer(self, state):
        evidence = _select_claim_context(state, limit=20)
        coverage = state.get("coverage_matrix") or {}
        ledger = state.get("retrieval_ledger") or []
        evidence_quality = state.get("evidence_quality") or []
        retrieval_diagnostics = state.get("retrieval_diagnostics") or []
        if not evidence:
            answer = "当前知识库中未找到可支撑回答的证据。"
            _write_stream_event({"type": "answer_delta", "delta": answer})
        else:
            context = "\n\n".join(
                f"[{index}] Source: {item.get('source_name')} | Kind: {item.get('content_kind')} | "
                f"Locator: {json.dumps(item.get('locator') or {}, ensure_ascii=False)}\n{item.get('content')}"
                for index, item in enumerate(evidence[:20], start=1)
            )
            coverage_text = json.dumps(coverage, ensure_ascii=False, indent=2)
            ledger_text = json.dumps(ledger, ensure_ascii=False, indent=2)
            quality_text = json.dumps(evidence_quality[:20], ensure_ascii=False, indent=2)
            diagnostics_text = json.dumps(retrieval_diagnostics[-12:], ensure_ascii=False, indent=2)
            user_prompt = (
                f"用户问题：{state.get('user_query')}\n\n"
                f"证据覆盖度：\n{coverage_text}\n\n"
                f"检索账本（按子问题列出覆盖/缺口/gap feedback）：\n{ledger_text}\n\n"
                f"证据质量评分：\n{quality_text}\n\n"
                f"检索诊断：\n{diagnostics_text}\n\n"
                f"证据：\n{context}\n\n"
                "请输出中文答案，按子问题组织，包含来源说明，并列出缺失信息。"
                "如果检索账本中某个子问题 status 不是 covered，必须明确说明缺口，不要把弱证据写成确定结论。"
                "如果 coverage_matrix.conflicts 非空，必须单独列出证据冲突，不能把冲突值合并成确定结论。"
                "对 evidence_kind=derived_topology 的内容只能说明已观察到的连接/拓扑；"
                "只有同一 part_number 的 evidence_kind=datasheet_claim 才能确认器件保护能力。"
                "若存在多个 power-control candidate，必须逐个按输入/输出网络列示，不能将其归纳为某一个未指定输出的能力。"
            )
            try:
                messages = [
                    {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ]
                parts = []
                for delta in _stream_chat_with_usage_stage(self.llm_client, messages, "final_answer"):
                    parts.append(delta)
                    _write_stream_event({"type": "answer_delta", "delta": delta})
                answer = "".join(parts).strip()
            except Exception as exc:
                answer = self._fallback_answer(state, evidence, exc)
                _write_stream_event({"type": "answer_delta", "delta": answer})
        return {
            **state,
            "answer": answer,
            "trace": list(state.get("trace") or []) + [{"node": "compose_answer", "message": "Answer composed", "metadata": {}}],
        }

    def _verify_grounding(self, state):
        evidence_count = len(state.get("merged_evidence") or [])
        coverage = state.get("coverage_matrix") or {}
        conflicts = coverage.get("conflicts") or []
        weak_quality = [
            item
            for item in state.get("evidence_quality") or []
            if float(item.get("score") or 0.0) < 0.45
        ]
        verification = {
            "grounded": bool(evidence_count),
            "unsupported_claims": [],
            "weak_claims": [
                {"evidence_id": item.get("evidence_id", ""), "reason": "evidence_quality_below_threshold"}
                for item in weak_quality[:10]
            ],
            "conflicts": conflicts,
            "citation_coverage": 1.0 if evidence_count else 0.0,
        }
        observability = self._format_observability(state, verification)
        # Observability footer exposed via last_footer for collapsed rendering;
        # final_response carries only the answer body for a clean stream.
        self._last_footer = observability
        return {
            **state,
            "verification": verification,
            "final_response": state.get("answer", "").strip(),
        }

    def _fallback_answer(self, state, evidence: list[dict], exc: Exception) -> str:
        lines = [
            f"已完成多源检索，但生成模型调用失败：{exc}",
            "",
            "可用证据摘要：",
        ]
        for index, item in enumerate(evidence[:10], start=1):
            lines.append(f"{index}. {item.get('source_name')}: {str(item.get('content') or '')[:220]}")
        conflicts = (state.get("coverage_matrix") or {}).get("conflicts") or []
        if conflicts:
            lines.extend(["", "检测到的证据冲突："])
            for conflict in conflicts[:5]:
                lines.append(f"- {conflict.get('field')}: {conflict.get('values')}")
        return "\n".join(lines)

    def _compose_direct_answer(self, state):
        # Direct answer path: no retrieval, no evidence, no grounding verification
        # (verify_grounding is retrieval-specific and would misreport grounded=False).
        # Footer (trace + route note) is exposed via last_footer for the frontend
        # to render in a collapsed expander; kept out of final_response so the
        # streamed answer stays clean.
        query = state.get("user_query", "").strip()
        history = json.dumps((state.get("history") or [])[-6:], ensure_ascii=False, indent=2)
        try:
            messages = [
                {"role": "system", "content": "你是一个硬件领域的智能助手。请使用中文回答。"},
                {"role": "user", "content": f"对话历史：\n{history}\n\n用户问题：{query}"},
            ]
            parts = []
            for delta in _stream_chat_with_usage_stage(self.llm_client, messages, "direct_answer"):
                parts.append(delta)
                _write_stream_event({"type": "answer_delta", "delta": delta})
            state = {
                **state,
                "answer": "".join(parts).strip(),
                "trace": list(state.get("trace") or []) + [
                    {"node": "compose_direct_answer", "message": "Direct answer streamed", "metadata": {}}
                ],
            }
        except Exception:
            state = compose_direct_answer(state, self.llm_client)
            _write_stream_event({"type": "answer_delta", "delta": state.get("answer", "")})
        self._last_footer = self._format_direct_answer_footer(state)
        return {
            **state,
            "final_response": state.get("answer", "").strip(),
        }

    def _format_direct_answer_footer(self, state: dict) -> str:
        # Lightweight version of _format_observability: route note + trace only.
        # Omits Retrieval Diagnostics / Evidence Quality / Conflict Check (all
        # empty for direct answers).
        route = state.get("route_decision") or {}
        category = route.get("category", "")
        reason = route.get("reason", "")
        sections = ["**执行时间线**", self._format_trace(state.get("trace") or [])]
        sections.append("\n**路由说明**")
        sections.append(f"- 直接回答（未检索知识库） | 类别：{category} | {reason}")
        return "\n".join(sections)

    def _format_trace(self, trace: list[dict]) -> str:
        if not trace:
            return "- 无"
        lines = []
        for index, item in enumerate(trace, start=1):
            metadata = _sanitize_observability_value(item.get("metadata") or {})
            summary = _format_trace_metadata(metadata)
            suffix = f" | {summary}" if summary else ""
            lines.append(f"{index}. `{item.get('node')}` {item.get('message')}{suffix}")
        return "\n".join(lines)

    def _format_observability(self, state: dict, verification: dict) -> str:
        coverage = state.get("coverage_matrix") or {}
        ledger = state.get("retrieval_ledger") or []
        diagnostics = state.get("retrieval_diagnostics") or []
        quality = state.get("evidence_quality") or []
        top_quality = sorted(quality, key=lambda item: float(item.get("score") or 0.0), reverse=True)[:5]
        plan = (state.get("source_plan") or {}).get("source_plan") or []
        total_tool_calls = sum(len(item.get("tool_calls") or []) for item in plan)
        sufficiency = state.get("sufficiency") or {}
        sections = ["**概览**"]
        sections.append(
            f"- 检索轮次：{int(state.get('retrieval_round') or 0)} | "
            f"计划源：{len(plan)} | 计划调用：{total_tool_calls} | "
            f"证据：{len(state.get('merged_evidence') or [])} | "
            f"充分性：{sufficiency.get('status') or '-'}"
        )
        sections.append("\n**执行时间线**")
        sections.append(self._format_trace(state.get("trace") or []))
        sections.append("\n**检索诊断**")
        if diagnostics:
            for item in diagnostics[-8:]:
                sections.append(
                    f"- {item.get('tool_name')} hits={item.get('hit_count')} "
                    f"status={item.get('status')} scoped={bool(item.get('filters'))}"
                )
        else:
            sections.append("- 无")
        sections.append("\n**检索账本**")
        if ledger:
            for item in ledger[:8]:
                sections.append(
                    f"- {item.get('sub_question_id')}: {item.get('status')} | "
                    f"expected={','.join(item.get('expected_evidence') or [])} | "
                    f"support={len(item.get('supporting_evidence_ids') or [])} | "
                    f"unsearched={len(item.get('unsearched_relevant_sources') or [])}"
                )
                if item.get("gap_feedback"):
                    sections.append(f"  - gap: {item.get('gap_feedback')}")
        else:
            sections.append("- 无")
        sections.append("\n**充分性与补检索**")
        if sufficiency:
            missing = sufficiency.get("missing") or []
            suggestions = sufficiency.get("suggested_queries") or []
            sections.append(f"- 状态：{sufficiency.get('status')} | 缺口：{len(missing)} | 建议补检索：{len(suggestions)}")
            for item in missing[:5]:
                sections.append(f"- 缺口：{item}")
            for item in suggestions[:5]:
                sections.append(f"- 建议：{item.get('tool_name')} | {item.get('reason') or item.get('query')}")
        else:
            sections.append("- 无")
        sections.append("\n**证据质量**")
        if top_quality:
            for item in top_quality:
                sections.append(
                    f"- {item.get('evidence_id')}: score={float(item.get('score') or 0.0):.2f}, reasons={', '.join(item.get('reasons') or [])}"
                )
        else:
            sections.append("- 无")
        conflicts = coverage.get("conflicts") or []
        sections.append("\n**冲突检查**")
        if conflicts:
            for conflict in conflicts[:5]:
                sections.append(f"- {conflict.get('field')}: {conflict.get('values')}")
        else:
            sections.append("- 未检测到结构化字段冲突")
        sections.append("\n**迭代检索**")
        rounds = self._iter_round_summary(state)
        if rounds:
            for rnd in rounds:
                sections.append(
                    f"- 轮 {rnd['round']}: status={rnd['status'] or '-'} "
                    f"missing={len(rnd.get('missing') or [])} "
                    f"suggested_queries={len(rnd.get('suggested_queries') or [])}"
                )
        else:
            sections.append("- 单轮检索，未触发多跳补检索")
        sections.append("\n**Grounding**")
        sections.append(f"- grounded={verification.get('grounded')} weak_claims={len(verification.get('weak_claims') or [])}")
        return "\n".join(sections)

    def _iter_round_summary(self, state: dict) -> list[dict]:
        """Rebuild per-round retrieval status from the node trace."""
        rounds: dict[int, dict] = {}
        for item in state.get("trace") or []:
            node = item.get("node")
            meta = item.get("metadata") or {}
            if node == "retrieve_evidence":
                rnd = meta.get("round")
                if rnd is None:
                    continue
                rounds.setdefault(rnd, {"round": rnd, "suggested_queries": [], "status": "", "missing": []})
            elif node == "judge_sufficiency":
                # Sufficiency is judged after a retrieve round, so attach it to that round.
                rnd = meta.get("round")
                if rnd is None:
                    # Use the most recent retrieve round if the trace did not include one.
                    rnd = max(rounds) if rounds else None
                if rnd is None:
                    continue
                entry = rounds.setdefault(rnd, {"round": rnd, "suggested_queries": [], "status": "", "missing": []})
                entry["status"] = meta.get("status", "")
                entry["missing"] = meta.get("missing", [])
            elif node == "plan_next_retrieval":
                rnd = meta.get("round")
                if rnd is None:
                    rnd = (max(rounds) if rounds else 0) + 1
                entry = rounds.setdefault(rnd, {"round": rnd, "suggested_queries": [], "status": "", "missing": []})
                entry["suggested_queries"] = meta.get("suggested_queries", [])
        return [rounds[k] for k in sorted(rounds)]

    def get_last_footer(self) -> str:
        """Return the observability/trace footer produced by the most recent
        stream call. The frontend renders this in a collapsed expander."""
        return self._last_footer or ""

    def get_last_retrieval_summary(self) -> dict:
        """Return the retrieval summary from the most recent stream call.

        Empty dict when no retrieval happened (direct-answer path, pending
        confirmation, or cancelled). The log layer reads this after the answer
        finishes streaming to populate query_traces.retriever_type/final_top_k/
        rewritten_query and the retrieved_evidence rows.
        """
        return self._last_retrieval_summary or {}

    def get_last_token_usage_summary(self):
        return self._last_token_usage_summary

    def clear_last_token_usage_summary(self) -> None:
        self._last_token_usage_summary = None

    def _reset_llm_usage(self) -> None:
        reset_usage = getattr(self.llm_client, "reset_usage", None)
        if callable(reset_usage):
            reset_usage()

    def _get_llm_usage_summary(self):
        get_usage_summary = getattr(self.llm_client, "get_usage_summary", None)
        if callable(get_usage_summary):
            return get_usage_summary()
        return None

    def _build_retrieval_summary(self, state: dict) -> dict:
        source_plan = (state.get("source_plan") or {}).get("source_plan") or []
        rewritten_queries: list[str] = []
        seen: set[str] = set()
        # First-round planner calls.
        for item in source_plan:
            for call in item.get("tool_calls") or []:
                q = (call.get("query") or "").strip()
                if q and q not in seen:
                    seen.add(q)
                    rewritten_queries.append(q)
        # Later multi-hop replanning calls.
        for call in state.get("next_retrieval_calls") or []:
            q = (call.get("query") or "").strip()
            if q and q not in seen:
                seen.add(q)
                rewritten_queries.append(q)
        merged = state.get("merged_evidence") or []
        evidence_rows = []
        for item in merged:
            metadata = item.get("metadata") or {}
            locator = item.get("locator") or {}
            evidence_rows.append(
                {
                    "id": item.get("id") or "",
                    "source_name": item.get("source_name") or "",
                    "score": item.get("score"),
                    "locator": locator,
                    "content_kind": item.get("content_kind") or metadata.get("content_kind") or "",
                    "processor_kind": item.get("processor_kind") or metadata.get("processor_kind") or "",
                    "content": item.get("content") or "",
                    "metadata": metadata,
                }
            )
        sufficiency = state.get("sufficiency") or {}
        diagnostics = state.get("retrieval_diagnostics") or []
        failed_diagnostic = next(
            (item for item in diagnostics if item.get("status") not in {"ok", ""}),
            None,
        )
        answer = str(state.get("answer") or state.get("final_response") or "")
        if failed_diagnostic:
            status = "failed"
            error_stage = "retrieval"
            error_message = str(failed_diagnostic.get("error") or failed_diagnostic.get("status") or "")
        elif not merged:
            status = "no_evidence"
            error_stage = "retrieval"
            error_message = "no evidence"
        elif answer.startswith("已完成多源检索，但生成模型调用失败"):
            status = "failed"
            error_stage = "answer"
            error_message = answer.splitlines()[0]
        else:
            status = "success"
            error_stage = ""
            error_message = ""
        return {
            "status": status,
            "error_stage": error_stage,
            "error_message": error_message,
            "rewritten_queries": rewritten_queries,
            "retriever_type": "multi_source_agent",
            "final_top_k": len(merged),
            "evidence": evidence_rows,
            "missing": sufficiency.get("missing") or [],
            "retrieval_rounds": int(state.get("retrieval_round") or 0),
            "sufficiency_status": sufficiency.get("status") or "",
            "trace": state.get("trace") or [],
            "tool_diagnostics": diagnostics,
            "claim_coverage": state.get("claim_coverage") or [],
            "retrieval_ledger": state.get("retrieval_ledger") or [],
            "evidence_quality": state.get("evidence_quality") or [],
            "verification": state.get("verification") or {},
        }


def _ctx_to_dict(ctx: RequestContext | None) -> dict:
    if ctx is None:
        return {}
    return {
        "user_id": ctx.user_id,
        "session_id": ctx.session_id,
        "roles": list(ctx.roles),
        "allowed_kbs": list(ctx.allowed_kbs),
        "kb_permissions": dict(ctx.kb_permissions),
        "metadata": dict(ctx.metadata),
    }


def _sanitize_observability_value(value, *, depth: int = 0):
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in _OBSERVABILITY_REDACTED_KEYS:
                sanitized[key_text] = "[redacted]"
            else:
                sanitized[key_text] = _sanitize_observability_value(item, depth=depth + 1)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_observability_value(item, depth=depth + 1) for item in value[:12]]
    if isinstance(value, str):
        if len(value) > 120:
            return value[:120] + "...[truncated]"
        return value
    return value


def _format_trace_metadata(metadata: dict) -> str:
    if not metadata:
        return ""
    parts = []
    for key in [
        "category",
        "needs_retrieval",
        "planned_sources",
        "tool_calls",
        "round",
        "total_evidence",
        "merged",
        "status",
        "chars",
    ]:
        if key in metadata and metadata.get(key) not in (None, "", []):
            parts.append(f"{key}={metadata.get(key)}")
    if metadata.get("missing"):
        parts.append(f"missing={len(metadata.get('missing') or [])}")
    if metadata.get("suggested_queries"):
        parts.append(f"suggested_queries={len(metadata.get('suggested_queries') or [])}")
    if metadata.get("error"):
        parts.append("error=present")
    return " | ".join(parts)
