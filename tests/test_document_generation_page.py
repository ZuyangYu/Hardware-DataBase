from __future__ import annotations

from unittest.mock import MagicMock

from src.ui.document_generation_page import (
    _matching_schemas,
    _render_durable_runs,
    _render_work_order_creation,
    _run_timeline,
)


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
class _WorkflowStreamlit:
    def __init__(self, *, buttons: dict[str, bool] | None = None):
        self.buttons = buttons or {}
        self.selectboxes = []
        self.infos = []
        self.errors = []
        self.warnings = []
        self.successes = []
        self.session_state = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def subheader(self, _message):
        pass

    def selectbox(self, label, options, **_kwargs):
        options = list(options)
        self.selectboxes.append((label, options))
        return options[0]

    def columns(self, count):
        return tuple(self for _ in range(count))

    def checkbox(self, _label, **_kwargs):
        return False

    def button(self, _label, *, key=None, **_kwargs):
        return self.buttons.get(key, False)

    def info(self, message):
        self.infos.append(message)

    def error(self, message):
        self.errors.append(message)

    def warning(self, message):
        self.warnings.append(message)

    def success(self, message):
        self.successes.append(message)

    def write(self, _message):
        pass


def _approved_options(*, knowledge_bases=None):
    return {
        "knowledge_bases": ["hardware"] if knowledge_bases is None else knowledge_bases,
        "templates": [{
            "template_version_id": "template-v1",
            "template_id": "review",
            "template_schema_id": "review-schema",
            "template_schema_version": "1",
        }],
        "schemas": [{"document_schema_id": "review-schema", "version": "1"}],
    }


def test_work_order_page_uses_authorized_knowledge_bases_not_projects():
    fake_st = _WorkflowStreamlit()
    pipeline = MagicMock()
    pipeline.list_accessible_projects.return_value = []
    pipeline.list_knowledge_base_document_generation_options.return_value = _approved_options()

    _render_work_order_creation(fake_st, pipeline, "ctx")

    assert ("已授权知识库", ["hardware"]) in fake_st.selectboxes
    pipeline.list_accessible_projects.assert_not_called()


def test_work_order_page_explains_when_no_authorized_knowledge_bases():
    fake_st = _WorkflowStreamlit()
    pipeline = MagicMock()
    pipeline.list_knowledge_base_document_generation_options.return_value = _approved_options(knowledge_bases=[])

    _render_work_order_creation(fake_st, pipeline, "ctx")

    assert fake_st.infos == ["当前账号没有可用于文档生成的知识库，请联系管理员授权知识库。"]


def test_durable_runs_explain_when_no_authorized_knowledge_bases():
    fake_st = _WorkflowStreamlit()
    pipeline = MagicMock()
    pipeline.list_knowledge_base_document_generation_options.return_value = _approved_options(knowledge_bases=[])

    _render_durable_runs(fake_st, pipeline, "ctx")

    assert fake_st.infos == ["当前账号没有可用于文档生成的知识库，请联系管理员授权知识库。"]


def test_work_order_page_creates_work_order_for_selected_knowledge_base():
    fake_st = _WorkflowStreamlit(buttons={"create-document-work-order": True})
    pipeline = MagicMock()
    pipeline.list_knowledge_base_document_generation_options.return_value = _approved_options()
    pipeline.create_knowledge_base_document_work_order.return_value = {"work_order_id": "wo-1"}

    _render_work_order_creation(fake_st, pipeline, "ctx")

    pipeline.create_knowledge_base_document_work_order.assert_called_once()
    args, kwargs = pipeline.create_knowledge_base_document_work_order.call_args
    assert args == ("ctx",)
    assert kwargs == {
        "knowledge_base_name": "hardware",
        "template_version_id": "template-v1",
        "document_schema_id": "review-schema",
        "document_schema_version": "1",
        "idempotency_key": kwargs["idempotency_key"],
    }
    assert kwargs["idempotency_key"].startswith("streamlit-kb-")


def test_durable_runs_filter_work_orders_by_selected_knowledge_base():
    fake_st = _WorkflowStreamlit()
    pipeline = MagicMock()
    pipeline.list_knowledge_base_document_generation_options.return_value = _approved_options()
    pipeline.list_knowledge_base_document_work_orders.return_value = [{"work_order_id": "wo-1"}]
    pipeline.get_document_run_status.return_value = {
        "work_order_id": "wo-1", "status": "planned", "artifacts": [],
    }

    _render_durable_runs(fake_st, pipeline, "ctx")

    assert ("已授权知识库", ["hardware"]) in fake_st.selectboxes
    pipeline.list_knowledge_base_document_work_orders.assert_called_once_with("ctx", "hardware")
    pipeline.list_accessible_projects.assert_not_called()
    pipeline.list_document_work_orders.assert_not_called()
