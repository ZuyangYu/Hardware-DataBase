from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

import src.settings as settings
from src.api.deps import current_user
from src.api.routes import costs as routes
from src.core.auth import AuthUser
from src.costs.capture import leaf_call
from src.costs.context import call_context, current_context
from src.costs.pricing import PriceVersion, price
from src.costs.sink import CostSink
from src.costs.store import CostConflict, CostStore
from src.costs.usage import normalize
from src.quality.store import sha256

DSN = os.getenv("HDB_TEST_POSTGRES_DSN", "")


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        yield CostStore(str(tmp_path / "cost.db"))
        return
    if not DSN:
        pytest.skip("HDB_TEST_POSTGRES_DSN is not configured")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    values = conninfo_to_dict(DSN)
    name = values["dbname"] = "cost_contract_" + uuid.uuid4().hex[:10]
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        monkeypatch.setattr(settings, "CONTROL_PLANE_BACKEND", "postgres")
        monkeypatch.setattr(settings, "CONTROL_PLANE_DATABASE_URL", make_conninfo(**values))
        yield CostStore(initialize=True)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def fixture_price(**changes):
    return PriceVersion(**{**dict(price_version="fixture-v1", endpoint_alias="fixture", provider="openai-compatible", requested_model="requested", response_model="response-v1",
        effective_from="2026-01-01T00:00:00Z", effective_to="2027-01-01T00:00:00Z", currency="USD", input_per_million="1", cached_input_per_million="0.1", output_per_million="4"), **changes})


def fixture_event(**changes):
    event = {"invocation_id": "call-fixture", "sequence": 1, "event_id": "call-fixture:1", "task_type": "chat", "task_id": "turn-fixture",
             "owner_user_id": 1, "department_id": 10, "purpose": "chat", "attempt_visibility": "logical_only",
             "provider": "openai-compatible", "endpoint_alias": "fixture", "requested_model": "requested", "response_model": "response-v1",
             "started_at": "2026-10-08T01:00:00+00:00", "finished_at": "2026-10-08T01:00:01+00:00", "status": "success",
             "input_tokens": 10000, "output_tokens": 1000, "cached_input_tokens": 8000, "cache_write_tokens": 0, "reasoning_tokens": 400, "usage_status": "reported"}
    event.update(changes)
    event["event_id"] = f"{event['invocation_id']}:{event['sequence']}"
    return event


def test_cache_and_reasoning_are_not_double_charged():
    assert Decimal(price(fixture_event(), fixture_price())["cost_amount"]) == Decimal("0.0068")
    assert price(fixture_event(usage_status="missing"), fixture_price())["cost_amount"] is None
    assert price(fixture_event(), None)["cost_status"] == "unknown_price"
    assert price(fixture_event(cache_write_tokens=5), fixture_price())["cost_status"] == "unknown_price"
    assert price(fixture_event(cached_input_tokens=10001), fixture_price())["cost_amount"] is None
    assert price(fixture_event(input_tokens=0, output_tokens=0, cached_input_tokens=0, reasoning_tokens=0), fixture_price())["cost_amount"] == "0.0"


