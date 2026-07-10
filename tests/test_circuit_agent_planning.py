import unittest
from dataclasses import dataclass

from src.agents.graph import _expected_evidence, plan_next_retrieval, plan_source_selection
from src.agents.tools.pipeline_catalog_tool import PipelineCatalogTool
from src.pipelines.document_rag.schemas import RequestContext


@dataclass
class _Record:
    id: int = 1
    document_name: str = "main_board.edf"
    original_file_name: str = "main_board.edf"
    processor_kind: str = "circuit_design"
    content_kind: str = "circuit_design"
    dataset_kind: str = "circuit"
    source_group: str = "netlist data"
    status: str = "indexed"
    local_path: str = "netlist-data/main_board.edf"
    file_size: int = 128
    department_id: str = "dept_hw"


class _DocumentStore:
    def list_documents(self, kb_name, department_id=None):
        return [_Record(department_id=department_id or "dept_hw")]


class _SpreadsheetService:
    def get_document_profile(self, record):
        return {}


class _FakeLLM:
    def chat(self, messages):
        return '{"tool_calls": [{"tool_name": "circuit_query", "query": "CAN0", "source_name": "manual.pdf", "reason": "bad pairing"}]}'


class _CatalogTool:
    def scan(self, kb_name, ctx):
        return {
            "sources": [
                {
                    "document_name": "manual.pdf",
                    "original_file_name": "manual.pdf",
                    "record_id": 11,
                    "processor_kind": "ragflow",
                    "content_kind": "document_text",
                    "source_group": "docs",
                    "status": "completed",
                }
            ]
        }


class CircuitAgentPlanningTests(unittest.TestCase):
    def test_expected_evidence_for_refdes_and_bom_includes_circuit_and_spreadsheet(self):
        self.assertEqual(
            set(_expected_evidence("U1200 BOM quantity")),
            {"circuit_design", "spreadsheet_table"},
        )

    def test_plan_source_selection_skips_failed_circuit_sources(self):
        state = {
            "user_query": "CAN0 connection",
            "question_analysis": {
                "sub_questions": [
                    {
                        "id": "sq_1",
                        "question": "CAN0 connection",
                        "expected_evidence": ["circuit_design"],
                    }
                ]
            },
            "catalog": {
                "sources": [
                    {
                        "document_name": "bad.edf",
                        "processor_kind": "circuit_design",
                        "content_kind": "circuit_design",
                        "status": "failed",
                    },
                    {
                        "document_name": "good.edf",
                        "processor_kind": "circuit_design",
                        "content_kind": "circuit_design",
                        "status": "indexed",
                    },
                ]
            },
            "trace": [],
        }

        result = plan_source_selection(state)

        planned = result["source_plan"]["source_plan"]
        self.assertEqual([item["source_name"] for item in planned], ["good.edf"])

    def test_plan_source_selection_fans_out_circuit_and_document_sources(self):
        state = {
            "user_query": "CAN0 connection design report",
            "question_analysis": {
                "sub_questions": [
                    {
                        "id": "sq_1",
                        "question": "CAN0 connection design report",
                        "expected_evidence": ["circuit_design", "document_text"],
                    }
                ]
            },
            "catalog": {
                "sources": [
                    {
                        "document_name": "main_board.edf",
                        "processor_kind": "circuit_design",
                        "content_kind": "circuit_design",
                        "status": "indexed",
                    },
                    {
                        "document_name": "design_report.pdf",
                        "processor_kind": "ragflow",
                        "content_kind": "document_text",
                        "status": "completed",
                    },
                ]
            },
            "trace": [],
        }

        result = plan_source_selection(state)

        tools = {
            call["tool_name"]
            for item in result["source_plan"]["source_plan"]
            for call in item["tool_calls"]
        }
        self.assertEqual(tools, {"circuit_query", "document_rag"})

    def test_plan_next_retrieval_rejects_circuit_tool_for_document_source(self):
        state = {
            "kb_name": "kb_hw",
            "user_query": "CAN0 connection",
            "sufficiency": {"suggested_queries": [{"query": "CAN0"}]},
            "retrieval_round": 1,
            "retrieval_diagnostics": [],
            "merged_evidence": [],
            "evidence": [],
            "source_plan": {"source_plan": []},
            "trace": [],
        }

        result = plan_next_retrieval(state, _FakeLLM(), _CatalogTool())

        self.assertEqual(result["next_retrieval_calls"], [])

    def test_catalog_summary_counts_circuit_sources(self):
        catalog = PipelineCatalogTool(
            document_store=_DocumentStore(),
            spreadsheet_service=_SpreadsheetService(),
        )

        result = catalog.scan(
            "kb_hw",
            RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
        )

        self.assertEqual(result["summary"]["circuits"], 1)


if __name__ == "__main__":
    unittest.main()
