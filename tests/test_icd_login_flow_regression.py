from types import SimpleNamespace
from unittest.mock import Mock

from src.agents.claim_evidence import InformationRequirement
from src.core.app_pipeline import AppPipeline
from src.document_authoring.icd_scope_decision import (
    IcdScopeDecision,
    IcdScopeException,
    IcdScopeItem,
)
from src.document_authoring.models import (
    DocumentFieldSchema,
    DocumentSchema,
    IcdScopeResolution,
    IcdScopeReview,
)
from src.pipelines.document_rag.schemas import RequestContext
import src.ui.document_generation_page as document_generation_page


class _ScopeReviewUi:
    def __init__(self, *, action="include", submit=True):
        self.labels, self.text, self.session_state = [], [], {}
        self.action = action
        self.submit = submit
        self.options = []

    def subheader(self, label): self.labels.append(label)
    def caption(self, message): self.text.append(message)
    def write(self, message):
        self.labels.append(message)
        self.text.append(message)
    def selectbox(self, _label, options, *, key, **_kwargs):
        self.options.append(list(options))
        return self.action
    def text_input(self, _label, *, key, **_kwargs): return "PGND stays internal"
    def button(self, label, *, key, **_kwargs):
        self.labels.append(label)
        return self.submit and key.startswith("submit-")
    def error(self, message): self.text.append(message)
    def success(self, message): self.text.append(message)
    def expander(self, label, **_kwargs):
        self.labels.append(label)
        return self
    def __enter__(self): return self
    def __exit__(self, *_args): return False


class _ScopeBlockedCreationUi:
    def __init__(self):
        self.successes, self.statuses, self.session_state = [], [], {}

    def subheader(self, _message): pass
    def selectbox(self, _label, options, **_kwargs): return list(options)[0]
    def columns(self, count): return tuple(self for _ in range(count))
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def button(self, _label, *, key=None, **_kwargs): return key == "auto-generate-document-submit"
    def status(self, _label, **_kwargs):
        status = Mock()
        self.statuses.append(status)
        return status
    def success(self, message): self.successes.append(message)
    def error(self, _message): pass
    def warning(self, _message): pass


def fixture_pins():
    return [
        {"refdes": "X1900", "pin_name": str(pin), "net_name": f"NET_{pin}"}
        for pin in range(1, 20)
    ]


def fixture_fpt():
    return {f"X1900-{pin}" for pin in range(1, 19)}


def icd_template():
    return DocumentSchema(
        document_schema_id="icd", version="1", document_type="icd",
        execution_mode="internal_harness",
        fields=[DocumentFieldSchema(
            field_id="pins", label="Connector pin definition",
            required_capabilities=["relationship_lookup"],
            retrieval_policy_id="retrieval-pins", verification_policy_id="verify-pins",
        )],
    )


