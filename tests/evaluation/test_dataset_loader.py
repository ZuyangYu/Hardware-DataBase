import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from src.evaluation.dataset_loader import DatasetValidationError, load_dataset, validate_dataset
from src.evaluation.schemas import EvaluationSample


class DatasetLoaderTests(unittest.TestCase):
    def _write(self, rows: list[dict] | list[str]) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "eval.jsonl"
        lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False) for row in rows]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def test_loads_valid_rows_and_ignores_blank_lines(self):
        path = self._write([
            {"id": "q1", "question": "问题", "reference_answer": "答案", "kb_name": "ADAS"},
            "",
            {"id": "q2", "question": "问题2", "reference_answer": "答案2", "kb_name": "ADAS"},
        ])

        samples = load_dataset(path)

        self.assertEqual([sample.id for sample in samples], ["q1", "q2"])

    def test_rejects_duplicate_ids(self):
        path = self._write([
            {"id": "q1", "question": "Q", "reference_answer": "A", "kb_name": "kb"},
            {"id": "q1", "question": "Q2", "reference_answer": "A2", "kb_name": "kb"},
        ])

        with self.assertRaisesRegex(DatasetValidationError, "duplicate sample id 'q1'.*line 2"):
            load_dataset(path)

    def test_invalid_json_reports_line_number(self):
        path = self._write(["{not-json}"])

        errors = validate_dataset(path)

        self.assertEqual(len(errors), 1)
        self.assertIn("line 1", errors[0])

    def test_request_context_rejects_secret_fields(self):
        with self.assertRaises(ValidationError):
            EvaluationSample(
                id="q1",
                question="Q",
                reference_answer="A",
                kb_name="kb",
                request_context={"api_key": "secret"},
            )

    def test_reference_contexts_are_required_only_when_context_recall_is_selected(self):
        path = self._write([
            {
                "id": "q1",
                "question": "Q",
                "reference_answer": "A",
                "kb_name": "kb",
                "metrics": ["context_recall"],
            }
        ])

        with self.assertRaisesRegex(DatasetValidationError, "reference_contexts"):
            load_dataset(path)


if __name__ == "__main__":
    unittest.main()
