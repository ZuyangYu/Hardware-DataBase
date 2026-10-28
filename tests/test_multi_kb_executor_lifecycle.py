"""R3 T0: multi-KB owns the existing pool without switching legacy tools."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

import src.settings as settings
from src.agents.tools import retrieval_executor as pools
from src.agents.tools.document_rag_tool import make_document_search_batch
from src.agents.tools.runtime import ToolRuntime


@pytest.fixture(autouse=True)
def isolate_pool():
    pools.reset_retrieval_executor(deadline_seconds=1)
    yield
    pools.reset_retrieval_executor(deadline_seconds=1)


@pytest.mark.parametrize("legacy,multi", [(False, False), (False, True), (True, False), (True, True)])
def test_switches_preserve_legacy_batch_path_and_share_one_pool(monkeypatch, legacy, multi):
    monkeypatch.setattr(settings, "SHARED_RETRIEVAL_EXECUTOR_ENABLED", legacy)
    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", multi, raising=False)
    pools.initialize_retrieval_executor()
    names = []

    class Backend:
        def retrieve(self, _kb, query, **_kwargs):
            names.append(threading.current_thread().name)
            return [SimpleNamespace(
                id=query, content=query, source_name="spec", backend="ragflow",
                retriever="vector", score=1.0, metadata={},
            )]

    result = make_document_search_batch(ToolRuntime(kb_name="A", ctx=None), Backend(), None)(["voltage"])
    assert "voltage" in result
    assert names[0].startswith("hdb-retrieval" if legacy else "hdb-document-search")
    if multi:
        pool = pools.get_multi_kb_retrieval_executor()
        assert pool is pools.get_retrieval_executor()
        name = pool.submit(lambda: threading.current_thread().name).result(timeout=1)
        assert name.startswith("hdb-retrieval")
    else:
        with pytest.raises(pools.MultiKnowledgeRetrievalUnavailable):
            pools.get_multi_kb_retrieval_executor()


def test_failed_initialization_stays_unavailable_until_successful_restart(monkeypatch):
    monkeypatch.setattr(settings, "SHARED_RETRIEVAL_EXECUTOR_ENABLED", False)
    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", True, raising=False)
    real_factory = pools.ThreadPoolExecutor

    def fail(**_kwargs):
        raise RuntimeError("pool unavailable")

    monkeypatch.setattr(pools, "ThreadPoolExecutor", fail)
    with pytest.raises(RuntimeError, match="pool unavailable"):
        pools.initialize_retrieval_executor()
    with pytest.raises(pools.MultiKnowledgeRetrievalUnavailable):
        pools.get_multi_kb_retrieval_executor()
    monkeypatch.setattr(pools, "ThreadPoolExecutor", real_factory)
    pools.initialize_retrieval_executor()
    assert pools.get_multi_kb_retrieval_executor().submit(lambda: "ready").result(timeout=1) == "ready"


def test_disabling_multi_kb_refuses_new_work_without_interrupting_running_call(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", True, raising=False)
    pools.initialize_retrieval_executor()
    pool = pools.get_multi_kb_retrieval_executor()
    release = threading.Event()
    started = threading.Event()

    def running():
        started.set()
        release.wait(timeout=2)
        return "finished"

    future = pool.submit(running)
    assert started.wait(timeout=1)
    try:
        monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", False)
        with pytest.raises(pools.MultiKnowledgeRetrievalUnavailable):
            pools.get_multi_kb_retrieval_executor()
        assert not future.done()
    finally:
        release.set()
    assert future.result(timeout=1) == "finished"


def test_shutdown_waits_to_deadline_and_restart_uses_new_pool(monkeypatch):
    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", True, raising=False)
    pools.initialize_retrieval_executor()
    first = pools.get_multi_kb_retrieval_executor()
    started, release = threading.Event(), threading.Event()

    def running():
        started.set()
        release.wait(timeout=2)

    future = first.submit(running)
    assert started.wait(timeout=1)
    try:
        assert pools.reset_retrieval_executor(deadline_seconds=0.01) == 1
        with pytest.raises(pools.MultiKnowledgeRetrievalUnavailable):
            pools.get_multi_kb_retrieval_executor()
        pools.initialize_retrieval_executor()
        assert pools.get_multi_kb_retrieval_executor() is not first
    finally:
        release.set()
        future.result(timeout=1)


def test_api_lifespan_initializes_and_closes_multi_kb_pool(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from src.api.app import lifespan

    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SHARED_RETRIEVAL_EXECUTOR_ENABLED", False)
    monkeypatch.setattr(settings, "RESOURCE_GOVERNOR_ENABLED", False)
    env_file = tmp_path / ".env"
    env_file.touch()
    monkeypatch.setattr(settings, "ENV_FILE_PATH", str(env_file))
    for flag in ("HDB_API_SPAWN_WORKER", "HDB_API_SPAWN_EXPORT_WORKER", "HDB_API_SPAWN_MEMORY_WORKER"):
        monkeypatch.setenv(flag, "0")

    async def run():
        async with lifespan(FastAPI()):
            pool = pools.get_multi_kb_retrieval_executor()
            assert pool.submit(lambda: "api").result(timeout=1) == "api"
        with pytest.raises(pools.MultiKnowledgeRetrievalUnavailable):
            pools.get_multi_kb_retrieval_executor()

    asyncio.run(run())


def test_worker_main_initializes_pool_before_running_and_closes_it(monkeypatch):
    from src.workers import main as worker_main

    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SHARED_RETRIEVAL_EXECUTOR_ENABLED", False)
    monkeypatch.setattr("sys.argv", ["worker", "--role", "chat"])
    monkeypatch.setattr(worker_main.signal, "signal", lambda *_args: None)
    seen = []

    class Worker:
        worker_id = "test-multi-kb"
        role = "chat"

        def __init__(self, **_kwargs):
            pass

        def stop(self, *_args):
            pass

        def run_forever(self):
            seen.append(pools.get_multi_kb_retrieval_executor().submit(lambda: "worker").result(timeout=1))

    monkeypatch.setattr(worker_main, "HardwareWorker", Worker)
    worker_main.main()
    assert seen == ["worker"]
    with pytest.raises(pools.MultiKnowledgeRetrievalUnavailable):
        pools.get_multi_kb_retrieval_executor()


def test_readiness_reports_enabled_pool_unavailable_then_ready(monkeypatch):
    from src.observability.health import _compute_ready

    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", True, raising=False)
    before = _compute_ready()
    assert before["dependencies"]["multi_kb_retrieval"]["status"] == "down"
    assert before["status"] == "not_ready"
    pools.initialize_retrieval_executor()
    assert _compute_ready()["dependencies"]["multi_kb_retrieval"]["status"] == "up"


def test_settings_reload_enables_multi_kb_without_changing_legacy_flag(monkeypatch):
    monkeypatch.setenv("MULTI_KB_CHAT_ENABLED", "true")
    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", False)
    settings.reload_settings()
    pools.initialize_retrieval_executor()
    assert settings.SHARED_RETRIEVAL_EXECUTOR_ENABLED is True
    assert pools.get_multi_kb_retrieval_executor().submit(lambda: "reloaded").result(timeout=1) == "reloaded"


def test_api_live_settings_owns_pool_initialization(monkeypatch, tmp_path):
    from src.core.app_pipeline import AppPipeline

    env_file = tmp_path / ".env"
    env_file.write_text("SHARED_RETRIEVAL_EXECUTOR_ENABLED=false\nMULTI_KB_CHAT_ENABLED=false\n")
    monkeypatch.setattr(settings, "ENV_FILE_PATH", str(env_file))
    monkeypatch.setenv("SHARED_RETRIEVAL_EXECUTOR_ENABLED", "false")
    monkeypatch.setenv("MULTI_KB_CHAT_ENABLED", "false")
    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", False)
    monkeypatch.setattr(settings, "SHARED_RETRIEVAL_EXECUTOR_ENABLED", False)
    AppPipeline.apply_settings({"MULTI_KB_CHAT_ENABLED": "true"})
    assert pools.get_multi_kb_retrieval_executor().submit(lambda: "live").result(timeout=1) == "live"
    assert settings.SHARED_RETRIEVAL_EXECUTOR_ENABLED is False
    AppPipeline.apply_settings({"MULTI_KB_CHAT_ENABLED": "false"})
    with pytest.raises(pools.MultiKnowledgeRetrievalUnavailable):
        pools.get_multi_kb_retrieval_executor()


def test_worker_reload_owns_pool_initialization(monkeypatch):
    from src.workers.main import HardwareWorker

    monkeypatch.setenv("MULTI_KB_CHAT_ENABLED", "true")
    monkeypatch.setattr(settings, "MULTI_KB_CHAT_ENABLED", False)
    worker = object.__new__(HardwareWorker)
    worker._env_mtime_ns = -2
    worker._reload_runtime_settings_if_changed()
    assert pools.get_multi_kb_retrieval_executor().submit(lambda: "live-worker").result(timeout=1) == "live-worker"
