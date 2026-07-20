from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Protocol

from .config import EvaluationConfig
from .schemas import AnswerSnapshot, EvaluationSample, MetricResult


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


class RagasBackend(Protocol):
    def score(self, records: list[dict[str, Any]], metric_names: list[str]) -> list[dict[str, Any]]: ...


def _metric_is_applicable(metric_name: str, sample: EvaluationSample, snapshot: AnswerSnapshot) -> bool:
    if metric_name == "context_recall":
        return bool(sample.reference_contexts and snapshot.retrieved_contexts)
    if metric_name in {"faithfulness", "context_precision"}:
        return bool(snapshot.retrieved_contexts)
    return True


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
    ) -> list[MetricResult]:
        unknown = sorted(set(metric_names) - STANDARD_METRICS)
        if unknown:
            raise ValueError(f"unknown RAGAS metrics: {', '.join(unknown)}")
        if not snapshots_prepared:
            snapshots, _ = self.prepare_snapshots_for_scoring(snapshots)
        snapshot_by_id = {snapshot.sample_id: snapshot for snapshot in snapshots}
        results: list[MetricResult] = []
        grouped: dict[tuple[str, ...], list[tuple[EvaluationSample, AnswerSnapshot]]] = defaultdict(list)

        for sample in samples:
            snapshot = snapshot_by_id.get(sample.id)
            if snapshot is None or snapshot.status != "success":
                for metric_name in metric_names:
                    results.append(
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
                    results.append(
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

        for applicable, pairs in grouped.items():
            records = [self._record(sample, snapshot) for sample, snapshot in pairs]
            for metric_name in applicable:
                try:
                    scored_rows = backend.score(records, [metric_name])  # type: ignore[union-attr]
                except Exception as exc:
                    scored_rows = [{metric_name: exc} for _ in records]
                for index, ((sample, _), row) in enumerate(zip(pairs, scored_rows, strict=True)):
                    value = row.get(metric_name)
                    attempts = 1
                    while isinstance(value, float) and math.isnan(value) and attempts <= self.config.max_retries:
                        attempts += 1
                        try:
                            retry_rows = backend.score([records[index]], [metric_name])  # type: ignore[union-attr]
                            value = retry_rows[0].get(metric_name)
                        except Exception as exc:
                            value = exc
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
                        }
                        if isinstance(value, Exception):
                            diagnostic["error_type"] = type(value).__name__
                            diagnostic["error_message"] = str(value)
                        results.append(
                            MetricResult(
                                sample_id=sample.id,
                                metric_name=metric_name,
                                status="failed",
                                reason=f"metric evaluation failed: {type(value).__name__}",
                                details={"evaluator_diagnostic": diagnostic},
                            )
                        )
                    else:
                        results.append(
                            MetricResult(
                                sample_id=sample.id,
                                metric_name=metric_name,
                                score=float(value),
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
    ) -> tuple[list[AnswerSnapshot], dict[str, dict[str, int | bool]]]:
        prepared: list[AnswerSnapshot] = []
        diagnostics: dict[str, dict[str, int | bool | str]] = {}
        for snapshot in snapshots:
            contexts, selection = self._scoring_contexts(snapshot)
            bounded_contexts, diagnostic = self._bounded_contexts(contexts)
            diagnostic["context_selection"] = selection
            prepared.append(snapshot.model_copy(update={"retrieved_contexts": bounded_contexts}))
            diagnostics[snapshot.sample_id] = diagnostic
        return prepared, diagnostics

    @staticmethod
    def _scoring_contexts(snapshot: AnswerSnapshot) -> tuple[list[str], str]:
        evidence_by_id = {
            str(item.get("id") or ""): str(item.get("content") or "")
            for item in snapshot.evidence
            if item.get("id") and item.get("content")
        }
        supporting_ids: list[str] = []
        for coverage in (snapshot.retrieval_summary or {}).get("claim_coverage") or []:
            if coverage.get("status") not in {"supported", "partial", "conflicting"}:
                continue
            supporting_ids.extend(str(item) for item in coverage.get("evidence_ids") or [])

        selected: list[str] = []
        seen: set[str] = set()
        for evidence_id in supporting_ids:
            content = evidence_by_id.get(evidence_id, "")
            if content and content not in seen:
                selected.append(content)
                seen.add(content)
        if not selected:
            return list(snapshot.retrieved_contexts), "original_order"
        for context in snapshot.retrieved_contexts:
            if context and context not in seen:
                selected.append(context)
                seen.add(context)
        return selected, "claim_coverage"

    def _bounded_contexts(self, contexts: list[str]) -> tuple[list[str], dict[str, int | bool]]:
        original_count = len(contexts)
        original_characters = sum(len(context) for context in contexts)
        bounded: list[str] = []
        scored_characters = 0

        for context in contexts:
            if len(bounded) >= self.config.max_contexts_per_sample:
                break
            remaining = self.config.max_context_chars - scored_characters
            if remaining <= 0:
                break
            bounded_context = context[:remaining]
            bounded.append(bounded_context)
            scored_characters += len(bounded_context)
            if len(bounded_context) < len(context):
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
    def _record(sample: EvaluationSample, snapshot: AnswerSnapshot) -> dict[str, Any]:
        return {
            "sample_id": sample.id,
            "user_input": sample.question,
            "response": snapshot.scored_response or snapshot.response,
            "retrieved_contexts": snapshot.retrieved_contexts,
            "reference": sample.reference_answer,
            "reference_contexts": sample.reference_contexts,
        }


class _NativeRagasBackend:
    def __init__(self, config: EvaluationConfig):
        self.config = config

    def _build_llm(self):
        from openai import AsyncOpenAI
        from ragas.llms import llm_factory

        llm_client = AsyncOpenAI(
            api_key=self.config.llm_api_key or "not-required",
            base_url=self.config.llm_base_url,
            timeout=self.config.timeout_seconds,
        )
        return llm_factory(
            self.config.llm_model,
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
        return [
            {name: row.get(RAGAS_RESULT_KEYS[name]) for name in metric_names}
            for row in frame.to_dict(orient="records")
        ]
