"""MCP bridge tools call the HDB API with the configured token."""

from __future__ import annotations

import httpx
import pytest

import src.mcp_bridge.server as bridge


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(bridge, "_TOKEN", "test-token")
    monkeypatch.setattr(bridge, "_API_BASE", "http://hdb.test/api/v1")
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    yield
    if bridge._http is not None:
        bridge._http.close()
    monkeypatch.setattr(bridge, "_http", None)


def _use(handler):
    bridge._http = httpx.Client(transport=httpx.MockTransport(handler))


def test_ask_knowledge_base_polls_until_terminal():
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/conversations":
            return httpx.Response(200, json={"id": 7})
        if path == "/api/v1/conversations/7/turns":
            return httpx.Response(201, json={"turn": {"id": "turn-1"}})
        if path == "/api/v1/turns/turn-1/start":
            return httpx.Response(202, json={})
        if path == "/api/v1/turns/turn-1":
            polls["n"] += 1
            if polls["n"] == 1:
                return httpx.Response(200, json={"status": "streaming"})
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "answer": "U1800 是 TPS62872 [1]",
                    "summary": {"evidence": [{"citation_number": 1}]},
                },
            )
        raise AssertionError(f"unexpected path {path}")

    _use(handler)
    result = bridge.ask_knowledge_base("U1800 型号", "AAA")
    assert result["status"] == "completed"
    assert "TPS62872" in result["answer"]
    assert result["evidence_count"] == 1
    assert polls["n"] == 2


def test_list_kbs_returns_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/kbs"
        return httpx.Response(200, json=[{"name": "AAA"}])

    _use(handler)
    assert bridge.list_kbs()[0]["name"] == "AAA"
