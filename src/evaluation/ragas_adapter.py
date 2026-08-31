from __future__ import annotations

import math
import random
import re
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


def _scoring_query_tokens(text: str) -> set[str]:
    """Return small, deterministic tokens for scoring-context reranking."""

    value = str(text or "").casefold()
    tokens = set(re.findall(r"[a-z][a-z0-9_.+-]{1,}|[0-9][a-z0-9_.+-]{1,}", value))
    for block in re.findall(r"[\u4e00-\u9fff]{2,}", value):
        tokens.add(block)
        for size in (2, 3):
            tokens.update(block[index : index + size] for index in range(len(block) - size + 1))
    return {token for token in tokens if len(token) >= 2}


def _scoring_context_relevance(question: str, content: str) -> tuple[int, bool]:
    tokens = _scoring_query_tokens(question)
    searchable = str(content or "").casefold()
    overlap = sum(token in searchable for token in tokens)
    boilerplate = any(
        marker in searchable
        for marker in ("填写说明", "template instructions", "封面", "cover", "模板变更历史")
    )
    return overlap, boilerplate


_MARKUP_TAG_RE = re.compile(r"<[^>]+>")
_MARKUP_SPACE_RE = re.compile(r"[ \t\u3000]{2,}")
_MARKUP_HINT_RE = re.compile(r"<(table|tr|td|th|br|p|div|span|ul|ol|li)[\s>/]", re.IGNORECASE)


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
    def score(self, records: list[dict[str, Any]], metric_names: list[str]) -> list[dict[str, Any]]: ...


def _metric_is_applicable(metric_name: str, sample: EvaluationSample, snapshot: AnswerSnapshot) -> bool:
    if metric_name == "context_recall":
        return bool(sample.reference_contexts and snapshot.retrieved_contexts)
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
    return status is not None and (status == 429 or status >= 500)


def _backoff_sleep(attempt: int, exc: Exception | None = None) -> None:
    """Exponential backoff before a scoring retry; honors Retry-After."""

    delay = _retry_after_seconds(exc) if exc is not None else None
    if delay is None:
        base = min(30.0, 2.0 ** max(1, attempt))
        delay = base * (0.5 + random.random())
    time.sleep(min(delay, 60.0))


