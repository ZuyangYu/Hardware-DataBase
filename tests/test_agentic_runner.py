import json
import unittest
import ast
from dataclasses import dataclass, field
from pathlib import Path

from src.agents.graph import judge_sufficiency, plan_next_retrieval, plan_source_selection_with_llm, retrieve_evidence, score_and_compare_evidence
from src.agents.query_tokens import tokenize_hardware_query
from src.agents.runner import MultiSourceAgentRunner
from src.agents.tools.document_rag_tool import DocumentRAGTool
from src.core.llm_client import LLMUsageRecord, LLMUsageSummary
from src.pipelines.document_store_sqlite import PipelineDocumentRecord
from src.pipelines.document_rag.schemas import (
    BackendHealth,
    BackendResult,
    DocumentInfo,
    Evidence,
    IngestResult,
    RequestContext,
)


class _FakeBackend:
    name = "fake_ragflow"

    def __init__(self):
        self.retrieve_calls = []

    def list_knowledge_bases(self):
        return ["kb"]

    def upload_files(self, kb_name, files, ctx=None, source_group=None, progress_callback=None):
        return IngestResult(success_count=len(files), total_count=len(files), backend=self.name)

    def list_documents(self, kb_name, ctx=None):
        return [
            DocumentInfo(
                id="doc-1",
                name="design_report.pdf",
                processor_kind="ragflow",
                status="completed",
                metadata={"content_kind": "document_text", "source_group": "design"},
            ),
            DocumentInfo(
                id="doc-2",
                name="bom.xlsx",
                processor_kind="spreadsheet_table",
                status="completed",
                metadata={"content_kind": "spreadsheet_table", "source_group": "material"},
            ),
        ]

    def retrieve(self, kb_name, query, top_k=None, ctx=None, filters=None):
        self.retrieve_calls.append({"kb_name": kb_name, "query": query, "top_k": top_k, "filters": filters or {}})
        # 多跳模拟：第一轮文档返回含料号 R-123；第二轮若 query 含 R-123 则返回 BOM 用量。
        if "R-123" in query and "用量" in query:
            return [
                Evidence(
                    id="bom-1",
                    content="R-123 用量：100，供应商：ACME",
                    source_name="bom.xlsx",
                    score=0.95,
                    metadata={"content_kind": "spreadsheet_table", "processor_kind": "spreadsheet_table"},
                    backend=self.name,
                    retriever="fake",
                )
            ]
        return [
            Evidence(
                id="chunk-a",
                content="设计选用料号 R-123 用于电源轨。",
                source_name="design_report.pdf",
                score=0.91,
                metadata={"content_kind": "document_text", "processor_kind": "ragflow", "page": 3},
                backend=self.name,
                retriever="fake",
            ),
            Evidence(
                id="chunk-b",
                content="电源轨 5V，由 R-123 提供。",
                source_name="design_report.pdf",
                score=0.73,
                metadata={"content_kind": "document_text", "processor_kind": "ragflow", "page": 4},
                backend=self.name,
                retriever="fake",
            ),
        ][: top_k or 5]

    def delete_document(self, kb_name, document_id, ctx=None):
        return BackendResult(ok=True, message="deleted", backend=self.name)

    def health_check(self):
        return BackendHealth(ok=True, backend=self.name)


@dataclass
class _FakeDocumentStore:
    records: list = field(default_factory=list)

    def list_documents(self, kb_name, department_id=None):
        return self.records

    def get_document_by_id_scoped(self, record_id, department_id):
        for record in self.records:
            if record.id == record_id and str(record.department_id) == str(department_id):
                return record
        return None


class _FakeSpreadsheetService:
    def get_document_profile(self, record):
        return {}


