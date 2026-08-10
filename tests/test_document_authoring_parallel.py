from __future__ import annotations

import pytest

from src.document_authoring.harness.runtime import InternalDocumentHarnessRuntime
from src.document_authoring.models import (
    DocumentSchema,
    DocumentWorkOrder,
    HarnessPolicy,
    KnowledgeBaseSourceSnapshot,
    TemplateVersion,
)


def test_harness_policy_defaults_to_three_parallel_units():
    policy = HarnessPolicy(harness_policy_id="parallel", version="1")

    assert policy.max_parallel_units == 3


@pytest.mark.parametrize("parallel_units", [0, 5])
def test_harness_policy_rejects_parallelism_outside_measured_limit(parallel_units: int):
    with pytest.raises(ValueError, match="max_parallel_units"):
        HarnessPolicy(
            harness_policy_id="parallel",
            version="1",
            max_parallel_units=parallel_units,
        )


def test_run_manifest_freezes_policy_parallelism():
    policy = HarnessPolicy(harness_policy_id="parallel", version="1", max_parallel_units=4)
    order = DocumentWorkOrder(
        work_order_id="order", scope_type="knowledge_base", knowledge_base_name="ADAS",
        project_id=None, baseline_id=None, baseline_content_hash="", source_set_snapshot_id="snapshot",
        template_version_id="template", document_schema_id="schema", document_schema_version="1",
        template_schema_id="schema", template_schema_version="1", retrieval_policy_version="1",
        renderer_policy_version="1", target_format="xlsx", execution_mode="internal_harness",
        harness_policy_id=policy.harness_policy_id, harness_policy_version=policy.version, created_by="tester",
    )
    snapshot = KnowledgeBaseSourceSnapshot.create(
        tenant_id="default", knowledge_base_name="ADAS", source_names=["design.pdf"], created_by="tester",
    )
    template = TemplateVersion(
        template_version_id="template", template_id="template", format="xlsx", content_hash="template-hash",
        template_schema_id="schema", template_schema_version="1", renderer_policy_id="renderer",
    )
    schema = DocumentSchema(document_schema_id="schema", version="1", document_type="checklist", status="approved")

    manifest = InternalDocumentHarnessRuntime.build_manifest(order, policy, snapshot, template, schema)

    assert manifest.max_parallel_units == 4
