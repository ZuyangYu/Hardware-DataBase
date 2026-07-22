from __future__ import annotations

from typing import Literal

from .schemas import EvaluationSample


RAGAS_METRICS = frozenset(
    {
        "answer_correctness",
        "faithfulness",
        "answer_relevancy",
        "context_precision",
        "context_recall",
    }
)
NON_RETRIEVAL_TAGS = frozenset({"direct", "small-talk", "permission", "isolation"})


def evaluation_cohort(sample: EvaluationSample) -> Literal["retrieval", "non_retrieval"]:
    """Classify samples that intentionally do not query the knowledge base."""

    if NON_RETRIEVAL_TAGS.intersection(sample.tags):
        return "non_retrieval"
    return "retrieval"


def is_ragas_metric(metric_name: str) -> bool:
    return metric_name in RAGAS_METRICS