class _FakeLLM:
    """全自动流程：router → analyze → planner → judge_sufficiency → (plan_next) → compose。

    可配置第一轮 sufficiency 是否触发多跳（insufficient + suggested_queries）。
    """

    def __init__(self, *, first_sufficient: bool = True, multi_hop_query: str | None = None, planner_fanout: bool = False):
        self._first_sufficient = first_sufficient
        self._multi_hop_query = multi_hop_query
        self._planner_fanout = planner_fanout
        self._judge_calls = 0

    def chat(self, messages):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "Query Router" in system or "查询路由器" in system:
            query_text = user.split("User query:\n", 1)[-1].split("\n", 1)[0]
            small_talk_markers = ("你好", "你是谁", "谢谢", "hello", "hi")
            general_markers = ("什么是", "解释", "概念", "一般原则")
            if any(m in query_text for m in small_talk_markers):
                return '{"category": "small_talk", "needs_retrieval": false, "reason": "识别为问候/寒暄。"}'
            if any(m in query_text for m in general_markers) and "design_report" not in query_text:
                return '{"category": "general_knowledge", "needs_retrieval": false, "reason": "通用知识可答。"}'
            return '{"category": "hardware_kb_query", "needs_retrieval": true, "reason": "需要知识库事实。"}'
        if "Question Analysis Agent" in system:
            return """
            {
              "intent": "multi_source_hardware_query",
              "summary": "查询设计选用的料号及其 BOM 用量。",
              "reasoning_summary": "跨文档与表格的多跳查询。",
              "entities": ["R-123", "design_report", "bom"],
              "sub_questions": [
                {"id": "sq_1", "question": "设计选用什么料号？", "expected_evidence": ["document_text"]},
                {"id": "sq_2", "question": "该料号在 BOM 里的用量是多少？", "expected_evidence": ["spreadsheet_table"]}
              ],
              "multi_hop": true
            }
            """
        if "Retrieval Planner and Query Rewriter Agent" in system:
            if self._planner_fanout:
                return """
                {
                  "source_plan": [
                    {"source_name": "design_report.pdf", "reason": "设计文档回答 ASIL 与模块状态。",
                     "tool_calls": [{"tool_name": "document_rag", "query": "Camera F03 ASIL 状态 硬件架构", "reason": "查架构状态", "top_k": 8}]},
                    {"source_name": "bom.xlsx", "reason": "表格源回答图像传感器芯片型号。",
                     "tool_calls": [
                       {"tool_name": "spreadsheet_semantic", "query": "Camera 图像传感器芯片 FVCM F03", "reason": "查语义行", "top_k": 8},
                       {"tool_name": "spreadsheet_cell", "query": "Camera 图像传感器芯片 FVCM F03", "reason": "查精确单元格", "top_k": 12}
                     ]}
                  ],
                  "skipped_sources": []
                }
                """
            return """
            {
              "source_plan": [
                {"source_name": "design_report.pdf", "reason": "设计文档含料号信息。",
                 "tool_calls": [{"tool_name": "document_rag", "query": "设计选用料号", "reason": "查料号", "top_k": 8}]}
              ],
              "skipped_sources": []
            }
            """
        # 重规划器分支须先于充分性分支：plan_next 的 system prompt 也提到"充分性判断器"。
        if "多跳重规划器" in system:
            return json.dumps({
                "tool_calls": [
                    {"tool_name": "spreadsheet_semantic", "query": self._multi_hop_query or "R-123 用量", "source_name": "bom.xlsx", "reason": "查 BOM", "top_k": 8}
                ]
            }, ensure_ascii=False)
        if "充分上下文" in system or "Sufficient Context" in system or "充分性" in system:
            self._judge_calls += 1
            if self._judge_calls == 1 and not self._first_sufficient:
                # 第一轮判 insufficient，给出基于已发现实体 R-123 的多跳新查询。
                return json.dumps({
                    "status": "insufficient_need_more",
                    "reason": "已查到料号 R-123，需去 BOM 查用量。",
                    "missing": ["R-123 在 BOM 中的用量"],
                    "suggested_queries": [
                        {"query": self._multi_hop_query or "R-123 用量", "tool_name": "spreadsheet_semantic", "source_name": "bom.xlsx", "reason": "跨语料查 BOM 用量"}
                    ],
                }, ensure_ascii=False)
            return json.dumps({
                "status": "sufficient",
                "reason": "证据已充分。",
                "missing": [],
                "suggested_queries": []
            }, ensure_ascii=False)
        return "基于证据的回答：料号 R-123 用量为 100，供应商 ACME。"

    def stream_chat(self, messages, **kwargs):
        yield self.chat(messages)


