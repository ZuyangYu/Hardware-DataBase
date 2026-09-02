from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable
from typing import Any

import httpx

from .access import build_evaluation_context
from .config import EvaluationConfig
from .schemas import EvaluationSample


_PING_TIMEOUT = 20.0
logger = logging.getLogger(__name__)


class EvaluationPreflight:
    """Validate scoped, evidence-required samples before online collection."""

    def __init__(self, pipeline_factory: Callable[[], Any]):
        self._pipeline_factory = pipeline_factory

    def validate(
        self,
        samples: list[EvaluationSample],
        *,
        scan_sources: bool = True,
    ) -> list[str]:
        """Validate sample contexts and, for online runs, source discovery.

        Offline scoring reuses an already-collected snapshot and must not touch
        the live knowledge-base catalog.  ``scan_sources=False`` retains the
        same context/negative-access checks while making that boundary explicit.
        """
        errors: list[str] = []
        catalog_sizes: dict[tuple[str, int | str], int] = {}
        catalog_failures: set[tuple[str, int | str]] = set()

        for sample in samples:
            context = build_evaluation_context(sample)
            if sample.expected_access == "denied":
                if sample.required_evidence_types or sample.metrics:
                    errors.append(
                        f"{sample.id}: expected_access=denied 不能声明正常检索证据或评分指标"
                    )
                    continue
                if context.has_kb_permission(sample.kb_name, "read"):
                    errors.append(
                        f"{sample.id}: expected_access=denied 的上下文仍具有 {sample.kb_name} 读取权限"
                    )
                continue
            if not sample.required_evidence_types:
                continue
            if not context.has_kb_permission(sample.kb_name, "read"):
                errors.append(f"{sample.id}: request context cannot read {sample.kb_name}")
                continue
            if not scan_sources:
                continue

            department_id = context.metadata.get("resource_department_id") or context.metadata.get(
                "department_id"
            )
            cache_key = (sample.kb_name, department_id or "")
            if cache_key in catalog_failures:
                errors.append(f"{sample.id}: unable to scan sources for {sample.kb_name}")
                continue
            if cache_key not in catalog_sizes:
                try:
                    pipeline = self._pipeline_factory()
                    scanner = getattr(pipeline, "scan_kb_sources", None)
                    if not callable(scanner):
                        raise AttributeError("pipeline does not expose scan_kb_sources")
                    catalog = scanner(sample.kb_name, context) or {}
                    catalog_sizes[cache_key] = len(catalog.get("sources") or [])
                except Exception as exc:
                    catalog_failures.add(cache_key)
                    logger.error(
                        "evaluation preflight source scan failed for kb=%s department=%s (%s)",
                        sample.kb_name,
                        department_id or "",
                        type(exc).__name__,
                    )
                    errors.append(f"{sample.id}: unable to scan sources for {sample.kb_name}")
                    continue

            if catalog_sizes[cache_key] == 0:
                errors.append(f"{sample.id}: no discoverable sources for {sample.kb_name}")

        return errors

    @staticmethod
    def validate_scoring(config: EvaluationConfig | None = None) -> list[str]:
        """Validate native RAGAS dependencies and evaluator configuration.

        The Streamlit page performs an early check for convenience, but runs
        can also be started through the controller/API. Keep this check in the
        worker path so a dependency or configuration problem becomes an
        explicit failed run instead of a batch of misleading zero scores.
        """

        errors: list[str] = []
        required_modules = ("ragas", "openai", "langchain_openai")
        missing = [
            module
            for module in required_modules
            if importlib.util.find_spec(module) is None
        ]
        if missing:
            errors.append(
                "评分依赖缺失："
                + ", ".join(missing)
                + "；请运行 uv sync --group eval"
            )
            return errors

        try:
            if config is None:
                config = EvaluationConfig.from_environment()
        except Exception as exc:
            errors.append(f"评估配置无效：{exc}")
            return errors
        errors.extend(_ping_endpoints(config))
        return errors


def _ping_endpoints(config: EvaluationConfig) -> list[str]:
    """Probe the judge LLM and embeddings endpoints with a minimal request.

    A run that starts against a quota-exhausted or misconfigured evaluator
    would otherwise burn the whole collection/scoring window producing
    batches of failed metrics.
    """

    errors: list[str] = []
    headers_llm = {}
    if config.llm_api_key:
        headers_llm["Authorization"] = f"Bearer {config.llm_api_key}"
    try:
        response = httpx.post(
            config.llm_base_url.rstrip("/") + "/chat/completions",
            headers=headers_llm,
            json={
                "model": config.llm_model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
            timeout=_PING_TIMEOUT,
        )
        if response.status_code != 200:
            errors.append(
                f"裁判 LLM 预检失败：HTTP {response.status_code} "
                f"{response.text[:120]}（{config.llm_model} @ {config.llm_base_url}）"
            )
    except Exception as exc:
        errors.append(f"裁判 LLM 预检不可达：{type(exc).__name__}: {exc}")

    headers_emb = {}
    if config.embedding_api_key:
        headers_emb["Authorization"] = f"Bearer {config.embedding_api_key}"
    try:
        response = httpx.post(
            config.embedding_base_url.rstrip("/") + "/embeddings",
            headers=headers_emb,
            json={"model": config.embedding_model, "input": ["ping"]},
            timeout=_PING_TIMEOUT,
        )
        if response.status_code != 200:
            errors.append(
                f"Embeddings 预检失败：HTTP {response.status_code} "
                f"{response.text[:120]}（{config.embedding_model} @ {config.embedding_base_url}）"
            )
    except Exception as exc:
        errors.append(f"Embeddings 预检不可达：{type(exc).__name__}: {exc}")
    return errors
