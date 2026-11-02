"""Agent assembly boundary.

This is the only module that calls ``create_deep_agent``.  The runner keeps
request orchestration, stream adaptation and domain events; the generic agent
composition lives here so middleware can be added without touching the loop.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from deepagents import create_deep_agent as default_create_deep_agent

from src.agents.middleware.session_context import SessionContextMiddleware


def build_agent(
    *,
    model: Any,
    tools: Sequence[Any],
    system_prompt: str,
    checkpointer: Any = None,
    subagents: Sequence[Any] | None = None,
    session_state_text: str = "",
    middleware: Sequence[Any] | None = None,
    factory: Callable[..., Any] = default_create_deep_agent,
) -> Any:
    """Compose the generic agent graph.

    ``factory`` is injectable so tests (and alternate SDK pins) can replace the
    deepagents constructor without patching this module's globals.  The runner
    passes its module-level ``create_deep_agent`` symbol, preserving the
    existing test seam.
    """

    stack = list(middleware or [])
    if session_state_text:
        # Innermost: append after any other middleware that rewrites the
        # system message, so the continuity block is never lost.
        stack.append(SessionContextMiddleware(session_state_text))
    kwargs: dict[str, Any] = {
        "model": model,
        "tools": list(tools),
        "system_prompt": system_prompt,
        "checkpointer": checkpointer,
    }
    if stack:
        kwargs["middleware"] = stack
    if subagents:
        kwargs["subagents"] = list(subagents)
    return factory(**kwargs)


__all__ = ["build_agent"]
