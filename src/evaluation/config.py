from __future__ import annotations

import os
from dataclasses import dataclass


class EvaluationConfigurationError(ValueError):
    pass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(_env(name, str(default)))
    except ValueError as exc:
        raise EvaluationConfigurationError(f"{name} must be an integer") from exc
    if value < 1:
        raise EvaluationConfigurationError(f"{name} must be at least 1")
    return value


def _non_negative_int_env(name: str, default: int) -> int:
    try:
        value = int(_env(name, str(default)))
    except ValueError as exc:
        raise EvaluationConfigurationError(f"{name} must be an integer") from exc
    if value < 0:
        raise EvaluationConfigurationError(f"{name} must be at least 0")
    return value


def _fraction_env(name: str, default: float) -> float:
    try:
        value = float(_env(name, str(default)))
    except ValueError as exc:
        raise EvaluationConfigurationError(f"{name} must be a number between 0 and 1") from exc
    if not 0 < value < 1:
        raise EvaluationConfigurationError(f"{name} must be greater than 0 and less than 1")
    return value


def _ollama_v1_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    return base_url if base_url.endswith("/v1") else f"{base_url}/v1"


@dataclass(frozen=True)
class EvaluationConfig:
    llm_provider: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    llm_max_tokens: int = 4096
    max_contexts_per_sample: int = 4
    max_context_chars: int = 2000
    max_context_chars_per_item: int = 800
    scoring_max_budget_attempts: int = 1
    scoring_context_shrink_factor: float = 0.5
    timeout_seconds: int = 60
    max_workers: int = 4
    max_retries: int = 0
    output_root: str = "storage/evaluations"

    @classmethod
    def from_environment(cls) -> "EvaluationConfig":
        eval_provider = _env("EVAL_LLM_PROVIDER")
        provider = (eval_provider or _env("AGENT_LLM_PROVIDER", "ollama")).casefold()
        if provider not in {"ollama", "custom"}:
            raise EvaluationConfigurationError("EVAL_LLM_PROVIDER must be 'ollama' or 'custom'")

        if eval_provider:
            base_url = _env("EVAL_LLM_BASE_URL")
            api_key = _env("EVAL_LLM_API_KEY")
            model = _env("EVAL_LLM_MODEL")
        elif provider == "ollama":
            base_url = _env("AGENT_OLLAMA_BASE_URL", "http://localhost:11434")
            api_key = "ollama"
            model = _env("AGENT_OLLAMA_MODEL", "qwen2.5:32b")
        else:
            base_url = _env("AGENT_CUSTOM_BASE_URL")
            api_key = _env("AGENT_CUSTOM_API_KEY")
            model = _env("AGENT_CUSTOM_MODEL")

        if provider == "ollama":
            base_url = _ollama_v1_url(base_url)
            api_key = api_key or "ollama"
        if not base_url:
            raise EvaluationConfigurationError("evaluator LLM base URL is required")
        if not model:
            raise EvaluationConfigurationError("evaluator LLM model is required")

        embedding_base_url = _env("EVAL_EMBEDDING_BASE_URL")
        embedding_model = _env("EVAL_EMBEDDING_MODEL")
        if not embedding_base_url:
            raise EvaluationConfigurationError("EVAL_EMBEDDING_BASE_URL is required")
        if not embedding_model:
            raise EvaluationConfigurationError("EVAL_EMBEDDING_MODEL is required")
        llm_max_tokens = _positive_int_env("EVAL_LLM_MAX_TOKENS", 4096)
        max_contexts_per_sample = _positive_int_env("EVAL_MAX_CONTEXTS_PER_SAMPLE", 4)
        max_context_chars = _positive_int_env("EVAL_MAX_CONTEXT_CHARS", 2000)
        max_context_chars_per_item = _positive_int_env("EVAL_MAX_CONTEXT_CHARS_PER_ITEM", 800)
        scoring_max_budget_attempts = _positive_int_env("EVAL_SCORING_MAX_BUDGET_ATTEMPTS", 1)
        scoring_context_shrink_factor = _fraction_env("EVAL_SCORING_CONTEXT_SHRINK_FACTOR", 0.5)
        timeout_seconds = _positive_int_env("EVAL_TIMEOUT_SECONDS", 60)
        max_workers = _positive_int_env("EVAL_MAX_WORKERS", 4)
        max_retries = _non_negative_int_env("EVAL_MAX_RETRIES", 0)

        return cls(
            llm_provider=provider,
            llm_base_url=base_url,
            llm_api_key=api_key,
            llm_model=model,
            embedding_base_url=embedding_base_url,
            embedding_api_key=_env("EVAL_EMBEDDING_API_KEY"),
            embedding_model=embedding_model,
            llm_max_tokens=llm_max_tokens,
            max_contexts_per_sample=max_contexts_per_sample,
            max_context_chars=max_context_chars,
            max_context_chars_per_item=max_context_chars_per_item,
            scoring_max_budget_attempts=scoring_max_budget_attempts,
            scoring_context_shrink_factor=scoring_context_shrink_factor,
            timeout_seconds=timeout_seconds,
            max_workers=max_workers,
            max_retries=max_retries,
            output_root=_env("EVAL_OUTPUT_ROOT", "storage/evaluations"),
        )

    def public_metadata(self) -> dict[str, str | int | float]:
        return {
            "llm_provider": self.llm_provider,
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "embedding_base_url": self.embedding_base_url,
            "embedding_model": self.embedding_model,
            "llm_max_tokens": self.llm_max_tokens,
            "max_contexts_per_sample": self.max_contexts_per_sample,
            "max_context_chars": self.max_context_chars,
            "max_context_chars_per_item": self.max_context_chars_per_item,
            "scoring_max_budget_attempts": self.scoring_max_budget_attempts,
            "scoring_context_shrink_factor": self.scoring_context_shrink_factor,
            "timeout_seconds": self.timeout_seconds,
            "max_workers": self.max_workers,
            "max_retries": self.max_retries,
        }
