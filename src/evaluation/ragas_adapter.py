from __future__ import annotations

import asyncio
import json
import math
import random
import re
import threading
import time
from collections import defaultdict
from typing import Any, Callable, Protocol

from .config import EvaluationConfig
from .schemas import AnswerSnapshot, EvaluationSample, MetricResult
from src.observability import observe


STANDARD_METRICS = {
    "answer_correctness",
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
}
RAGAS_RESULT_KEYS = {
    "answer_correctness": "answer_correctness",
    "faithfulness": "faithfulness",
    "answer_relevancy": "answer_relevancy",
    "context_precision": "llm_context_precision_with_reference",
    "context_recall": "context_recall",
}
CONTEXT_METRICS = {"faithfulness", "context_precision", "context_recall"}
RAW_CONTEXT_METRICS = {"context_precision", "context_recall"}
_RETRYABLE_EXCEPTION_NAMES = {
    "APITimeoutError",
    "APIConnectionError",
    "ConnectTimeout",
    "NetworkError",
    "ReadTimeout",
    "RemoteProtocolError",
    "TimeoutException",
}


def _scoring_query_tokens(text: str) -> set[str]:
    """Return small, deterministic tokens for scoring-context reranking."""

    value = str(text or "").casefold()
    tokens = set(re.findall(r"[a-z][a-z0-9_.+-]{1,}|[0-9][a-z0-9_.+-]{1,}", value))
    for block in re.findall(r"[\u4e00-\u9fff]{2,}", value):
        tokens.add(block)
        for size in (2, 3):
            tokens.update(
                block[index : index + size] for index in range(len(block) - size + 1)
            )
    return {token for token in tokens if len(token) >= 2}


def _scoring_context_relevance(question: str, content: str) -> tuple[int, bool]:
    tokens = _scoring_query_tokens(question)
    searchable = str(content or "").casefold()
    overlap = sum(token in searchable for token in tokens)
    boilerplate = any(
        marker in searchable
        for marker in (
            "填写说明",
            "template instructions",
            "封面",
            "cover",
            "模板变更历史",
            "资料源目录",
            "模板说明",
        )
    )
    return overlap, boilerplate


_MARKUP_TAG_RE = re.compile(r"<[^>]+>")
_MARKUP_SPACE_RE = re.compile(r"[ \t\u3000]{2,}")
_MARKUP_HINT_RE = re.compile(
    r"<(table|tr|td|th|br|p|div|span|ul|ol|li)[\s>/]", re.IGNORECASE
)


def strip_markup(text: str) -> str:
    """Convert HTML-ish chunks (spreadsheet tables) into plain text for scoring.

    Tag characters would otherwise consume the bounded scoring-character
    budget without contributing any judge-readable content. Only inputs that
    actually look like HTML are rewritten; comparisons such as ``a < b > c``
    in plain text are left untouched.
    """

    value = str(text or "")
    if "<" not in value or not _MARKUP_HINT_RE.search(value):
        return value
    return _MARKUP_SPACE_RE.sub(" ", _MARKUP_TAG_RE.sub(" ", value)).strip()


class RagasBackend(Protocol):
    def score(
        self, records: list[dict[str, Any]], metric_names: list[str]
    ) -> list[dict[str, Any]]: ...


def _metric_is_applicable(
    metric_name: str, sample: EvaluationSample, snapshot: AnswerSnapshot
) -> bool:
    if metric_name == "context_recall":
        # LLM Context Recall derives claims from the reference answer;
        # reference_contexts are only required by ID/non-LLM recall metrics.
        return bool(sample.reference_answer.strip() and snapshot.retrieved_contexts)
    if metric_name in {"faithfulness", "context_precision"}:
        return bool(snapshot.retrieved_contexts)
    return True


def _error_status_code(exc: Exception) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    return status_code if isinstance(status_code, int) else None


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    raw = headers.get("Retry-After") if headers else None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _is_rate_or_server_error(exc: Exception) -> bool:
    status = _error_status_code(exc)
    return status is not None and (status in {408, 429} or status >= 500)


def _backoff_sleep(attempt: int, exc: Exception | None = None) -> None:
    """Exponential backoff before a scoring retry; honors Retry-After."""

    delay = _retry_after_seconds(exc) if exc is not None else None
    if delay is None:
        base = min(30.0, 2.0 ** max(1, attempt))
        delay = base * (0.5 + random.random())
    time.sleep(min(delay, 60.0))


def _is_retryable_context_error(exc: Exception) -> bool:
    return (
        isinstance(exc, (TimeoutError, ConnectionError))
        or _error_status_code(exc) == 400
    )


def _is_retryable_evaluator_error(exc: Exception) -> bool:
    """Identify transient judge failures without importing optional clients."""

    if isinstance(exc, (TimeoutError, ConnectionError)) or _is_rate_or_server_error(
        exc
    ):
        return True
    return any(
        name in _RETRYABLE_EXCEPTION_NAMES
        for name in (base.__name__ for base in type(exc).__mro__)
    )


