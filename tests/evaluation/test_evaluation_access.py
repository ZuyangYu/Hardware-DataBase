import pytest

from src.core.auth import KnowledgeBaseSummary
from src.evaluation.access import (
    assess_access,
    build_evaluation_context,
    normalize_sample_for_binding,
    resolve_knowledge_base,
    select_evaluation_samples,
)
from src.evaluation.schemas import EvaluationSample


def _kb(kb_id: int, name: str, department_id: int) -> KnowledgeBaseSummary:
    return KnowledgeBaseSummary(
        name=name,
        kb_id=kb_id,
        department_id=department_id,
        department_name=f"部门-{department_id}",
        registered=True,
        physical_exists=False,
    )


def _sample(sample_id: str, kb_name: str = "ADAS", **overrides) -> EvaluationSample:
    values = {
        "id": sample_id,
        "question": f"问题 {sample_id}",
        "reference_answer": f"答案 {sample_id}",
        "kb_name": kb_name,
    }
    values.update(overrides)
    return EvaluationSample(**values)


def _binding(kb_id: int, name: str, department_id: int):
    return resolve_knowledge_base([_kb(kb_id, name, department_id)], kb_id=kb_id, kb_name=name)


def test_resolve_knowledge_base_uses_id_and_real_department_and_rejects_name_mismatch():
    binding = resolve_knowledge_base(
        [_kb(1, "ADAS", 47), _kb(2, "ADAS", 100)],
        kb_id=2,
        kb_name="ADAS",
    )

    assert binding.kb_id == 2
    assert binding.kb_name == "ADAS"
    assert binding.department_id == 100

    with pytest.raises(ValueError, match="不匹配"):
        resolve_knowledge_base([_kb(1, "ADAS", 47)], kb_id=1, kb_name="OTHER")


def test_name_only_resolution_rejects_cross_department_duplicates():
    with pytest.raises(ValueError, match="kb_id"):
        resolve_knowledge_base([_kb(1, "ADAS", 47), _kb(2, "ADAS", 100)], kb_name="ADAS")


def test_select_evaluation_samples_filters_by_kb_before_optional_filters():
    selection = select_evaluation_samples(
        [_sample("adas-1"), _sample("other-1", "OTHER"), _sample("adas-2")],
        _binding(1, "ADAS", 47),
        sample_ids=["adas-2", "other-1"],
    )

    assert [sample.id for sample in selection.samples] == ["adas-2"]
    assert selection.dataset_total_count == 3
    assert selection.matched_sample_count == 2
    assert selection.dataset_sample_count == 1
    assert selection.filtered_sample_count == 2
    assert selection.normal_sample_count == 1
    assert selection.expected_denied_sample_count == 0


def test_normalization_uses_selected_scope_and_drops_foreign_dataset_permissions():
    sample = _sample(
        "legacy",
        request_context={
            "user_id": "attacker",
            "roles": ["system_admin"],
            "department_id": 47,
            "allowed_kbs": ["47:ADAS", "100:OTHER"],
            "kb_permissions": {"47:ADAS": "admin", "100:OTHER": "read"},
        },
    )

    normalized = normalize_sample_for_binding(sample, _binding(2, "ADAS", 100))
    context = build_evaluation_context(normalized)

    assert context.user_id == "evaluation"
    assert context.roles == ["user"]
    assert context.metadata["department_id"] == 100
    assert context.allowed_kbs == ["100:ADAS"]
    assert context.kb_permissions == {"100:ADAS": "read"}
    assert context.has_kb_permission("ADAS", "read")
    assert not context.has_kb_permission("OTHER", "read")


def test_denied_sample_cannot_reenable_selected_kb_with_dataset_context():
    sample = _sample(
        "denied",
        expected_access="denied",
        request_context={
            "department_id": 47,
            "allowed_kbs": ["100:ADAS"],
            "kb_permissions": {"100:ADAS": "read"},
        },
    )

    normalized = normalize_sample_for_binding(sample, _binding(2, "ADAS", 100))
    context = build_evaluation_context(normalized)

    assert normalized.expected_access == "denied"
    assert context.allowed_kbs == []
    assert context.kb_permissions == {}
    assert not context.has_kb_permission("ADAS", "read")


def test_assess_access_uses_structured_retrieval_state_not_answer_keywords():
    sample = _sample("denied", expected_access="denied")
    context = build_evaluation_context(
        normalize_sample_for_binding(sample, _binding(1, "ADAS", 47))
    )

    denied = assess_access(
        sample,
        context,
        {"status": "success", "evidence": []},
        response="没有权限，但这段话本身不能决定权限结果",
    )
    leaked = assess_access(
        sample,
        context,
        {"status": "success", "evidence": [{"content": "secret evidence"}]},
        response="没有权限",
    )
    explicitly_denied_but_leaked = assess_access(
        sample,
        context,
        {
            "access_decision": "denied",
            "status": "success",
            "evidence": [{"content": "secret evidence"}],
        },
    )
    backend_denial = assess_access(
        _sample("allowed"),
        build_evaluation_context(
            normalize_sample_for_binding(_sample("allowed"), _binding(1, "ADAS", 47))
        ),
        {"status": "permission_denied", "evidence": []},
    )

    assert denied["observed"] == "denied"
    assert leaked["observed"] == "allowed"
    assert explicitly_denied_but_leaked["observed"] == "allowed"
    assert backend_denial["observed"] == "denied"
