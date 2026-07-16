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


if __name__ == "__main__":
    unittest.main()
