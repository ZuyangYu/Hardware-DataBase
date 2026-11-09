"""MCP bridge tools call the HDB API with the configured token."""

from __future__ import annotations

import base64
import httpx
import pytest
from types import SimpleNamespace

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


def test_list_kbs_returns_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/kbs"
        return httpx.Response(200, json=[{"name": "AAA"}])

    _use(handler)
    assert bridge.list_kbs()[0]["name"] == "AAA"


def test_query_circuit_uses_first_design_and_filters():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/kbs/AAA/structured/circuit-designs":
            return httpx.Response(
                200,
                json={"designs": [{"design_id": "design-1"}], "failed_logs": []},
            )
        if path == "/api/v1/kbs/AAA/structured/circuit-designs/design-1":
            seen["params"] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    "summary": {"design_id": "design-1"},
                    "modules": [{"module_id": "m1"}],
                    "instances": [{"refdes": "U1800"}],
                    "nets": [{"name": "VCC0V75"}],
                    "cross_references": [],
                },
            )
        raise AssertionError(f"unexpected path {path}")

    _use(handler)
    result = bridge.query_circuit("AAA", "U1800")
    assert result["design_id"] == "design-1"
    assert result["instances"][0]["refdes"] == "U1800"
    assert seen["params"] == {"net_query": "U1800", "instance_query": "U1800"}


def test_create_drawio_file_builds_valid_xml_with_escaped_labels():
    result = bridge.create_drawio_file(
        "test<diagram>.drawio",
        blocks=[
            {"id": "soc", "label": "主控 <SoC>", "x": 100, "y": 100, "w": 200, "h": 100},
            {"id": "ddr", "label": "LPDDR4", "x": 400, "y": 100, "w": 200, "h": 100},
        ],
        edges=[
            {"source": "soc", "target": "ddr", "label": "DDR0_LP4"},
        ],
        labels=[{"text": "VCC3V3", "x": 250, "y": 60, "w": 80, "h": 30}],
    )
    assert result["filename"] == "test_diagram_.drawio"
    assert result["sha256"]
    import xml.etree.ElementTree as ET

    root = ET.fromstring(result["content"])
    cells = root.findall(".//mxCell")
    assert any(cell.attrib.get("id") == "0" for cell in cells)
    assert any(cell.attrib.get("id") == "1" for cell in cells)
    assert any(cell.attrib.get("id") == "soc" for cell in cells)
    assert any(cell.attrib.get("id") == "e1" for cell in cells)
    assert "&lt;SoC&gt;" in result["content"]
    assert "DDR0_LP4" in result["content"]


def test_create_drawio_rejects_unknown_edge_target():
    import pytest

    with pytest.raises(ValueError, match="source/target"):
        bridge.create_drawio_file(
            "bad.drawio",
            blocks=[{"id": "soc", "label": "SoC", "x": 0, "y": 0, "w": 100, "h": 50}],
            edges=[{"source": "soc", "target": "missing"}],
        )


def test_query_table_validates_and_executes(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_visible_kb",
        lambda _name: {"name": "AAA", "department_id": "1739", "permission": "read"},
    )
    monkeypatch.setattr(
        "src.services.spreadsheet_index_service.SpreadsheetIndexService.db_path",
        lambda self, department_id, kb_name, create=False: "/tmp/unused.db",
    )
    monkeypatch.setattr(
        "src.agents.tools.spreadsheet_tools._load_sql_registry",
        lambda _path: [{"table_name": "t1", "document_name": "d", "sheet_name": "s"}],
    )
    monkeypatch.setattr(
        "src.agents.tools.spreadsheet_tools._validate_readonly_sql",
        lambda _sql, _allowed: (SimpleNamespace(sql=lambda dialect: "SELECT a FROM t1 LIMIT 200"), None),
    )
    monkeypatch.setattr(
        "src.agents.tools.spreadsheet_tools._execute_readonly_sql",
        lambda _path, _sql: ([{"a": 1}, {"a": 2}], None),
    )
    result = bridge.query_table("AAA", "SELECT a FROM t1")
    assert result["tables"] == ["t1"]
    assert result["row_count"] == 2
    assert result["columns"] == ["a"]
    assert result["rows"][0]["a"] == 1


