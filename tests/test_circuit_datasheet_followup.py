import unittest

from src.agents.graph import _derived_datasheet_calls, _is_matching_datasheet_capability, retrieve_evidence
from src.agents.state import Evidence


class CircuitDatasheetFollowupTests(unittest.TestCase):
    def test_protection_question_generates_no_document_query_without_verified_links(self):
        calls = _derived_datasheet_calls(
            "电源输出电路是否有短地保护？",
            [{"metadata": {"evidence_kind": "derived_topology", "capability_candidate": True, "part_numbers": ["TPS22919", "TPS22919"]}}],
        )

        self.assertEqual(calls, [])

    def test_verified_links_unlock_record_filtered_part_lookup(self):
        calls = _derived_datasheet_calls(
            "电源输出电路是否有短地保护？",
            [{"metadata": {"evidence_kind": "derived_topology", "capability_candidate": True, "part_numbers": ["TPS22919"]}}],
            verified_links=[{"refdes": "U1", "part_number": "TPS22919", "record_ids": [42]}],
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool_name"], "document_rag")
        self.assertIn("TPS22919", calls[0]["query"])
        self.assertEqual(calls[0]["filters"], {"allowed_record_ids": [42]})

    def test_parts_without_verified_links_are_never_looked_up(self):
        calls = _derived_datasheet_calls(
            "电源输出电路是否有短地保护？",
            [{"metadata": {"evidence_kind": "derived_topology", "capability_candidate": True, "part_numbers": ["TPS22919"]}}],
            verified_links=[{"refdes": "U9", "part_number": "OTHER-123", "record_ids": [7]}],
        )

        self.assertEqual(calls, [])

    def test_bias_question_does_not_generate_manual_followup(self):
        calls = _derived_datasheet_calls(
            "输入输出信号是否共用上拉电源？",
            [{"metadata": {"evidence_kind": "derived_topology", "part_numbers": ["RES0402"]}}],
        )

        self.assertEqual(calls, [])

    def test_datasheet_claim_requires_matching_part_and_capability_text(self):
        self.assertTrue(_is_matching_datasheet_capability("TPS22919.pdf", "TPS22919 provides current limit and thermal shutdown.", "TPS22919"))
        self.assertFalse(_is_matching_datasheet_capability("unrelated.pdf", "Current limit is available.", "TPS22919"))
        self.assertFalse(_is_matching_datasheet_capability("TPS22919.pdf", "Pin description and package dimensions.", "TPS22919"))

    def test_retrieval_skips_derived_lookup_without_verified_links(self):
        document = _DocumentTool()
        state = {
            "kb_name": "kb_hw",
            "user_query": "电源输出电路是否有短地保护？",
            "source_plan": {"source_plan": [{"tool_calls": [{"tool_name": "circuit_query", "query": "电源输出电路是否有短地保护？"}]}]},
            "evidence": [],
            "trace": [],
        }

        result = retrieve_evidence(state, {"circuit_query": _CircuitTool(), "document_rag": document})

        self.assertEqual(document.queries, [])
        self.assertTrue(
            any(
                item.get("status") == "skipped_no_verified_datasheet_link"
                for item in result["retrieval_diagnostics"]
            )
        )

    def test_retrieval_runs_record_filtered_lookup_with_verified_links(self):
        document = _DocumentTool()
        state = {
            "kb_name": "kb_hw",
            "user_query": "电源输出电路是否有短地保护？",
            "source_plan": {"source_plan": [{"tool_calls": [{"tool_name": "circuit_query", "query": "电源输出电路是否有短地保护？"}]}]},
            "evidence": [],
            "trace": [],
            "_verified_datasheet_links": [{"refdes": "U1", "part_number": "TPS22919", "record_ids": [42]}],
        }

        retrieve_evidence(state, {"circuit_query": _CircuitTool(), "document_rag": document})

        self.assertEqual(len(document.queries), 1)
        self.assertIn("TPS22919", document.queries[0])
        self.assertEqual(document.filters[-1], {"allowed_record_ids": [42]})


class _CircuitTool:
    def run(self, *args, **kwargs):
        return [
            Evidence(
                id="circuit:1:topology:protection_load_switch:U1",
                content="Observed load switch U1.",
                source_name="main_board.edf",
                content_kind="circuit_design",
                processor_kind="circuit_design",
                metadata={"evidence_kind": "derived_topology", "capability_candidate": True, "part_numbers": ["TPS22919"]},
            )
        ]


class _DocumentTool:
    def __init__(self):
        self.queries = []
        self.filters = []

    def run(self, query, *args, filters=None, **kwargs):
        self.queries.append(query)
        self.filters.append(filters)
        return [
            Evidence(
                id="document:manual:1",
                content="TPS22919 supports current limit.",
                source_name="TPS22919.pdf",
                content_kind="document_text",
                processor_kind="ragflow",
            )
        ]


if __name__ == "__main__":
    unittest.main()
