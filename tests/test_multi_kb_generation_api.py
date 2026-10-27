pytest_plugins = ("tests.test_multi_kb_turn_flow", "tests.test_knowledge_scope")


def test_generation_api_passes_server_authorized_multi_scope(api, monkeypatch):
    import src.settings
    monkeypatch.setattr(src.settings, "MULTI_KB_HUMAN_REVIEW_ENABLED", True)
    client, _auth, _actor, _owner, ids, _conv, pipeline = api
    captured = []
    pipeline.create_document_generation_session = lambda ctx, **kwargs: captured.append(ctx) or {"session_id": "s"}
    response = client.post("/api/v1/document-generation/sessions?kb=A", json={
        "contract_version": "output_spec_v1", "purpose": "输入电压说明",
        "knowledge_scope": {"purpose": "compose", "sources": [
            {"kb_id": ids["A"], "role": "primary"}, {"kb_id": ids["B"], "role": "reference"},
        ]},
    })
    assert response.status_code == 200, response.text
    assert {s["kb_id"] for s in captured[0].metadata["knowledge_scope_snapshot"]["sources"]} == {ids["A"], ids["B"]}


def test_generation_api_denied_source_never_creates_session(api, monkeypatch):
    import src.settings
    monkeypatch.setattr(src.settings, "MULTI_KB_HUMAN_REVIEW_ENABLED", True)
    client, _auth, _actor, _owner, ids, _conv, pipeline = api
    captured = []
    pipeline.create_document_generation_session = lambda *args, **kwargs: captured.append(True) or {}
    response = client.post("/api/v1/document-generation/sessions?kb=A", json={
        "contract_version": "output_spec_v1", "purpose": "输入电压说明",
        "knowledge_scope": {"purpose": "compose", "sources": [
            {"kb_id": ids["A"], "role": "primary"}, {"kb_id": ids["private"], "role": "reference"},
        ]},
    })
    assert response.status_code == 403, response.text
    assert captured == []