def test_upload_file_posts_multipart_and_returns_ack():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.read().decode("utf-8", "replace")
        return httpx.Response(
            200,
            json={
                "success_count": 1,
                "total_count": 1,
                "failed_count": 0,
                "skipped_count": 0,
                "status": "ok",
            },
        )

    _use(handler)
    result = bridge.upload_file(
        "AAA", "note.txt", base64.b64encode(b"hello").decode()
    )
    assert result["success_count"] == 1
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/kbs/AAA/files"
    assert "multipart/form-data" in seen["content_type"]
    assert "note.txt" in seen["body"]
    assert "hello" in seen["body"]


def test_create_drawio_file_auto_routes_without_explicit_points():
    result = bridge.create_drawio_file(
        "auto.drawio",
        blocks=[
            {"id": "a", "label": "A", "x": 100, "y": 100, "w": 100, "h": 60},
            {"id": "b", "label": "B", "x": 400, "y": 300, "w": 100, "h": 60},
        ],
        edges=[{"source": "a", "target": "b", "label": "A→B"}],
    )
    assert 'as="sourcePoint"' in result["content"]
    assert 'as="targetPoint"' in result["content"]
    assert '<Array as="points">' in result["content"]
    import xml.etree.ElementTree as ET

    ET.fromstring(result["content"])


def test_list_tables_uses_department_scoped_database(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_visible_kb",
        lambda _name: {"name": "AAA", "department_id": "1739", "permission": "read"},
    )
    seen = {}

    def db_path(self, department_id, kb_name, create=False):
        seen["department_id"] = department_id
        seen["kb_name"] = kb_name
        return "/tmp/unused.db"

    monkeypatch.setattr(
        "src.services.spreadsheet_index_service.SpreadsheetIndexService.db_path",
        db_path,
    )
    monkeypatch.setattr(
        "src.agents.tools.spreadsheet_tools._load_sql_registry",
        lambda _path: [{"table_name": "t1", "row_count": 7}],
    )
    result = bridge.list_tables("AAA")
    assert result["table_count"] == 1
    assert result["tables"][0]["table_name"] == "t1"
    assert seen == {"department_id": "1739", "kb_name": "AAA"}


def test_asset_read_tools_use_expected_paths():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/assets"):
            return httpx.Response(200, json=[{"id": 1, "name": "connector"}])
        if "/assets/" in request.url.path:
            return httpx.Response(200, json={"id": 1, "name": "connector"})
        if request.url.path.endswith("/doc-assets"):
            return httpx.Response(200, json=[{"id": 9, "title": "HSI"}])
        if "/doc-assets/" in request.url.path:
            return httpx.Response(200, json={"id": 9, "title": "HSI"})
        raise AssertionError(f"unexpected path {request.url.path}")

    _use(handler)
    assert bridge.list_assets("AAA")[0]["name"] == "connector"
    assert bridge.get_asset("AAA", "1")["name"] == "connector"
    assert bridge.list_doc_assets("AAA")[0]["title"] == "HSI"
    assert bridge.get_doc_asset("AAA", "9")["title"] == "HSI"
    assert seen == [
        "/api/v1/kbs/AAA/assets",
        "/api/v1/kbs/AAA/assets/1",
        "/api/v1/kbs/AAA/doc-assets",
        "/api/v1/kbs/AAA/doc-assets/9",
    ]


def test_download_kb_file_reads_archived_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_visible_kb",
        lambda _name: {"name": "AAA", "department_id": "1739", "permission": "read"},
    )
    monkeypatch.setattr(bridge, "_USERNAME", "aaa_admin")
    archive = tmp_path / "pipeline_archives" / "departments" / "1739" / "kbs" / "AAA"
    target = archive / "docs" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"hello archive")

    import src.settings as settings

    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "src.core.auth.AuthService.get_user_by_username",
        lambda self, _username: SimpleNamespace(id=4, username="aaa_admin", department_id="1739"),
    )
    monkeypatch.setattr(
        "src.api.context.build_context_for_user",
        lambda user, kb_name, auth=None: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "src.core.app_pipeline.AppPipeline.list_file_infos",
        lambda self, kb_name, ctx=None: [
            SimpleNamespace(id="f1", name="a.txt", local_path="docs/a.txt")
        ],
    )
    result = bridge.download_kb_file("AAA", "f1")
    assert result["filename"] == "a.txt"
    assert base64.b64decode(result["content_base64"]) == b"hello archive"


