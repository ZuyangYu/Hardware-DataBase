import re
import unittest
from pathlib import Path

from src.evaluation.dataset_loader import load_dataset


DATASET = Path("evaluation/datasets/aaa_hard_eval_v1.jsonl")


def _contains(text: str, token: str) -> bool:
    """镜像 hardware_metrics._contains 的归一化规则（含空白压缩）。"""

    def norm(value: str) -> str:
        compact = re.sub(r"\s+", "", value)
        return compact.casefold()

    return norm(token) in norm(text)


class BuiltinDatasetTests(unittest.TestCase):
    def test_builtin_dataset_sample_count(self):
        self.assertEqual(len(load_dataset(DATASET)), 47)

    def test_builtin_dataset_has_required_scenario_coverage(self):
        samples = load_dataset(DATASET)
        tags = {tag for sample in samples for tag in sample.tags}
        self.assertTrue(
            {
                "circuit",
                "document",
                "spreadsheet",
                "cross-source",
                "multi-hop",
                "conflict",
                "missing-evidence",
                "permission",
                "trap",
            }
            <= tags
        )

    def test_sample_ids_are_stable_and_rubrics_are_actionable(self):
        samples = load_dataset(DATASET)
        self.assertTrue(all(sample.id.startswith("aaa-") for sample in samples))
        actionable = all(
            sample.rubric.required_facts
            or sample.rubric.must_disclose_missing
            or sample.rubric.must_disclose_conflicts
            or sample.expected_access == "denied"
            for sample in samples
        )
        self.assertTrue(actionable)

    def test_reference_answers_satisfy_their_literal_required_facts(self):
        samples = load_dataset(DATASET)
        mismatches = {
            sample.id: [
                fact
                for fact in sample.rubric.required_facts
                if not _contains(sample.reference_answer, fact)
            ]
            for sample in samples
        }

        self.assertEqual(
            {sample_id: facts for sample_id, facts in mismatches.items() if facts},
            {},
        )

    def test_builtin_dataset_uses_active_aaa_scope(self):
        samples = load_dataset(DATASET)
        self.assertTrue(all(sample.kb_name == "AAA" for sample in samples))
        standard_scope = [
            sample
            for sample in samples
            if sample.request_context.get("user_id") == "aaa_admin"
        ]
        self.assertTrue(
            all(
                sample.request_context.get("allowed_kbs") == ["1739:AAA"]
                for sample in standard_scope
            )
        )
        self.assertTrue(
            all(
                sample.request_context.get("kb_permissions") == {"1739:AAA": "read"}
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

        self.assertEqual(denied, {"aaa-p-denied-other-kb": "denied"})

    def test_conflict_sample_requires_both_models_and_disclosure(self):
        sample = next(
            sample
            for sample in load_dataset(DATASET)
            if sample.id == "aaa-d-conflict-mcu-model"
        )

        self.assertTrue(sample.rubric.must_disclose_conflicts)
        answer = sample.reference_answer
        for token in ("TC367", "TC377"):
            self.assertIn(token, answer)

    def test_trap_sample_keeps_distinct_sampling_resistor_facts(self):
        sample = next(
            sample
            for sample in load_dataset(DATASET)
            if sample.id == "aaa-x-eq6-1v8-two-nets"
        )

        for token in ("R1111", "R1141", "AN3", "AN5"):
            self.assertIn(token, sample.rubric.required_facts)


if __name__ == "__main__":
    unittest.main()
