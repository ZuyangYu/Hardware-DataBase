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
    ) -> list[MetricResult]:
        unknown = sorted(set(metric_names) - STANDARD_METRICS)
        if unknown:
            raise ValueError(f"unknown RAGAS metrics: {', '.join(unknown)}")
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
            try:
                scored_rows = backend.score(records, list(applicable))  # type: ignore[union-attr]
            except Exception as exc:
                scored_rows = [{name: exc for name in applicable} for _ in records]
            for (sample, _), row in zip(pairs, scored_rows, strict=True):
                for metric_name in applicable:
                    value = row.get(metric_name)
                    if isinstance(value, Exception) or value is None or (
                        isinstance(value, float) and math.isnan(value)
                    ):
                        results.append(
                            MetricResult(
                                sample_id=sample.id,
                                metric_name=metric_name,
                                status="failed",
                                reason=f"metric evaluation failed: {type(value).__name__}",
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

    @staticmethod
    def _record(sample: EvaluationSample, snapshot: AnswerSnapshot) -> dict[str, Any]:
        return {
            "sample_id": sample.id,
            "user_input": sample.question,
            "response": snapshot.response,
            "retrieved_contexts": snapshot.retrieved_contexts,
            "reference": sample.reference_answer,
            "reference_contexts": sample.reference_contexts,
        }


class _NativeRagasBackend:
    def __init__(self, config: EvaluationConfig):
        self.config = config

    def score(self, records: list[dict[str, Any]], metric_names: list[str]) -> list[dict[str, Any]]:
        try:
            from openai import AsyncOpenAI
            from ragas import EvaluationDataset, evaluate
            from ragas.embeddings import OpenAIEmbeddings
            from ragas.llms import llm_factory
            import ragas.metrics as ragas_metrics
        except ImportError as exc:
            raise RuntimeError("RAGAS evaluation dependencies are missing; run 'uv sync --group eval'") from exc

        llm_client = AsyncOpenAI(
            api_key=self.config.llm_api_key or "not-required",
            base_url=self.config.llm_base_url,
            timeout=self.config.timeout_seconds,
        )
        embedding_client = AsyncOpenAI(
            api_key=self.config.embedding_api_key or "not-required",
            base_url=self.config.embedding_base_url,
            timeout=self.config.timeout_seconds,
        )
        llm = llm_factory(self.config.llm_model, client=llm_client)
        embeddings = OpenAIEmbeddings(client=embedding_client, model=self.config.embedding_model)
        constructors = {
            "answer_correctness": "AnswerCorrectness",
            "faithfulness": "Faithfulness",
            "answer_relevancy": "ResponseRelevancy",
            "context_precision": "LLMContextPrecisionWithReference",
            "context_recall": "LLMContextRecall",
        }
        metrics = []
        for name in metric_names:
            metric_type = getattr(ragas_metrics, constructors[name])
            metrics.append(metric_type())
        dataset = EvaluationDataset.from_list(
            [{key: value for key, value in record.items() if key != "sample_id"} for record in records]
        )
        evaluated = evaluate(
            dataset=dataset,
            metrics=metrics,
            llm=llm,
            embeddings=embeddings,
            raise_exceptions=False,
            show_progress=False,
        )
        frame = evaluated.to_pandas()
        return [
            {name: row.get(RAGAS_RESULT_KEYS[name]) for name in metric_names}
            for row in frame.to_dict(orient="records")
        ]