def test_delete_file_is_dry_run_until_confirmed():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"ok": True, "message": "deleted"})

    _use(handler)
    dry = bridge.delete_file("AAA", "note.txt")
    assert dry["dry_run"] is True and seen == []
    result = bridge.delete_file("AAA", "note.txt", confirm=True)
    assert result["ok"] is True
    assert seen == [("DELETE", "/api/v1/kbs/AAA/files/note.txt")]


def test_query_circuit_mode_filters_and_summary_off():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/kbs/AAA/structured/circuit-designs":
            return httpx.Response(
                200,
                json={"designs": [{"design_id": "design-1"}], "failed_logs": []},
            )
        if request.url.path == "/api/v1/kbs/AAA/structured/circuit-designs/design-1":
            seen["params"] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    "summary": {"design_id": "design-1"},
                    "modules": [{"module_id": "m1"}],
                    "instances": [{"refdes": "U1800"}],
                    "nets": [{"name": "VCC0V75"}],
                    "cross_references": [],
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    _use(handler)
    result = bridge.query_circuit(
        "AAA",
        "X1900",
        mode="nets",
        include_summary=False,
        include_modules=False,
    )
    assert seen["params"] == {"net_query": "X1900"}
    assert result["summary"] == {}
    assert result["modules"] == []
    assert result["nets"][0]["name"] == "VCC0V75"
    assert result["mode"] == "nets"

    result_all = bridge.query_circuit(
        "AAA",
        "X1900",
        include_summary=True,
        include_modules=True,
    )
    assert seen["params"] == {"net_query": "X1900", "instance_query": "X1900"}
    assert result_all["summary"]["design_id"] == "design-1"
    assert result_all["modules"] and result_all["instances"]


def test_read_file_chunks_start_and_query_filter():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/kbs/AAA/files/f9/chunks"
        return httpx.Response(
            200,
            json={
                "document_id": "d1",
                "file_name": "a.pdf",
                "chunk_count": 5,
                "backend": "ragflow",
                "chunks": [
                    {"index": i, "content": f"chunk alpha {i}", "metadata": {}}
                    for i in range(5)
                ],
            },
        )

    _use(handler)
    result = bridge.read_file_chunks("AAA", "f9", start_index=2, query="alpha")
    assert result["chunk_count"] == 5
    assert result["start_index"] == 2
    assert [row["index"] for row in result["chunks"]] == [2, 3, 4]

    filtered = bridge.read_file_chunks("AAA", "f9", query="alpha")
    assert [row["index"] for row in filtered["chunks"]] == [0, 1, 2, 3, 4]

    missing = bridge.read_file_chunks("AAA", "f9", query="beta")
    assert missing["chunks"] == []


