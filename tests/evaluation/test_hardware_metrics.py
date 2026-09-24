import unittest

from src.evaluation.hardware_metrics import score_document_generation, score_hardware_rules
from src.evaluation.schemas import (
    AnswerSnapshot,
    DocumentGenerationEvalRecord,
    DocumentGenerationSnapshot,
    EvaluationSample,
    SampleRubric,
)


def _sample(**rubric_overrides):
    rubric = SampleRubric(**rubric_overrides)
    return EvaluationSample(
        id="q1",
        question="Q",
        reference_answer="A",
        kb_name="ADAS",
        rubric=rubric,
    )


def _snapshot(answer: str, evidence=None):
    return AnswerSnapshot(
        sample_id="q1",
        question="Q",
        kb_name="ADAS",
        response=answer,
        evidence=evidence or [],
    )


def _by_name(results, name):
    return next(result for result in results if result.metric_name == name)


class HardwareMetricTests(unittest.TestCase):
    def test_returns_only_lexical_metrics(self):
        sample = _sample(
            required_facts=["U1700", "LP87702-Q1", "VCC1V1"],
            must_disclose_missing=True,
            must_disclose_conflicts=True,
            forbidden_claims=["TC377"],
        )

        names = [metric.metric_name for metric in score_hardware_rules(sample, _snapshot("answer"))]

        self.assertEqual(names, ["forbidden_claims", "evidence_consistency"])

    def test_forbidden_claims_metric_flags_exact_wrong_tokens(self):
        sample = _sample(forbidden_claims=["TC377"])

        clean = _by_name(score_hardware_rules(sample, _snapshot("U900 是 TC367")), "forbidden_claims")
        hit = _by_name(score_hardware_rules(sample, _snapshot("U900 是 TC377")), "forbidden_claims")

        self.assertEqual(clean.score, 1.0)
        self.assertEqual(hit.score, 0.0)
        self.assertEqual(hit.details["forbidden_hits"], ["TC377"])

    def test_forbidden_claims_not_applicable_without_claims(self):
        metric = _by_name(score_hardware_rules(_sample(), _snapshot("answer")), "forbidden_claims")
        self.assertEqual(metric.status, "not_applicable")

    def test_forbidden_claim_matching_normalizes_case_and_width(self):
        sample = _sample(forbidden_claims=["TC377"])

        metric = _by_name(
            score_hardware_rules(sample, _snapshot("主控是 ｔｃ３７７ 系列")), "forbidden_claims"
        )

        self.assertEqual(metric.score, 0.0)

    def test_evidence_consistency_reports_missing_evidence_types(self):
        sample = EvaluationSample(
            id="q1",
            question="Q",
            reference_answer="A",
            kb_name="ADAS",
            required_evidence_types=["circuit_design", "document"],
        )
        evidence = [{"content_kind": "circuit_design", "content": "net"}]

        metric = _by_name(score_hardware_rules(sample, _snapshot("answer", evidence)), "evidence_consistency")

        self.assertEqual(metric.score, 0.5)
        self.assertEqual(metric.details["missing_evidence_types"], ["document"])

    def test_evidence_consistency_normalizes_document_text_alias(self):
        sample = EvaluationSample(
            id="q1",
            question="Q",
            reference_answer="A",
            kb_name="ADAS",
            required_evidence_types=["document"],
        )
        evidence = [{"content_kind": "document_text", "content": "document evidence"}]

        metric = _by_name(
            score_hardware_rules(sample, _snapshot("answer", evidence)),
            "evidence_consistency",
        )

        self.assertEqual(metric.score, 1.0)
        self.assertEqual(metric.details["missing_evidence_types"], [])

    def test_document_generation_metrics_measure_mapping_evidence_and_safety(self):
        record = DocumentGenerationEvalRecord(
            id="doc-1",
            template_fixture="current_review.xlsx",
            field_id="rated_current",
            expected_value="10 A",
            allowed_sources=["power_spec.pdf"],
        )
        snapshot = DocumentGenerationSnapshot(
            sample_id="doc-1",
            template_fixture="current_review.xlsx",
            mapped_field_id="rated_current",
            filled_value="10 A",
            evidence_sources=["power_spec.pdf"],
            retrieved_evidence_sources=["power_spec.pdf"],
            attempted_fill_count=1,
            auto_approved=True,
        )

        metrics = {metric.metric_name: metric.score for metric in score_document_generation(record, snapshot)}

        self.assertEqual(metrics["template_mapping_precision"], 1.0)
        self.assertEqual(metrics["field_recall_at_k"], 1.0)
        self.assertEqual(metrics["evidence_support_rate"], 1.0)
        self.assertEqual(metrics["fixed_content_overwrite_rate"], 0.0)
        self.assertEqual(metrics["auto_approval_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