def run_logged_in_kb_flow(*, edf_pins, fpt, template):
    pipeline = object.__new__(AppPipeline)
    ctx = RequestContext(
        user_id="alice", tenant_id="tenant-a", metadata={"department_id": "hw"},
        kb_permissions={"hw:hardware": "read"},
    )
    order = SimpleNamespace(
        work_order_id="work-icd-1", document_schema_id=template.document_schema_id,
        document_schema_version=template.version,
    )
    review = IcdScopeReview(
        work_order_id=order.work_order_id,
        source_snapshot_hash="snapshot-hash",
        decision=IcdScopeDecision(
            auto_items=[IcdScopeItem(**pin) for pin in edf_pins if f"{pin['refdes']}-{pin['pin_name']}" in fpt],
            exceptions=[IcdScopeException(
                exception_id="extra-pin-exposure", kind="extra_pin_exposure", **edf_pins[-1],
                recommended_action="mark_pending", user_instruction="Confirm exposure.",
            )],
            frozen_pin_mappings=edf_pins,
        ),
    )
    service = Mock()
    service.create_knowledge_base_work_order.return_value = order
    service.resolve_source_snapshot.return_value = SimpleNamespace(
        source_set_snapshot_id="snapshot-1", source_names=["board.edf"],
    )
    service._schema.return_value = template
    service.prepare_icd_scope_review.return_value = review
    service.get_icd_scope_review.return_value = review
    service.submit_icd_scope_resolution.return_value = review.model_copy(update={
        "status": "frozen",
        "resolutions": [],
    })
    service.build_knowledge_base_retrieval_outcome.side_effect = (
        lambda _kb, _sources, evidences, **_kwargs: SimpleNamespace(evidences=evidences)
    )
    pipeline.document_generation = service
    pipeline.list_file_infos = Mock(return_value=[SimpleNamespace(name="board.edf")])
    pipeline.backend = Mock()
    pipeline.backend.retrieve.return_value = []
    pipeline.circuit_service = Mock()
    pipeline.circuit_service.list_pin_mapping_evidence.return_value = []
    pipeline.spreadsheet_service = None

    blocked = pipeline.auto_generate_knowledge_base_document(
        ctx, knowledge_base_name="hardware", template_version_id="template-1",
        document_schema_id="icd", document_schema_version="1",
    )
    assert blocked["stage"] == "scope_review_required"
    pending = pipeline.get_icd_scope_review(ctx, order.work_order_id)
    frozen = pipeline.submit_icd_scope_resolution(
        ctx, order.work_order_id,
        resolutions=[{"exception_id": "extra-pin-exposure", "action": "exclude"}],
        comment="PGND stays internal",
    )
    pipeline.circuit_service = None
    retrieve = pipeline._knowledge_base_retriever(
        ctx, "hardware", ["board.edf"], icd_scope_review=pending,
    )
    outcome = retrieve(InformationRequirement(
        requirement_id="pins", semantic_unit_id="pins", claim_type="relationship",
        subject="connector pins", required_capabilities=["relationship_lookup"],
    ), 0)
    candidate_pin_set = outcome.evidences[-1].metadata["pin_mappings"]
    return SimpleNamespace(
        auto_adopted_count=len(pending.decision.auto_items),
        exceptions=[exception.kind for exception in pending.exceptions],
        candidate_pin_set=candidate_pin_set,
        frozen_pin_set=pending.decision.frozen_pin_mappings,
        frozen=frozen,
    )


def test_logged_in_icd_flow_injects_frozen_pins_and_requires_only_pgnd_decision():
    result = run_logged_in_kb_flow(
        edf_pins=fixture_pins(), fpt=fixture_fpt(), template=icd_template(),
    )

    assert result.auto_adopted_count == 18
    assert result.exceptions == ["extra_pin_exposure"]
    assert result.candidate_pin_set == result.frozen_pin_set


def test_scope_review_ui_resumes_same_work_order_after_one_explicit_batch():
    review = IcdScopeReview(
        work_order_id="work-ui-1", source_snapshot_hash="snapshot-hash",
        decision=IcdScopeDecision(
            auto_items=[IcdScopeItem(refdes="X1900", pin_name="1", net_name="CAN_H")],
            exceptions=[IcdScopeException(
                exception_id="exception-pgnd", kind="extra_pin_exposure",
                refdes="J7", pin_name="3", net_name="PGND",
                recommended_action="mark_pending", user_instruction="Confirm exposure.",
            )],
        ),
    )
    pipeline = Mock()
    pipeline.get_icd_scope_review.return_value = review
    rendered = _ScopeReviewUi(action="不纳入")

    document_generation_page._render_icd_scope_review(rendered, pipeline, "ctx", "work-ui-1")

    assert {"发现的问题", "系统建议", "你需要做什么"} <= set(rendered.labels)
    assert "X1900-1" not in " ".join(rendered.text)
    assert rendered.options == [["请选择…", "纳入", "不纳入"]]
    pipeline.submit_icd_scope_resolution.assert_called_once_with(
        "ctx", "work-ui-1",
        resolutions=[{"exception_id": "exception-pgnd", "action": "exclude"}],
        comment="PGND stays internal",
    )
    pipeline.continue_knowledge_base_document_generation.assert_called_once_with("ctx", "work-ui-1")


