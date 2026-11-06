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
    bridge._EVIDENCE_CACHE.clear()
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
                    "summary": {
                        "evidence": [
                            {
                                "citation_number": 1,
                                "source_name": "825504380_ADAS_SCH_TCN2.EDF",
                                "locator": "U1800",
                                "snippet": "TPS62872",
                            }
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected path {path}")

    _use(handler)
    result = bridge.ask_knowledge_base("U1800 型号", "AAA")
    assert result["status"] == "completed"
    assert "TPS62872" in result["answer"]
    assert result["evidence_count"] == 1
    assert result["evidence"][0]["source_name"] == "825504380_ADAS_SCH_TCN2.EDF"
    evidence_id = result["evidence"][0]["evidence_id"]
    assert bridge.get_evidence(evidence_id)["snippet"] == "TPS62872"
    assert polls["n"] == 2


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
