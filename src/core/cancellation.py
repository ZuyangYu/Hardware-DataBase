from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class QueryCancelled(Exception):
    """Signal that a user cancelled an in-flight query.

    This is deliberately separate from ordinary tool failures so the turn
    worker can finish the turn as cancelled instead of recording a retrieval
    error or generating a partial answer.
    """


_CURRENT_CANCEL_CHECK: ContextVar[Callable[[], bool] | None] = ContextVar(
    "hdb_query_cancel_check", default=None
)


def current_cancel_check() -> Callable[[], bool] | None:
    """Return the cancel predicate bound to the current execution context."""
    return _CURRENT_CANCEL_CHECK.get()


@contextmanager
def bind_cancel_check(check: Callable[[], bool] | None) -> Iterator[None]:
    """Bind a turn's cancel predicate so model adapters can poll it.

    The agent runner binds the predicate around ``agent.stream``.  The model
    adapter then checks it between stream chunks (and while waiting for a
    chunk), which lets a Stop request release the LLM governor slot without
    waiting for the provider to finish generating tokens nobody will read.
    """
    token = _CURRENT_CANCEL_CHECK.set(check)
    try:
        yield
    finally:
        _CURRENT_CANCEL_CHECK.reset(token)
