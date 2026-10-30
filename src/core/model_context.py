"""Model-aware context-window metadata.

The configured ``AGENT_MODEL_MAX_INPUT_TOKENS`` is an upper-bound override, not
a claim about the model.  A small curated catalog prevents a 32k Ollama model
from inheriting a 64k default and compressing too late.  Unknown models keep the
configured value; configured zero still disables proactive compaction.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_CONTEXT_WINDOW = 32_768

# Conservative family-level entries.  Exact model names are matched first;
# prefixes cover versions/tags such as ``qwen2.5:32b`` or ``deepseek-chat``.
_MODEL_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("qwen2.5", 32_768),
    ("qwen2.5-coder", 32_768),
    ("qwen-plus", 131_072),
    ("qwen-max", 32_768),
    ("qwen3", 131_072),
    ("deepseek", 131_072),
    ("gpt-4o", 128_000),
    ("gpt-4.1", 128_000),
    ("claude", 200_000),
    ("llama-3", 131_072),
    ("llama3", 131_072),
)


@dataclass(frozen=True)
class ContextWindowResolution:
    window: int
    source: str
    catalog_window: int | None = None
    capped: bool = False


def _normalize_model(model: str) -> str:
    return str(model or "").strip().casefold()


def catalog_context_window(model: str) -> int | None:
    """Return the curated window for a model family, or ``None``."""

    normalized = _normalize_model(model)
    if not normalized:
        return None
    for prefix, window in _MODEL_CONTEXT_WINDOWS:
        if normalized.startswith(prefix):
            return window
    return None


def resolve_model_context_window(
    provider: str,
    model: str,
    configured: int,
) -> ContextWindowResolution:
    """Resolve the effective context window for one model.

    - ``configured == 0`` means "do not inject a profile" (legacy kill switch).
    - A known catalog value caps a larger configured value so a stale global
      default cannot disable compaction timing for a smaller model.
    - A smaller configured value is kept: deployments may deliberately reserve
      headroom for long tool results.
    """

    configured = max(0, int(configured or 0))
    catalog = catalog_context_window(model)
    if configured == 0:
        return ContextWindowResolution(0, "disabled", catalog, False)
    if catalog is None:
        return ContextWindowResolution(configured, "configured", None, False)
    if configured > catalog:
        return ContextWindowResolution(catalog, "model_catalog_cap", catalog, True)
    return ContextWindowResolution(configured, "configured", catalog, False)


def model_context_window(model: object, *, fallback: int = 0) -> int:
    """Read the window already applied to a chat model instance."""

    profile = getattr(model, "profile", None)
    if isinstance(profile, dict):
        value = profile.get("max_input_tokens")
        if isinstance(value, int) and value > 0:
            return value
    return max(0, int(fallback or 0))


__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "ContextWindowResolution",
    "catalog_context_window",
    "model_context_window",
    "resolve_model_context_window",
]
