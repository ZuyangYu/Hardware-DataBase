"""Project-level span names and OpenInference semantic values."""

from __future__ import annotations

from enum import StrEnum


OPENINFERENCE_SPAN_KIND = "openinference.span.kind"


class SpanKind(StrEnum):
    AGENT = "AGENT"
    CHAIN = "CHAIN"
    EVALUATOR = "EVALUATOR"
    LLM = "LLM"
    RETRIEVER = "RETRIEVER"
    RERANKER = "RERANKER"
    TOOL = "TOOL"
    PROMPT = "PROMPT"
    EMBEDDING = "EMBEDDING"


QUERY_ATTRIBUTES = {
    "hdb.query.mode",
    "hdb.query.source",
    "hdb.turn.id",
    "hdb.session.id",
}

METRIC_LABEL_KEYS = {
    "status",
    "mode",
    "retriever",
    "stage",
    "queue",
    "operation",
    "tool",
    "metric",
    "provider",
    "streaming",
    # Memory labels are intentionally limited to bounded enumerations.  IDs,
    # query text and exception messages must never become metric dimensions.
    "backend",
    "enabled",
    "kind",
    "job_kind",
    "projection_kind",
    "scope",
    "semantic_index",
}
