"""MultiSourceAgentRunner: thin facade over the deepagents harness.

The heavy lifting (tool-calling loop, context management, summarization) is
delegated to ``deepagents.create_deep_agent``; this module only:

- binds request-scoped tool closures via ``ToolRuntime`` (kb / ctx / cancel /
  event callback), so concurrent streams never share state;
- streams the deepagents message stream live: text of the in-flight model
  message is yielded immediately as provisional answer deltas; once a message
  turns out to carry tool calls it is reclassified as interim narration and
  announced via the ``narration`` event so consumers can retract the already-
  streamed text (``strip_narration_segments``). Progress also goes through
  ``event_callback`` as ``stage`` / ``tool_started`` / ``tool_result`` /
  ``degraded`` events;
- builds the retrieval summary + observability footer consumed by the API
  layer (query traces, log center, ``done`` payload).

Model access goes through ``src.core.model_factory`` and its allowlisted
profiles: ``ollama:`` for local deployment, ``openai:`` with a custom
base_url for any OpenAI-compatible cloud endpoint.
"""

from __future__ import annotations

import contextvars
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, Sequence

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from langchain_core.messages import AIMessage, ToolMessage

from src.agents.answer_stream import (
    AnswerSplitter as _AnswerSplitter,
    strip_narration_segments,  # noqa: F401 - re-exported for API/eval callers
)
from src.agents.assembly import build_agent
from src.agents.turn_prompt import (
    EXPORT_PROMPT as _EXPORT_PROMPT,  # noqa: F401 - re-exported for callers
    SYSTEM_PROMPT as _SYSTEM_PROMPT,  # noqa: F401 - re-exported for callers
    build_turn_prompt,
)
from src.agents.middleware.gap_closure import GapClosureMiddleware
from src.agents.middleware.mcp_tool_search import McpToolSearchMiddleware
from src.agents.skill_materializer import (
    load_skill_prompt_context,
    materialize_text_skills,
)
from src.agents.context_budget import (
    initial_context_state,
    publish_turn_context,
    read_agent_values,
)
from src.agents.middleware.loop_budget import (
    CallBudgetExceeded,
    LoopBudget,
    LoopBudgetMiddleware,
    ProgressStopped,
    TimeBudgetExceeded,
)
from src.agents.session_recall import (
    evidence_rows,
    load_session_context,
)
from src.agents.session_service import prepare_session
from src.agents.turn_tools import build_turn_tools
from src.agents.subagent_trace import record_subagent_end, record_subagent_start
from src.contracts.agent_events import validate_event_envelope
from src.agents.turn_meta import (
    PARTIAL_SYNTHESIS_ENABLED,
    RESEARCH_SUBAGENT_ENABLED,
    TokenUsageSummary,
    _accumulate_usage,
    _agent_active,
    _agent_evidence_budget,
    _agent_recursion_limit,
    _agent_step_budget_answer,
    _build_citation_verification,
    _build_claim_coverage,
    _build_evidence_quality,
    _build_turn_verification,
    _complex_task_prompt,
    _empty_answer_delivery,
    _evidence_digest,
    _extract_text_delta,
    _is_incomplete_answer,
    answer_gap_lines,
    _normalize_citation_labels,
    _research_subagent_spec,
    _scope_dependency_fields,
    _scrub_invalid_citations,
    _synthesize_partial_answer,
)
from src.agents.orchestration.capabilities import (
    CapabilityResolver,
    authorized_capabilities_for_conversation,
    deployed_capabilities_from_settings,
)
from src.agents.mcp.policy import (
    McpServerSnapshot,
    resolve_visible_servers,
)
from src.agents.tools.runtime import (
    ToolDiagnostics,
    ToolRuntime,
    memory_context_item,
)
from src.attachments.audit import begin_attachment_audit, finish_attachment_audit
from src.circuit.index_service import CircuitIndexService
from src.core.cancellation import QueryCancelled, bind_cancel_check
from src.core.conversation import GENERAL_CHAT_KB_NAME
from src.core.conversation_orchestrator import ConversationOrchestrator
from src.core.intent import IntentPlan, classify_intent
from src.core.model_factory import create_chat_model
from src import settings
from src.observability import observe
from src.observability.metrics import counter, record_agent, record_agent_stage
from src.pipelines.document_rag.base import RAGBackend
from src.pipelines.document_rag.schemas import RequestContext
from src.pipelines.document_store import PipelineDocumentStore
from src.services.spreadsheet_index_service import SpreadsheetIndexService
from src.memory.service import MemoryService
from src.services.knowledge_scope import KnowledgeScopeResolver
from src.services.knowledge_scope_models import KnowledgeScopeSnapshot
from src.services.multi_kb_retrieval import MultiKnowledgeRetrievalService, RetrievalBudget

# M9: 收窄 deepagents 默认能力面。禁用框架自动装配的 general-purpose 子代理
# (task 工具随之移除)，并剥离与 SYSTEM_PROMPT 声明不符的文件系统/命令执行工具，
# 仅保留业务检索工具与官方 Skill 渐进式披露所需的 read_file。
# read_file 只在有物化 Skill 的回合由 SkillReadGateMiddleware 放行；backend
# 固定 root 到本次只读 Skill 目录，写操作继续 deny。
_APP_AGENT_EXCLUDED_TOOLS = frozenset({
    "ls", "write_file", "edit_file", "glob", "grep", "delete", "execute",
})
for _provider_key in ("openai", "ollama"):
    try:
        register_harness_profile(
            _provider_key,
            HarnessProfile(
                general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
                excluded_tools=_APP_AGENT_EXCLUDED_TOOLS,
            ),
        )
    except Exception:
        break


# LangGraph checkpointer：agent 会话状态（thread 级完整消息历史）持久化。
# PostgreSQL 后端为每次构图返回独立 saver（共享连接池），避免实例锁串行化
# 并发会话；SQLite 后端保持进程级单例（v3 §10）。
def _get_checkpointer() -> Any:
    from src.agents.checkpoints.factory import get_checkpointer

    return get_checkpointer()


def _create_checkpointer() -> Any:
    from src.agents.checkpoints.factory import create_checkpointer

    return create_checkpointer()


def forget_thread(thread_id: str) -> bool:
    """Drop the persisted agent state for a thread (session clear/delete).

    Removes the canonical thread, every attempt namespace and the pointer
    (P0a attempt isolation).
    """
    if not str(thread_id or "").strip():
        return False
    try:
        from src.agents.checkpoints.attempts import forget_thread_state

        forget_thread_state(_get_checkpointer(), str(thread_id))
        return True
    except Exception:
        # The API path remains fail-soft; the cleanup outbox can inspect the
        # boolean result and retry the durable cleanup asynchronously.
        return False


_CITATION_RE = re.compile(r"\[(\d+)\]")
_ATTACHMENT_PROPOSAL_CONFIRM_PATTERN = re.compile(
    r"确认.{0,6}(?:修改|替换|章节|提案)|"
    r"按(?:照)?(?:此|该).{0,4}(?:修改|替换)|"
    r"同意.{0,6}(?:修改|替换|章节)",
    re.IGNORECASE,
)
_ATTACHMENT_PROPOSAL_CANCEL_PATTERN = re.compile(
    r"取消|放弃|撤销|不要修改|别改|不修改",
    re.IGNORECASE,
)
_ATTACHMENT_EDIT_ACTION_PATTERN = re.compile(
    r"完善|补充|改写|修改|修订|替换|更新|补齐|重写",
    re.IGNORECASE,
)
_ATTACHMENT_SECTION_TARGET_PATTERN = re.compile(
    r"第\s*[0-9一二三四五六七八九十百两]+\s*(?:章|节|部分|篇)"
    r"|章节|整节|小节|文档内容|正文",
    re.IGNORECASE,
)
_DOCUMENT_TASK_FOLLOW_UP_PATTERN = re.compile(
    r"取消|撤销|终止|暂停|继续(?:生成|执行)?|恢复|重试|"
    r"申请(?:正式)?(?:发布|上线)|请求(?:正式)?发布|提交(?:正式)?发布|"
    r"(?:查看|查询|获取|显示).{0,12}(?:任务|文档)?(?:状态|进度)|"
    r"候选.{0,12}(?:下载|发布)|"
    r"cancel|pause|resume|retry|release|status|candidate",
    re.IGNORECASE,
)


