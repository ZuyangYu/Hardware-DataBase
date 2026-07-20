import unittest
from dataclasses import dataclass

from src.agents.graph import (
    _expected_evidence,
    _required_candidate_evidence,
    analyze_question_with_llm,
    plan_next_retrieval,
    plan_source_selection,
    plan_source_selection_with_llm,
)
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


class _DocumentOnlyAnalysisLLM:
    def chat(self, messages):
        return (
            '{"intent":"lookup","summary":"查询器件",'
            '"entities":["U1800"],"sub_questions":['
            '{"id":"sq_1","question":"U1800是什么器件，有什么功能？",'
            '"expected_evidence":["document_text"]}]}'
        )


class _DocumentOnlyPlannerLLM:
    def chat(self, messages):
        return (
            '{"source_plan":[{"source_name":"manual.pdf",'
            '"reason":"查文档","tool_calls":[{"tool_name":"document_rag",'
            '"query":"U1800功能","reason":"查功能","top_k":8}]}],'
            '"skipped_sources":[]}'
        )


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


class _CircuitCatalogTool:
    def scan(self, kb_name, ctx):
        return {
            "sources": [
                {
                    "document_name": "board.edf",
                    "record_id": 41,
                    "processor_kind": "circuit_design",
                    "content_kind": "circuit_design",
                    "status": "indexed",
                }
            ]
        }


class _DocumentOnlyNextPlannerLLM:
    def chat(self, messages):
        return (
            '{"tool_calls":[{"tool_name":"document_rag","query":"U1800功能",'
            '"reason":"查文档","top_k":8}]}'
        )


class CircuitAgentPlanningTests(unittest.TestCase):
    def test_required_circuit_evidence_covers_generic_circuit_questions(self):
        for query in [
            "U1700 的每个引脚连接到哪个网络？",
            "网络 VCC3V3 上连接了哪些芯片？",
            "以太网 PHY 的 1.0V 电源如何一级级供出？",
            "板上 TPS22918 做了哪些电源开关？",
        ]:
            self.assertIn("circuit_design", _required_candidate_evidence(query))

    def test_llm_document_only_analysis_cannot_drop_refdes_requirement(self):
        state = {
            "user_query": "U1800是什么器件，有什么功能？",
            "history": [],
            "trace": [],
        }

        result = analyze_question_with_llm(state, _DocumentOnlyAnalysisLLM())

        expected = result["question_analysis"]["sub_questions"][0]["expected_evidence"]
        self.assertIn("circuit_design", expected)
        self.assertIn("document_text", expected)

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

    def test_document_only_llm_plan_is_completed_with_each_indexed_circuit_source(self):
        state = {
            "user_query": "U1800是什么器件，有什么功能？",
            "question_analysis": {
                "sub_questions": [
                    {
                        "id": "sq_1",
                        "question": "U1800是什么器件，有什么功能？",
                        "expected_evidence": ["circuit_design", "document_text"],
                    }
                ]
            },
            "catalog": {
                "sources": [
                    {
                        "document_name": "board_a.edf",
                        "record_id": 1,
                        "processor_kind": "circuit_design",
                        "content_kind": "circuit_design",
                        "status": "indexed",
                    },
                    {
                        "document_name": "board_b.edf",
                        "record_id": 2,
                        "processor_kind": "circuit_design",
                        "content_kind": "circuit_design",
                        "status": "indexed",
                    },
                    {
                        "document_name": "manual.pdf",
                        "record_id": 3,
                        "processor_kind": "ragflow",
                        "content_kind": "document_text",
                        "status": "parsed",
                    },
                ]
            },
            "trace": [],
        }

        result = plan_source_selection_with_llm(state, _DocumentOnlyPlannerLLM())

        circuit_calls = [
            call
            for item in result["source_plan"]["source_plan"]
            for call in item["tool_calls"]
            if call["tool_name"] == "circuit_query"
        ]
        self.assertEqual(
            {call["filters"]["source_name"] for call in circuit_calls},
            {"board_a.edf", "board_b.edf"},
        )

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

    def test_replan_adds_unsearched_circuit_call_when_llm_only_returns_document(self):
        state = {
            "kb_name": "kb_hw",
            "user_query": "U1800是什么器件，有什么功能？",
            "sufficiency": {"suggested_queries": [{"query": "U1800"}]},
            "retrieval_round": 1,
            "retrieval_diagnostics": [],
            "merged_evidence": [],
            "source_plan": {"source_plan": []},
            "retrieval_ledger": [
                {
                    "missing_evidence_types": ["circuit_design"],
                    "unsearched_relevant_sources": ["board.edf"],
                }
            ],
            "trace": [],
        }

        result = plan_next_retrieval(state, _DocumentOnlyNextPlannerLLM(), _CircuitCatalogTool())

        self.assertTrue(
            any(call["tool_name"] == "circuit_query" for call in result["next_retrieval_calls"])
        )

    def test_replan_keeps_deterministic_circuit_gap_when_llm_is_unavailable(self):
        state = {
            "kb_name": "kb_hw",
            "user_query": "U1800是什么器件，有什么功能？",
            "sufficiency": {"suggested_queries": [{"query": "U1800"}]},
            "retrieval_round": 1,
            "retrieval_diagnostics": [],
            "merged_evidence": [],
            "source_plan": {"source_plan": []},
            "retrieval_ledger": [
                {
                    "missing_evidence_types": ["circuit_design"],
                    "unsearched_relevant_sources": ["board.edf"],
                }
            ],
            "trace": [],
        }

        result = plan_next_retrieval(state, None, _CircuitCatalogTool())

        self.assertEqual(result["next_retrieval_calls"][0]["tool_name"], "circuit_query")

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
