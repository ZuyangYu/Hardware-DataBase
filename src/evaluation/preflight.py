from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .answer_runner import _request_context
from .schemas import EvaluationSample


class EvaluationPreflight:
    """Validate scoped, evidence-required samples before online collection."""

    def __init__(self, pipeline_factory: Callable[[], Any]):
        self._pipeline_factory = pipeline_factory

    def validate(self, samples: list[EvaluationSample]) -> list[str]:
        errors: list[str] = []
        catalog_sizes: dict[tuple[str, int | str], int] = {}

        for sample in samples:
            if not sample.required_evidence_types:
                continue
            context = _request_context(sample)
            if not context.has_kb_permission(sample.kb_name, "read"):
                errors.append(f"{sample.id}: request context cannot read {sample.kb_name}")
                continue

            department_id = context.metadata.get("resource_department_id") or context.metadata.get(
                "department_id"
            )
            cache_key = (sample.kb_name, department_id or "")
            if cache_key not in catalog_sizes:
                try:
                    pipeline = self._pipeline_factory()
                    catalog = pipeline.agent.catalog_tool.scan(sample.kb_name, context) or {}
                    catalog_sizes[cache_key] = len(catalog.get("sources") or [])
                except Exception:
                    errors.append(f"{sample.id}: unable to scan sources for {sample.kb_name}")
                    continue

            if catalog_sizes[cache_key] == 0:
                errors.append(f"{sample.id}: no discoverable sources for {sample.kb_name}")

        return errors
