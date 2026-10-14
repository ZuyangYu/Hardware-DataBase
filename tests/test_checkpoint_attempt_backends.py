"""SQLite/PostgreSQL contracts, including a forced cross-process commit race."""

import multiprocessing
import os
import uuid
from contextlib import contextmanager

import pytest

from src.agents.checkpoints import (
    PostgresCheckpointerProvider, SqliteCheckpointerProvider, StaleAttemptCommit,
    attempt_checkpoint_ref, commit_attempt, forget_thread_state, prepare_attempt,
)
from src.agents.checkpoints.attempts import _store_for
from tests.test_agent_checkpoint_attempts import _build_app, _config


@pytest.mark.parametrize("snapshot_frequency", [1, 3, 1000])
def test_delta_history_survives_fork_commit_and_source_purge(checkpoint_backend, snapshot_frequency):
    from typing import Annotated, TypedDict
    from langgraph.channels.delta import DeltaChannel
    from langgraph.graph import END, START, StateGraph

    def append_batches(state, batches):
        return state + [value for batch in batches for value in batch]

    class State(TypedDict):
        log: Annotated[list[str], DeltaChannel(append_batches, snapshot_frequency=snapshot_frequency)]

    def build(saver):
        graph = StateGraph(State)
        graph.add_node("append", lambda state: {"log": ["node"]})
        graph.add_edge(START, "append")
        graph.add_edge("append", END)
        return graph.compile(checkpointer=saver)

    backend, location = checkpoint_backend
    visible = "delta-" + uuid.uuid4().hex
    options = {"pointer_path": _pointer_path(backend, location)}
    with _saver(backend, location) as saver:
        app = build(saver)
        app.invoke({"log": ["seed"]}, _config(visible))
        attempt = prepare_attempt(saver, visible, 1, **options)
        assert app.get_state(_config(attempt)).values["log"] == ["seed", "node"]
        app.invoke({"log": ["first"]}, _config(attempt))
        assert commit_attempt(saver, visible, attempt, 1, **options)
    with _saver(backend, location, setup=False) as other:
        app = build(other)
        next_attempt = prepare_attempt(other, visible, 2, **options)
        assert app.get_state(_config(next_attempt)).values["log"] == ["seed", "node", "first", "node"]
        app.invoke({"log": ["second"]}, _config(next_attempt))
        assert commit_attempt(other, visible, next_attempt, 2, **options)
        assert other.get_tuple(_config(attempt)) is None
        assert app.get_state(_config(visible)).values["log"] == ["seed", "node", "first", "node", "second", "node"]


@contextmanager
def _saver(backend, location, *, setup=True):
    provider = (PostgresCheckpointerProvider(location, setup=setup) if backend == "postgres"
                else SqliteCheckpointerProvider(location))
    try:
        saver = provider.create()
        if backend == "sqlite":
            saver.setup()
        yield saver
    finally:
        provider.close()


@pytest.fixture(params=["sqlite", "postgres"])
def checkpoint_backend(request, tmp_path):
    if request.param == "sqlite":
        yield "sqlite", str(tmp_path / "checkpoints.db")
        return
    dsn = os.environ.get("HDB_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("HDB_TEST_POSTGRES_DSN is not configured")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    schema = "checkpoint_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield "postgres", make_conninfo(dsn, options=f"-c search_path={schema},public")
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _pointer_path(backend, location):
    return location if backend == "sqlite" else None


def test_attempt_roundtrip_pending_writes_and_shared_pointer(checkpoint_backend):
    backend, location = checkpoint_backend
    visible = "contract-" + uuid.uuid4().hex
    options = {"pointer_path": _pointer_path(backend, location)}
    with _saver(backend, location) as saver:
        app = _build_app(saver)
        app.invoke({"log": ["seed"]}, _config(visible))
        source = saver.get_tuple(_config(visible))
        saver.put_writes(source.config, [("log", ["pending"])], task_id="pending-task")
        attempt = prepare_attempt(saver, visible, 1, **options)
        assert list(saver.get_tuple(_config(attempt)).pending_writes) == [("pending-task", "log", ["pending"])]
        app.invoke({"log": ["first"]}, _config(attempt))
        assert attempt_checkpoint_ref(saver, attempt, fencing_token=1)["backend"] == backend
        assert commit_attempt(saver, visible, attempt, 1, **options)
    # A fresh provider represents a different replica, without local pointer state.
    with _saver(backend, location, setup=False) as other:
        assert _store_for(other, options["pointer_path"]).pointer(visible)["fencing_token"] == 1
        next_attempt = prepare_attempt(other, visible, 2, **options)
        assert _build_app(other).get_state(_config(next_attempt)).values["log"][-2:] == ["first", "node"]
        assert commit_attempt(other, visible, next_attempt, 2, **options)
        assert other.get_tuple(_config(attempt)) is None
        with pytest.raises(StaleAttemptCommit):
            commit_attempt(other, visible, attempt, 1, **options)
        forget_thread_state(other, visible, **options)
        assert other.get_tuple(_config(visible)) is None
        assert _store_for(other, options["pointer_path"]).pointer(visible) is None


def _commit_process(backend, location, visible, token, entered, release, finished, results):
    from src.agents.checkpoints import attempts
    try:
        with _saver(backend, location, setup=False) as saver:
            original = attempts.copy_latest_checkpoint
            def paused_copy(*args, **kwargs):
                entered.set()
                if token == 1 and not release.wait(20):
                    raise TimeoutError("test did not release old commit")
                return original(*args, **kwargs)
            attempts.copy_latest_checkpoint = paused_copy
            result = commit_attempt(saver, visible, f"{visible}::attempt-{token}", token,
                                    pointer_path=_pointer_path(backend, location))
            results.put((token, result))
    except Exception as exc:
        results.put((token, type(exc).__name__, str(exc)))
    finally:
        finished.set()


def test_commit_serializes_copy_and_cas_across_processes(checkpoint_backend):
    backend, location = checkpoint_backend
    visible = "race-" + uuid.uuid4().hex
    options = {"pointer_path": _pointer_path(backend, location)}
    with _saver(backend, location) as saver:
        app = _build_app(saver)
        app.invoke({"log": ["seed"]}, _config(visible))
        for token in (1, 2):
            attempt = prepare_attempt(saver, visible, token, **options)
            app.invoke({"log": [f"attempt-{token}"]}, _config(attempt))
    ctx = multiprocessing.get_context("spawn")
    entered_old, entered_new, release = (ctx.Event() for _ in range(3))
    finished_old, finished_new = ctx.Event(), ctx.Event()
    results = ctx.Queue()
    old = ctx.Process(target=_commit_process, args=(backend, location, visible, 1, entered_old, release, finished_old, results))
    new = ctx.Process(target=_commit_process, args=(backend, location, visible, 2, entered_new, release, finished_new, results))
    try:
        old.start()
        assert entered_old.wait(20), "old process did not enter canonical copy"
        new.start()
        # The older writer holds the cross-process lock throughout its copy.
        assert not entered_new.wait(1), "new commit copied while old canonical copy was in progress"
        release.set()
        assert finished_old.wait(20)
        assert finished_new.wait(20)
        old.join(5)
        new.join(5)
        assert sorted([results.get(timeout=5), results.get(timeout=5)]) == [(1, True), (2, True)]
        with _saver(backend, location, setup=False) as saver:
            assert _store_for(saver, options["pointer_path"]).pointer(visible)["fencing_token"] == 2
            assert _build_app(saver).get_state(_config(visible)).values["log"][-2:] == ["attempt-2", "node"]
    finally:
        release.set()
        for process in (old, new):
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(5)
        results.close()