@pytest.mark.parametrize("raw,status,expected", [
    ({}, "missing", (None, None)),
    ({"usage": {"prompt_tokens": 0, "completion_tokens": 0}}, "reported", (0, 0)),
    ({"usage": {"prompt_tokens": 10}}, "partial", (10, None)),
    ({"usage": {"prompt_tokens": float('nan'), "completion_tokens": 10}}, "partial", (None, 10)),
    ({"usage": {"prompt_tokens": True, "completion_tokens": -1}}, "missing", (None, None)),
    ({"usage": {"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 11}}}, "partial", (10, 1)),
    ({"usage_metadata": {"input_tokens": 10, "output_tokens": 1, "input_token_details": {"cache_creation": "5"}}}, "partial", (10, 1)),
    ({"usage": {"prompt_tokens": 10, "completion_tokens": 1, "completion_tokens_details": {"reasoning_tokens": True}}}, "partial", (10, 1)),
])
def test_usage_zero_missing_invalid_and_partial(raw, status, expected):
    result = normalize(raw)
    assert result.usage_status == status
    assert (result.input_tokens, result.output_tokens) == expected


def test_exact_pricing_idempotent_events_reordering_and_unknowns(store):
    store.add_price(fixture_price())
    terminal = fixture_event()
    with ThreadPoolExecutor(max_workers=4) as pool:
        inserted = list(pool.map(lambda _: store.ingest(terminal), range(8)))
    assert sum(inserted) == 1
    store.ingest(fixture_event(sequence=0, status="started", usage_status="missing"))
    admin = AuthUser(4, "admin", "system_admin", True)
    assert store.summary(admin)["known_amounts"] == {"USD": "0.0068"}
    assert store.summary(admin)["calls"] == 1
    assert store.invocations(admin)[0]["status"] == "success"
    with pytest.raises(CostConflict):
        store.ingest(fixture_event(output_tokens=2000))
    store.ingest(fixture_event(invocation_id="call-missing", usage_status="missing", input_tokens=None, output_tokens=None))
    store.ingest(fixture_event(invocation_id="call-other-model", response_model="unregistered-version"))
    total = store.summary(admin)
    assert total["unknown_cost_calls"] == 2
    assert total["usage_coverage"] == 2 / 3
    assert total["price_coverage"] == 1 / 3
    assert total["known_amounts"] == {"USD": "0.0068"}


def test_price_versions_overlap_and_history_are_immutable(store):
    version = fixture_price()
    store.add_price(version)
    store.add_price(version)
    with pytest.raises(CostConflict):
        store.add_price(fixture_price(output_per_million="10"))
    with pytest.raises(CostConflict):
        store.add_price(fixture_price(price_version="overlap"))
    store.ingest(fixture_event())
    store.add_price(fixture_price(price_version="later", effective_from="2027-01-01T00:00:00Z", effective_to=None, input_per_million="10"))
    assert store.invocations(AuthUser(4, "admin", "system_admin", True))[0]["price_version"] == "fixture-v1"


def test_time_scope_csv_and_task_access_match(store):
    store.add_price(fixture_price())
    store.ingest(fixture_event())
    store.ingest(fixture_event(invocation_id="other-dept", owner_user_id=2, department_id=20, task_id="hidden-task", purpose="evaluation_judge"))
    store.ingest(fixture_event(invocation_id="formula", task_id="=formula"))
    viewer = [AuthUser(1, "owner", "employee", True, 10)]
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[current_user] = lambda: viewer[0]
    app.dependency_overrides[routes._store] = lambda: store
    with TestClient(app) as client:
        assert client.get('/api/v1/costs/summary').json()["calls"] == 2
        csv = client.get('/api/v1/costs/export')
        assert csv.status_code == 200 and "hidden-task" not in csv.text and "'=formula" in csv.text
        assert client.get('/api/v1/costs/tasks/chat/hidden-task').status_code == 404
        assert client.get('/api/v1/costs/invocations?purpose=evaluation_judge').json() == []
        assert client.get('/api/v1/costs/summary?start=2026-11-01T00:00:00Z').json()["calls"] == 0
        assert client.get('/api/v1/costs/summary?start=2026-11-01').status_code == 422
        viewer[0] = AuthUser(4, "admin", "system_admin", True)
        assert client.get('/api/v1/costs/summary').json()["calls"] == 3


def test_no_data_coverage_is_unknown(store):
    summary = store.summary(AuthUser(4, "admin", "system_admin", True))
    assert summary["usage_coverage"] is None and summary["price_coverage"] is None


def test_cost_trends_exact_currency_ranking_time_buckets_and_filtered_scope(store):
    from collections import defaultdict

    store.add_price(fixture_price())
    store.add_price(fixture_price(price_version="eur-price", requested_model="eur-model", currency="EUR"))
    store.ingest(fixture_event(model_alias="fixture-model", project_id="project-a"))
    store.ingest(fixture_event(invocation_id="usd-day-two", model_alias="fixture-model", project_id="project-a", started_at="2026-10-09T01:00:00+00:00"))
    store.ingest(fixture_event(invocation_id="unknown", model_alias="fixture-model", project_id="project-a", response_model="unknown"))
    store.ingest(fixture_event(invocation_id="eur", model_alias="eur-model", requested_model="eur-model", project_id="project-a"))
    store.ingest(fixture_event(invocation_id="other", owner_user_id=2, department_id=20, project_id="project-b"))
    employee = AuthUser(1, "owner", "employee", True, 10)
    trends = store.trends(employee, project_id="project-a")
    assert trends["summary"]["calls"] == 4
    assert trends["summary"]["unknown_cost_calls"] == 1
    assert len(trends["buckets"]) == 2
    amounts = defaultdict(Decimal)
    for bucket in trends["buckets"]:
        for currency, amount in bucket["known_amounts"].items():
            amounts[currency] += Decimal(amount)
    assert dict(amounts) == {"USD": Decimal("0.0136"), "EUR": Decimal("0.0068")}
    assert {k: str(v) for k, v in amounts.items()} == store.summary(employee, project_id="project-a")["known_amounts"]
    assert set(trends["rankings_by_currency"]) == {"USD", "EUR"}
    assert store.trends(employee, department_id=20)["summary"]["calls"] == 0
    assert store.summary(employee, model_alias="eur-model")["calls"] == 1
    assert store.summary(employee, endpoint_alias="unregistered")["calls"] == 0
    assert store.trends(employee, end="2026-10-09T00:00:00+00:00")["summary"]["calls"] == 3


def test_versioned_cost_budget_never_claims_complete_spend_from_unknowns(store):
    from datetime import datetime, timezone
    from src.costs.budgets import BudgetVersion

    admin = AuthUser(4, "admin", "system_admin", True)
    employee = AuthUser(1, "owner", "employee", True, 10)
    first = BudgetVersion(budget_version="fixture-budget", department_id=10, currency="USD", amount_limit="0.005",
                          effective_from="2026-10-08T00:00:00Z", effective_to="2026-10-09T00:00:00Z")
    with pytest.raises(PermissionError):
        store.add_budget(employee, first)
    store.add_budget(admin, first)
    store.add_budget(admin, first)
    with pytest.raises(CostConflict, match="immutable"):
        store.add_budget(admin, first.model_copy(update={"amount_limit": Decimal("1")}))
    with pytest.raises(CostConflict, match="overlapping"):
        store.add_budget(admin, first.model_copy(update={"budget_version": "another-version"}))
    store.add_budget(admin, first.model_copy(update={"budget_version": "global-budget", "department_id": None}))
    store.add_budget(admin, first.model_copy(update={"budget_version": "judge-budget", "purpose": "evaluation_judge", "amount_limit": Decimal("1")}))
    store.add_price(fixture_price())
    store.ingest(fixture_event())
    store.ingest(fixture_event(invocation_id="cross-owned", department_id=20))
    store.ingest(fixture_event(invocation_id="unknown-budget-cost", purpose="evaluation_judge", usage_status="missing", input_tokens=None, output_tokens=None))
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    statuses = {b["policy"]["budget_version"]: b for b in store.budget_statuses(employee, now=now)}
    assert "global-budget" not in statuses
    assert statuses["fixture-budget"]["status"] == "exceeded_known_amount"
    assert statuses["fixture-budget"]["known_amount"] == "0.0068"
    assert statuses["fixture-budget"]["summary"]["calls"] == 2
    assert statuses["judge-budget"]["status"] == "insufficient_cost_coverage"
    assert all(b["completeness"] == "recorded_calls_only_not_proven" for b in statuses.values())
    assert store.budget_statuses(AuthUser(9, "no-dept", "employee", True), now=now) == []
    assert store.budget_statuses(AuthUser(3, "other", "employee", True, 20), now=now) == []
    assert store.budget_statuses(employee, now=datetime(2026, 10, 9, tzinfo=timezone.utc), active_only=True) == []


def test_context_isolated_async_tasks_and_nested_leaf_dedup(monkeypatch):
    events = []
    import src.costs.capture as capture

    monkeypatch.setattr(settings, "COST_LEDGER_ENABLED", True)
    monkeypatch.setattr(capture, "get_sink", lambda: SimpleNamespace(emit=lambda event: events.append(event)))
    model = SimpleNamespace(hdb_model="requested", hdb_provider="openai-compatible", openai_api_base="https://private.test")
    registry_key = sha256(["openai-compatible", "https://private.test", "requested"])
    monkeypatch.setattr(settings, "COST_MODEL_REGISTRY", {registry_key: {"endpoint_alias": "fixture", "model_alias": "fixture-model"}})
    async def run(task_id):
        with call_context(task_type="chat", task_id=task_id, purpose="chat", owner_user_id=1, department_id=10):
            await asyncio.sleep(0)
            with leaf_call(model) as leaf:
                with leaf_call(model) as nested:
                    assert nested is None
                leaf.response = AIMessage(content="Private answer", usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
        assert current_context()["task_type"] == "unattributed"
    async def all_tasks():
        await asyncio.gather(run("task-1"), run("task-2"))
    asyncio.run(all_tasks())
    assert len(events) == 4
    assert {e["task_id"] for e in events} == {"task-1", "task-2"}
    assert all(e["endpoint_alias"] == "fixture" for e in events)
    assert "Private answer" not in json.dumps(events) and "private.test" not in json.dumps(events)


def test_memory_context_without_provider_call_does_not_create_cost_event(monkeypatch):
    events = []
    import src.costs.capture as capture

    monkeypatch.setattr(settings, "COST_LEDGER_ENABLED", True)
    monkeypatch.setattr(capture, "get_sink", lambda: SimpleNamespace(emit=lambda event: events.append(event)))
    model = SimpleNamespace(hdb_model="memory-model", hdb_provider="openai-compatible", openai_api_base="https://memory.test")

    with call_context(task_type="memory", task_id="memory-job", purpose="memory", owner_user_id=1, department_id=10):
        # Memory catalog reads/writes and job bookkeeping carry context only;
        # they must not become a model-cost row without a provider leaf call.
        pass
    assert events == []

    with call_context(task_type="memory", task_id="memory-job", purpose="memory", owner_user_id=1, department_id=10):
        with leaf_call(model) as call:
            call.response = AIMessage(content="extracted", usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
    assert events and events[-1]["purpose"] == "memory"


def test_stream_final_usage_once_and_cancellation_preserves_usage(monkeypatch):
    events = []
    import src.costs.capture as capture

    monkeypatch.setattr(settings, "COST_LEDGER_ENABLED", True)
    monkeypatch.setattr(capture, "get_sink", lambda: SimpleNamespace(emit=lambda e: events.append(e)))
    model = SimpleNamespace(model="requested")
    with leaf_call(model) as call:
        call.chunk(AIMessageChunk(content="a"))
        call.chunk(AIMessageChunk(content="b"))
        call.chunk(AIMessageChunk(content="", usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}))
    assert len(events) == 2 and events[-1]["input_tokens"] == 10 and events[-1]["output_tokens"] == 2
    with pytest.raises(GeneratorExit):
        with leaf_call(model) as call:
            call.chunk(AIMessageChunk(content="a", usage_metadata={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}))
            raise GeneratorExit()
    assert events[-1]["status"] == "cancelled" and events[-1]["input_tokens"] == 5
    with leaf_call(model) as call:
        call.chunk({"usage_metadata": {"input_tokens": 5, "output_tokens": 1, "input_token_details": {"cache_creation": "invalid"}}})
        call.chunk({"usage_metadata": {"input_tokens": 0, "output_tokens": 1}})
    assert events[-1]["usage_status"] == "partial"


def test_real_gateway_sync_async_stream_and_structured_boundary(monkeypatch):
    from src.core.model_gateway import build_governed_model
    from langchain_openai import ChatOpenAI
    import src.costs.capture as capture

    events = []
    monkeypatch.setattr(settings, "COST_LEDGER_ENABLED", True)
    monkeypatch.setattr(capture, "get_sink", lambda: SimpleNamespace(emit=lambda e: events.append(e)))
    response = ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok", usage_metadata={"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}))])
    monkeypatch.setattr(ChatOpenAI, "_generate", lambda *_a, **_k: response)
    async def generate(*_a, **_k):
        return response
    monkeypatch.setattr(ChatOpenAI, "_agenerate", generate)
    chunk = ChatGenerationChunk(message=AIMessageChunk(content="ok", usage_metadata={"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}))
    monkeypatch.setattr(ChatOpenAI, "_stream", lambda *_a, **_k: iter([chunk]))
    async def stream(*_a, **_k):
        yield chunk
    monkeypatch.setattr(ChatOpenAI, "_astream", stream)
    model = build_governed_model(provider="custom", model="requested", api_key="fixture-key")
    model.invoke([HumanMessage(content="private")])
    list(model.stream([HumanMessage(content="private")]))
    asyncio.run(model.ainvoke([HumanMessage(content="private")]))
    async def collect():
        return [item async for item in model.astream([HumanMessage(content="private")])]
    asyncio.run(collect())
    assert len(events) == 8
    assert all(e["input_tokens"] == 3 for e in events if e["sequence"] == 1)


def test_native_openai_judge_reasks_each_recorded(monkeypatch):
    from openai import AsyncOpenAI
    from src.costs.sdk import instrument_async_openai
    import src.costs.capture as capture

    events = []
    monkeypatch.setattr(settings, "COST_LEDGER_ENABLED", True)
    monkeypatch.setattr(capture, "get_sink", lambda: SimpleNamespace(emit=lambda e: events.append(e)))
    async def handler(request):
        return httpx.Response(200, json={"id": "request-1", "object": "chat.completion", "created": 1, "model": "response-v1", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = instrument_async_openai(AsyncOpenAI(api_key="fixture", base_url="https://fixture.test/v1", http_client=http))
            for _ in range(2):
                await client.chat.completions.create(model="requested", messages=[{"role": "user", "content": "private"}])
    asyncio.run(run())
    assert len(events) == 4
    assert all(e["purpose"] == "evaluation_judge" for e in events)
    assert len({e["invocation_id"] for e in events}) == 2
    assert events[-1]["input_tokens"] == 10 and events[-1]["response_model"] == "response-v1"


def test_sdk_hidden_http_retries_remain_one_logical_call_with_explicit_visibility(monkeypatch):
    from openai import AsyncOpenAI
    from src.costs.sdk import instrument_async_openai
    import src.costs.capture as capture

    events, requests = [], []
    monkeypatch.setattr(settings, "COST_LEDGER_ENABLED", True)
    monkeypatch.setattr(capture, "get_sink", lambda: SimpleNamespace(emit=events.append))
    async def handler(request):
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(429, headers={"retry-after-ms": "1"}, json={"error": {"message": "fixture limit", "type": "rate_limit"}})
        return httpx.Response(200, json={"id": "fixture-request", "object": "chat.completion", "created": 1, "model": "response-v1",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with instrument_async_openai(AsyncOpenAI(api_key="fixture", base_url="https://fixture.test/v1", http_client=http, max_retries=2)) as client:
                await client.chat.completions.create(model="requested", messages=[{"role": "user", "content": "fixture"}])
    asyncio.run(run())
    assert len(requests) == 3
    assert len(events) == 2 and len({e["invocation_id"] for e in events}) == 1
    assert events[-1]["attempt_visibility"] == "logical_only" and events[-1]["provider_attempt_no"] is None
    assert events[-1]["input_tokens"] == 10


def test_spool_db_outage_replay_restart_and_corruption(tmp_path):
    store = CostStore(str(tmp_path / 'cost.db'))
    offline = [True]
    def factory():
        if offline[0]:
            raise ConnectionError("fixture outage")
        return store
    spool = tmp_path / "spool"
    sink = CostSink(factory, spool=spool)
    sink.emit(fixture_event())
    sink.queue.join()
    sink.close()
    assert len(list(spool.glob('*.json'))) == 1
    assert sink.snapshot()["write_failures"] > 0 and sink.snapshot()["dropped"] == 0
    (spool / "bad.json").write_text('{broken')
    offline[0] = False
    restored = CostSink(factory, spool=spool)
    deadline = time.monotonic() + 5
    while not list(spool.glob('*.corrupt')) or list(spool.glob('*.json')):
        if time.monotonic() > deadline:
            raise AssertionError('spool not replayed')
        time.sleep(.01)
    restored.emit(fixture_event())
    restored.queue.join()
    restored.close()
    assert store.summary(AuthUser(4, "admin", "system_admin", True))["calls"] == 1


def test_spool_full_and_queue_full_signal_loss_without_blocking(tmp_path):
    def offline():
        raise ConnectionError('offline')
    sink = CostSink(offline, spool=tmp_path / 'full', max_bytes=1, max_events=1)
    before = time.monotonic()
    for n in range(100):
        sink.emit(fixture_event(invocation_id=f'full-{n}'))
    assert time.monotonic() - before < 1
    sink.queue.join()
    sink.close()
    stats = sink.snapshot()
    assert stats['dropped'] > 0 and stats['spool_full'] > 0


def test_async_governor_cancelled_acquisition_releases_the_same_context():
    import threading
    from contextlib import contextmanager
    from contextvars import ContextVar
    from src.core.model_gateway import _async_slot

    variable = ContextVar('fixture-governor', default=False)
    entered, release = threading.Event(), threading.Event()
    exited = []
    @contextmanager
    def slot():
        token = variable.set(True)
        entered.set()
        release.wait(5)
        try:
            yield
        finally:
            variable.reset(token)
            exited.append(True)
    async def run():
        async with _async_slot(slot(), 'batch'):
            raise AssertionError('cancelled task should not enter the provider body')
    async def scenario():
        task = asyncio.create_task(run())
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert exited == [True] and not variable.get()


def test_restart_automatically_adopts_orphan_spool_without_double_charge(tmp_path):
    store = CostStore(str(tmp_path / 'cost.db'))
    root = tmp_path / 'spools'
    old = CostSink(lambda: (_ for _ in ()).throw(ConnectionError('offline')), spool=root/'old-process')
    old.emit(fixture_event())
    old.queue.join()
    old.close()
    assert len(list((root/'old-process').glob('*.json'))) == 1
    new = CostSink(lambda: store, spool=root/'new-process', replay_root=root)
    deadline = time.monotonic()+5
    while list((root/'old-process').glob('*.json')):
        if time.monotonic()>deadline:
            raise AssertionError('orphan spool was not adopted')
        time.sleep(.01)
    new.close()
    assert store.summary(AuthUser(4, 'admin', 'system_admin', True))['calls'] == 1


@pytest.mark.parametrize("accounting_enabled", [False, True])
def test_openinference_cost_boundary_is_one_leaf_llm_not_runtime_parent(monkeypatch, accounting_enabled):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from langchain_openai import ChatOpenAI
    from src.core.model_gateway import build_governed_model
    from src.core.chat_model_runtime import invoke_text
    import src.observability.tracing as tracing
    import src.costs.capture as capture

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, '_TRACER', provider.get_tracer('leaf-contract'))
    monkeypatch.setattr(settings, 'COST_LEDGER_ENABLED', accounting_enabled)
    events = []
    monkeypatch.setattr(capture, 'get_sink', lambda: SimpleNamespace(emit=events.append))
    response = ChatResult(generations=[ChatGeneration(message=AIMessage(content='private', response_metadata={'model_name': 'response-v1'}, usage_metadata={'input_tokens': 3, 'output_tokens': 1, 'total_tokens': 4}))])
    monkeypatch.setattr(ChatOpenAI, '_generate', lambda *_a, **_k: response)
    model = build_governed_model(provider='custom', model='requested', api_key='fixture')
    invoke_text(model, [{'role': 'user', 'content': 'private'}], operation='chat', profile='default')
    spans = exporter.get_finished_spans()
    llm = [span for span in spans if span.attributes.get('openinference.span.kind') == 'LLM']
    assert len(llm) == 1 and llm[0].name == 'hdb.llm.provider'
    assert llm[0].attributes['gen_ai.request.model'] == 'requested'
    assert llm[0].attributes['gen_ai.response.model'] == 'response-v1'
    assert llm[0].attributes['gen_ai.usage.input_tokens'] == 3
    assert llm[0].attributes['hdb.gen_ai.mapping_version'] == 'hdb-genai-20261008-v1'
    assert len(events) == (2 if accounting_enabled else 0)
    provider.shutdown()


def test_sink_storage_target_is_frozen_across_settings_reload(tmp_path, monkeypatch):
    import src.costs.sink as sink_module

    monkeypatch.setattr(settings, "CONTROL_PLANE_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path / "before"))
    prior = sink_module.get_sink()
    try:
        monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path / "after"))
        prior.emit(fixture_event())
        prior.queue.join()
        assert prior.store_factory().summary(AuthUser(4, "admin", "system_admin", True))["calls"] == 1
        assert CostStore().summary(AuthUser(4, "admin", "system_admin", True))["calls"] == 0
        assert sink_module.spool_root("postgres", "database-A", str(tmp_path)) != sink_module.spool_root("postgres", "database-B", str(tmp_path))
    finally:
        prior.close()
        with sink_module._LOCK:
            for key in [key for key, value in sink_module._SINKS.items() if value is prior]:
                sink_module._SINKS.pop(key)