class RagasAdapter:
    def __init__(self, config: EvaluationConfig, backend: RagasBackend | None = None):
        self.config = config
        self._backend = backend

    def score(
        self,
        samples: list[EvaluationSample],
        snapshots: list[AnswerSnapshot],
        metric_names: list[str],
        *,
        snapshots_prepared: bool = False,
        on_result: Callable[[MetricResult], None] | None = None,
    ) -> list[MetricResult]:
        unknown = sorted(set(metric_names) - STANDARD_METRICS)
        if unknown:
            raise ValueError(f"unknown RAGAS metrics: {', '.join(unknown)}")
        if not snapshots_prepared:
            snapshots, _ = self.prepare_snapshots_for_scoring(snapshots)
        snapshot_by_id = {snapshot.sample_id: snapshot for snapshot in snapshots}
        results: list[MetricResult] = []

        def emit(result: MetricResult) -> None:
            results.append(result)
            if on_result is not None:
                on_result(result)

        grouped: dict[
            tuple[str, ...], list[tuple[EvaluationSample, AnswerSnapshot]]
        ] = defaultdict(list)

        for sample in samples:
            snapshot = snapshot_by_id.get(sample.id)
            if snapshot is None or snapshot.status != "success":
                for metric_name in metric_names:
                    emit(
                        MetricResult(
                            sample_id=sample.id,
                            metric_name=metric_name,
                            status="failed",
                            reason="answer snapshot is missing or failed",
                        )
                    )
                continue
            applicable = tuple(
                metric_name
                for metric_name in metric_names
                if _metric_is_applicable(metric_name, sample, snapshot)
            )
            for metric_name in metric_names:
                if metric_name not in applicable:
                    emit(
                        MetricResult(
                            sample_id=sample.id,
                            metric_name=metric_name,
                            status="not_applicable",
                            reason="required contexts are unavailable",
                        )
                    )
            if applicable:
                grouped[applicable].append((sample, snapshot))

        backend = self._backend
        if grouped and backend is None:
            backend = _NativeRagasBackend(self.config)

        # A native RAGAS batch does not yield rows until every input finishes.
        # For a live progress view, evaluate one sample at a time so callers
        # receive and can persist each metric result immediately.
        if on_result is not None:
            for applicable, pairs in grouped.items():
                for sample, snapshot in pairs:
                    for result in self.score(
                        [sample],
                        [snapshot],
                        list(applicable),
                        snapshots_prepared=True,
                    ):
                        emit(result)
            order = {
                (sample.id, metric_name): index
                for index, (sample, metric_name) in enumerate(
                    (sample, metric_name)
                    for sample in samples
                    for metric_name in metric_names
                )
            }
            return sorted(
                results, key=lambda item: order[(item.sample_id, item.metric_name)]
            )

        for applicable, pairs in grouped.items():
            for metric_name in applicable:
                records = [
                    self._record(sample, snapshot, metric_name)
                    for sample, snapshot in pairs
                ]
                with observe.evaluator(
                    "hdb.evaluation.metric",
                    metric=metric_name,
                    stage="score",
                    sample_count=len(records),
                ) as observation:
                    try:
                        scored_rows = backend.score(records, [metric_name])  # type: ignore[union-attr]
                    except Exception as exc:
                        observation.error(exc)
                        scored_rows = [{metric_name: exc} for _ in records]
                for index, ((sample, _), row) in enumerate(
                    zip(pairs, scored_rows, strict=True)
                ):
                    value = row.get(metric_name)
                    attempts = 1
                    retry_diagnostic: dict[str, int | str] | None = None
                    if (
                        isinstance(value, Exception)
                        and metric_name in CONTEXT_METRICS
                        and _is_retryable_context_error(value)
                        and not snapshot.metadata.get("_full_contexts_for_scoring")
                    ):
                        value, attempts, retry_diagnostic = (
                            self._retry_with_smaller_context(
                                backend,  # type: ignore[arg-type]
                                sample,
                                pairs[index][1],
                                metric_name,
                                initial_error=value,
                                initial_attempts=attempts,
                            )
                        )
                    while (
                        isinstance(value, float)
                        and math.isnan(value)
                        and attempts <= self.config.max_retries
                    ):
                        attempts += 1
                        _backoff_sleep(attempts)
                        try:
                            retry_rows = backend.score([records[index]], [metric_name])
                            value = retry_rows[0].get(metric_name)
                        except Exception as exc:
                            value = exc
                    if (
                        isinstance(value, Exception)
                        and metric_name in CONTEXT_METRICS
                        and _is_retryable_context_error(value)
                        and retry_diagnostic is None
                        and not snapshot.metadata.get("_full_contexts_for_scoring")
                    ):
                        value, attempts, retry_diagnostic = (
                            self._retry_with_smaller_context(
                                backend,  # type: ignore[arg-type]
                                sample,
                                pairs[index][1],
                                metric_name,
                                initial_error=value,
                                initial_attempts=attempts,
                            )
                        )
                    if (
                        isinstance(value, Exception)
                        and metric_name not in CONTEXT_METRICS
                        and _is_retryable_evaluator_error(value)
                    ):
                        value, attempts, transient_diagnostic = (
                            self._retry_transient_metric(
                                backend,  # type: ignore[arg-type]
                                records[index],
                                metric_name,
                                initial_error=value,
                                initial_attempts=attempts,
                            )
                        )
                        if transient_diagnostic is not None:
                            if retry_diagnostic is not None:
                                transient_diagnostic = {
                                    **retry_diagnostic,
                                    **transient_diagnostic,
                                }
                            retry_diagnostic = transient_diagnostic
                    if (
                        isinstance(value, Exception)
                        or value is None
                        or (isinstance(value, float) and math.isnan(value))
                    ):
                        diagnostic_kind = (
                            "exception"
                            if isinstance(value, Exception)
                            else "missing"
                            if value is None
                            else "nan"
                        )
                        diagnostic = {
                            "kind": diagnostic_kind,
                            "value_type": type(value).__name__,
                            "attempts": attempts,
                            "sample_id": sample.id,
                            "metric_name": metric_name,
                            "context_count": len(pairs[index][1].retrieved_contexts),
                            "context_characters": sum(
                                len(context)
                                for context in pairs[index][1].retrieved_contexts
                            ),
                        }
                        if retry_diagnostic:
                            for source_key, diagnostic_key in (
                                ("final_context_count", "context_count"),
                                ("final_context_characters", "context_characters"),
                                ("context_budget_attempts", "context_budget_attempts"),
                            ):
                                if source_key in retry_diagnostic:
                                    diagnostic[diagnostic_key] = retry_diagnostic[
                                        source_key
                                    ]
                        if isinstance(value, Exception):
                            diagnostic["error_type"] = type(value).__name__
                            diagnostic["error_message"] = (
                                f"upstream evaluator request failed ({type(value).__name__})"
                            )[:200]
                            status_code = _error_status_code(value)
                            if status_code is not None:
                                diagnostic["status_code"] = status_code
                        emit(
                            MetricResult(
                                sample_id=sample.id,
                                metric_name=metric_name,
                                status="failed",
                                reason=f"metric evaluation failed: {type(value).__name__}",
                                details={"evaluator_diagnostic": diagnostic},
                            )
                        )
                    else:
                        details = (
                            {"evaluator_diagnostic": retry_diagnostic}
                            if retry_diagnostic
                            else {}
                        )
                        emit(
                            MetricResult(
                                sample_id=sample.id,
                                metric_name=metric_name,
                                score=float(value),
                                details=details,
                            )
                        )
        order = {
            (sample.id, metric_name): index
            for index, (sample, metric_name) in enumerate(
                (sample, metric_name)
                for sample in samples
                for metric_name in metric_names
            )
        }
        return sorted(
            results, key=lambda item: order[(item.sample_id, item.metric_name)]
        )

    def score_batched(
        self,
        samples: list[EvaluationSample],
        snapshots: list[AnswerSnapshot],
        metric_names: list[str],
        *,
        snapshots_prepared: bool = False,
        on_result: Callable[[MetricResult], None] | None = None,
    ) -> list[MetricResult]:
        """Score a dataset in as few native RAGAS calls as possible.

        The original ``score`` method deliberately keeps its per-metric shape
        for compatibility with older callers and fine-grained retry tests. A
        live UI, however, must not turn one dataset into one ``evaluate`` call
        per sample. This path sends every applicable metric and all samples in
        a group to the backend together; retries are restricted to failed
        cells only.
        """

        unknown = sorted(set(metric_names) - STANDARD_METRICS)
        if unknown:
            raise ValueError(f"unknown RAGAS metrics: {', '.join(unknown)}")
        if not snapshots_prepared:
            snapshots, _ = self.prepare_snapshots_for_scoring(snapshots)
        snapshot_by_id = {snapshot.sample_id: snapshot for snapshot in snapshots}
        results: list[MetricResult] = []

        def emit(result: MetricResult) -> None:
            results.append(result)
            if on_result is not None and result.status != "not_applicable":
                on_result(result)

        grouped: dict[
            tuple[str, ...], list[tuple[EvaluationSample, AnswerSnapshot]]
        ] = defaultdict(list)
        for sample in samples:
            snapshot = snapshot_by_id.get(sample.id)
            if snapshot is None or snapshot.status != "success":
                for metric_name in metric_names:
                    emit(
                        MetricResult(
                            sample_id=sample.id,
                            metric_name=metric_name,
                            status="failed",
                            reason="answer snapshot is missing or failed",
                        )
                    )
                continue
            applicable = tuple(
                metric_name
                for metric_name in metric_names
                if _metric_is_applicable(metric_name, sample, snapshot)
            )
            for metric_name in metric_names:
                if metric_name not in applicable:
                    emit(
                        MetricResult(
                            sample_id=sample.id,
                            metric_name=metric_name,
                            status="not_applicable",
                            reason="required contexts are unavailable",
                        )
                    )
            if applicable:
                grouped[applicable].append((sample, snapshot))

        backend = self._backend
        if grouped and backend is None:
            backend = _NativeRagasBackend(self.config)

        execution_groups: dict[
            tuple[str, tuple[str, ...]], list[tuple[EvaluationSample, AnswerSnapshot]]
        ] = defaultdict(list)
        for applicable, pairs in grouped.items():
            raw_metrics = tuple(
                name for name in applicable if name in RAW_CONTEXT_METRICS
            )
            curated_metrics = tuple(
                name for name in applicable if name not in RAW_CONTEXT_METRICS
            )
            if raw_metrics:
                execution_groups[("raw", raw_metrics)].extend(pairs)
            if curated_metrics:
                execution_groups[("curated", curated_metrics)].extend(pairs)

        for (context_mode, applicable), pairs in execution_groups.items():
            metric_list = list(applicable)
            if context_mode == "raw":
                raw_pairs: list[tuple[EvaluationSample, AnswerSnapshot]] = []
                for sample, snapshot in pairs:
                    raw_contexts = snapshot.metadata.get("_raw_retrieved_contexts")
                    if isinstance(raw_contexts, list):
                        if not snapshot.metadata.get("_full_contexts_for_scoring"):
                            raw_contexts, _ = self._bounded_contexts(raw_contexts)
                        snapshot = snapshot.model_copy(
                            update={"retrieved_contexts": raw_contexts}
                        )
                    raw_pairs.append((sample, snapshot))
                pairs = raw_pairs
            records = [
                self._record_for_metrics(sample, snapshot, metric_list)
                for sample, snapshot in pairs
            ]
            unique_records: list[dict[str, Any]] = []
            unique_indices: list[int] = []
            record_to_unique: dict[str, int] = {}
            for record in records:
                key = json.dumps(
                    {k: v for k, v in record.items() if k != "sample_id"},
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                )
                unique_index = record_to_unique.get(key)
                if unique_index is None:
                    unique_index = len(unique_records)
                    record_to_unique[key] = unique_index
                    unique_records.append(record)
                unique_indices.append(unique_index)
            try:
                unique_rows = backend.score(unique_records, metric_list)  # type: ignore[union-attr]
                scored_rows = [
                    unique_rows[index] if index < len(unique_rows) else {}
                    for index in unique_indices
                ]
            except Exception as exc:
                scored_rows = [
                    {metric_name: exc for metric_name in metric_list} for _ in records
                ]
            if len(scored_rows) < len(records):
                scored_rows = list(scored_rows) + [
                    {} for _ in range(len(records) - len(scored_rows))
                ]

            for index, ((sample, snapshot), row) in enumerate(
                zip(pairs, scored_rows, strict=False)
            ):
                for metric_name in metric_list:
                    value = row.get(metric_name)
                    attempts = 1
                    retry_diagnostic: dict[str, int | str] | None = None
                    if (
                        isinstance(value, Exception)
                        and metric_name in CONTEXT_METRICS
                        and _is_retryable_context_error(value)
                    ):
                        value, attempts, retry_diagnostic = (
                            self._retry_with_smaller_context(
                                backend,
                                sample,
                                snapshot,
                                metric_name,
                                initial_error=value,
                                initial_attempts=attempts,
                            )
                        )
                    while (
                        isinstance(value, float)
                        and math.isnan(value)
                        and attempts <= self.config.max_retries
                    ):
                        attempts += 1
                        _backoff_sleep(attempts)
                        try:
                            retry_rows = backend.score(
                                [
                                    self._record_for_metrics(
                                        sample, snapshot, [metric_name]
                                    )
                                ],
                                [metric_name],
                            )
                            value = retry_rows[0].get(metric_name)
                        except Exception as exc:
                            value = exc
                    if (
                        isinstance(value, Exception)
                        and metric_name in CONTEXT_METRICS
                        and _is_retryable_context_error(value)
                        and retry_diagnostic is None
                    ):
                        value, attempts, retry_diagnostic = (
                            self._retry_with_smaller_context(
                                backend,
                                sample,
                                snapshot,
                                metric_name,
                                initial_error=value,
                                initial_attempts=attempts,
                            )
                        )
                    if (
                        isinstance(value, Exception)
                        and metric_name not in CONTEXT_METRICS
                        and _is_retryable_evaluator_error(value)
                    ):
                        value, attempts, transient_diagnostic = (
                            self._retry_transient_metric(
                                backend,
                                self._record_for_metrics(
                                    sample, snapshot, [metric_name]
                                ),
                                metric_name,
                                initial_error=value,
                                initial_attempts=attempts,
                            )
                        )
                        if transient_diagnostic is not None:
                            if retry_diagnostic is not None:
                                transient_diagnostic = {
                                    **retry_diagnostic,
                                    **transient_diagnostic,
                                }
                            retry_diagnostic = transient_diagnostic

                    if (
                        isinstance(value, Exception)
                        or value is None
                        or (isinstance(value, float) and math.isnan(value))
                    ):
                        diagnostic_kind = (
                            "exception"
                            if isinstance(value, Exception)
                            else "missing"
                            if value is None
                            else "nan"
                        )
                        diagnostic = {
                            "kind": diagnostic_kind,
                            "value_type": type(value).__name__,
                            "attempts": attempts,
                            "sample_id": sample.id,
                            "metric_name": metric_name,
                            "context_count": len(snapshot.retrieved_contexts),
                            "context_characters": sum(
                                len(context) for context in snapshot.retrieved_contexts
                            ),
                        }
                        if retry_diagnostic:
                            for source_key, diagnostic_key in (
                                ("final_context_count", "context_count"),
                                ("final_context_characters", "context_characters"),
                                ("context_budget_attempts", "context_budget_attempts"),
                            ):
                                if source_key in retry_diagnostic:
                                    diagnostic[diagnostic_key] = retry_diagnostic[
                                        source_key
                                    ]
                        if isinstance(value, Exception):
                            diagnostic["error_type"] = type(value).__name__
                            diagnostic["error_message"] = (
                                f"upstream evaluator request failed ({type(value).__name__})"
                            )[:200]
                            status_code = _error_status_code(value)
                            if status_code is not None:
                                diagnostic["status_code"] = status_code
                        emit(
                            MetricResult(
                                sample_id=sample.id,
                                metric_name=metric_name,
                                status="failed",
                                reason=f"metric evaluation failed: {type(value).__name__}",
                                details={"evaluator_diagnostic": diagnostic},
                            )
                        )
                    else:
                        emit(
                            MetricResult(
                                sample_id=sample.id,
                                metric_name=metric_name,
                                score=float(value),
                                details={"evaluator_diagnostic": retry_diagnostic}
                                if retry_diagnostic
                                else {},
                            )
                        )

        order = {
            (sample.id, metric_name): index
            for index, (sample, metric_name) in enumerate(
                (sample, metric_name)
                for sample in samples
                for metric_name in metric_names
            )
        }
        return sorted(
            results, key=lambda item: order[(item.sample_id, item.metric_name)]
        )

    def prepare_snapshots_for_scoring(
        self,
        snapshots: list[AnswerSnapshot],
        *,
        full_contexts: bool = False,
    ) -> tuple[list[AnswerSnapshot], dict[str, dict[str, Any]]]:
        prepared: list[AnswerSnapshot] = []
        diagnostics: dict[str, dict[str, Any]] = {}
        for snapshot in snapshots:
            raw_contexts = list(snapshot.retrieved_contexts)
            if full_contexts:
                bounded_contexts = raw_contexts
                diagnostic = {
                    "original_context_count": len(raw_contexts),
                    "original_context_characters": sum(
                        len(item) for item in raw_contexts
                    ),
                    "scored_context_count": len(raw_contexts),
                    "scored_context_characters": sum(
                        len(item) for item in raw_contexts
                    ),
                    "contexts_truncated": False,
                }
                selection = {
                    "context_selection": "raw_original_order",
                    "selected_evidence_ids": [],
                    "selected_claim_ids": [],
                    "excluded_evidence_ids": [],
                }
            else:
                contexts, selection = self._scoring_contexts(snapshot)
                bounded_contexts, diagnostic = self._bounded_contexts(contexts)
            diagnostic.update(selection)
            # Keep the exact initial context window visible in the report. The
            # snapshot retains the full retrieval result for auditability, but
            # these are the strings actually sent to RAGAS before any
            # per-metric retry shrinks the window further.
            diagnostic["scored_contexts"] = bounded_contexts
            prepared.append(
                snapshot.model_copy(
                    update={
                        "retrieved_contexts": bounded_contexts,
                        "metadata": {
                            **snapshot.metadata,
                            "_raw_retrieved_contexts": raw_contexts,
                            "_full_contexts_for_scoring": full_contexts,
                        },
                    }
                )
            )
            diagnostics[snapshot.sample_id] = diagnostic
        return prepared, diagnostics

    @staticmethod
    def _scoring_contexts(
        snapshot: AnswerSnapshot,
    ) -> tuple[list[str], dict[str, object]]:
        evidence_by_id = {
            str(item.get("id") or ""): item
            for item in snapshot.evidence
            if item.get("id") and item.get("content")
        }
        quality_by_id = {
            str(item.get("evidence_id") or ""): float(item.get("score") or 0.0)
            for item in (snapshot.retrieval_summary or {}).get("evidence_quality") or []
            if item.get("evidence_id")
        }

        evidence_relevance = {
            evidence_id: _scoring_context_relevance(
                snapshot.question,
                str(evidence_by_id[evidence_id].get("content") or ""),
            )
            for evidence_id in evidence_by_id
        }
        quality_ids = sorted(
            (
                evidence_id
                for evidence_id in quality_by_id
                if evidence_id in evidence_by_id
            ),
            # Drop boilerplate first because historical runs may contain
            # quality scores produced by the old source-name matching
            # heuristic. Question relevance is considered before the quality
            # score so a highly rated catalog/template chunk cannot displace
            # a lower-rated chunk containing the answer facts.
            key=lambda evidence_id: (
                not evidence_relevance.get(evidence_id, (0, False))[1],
                evidence_relevance.get(evidence_id, (0, False))[0],
                quality_by_id[evidence_id],
                evidence_id,
            ),
            reverse=True,
        )
        selected_ids: list[str] = []
        selected_claim_ids: list[str] = []
        candidate_ids: list[str] = []
        for coverage in (snapshot.retrieval_summary or {}).get("claim_coverage") or []:
            if coverage.get("status") not in {"supported", "partial", "conflicting"}:
                continue
            candidates = [
                str(evidence_id)
                for evidence_id in coverage.get("evidence_ids") or []
                if str(evidence_id) in evidence_by_id
            ]
            candidate_ids.extend(candidates)
            if not candidates:
                continue
            # Preserve the strongest evidence from every available content kind.
            # Joint hardware questions often need both circuit and document proof;
            # selecting one global maximum silently discarded one of those sources.
            best_by_kind: dict[str, str] = {}
            for evidence_id in candidates:
                item = evidence_by_id[evidence_id]
                kind = str(
                    item.get("content_kind")
                    or (item.get("metadata") or {}).get("content_kind")
                    or "unknown"
                )
                current = best_by_kind.get(kind)
                if current is None or (
                    quality_by_id.get(evidence_id, 0.0),
                    evidence_id,
                ) > (
                    quality_by_id.get(current, 0.0),
                    current,
                ):
                    best_by_kind[kind] = evidence_id
            claim_selected = sorted(
                best_by_kind.values(),
                key=lambda item: (quality_by_id.get(item, 0.0), item),
                reverse=True,
            )
            if claim_selected:
                selected_claim_ids.append(str(coverage.get("claim_id") or ""))
            for selected_id in claim_selected:
                if selected_id not in selected_ids:
                    selected_ids.append(selected_id)

        # ``retrieved_contexts`` is kept in the retriever's original order for
        # auditability, but that order often starts with generic helper text.
        # When evidence quality is available, put the highest-quality evidence
        # ahead of that fallback list so the bounded RAGAS input does not drop
        # the facts that retrieval already identified as useful.
        prioritized_ids: list[str] = []
        if selected_ids and quality_ids:
            claim_relevance = max(
                evidence_relevance.get(evidence_id, (0, False))[0]
                for evidence_id in selected_ids
            )
            # A stale claim ledger can contain a technically valid but
            # question-irrelevant topology row. Let a clearly more relevant
            # high-quality document row lead the bounded context window, while
            # preserving claim evidence when relevance is tied or unavailable.
            prioritized_ids.extend(
                evidence_id
                for evidence_id in quality_ids
                if evidence_relevance.get(evidence_id, (0, False))[0] > claim_relevance
            )
        prioritized_ids.extend(selected_ids)
        for evidence_id in quality_ids:
            if evidence_id not in prioritized_ids:
                prioritized_ids.append(evidence_id)

        if not prioritized_ids:
            return list(snapshot.retrieved_contexts), {
                "context_selection": "original_order",
                "selected_evidence_ids": [],
                "selected_claim_ids": [],
                "excluded_evidence_ids": [],
            }

        selected: list[str] = []
        seen: set[str] = set()
        for evidence_id in prioritized_ids:
            content = str(evidence_by_id[evidence_id].get("content") or "")
            if content and content not in seen:
                selected.append(content)
                seen.add(content)
        for context in snapshot.retrieved_contexts:
            if context and context not in seen:
                selected.append(context)
                seen.add(context)
        return selected, {
            "context_selection": (
                "claim_coverage+evidence_quality"
                if selected_ids and quality_ids
                else "evidence_quality"
                if quality_ids
                else "claim_coverage"
            ),
            "selected_evidence_ids": selected_ids,
            "selected_claim_ids": selected_claim_ids,
            "excluded_evidence_ids": sorted(set(candidate_ids) - set(selected_ids)),
            "quality_prioritized_evidence_ids": quality_ids,
        }

    def _retry_transient_metric(
        self,
        backend: RagasBackend,
        record: dict[str, Any],
        metric_name: str,
        *,
        initial_error: Exception,
        initial_attempts: int,
    ) -> tuple[Any, int, dict[str, int | str] | None]:
        """Retry transient judge failures for metrics without context fallback."""

        attempts = initial_attempts
        value: Any = initial_error
        if self.config.max_retries <= 0:
            return value, attempts, None

        while attempts <= self.config.max_retries and isinstance(value, Exception):
            if not _is_retryable_evaluator_error(value):
                break
            attempts += 1
            _backoff_sleep(attempts, value)
            try:
                retry_rows = backend.score([record], [metric_name])
                value = retry_rows[0].get(metric_name)
            except Exception as exc:
                value = exc

        diagnostic = {
            "kind": "recovered_after_retry"
            if not isinstance(value, Exception)
            else "retry_exhausted",
            "attempts": attempts,
            "sample_id": str(record.get("sample_id") or ""),
            "metric_name": metric_name,
        }
        return value, attempts, diagnostic

    def _retry_with_smaller_context(
        self,
        backend: RagasBackend,
        sample: EvaluationSample,
        snapshot: AnswerSnapshot,
        metric_name: str,
        *,
        initial_error: Exception,
        initial_attempts: int,
    ) -> tuple[Any, int, dict[str, int | str] | None]:
        budget = sum(len(context) for context in snapshot.retrieved_contexts)
        attempts = initial_attempts
        context_budget_attempts = 1
        original_characters = budget
        original_count = len(snapshot.retrieved_contexts)
        contexts = snapshot.retrieved_contexts
        value: Any = initial_error

        while (
            context_budget_attempts < self.config.scoring_max_budget_attempts
            and budget > 1
        ):
            next_budget = max(
                1, int(budget * self.config.scoring_context_shrink_factor)
            )
            if next_budget >= budget:
                break
            budget = next_budget
            contexts, _ = self._bounded_contexts(
                snapshot.retrieved_contexts, max_context_chars=budget
            )
            attempts += 1
            context_budget_attempts += 1
            _backoff_sleep(
                attempts,
                initial_error if isinstance(initial_error, Exception) else None,
            )
            try:
                rows = backend.score(
                    [
                        self._record(
                            sample, snapshot, metric_name, retrieved_contexts=contexts
                        )
                    ],
                    [metric_name],
                )
                value = rows[0].get(metric_name)
            except Exception as exc:
                value = exc
            if not isinstance(value, Exception):
                return (
                    value,
                    attempts,
                    {
                        "kind": "recovered_with_smaller_context",
                        "attempts": attempts,
                        "sample_id": sample.id,
                        "metric_name": metric_name,
                        "context_budget_attempts": context_budget_attempts,
                        "original_context_count": original_count,
                        "original_context_characters": original_characters,
                        "final_context_count": len(contexts),
                        "final_context_characters": sum(
                            len(context) for context in contexts
                        ),
                    },
                )
            if not _is_retryable_context_error(value):
                return value, attempts, None

        return (
            value,
            attempts,
            {
                "kind": "context_budget_exhausted",
                "attempts": attempts,
                "sample_id": sample.id,
                "metric_name": metric_name,
                "context_budget_attempts": context_budget_attempts,
                "original_context_count": original_count,
                "original_context_characters": original_characters,
                "final_context_count": len(contexts),
                "final_context_characters": sum(len(context) for context in contexts),
            },
        )

    def _bounded_contexts(
        self,
        contexts: list[str],
        *,
        max_context_chars: int | None = None,
    ) -> tuple[list[str], dict[str, int | bool]]:
        original_count = len(contexts)
        original_characters = sum(len(context) for context in contexts)
        bounded: list[str] = []
        scored_characters = 0
        total_budget = max_context_chars or self.config.max_context_chars

        seen: set[str] = set()
        seen_prefixes: set[str] = set()
        for context in contexts:
            if len(bounded) >= self.config.max_contexts_per_sample:
                break
            context = strip_markup(context)
            if not context or context in seen:
                continue
            prefix = re.sub(r"\s+", "", context.casefold())[:120]
            if len(prefix) >= 24 and prefix in seen_prefixes:
                continue
            seen.add(context)
            seen_prefixes.add(prefix)
            remaining = total_budget - scored_characters
            if remaining <= 0:
                break
            bounded_context = context[
                : min(remaining, self.config.max_context_chars_per_item)
            ]
            bounded.append(bounded_context)
            scored_characters += len(bounded_context)
            if scored_characters >= total_budget:
                break

        diagnostic = {
            "original_context_count": original_count,
            "original_context_characters": original_characters,
            "scored_context_count": len(bounded),
            "scored_context_characters": scored_characters,
            "contexts_truncated": len(bounded) != original_count
            or scored_characters != original_characters,
        }
        return bounded, diagnostic

    @staticmethod
    def _record(
        sample: EvaluationSample,
        snapshot: AnswerSnapshot,
        metric_name: str,
        *,
        retrieved_contexts: list[str] | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "sample_id": sample.id,
            "user_input": sample.question,
        }
        if metric_name in {"answer_correctness", "answer_relevancy", "faithfulness"}:
            record["response"] = snapshot.scored_response or snapshot.response
        if metric_name in {"answer_correctness", "context_precision", "context_recall"}:
            record["reference"] = sample.reference_answer
        if metric_name in {"faithfulness", "context_precision", "context_recall"}:
            record["retrieved_contexts"] = (
                snapshot.retrieved_contexts
                if retrieved_contexts is None
                else retrieved_contexts
            )
        return record

    @staticmethod
    def _record_for_metrics(
        sample: EvaluationSample,
        snapshot: AnswerSnapshot,
        metric_names: list[str],
    ) -> dict[str, Any]:
        """Build one record containing the union of fields used by a batch."""

        record: dict[str, Any] = {
            "sample_id": sample.id,
            "user_input": sample.question,
        }
        if any(
            name in {"answer_correctness", "answer_relevancy", "faithfulness"}
            for name in metric_names
        ):
            record["response"] = snapshot.scored_response or snapshot.response
        if any(
            name in {"answer_correctness", "context_precision", "context_recall"}
            for name in metric_names
        ):
            record["reference"] = sample.reference_answer
        if any(name in CONTEXT_METRICS for name in metric_names):
            record["retrieved_contexts"] = snapshot.retrieved_contexts
        return record


