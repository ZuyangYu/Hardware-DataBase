from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from functools import wraps


@dataclass(frozen=True)
class CallContext:
    task_type: str = "unattributed"
    task_id: str = ""
    owner_user_id: int | None = None
    department_id: int | None = None
    project_id: str = ""
    execution_attempt_id: str = ""
    evaluation_run_id: str = ""
    sample_id: str = ""
    purpose: str = "unattributed"
    operation: str = "generate"
    profile: str = "unknown"
    turn_id: str = ""


_CONTEXT = ContextVar("llm_cost_context", default=CallContext())
_ACTIVE = ContextVar("llm_cost_active", default=False)


def current_context() -> dict:
    return asdict(_CONTEXT.get())


def update_context(**fields):
    """Use inside a call_context scope, which restores the caller on exit."""
    _CONTEXT.set(replace(_CONTEXT.get(), **fields))


def numeric_identity(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


@contextmanager
def runtime_scope(*, operation: str, profile: str):
    current = _CONTEXT.get()
    purpose = current.purpose
    if purpose == "unattributed":
        purpose = next((name for name in ("authoring", "memory", "ingestion", "chat", "quality_analysis") if name in profile or name in operation), "unattributed")
    with call_context(operation=operation, profile=profile, purpose=purpose):
        yield


@contextmanager
def call_context(**fields):
    token = _CONTEXT.set(replace(_CONTEXT.get(), **fields))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def attributed(purpose: str, task_type: str, task_key: str):
    """Bind a worker's known key without relying on process-global last usage."""
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            user = kwargs.get("user")
            with call_context(purpose=purpose, task_type=task_type, task_id=str(kwargs.get(task_key, "")),
                              turn_id=str(kwargs.get(task_key, "")) if task_type == "chat" else "",
                              owner_user_id=getattr(user, "id", None), department_id=getattr(user, "department_id", None)):
                return fn(*args, **kwargs)
        return run
    return decorate
