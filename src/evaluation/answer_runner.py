from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import inspect
import re
import threading
from time import perf_counter
from typing import Any

from src.agents.runner import strip_narration_segments
from src.pipelines.document_rag.schemas import RequestContext

from .access import assess_access, build_evaluation_context
from .schemas import AnswerSnapshot, EvaluationSample


_SENSITIVE_KEYS = {"api_key", "password", "secret", "token", "authorization"}
_ADMINISTRATIVE_SECTION_TITLES = {
    "来源说明",
    "证据来源",
    "检索账本",
    "证据覆盖度",
    "检索诊断",
    "证据质量",
}
_SUBQUESTION_STATUS = re.compile(
    r"^(?:[-*]\s*)?子问题\s*(?:sq[_-]?\d+|\d+)\s*(?:已完全覆盖|已覆盖).*$",
    re.IGNORECASE,
)


def _heading_title(line: str) -> str:
    title = line.strip()
    title = re.sub(r"^#{1,6}\s*", "", title)
    title = re.sub(r"^\*{1,2}\s*", "", title)
    title = re.sub(r"\s*\*{1,2}$", "", title)
    return title.rstrip("：:").strip()


def _is_section_heading(line: str) -> bool:
    stripped = line.strip()
    return bool(
        re.match(r"^#{1,6}\s+", stripped)
        or re.match(r"^\*{1,2}.+?\*{1,2}[:：]?$", stripped)
    )


def extract_scored_response(response: str) -> tuple[str, dict[str, object]]:
    """Remove known presentation-only sections without discarding answer claims."""

    original = response.strip()
    kept: list[str] = []
    removed_sections: list[str] = []
    skipping_administrative_section = False

    for line in original.splitlines():
        title = _heading_title(line)
        if title in _ADMINISTRATIVE_SECTION_TITLES:
            skipping_administrative_section = True
            if title not in removed_sections:
                removed_sections.append(title)
            continue
        if _SUBQUESTION_STATUS.fullmatch(line.strip()):
            if "子问题覆盖状态" not in removed_sections:
                removed_sections.append("子问题覆盖状态")
            continue
        if skipping_administrative_section:
            if _is_section_heading(line):
                skipping_administrative_section = False
            else:
                continue
        kept.append(line)

    scored = "\n".join(kept).strip() or original
    diagnostic = {
        "filtered": scored != original,
        "removed_sections": removed_sections,
        "original_characters": len(original),
        "scored_characters": len(scored),
    }
    return scored, diagnostic


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): "[redacted]"
            if str(key).casefold() in _SENSITIVE_KEYS
            else _sanitize(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item, depth + 1) for item in value]
    return value


def _safe_error_message(exc: Exception | None) -> str:
    if exc is None:
        return "evaluation collection failed; see application logs"
    detail = re.sub(
        r"(?i)\b(api[_-]?key|password|secret|token|authorization)\b(?:\s*[=:]\s*|\s*[-_]\s*)?\S*",
        r"\1=[redacted]",
        str(exc),
    )
    return f"evaluation collection failed ({type(exc).__name__}: {detail[:300]})"


def _request_context(sample: EvaluationSample) -> RequestContext:
    """Build a bounded evaluation context from a normalized sample."""

    return build_evaluation_context(sample)