def test_scope_review_ui_requires_an_explicit_include_or_exclude_for_every_exception():
    review = IcdScopeReview(
        work_order_id="work-ui-2", source_snapshot_hash="snapshot-hash",
        decision=IcdScopeDecision(exceptions=[IcdScopeException(
            exception_id="exception-pgnd", kind="extra_pin_exposure",
            refdes="J7", pin_name="3", net_name="PGND",
            recommended_action="mark_pending", user_instruction="Confirm exposure.",
        )]),
    )
    pipeline = Mock()
    pipeline.get_icd_scope_review.return_value = review
    rendered = _ScopeReviewUi(action="请选择…")

    document_generation_page._render_icd_scope_review(rendered, pipeline, "ctx", "work-ui-2")

    pipeline.submit_icd_scope_resolution.assert_not_called()
    pipeline.continue_knowledge_base_document_generation.assert_not_called()
    assert any("明确选择“纳入”或“不纳入”" in message for message in rendered.text)


def test_scope_review_ui_shows_frozen_summary_without_edit_controls():
    review = IcdScopeReview(
        work_order_id="work-ui-3", source_snapshot_hash="snapshot-hash",
        decision=IcdScopeDecision(exceptions=[IcdScopeException(
            exception_id="exception-pgnd", kind="extra_pin_exposure",
            refdes="J7", pin_name="3", net_name="PGND",
            recommended_action="mark_pending", user_instruction="Confirm exposure.",
        )]),
        status="frozen",
        resolutions=[IcdScopeResolution(
            exception_id="exception-pgnd", action="exclude", actor_id="alice",
        )],
    )
    pipeline = Mock()
    pipeline.get_icd_scope_review.return_value = review
    rendered = _ScopeReviewUi()

    document_generation_page._render_icd_scope_review(rendered, pipeline, "ctx", "work-ui-3")

    pipeline.submit_icd_scope_resolution.assert_not_called()
    pipeline.continue_knowledge_base_document_generation.assert_not_called()
    assert "ICD 范围已冻结" in rendered.labels
    assert "应用处理结果并继续生成" not in rendered.labels


def test_scope_review_ui_explains_connector_scope_unknown_without_actions():
    review = IcdScopeReview(
        work_order_id="work-ui-connector-scope", source_snapshot_hash="snapshot-hash",
        decision=IcdScopeDecision(exceptions=[IcdScopeException(
            exception_id="connector-scope-unknown", kind="connector_scope_unknown",
            recommended_action="mark_pending",
            user_instruction="Provide a connector identifier.",
        )]),
    )
    pipeline = Mock()
    pipeline.get_icd_scope_review.return_value = review
    rendered = _ScopeReviewUi()

    document_generation_page._render_icd_scope_review(
        rendered, pipeline, "ctx", "work-ui-connector-scope"
    )

    pipeline.submit_icd_scope_resolution.assert_not_called()
    assert rendered.options == []
    assert any(
        "补充模板检索条件/Pin Definition 位号后重新生成" in message
        for message in rendered.text
    )


def test_scope_review_ui_explains_connector_mapping_missing_without_actions():
    review = IcdScopeReview(
        work_order_id="work-ui-connector-mapping", source_snapshot_hash="snapshot-hash",
        decision=IcdScopeDecision(exceptions=[IcdScopeException(
            exception_id="connector-mapping-missing", kind="connector_mapping_missing",
            refdes="X302", recommended_action="check_edf_mapping",
            user_instruction="请检查已上传 EDF 是否包含 X302 的管脚映射。",
        )]),
    )
    pipeline = Mock()
    pipeline.get_icd_scope_review.return_value = review
    rendered = _ScopeReviewUi()

    document_generation_page._render_icd_scope_review(
        rendered, pipeline, "ctx", "work-ui-connector-mapping"
    )

    pipeline.submit_icd_scope_resolution.assert_not_called()
    assert rendered.options == []
    assert any("检查已上传 EDF" in message for message in rendered.text)