class _UsageTrackingLLM(_FakeLLM):
    def __init__(self):
        super().__init__(first_sufficient=True)
        self.records = []

    def reset_usage(self):
        self.records = []

    def chat(self, messages, **kwargs):
        stage = kwargs.get("usage_stage", "missing")
        self.records.append(
            LLMUsageRecord(
                stage=stage,
                provider="fake",
                model="fake-model",
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                usage_returned=True,
            )
        )
        return super().chat(messages)

    def stream_chat(self, messages, **kwargs):
        stage = kwargs.get("usage_stage", "missing")
        self.records.append(
            LLMUsageRecord(
                stage=stage,
                provider="fake",
                model="fake-model",
                prompt_tokens=20,
                completion_tokens=8,
                total_tokens=28,
                usage_returned=True,
            )
        )
        yield super().chat(messages)

    def get_usage_summary(self):
        by_stage = {}
        for record in self.records:
            current = by_stage.get(record.stage, LLMUsageSummary())
            by_stage[record.stage] = LLMUsageSummary(
                provider=record.provider,
                model=record.model,
                prompt_tokens=current.prompt_tokens + record.prompt_tokens,
                completion_tokens=current.completion_tokens + record.completion_tokens,
                total_tokens=current.total_tokens + record.total_tokens,
                call_count=current.call_count + 1,
                usage_returned_count=current.usage_returned_count + 1,
            )
        return LLMUsageSummary(
            provider="fake",
            model="fake-model",
            prompt_tokens=sum(record.prompt_tokens for record in self.records),
            completion_tokens=sum(record.completion_tokens for record in self.records),
            total_tokens=sum(record.total_tokens for record in self.records),
            call_count=len(self.records),
            usage_returned_count=len(self.records),
            by_stage=by_stage,
        )


class _NoKwargStreamLLM(_FakeLLM):
    def stream_chat(self, messages):
        yield "streamed-no-kwargs"


class _BadLLM:
    """LLM 全程返回非 JSON → 验证降级不挂。"""

    def chat(self, messages):
        system = messages[0]["content"]
        if "Query Router" in system or "查询路由器" in system:
            # router 失败 → 确定性兜底；design_report 含硬件信号 → 检索
            return "not json"
        if "Question Analysis Agent" in system:
            return "not json"
        if "Retrieval Planner" in system:
            return "not json"
        if "充分" in system or "重规划" in system:
            return "not json"
        return "fallback answer"


class _AliasPlannerLLM:
    def __init__(self, *, mode: str):
        self.mode = mode

    def chat(self, messages):
        if self.mode == "initial":
            return json.dumps(
                {
                    "source_plan": [
                        {
                            "source_name": "摄像头模块.docx",
                            "reason": "LLM 按原始文件名选择摄像头模块资料。",
                            "tool_calls": [
                                {
                                    "tool_name": "document_rag",
                                    "query": "FVCM 图像传感器芯片型号",
                                    "reason": "查摄像头模块文档",
                                    "top_k": 8,
                                }
                            ],
                        }
                    ],
                    "skipped_sources": [],
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "tool_calls": [
                    {
                        "tool_name": "document_rag",
                        "query": "FVCM 图像传感器芯片型号",
                        "source_name": "摄像头模块.docx",
                        "reason": "跨源查原始摄像头模块资料",
                        "top_k": 8,
                    }
                ]
            },
            ensure_ascii=False,
        )


