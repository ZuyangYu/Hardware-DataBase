from types import SimpleNamespace
from unittest.mock import Mock

from src.agents.claim_evidence import InformationRequirement
from src.core.app_pipeline import AppPipeline
from src.document_authoring.icd_scope_decision import (
    IcdScopeDecision,
    IcdScopeException,
    IcdScopeItem,
)
from src.document_authoring.models import DocumentFieldSchema, DocumentSchema, IcdScopeReview
from src.pipelines.document_rag.schemas import RequestContext
import src.ui.document_generation_page as document_generation_page


class _ScopeReviewUi:
    def __init__(self):
        self.labels, self.text, self.session_state = [], [], {}

    def subheader(self, label): self.labels.append(label)
    def caption(self, message): self.text.append(message)
    def write(self, message): self.labels.append(message); self.text.append(message)
    def selectbox(self, _label, options, *, key, **_kwargs): return list(options)[0]
    def text_input(self, _label, *, key, **_kwargs): return "PGND stays internal"
    def button(self, label, *, key, **_kwargs): self.labels.append(label); return key.startswith("submit-")
    def error(self, message): self.text.append(message)
    def success(self, message): self.text.append(message)
    def expander(self, label, **_kwargs): self.labels.append(label); return self
    def __enter__(self): return self
    def __exit__(self, *_args): return False


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


def test_scope_review_ui_explains_exceptions_and_submits_one_batch():
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
    rendered = _ScopeReviewUi()

    document_generation_page._render_icd_scope_review(rendered, pipeline, "ctx", "work-ui-1")

    assert {"发现的问题", "系统建议", "你需要做什么"} <= set(rendered.labels)
    assert "X1900-1" not in " ".join(rendered.text)
    pipeline.submit_icd_scope_resolution.assert_called_once_with(
        "ctx", "work-ui-1",
        resolutions=[{"exception_id": "exception-pgnd", "action": "mark_pending"}],
        comment="PGND stays internal",
    )
