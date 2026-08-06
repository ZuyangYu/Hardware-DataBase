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
    def test_completeness_lists_missing_facts(self):
        sample = _sample(required_facts=["U1700", "LP87702-Q1", "VCC1V1"])

        metric = _by_name(score_hardware_rules(sample, _snapshot("U1700 是 LP87702-Q1")), "completeness")

        self.assertAlmostEqual(metric.score, 2 / 3)
        self.assertEqual(metric.details["missing_facts"], ["VCC1V1"])

    def test_completeness_normalizes_case_and_unicode_width(self):
        sample = _sample(required_facts=["VCC1V1", "LP87702-Q1"])

        metric = _by_name(score_hardware_rules(sample, _snapshot("vcc1v1，ＬＰ８７７０２－Ｑ１")), "completeness")

        self.assertEqual(metric.score, 1.0)

    def test_missing_information_honesty_rejects_forbidden_claim(self):
        sample = _sample(must_disclose_missing=True, forbidden_claims=["12.5元"])

        metric = _by_name(
            score_hardware_rules(sample, _snapshot("未找到 BOM，但单价为 12.5元")),
            "missing_information_honesty",
        )

        self.assertEqual(metric.score, 0.0)
        self.assertEqual(metric.details["forbidden_hits"], ["12.5元"])

    def test_missing_information_honesty_accepts_explicit_gap(self):
        sample = _sample(must_disclose_missing=True)

        metric = _by_name(
            score_hardware_rules(sample, _snapshot("当前知识库未找到替代料和价格证据。")),
            "missing_information_honesty",
        )

        self.assertEqual(metric.score, 1.0)

    def test_conflict_disclosure_is_not_applicable_without_requirement(self):
        metric = _by_name(score_hardware_rules(_sample(), _snapshot("answer")), "conflict_disclosure")
        self.assertEqual(metric.status, "not_applicable")

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
