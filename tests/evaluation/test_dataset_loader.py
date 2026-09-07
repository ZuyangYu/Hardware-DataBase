import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from src.evaluation.dataset_loader import (
    DatasetValidationError,
    load_dataset,
    load_document_generation_dataset,
    validate_dataset,
)
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

    def test_context_recall_uses_reference_answer_without_reference_contexts(self):
        path = self._write([
            {
                "id": "q1",
                "question": "Q",
                "reference_answer": "A",
                "kb_name": "kb",
                "metrics": ["context_recall"],
            }
        ])

        samples = load_dataset(path)
        self.assertEqual(samples[0].reference_contexts, [])

    def test_document_generation_dataset_preserves_expected_value_and_allowed_sources(self):
        path = self._write([{
            "id": "doc-1",
            "template_fixture": "current_review.xlsx",
            "field_id": "rated_current",
            "expected_value": "10 A",
            "allowed_sources": ["power_spec.pdf"],
        }])

        records = load_document_generation_dataset(path)

        self.assertEqual(records[0].expected_value, "10 A")
        self.assertEqual(records[0].allowed_sources, ["power_spec.pdf"])

    def test_builtin_document_generation_dataset_loads_independently(self):
        records = load_document_generation_dataset(
            Path("evaluation/datasets/document_generation_v1.jsonl")
        )

        self.assertEqual(len(records), 3)
        self.assertEqual(records[0].field_id, "rated_current")


if __name__ == "__main__":
    unittest.main()