def _direct_attachment_proposal_answer(
    *,
    rt: Any,
    query: str,
    emit_event: Callable[[dict], None],
) -> str | None:
    """Confirm or cancel a pending governed attachment proposal.

    Returns ``None`` when the turn is unrelated to a pending proposal so the
    normal agent loop keeps handling it.  This path is deterministic and never
    calls the model: nothing is written until the user explicitly confirms.
    """

    confirm_requested = bool(_ATTACHMENT_PROPOSAL_CONFIRM_PATTERN.search(query))
    cancel_requested = bool(_ATTACHMENT_PROPOSAL_CANCEL_PATTERN.search(query)) and not confirm_requested
    if not confirm_requested and not cancel_requested:
        return None
    service = getattr(rt, "attachment_service", None)
    session_id = int(getattr(rt, "attachment_session_id", 0) or 0)
    user_id = int(getattr(rt, "attachment_user_id", 0) or 0)
    if service is None or not session_id or not user_id:
        return None
    try:
        proposal = service.get_pending_attachment_proposal(session_id=session_id, user_id=user_id)
    except Exception:  # noqa: BLE001 - control path is fail-soft
        return None
    if proposal is None:
        return None

    if cancel_requested:
        attempt = begin_attachment_audit(
            action="attachment.proposal_reject",
            target_type="attachment_edit_proposal",
            target_id=proposal.proposal_id,
            actor_user_id=user_id,
            kb_name=str(getattr(rt, "kb_name", "") or ""),
            metadata={"session_id": session_id, "source": "chat"},
        )
        try:
            service.reject_attachment_proposal(
                proposal_id=proposal.proposal_id,
                session_id=session_id,
                user_id=user_id,
                note="用户在对话中取消",
            )
        except Exception as exc:  # noqa: BLE001
            finish_attachment_audit(
                attempt,
                success=False,
                error_message=str(exc),
                metadata={"session_id": session_id, "source": "chat"},
            )
            return f"取消章节修改提案失败：{str(exc)[:200]}"
        finish_attachment_audit(
            attempt,
            success=True,
            metadata={"session_id": session_id, "source": "chat"},
        )
        emit_event({
            "type": "stage",
            "payload": {
                "key": "attachment_edit",
                "label": "附件修订",
                "status": "done",
                "detail": "已取消章节修改提案",
            },
        })
        return "已取消该附件章节修改提案，未生成候选，原附件保持不变。"

    attempt = begin_attachment_audit(
        action="attachment.proposal_confirm",
        target_type="attachment_edit_proposal",
        target_id=proposal.proposal_id,
        actor_user_id=user_id,
        kb_name=str(getattr(rt, "kb_name", "") or ""),
        metadata={"session_id": session_id, "source": "chat"},
    )
    try:
        revision = service.confirm_attachment_proposal(
            proposal_id=proposal.proposal_id,
            session_id=session_id,
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001
        finish_attachment_audit(
            attempt,
            success=False,
            error_message=str(exc),
            metadata={"session_id": session_id, "source": "chat"},
        )
        return f"确认章节修改提案失败：{str(exc)[:200]}"

    from src.attachments.service import revision_change_summary

    change_summary = revision_change_summary(revision)
    finish_attachment_audit(
        attempt,
        success=True,
        target_type="attachment_revision",
        target_id=revision.revision_id,
        metadata={
            "session_id": session_id,
            "proposal_id": proposal.proposal_id,
            "parent_attachment_id": revision.parent_attachment_id,
            "child_attachment_id": revision.child_attachment_id,
            "operation": revision.operation,
            "change_summary": change_summary,
            "source": "chat",
        },
    )
    download_url = (
        f"/api/v1/conversations/{session_id}/attachments/"
        f"{revision.child_attachment_id}/download"
    )
    message = (
        f"已确认章节修改提案并生成候选（变更：{change_summary}）；"
        "原附件未修改，可下载候选复核。"
    )
    emit_event({
        "type": "attachment_revision",
        "payload": {
            "revision_id": revision.revision_id,
            "parent_attachment_id": revision.parent_attachment_id,
            "child_attachment_id": revision.child_attachment_id,
            "operation": revision.operation,
            "change_summary": change_summary,
            "download_url": download_url,
        },
    })
    emit_event({
        "type": "document_card",
        "payload": {
            "card": {
                "kind": "attachment_revision",
                "status": revision.status,
                "next_actions": [],
                "kb_name": str(getattr(rt, "kb_name", "") or ""),
                "content": message,
                "candidate_state": "available",
                "release_state": revision.release_state,
                "artifacts": [{
                    "artifact_id": revision.child_attachment_id,
                    "stage": revision.status,
                    "output_format": str((revision.details or {}).get("format") or ""),
                    "download_url": download_url,
                }],
            },
        },
    })
    return message


@dataclass
class _RunRecord:
    """Per-execution-context holder for the most recent stream's observability.

    MultiSourceAgentRunner is a process-wide singleton, so per-instance state
    would be shared across concurrent sessions. Scoping the record to a
    ContextVar gives each thread/session its own state; the public get_last_*
    API is unchanged.
    """

    footer: str = ""
    retrieval_summary: dict = field(default_factory=dict)
    token_usage_summary: object = None
    answer: str = ""


_RUN_RECORD: contextvars.ContextVar[_RunRecord | None] = contextvars.ContextVar(
    "agent_run_record", default=None
)


@dataclass
class _TurnPlan:
    """Everything a streaming turn needs before the first token is emitted.

    ``_prepare_turn`` builds this.  The split follows the natural yield
    boundary: the first ``yield`` used to sit ~1300 lines into ``_stream_impl``,
    so preparation is synchronous and the streaming section stays focused on
    event ordering, cancellation and citations.
    """

    record: _RunRecord
    emit_event: Callable[[dict], None]
    rt: Any
    tools: list[Any]
    system_prompt: str
    intent_plan: Any
    conversation_plan: Any
    is_general: bool
    direct_control_answer: str | None
    direct_answer_grounding: str
    context_fragments: list[tuple[str, str]] = field(default_factory=list)
    execution_tools: list[Any] = field(default_factory=list)
    skill_root: str | None = None
    mcp_tool_search_enabled: bool = False


def _current_run() -> _RunRecord:
    record = _RUN_RECORD.get()
    if record is None:
        record = _RunRecord()
        _RUN_RECORD.set(record)
    return record


_FAST_WORKFLOW = """本次为快速模式，请尽量精简：
1. 直接用最相关的检索工具检索 1-2 次（可跳过目录扫描）。
2. 拿到证据后立即给出简洁回答。
"""

_DEEP_WORKFLOW = """请充分检索后再回答：
"""









_CLARIFICATION_NEGATION_MARKERS = (
    "不要", "不确认", "不生成", "暂不", "先不", "取消", "停止", "别",
    "如果", "要是", "除非", "之后再", "但", "不过",
)
_CLARIFICATION_QUESTION_MARKERS = (
    "?", "？", "为什么", "怎么", "如何", "是否", "能否", "什么意思", "吗", "呢",
)
_CLARIFICATION_REQUEST_MARKERS = (
    "帮我", "帮忙", "麻烦", "新建", "重新生成", "重新做", "生成一个", "生成一份",
    "创建文档", "创建一份", "改成", "换成", "修改", "调整",
)
_FREETEXT_CLARIFICATION_QUESTION_IDS = {
    "target_identity", "source_scope", "outline", "purpose",
}
_KNOWLEDGE_BASE_DELEGATION_RE = re.compile(
    r"(?:根据|按照|按|基于|来自|从|依据)[^。；;，,\n]{0,32}?知识库"
    r"|由知识库[^。；;，,\n]{0,16}?(?:确定|决定|判断)"
    r"|你(?:自己)?(?:来)?(?:判断|决定|定)"
    r"|自行(?:判断|决定)"
    r"|自动(?:确定|判断|决定)",
    re.IGNORECASE,
)








def _coerce_tool_payload(raw: Any) -> dict[str, Any]:
    """Normalize a tool return value to a JSON object.

    Tools normally return an object-shaped JSON string, but a defensive
    normalization keeps a malformed payload from escaping the continuation
    handler and flipping an already-recorded clarification answer to rejected.
    """

    if isinstance(raw, str):
        raw = json.loads(raw)
    return raw if isinstance(raw, dict) else {}
















_READ_ONLY_ACTION_KINDS = frozenset({"search", "inspect", "compare", "summarize"})


def _mapping_from_value(value: Any) -> dict[str, Any] | None:
    """Return a JSON-like mapping from common tool result shapes."""

    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            result = dump(mode="json")
        except TypeError:
            result = dump()
        return result if isinstance(result, dict) else None
    return None


def _walk_action_payload(value: Any):
    """Yield bounded mapping/list values nested in an action receipt."""

    mapping = _mapping_from_value(value)
    if mapping is not None:
        yield mapping
        for nested in mapping.values():
            yield from _walk_action_payload(nested)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk_action_payload(nested)






_OUTPUT_FORMAT_LABELS = {
    "md": "Markdown",
    "markdown": "Markdown",
    "docx": "Word/DOCX",
    "xlsx": "Excel/XLSX",
    "xlsm": "Excel XLSM",
    "pdf": "PDF",
    "pptx": "PowerPoint/PPTX",
}


def _direct_intent_clarification_answer(plan: IntentPlan | None) -> str | None:
    """Return a safe clarification for an underspecified export request.

    Known output-contract errors need a server-owned answer before the model
    loop runs.  Only deterministic reason codes are handled here; an
    unrecognised model/intent clarification is left to the normal assistant.
    """

    if plan is None or plan.action != "clarify":
        return None
    reasons = set(str(item).strip() for item in plan.reason_codes if str(item).strip())
    if "multiple_output_formats" in reasons:
        labels = [
            _OUTPUT_FORMAT_LABELS.get(str(item).strip().casefold(), str(item).strip())
            for item in plan.requested_formats
            if str(item).strip()
        ]
        requested = "、".join(dict.fromkeys(labels)) or "多个格式"
        return (
            f"这条文档请求同时指定了 {requested}，一次只能选择一种输出格式。"
            "请明确选择 Word/DOCX、PDF、Excel/XLSX、PowerPoint/PPTX 或 Markdown；"
            "系统不会自动选择格式，也不会先生成其中一种。"
        )
    if "unsupported_output_format" in reasons:
        return (
            "当前文档生成不支持你指定的输出格式。"
            "支持 Word/DOCX、PDF、Excel/XLSX、PowerPoint/PPTX 或 Markdown；"
            "请改选一种支持的格式后再生成。"
        )
    if "ambiguous_document_request" in reasons:
        return (
            "请先明确希望生成的格式：Word/DOCX、PDF、Excel/XLSX、"
            "PowerPoint/PPTX 或 Markdown；确认后我再提交候选文档任务。"
        )
    return None


def _finalize_task_attempt(
    *,
    store: Any,
    task: Any,
    partial: bool,
    answer_text: str = "",
    evidence_count: int = 0,
) -> None:
    """Sync task status/artifacts/verification after one attempt."""

    try:
        from src.agents.task_verification import build_task_verification

        artifacts = store.scan_workspace_artifacts(task.id)
        kb_mounted = bool(getattr(task, "kb_name", "")) and task.kb_name != "__general__"
        verification = build_task_verification(
            artifacts=artifacts,
            answer_text=answer_text,
            evidence_count=evidence_count,
            partial=partial,
            kb_mounted=kb_mounted,
        )
        store.set_verification(task.id, verification)
        store.set_status(task.id, "paused" if partial else "completed")
    except Exception:
        # Task bookkeeping must never fail a user-visible answer.
        pass


class MultiSourceAgentRunner:
    def __init__(
        self,
        *,
        rag_backend: RAGBackend,
        document_store: PipelineDocumentStore | None = None,
        spreadsheet_service: SpreadsheetIndexService | None = None,
        circuit_service: CircuitIndexService | None = None,
        conversation_service=None,
        memory_service_factory: Callable[[], MemoryService] | None = None,
        wiki_service: Any | None = None,
    ):
        self.rag_backend = rag_backend
        self.document_store = document_store or PipelineDocumentStore()
        self.spreadsheet_service = spreadsheet_service or SpreadsheetIndexService()
        self.circuit_service = circuit_service or CircuitIndexService()
        if conversation_service is None:
            from src.external_conversations.query_engine import ExternalConversationQueryEngine

            conversation_service = ExternalConversationQueryEngine()
        self.conversation_service = conversation_service
        self.memory_service_factory = memory_service_factory or MemoryService
        self.wiki_service = wiki_service
        self.conversation_orchestrator = ConversationOrchestrator()
        # A deployment may inject a structured model caller here.  The
        # default parser is deliberately deterministic unless one is supplied,
        # so merely enabling the route never grants a model arbitrary writes.
        self._model_lock = threading.Lock()

    @staticmethod
    def _numeric_identity(value: Any) -> int | None:
        """Convert durable user/session ids to integers, failing closed."""
        try:
            normalized = str(value or "").strip()
            if not normalized or not normalized.isdigit():
                return None
            number = int(normalized)
            return number if number > 0 else None
        except (TypeError, ValueError):
            return None

    def _field_plan_for_targets(self, query: str, targets: Sequence) -> Any | None:
        if not bool(getattr(settings, "MULTI_KB_EXPLICIT_FIELDS_ENABLED", False)):
            return None
        try:
            from src.services.field_requirements import FieldCoveragePlan, extract_requirements

            requirements = extract_requirements(query, targets)
            return FieldCoveragePlan(requirements) if requirements else None
        except Exception:
            return None

    def _history_target_hint(
        self, *, action_scope: Any, user_id: int | None, session_id: int | None,
    ) -> list[dict]:
        """Read the nearest completed turn's server target summary."""
        if user_id is None or session_id is None:
            return []
        current_turn_id = ""
        if isinstance(action_scope, dict):
            current_turn_id = str(action_scope.get("turn_id") or "")
        try:
            from src.core.conversation import ConversationService
            from src.services.retrieval_targets import history_target_hint

            for turn in ConversationService().list_recent_turns(int(user_id), int(session_id), limit=5):
                if turn.id == current_turn_id or turn.status != "completed":
                    continue
                hint = history_target_hint(turn.summary)
                if hint:
                    return hint
        except Exception:
            return []
        return []

    def _get_model(self):
        # create_chat_model is lru_cached; the lock avoids duplicate builds on
        # first concurrent requests after startup.
        with self._model_lock:
            return create_chat_model()

    @staticmethod

    @staticmethod
    def _context_has_permission(ctx: RequestContext | None, kb_name: str, permission: str) -> bool:
        if ctx is None or not str(kb_name or "").strip():
            return False
        try:
            return bool(ctx.has_kb_permission(str(kb_name), permission))
        except Exception:
            return False


    def _conversation_capabilities(
        self,
        *,
        ctx: RequestContext | None,
        kb_name: str,
        has_attachments: bool,
        has_mcp_access: bool = False,
        has_mcp_write_access: bool = False,
    ) -> tuple[CapabilityResolver, set[str]]:
        """Resolve the server-owned capability boundary for this turn."""

        has_kb_read = self._context_has_permission(ctx, kb_name, "read")
        has_kb_write = self._context_has_permission(ctx, kb_name, "write")
        authorized = authorized_capabilities_for_conversation(
            has_kb_read=has_kb_read,
            has_kb_write=has_kb_write,
            has_attachments=has_attachments,
            has_mcp_access=has_mcp_access,
            has_mcp_write_access=has_mcp_write_access,
        )
        resolver = CapabilityResolver(deployed=deployed_capabilities_from_settings())
        return resolver, authorized



    def _prefetch_memory_context(
        self,
        *,
        query: str,
        kb_name: str,
        ctx: RequestContext | None,
        rt: ToolRuntime,
    ) -> str:
        """Fetch bounded memory context before creating the DeepAgent.

        Memory remains a fail-open hint: missing identity, unavailable Store,
        disabled memory, or any other read failure must not make the hardware
        query fail.  The fixed formatter supplies the untrusted-data boundary;
        the Agent still has to retrieve formal evidence before answering.
        """
        if ctx is None or not str(getattr(ctx, "user_id", "") or "").strip():
            return ""
        rt.check_cancel()
        started = time.monotonic()
        call_id = rt.next_invocation_id()
        rt.tool_started(
            call_id=call_id,
            tool_name="memory_search",
            query=query,
            automatic=True,
        )
        service = None
        try:
            service = self.memory_service_factory()
            rows = service.search(
                query,
                request_context=ctx,
                actor=None,
                scope="all",
                status="all",
                top_k=None,
                kb_name=kb_name,
            )
            public = [memory_context_item(row) for row in rows]
            rt.add_memory_context(public)
            context = service.format_context(public)
            rt.log_query("memory_search", query)
            rt.record_diagnostic(
                ToolDiagnostics(
                    tool_name="memory_search",
                    hit_count=len(rows),
                    latency_ms=int((time.monotonic() - started) * 1000),
                    filters={"scope": "all", "status": "all", "automatic": "true"},
                )
            )
            rt.tool_result(
                call_id=call_id,
                tool_name="memory_search",
                hit_count=len(rows),
                status="done",
                latency_ms=int((time.monotonic() - started) * 1000),
                automatic=True,
            )
            if context:
                counter("hdb.memory.injected", attributes={"scope": "all"}, value=len(rows))
            return context
        except QueryCancelled:
            raise
        except Exception as exc:
            # Memory is an optional hint and must never take down the formal
            # document/circuit retrieval path.
            rt.record_diagnostic(
                ToolDiagnostics(
                    tool_name="memory_search",
                    status="failed",
                    error=str(exc)[:300],
                    latency_ms=int((time.monotonic() - started) * 1000),
                    filters={"scope": "all", "status": "all", "automatic": "true"},
                )
            )
            rt.tool_result(
                call_id=call_id,
                tool_name="memory_search",
                hit_count=0,
                status="failed",
                latency_ms=int((time.monotonic() - started) * 1000),
                error=str(exc)[:300],
                automatic=True,
            )
            return ""
        finally:
            if service is not None:
                close = getattr(service, "close", None)
                if callable(close):
                    close()



    def stream(
        self,
        *,
        query: str,
        kb_name: str,
        history: list[tuple[str, str]],
        ctx: RequestContext | None = None,
        thread_id: str = "",
        event_callback: Callable[[dict], None] | None = None,
        query_mode: str = "deep",
        should_cancel: Callable[[], bool] | None = None,
        persist_thread: bool = False,
        wiki_assist: bool = False,
        attachments: list[Any] | None = None,
        source_scope: str = "auto",
        attachment_user_id: int | None = None,
        conversation_session_id: int | None = None,
        action_scope: Any | None = None,
        knowledge_scope: KnowledgeScopeSnapshot | None = None,
        knowledge_scope_resolver: KnowledgeScopeResolver | None = None,
        task_id: str = "",
    ) -> Generator[str, None, None]:
        """Observability wrapper: one agent span + metrics per run."""
        started = time.monotonic()
        status = "success"
        _agent_active(1)
        with observe.agent(
            "hdb.agent.run",
            **{
                "hdb.query.mode": query_mode,
                "hdb.query.source": "multi_source_agent",
                "hdb.session.id": thread_id,
            },
        ) as observation:
            observation.set_input(query, content_kind="query")
            try:
                yield from self._stream_impl(
                    query=query,
                    kb_name=kb_name,
                    history=history,
                    ctx=ctx,
                    thread_id=thread_id,
                    event_callback=event_callback,
                    query_mode=query_mode,
                    should_cancel=should_cancel,
                    persist_thread=persist_thread,
                    wiki_assist=wiki_assist,
                    attachments=attachments,
                    source_scope=source_scope,
                    attachment_user_id=attachment_user_id,
                    conversation_session_id=conversation_session_id,
                    action_scope=action_scope,
                    knowledge_scope=knowledge_scope,
                    knowledge_scope_resolver=knowledge_scope_resolver,
                    task_id=task_id,
                )
            except QueryCancelled:
                status = "cancelled"
                try:
                    from src.observability.metrics import record_agent_cancel

                    record_agent_cancel(reason="user_or_timeout")
                except Exception:
                    pass
                raise
            except Exception as exc:
                status = "failed"
                observation.error(exc)
                raise
            finally:
                _agent_active(-1)
                summary = self.get_last_retrieval_summary()
                try:
                    from src.observability.metrics import record_agent_step

                    record_agent_step(
                        step_count=len(summary.get("tool_diagnostics") or []) + 1,
                        status=status,
                    )
                except Exception:
                    pass
                run_record = _current_run()
                observation.set_token_usage(run_record.token_usage_summary if run_record is not None else None)
                if run_record.answer:
                    observation.set_output(run_record.answer, content_kind="llm")
                observation.set("hdb.agent.retrieval_round", int(summary.get("retrieval_rounds") or 0))
                observation.set("hdb.evidence.count", len(summary.get("evidence") or []))
                observation.set("hdb.retrieval.calls", len(summary.get("tool_diagnostics") or []))
                observation.set(
                    "hdb.retrieval.hits",
                    sum(int(item.get("hit_count") or 0) for item in summary.get("tool_diagnostics") or []),
                )
                observation.set("hdb.retrieval.final_top_k", int(summary.get("final_top_k") or 0))
                observation.set("hdb.retriever.type", summary.get("retriever_type") or "")
                observation.set("hdb.retrieval.status", summary.get("status") or "")
                if summary.get("rewritten_queries"):
                    observation.set(
                        "hdb.query.rewritten",
                        json.dumps(summary.get("rewritten_queries"), ensure_ascii=False),
                    )
                observation.set("hdb.agent.status", status)
                observation.outcome(status)
                record_agent(
                    status=status,
                    mode=query_mode,
                    duration_s=max(0.0, time.monotonic() - started),
                    retrieval_rounds=int(summary.get("retrieval_rounds") or 0),
                )

    def _prepare_turn(
        self,
        *,
        query: str,
        kb_name: str,
        history: list[tuple[str, str]],
        ctx: RequestContext | None = None,
        thread_id: str = "",
        event_callback: Callable[[dict], None] | None = None,
        query_mode: str = "deep",
        should_cancel: Callable[[], bool] | None = None,
        persist_thread: bool = False,
        wiki_assist: bool = False,
        attachments: list[Any] | None = None,
        source_scope: str = "auto",
        attachment_user_id: int | None = None,
        conversation_session_id: int | None = None,
        action_scope: Any | None = None,
        knowledge_scope: KnowledgeScopeSnapshot | None = None,
        knowledge_scope_resolver: KnowledgeScopeResolver | None = None,
    ) -> "_TurnPlan":
        """Build the synchronous part of a turn before streaming starts.

        Routing, tool assembly, prompt construction, document-context
        recovery and memory prefetch all happen here.  None of it yields,
        so it is testable without a generator and the streaming section in
        ``_stream_impl`` only has to reason about event ordering.
        """
        record = _RunRecord()
        _RUN_RECORD.set(record)

        if knowledge_scope is not None and not isinstance(knowledge_scope, KnowledgeScopeSnapshot):
            knowledge_scope = KnowledgeScopeSnapshot.model_validate(knowledge_scope)
        if knowledge_scope is not None and knowledge_scope_resolver is not None:
            # This is the model-submission gate. Retrieval performs the same
            # check again before and after every source call.
            knowledge_scope_resolver.revalidate(
                knowledge_scope.actor_user_id, knowledge_scope
            )

        kb_label = str(kb_name or "").strip()
        is_general = (not kb_label) or kb_label == GENERAL_CHAT_KB_NAME
        requested_attachment_refs = [
            ref for ref in (attachments or []) if getattr(ref, "asset_id", "")
        ]
        requested_has_attachments = bool(requested_attachment_refs)
        # Scope-based assembly (design §5.2/§11.3): the durable scope decided
        # at turn creation wins; "auto" expands deterministically here.
        from src.attachments.models import (
            resolve_source_scope,
            scope_allows_attachments,
        )

        scope = resolve_source_scope(
            requested_scope=source_scope,
            has_attachments=requested_has_attachments,
            is_general_chat=is_general,
        )
        attachment_refs = (
            requested_attachment_refs if scope_allows_attachments(scope) else []
        )
        has_attachments = bool(attachment_refs)
        # The checkpoint thread is attempt-scoped and may be opaque. File ACLs
        # and document-task recovery use the durable conversation identity.
        logical_session_id = (
            self._numeric_identity(conversation_session_id)
            or self._numeric_identity(getattr(ctx, "session_id", None))
            or self._numeric_identity(thread_id)
        )
        logical_session = str(logical_session_id or thread_id or getattr(ctx, "session_id", "") or "")
        effective_scope = scope
        # The governed unified surface requires an authenticated RequestContext
        # for capability/MCP resolution.  Internal compatibility callers that
        # omit it keep the legacy assembly; they cannot accidentally trigger a
        # half-authorized unified/MCP path.
        unified_tools_enabled = bool(ctx is not None) and bool(
            getattr(settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", False)
        )
        if is_general:
            # 通用对话与知识库问答共用同一个 deepagents agent 循环、同一个模型
            # 接口和同一个系统提示词来源；唯一差异是不注册检索工具。
            scope_kb = ""
            kb_scope_line = (
                "当前未挂载知识库，进行通用对话。不要声称读取了任何私有文档、表格或电路"
                "数据；如用户需要基于知识库资料回答，请提示先在对话侧栏挂载知识库。"
            )
            workflow = ""
        else:
            scope_kb = kb_label
            kb_scope_line = f"当前知识库为「{kb_label}」。"
            workflow = _DEEP_WORKFLOW if query_mode == "deep" else _FAST_WORKFLOW
            if knowledge_scope is not None:
                selected = "、".join(
                    f"{source.kb_name}（{source.role}）"
                    for source in knowledge_scope.sources
                )
                kb_scope_line = (
                    f"当前受控知识库范围：{selected}。每条结论必须保留真实来源身份；"
                    "未列入范围的知识库不得检索。"
                )
        if has_attachments:
            names = "、".join(str(getattr(ref, "filename", "") or "附件") for ref in attachment_refs[:5])
            attachment_scope_line = (
                f"本轮用户提供了 {len(attachment_refs)} 个会话附件（{names}）。"
                "附件内容只能通过 attachment_* 工具读取，"
                "不要声称直接读取了本地文件。"
            )
            attachment_scope_line += (
                "用户要求修改附件时，用 attachment_edit 生成候选副本："
                "replace_text 精确替换唯一文本；"
                "replace_section 按 heading_path 标题替换整节正文（其余章节保持不变；"
                "新正文自动沿用原标题格式与原节样式/字体/段落格式，原节表格、图片"
                "与嵌入对象默认移至节末保留；需要放回指定位置时用单独一行 "
                "[[embed]] 或 [[embed:N]] 占位符；仅当用户明确要求删除这些对象时才传 "
                "preserve_embedded=false）；"
                "append_paragraphs 在指定章节末尾（不传 heading_path 则文档末尾）"
                "追加段落；"
                "set_cell/replace_cell_text 修改 .xlsx/.xlsm 单元格；"
                "PDF 用 annotate_text 生成批注候选（原件不变）。"
                "未指明版本时默认修改最新修订候选；可用 attachment_revision_history "
                "查看版本链、变更摘要与发布状态。候选不会覆盖原附件，"
                "完成后告知下载地址。用户要求正式发布候选时，用 "
                "request_attachment_release 提交审核，不会直接发布。"
            )
            if (
                _ATTACHMENT_EDIT_ACTION_PATTERN.search(query)
                and _ATTACHMENT_SECTION_TARGET_PATTERN.search(query)
            ):
                # “检索知识库并完善附件第N章，然后替换/输出新文档” is an
                # execution task, not a Q&A: models otherwise stop after a
                # retrieval summary.  Direct them to produce the candidate.
                attachment_scope_line += (
                    "本轮是附件文档修改任务而非问答：先检索/读取附件定位目标章节，"
                    "再按需检索知识库补齐内容，然后必须调用 attachment_edit"
                    "（章节替换用 operation=replace_section 且给出完整新正文；"
                    "其余章节保持不变）生成候选副本，最后给出候选下载地址与变更摘要；"
                    "不要只输出检索总结，也不要要求用户再次确认（候选本身即可复核）。"
                )
            kb_scope_line = f"{kb_scope_line}\n{attachment_scope_line}"

        def emit_event(evt: dict) -> None:
            # 通用对话可以在内部读取长期记忆，但这是上下文 plumbing，不应在
            # 用户可见的检索执行轨迹中显示。故只过滤展示事件，内部诊断仍保留。
            # MCP 外部调用是用户显式订阅的服务，必须保留时间线可见性。
            if is_general and evt.get("type") in {"stage", "tool_started", "tool_result", "narration"}:
                payload = evt.get("payload") or {}
                if payload.get("tool_kind") not in {"mcp", "sandbox"}:
                    return
            # Validate before the callback so a typo cannot enter the durable
            # turn stream through the runner path.
            event = validate_event_envelope(evt)
            if event_callback is not None:
                try:
                    event_callback(event)
                except Exception:
                    pass

        # RequestContext.user_id is the username by design; the durable turn
        # worker passes AuthUser.id explicitly.  Attachment ACLs and MCP
        # subscriptions share this one numeric identity.
        numeric_user_id = self._numeric_identity(attachment_user_id) or self._numeric_identity(
            getattr(ctx, "user_id", None)
        )
        rt = ToolRuntime(
            kb_name=scope_kb,
            ctx=ctx,
            chat_session_id=logical_session,
            top_k=8 if query_mode == "deep" else 5,
            query_mode=query_mode,
            question=query,
            evidence_budget=_agent_evidence_budget(attachment_refs, question=query),
            should_cancel=should_cancel,
            on_event=emit_event,
            attachment_refs=attachment_refs,
            source_scope=effective_scope,
            # RequestContext.user_id is the username by design; the durable
            # turn worker passes AuthUser.id explicitly for attachment ACLs.
            attachment_user_id=numeric_user_id,
            attachment_session_id=logical_session_id,
            attachment_edit_enabled=True,
            knowledge_scope=knowledge_scope,
            document_store=self.document_store,
        )
        rt.candidate_mode = not is_general
        if rt.candidate_mode:
            load_session_context(rt, conversation_session_id, numeric_user_id)
        if knowledge_scope is not None:
            from src.services.retrieval_targets import compile_work_items, looks_like_follow_up, resolve_turn_targets

            history_hint = None
            if looks_like_follow_up(query):
                history_hint = self._history_target_hint(
                    action_scope=action_scope, user_id=numeric_user_id,
                    session_id=conversation_session_id,
                )
            rt.turn_retrieval_targets = resolve_turn_targets(
                query,
                sources=knowledge_scope.sources,
                document_store=self.document_store,
                turn_id=str(thread_id or ""),
                scope_fingerprint=knowledge_scope.selection_hash,
                history_hint=history_hint,
            )
            if rt.turn_retrieval_targets.resolution == "resolved":
                rt.turn_work_items = compile_work_items(rt.turn_retrieval_targets.targets)
                from src.services.turn_evidence_plan import TurnEvidencePlan

                rt.turn_evidence_plan = TurnEvidencePlan.from_targets(
                    rt.turn_retrieval_targets.targets,
                    turn_id=str(thread_id or ""),
                    selected_kb_ids=[source.kb_id for source in knowledge_scope.sources],
                )
                rt.field_evidence_plan = self._field_plan_for_targets(
                    query, rt.turn_retrieval_targets.targets,
                )
            if knowledge_scope_resolver is None:
                # Direct callers may inject a service in tests, but the
                # authenticated worker always supplies the live resolver.
                rt.multi_kb_retrieval = None
            else:
                from src.services.structured_retrieval import build_structured_retrieval_adapters

                rt.multi_kb_retrieval = MultiKnowledgeRetrievalService(
                    knowledge_scope_resolver, self.rag_backend,
                    adapters=build_structured_retrieval_adapters(
                        document_store=self.document_store,
                        spreadsheet_service=self.spreadsheet_service,
                        circuit_service=self.circuit_service,
                        conversation_service=self.conversation_service,
                        wiki_service=self.wiki_service,
                    ),
                )
            rt.retrieval_budget = RetrievalBudget(
                max_calls=int(getattr(settings, "MULTI_KB_MAX_CALLS_PER_TURN", 24)),
                timeout_seconds=float(
                    getattr(settings, "MULTI_KB_RETRIEVAL_DEADLINE_SECONDS", 30)
                ),
                should_cancel=should_cancel,
            )
        elif rt.kb_name and ctx is not None:
            # Single-KB compatibility uses the same target shape without
            # fabricating a multi-KB knowledge scope or selection hash.
            from types import SimpleNamespace

            from src.services.retrieval_targets import compile_work_items, looks_like_follow_up, resolve_turn_targets
            from src.services.turn_evidence_plan import TurnEvidencePlan

            metadata = dict(getattr(ctx, "metadata", {}) or {})
            kb_id = metadata.get("kb_id")
            if kb_id:
                source = SimpleNamespace(
                    kb_id=int(kb_id), kb_name=rt.kb_name,
                    department_id=str(metadata.get("resource_department_id") or metadata.get("department_id") or ""),
                    role="primary",
                )
                history_hint = None
                if looks_like_follow_up(query):
                    history_hint = self._history_target_hint(
                        action_scope=action_scope, user_id=numeric_user_id,
                        session_id=conversation_session_id,
                    )
                rt.turn_retrieval_targets = resolve_turn_targets(
                    query, sources=[source], document_store=self.document_store,
                    turn_id=str(thread_id or ""), scope_fingerprint=f"kb:{kb_id}",
                    history_hint=history_hint,
                )
                if rt.turn_retrieval_targets.resolution == "resolved":
                    rt.turn_work_items = compile_work_items(rt.turn_retrieval_targets.targets)
                    rt.turn_evidence_plan = TurnEvidencePlan.from_targets(
                        rt.turn_retrieval_targets.targets, turn_id=str(thread_id or ""),
                        selected_kb_ids=[int(kb_id)],
                    )
                    rt.field_evidence_plan = self._field_plan_for_targets(
                        query, rt.turn_retrieval_targets.targets,
                    )
        if attachment_refs:
            from src.attachments.service import AttachmentService

            rt.attachment_service = AttachmentService()

        intent_plan = classify_intent(
            query,
            has_attachments=has_attachments,
            has_kb=not is_general,
        )
        conversation_plan = self.conversation_orchestrator.plan(
            query=query,
            has_attachments=has_attachments,
            has_kb=not is_general,
            source_scope=effective_scope,
        )
        from src.agents.turn_evidence_runtime import configure_turn_evidence

        configure_turn_evidence(
            rt=rt,
            conversation_plan=conversation_plan,
            intent_plan=intent_plan,
            effective_scope=effective_scope,
            has_attachments=has_attachments,
            has_kb=not is_general,
        )
        # Resolve the MCP server snapshot BEFORE capabilities: visibility comes
        # from the authenticated turn (role/department/subscription), and its
        # result feeds has_mcp_access.  A capability never decides visibility.
        mcp_snapshot = (
            resolve_visible_servers(ctx, user_id=numeric_user_id)
            if unified_tools_enabled
            else McpServerSnapshot()
        )
        mcp_department_id: Any = None
        if ctx is not None:
            metadata = dict(getattr(ctx, "metadata", {}) or {})
            mcp_department_id = metadata.get("resource_department_id") or metadata.get("department_id")
        capability_resolver, authorized_capabilities = self._conversation_capabilities(
            ctx=ctx,
            kb_name=scope_kb,
            has_attachments=has_attachments,
            has_mcp_access=bool(mcp_snapshot.servers),
            has_mcp_write_access=bool(mcp_snapshot.allow_write_slugs)
            and bool(getattr(settings, "MCP_WRITE_ENABLED", False)),
        )
        effective_capabilities = capability_resolver.effective(
            authorized=authorized_capabilities,
            source_scope=effective_scope,
        )
        memory_context = (
            self._prefetch_memory_context(
                query=query,
                kb_name=scope_kb,
                ctx=ctx,
                rt=rt,
            )
            if rt.knowledge_scope is None
            else ""
        )
        skill_root = None
        if not is_general:
            skill_root = materialize_text_skills(ctx)
        skill_context = (
            ""
            if skill_root
            else load_skill_prompt_context(
                ctx,
                on_error=lambda exc: emit_event(
                    {
                        "type": "degraded",
                        "payload": {
                            "stage": "skill_context",
                            "component": "skill",
                            "failure_type": type(exc).__name__,
                            "capability": "unavailable",
                        },
                    }
                ),
            )
        )
        tool_bundle = build_turn_tools(
            rt=rt,
            ctx=ctx,
            is_general=is_general,
            wiki_assist=wiki_assist,
            effective_scope=effective_scope,
            unified_tools_enabled=unified_tools_enabled,
            effective_capabilities=effective_capabilities,
            mcp_snapshot=mcp_snapshot,
            mcp_department_id=mcp_department_id,
            metadata=dict(getattr(ctx, "metadata", {}) or {}),
            numeric_user_id=numeric_user_id,
            rag_backend=self.rag_backend,
            document_store=self.document_store,
            spreadsheet_service=self.spreadsheet_service,
            circuit_service=self.circuit_service,
            conversation_service=self.conversation_service,
            wiki_service=self.wiki_service,
            memory_service_factory=self.memory_service_factory,
            emit_event=emit_event,
        )
        tools = tool_bundle.tools
        from src.result_exports import infer_export_intent

        turn_prompt = build_turn_prompt(
            kb_name=scope_kb,
            workflow=workflow,
            kb_scope_line=kb_scope_line,
            is_general=is_general,
            wiki_assist=wiki_assist,
            memory_context=memory_context,
            skill_context=skill_context,
            tool_fragments=tool_bundle.fragments,
            tool_suffix=tool_bundle.prompt_suffix,
            skill_present=bool(skill_root),
            export=infer_export_intent(query) is not None,
        )
        system_prompt = turn_prompt.system_prompt
        context_fragments = turn_prompt.fragments

        direct_control_answer: str | None = None
        direct_answer_grounding = "intent_clarification"
        direct_control_answer = _direct_intent_clarification_answer(intent_plan)

        if direct_control_answer is None and has_attachments:
            # Governed two-stage section edits: the user's confirmation (or
            # cancellation) executes deterministically, never via the model.
            proposal_answer = _direct_attachment_proposal_answer(
                rt=rt, query=query, emit_event=emit_event
            )
            if proposal_answer is not None:
                direct_control_answer = proposal_answer
                direct_answer_grounding = "attachment_proposal"

        return _TurnPlan(
            record=record,
            emit_event=emit_event,
            rt=rt,
            tools=tools,
            system_prompt=system_prompt,
            intent_plan=intent_plan,
            conversation_plan=conversation_plan,
            is_general=is_general,
            direct_control_answer=direct_control_answer,
            direct_answer_grounding=direct_answer_grounding,
            context_fragments=context_fragments,
            execution_tools=tool_bundle.execution_tools,
            skill_root=skill_root,
            mcp_tool_search_enabled=tool_bundle.mcp_tool_search_enabled,
        )


    def _stream_impl(
        self,
        *,
        query: str,
        kb_name: str,
        history: list[tuple[str, str]],
        ctx: RequestContext | None = None,
        thread_id: str = "",
        event_callback: Callable[[dict], None] | None = None,
        query_mode: str = "deep",
        should_cancel: Callable[[], bool] | None = None,
        persist_thread: bool = False,
        wiki_assist: bool = False,
        attachments: list[Any] | None = None,
        source_scope: str = "auto",
        attachment_user_id: int | None = None,
        conversation_session_id: int | None = None,
        action_scope: Any | None = None,
        knowledge_scope: KnowledgeScopeSnapshot | None = None,
        knowledge_scope_resolver: KnowledgeScopeResolver | None = None,
        task_id: str = "",
    ) -> Generator[str, None, None]:
        """Run the deep agent and stream answer deltas live.

        Text of the in-flight model message is yielded immediately as
        provisional answer; a message that turns out to carry tool calls is
        reclassified as interim narration and announced via ``event_callback``
        as a ``narration`` event so consumers can retract the already-streamed
        text (see ``strip_narration_segments``). Other side-channel events
        (stage / tool_started / tool_result / degraded) are forwarded as
        ``{"type": ..., "payload": {...}}`` dicts via ``event_callback``.
        """
        plan = self._prepare_turn(
            query=query,
            kb_name=kb_name,
            history=history,
            ctx=ctx,
            thread_id=thread_id,
            event_callback=event_callback,
            query_mode=query_mode,
            should_cancel=should_cancel,
            persist_thread=persist_thread,
            wiki_assist=wiki_assist,
            attachments=attachments,
            source_scope=source_scope,
            attachment_user_id=attachment_user_id,
            conversation_session_id=conversation_session_id,
            action_scope=action_scope,
            knowledge_scope=knowledge_scope,
            knowledge_scope_resolver=knowledge_scope_resolver,
        )
        record = plan.record
        emit_event = plan.emit_event
        rt = plan.rt
        tools = plan.tools
        system_prompt = plan.system_prompt
        intent_plan = plan.intent_plan
        conversation_plan = plan.conversation_plan
        is_general = plan.is_general
        direct_control_answer = plan.direct_control_answer
        direct_answer_grounding = plan.direct_answer_grounding
        skill_root = plan.skill_root

        # General-agent task mode: a durable task supplies the workspace and an
        # *optional* external definition; there is no fixed workflow here.
        task = None
        task_store = None
        if task_id:
            from src.agents.task_definitions import build_task_prompt, resolve_definition
            from src.agents.tools.task_tools import build_task_tools
            from src.agents.tools.workspace_tools import build_workspace_tools
            from src.core.task_store import TaskStore

            task_store = TaskStore()
            task = task_store.get_task(str(task_id))
            if task is not None:
                definition = resolve_definition(task.definition_id)
                task_artifacts = task_store.list_artifacts(task.id)
                tools = (
                    list(tools)
                    + build_workspace_tools(task.workspace_dir, rt)
                    + build_task_tools(task.id, task_store, rt)
                )
                system_prompt = system_prompt + build_task_prompt(
                    task, definition, task_artifacts
                )

        if direct_control_answer is not None:
            record.answer = direct_control_answer
            _current_run().answer = direct_control_answer
            record.footer = ""
            record.token_usage_summary = TokenUsageSummary(
                provider="server",
                model="server-controlled",
            )
            record.retrieval_summary = {
                "status": "success",
                "error_stage": "",
                "error_message": "",
                "rewritten_queries": [],
                "retriever_type": "server",
                "final_top_k": 0,
                "evidence": [],
                "memory_context": [],
                "missing": [],
                "retrieval_rounds": 0,
                "sufficiency_status": "not_applicable",
                "trace": [],
                "tool_diagnostics": [],
                "claim_coverage": [],
                "retrieval_ledger": [],
                "evidence_quality": [],
                "verification": {
                    "grounded": False,
                    "grounding_method": direct_answer_grounding,
                    "unsupported_claims": [],
                    "weak_claims": [],
                    "conflicts": [],
                    "citation_coverage": 0.0,
                },
                "intent_plan": intent_plan.to_dict(),
                "conversation_plan": conversation_plan.to_dict(),
            }
            record.retrieval_summary.update(_scope_dependency_fields(knowledge_scope))
            yield direct_control_answer
            return

        if knowledge_scope is not None and knowledge_scope_resolver is not None:
            knowledge_scope_resolver.revalidate(
                knowledge_scope.actor_user_id, knowledge_scope
            )
        model = self._get_model()
        recursion_limit, budget_state = _agent_recursion_limit(
            query=query,
            query_mode=query_mode,
            attachment_refs=list(getattr(rt, "attachment_refs", []) or []),
            knowledge_scope=knowledge_scope,
        )
        session = prepare_session(
            thread_id=thread_id,
            persist_thread=persist_thread,
            recursion_limit=recursion_limit,
            history=history,
            query=query,
            checkpointer_factory=_create_checkpointer,
        )
        checkpointer = session.checkpointer
        budget_state.update(
            {
                "status": "allocated",
                "exhausted": False,
                "tool_calls": 0,
                "evidence_count": 0,
                "partial_answer": False,
            }
        )
        show_turn_meta = (
            str(budget_state["complexity"].get("level") or "simple") != "simple"
        )
        if show_turn_meta:
            emit_event({"type": "budget", "payload": dict(budget_state)})

        agent_subagents: list[dict[str, Any]] | None = None
        if (
            RESEARCH_SUBAGENT_ENABLED
            and query_mode == "deep"
            and str(budget_state["complexity"].get("level") or "simple") == "complex"
        ):
            research = _research_subagent_spec(
                tools=plan.execution_tools, model=model, skill_root=skill_root
            )
            if research is not None:
                agent_subagents = [research]
        if show_turn_meta and query_mode == "deep":
            system_prompt = _complex_task_prompt(
                system_prompt, has_subagents=bool(agent_subagents)
            )
        if not is_general:
            system_prompt += (
                "\n引用编号只对当前轮有效；不要复用上一轮回答或对话历史中的 [n]，"
                "必须使用本轮工具返回的证据编号。"
            )
        if skill_root:
            system_prompt += (
                "\n本轮 read_file 仅用于读取已审核并物化的 Skill 的 SKILL.md，"
                "不得读取或尝试读取其他路径。"
            )
        tool_call_cap = budget_state.get("tool_call_cap")
        if tool_call_cap:
            system_prompt += (
                f"\n本轮所有工具调用总次数硬上限为 {int(tool_call_cap)} 次"
                "(用户在问题上指定的约束，包括检索、读证据等全部工具)。"
                "达到上限后系统会立即停止检索并转入部分答案合成；"
                "请把调用集中在最必要的查询上，不要重复相近的检索。"
            )
        turn_started = time.monotonic()
        pre_candidates = set(rt.candidate_evidence)
        loop_budget = LoopBudget(rt)
        loop_budget.tool_call_cap = tool_call_cap
        # Internal loop time limit. A declared tool-call cap already implies a
        # short turn, so the regular 480 s stop stands. Without a declared cap
        # a deep turn is completeness-first: it gets the external deep watchdog
        # (CHAT_QUEUE_DEADLINE_DEEP_SECONDS) minus a wrap-up margin, instead of
        # being cut at 480 s. Everything else keeps the regular stop. The
        # deadline is set before the agent is built so the middleware instance
        # bound into the graph always sees it.
        loop_seconds = 480
        if query_mode == "deep" and not loop_budget.tool_call_cap:
            loop_seconds = max(
                60,
                int(getattr(settings, "CHAT_QUEUE_DEADLINE_DEEP_SECONDS", 900)) - 120,
            )
        loop_budget.deadline = turn_started + loop_seconds
        gap_middleware = GapClosureMiddleware(
            rt=rt,
            pre_candidates=pre_candidates,
            turn_started=turn_started,
            is_general=is_general,
            query_mode=query_mode,
            has_checkpointer=checkpointer is not None,
        )
        agent_middleware: list[Any] = [LoopBudgetMiddleware(loop_budget)]
        if plan.mcp_tool_search_enabled:
            agent_middleware.append(McpToolSearchMiddleware(rt))
        agent_middleware.append(gap_middleware)
        agent = build_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            checkpointer=checkpointer,
            subagents=agent_subagents,
            session_state_text=getattr(rt, "session_state_text", ""),
            skill_root=skill_root,
            middleware=agent_middleware,
            factory=create_deep_agent,
        )
        config = session.config
        messages = session.messages
        usage = TokenUsageSummary(
            provider=str(type(model).__module__),
            model=str(getattr(model, "model_name", None) or getattr(model, "model", "") or ""),
        )
        _, initial_summarization = initial_context_state(
            agent, config, has_checkpointer=checkpointer is not None)

        timeline: list[dict[str, Any]] = []
        splitter = _AnswerSplitter(
            on_narration=lambda text: emit_event({"type": "narration", "payload": {"text": text}})
        )
        recursion_stopped = progress_stopped = time_stopped = cap_stopped = False
        seen_tool_calls: set[str] = set()
        subagents: list[dict[str, Any]] = []
        subagent_by_call: dict[str, dict[str, Any]] = {}

        def _stream_round(round_splitter: _AnswerSplitter):
            for chunk, metadata in agent.stream(
                {"messages": messages},
                config=config,
                stream_mode="messages",
            ):
                rt.check_cancel()
                node = str((metadata or {}).get("langgraph_node") or "")
                _accumulate_usage(usage, chunk)
                if isinstance(chunk, ToolMessage):
                    call_id = str(getattr(chunk, "tool_call_id", "") or "")
                    subagent = subagent_by_call.get(call_id)
                    if subagent is not None:
                        record_subagent_end(
                            subagent=subagent,
                            result_text=_extract_text_delta(chunk),
                            failed=str(getattr(chunk, "status", "") or "").lower() == "error",
                            emit_event=emit_event,
                        )
                    continue
                # Real providers stream AIMessageChunk pieces; fake/fallback
                # paths deliver a whole AIMessage per model call. Empty-text
                # chunks still matter: they may carry tool-call metadata that
                # reclassifies the segment as narration.
                if node != "model" or not isinstance(chunk, AIMessage):
                    continue
                for call in getattr(chunk, "tool_calls", None) or []:
                    if not isinstance(call, dict):
                        continue
                    call_name = str(call.get("name") or "").strip()
                    call_id = str(call.get("id") or "").strip()
                    args = call.get("args") if isinstance(call.get("args"), dict) else {}
                    fingerprint = call_id or f"{call_name}:{json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)}"
                    if not call_name or fingerprint in seen_tool_calls:
                        continue
                    if call_name == "task":
                        record_subagent_start(
                            args=args,
                            call_id=call_id,
                            fingerprint=fingerprint,
                            seen_tool_calls=seen_tool_calls,
                            subagents=subagents,
                            subagent_by_call=subagent_by_call,
                            emit_event=emit_event,
                        )
                delta = round_splitter.feed(chunk, _extract_text_delta(chunk))
                if delta:
                    yield delta

        from langgraph.errors import GraphRecursionError

        try:
            with bind_cancel_check(should_cancel):
                for delta in _stream_round(splitter):
                    yield delta
        except ProgressStopped:
            progress_stopped = True
        except TimeBudgetExceeded:
            time_stopped = True
        except CallBudgetExceeded:
            cap_stopped = True
        except QueryCancelled:
            return
        except Exception as exc:
            if isinstance(exc, GraphRecursionError):
                # A runaway tool-call loop must not surface as an opaque
                # system error: stop cleanly, keep the evidence already
                # gathered and synthesize what can be delivered.
                recursion_stopped = True
                emit_event(
                    {
                        "type": "degraded",
                        "payload": {
                            "stage": "agent_loop",
                            "reason": "单轮工具调用步数已达上限，已停止继续检索。",
                        },
                    }
                )
            else:
                from src.core.error_friendly import friendly_error_message

                emit_event(
                    {
                        "type": "degraded",
                        "payload": {
                            "stage": "agent_loop",
                            "reason": friendly_error_message(exc),
                        },
                    }
                )
                raise

        # Recover the summarization state from the assembled graph state.
        # The raw message log is retained by deepagents; `_summarization_event`
        # tells us what the *next* model call actually sees.
        final_values = read_agent_values(
            agent, config, has_checkpointer=checkpointer is not None) or {}

        # finish() only returns the final text for the record; it is not re-yielded.
        rt.finalize_evidence()
        answer_text = _normalize_citation_labels(splitter.finish())
        partial_delivery = False
        if recursion_stopped or progress_stopped or time_stopped or cap_stopped:
            reason = (
                "recursion_limit" if recursion_stopped
                else "tool_call_limit" if cap_stopped
                else "time_limit" if time_stopped
                else "no_progress"
            )
            budget_state.update(
                {
                    "status": "exhausted" if recursion_stopped else "partial",
                    "exhausted": recursion_stopped,
                    "stopped_reason": reason,
                    "tool_calls": len(rt.diagnostics),
                    "evidence_count": len(rt.evidence),
                }
            )
            emit_event({"type": "budget_exhausted" if recursion_stopped else "budget", "payload": dict(budget_state)})
            synthesized: str | None = None
            if PARTIAL_SYNTHESIS_ENABLED:
                synthesized = _synthesize_partial_answer(
                    model=model,
                    query=query,
                    evidence=rt.evidence,
                    partial_answer=answer_text,
                    emit_event=emit_event,
                    budget_state=budget_state,
                )
            if synthesized:
                answer_text = synthesized
                partial_delivery = True
                budget_state["partial_answer"] = True
                budget_state["synthesis"] = "model"
                emit_event(
                    {
                        "type": "partial_result",
                        "payload": {
                            "reason": reason,
                            "synthesized": True,
                            "evidence_count": len(rt.evidence),
                            "answer_chars": len(synthesized),
                        },
                    }
                )
                yield synthesized
            else:
                # Fail-soft path when the model cannot synthesize (or
                # synthesis is disabled): keep the already-registered evidence
                # visible as a bounded digest, then append the actionable
                # budget notice.
                digest = _evidence_digest(rt.evidence)
                notice = _agent_step_budget_answer(rt, reason)
                combined = "\n\n".join(
                    part for part in (answer_text.strip(), digest, notice) if part
                )
                answer_text = combined
                partial_delivery = True
                budget_state["partial_answer"] = bool(combined)
                budget_state["synthesis"] = "digest" if digest else "notice"
                emit_event(
                    {
                        "type": "partial_result",
                        "payload": {
                            "reason": reason,
                            "synthesized": False,
                            "fallback": "evidence_digest" if digest else "notice",
                            "evidence_count": len(rt.evidence),
                            "answer_chars": len(combined),
                        },
                    }
                )
                yield combined
            record.answer = answer_text
            _current_run().answer = answer_text
        elif answer_text:
            record.answer = answer_text
            _current_run().answer = answer_text

        # Translate live diagnostics into the summary/footer projections.
        for diag in rt.diagnostics:
            message = f"{diag.get('hit_count')} hits"
            if int(diag.get("candidate_count") or 0) > 0:
                message += f" · {diag['candidate_count']} candidates"
            timeline.append(
                {
                    "node": diag.get("tool_name"),
                    "message": message,
                    "metadata": diag,
                }
            )
            record_agent_stage(
                stage=str(diag.get("tool_name") or "tool"),
                duration_s=max(0.0, float(diag.get("latency_ms") or 0) / 1000.0),
                status="success" if diag.get("status") != "failed" else "failed",
            )

        record.token_usage_summary = usage
        rt.finalize_evidence()
        answer_text = _current_run().answer
        if not partial_delivery and _is_incomplete_answer(
            answer_text, show_turn_meta=show_turn_meta, has_plan=False
        ):
            answer_text = ""
        if not answer_text and rt.evidence and PARTIAL_SYNTHESIS_ENABLED:
            answer_text, partial_delivery = _empty_answer_delivery(
                model=model,
                query=query,
                evidence=rt.evidence,
                emit_event=emit_event,
                budget_state=budget_state,
            )
            if answer_text:
                yield answer_text
        if not answer_text:
            # No answer text streamed and synthesis was unavailable/disabled.
            if rt.evidence:
                source_names = [name for name in dict.fromkeys(str(item.source_name or "") for item in rt.evidence) if name]
                listed = ", ".join(source_names[:8]) + (" 等" if len(source_names) > 8 else "")
                fallback = (
                    f"本轮未能生成带引用标注的回答，仅检索到候选证据来源：{listed}。"
                    "建议换个问法或补充更具体的关键词后重试。"
                )
            else:
                fallback = settings.NO_CONTEXT_PROMPT or "未生成回答。"
            answer_text = str(fallback)
            record.answer = answer_text
            _current_run().answer = answer_text
            yield answer_text
        answer_text, removed_citations = _scrub_invalid_citations(answer_text, rt.evidence)
        record.answer = _current_run().answer = answer_text
        if task is not None and task_store is not None:
            _finalize_task_attempt(
                store=task_store,
                task=task,
                partial=partial_delivery,
                answer_text=answer_text,
                evidence_count=len(rt.evidence),
            )
        if (
            answer_text
            and (partial_delivery or recursion_stopped)
            and checkpointer is not None
            and hasattr(agent, "update_state")
        ):
            # Otherwise the next turn loses the authoritative synthesized answer.
            try:
                agent.update_state(
                    config,
                    {"messages": [AIMessage(content=answer_text)]},
                )
            except Exception:
                pass
        context_manifest = publish_turn_context(
            model=model,
            system_prompt=plan.system_prompt,
            fragments=plan.context_fragments,
            evidence=rt.evidence,
            max_prompt_tokens=usage.max_prompt_tokens,
            history_turns=len(history),
            initial_summarization=initial_summarization,
            final_values=final_values,
            turn_id=str(thread_id or ""),
            event_callback=emit_event,
            budget_state=budget_state,
            fallback_window=int(getattr(settings, "AGENT_MODEL_MAX_INPUT_TOKENS", 0) or 0),
        )

        evidence_gaps: list[str] = []
        turn_evidence_plan = getattr(rt, "turn_evidence_plan", None)
        if turn_evidence_plan is not None:
            try:
                coverage = turn_evidence_plan.coverage_snapshot()
                evidence_gaps = [
                    f"{item.get('source_type') or '目标'} {item.get('target_id') or ''}："
                    f"{item.get('state')} {item.get('reason') or ''}".strip()
                    for item in coverage.get("requirements", [])
                    if item.get("state") not in {"admitted"}
                ]
            except Exception:
                evidence_gaps = []
        if show_turn_meta or recursion_stopped or partial_delivery:
            verification = _build_turn_verification(
                answer_text=answer_text,
                evidence=rt.evidence,
                query=query,
                # 部分交付跳过独立模型校验：此时收尾时间预算最紧，
                # `_semantic_verification` 是循环后唯一可能还在跑的大 LLM 调用，
                # 留着它撞看门狗就把已流出的部分答案一起拖死（B1 案例）。
                model=None if partial_delivery else model,
                complexity=budget_state.get("complexity") or {},
                budget_exhausted=recursion_stopped,
                partial_delivery=partial_delivery,
                require_evidence=not is_general,
                evidence_gaps=evidence_gaps,
            )
        else:
            # Simple turns keep only the historical lexical diagnostics.
            verification = _build_citation_verification(answer_text, rt.evidence)
        verification["removed_citations"] = removed_citations
        verification["answer_gaps"] = answer_gap_lines(answer_text)
        budget_state.update(
            {
                "status": (
                    "exhausted"
                    if recursion_stopped
                    else "partial"
                    if partial_delivery
                    else "ok"
                ),
                "tool_calls": len(rt.diagnostics),
                "evidence_count": len(rt.evidence),
                "partial_answer": partial_delivery,
            }
        )
        record.retrieval_summary = self._build_retrieval_summary(rt, timeline, verification)
        record.retrieval_summary["context_manifest"] = context_manifest
        meta_visible = (
            show_turn_meta
            or recursion_stopped
            or partial_delivery
            or bool(budget_state.get("context_window"))
            or bool(budget_state.get("context_compaction"))
        )
        if meta_visible:
            record.retrieval_summary["budget"] = dict(budget_state)
        if subagents:
            persisted_subagents = []
            for subagent in subagents:
                row = {
                    key: value for key, value in subagent.items() if not key.startswith("_")
                }
                if row.get("status") == "running":
                    row["status"] = "abandoned"
                persisted_subagents.append(row)
            record.retrieval_summary["subagents"] = persisted_subagents
        if meta_visible:
            emit_event({"type": "budget", "payload": dict(budget_state)})
        if show_turn_meta or recursion_stopped or partial_delivery:
            emit_event({"type": "verification", "payload": verification})
        if getattr(rt, "turn_retrieval_targets", None) is not None:
            record.retrieval_summary["retrieval_targets"] = {
                "resolution": rt.turn_retrieval_targets.resolution,
                "target_fingerprint": rt.turn_retrieval_targets.target_fingerprint,
                "targeted_kb_ids": list(rt.turn_retrieval_targets.targeted_kb_ids),
                "targets": [
                    {
                        "target_id": target.target_id, "kb_id": target.kb_id,
                        "record_id": target.record_id, "document_name": target.document_name,
                        "processor_kind": target.processor_kind, "query_id": target.query_id,
                        "locators": [locator.__dict__ for locator in target.locators],
                    }
                    for target in rt.turn_retrieval_targets.targets
                ],
            }
        if getattr(rt, "turn_evidence_plan", None) is not None:
            record.retrieval_summary["turn_evidence_coverage"] = rt.turn_evidence_plan.coverage_snapshot()
        source_budget = rt.source_budget_snapshot()
        if source_budget:
            record.retrieval_summary["source_budget"] = source_budget
            record.retrieval_summary["turn_context_used"] = int(
                source_budget.get("global_used_bytes") or 0
            )
        if getattr(rt, "evidence_policy", None) is not None:
            record.retrieval_summary["evidence_policy"] = rt.evidence_policy.to_dict()
        record.retrieval_summary["invocations"] = list(rt.invocations)
        record.retrieval_summary["intent_plan"] = intent_plan.to_dict()
        record.retrieval_summary["conversation_plan"] = conversation_plan.to_dict()
        record.footer = "" if is_general else self._format_footer(rt, verification, timeline)

    # ------------------------------------------------------------------
    # Public observability contract (unchanged shape)

    def get_last_footer(self) -> str:
        record = _RUN_RECORD.get()
        return record.footer if record is not None else ""

    def get_last_agent_answer(self) -> str:
        record = _RUN_RECORD.get()
        return record.answer if record is not None else ""

    def get_last_retrieval_summary(self) -> dict:
        record = _RUN_RECORD.get()
        return record.retrieval_summary if record is not None else {}

    def get_last_token_usage_summary(self):
        record = _RUN_RECORD.get()
        return record.token_usage_summary if record is not None else None

    def clear_last_token_usage_summary(self) -> None:
        record = _RUN_RECORD.get()
        if record is not None:
            record.token_usage_summary = None

    # ------------------------------------------------------------------

    def _build_retrieval_summary(
        self,
        rt: ToolRuntime,
        timeline: list[dict[str, Any]],
        verification: dict[str, Any],
    ) -> dict:
        failed = next((item for item in rt.diagnostics if item.get("status") not in {"ok"}), None)
        scope_failed = next(
            (
                item for item in rt.multi_kb_source_outcomes
                if item.get("status") not in {"success_with_hits", "success_empty"}
            ),
            None,
        )
        answer_text = _current_run().answer
        if (failed is not None or scope_failed is not None) and rt.evidence:
            status, error_stage, error_message = (
                "partial_failure",
                "retrieval",
                str(
                    (failed or {}).get("error")
                    or (failed or {}).get("status")
                    or (scope_failed or {}).get("error_code")
                    or (scope_failed or {}).get("status")
                    or ""
                ),
            )
        elif (failed is not None or scope_failed is not None) and not rt.evidence:
            status, error_stage, error_message = "failed", "retrieval", str(
                (failed or {}).get("error")
                or (failed or {}).get("status")
                or (scope_failed or {}).get("error_code")
                or (scope_failed or {}).get("status")
                or "retrieval failed"
            )
        elif not rt.evidence:
            status, error_stage, error_message = "no_evidence", "retrieval", "no evidence"
        else:
            status, error_stage, error_message = "success", "", ""
        result = {
            "status": status,
            "error_stage": error_stage,
            "error_message": error_message,
            "rewritten_queries": [item["query"] for item in rt.queries],
            "retriever_type": "multi_source_agent",
            "final_top_k": len(rt.evidence),
            "evidence": evidence_rows(rt.evidence),
            "memory_context": list(rt.memory_context),
            "missing": [],
            "retrieval_rounds": len(rt.diagnostics),
            "sufficiency_status": "insufficient" if not rt.evidence else "not_evaluated",
            "trace": timeline,
            "tool_diagnostics": list(rt.diagnostics),
            "claim_coverage": _build_claim_coverage(answer_text, rt.evidence),
            "retrieval_ledger": list(rt.queries),
            "evidence_quality": _build_evidence_quality(rt.evidence),
            "verification": verification,
            "multi_kb_source_outcomes": list(rt.multi_kb_source_outcomes),
            "multi_kb_fusions": list(rt.multi_kb_fusions),
        }
        result.update(_scope_dependency_fields(rt.knowledge_scope))
        plan = getattr(rt, "turn_evidence_plan", None)
        if plan is not None:
            snapshot = plan.coverage_snapshot()
            result["missing"] = [
                item for item in snapshot.get("requirements", [])
                if item.get("state") in {"missing", "blocked", "pending", "candidate"}
            ]
            result["turn_evidence_coverage"] = snapshot
        field_plan = getattr(rt, "field_evidence_plan", None)
        if field_plan is not None:
            result["field_evidence_coverage"] = field_plan.coverage_snapshot()
        return result

    def _format_footer(
        self,
        rt: ToolRuntime,
        verification: dict[str, Any],
        timeline: list[dict[str, Any]],
    ) -> str:
        sections = ["**概览**"]
        sections.append(
            f"- 检索工具调用：{len(rt.diagnostics)} | 证据：{len(rt.evidence)} | 模式：{rt.query_mode}"
        )
        distinct_tools = len({str(d.get("tool_name") or "") for d in rt.diagnostics if d.get("tool_name")})
        distinct_queries = len(rt.queries)
        sections.append(
            f"- 多跳深度：{len(rt.diagnostics)} 次调用 · {distinct_queries} 个不同查询 · 涉及 {distinct_tools} 类工具"
        )
        sections.append("\n**执行时间线**")
        if timeline:
            for index, item in enumerate(timeline, start=1):
                diag = item.get("metadata") or {}
                sections.append(
                    f"{index}. `{item.get('node')}` {item.get('message')}"
                    f" | latency={diag.get('latency_ms', 0)}ms"
                )
        else:
            sections.append("- 无")
        sections.append("\n**检索诊断**")
        if rt.diagnostics:
            for diag in rt.diagnostics[-8:]:
                candidates = int(diag.get("candidate_count") or 0)
                candidate_text = (
                    f" candidates={candidates}" if candidates > 0 else ""
                )
                sections.append(
                    f"- {diag.get('tool_name')} hits={diag.get('hit_count')}"
                    f"{candidate_text} status={diag.get('status')}"
                    f" latency={diag.get('latency_ms')}ms"
                )
        else:
            sections.append("- 无")
        failed_sources = [
            item for item in rt.multi_kb_source_outcomes
            if item.get("status") not in {"success_with_hits", "success_empty"}
        ]
        if failed_sources:
            sections.append("\n**未完成来源**")
            for item in failed_sources[-8:]:
                sections.append(
                    f"- kb:{item.get('kb_id')} {item.get('status')} "
                    f"{item.get('error_code') or item.get('status')}"
                )
        plan = getattr(rt, "turn_evidence_plan", None)
        if plan is not None:
            coverage = plan.coverage_snapshot()
            sections.append("\n**目标证据覆盖**")
            sections.append(
                f"- 已准入 {coverage['states'].get('admitted', 0)} / 必需 {coverage['required']}；"
                f"候选 {coverage['states'].get('candidate', 0)}，缺失 {coverage['states'].get('missing', 0)}，"
                f"受限 {coverage['states'].get('blocked', 0)}"
            )
            incomplete = [
                item for item in coverage.get("requirements", [])
                if item.get("state") not in {"admitted"}
            ]
            for item in incomplete[:8]:
                sections.append(
                    f"- 目标 {item.get('target_id')} / 查询 {item.get('query_id')}："
                    f"{item.get('state')} {item.get('reason') or ''}".rstrip()
                )
        source_budget = rt.source_budget_snapshot()
        if source_budget:
            sections.append("\n**来源预算**")
            for source, slice_ in (source_budget.get("slices") or {}).items():
                sections.append(
                    f"- {source}: {slice_.get('used_bytes', 0)}/{slice_.get('hard_bytes', 0)} bytes"
                    f" · {slice_.get('used_items', 0)} 条 · {slice_.get('status')}"
                )
        field_plan = getattr(rt, "field_evidence_plan", None)
        if field_plan is not None:
            field_coverage = field_plan.coverage_snapshot()
            sections.append("\n**字段级验证**")
            sections.append(
                f"- 支持 {field_coverage['states'].get('supported', 0)} / 必需 {field_coverage['required']}；"
                f"矛盾 {field_coverage['states'].get('contradicted', 0)}，"
                f"未验证 {field_coverage['states'].get('unknown', 0)}"
            )
            for item in field_coverage.get("requirements", []):
                if item.get("status") == "supported":
                    continue
                sections.append(
                    f"- {item.get('field_type')} / 目标 {item.get('target_id')}："
                    f"{item.get('status')} {item.get('reason') or ''}".rstrip()
                )
        sections.append("\n**交付校验**")
        if verification.get("status"):
            sections.append(
                f"- 状态：{verification.get('status')}；检查项 {len(verification.get('checks') or [])}；"
                f"缺口 {len(verification.get('gaps') or [])}"
            )
            for check in (verification.get("checks") or [])[:8]:
                sections.append(
                    f"- {check.get('label') or check.get('key')}：{check.get('status')}"
                    + (f" · {check.get('detail')}" if check.get("detail") else "")
                )
        else:
            sections.append(
                "- 语义支持=未评估 · 证据引用覆盖率={cov:.0%} · 无效引用={invalid} · 校验方式=仅核对 [n] 编号（未做逐句归因或冲突校验）".format(
                    invalid=verification.get("invalid_citations", []),
                    cov=float(verification.get("citation_coverage") or 0.0),
                )
            )
        return "\n".join(sections)
