import unittest
from unittest import mock

from src.core.app_pipeline import AppPipeline


class _Order:
    work_order_id = "wo-1"
    scope_type = "knowledge_base"
    knowledge_base_name = "shared"


class _Snapshot:
    source_set_snapshot_id = "snap-1"
    source_names = ["a.pdf"]


class _ScopeReview:
    pending_count = 0
    exceptions = []


class _DocGen:
    def __init__(self):
        self.worker = mock.Mock()
        self.worker.submit.return_value = "bg-1"
        self.store = mock.Mock()
        self.store.get_work_order.return_value = _Order()

    def resolve_source_snapshot(self, order):
        self.snapshot_called = order
        return _Snapshot()

    def prepare_icd_scope_review(self, ctx, work_order_id, decision):
        return _ScopeReview()

    def get_icd_scope_review(self, ctx, work_order_id):
        return _ScopeReview()

    def run_internal_harness(self, ctx, work_order_id, retrieve=None):
        self.harness_called = (ctx, work_order_id, retrieve)
        return {"artifact_id": "art-1"}


class PrepareTests(unittest.TestCase):
    def _pipeline(self, **attrs):
        p = object.__new__(AppPipeline)
        defaults = {
            "document_generation": _DocGen(),
            "backend": mock.Mock(),
            "circuit_service": None,
            "create_knowledge_base_document_work_order": self._create_order,
            "_icd_template_profile": lambda order: None,
            "_icd_connector_scope_schema": lambda order: None,
            "_icd_front_view_connector_refdes": lambda order: [],
            "_knowledge_base_retriever": lambda *a, **k: object(),
        }
        defaults.update(attrs)
        for k, v in defaults.items():
            setattr(p, k, v)
        return p

    def _create_order(self, ctx, *, knowledge_base_name, **kwargs):
        return _Order()

    def test_prepare_ready_without_scope(self):
        p = self._pipeline()
        result = p.prepare_knowledge_base_document_generation(
            mock.Mock(), knowledge_base_name="shared",
            template_version_id="t1", document_schema_id="s1", document_schema_version="1",
        )
        self.assertEqual(result["stage"], "ready")
        self.assertEqual(result["work_order_id"], "wo-1")

    def test_prepare_icd_sample_blocks(self):
        p = self._pipeline(_icd_template_profile=lambda order: mock.Mock(kind="icd_sample", issues=[], connector_blocks=[]))
        result = p.prepare_knowledge_base_document_generation(
            mock.Mock(), knowledge_base_name="shared",
            template_version_id="t1", document_schema_id="s1", document_schema_version="1",
        )
        self.assertEqual(result["stage"], "template_contract_review_required")

    def test_submit_backgrounds_continue(self):
        p = self._pipeline()
        run_id = p.submit_knowledge_base_document_generation(mock.Mock(), "wo-1")
        self.assertEqual(run_id, "bg-1")
        p.document_generation.worker.submit.assert_called_once()
        args, kwargs = p.document_generation.worker.submit.call_args
        self.assertEqual(args[0], "wo-1")
        # The wrapped lambda must be callable and delegate to continue_...
        p.continue_knowledge_base_document_generation = mock.Mock(return_value={"ok": True})
        args[1]()
        p.continue_knowledge_base_document_generation.assert_called_once()

    def test_prepare_forwards_extra_kwargs_to_create_order(self):
        seen = {}
        p = self._pipeline(
            create_knowledge_base_document_work_order=lambda ctx, **kw: (
                seen.update(kw) or _Order()
            )
        )
        p.prepare_knowledge_base_document_generation(
            mock.Mock(), knowledge_base_name="shared",
            template_version_id="t1", document_schema_id="s1", document_schema_version="1",
            idempotency_key="k",
        )
        self.assertEqual(seen["idempotency_key"], "k")

    def test_auto_generate_delegates_to_prepare_then_harness(self):
        dg = _DocGen()
        p = self._pipeline(document_generation=dg)
        result = p.auto_generate_knowledge_base_document(
            mock.Mock(), knowledge_base_name="shared",
            template_version_id="t1", document_schema_id="s1", document_schema_version="1",
        )
        self.assertEqual(result["artifact_id"], "art-1")
        self.assertTrue(dg.harness_called is not None)