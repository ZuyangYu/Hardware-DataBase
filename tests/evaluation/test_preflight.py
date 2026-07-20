import unittest

from src.evaluation.preflight import EvaluationPreflight
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


if __name__ == "__main__":
    unittest.main()
