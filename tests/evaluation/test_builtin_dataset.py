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

    def test_builtin_dataset_uses_active_adas_new_scope(self):
        samples = load_dataset(DATASET)
        self.assertTrue(all(sample.kb_name == "ADAS_new" for sample in samples))
        standard_scope = [
            sample
            for sample in samples
            if sample.request_context.get("user_id") == "eval_dept_96"
        ]
        self.assertTrue(
            all(sample.request_context.get("allowed_kbs") == ["96:ADAS_new"] for sample in standard_scope)
        )
        self.assertTrue(
            all(
                sample.request_context.get("kb_permissions") == {"96:ADAS_new": "read"}
                for sample in standard_scope
            )
        )


if __name__ == "__main__":
    unittest.main()