class _AliasCatalogTool:
    def scan(self, kb_name, ctx):
        return {
            "sources": [
                {
                    "record_id": 21,
                    "document_name": "uploaded-camera-module-docx",
                    "original_file_name": "摄像头模块.docx",
                    "processor_kind": "ragflow",
                    "content_kind": "document_text",
                    "source_group": "design",
                }
            ],
            "summary": {"total": 1, "documents": 1, "spreadsheets": 0},
            "errors": [],
        }


class _PromptCaptureLLM:
    def __init__(self, payload):
        self.payload = payload
        self.last_user_prompt = ""

    def chat(self, messages):
        self.last_user_prompt = messages[-1]["content"]
        return json.dumps(self.payload, ensure_ascii=False)


class AgenticRunnerTests(unittest.TestCase):
    def test_retrieval_does_not_truncate_required_circuit_sources(self):
        calls = []

        class _Tool:
            def run(self, query, kb_name, ctx, top_k=5, filters=None):
                calls.append((query, (filters or {}).get("source_name", "")))
                return []

        circuit_calls = [
            {
                "tool_name": "circuit_query",
                "query": f"U{i}",
                "filters": {"source_name": f"board_{i}.edf"},
                "top_k": 8,
            }
            for i in range(9)
        ]
        state = {
            "kb_name": "kb",
            "user_query": "U1800是什么器件？",
            "source_plan": {
                "source_plan": [
                    {
                        "tool_calls": [
                            {
                                "tool_name": "document_rag",
                                "query": "U1800功能",
                                "filters": {},
                                "top_k": 8,
                            },
                            *circuit_calls,
                        ]
                    }
                ]
            },
            "retrieval_round": 0,
            "evidence": [],
            "trace": [],
        }

        retrieve_evidence(state, {"document_rag": _Tool(), "circuit_query": _Tool()})

        self.assertEqual({source for _, source in calls if source}, {f"board_{i}.edf" for i in range(9)})

    def test_generic_document_text_does_not_cover_a_different_refdes(self):
        state = {
            "question_analysis": {
                "entities": ["U1800"],
                "sub_questions": [
                    {
                        "id": "sq_1",
                        "question": "U1800是什么器件，有什么功能？",
                        "expected_evidence": ["document_text"],
                    }
                ],
            },
            "catalog": {"sources": []},
            "merged_evidence": [
                {
                    "id": "doc-u1700",
                    "source_name": "manual.pdf",
                    "content": "U1700是什么器件，有什么功能？",
                    "content_kind": "document_text",
                    "processor_kind": "ragflow",
                    "score": 0.9,
                }
            ],
            "retrieval_diagnostics": [],
            "source_plan": {"source_plan": []},
            "trace": [],
        }

        result = score_and_compare_evidence(state)

        self.assertEqual(result["retrieval_ledger"][0]["status"], "missing")
        self.assertIn("document_text", result["retrieval_ledger"][0]["missing_evidence_types"])

    def test_runner_uses_compiled_langgraph_without_legacy_manual_pipeline(self):
        runner_path = Path(__file__).resolve().parents[1] / "src" / "agents" / "runner.py"
        tree = ast.parse(runner_path.read_text(encoding="utf-8"))
        source = runner_path.read_text(encoding="utf-8")

        self.assertNotIn("def _scan_kb_catalog", source)
        self.assertNotIn("def _retrieve_evidence", source)
        self.assertNotIn("merge_evidence(", source)
        self.assertNotIn("score_and_compare_evidence(", source)
        self.assertNotIn("should_continue(", source)

        stream_func = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "stream"
        )
        self.assertFalse(any(isinstance(node, ast.While) for node in ast.walk(stream_func)))
        self.assertIn("self.graph.stream", source)

    def test_agent_state_has_no_legacy_human_confirmation_fields(self):
        state_path = Path(__file__).resolve().parents[1] / "src" / "agents" / "state.py"
        source = state_path.read_text(encoding="utf-8")
        for legacy_name in [
            "ApprovalAction",
            "needs_user_confirmation",
            "question_approval",
            "source_approval",
            "pending_human_action",
            "source_scope_note",
        ]:
            self.assertNotIn(legacy_name, source)

    def _runner(self, llm):
        backend = _FakeBackend()
        runner = MultiSourceAgentRunner(
            rag_backend=backend,
            document_store=_FakeDocumentStore(),
            spreadsheet_service=_FakeSpreadsheetService(),
            llm_client=llm,
        )
        return runner, backend

    def _ctx(self):
        return RequestContext(user_id="u1", session_id="s1", roles=["user"], metadata={"department_id": "7"})

    def test_autonomous_single_stream_yields_answer(self):
        runner, backend = self._runner(_FakeLLM(first_sufficient=True))
        out = "".join(runner.stream(query="查 design_report 选用的料号", kb_name="kb", history=[], ctx=self._ctx()))
        self.assertIn("R-123", out)
        # 无 pending（全自动），footer 含检索
        footer = runner.get_last_footer()
        self.assertIn("执行时间线", footer)
        self.assertIn("检索诊断", footer)
        self.assertTrue(backend.retrieve_calls)

    def test_multi_hop_dynamic_requery(self):
        runner, backend = self._runner(_FakeLLM(first_sufficient=False, multi_hop_query="R-123 用量"))
        "".join(runner.stream(query="查 design_report 料号及 BOM 用量", kb_name="kb", history=[], ctx=self._ctx()))
        # 第二轮基于第一轮发现实体 R-123 产出了新查询；rewritten_queries 汇总所有轮次查询。
        summary = runner.get_last_retrieval_summary()
        rewritten = summary.get("rewritten_queries") or []
        self.assertTrue(any("R-123" in q and "用量" in q for q in rewritten), f"rewritten={rewritten}")
        # 确实跑了多轮检索
        self.assertGreaterEqual(summary.get("retrieval_rounds", 0), 2)
        # footer 含迭代检索段
        footer = runner.get_last_footer()
        self.assertIn("迭代检索", footer)

    def test_sufficiency_sufficient_stops_one_round(self):
        runner, backend = self._runner(_FakeLLM(first_sufficient=True))
        "".join(runner.stream(query="查 design_report 料号", kb_name="kb", history=[], ctx=self._ctx()))
        # sufficient → 不触发下一轮重规划；首轮调用数由 planner 的 fanout 决定。
        summary = runner.get_last_retrieval_summary()
        self.assertEqual(summary.get("retrieval_rounds"), 1)

    def test_retrieval_summary_contains_structured_log_fields(self):
        runner, backend = self._runner(_FakeLLM(first_sufficient=True))
        "".join(runner.stream(query="查 design_report 料号", kb_name="kb", history=[], ctx=self._ctx()))
        summary = runner.get_last_retrieval_summary()
        self.assertEqual(summary.get("status"), "success")
        self.assertIn("trace", summary)
        self.assertIn("tool_diagnostics", summary)
        self.assertIn("claim_coverage", summary)
        self.assertIn("retrieval_ledger", summary)
        self.assertIn("evidence_quality", summary)
        self.assertIn("verification", summary)
        self.assertEqual(summary.get("sufficiency_status"), "sufficient")

    def test_runner_exposes_token_usage_summary_by_stage(self):
        llm = _UsageTrackingLLM()
        runner, backend = self._runner(llm)
        "".join(runner.stream(query="check design_report part number", kb_name="kb", history=[], ctx=self._ctx()))

        summary = runner.get_last_token_usage_summary()

        self.assertGreater(summary.total_tokens, 0)
        self.assertIn("query_router", summary.by_stage)
        self.assertIn("question_analysis", summary.by_stage)
        self.assertIn("source_planning", summary.by_stage)
        self.assertIn("final_answer", summary.by_stage)

    def test_runner_keeps_streaming_with_llm_client_that_rejects_usage_stage_kwarg(self):
        runner, backend = self._runner(_NoKwargStreamLLM(first_sufficient=True))

        out = "".join(runner.stream(query="check design_report part number", kb_name="kb", history=[], ctx=self._ctx()))

        self.assertIn("streamed-no-kwargs", out)
        self.assertNotIn("调用失败", out)

    def test_planner_search_fanout_can_cover_document_and_spreadsheet_sources(self):
        runner, backend = self._runner(_FakeLLM(first_sufficient=True, planner_fanout=True))
        "".join(runner.stream(query="ADAS 的 Camera 模块用的图像传感器芯片是什么？ASIL 等级和状态是什么？", kb_name="kb", history=[], ctx=self._ctx()))
        summary = runner.get_last_retrieval_summary()
        rewritten = " | ".join(summary.get("rewritten_queries") or [])
        self.assertIn("ASIL", rewritten)
        self.assertIn("图像传感器芯片", rewritten)
        footer = runner.get_last_footer()
        self.assertIn("Search fanout planned by LLM agent", footer)
        self.assertIn("计划源：2", footer)

    def test_observability_footer_redacts_raw_queries_and_filters(self):
        runner, backend = self._runner(_FakeLLM(first_sufficient=True))
        sensitive_query = "ADAS Camera SECRET-SENSOR-123 图像传感器芯片"
        "".join(runner.stream(query=sensitive_query, kb_name="kb", history=[], ctx=self._ctx()))
        footer = runner.get_last_footer()
        self.assertNotIn("SECRET-SENSOR-123", footer)
        self.assertNotIn("filters={", footer)
        self.assertIn("执行时间线", footer)
        self.assertIn("检索诊断", footer)
        self.assertIn("scoped=", footer)

    def test_sufficiency_llm_failure_judges_partial(self):
        runner, backend = self._runner(_BadLLM())
        out = "".join(runner.stream(query="check design_report EMI", kb_name="kb", history=[], ctx=self._ctx()))
        # 不挂，出了答案（fallback 或错误响应），无死循环
        self.assertTrue(out)
        # judge_sufficiency LLM 失败 → 判 partial，未触发多跳空转
        summary = runner.get_last_retrieval_summary()
        self.assertLessEqual(summary.get("retrieval_rounds", 0), 2)

    def test_smalltalk_routes_to_direct_answer(self):
        runner, backend = self._runner(_FakeLLM())
        "".join(runner.stream(query="你好", kb_name="kb", history=[], ctx=self._ctx()))
        self.assertIn("直接回答（未检索知识库）", runner.get_last_footer())
        self.assertFalse(backend.retrieve_calls)

    def test_general_knowledge_routes_to_direct_answer(self):
        runner, backend = self._runner(_FakeLLM())
        "".join(runner.stream(query="什么是 EMI", kb_name="kb", history=[], ctx=self._ctx()))
        self.assertIn("直接回答（未检索知识库）", runner.get_last_footer())
        self.assertFalse(backend.retrieve_calls)

    def test_router_deterministic_fallback(self):
        # llm_client=None → route_query 走确定性路径。
        runner, backend = self._runner(None)
        "".join(runner.stream(query="你好", kb_name="kb", history=[], ctx=self._ctx()))
        self.assertIn("直接回答（未检索知识库）", runner.get_last_footer())
        self.assertFalse(backend.retrieve_calls)

    def test_project_hardware_query_routes_to_retrieval(self):
        runner, backend = self._runner(_FakeLLM())
        "".join(runner.stream(query="ADAS项目硬件设计", kb_name="kb", history=[], ctx=self._ctx()))
        self.assertTrue(backend.retrieve_calls)

    def test_hardware_query_tokenizer_handles_chinese_and_acronyms(self):
        tokens = tokenize_hardware_query("检查主板EMI整改后的电压5V和替代料号", max_tokens=16)
        self.assertIn("emi", tokens)
        self.assertIn("5v", tokens)
        self.assertIn("电压", tokens)
        self.assertIn("料号", tokens)

    def test_document_rag_does_not_drop_backend_hits_with_alias_source_name(self):
        backend = _FakeBackend()
        store = _FakeDocumentStore(
            records=[
                PipelineDocumentRecord(
                    id=11,
                    kb_name="kb",
                    document_name="archived-design.pdf",
                    original_file_name="design_report.pdf",
                    dataset_kind="design",
                    dataset_id="dataset-1",
                    document_id="doc-1",
                    source_group="design",
                    department_id="7",
                    uploaded_by="u1",
                    status="completed",
                )
            ]
        )
        tool = DocumentRAGTool(backend, store)
        ctx = RequestContext(user_id="u1", session_id="s1", roles=["user"], metadata={"department_id": "7"})
        hits = tool.run("EMI", "kb", ctx, top_k=2, filters={"record_id": 11, "source_name": "archived-design.pdf"})
        self.assertEqual(len(hits), 2)
        self.assertEqual(
            backend.retrieve_calls[0]["filters"]["source_names"],
            ["design_report.pdf", "archived-design.pdf"],
        )

    def test_retrieve_evidence_uses_next_retrieval_calls_on_round_two(self):
        calls = []

        class _Tool:
            def run(self, query, kb_name, ctx, top_k=5, filters=None):
                calls.append({"query": query, "filters": filters or {}})
                return []

        state = {
            "kb_name": "kb",
            "user_query": "查 R-123",
            "source_plan": {"source_plan": [{"tool_calls": [{"tool_name": "document_rag", "query": "原始", "filters": {}, "top_k": 8}]}]},
            "next_retrieval_calls": [{"tool_name": "document_rag", "query": "多跳新查询 R-123", "filters": {}, "top_k": 8}],
            "retrieval_round": 1,  # 进入第二轮
            "evidence": [],
            "trace": [],
        }
        retrieve_evidence(state, {"document_rag": _Tool()})
        # 第二轮应使用 next_retrieval_calls 的 query，而非 source_plan
        self.assertEqual(calls[0]["query"], "多跳新查询 R-123")

    def test_llm_planner_source_name_can_match_original_filename(self):
        state = {
            "kb_name": "kb",
            "user_query": "FVCM 图像传感器芯片型号",
            "question_analysis": {
                "sub_questions": [
                    {"id": "sq_1", "question": "FVCM 图像传感器芯片型号", "expected_evidence": ["document_text"]}
                ]
            },
            "catalog": _AliasCatalogTool().scan("kb", None),
            "trace": [],
        }
        planned = plan_source_selection_with_llm(state, _AliasPlannerLLM(mode="initial"))
        call = planned["source_plan"]["source_plan"][0]["tool_calls"][0]
        self.assertEqual(call["filters"]["source_name"], "uploaded-camera-module-docx")
        self.assertEqual(call["filters"]["record_id"], 21)

    def test_plan_next_retrieval_source_name_can_match_original_filename(self):
        state = {
            "kb_name": "kb",
            "user_query": "FVCM 图像传感器芯片型号",
            "retrieval_round": 1,
            "sufficiency": {
                "status": "insufficient_need_more",
                "suggested_queries": [
                    {
                        "query": "FVCM 图像传感器芯片型号",
                        "tool_name": "document_rag",
                        "source_name": "摄像头模块.docx",
                    }
                ],
            },
            "retrieval_diagnostics": [],
            "merged_evidence": [],
            "source_plan": {"source_plan": []},
            "trace": [],
        }
        planned = plan_next_retrieval(state, _AliasPlannerLLM(mode="next"), _AliasCatalogTool())
        call = planned["next_retrieval_calls"][0]
        self.assertEqual(call["filters"]["source_name"], "uploaded-camera-module-docx")
        self.assertEqual(call["filters"]["record_id"], 21)

    def test_retrieval_ledger_tracks_unsearched_relevant_sources(self):
        state = {
            "question_analysis": {
                "entities": ["R-123"],
                "sub_questions": [
                    {"id": "sq_1", "question": "R-123 在 BOM 里的用量是多少？", "expected_evidence": ["spreadsheet_table"]}
                ],
            },
            "catalog": {
                "sources": [
                    {
                        "document_name": "bom.xlsx",
                        "processor_kind": "spreadsheet_table",
                        "content_kind": "spreadsheet_table",
                        "source_group": "material",
                    }
                ]
            },
            "merged_evidence": [],
            "retrieval_diagnostics": [
                {
                    "tool_name": "document_rag",
                    "filters": {"source_name": "design_report.pdf"},
                    "hit_count": 2,
                    "status": "ok",
                }
            ],
            "source_plan": {"source_plan": []},
            "trace": [],
        }
        scored = score_and_compare_evidence(state)
        ledger = scored["retrieval_ledger"]
        self.assertEqual(ledger[0]["status"], "missing")
        self.assertIn("spreadsheet_table", ledger[0]["missing_evidence_types"])
        self.assertIn("bom.xlsx", ledger[0]["unsearched_relevant_sources"])
        self.assertIn("未覆盖来源", ledger[0]["gap_feedback"])

    def test_sufficiency_prompt_includes_retrieval_ledger(self):
        llm = _PromptCaptureLLM(
            {
                "status": "insufficient_need_more",
                "reason": "BOM 用量缺失。",
                "missing": ["R-123 BOM 用量"],
                "suggested_queries": [
                    {
                        "query": "R-123 BOM 用量",
                        "tool_name": "spreadsheet_semantic",
                        "source_name": "bom.xlsx",
                        "reason": "账本显示 BOM 未查。",
                    }
                ],
            }
        )
        state = {
            "user_query": "查 R-123 用量",
            "retrieval_round": 1,
            "question_analysis": {"sub_questions": []},
            "merged_evidence": [],
            "intermediate_answer": "缺失：R-123 BOM 用量",
            "coverage_matrix": {"coverage": [], "conflicts": []},
            "retrieval_ledger": [{"sub_question_id": "sq_1", "gap_feedback": "优先补查未覆盖来源: bom.xlsx"}],
            "trace": [],
        }
        judged = judge_sufficiency(state, llm)
        self.assertIn("检索账本", llm.last_user_prompt)
        self.assertIn("bom.xlsx", llm.last_user_prompt)
        self.assertEqual(judged["sufficiency"]["status"], "insufficient_need_more")

    def test_plan_next_prompt_includes_ledger_feedback(self):
        llm = _PromptCaptureLLM(
            {
                "tool_calls": [
                    {
                        "tool_name": "spreadsheet_semantic",
                        "query": "R-123 BOM 用量",
                        "source_name": "bom.xlsx",
                        "reason": "按账本补查未覆盖来源。",
                        "top_k": 8,
                    }
                ]
            }
        )
        class _Catalog:
            def scan(self, kb_name, ctx):
                return {
                    "sources": [
                        {
                            "record_id": 31,
                            "document_name": "bom.xlsx",
                            "processor_kind": "spreadsheet_table",
                            "content_kind": "spreadsheet_table",
                        }
                    ]
                }

        state = {
            "kb_name": "kb",
            "user_query": "查 R-123 用量",
            "retrieval_round": 1,
            "sufficiency": {
                "suggested_queries": [
                    {"query": "R-123 BOM 用量", "tool_name": "spreadsheet_semantic", "source_name": "bom.xlsx"}
                ]
            },
            "retrieval_diagnostics": [],
            "merged_evidence": [],
            "source_plan": {"source_plan": []},
            "retrieval_ledger": [{"sub_question_id": "sq_1", "gap_feedback": "优先补查未覆盖来源: bom.xlsx"}],
            "trace": [],
        }
        planned = plan_next_retrieval(state, llm, _Catalog())
        self.assertIn("检索账本", llm.last_user_prompt)
        self.assertIn("优先补查未覆盖来源", llm.last_user_prompt)
        self.assertEqual(planned["next_retrieval_calls"][0]["filters"]["source_name"], "bom.xlsx")


if __name__ == "__main__":
    unittest.main()
