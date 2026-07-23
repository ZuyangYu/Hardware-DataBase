from __future__ import annotations

from src.ui.document_generation_page import _matching_schemas, _run_timeline


def test_run_timeline_marks_current_harness_node_and_terminal_error():
    timeline = _run_timeline({
        "status": "retrieving",
        "harness_run": {
            "current_node": "draft_ready_unit",
            "status": "running",
            "error": None,
        },
    })

    assert ("撰写", "active") in timeline
    assert ("渲染", "pending") in timeline


def test_run_timeline_exposes_failed_run_error():
    timeline = _run_timeline({
        "status": "blocked",
        "harness_run": {
            "current_node": "failed",
            "status": "failed",
            "error": {"message": "writer unavailable"},
        },
    })

    assert ("失败：writer unavailable", "error") in timeline


def test_run_timeline_maps_durable_non_harness_order_states():
    assert ("撰写", "active") in _run_timeline({"status": "drafting"})
    assert ("校验", "active") in _run_timeline({"status": "validating"})
    assert ("渲染", "active") in _run_timeline({"status": "rendering"})


def test_matching_schemas_requires_template_schema_version():
    template = {"template_schema_id": "review", "template_schema_version": "2"}
    schemas = [
        {"document_schema_id": "review", "version": "1"},
        {"document_schema_id": "review", "version": "2"},
    ]

    assert list(_matching_schemas(template, schemas)) == ["review@2"]
