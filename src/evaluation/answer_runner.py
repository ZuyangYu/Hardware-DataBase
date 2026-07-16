from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from src.pipelines.document_rag.schemas import RequestContext

from .schemas import AnswerSnapshot, EvaluationSample


_SENSITIVE_KEYS = {"api_key", "password", "secret", "token", "authorization"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if str(key).casefold() in _SENSITIVE_KEYS else _sanitize(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item, depth + 1) for item in value]
    return value


def _request_context(sample: EvaluationSample) -> RequestContext:
    raw = sample.request_context
    department_id = raw.get("department_id")
    metadata = {"department_id": department_id} if department_id not in (None, "") else {}
    return RequestContext(
        user_id=str(raw.get("user_id") or "evaluation"),
        session_id=str(raw.get("session_id") or f"eval-{sample.id}"),
        roles=list(raw.get("roles") or []),
        allowed_kbs=[str(value) for value in raw.get("allowed_kbs") or []],
        kb_permissions={str(key): str(value) for key, value in (raw.get("kb_permissions") or {}).items()},
        metadata=metadata,
    )


class AnswerRunner:
    def __init__(self, pipeline_factory: Callable[[], Any]):
        self._pipeline_factory = pipeline_factory

    def collect(self, sample: EvaluationSample) -> AnswerSnapshot:
        started_at = _utc_now()
        started = perf_counter()
        try:
            pipeline = self._pipeline_factory()
        except Exception:
            return self._failed(sample, started_at, started, "pipeline_initialization")

        try:
            parts = list(
                pipeline.query(
                    sample.question,
                    sample.kb_name,
                    [],
                    ctx=_request_context(sample),
                    agent_thread_id=f"eval-{sample.id}",
                )
            )
            summary = pipeline.get_last_retrieval_summary() or {}
            safe_summary = _sanitize(summary)
            evidence = list(safe_summary.get("evidence") or [])
            contexts = [str(item.get("content") or "") for item in evidence if item.get("content")]
            return AnswerSnapshot(
                sample_id=sample.id,
                question=sample.question,
                kb_name=sample.kb_name,
                response="".join(str(part) for part in parts),
                retrieved_contexts=contexts,
                evidence=evidence,
                retrieval_summary=safe_summary,
                started_at=started_at,
                finished_at=_utc_now(),
                duration_seconds=max(0.0, perf_counter() - started),
            )
        except Exception:
            return self._failed(sample, started_at, started, "answer_collection")

    @staticmethod
    def _failed(sample: EvaluationSample, started_at: str, started: float, stage: str) -> AnswerSnapshot:
        return AnswerSnapshot(
            sample_id=sample.id,
            question=sample.question,
            kb_name=sample.kb_name,
            status="failed",
            error_stage=stage,
            error_message="evaluation collection failed; see application logs",
            started_at=started_at,
            finished_at=_utc_now(),
            duration_seconds=max(0.0, perf_counter() - started),
        )