def test_search_knowledge_base_locators_and_cache():
    calls = {"chunks": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/kbs/AAA/files":
            return httpx.Response(
                200,
                json=[
                    {"id": "f1", "name": "ICD.xlsx", "status": "indexed"},
                    {"id": "f2", "name": "HSI.docx", "status": "parsed"},
                ],
            )
        if request.url.path.startswith("/api/v1/kbs/AAA/files/f1/chunks"):
            calls["chunks"] += 1
            return httpx.Response(
                200,
                json={
                    "document_id": "f1",
                    "file_name": "ICD.xlsx",
                    "chunk_count": 2,
                    "backend": "ragflow",
                    "chunks": [
                        {"index": 15, "content": "X1900 pin14 UBD 供电 9~16V", "metadata": {"ragflow_chunk_id": "aaa-15"}},
                        {"index": 16, "content": "无关内容", "metadata": {"ragflow_chunk_id": "aaa-16"}},
                    ],
                },
            )
        if request.url.path.startswith("/api/v1/kbs/AAA/files/f2/chunks"):
            calls["chunks"] += 1
            return httpx.Response(
                200,
                json={
                    "document_id": "f2",
                    "file_name": "HSI.docx",
                    "chunk_count": 1,
                    "backend": "ragflow",
                    "chunks": [
                        {"index": 43, "content": "HSI chunk 提到 UBD 供电", "metadata": {"ragflow_chunk_id": "hsi-43"}}
                    ],
                },
            )
        raise AssertionError(f"unexpected path {path}")

    _use(handler)
    first = bridge.search_knowledge_base("AAA", "UBD 供电", top_k=5)
    assert calls["chunks"] == 2
    assert first["files_searched"] == 2
    assert first["chunks_searched"] == 3
    top = first["results"][0]
    assert top["file_id"] == "f1"
    assert top["chunk_index"] == 15
    assert top["rag_id"] == "aaa-15"
    assert top["score"] > 0

    second = bridge.search_knowledge_base("AAA", "UBD 供电", top_k=5)
    assert calls["chunks"] == 2  # cache reuse, no extra fetch
    assert second["result_count"] == len(first["results"])

    narrowed = bridge.search_knowledge_base("AAA", "UBD", file_id="f2", top_k=5)
    assert calls["chunks"] == 2
    assert all(row["file_id"] == "f2" for row in narrowed["results"])

    bridge._CHUNK_CACHE.clear()


def test_download_kb_file_falls_back_to_kb_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bridge,
        "_visible_kb",
        lambda _name: {
            "name": "AAA",
            "department_id": "1739",
            "permission": "read",
            "owner_user_id": 4,
        },
    )
    monkeypatch.setattr(bridge, "_USERNAME", "")  # no user creds in this deployment
    monkeypatch.setattr(bridge, "_LOGIN_USER", {})

    archive = tmp_path / "pipeline_archives" / "departments" / "1739" / "kbs" / "AAA"
    target = archive / "docs" / "b.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"owner-fallback")

    import src.settings as settings

    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "src.core.auth.AuthService.get_user_by_username",
        lambda self, _username: None,
    )
    monkeypatch.setattr(
        "src.core.auth.AuthService.get_user_by_id",
        lambda self, _user_id: SimpleNamespace(id=4, username="owner", department_id="1739"),
    )
    monkeypatch.setattr(
        "src.api.context.build_context_for_user",
        lambda user, kb_name, auth=None: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "src.core.app_pipeline.AppPipeline.list_file_infos",
        lambda self, kb_name, ctx=None: [
            SimpleNamespace(id="f2", name="b.txt", local_path="docs/b.txt")
        ],
    )
    result = bridge.download_kb_file("AAA", "f2")
    assert base64.b64decode(result["content_base64"]) == b"owner-fallback"


