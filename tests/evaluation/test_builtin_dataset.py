import unittest
from pathlib import Path

from src.evaluation.dataset_loader import load_dataset


DATASET = Path("evaluation/datasets/hardware_qa_v1.jsonl")


class BuiltinDatasetTests(unittest.TestCase):
    def test_builtin_dataset_has_exactly_twenty_five_samples(self):
        self.assertEqual(len(load_dataset(DATASET)), 25)

    def test_builtin_dataset_has_required_scenario_coverage(self):
        samples = load_dataset(DATASET)
        tags = {tag for sample in samples for tag in sample.tags}
        self.assertTrue(
            {"circuit", "document", "joint", "multihop", "missing", "conflict", "permission", "direct"}
            <= tags
        )

    def test_sample_ids_are_stable_and_rubrics_are_actionable(self):
        samples = load_dataset(DATASET)
        self.assertTrue(all(sample.id.startswith("hw-v1-") for sample in samples))
        self.assertTrue(all(sample.rubric.required_facts or sample.rubric.must_disclose_missing or sample.rubric.must_disclose_conflicts for sample in samples))

    def test_reference_answers_satisfy_their_literal_required_facts(self):
        samples = load_dataset(DATASET)
        mismatches = {
            sample.id: [
                fact
                for fact in sample.rubric.required_facts
                if fact.casefold() not in sample.reference_answer.casefold()
            ]
            for sample in samples
        }

        self.assertEqual(
            {sample_id: facts for sample_id, facts in mismatches.items() if facts},
            {},
        )

    def test_builtin_dataset_uses_active_adas_scope(self):
        samples = load_dataset(DATASET)
        self.assertTrue(all(sample.kb_name == "ADAS" for sample in samples))
        standard_scope = [
            sample
            for sample in samples
            if sample.request_context.get("user_id") == "eval_dept_47"
        ]
        self.assertTrue(
            all(sample.request_context.get("allowed_kbs") == ["47:ADAS"] for sample in standard_scope)
        )
        self.assertTrue(
            all(
                sample.request_context.get("kb_permissions") == {"47:ADAS": "read"}
                for sample in standard_scope
            )
        )

    def test_permission_isolation_samples_are_explicit_denials(self):
        samples = load_dataset(DATASET)
        denied = {
            sample.id: sample.expected_access
            for sample in samples
            if "permission" in sample.tags
        }

        self.assertEqual(
            denied,
            {
                "hw-v1-permission-empty-scope": "denied",
                "hw-v1-permission-cross-kb-isolation": "denied",
            },
        )

    def test_noisy_pmic_reference_answer_contains_only_verifiable_facts(self):
        sample = next(
            sample
            for sample in load_dataset(DATASET)
            if sample.id == "hw-v1-joint-pmic-noisy-query"
        )

        self.assertEqual(
            sample.reference_answer,
            "LP87702-Q1 是 U1700，输入 VCC3V3，关键输出为 VCC1V1、VCC1V8、VCC50；配置依据 HSI 7.2 节。",
        )


    def test_power_path_calibration_keeps_only_directly_supported_requirements(self):
        samples = {sample.id: sample for sample in load_dataset(DATASET)}

        self.assertIn("VCC0V75", samples["hw-v1-joint-vcc0v75"].question)
        self.assertNotIn(
            "LP87702-Q1",
            samples["hw-v1-circuit-u1700-pins"].rubric.required_facts,
        )
        self.assertNotIn(
            "确定的保护阈值",
            samples["hw-v1-joint-overvoltage"].rubric.forbidden_claims,
        )


if __name__ == "__main__":
    unittest.main()
