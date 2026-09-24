import json
import tempfile
import unittest
from pathlib import Path

from src.evaluation.cli import main, parse_thresholds
from src.evaluation.schemas import EvaluationSummary, GateResult


class FakePaths:
    report_html = Path()


class FakeService:
    last_kwargs = None

    @staticmethod
    def validate(path):
        return []

    def run(self, dataset, output, **kwargs):
        type(self).last_kwargs = kwargs
        failed = kwargs.get("fail_on_threshold", False)
        summary = EvaluationSummary(
            run_id="run-1",
            gate=GateResult(passed=False, exit_code=2 if failed else 0),
        )
        return summary, [], FakePaths()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.dataset = self.root / "dataset.jsonl"
        self.dataset.write_text(
            json.dumps({"id": "q1", "question": "Q", "reference_answer": "A", "kb_name": "kb"}),
            encoding="utf-8",
        )

    def test_validate_returns_zero_for_valid_dataset(self):
        code = main(["validate", "--dataset", str(self.dataset)])
        self.assertEqual(code, 0)

    def test_validate_returns_one_for_invalid_dataset(self):
        self.dataset.write_text("{invalid}", encoding="utf-8")
        code = main(["validate", "--dataset", str(self.dataset)])
        self.assertEqual(code, 1)

    def test_run_only_propagates_gate_exit_with_flag(self):
        def factory():
            return FakeService()

        normal = main(
            ["run", "--dataset", str(self.dataset), "--output", str(self.root)],
            service_factory=factory,
        )
        gated = main(
            [
                "run",
                "--dataset",
                str(self.dataset),
                "--output",
                str(self.root),
                "--fail-on-threshold",
            ],
            service_factory=factory,
        )

        self.assertEqual(normal, 0)
        self.assertEqual(gated, 2)

    def test_filters_and_thresholds_are_forwarded(self):
        main(
            [
                "run",
                "--dataset",
                str(self.dataset),
                "--output",
                str(self.root),
                "--sample-id",
                "q1",
                "--tag",
                "joint",
                "--threshold",
                "faithfulness=0.8",
            ],
            service_factory=lambda: FakeService(),
        )

        self.assertEqual(FakeService.last_kwargs["sample_ids"], {"q1"})
        self.assertEqual(FakeService.last_kwargs["tags"], {"joint"})
        self.assertEqual(FakeService.last_kwargs["thresholds"], {"faithfulness": 0.8})

    def test_parse_thresholds_rejects_out_of_range_value(self):
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            parse_thresholds(["faithfulness=1.2"])


if __name__ == "__main__":
    unittest.main()
