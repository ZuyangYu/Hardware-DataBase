import unittest

from src.agents.runner import (
    MultiSourceAgentRunner,
    _build_claim_coverage,
    _build_evidence_quality,
)
from src.agents.schemas import Evidence
from src.agents.tools.runtime import ToolDiagnostics, ToolRuntime


def _ev(citations, score=0.9, content="x"):
    items = []
    for idx, cit in enumerate(citations):
        items.append(
            Evidence(
                id=f"e{idx}",
                content=content,
                source_name="s",
                content_kind="doc",
                processor_kind="doc",
                score=score,
                metadata={"citation_number": cit},
            )
        )
    return items


def _diag(tool_name, status="ok", hit_count=3, error=""):
    return ToolDiagnostics(
        tool_name=tool_name,
        status=status,
        hit_count=hit_count,
        error=error,
        latency_ms=1,
        filters={},
    )


class ClaimCoverageTests(unittest.TestCase):
    def test_parses_citations(self):
        cov = _build_claim_coverage("见[1]与[3]", _ev([1, 2, 3]))
        self.assertEqual(len(cov), 2)
        ids = {c["evidence_ids"][0] for c in cov}
        self.assertEqual(ids, {"e0", "e2"})
        self.assertTrue(all(c["status"] == "supported" for c in cov))

    def test_ignores_unknown_citation(self):
        self.assertEqual(_build_claim_coverage("见[9]", _ev([1])), [])

    def test_empty_answer_no_citations(self):
        self.assertEqual(_build_claim_coverage("", _ev([1])), [])


class EvidenceQualityTests(unittest.TestCase):
    def test_shape_and_score(self):
        q = _build_evidence_quality(_ev([1, 2], score=0.42))
        self.assertEqual(len(q), 2)
        self.assertEqual(q[0]["evidence_id"], "e0")
        self.assertEqual(q[0]["score"], 0.42)

    def test_non_numeric_score_is_zero(self):
        items = _ev([1])
        items[0].score = None
        q = _build_evidence_quality(items)
        self.assertEqual(q[0]["score"], 0.0)


class RetrievalSummaryTests(unittest.TestCase):
    def _runner(self):
        class _FakeRAGBackend:
            name = "fake"

            def retrieve(self, *args, **kwargs):
                return []

        return MultiSourceAgentRunner(rag_backend=_FakeRAGBackend())

    def _verification(self):
        return {
            "grounded": True,
            "grounding_method": "citation_presence",
            "unsupported_claims": [],
            "weak_claims": [],
            "conflicts": [],
            "citation_coverage": 1.0,
        }

    def test_partial_failure_status_and_honest_fields(self):
        rt = ToolRuntime(kb_name="k", ctx=None)
        rt.record_diagnostic(_diag("document_search", status="failed", hit_count=0, error="boom"))
        rt.add_evidence(_ev([1]))
        rt.log_query("document_search", "q1")
        summary = self._runner()._build_retrieval_summary(rt, [], self._verification())
        self.assertEqual(summary["status"], "partial_failure")
        self.assertEqual(summary["error_message"], "boom")
        # Honest fields, not the old hardcoded constants.
        self.assertEqual(summary["retrieval_rounds"], 1)
        self.assertEqual(summary["sufficiency_status"], "sufficient")
        self.assertEqual(summary["retrieval_ledger"], [{"tool_name": "document_search", "query": "q1"}])
        self.assertEqual(summary["evidence_quality"][0]["evidence_id"], "e0")
        self.assertEqual(summary["evidence_quality"][0]["score"], 0.9)

    def test_no_evidence_is_failed_and_insufficient(self):
        rt = ToolRuntime(kb_name="k", ctx=None)
        rt.record_diagnostic(_diag("circuit_search", status="failed", hit_count=0))
        summary = self._runner()._build_retrieval_summary(rt, [], self._verification())
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["sufficiency_status"], "insufficient")
        self.assertEqual(summary["claim_coverage"], [])

    def test_success_status(self):
        rt = ToolRuntime(kb_name="k", ctx=None)
        rt.record_diagnostic(_diag("document_search", hit_count=4))
        rt.add_evidence(_ev([1, 2]))
        summary = self._runner()._build_retrieval_summary(rt, [], self._verification())
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["retrieval_rounds"], 1)


if __name__ == "__main__":
    unittest.main()


class SystemPromptDualPathTests(unittest.TestCase):
    """守护双路校验协议: SQL 失败回退文本检索 + 交叉验证规则必须在提示词中."""

    def test_prompt_contains_dual_path_protocol(self):
        from src.agents.runner import _SYSTEM_PROMPT

        self.assertIn("回退用 spreadsheet_row_search/spreadsheet_cell_lookup", _SYSTEM_PROMPT)
        self.assertIn("交叉验证", _SYSTEM_PROMPT)
        self.assertIn("不能合并成一个确定结论", _SYSTEM_PROMPT)