def test_main_transports(monkeypatch):
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(bridge.mcp, "run", fake_run)
    monkeypatch.delenv("HDB_MCP_TRANSPORT", raising=False)
    monkeypatch.delenv("HDB_MCP_HOST", raising=False)
    monkeypatch.delenv("HDB_MCP_PORT", raising=False)
    bridge.main()
    assert calls[-1] == {"transport": "stdio"}

    monkeypatch.setenv("HDB_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("HDB_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("HDB_MCP_PORT", "9100")
    monkeypatch.setenv("HDB_MCP_HTTP_ALLOW_UNAUTHED", "1")
    bridge.main()
    assert calls[-1] == {"transport": "streamable-http"}
    assert bridge.mcp.settings.host == "0.0.0.0"
    assert bridge.mcp.settings.port == 9100


def _recording_app(recorder: dict) -> object:
    async def app(scope, receive, send):
        recorder["called"] = True
    return app


def _run_middleware_scope(mw: object, headers: dict[str, str]) -> dict[str, object]:
    import asyncio

    captured: dict[str, object] = {}

    async def scope_app(scope, receive, send):  # noqa: ANN001
        captured["app_called"] = True

    async def receive():  # noqa: ANN201
        return {"type": "http.request"}

    async def send(message):  # noqa: ANN001
        captured.setdefault("messages", []).append(message)

    scope = {
        "type": "http",
        "path": "/mcp",
        "method": "POST",
        "headers": [
            (key.encode("latin-1"), value.encode("latin-1")) for key, value in headers.items()
        ],
    }
    asyncio.run(mw(scope, receive, send))
    return captured


def test_http_auth_static_token_mode(monkeypatch):
    monkeypatch.setenv("HDB_MCP_HTTP_TOKEN", "hdb-shared-secret")
    recorder: dict = {}
    mw = bridge._McpTokenAuthMiddleware(_recording_app(recorder), mode="static-token")

    wrong = _run_middleware_scope(mw, {})  # no header
    messages = wrong.get("messages") or []
    assert messages and messages[0]["status"] == 401
    assert recorder.get("called") is not True

    good = _run_middleware_scope(mw, {"X-HDB-MCP-Token": "hdb-shared-secret"})
    assert recorder.get("called") is True
    assert (good.get("messages") or []) == []


def test_http_auth_static_token_fails_closed(monkeypatch):
    monkeypatch.delenv("HDB_MCP_HTTP_TOKEN", raising=False)
    mw = bridge._McpTokenAuthMiddleware(_recording_app({}), mode="static-token")
    result = _run_middleware_scope(mw, {"X-HDB-MCP-Token": "bogus"})
    messages = result.get("messages") or []
    assert messages and messages[0]["status"] == 401


def test_http_auth_mcp_token_verifies_via_platform(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.url.path == "/api/v1/mcp/session"
            and request.headers.get("X-HDB-MCP-Token") == "hdbmcp_goodtoken"
        ):
            return httpx.Response(200, json={"token": "bearer-123", "user": {"username": "alice"}})
        return httpx.Response(401, json={"detail": "mcp token revoked"})

    monkeypatch.setattr(bridge, "_API_BASE", "http://hdb.test/api/v1")
    _use(handler)
    recorder: dict = {}
    mw = bridge._McpTokenAuthMiddleware(_recording_app(recorder), mode="mcp-token")

    wrong = _run_middleware_scope(mw, {})  # no header
    messages = wrong.get("messages") or []
    assert messages and messages[0]["status"] == 401

    rejected = _run_middleware_scope(mw, {"X-HDB-MCP-Token": "hdbmcp_revoked"})
    messages = rejected.get("messages") or []
    assert messages and messages[0]["status"] == 401

    (_run_middleware_scope(mw, {"X-HDB-MCP-Token": "hdbmcp_goodtoken"})
    )
    assert recorder.get("called") is True
    assert bridge._HTTP_AUTH_CACHE["hdbmcp_goodtoken"][1] == "bearer-123"
    bridge._HTTP_AUTH_CACHE.clear()


def test_main_guard_and_bind_flows(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(bridge.mcp, "run", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setenv("HDB_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("HDB_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("HDB_MCP_PORT", "9100")
    monkeypatch.delenv("HDB_MCP_HTTP_AUTH", raising=False)
    monkeypatch.delenv("HDB_MCP_HTTP_ALLOW_UNAUTHED", raising=False)
    monkeypatch.delenv("HDB_MCP_HTTP_TOKEN", raising=False)

    with pytest.raises(RuntimeError) as excinfo:  # open LAN refused by default
        bridge.main()
    assert "Refusing to bind" in str(excinfo.value)

    monkeypatch.setenv("HDB_MCP_HTTP_ALLOW_UNAUTHED", "1")  # acknowledged open socket
    bridge.main()
    assert calls[-1] == {"transport": "streamable-http"}

    # auth path: static-token guarded uvicorn, runner mocked to avoid live serve
    monkeypatch.delenv("HDB_MCP_HTTP_ALLOW_UNAUTHED", raising=False)
    monkeypatch.setenv("HDB_MCP_HTTP_AUTH", "static-token")
    monkeypatch.setenv("HDB_MCP_HTTP_TOKEN", "hdb-shared-secret")
    import uvicorn as _uvicorn_mod

    uv_calls: list[dict] = []
    monkeypatch.setattr(
        _uvicorn_mod, "run", lambda *args, **kwargs: uv_calls.append({"args": args, **kwargs})
    )
    bridge.main()
    assert uv_calls[-1]["port"] == 9100 and uv_calls[-1]["host"] == "0.0.0.0"