def _is_retryable_context_error(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, ConnectionError)) or _error_status_code(exc) == 400


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

        grouped: dict[tuple[str, ...], list[tuple[EvaluationSample, AnswerSnapshot]]] = defaultdict(list)

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
                    (sample, metric_name) for sample in samples for metric_name in metric_names
                )
            }
            return sorted(results, key=lambda item: order[(item.sample_id, item.metric_name)])

        for applicable, pairs in grouped.items():
            for metric_name in applicable:
                records = [self._record(sample, snapshot, metric_name) for sample, snapshot in pairs]
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
                for index, ((sample, _), row) in enumerate(zip(pairs, scored_rows, strict=True)):
                    value = row.get(metric_name)
                    attempts = 1
                    retry_diagnostic: dict[str, int | str] | None = None
                    if (
                        isinstance(value, Exception)
                        and metric_name in CONTEXT_METRICS
                        and _is_retryable_context_error(value)
                    ):
                        value, attempts, retry_diagnostic = self._retry_with_smaller_context(
                            backend,  # type: ignore[arg-type]
                            sample,
                            pairs[index][1],
                            metric_name,
                            initial_error=value,
                            initial_attempts=attempts,
                        )
                    while isinstance(value, float) and math.isnan(value) and attempts <= self.config.max_retries:
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
                    ):
                        value, attempts, retry_diagnostic = self._retry_with_smaller_context(
                            backend,  # type: ignore[arg-type]
                            sample,
                            pairs[index][1],
                            metric_name,
                            initial_error=value,
                            initial_attempts=attempts,
                        )
                    if isinstance(value, Exception) or value is None or (
                        isinstance(value, float) and math.isnan(value)
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
                                len(context) for context in pairs[index][1].retrieved_contexts
                            ),
                        }
                        if retry_diagnostic:
                            diagnostic.update(
                                {
                                    "context_count": retry_diagnostic["final_context_count"],
                                    "context_characters": retry_diagnostic["final_context_characters"],
                                    "context_budget_attempts": retry_diagnostic["context_budget_attempts"],
                                }
                            )
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
                        details = {"evaluator_diagnostic": retry_diagnostic} if retry_diagnostic else {}
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
                (sample, metric_name) for sample in samples for metric_name in metric_names
            )
        }
        return sorted(results, key=lambda item: order[(item.sample_id, item.metric_name)])

    def prepare_snapshots_for_scoring(
        self,
        snapshots: list[AnswerSnapshot],
    ) -> tuple[list[AnswerSnapshot], dict[str, dict[str, Any]]]:
        prepared: list[AnswerSnapshot] = []
        diagnostics: dict[str, dict[str, Any]] = {}
        for snapshot in snapshots:
            contexts, selection = self._scoring_contexts(snapshot)
            bounded_contexts, diagnostic = self._bounded_contexts(contexts)
            diagnostic.update(selection)
            # Keep the exact initial context window visible in the report. The
            # snapshot retains the full retrieval result for auditability, but
            # these are the strings actually sent to RAGAS before any
            # per-metric retry shrinks the window further.
            diagnostic["scored_contexts"] = bounded_contexts
            prepared.append(snapshot.model_copy(update={"retrieved_contexts": bounded_contexts}))
            diagnostics[snapshot.sample_id] = diagnostic
        return prepared, diagnostics

    @staticmethod
    def _scoring_contexts(snapshot: AnswerSnapshot) -> tuple[list[str], dict[str, object]]:
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
            (evidence_id for evidence_id in quality_by_id if evidence_id in evidence_by_id),
            # Drop boilerplate first because historical runs may contain
            # quality scores produced by the old source-name matching
            # heuristic. Quality remains the primary ranking signal, with
            # lexical overlap breaking ties.
            key=lambda evidence_id: (
                not evidence_relevance.get(evidence_id, (0, False))[1],
                quality_by_id[evidence_id],
                evidence_relevance.get(evidence_id, (0, False))[0],
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
                if current is None or (quality_by_id.get(evidence_id, 0.0), evidence_id) > (
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

        while context_budget_attempts < self.config.scoring_max_budget_attempts and budget > 1:
            next_budget = max(1, int(budget * self.config.scoring_context_shrink_factor))
            if next_budget >= budget:
                break
            budget = next_budget
            contexts, _ = self._bounded_contexts(snapshot.retrieved_contexts, max_context_chars=budget)
            attempts += 1
            context_budget_attempts += 1
            _backoff_sleep(attempts, initial_error if isinstance(initial_error, Exception) else None)
            try:
                rows = backend.score(
                    [self._record(sample, snapshot, metric_name, retrieved_contexts=contexts)],
                    [metric_name],
                )
                value = rows[0].get(metric_name)
            except Exception as exc:
                value = exc
            if not isinstance(value, Exception):
                return value, attempts, {
                    "kind": "recovered_with_smaller_context",
                    "attempts": attempts,
                    "sample_id": sample.id,
                    "metric_name": metric_name,
                    "context_budget_attempts": context_budget_attempts,
                    "original_context_count": original_count,
                    "original_context_characters": original_characters,
                    "final_context_count": len(contexts),
                    "final_context_characters": sum(len(context) for context in contexts),
                }
            if not _is_retryable_context_error(value):
                return value, attempts, None

        return value, attempts, {
            "kind": "context_budget_exhausted",
            "attempts": attempts,
            "sample_id": sample.id,
            "metric_name": metric_name,
            "context_budget_attempts": context_budget_attempts,
            "original_context_count": original_count,
            "original_context_characters": original_characters,
            "final_context_count": len(contexts),
            "final_context_characters": sum(len(context) for context in contexts),
        }

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
            bounded_context = context[: min(remaining, self.config.max_context_chars_per_item)]
            bounded.append(bounded_context)
            scored_characters += len(bounded_context)
            if scored_characters >= total_budget:
                break

        diagnostic = {
            "original_context_count": original_count,
            "original_context_characters": original_characters,
            "scored_context_count": len(bounded),
            "scored_context_characters": scored_characters,
            "contexts_truncated": len(bounded) != original_count or scored_characters != original_characters,
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
                snapshot.retrieved_contexts if retrieved_contexts is None else retrieved_contexts
            )
        return record


class _NativeRagasBackend:
    def __init__(self, config: EvaluationConfig):
        self.config = config

    def _build_llm(self, *, base_url: str | None = None, api_key: str | None = None,
                   model: str | None = None):
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
        return llm_factory(
            use_model,
            client=llm_client,
            max_tokens=self.config.llm_max_tokens,
        )

    def _build_embeddings(self):
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(
            model=self.config.embedding_model,
            api_key=self.config.embedding_api_key or "not-required",
            base_url=self.config.embedding_base_url,
            request_timeout=self.config.timeout_seconds,
            # OpenAI-compatible providers, including Volcengine Ark, accept
            # text inputs but reject the integer token arrays LangChain sends
            # when its length-safety path is enabled. Evaluation contexts are
            # already bounded before this backend is called.
            check_embedding_ctx_length=False,
        )

    def _build_run_config(self):
        from ragas.run_config import RunConfig

        return RunConfig(
            timeout=self.config.timeout_seconds,
            max_workers=self.config.max_workers,
            max_retries=self.config.max_retries,
        )

    def _build_metrics(self, metric_names: list[str]) -> list[Any]:
        from ragas.metrics._answer_correctness import AnswerCorrectness
        from ragas.metrics._answer_relevance import ResponseRelevancy
        from ragas.metrics._context_precision import LLMContextPrecisionWithReference
        from ragas.metrics._context_recall import LLMContextRecall
        from ragas.metrics._faithfulness import Faithfulness

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
                metric_type(strictness=1)
                if name == "answer_relevancy"
                else metric_type(max_retries=self.config.max_retries)
            )
        return metrics

    def score(self, records: list[dict[str, Any]], metric_names: list[str]) -> list[dict[str, Any]]:
        try:
            from ragas import EvaluationDataset, evaluate
            from langchain_openai import OpenAIEmbeddings  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("RAGAS evaluation dependencies are missing; run 'uv sync --group eval'") from exc

        llm = self._build_llm()
        embeddings = self._build_embeddings()
        metrics = self._build_metrics(metric_names)
        dataset = EvaluationDataset.from_list(
            [{key: value for key, value in record.items() if key != "sample_id"} for record in records]
        )
        evaluated = evaluate(
            dataset=dataset,
            metrics=metrics,
            llm=llm,
            embeddings=embeddings,
            run_config=self._build_run_config(),
            raise_exceptions=len(records) == 1,
            show_progress=False,
        )
        frame = evaluated.to_pandas()
        rows = [
            {name: row.get(RAGAS_RESULT_KEYS[name]) for name in metric_names}
            for row in frame.to_dict(orient="records")
        ]
        return self._fill_with_fallback(records, metric_names, rows, llm, embeddings)

    def _fill_with_fallback(self, records, metric_names, rows, primary_llm, primary_embeddings):
        """Re-run failed entries against a fallback judge when one is configured.

        Primary-judge failures (quota, 5xx, timeouts) leave NaN/exception cells;
        the fallback judge only fills those cells so healthy primary scores
        stay untouched.
        """

        if not self.config.fallback_ready or not rows:
            return rows
        failed_positions = [
            (row_idx, name)
            for row_idx, row in enumerate(rows)
            for name in metric_names
            if self._is_failed_value(row.get(name))
        ]
        if not failed_positions:
            return rows
        try:
            from ragas import EvaluationDataset, evaluate

            fallback_llm = self._build_llm(
                base_url=self.config.llm_fallback_base_url,
                api_key=self.config.llm_fallback_api_key or None,
                model=self.config.llm_fallback_model,
            )
            evaluated = evaluate(
                dataset=EvaluationDataset.from_list(
                    [{k: v for k, v in rec.items() if k != "sample_id"} for rec in records]
                ),
                metrics=self._build_metrics(metric_names),
                llm=fallback_llm,
                embeddings=primary_embeddings,
                run_config=self._build_run_config(),
                raise_exceptions=False,
                show_progress=False,
            )
            frame = evaluated.to_pandas()
            fallback_rows = [
                {name: row.get(RAGAS_RESULT_KEYS[name]) for name in metric_names}
                for row in frame.to_dict(orient="records")
            ]
        except Exception:
            return rows
        for row_idx, name in failed_positions:
            if row_idx >= len(fallback_rows):
                continue
            value = fallback_rows[row_idx].get(name)
            if not self._is_failed_value(value):
                rows[row_idx][name] = value
        return rows

    @staticmethod
    def _is_failed_value(value: Any) -> bool:
        if value is None or isinstance(value, Exception):
            return True
        return isinstance(value, float) and math.isnan(value)