class AnswerRunner:
    def __init__(self, pipeline_factory: Callable[[], Any]):
        self._pipeline_factory = pipeline_factory
        # AppPipeline initialization opens the document/circuit indexes and
        # creates the Agent runtime. Collection already uses a thread pool, so
        # keep one pipeline per worker thread instead of rebuilding that stack
        # for every sample. ContextVar-backed agent state keeps concurrent
        # queries isolated while the thread-local prevents cross-thread reuse.
        self._thread_local = threading.local()

    def _pipeline_for_current_thread(self) -> Any:
        pipeline = getattr(self._thread_local, "pipeline", None)
        if pipeline is None:
            pipeline = self._pipeline_factory()
            self._thread_local.pipeline = pipeline
        return pipeline

    def collect(self, sample: EvaluationSample) -> AnswerSnapshot:
        started_at = _utc_now()
        started = perf_counter()
        context = _request_context(sample)
        if sample.expected_access == "denied":
            return self._access_denied(sample, started_at, started, context)
        try:
            pipeline = self._pipeline_for_current_thread()
        except Exception as exc:
            return self._failed(
                sample,
                started_at,
                started,
                "pipeline_initialization",
                exc,
                context=context,
            )

        try:
            # The agent streams provisional answer deltas live; narration
            # segments (model messages that carry tool calls) are announced
            # via events and must be stripped from the joined response so the
            # scored text is the authoritative answer only.
            narrated: list[str] = []

            def _on_event(evt: dict) -> None:
                if evt.get("type") == "narration":
                    narrated.append(str((evt.get("payload") or {}).get("text") or ""))

            kwargs: dict[str, Any] = {}
            if "event_callback" in inspect.signature(pipeline.query).parameters:
                kwargs["event_callback"] = _on_event
            parts = list(
                pipeline.query(
                    sample.question,
                    sample.kb_name,
                    [],
                    ctx=context,
                    agent_thread_id=f"eval-{sample.id}",
                    **kwargs,
                )
            )
            summary = pipeline.get_last_retrieval_summary() or {}
            safe_summary = _sanitize(summary)
            evidence = list(safe_summary.get("evidence") or [])
            contexts = [
                str(item.get("content") or "")
                for item in evidence
                if item.get("content")
            ]
            response = strip_narration_segments(
                "".join(str(part) for part in parts), narrated
            )
            if safe_summary.get("status") == "failed":
                error_message = str(safe_summary.get("error_message") or "").strip()
                return self._failed(
                    sample,
                    started_at,
                    started,
                    str(safe_summary.get("error_stage") or "answer_collection"),
                    RuntimeError(error_message) if error_message else None,
                    evidence=evidence,
                    retrieved_contexts=contexts,
                    retrieval_summary=safe_summary,
                    context=context,
                )
            if response.lstrip().startswith("系统错误:"):
                return self._failed(
                    sample,
                    started_at,
                    started,
                    "answer_collection",
                    context=context,
                    retrieval_summary=safe_summary,
                    evidence=evidence,
                    retrieved_contexts=contexts,
                )
            scored_response, filter_diagnostic = extract_scored_response(response)
            access_check = assess_access(
                sample,
                context,
                safe_summary,
                response=response,
                evidence=evidence,
            )
            return AnswerSnapshot(
                sample_id=sample.id,
                question=sample.question,
                kb_name=sample.kb_name,
                response=response,
                scored_response=scored_response,
                retrieved_contexts=contexts,
                evidence=evidence,
                retrieval_summary=safe_summary,
                started_at=started_at,
                finished_at=_utc_now(),
                duration_seconds=max(0.0, perf_counter() - started),
                metadata={"scored_response_filter": filter_diagnostic},
                access_check=access_check,
            )
        except Exception as exc:
            return self._failed(
                sample,
                started_at,
                started,
                "answer_collection",
                exc,
                context=context,
            )

    @staticmethod
    def _access_denied(
        sample: EvaluationSample,
        started_at: str,
        started: float,
        context: RequestContext,
    ) -> AnswerSnapshot:
        """Record an expected authorization denial without running retrieval.

        Denial samples are negative authorization controls, not answer-quality
        prompts.  Keeping them out of the agent pipeline also prevents tools
        that use a different local index from accidentally returning content
        before the primary RAG backend can reject the request.
        """
        retrieval_summary = {
            "status": "permission_denied",
            "error_stage": "authorization",
            "error_message": "expected_access=denied; normal retrieval was skipped",
            "access_decision": "denied",
            "evidence": [],
        }
        response = "权限拒绝：该评估样本不具备所选知识库的读取权限。"
        access_check = assess_access(
            sample,
            context,
            retrieval_summary,
            response=response,
            evidence=[],
        )
        return AnswerSnapshot(
            sample_id=sample.id,
            question=sample.question,
            kb_name=sample.kb_name,
            response=response,
            scored_response=response,
            retrieved_contexts=[],
            evidence=[],
            retrieval_summary=retrieval_summary,
            started_at=started_at,
            finished_at=_utc_now(),
            duration_seconds=max(0.0, perf_counter() - started),
            metadata={"collection_mode": "access_check_only"},
            access_check=access_check,
        )

    @staticmethod
    def _failed(
        sample: EvaluationSample,
        started_at: str,
        started: float,
        stage: str,
        exc: Exception | None = None,
        *,
        evidence: list[dict[str, Any]] | None = None,
        retrieved_contexts: list[str] | None = None,
        retrieval_summary: dict[str, Any] | None = None,
        context: RequestContext | None = None,
    ) -> AnswerSnapshot:
        access_check = (
            assess_access(
                sample,
                context,
                retrieval_summary,
                evidence=evidence,
            )
            if context is not None
            else {
                "expected": sample.expected_access,
                "observed": "unknown",
                "reason": "access could not be evaluated because collection failed",
            }
        )
        return AnswerSnapshot(
            sample_id=sample.id,
            question=sample.question,
            kb_name=sample.kb_name,
            status="failed",
            error_stage=stage,
            error_message=_safe_error_message(exc),
            evidence=evidence or [],
            retrieved_contexts=retrieved_contexts or [],
            retrieval_summary=retrieval_summary or {},
            started_at=started_at,
            finished_at=_utc_now(),
            duration_seconds=max(0.0, perf_counter() - started),
            access_check=access_check,
        )
