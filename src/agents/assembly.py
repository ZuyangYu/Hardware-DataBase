"""Agent assembly boundary.

This is the only module that calls ``create_deep_agent``.  The runner keeps
request orchestration, stream adaptation and domain events; the generic agent
composition lives here so middleware can be added without touching the loop.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from deepagents import create_deep_agent as default_create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemPermission

from src.agents.middleware.session_context import SessionContextMiddleware
from src.agents.middleware.skill_read import SkillReadGateMiddleware


def build_agent(
    *,
    model: Any,
    tools: Sequence[Any],
    system_prompt: str,
    checkpointer: Any = None,
    subagents: Sequence[Any] | None = None,
    session_state_text: str = "",
    skill_root: str | None = None,
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
    # The official SkillsMiddleware reads SKILL.md through read_file.  Keep
    # that tool only on turns with a materialized, read-only skill root.
    stack.append(SkillReadGateMiddleware(enabled=bool(skill_root)))
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
    if skill_root:
        # Official progressive disclosure: only SKILL.md metadata is in the
        # prompt; the model reads full instructions on demand.  Writes are
        # denied so the agent cannot mutate the reviewed skill source.
        kwargs["skills"] = ["/"]
        kwargs["backend"] = FilesystemBackend(
            root_dir=skill_root, virtual_mode=True
        )
        kwargs["permissions"] = [
            FilesystemPermission(
                operations=["write"], paths=["/**"], mode="deny"
            )
        ]
    if stack:
        kwargs["middleware"] = stack
    if subagents:
        kwargs["subagents"] = list(subagents)
    return factory(**kwargs)


__all__ = ["build_agent"]