def test_continue_kb_generation_reuses_the_existing_work_order_and_snapshot():
    pipeline = object.__new__(AppPipeline)
    ctx = RequestContext(
        user_id="alice", tenant_id="tenant-a", metadata={"department_id": "hw"},
        kb_permissions={"hw:hardware": "read"},
    )
    order = SimpleNamespace(
        work_order_id="work-continue-1", scope_type="knowledge_base",
        knowledge_base_name="hardware",
    )
    review = IcdScopeReview(
        work_order_id=order.work_order_id, source_snapshot_hash="snapshot-hash",
        decision=IcdScopeDecision(), status="frozen",
    )
    snapshot = SimpleNamespace(source_set_snapshot_id="snapshot-1", source_names=["board.edf"])
    service = Mock()
    service.store.get_work_order.return_value = order
    service.get_icd_scope_review.return_value = review
    service.resolve_source_snapshot.return_value = snapshot
    service.run_internal_harness.return_value = "candidate-1"
    pipeline.document_generation = service
    pipeline._knowledge_base_retriever = Mock(return_value="retrieve")

    result = pipeline.continue_knowledge_base_document_generation(ctx, order.work_order_id)

    assert result == "candidate-1"
    service.require_work_order_capability.assert_called_once_with(ctx, order, "view_project")
    service.get_icd_scope_review.assert_called_once_with(ctx, order.work_order_id)
    pipeline._knowledge_base_retriever.assert_called_once_with(
        ctx, "hardware", ["board.edf"],
        source_set_snapshot_id="snapshot-1", icd_scope_review=review,
    )
    service.run_internal_harness.assert_called_once_with(
        ctx, order.work_order_id, retrieve="retrieve",
    )


def test_auto_generation_scope_block_explains_that_no_candidate_exists_yet():
    st = _ScopeBlockedCreationUi()
    pipeline = Mock()
    pipeline.list_knowledge_base_document_generation_options.return_value = {
        "knowledge_bases": ["hardware"],
        "templates": [{
            "template_version_id": "template-v1", "template_id": "icd",
            "template_schema_id": "icd-schema", "template_schema_version": "1",
        }],
        "schemas": [{"document_schema_id": "icd-schema", "version": "1"}],
    }
    pipeline.auto_generate_knowledge_base_document.return_value = {
        "stage": "scope_review_required", "work_order_id": "work-scope-1",
    }

    document_generation_page._render_work_order_creation(st, pipeline, "ctx")

    assert any("需处理少量 ICD 范围异常" in message for message in st.successes)
    assert any("work-scope-1" in message for message in st.successes)
    st.statuses[0].update.assert_called_with(
        label="已创建工作单，等待 ICD 范围处理", state="complete",
    )


def test_auto_generation_template_contract_stop_does_not_claim_a_candidate_exists():
    st = _ScopeBlockedCreationUi()
    pipeline = Mock()
    pipeline.list_knowledge_base_document_generation_options.return_value = {
        "knowledge_bases": ["hardware"],
        "templates": [{
            "template_version_id": "template-v1", "template_id": "icd",
            "template_schema_id": "icd-schema", "template_schema_version": "1",
        }],
        "schemas": [{"document_schema_id": "icd-schema", "version": "1"}],
    }
    pipeline.auto_generate_knowledge_base_document.return_value = {
        "stage": "template_contract_review_required",
        "work_order_id": "work-template-1",
        "issues": [{"code": "icd_formal_template_required"}],
    }

    document_generation_page._render_work_order_creation(st, pipeline, "ctx")

    assert any("正式 ICD 模板" in message for message in st.successes)
    assert not any("候选文件" in message for message in st.successes)
    st.statuses[0].update.assert_called_with(
        label="已创建工作单，等待正式 ICD 模板", state="complete",
    )