class _NativeRagasBackend:
    def __init__(self, config: EvaluationConfig):
        self.config = config
        self._llm = None
        self._embeddings = None
        self._modern_embeddings = None
        self._modern_metrics_cache: dict[tuple[str, ...], list[Any]] = {}

    def _build_llm(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ):
        primary = base_url is None and api_key is None and model is None
        if primary and self._llm is not None:
            return self._llm
        from openai import AsyncOpenAI
        from ragas.llms import llm_factory

        use_base = base_url or self.config.llm_base_url
        use_key = api_key or self.config.llm_api_key
        use_model = model or self.config.llm_model
        llm_client = AsyncOpenAI(
            api_key=use_key or "not-required",
            base_url=use_base,
            timeout=self.config.timeout_seconds,
        )
        llm = llm_factory(
            use_model,
            client=llm_client,
            max_tokens=self.config.llm_max_tokens,
        )
        if primary:
            self._llm = llm
        return llm

    def _build_embeddings(self):
        if self._embeddings is not None:
            return self._embeddings
        from langchain_openai import OpenAIEmbeddings

        kwargs = {
            "model": self.config.embedding_model,
            "api_key": self.config.embedding_api_key or "not-required",
            "base_url": self.config.embedding_base_url,
            "request_timeout": self.config.timeout_seconds,
            # OpenAI-compatible providers, including Volcengine Ark, accept
            # text inputs but reject the integer token arrays LangChain sends
            # when its length-safety path is enabled. Evaluation contexts are
            # already bounded before this backend is called.
            "check_embedding_ctx_length": False,
        }
        if self.config.embedding_dims is not None:
            kwargs["dimensions"] = self.config.embedding_dims
        self._embeddings = OpenAIEmbeddings(
            **kwargs,
        )
        return self._embeddings

    def _build_modern_embeddings(self):
        """Build the collections-API embedding provider once per run."""

        if self._modern_embeddings is not None:
            return self._modern_embeddings
        from openai import AsyncOpenAI
        from ragas.embeddings import OpenAIEmbeddings as ModernOpenAIEmbeddings

        config = self.config
        client = AsyncOpenAI(
            api_key=config.embedding_api_key or "not-required",
            base_url=config.embedding_base_url,
            timeout=config.timeout_seconds,
        )

        class ConfiguredEmbeddings(ModernOpenAIEmbeddings):
            async def aembed_text(self, text: str, **kwargs: Any) -> list[float]:
                if config.embedding_dims is not None:
                    kwargs.setdefault("dimensions", config.embedding_dims)
                return await super().aembed_text(text, **kwargs)

            async def aembed_texts(
                self, texts: list[str], **kwargs: Any
            ) -> list[list[float]]:
                if config.embedding_dims is not None:
                    kwargs.setdefault("dimensions", config.embedding_dims)
                return await super().aembed_texts(texts, **kwargs)

        self._modern_embeddings = ConfiguredEmbeddings(
            client=client,
            model=config.embedding_model,
        )
        return self._modern_embeddings

    def _build_run_config(self):
        from ragas.run_config import RunConfig

        return RunConfig(
            timeout=self.config.timeout_seconds,
            max_workers=self.config.max_workers,
            max_retries=self.config.max_retries,
        )

    def _build_metrics(self, metric_names: list[str]) -> list[Any]:
        # Legacy helper retained for older callers/tests only. Production
        # scoring uses the collections API in ``_build_modern_metrics``.
        # Keep imports public to avoid private-module coupling while callers
        # migrate away from the deprecated evaluate()-style path.
        from ragas.metrics import (
            AnswerCorrectness,
            Faithfulness,
            LLMContextPrecisionWithReference,
            LLMContextRecall,
            ResponseRelevancy,
        )

        constructors = {
            "answer_correctness": AnswerCorrectness,
            "faithfulness": Faithfulness,
            "answer_relevancy": ResponseRelevancy,
            "context_precision": LLMContextPrecisionWithReference,
            "context_recall": LLMContextRecall,
        }
        metrics = []
        for name in metric_names:
            metric_type = constructors[name]
            metrics.append(
                metric_type()
                if name == "answer_relevancy"
                else metric_type(max_retries=self.config.max_retries)
            )
        return metrics

    def _build_modern_metrics(self, metric_names: list[str], llm: Any) -> list[Any]:
        cache_key = tuple(metric_names) + (f"llm:{id(llm)}",)
        cached = self._modern_metrics_cache.get(cache_key)
        if cached is not None:
            return cached
        from ragas.metrics.collections import (
            AnswerCorrectness,
            AnswerRelevancy,
            ContextPrecisionWithReference,
            ContextRecall,
            Faithfulness,
        )

        embeddings = self._build_modern_embeddings()
        constructors = {
            "answer_correctness": lambda: AnswerCorrectness(
                llm=llm, embeddings=embeddings
            ),
            "answer_relevancy": lambda: AnswerRelevancy(
                llm=llm, embeddings=embeddings
            ),
            "faithfulness": lambda: Faithfulness(llm=llm),
            "context_precision": lambda: ContextPrecisionWithReference(llm=llm),
            "context_recall": lambda: ContextRecall(llm=llm),
        }
        metrics = []
        for name in metric_names:
            metric = constructors[name]()
            # Collections metrics delegate retry scheduling to the caller;
            # retain the value for diagnostics and compatibility metadata.
            metric.max_retries = self.config.max_retries
            metrics.append(metric)
        self._modern_metrics_cache[cache_key] = metrics
        return metrics

    async def _score_modern_async(
        self,
        records: list[dict[str, Any]],
        metric_names: list[str],
        llm: Any,
    ) -> list[dict[str, Any]]:
        metrics = self._build_modern_metrics(metric_names, llm)
        semaphore = asyncio.Semaphore(max(1, self.config.max_workers))

        async def score_one(record: dict[str, Any], metric: Any) -> Any:
            metric_name = str(getattr(metric, "name", ""))
            if metric_name == "answer_correctness":
                fields = {"user_input", "response", "reference"}
            elif metric_name == "answer_relevancy":
                fields = {"user_input", "response"}
            elif metric_name == "faithfulness":
                fields = {"user_input", "response", "retrieved_contexts"}
            elif metric_name == "context_precision_with_reference":
                fields = {"user_input", "reference", "retrieved_contexts"}
            else:
                fields = {"user_input", "reference", "retrieved_contexts"}
            kwargs = {key: record[key] for key in fields if key in record}
            async with semaphore:
                result = await metric.ascore(**kwargs)
            return getattr(result, "value", result)

        jobs = [score_one(record, metric) for record in records for metric in metrics]
        values = await asyncio.gather(*jobs, return_exceptions=True)
        rows: list[dict[str, Any]] = []
        offset = 0
        for _record in records:
            row: dict[str, Any] = {}
            for metric_name in metric_names:
                row[metric_name] = values[offset]
                offset += 1
            rows.append(row)
        return rows

    @staticmethod
    def _run_async(coro):
        """Run a coroutine from sync code, including a host event loop."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        result: list[Any] = []
        error: list[BaseException] = []

        def runner() -> None:
            try:
                result.append(asyncio.run(coro))
            except BaseException as exc:  # pragma: no cover - host-loop fallback
                error.append(exc)

        thread = threading.Thread(target=runner, name="hdb-ragas-loop")
        thread.start()
        thread.join()
        if error:
            raise error[0]
        return result[0]

    def score(
        self, records: list[dict[str, Any]], metric_names: list[str]
    ) -> list[dict[str, Any]]:
        try:
            from ragas.metrics.collections import AnswerCorrectness  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "RAGAS evaluation dependencies are missing; run 'uv sync --group eval'"
            ) from exc

        llm = self._build_llm()
        rows = self._run_async(self._score_modern_async(records, metric_names, llm))
        primary_embeddings = (
            self._build_embeddings() if self.config.fallback_ready else None
        )
        return self._fill_with_fallback(
            records, metric_names, rows, llm, primary_embeddings
        )

    def _fill_with_fallback(
        self, records, metric_names, rows, primary_llm, primary_embeddings
    ):
        """Re-run failed entries against a fallback judge when one is configured.

        Primary-judge failures (quota, 5xx, timeouts) leave NaN/exception cells;
        the fallback judge only fills those cells so healthy primary scores
        stay untouched.
        """

        if not self.config.fallback_ready or not rows:
            return rows
        failed_by_metric: dict[str, list[int]] = {
            name: [
                row_idx
                for row_idx, row in enumerate(rows)
                if self._is_failed_value(row.get(name))
            ]
            for name in metric_names
        }
        failed_by_metric = {
            name: positions for name, positions in failed_by_metric.items() if positions
        }
        if not failed_by_metric:
            return rows
        try:
            fallback_llm = self._build_llm(
                base_url=self.config.llm_fallback_base_url,
                api_key=self.config.llm_fallback_api_key or None,
                model=self.config.llm_fallback_model,
            )
            for name, row_indices in failed_by_metric.items():
                # Only failed cells are sent to the fallback judge. This is
                # important for large runs: a single transient primary error
                # must not re-score every healthy sample and metric.
                fallback_records = [records[index] for index in row_indices]
                fallback_rows = self._run_async(
                    self._score_modern_async(fallback_records, [name], fallback_llm)
                )
                for offset, row_idx in enumerate(row_indices):
                    if offset >= len(fallback_rows):
                        continue
                    value = fallback_rows[offset].get(name)
                    if not self._is_failed_value(value):
                        rows[row_idx][name] = value
        except Exception:
            return rows
        return rows

    @staticmethod
    def _is_failed_value(value: Any) -> bool:
        if value is None or isinstance(value, Exception):
            return True
        return isinstance(value, float) and math.isnan(value)
