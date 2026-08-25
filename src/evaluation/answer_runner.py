from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import re
from time import perf_counter
from typing import Any

from src.pipelines.document_rag.schemas import RequestContext

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
            str(key): "[redacted]" if str(key).casefold() in _SENSITIVE_KEYS else _sanitize(item, depth + 1)
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
    """Build a bounded evaluation context from the sample.

    Security: the dataset JSONL is untrusted input. Roles are pinned to plain
    ``user`` (a dataset claiming ``dept_admin``/``system_admin`` is ignored)
    and ``user_id`` is fixed to ``evaluation`` so audit attribution cannot be
    spoofed; the dataset's declared user_id is kept in metadata only.
    KB scoping fields (department_id / allowed_kbs / kb_permissions) are still
    honored — they are required to reach dept-scoped structured indexes — but
    the run creator must authorize every referenced KB at run-creation time
    (see routes/evaluation.py::create_run), which also writes an audit record.
    """
    raw = sample.request_context
    department_id = raw.get("department_id")
    metadata = {"department_id": department_id} if department_id not in (None, "") else {}
    declared_user = str(raw.get("user_id") or "").strip()
    if declared_user:
        metadata["declared_user"] = declared_user
    return RequestContext(
        user_id="evaluation",
        session_id=str(raw.get("session_id") or f"eval-{sample.id}"),
        roles=["user"],
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
        except Exception as exc:
            return self._failed(sample, started_at, started, "pipeline_initialization", exc)

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
            response = "".join(str(part) for part in parts)
            if safe_summary.get("status") == "failed" or response.lstrip().startswith("系统错误:"):
                return self._failed(sample, started_at, started, "answer_collection")
            scored_response, filter_diagnostic = extract_scored_response(response)
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
            )
        except Exception as exc:
            return self._failed(sample, started_at, started, "answer_collection", exc)

    @staticmethod
    def _failed(
        sample: EvaluationSample,
        started_at: str,
        started: float,
        stage: str,
        exc: Exception | None = None,
    ) -> AnswerSnapshot:
        return AnswerSnapshot(
            sample_id=sample.id,
            question=sample.question,
            kb_name=sample.kb_name,
            status="failed",
            error_stage=stage,
            error_message=_safe_error_message(exc),
            started_at=started_at,
            finished_at=_utc_now(),
            duration_seconds=max(0.0, perf_counter() - started),
        )
