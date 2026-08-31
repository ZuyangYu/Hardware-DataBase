import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.agents.tools.spreadsheet_tools import SpreadsheetCellTool, _tokens
from src.evaluation.preflight import EvaluationPreflight
from src.pipelines.document_rag.schemas import RequestContext
from src.evaluation.schemas import EvaluationSample


def _sample(**overrides):
    values = {
        "id": "q1",
        "question": "Question",
        "reference_answer": "Answer",
        "kb_name": "ADAS_new",
        "required_evidence_types": ["circuit_design"],
        "request_context": {
            "user_id": "evaluation",
            "department_id": 96,
            "allowed_kbs": ["96:ADAS_new"],
            "kb_permissions": {"96:ADAS_new": "read"},
        },
    }
    values.update(overrides)
    return EvaluationSample(**values)


class _CatalogTool:
    def __init__(self, sources):
        self.sources = sources

    def scan(self, kb_name, ctx):
        return {"sources": self.sources}


class _Pipeline:
    def __init__(self, sources):
        self.agent = type("Agent", (), {"catalog_tool": _CatalogTool(sources)})()


class _SpreadsheetService:
    def __init__(self, path: Path):
        self.path = path

    def db_path(self, department_id, kb_name, create=False):
        return str(self.path)


class EvaluationPreflightTests(unittest.TestCase):
    def test_rejects_required_evidence_sample_when_catalog_is_empty(self):
        errors = EvaluationPreflight(lambda: _Pipeline([])).validate([_sample()])

        self.assertEqual(errors, ["q1: no discoverable sources for ADAS_new"])

    def test_accepts_required_evidence_sample_when_catalog_has_source(self):
        errors = EvaluationPreflight(lambda: _Pipeline([{"document_name": "schematic"}])).validate(
            [_sample()]
        )

        self.assertEqual(errors, [])

    def test_rejects_required_evidence_sample_without_scoped_read_permission(self):
        sample = _sample(
            request_context={
                "user_id": "evaluation",
                "department_id": 96,
                "allowed_kbs": [],
                "kb_permissions": {},
            }
        )

        errors = EvaluationPreflight(lambda: _Pipeline([{"document_name": "schematic"}])).validate(
            [sample]
        )

        self.assertEqual(errors, ["q1: request context cannot read ADAS_new"])

    def test_skips_catalog_requirement_for_expected_no_evidence_sample(self):
        sample = _sample(required_evidence_types=[], request_context={})

        errors = EvaluationPreflight(lambda: _Pipeline([])).validate([sample])

        self.assertEqual(errors, [])

    def test_scoring_preflight_reports_missing_optional_stack(self):
        def find_spec(name):
            return None if name == "ragas" else object()

        with patch("src.evaluation.preflight.importlib.util.find_spec", side_effect=find_spec):
            errors = EvaluationPreflight.validate_scoring(config=object())

        self.assertEqual(
            errors,
            ["评分依赖缺失：ragas；请运行 uv sync --group eval"],
        )

    def test_scoring_preflight_reports_invalid_environment_configuration(self):
        with (
            patch("src.evaluation.preflight.importlib.util.find_spec", return_value=object()),
            patch(
                "src.evaluation.preflight.EvaluationConfig.from_environment",
                side_effect=ValueError("EVAL_EMBEDDING_MODEL is required"),
            ),
        ):
            errors = EvaluationPreflight.validate_scoring()

        self.assertEqual(errors, ["评估配置无效：EVAL_EMBEDDING_MODEL is required"])

    def test_spreadsheet_tokens_keep_hardware_terms_without_query_fragments(self):
        tokens = _tokens("MCU 的供电电源有哪些？列出电源网络名")

        self.assertIn("mcu", tokens)
        self.assertIn("供电", tokens)
        self.assertIn("电源", tokens)
        self.assertIn("网络", tokens)
        self.assertNotIn("电电", tokens)
        self.assertNotIn("围使", _tokens("EQ6L 外围使用了几片 DDR，容量多大？"))
        self.assertNotIn("络名", tokens)

    def test_spreadsheet_cell_tool_ranks_relevant_rows_before_template_noise(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "table_indexes.db"
            with sqlite3.connect(db_path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE table_documents (
                        record_id INTEGER PRIMARY KEY,
                        document_name TEXT,
                        source_group TEXT
                    );
                    CREATE TABLE table_cells (
                        id INTEGER PRIMARY KEY,
                        record_id INTEGER,
                        sheet_name TEXT,
                        cell_ref TEXT,
                        row_index INTEGER,
                        col_index INTEGER,
                        header TEXT,
                        value TEXT,
                        raw_value TEXT,
                        number_format TEXT
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO table_documents VALUES (?, ?, ?)",
                    (42, "architecture.xlsx", "design data"),
                )
                connection.executemany(
                    "INSERT INTO table_cells VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (1, 42, "Cover", "A1", 1, 1, "电源网络", "模板填写说明", "模板填写说明", ""),
                        (2, 42, "Power", "C12", 12, 3, "电源网络", "VCC3V3_MCU", "VCC3V3_MCU", ""),
                    ],
                )

            context = RequestContext(metadata={"department_id": 47})
            evidence = SpreadsheetCellTool(_SpreadsheetService(db_path)).run(
                "MCU 的供电电源有哪些？列出电源网络名",
                "ADAS",
                context,
                top_k=1,
                filters={"record_id": 42},
            )

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].locator["cell_ref"], "C12")
        self.assertIn("VCC3V3_MCU", evidence[0].content)


if __name__ == "__main__":
    unittest.main()
