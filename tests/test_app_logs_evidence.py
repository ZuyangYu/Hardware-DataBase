import os
import tempfile
import unittest

from src.core.app_logs import AppLogService
from src.core.auth import ROLE_DEPT_ADMIN, ROLE_USER, AuthUser


def _make_viewer(user_id: int, role: str, department_id: int | None = 1) -> AuthUser:
    return AuthUser(id=user_id, username=f"user{user_id}", role=role, is_active=True, department_id=department_id)


class RetrievedEvidenceLoggingTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="applogs_")
        self.db_path = os.path.join(self._tmpdir, "logs.db")
        self.service = AppLogService(db_path=self.db_path)
        self.owner = _make_viewer(101, ROLE_USER, department_id=7)
        self.other = _make_viewer(202, ROLE_DEPT_ADMIN, department_id=7)

    def tearDown(self):
        for name in os.listdir(self._tmpdir):
            os.remove(os.path.join(self._tmpdir, name))
        os.rmdir(self._tmpdir)

    def _record_trace(self, user: AuthUser, original_query: str = "敏感问题", error_message: str = "") -> int:
        return self.service.record_query_trace(
            user=user,
            kb_name="kb-a",
            original_query=original_query,
            rewritten_query="改写后问题",
            backend="ragflow",
            retriever_type="multi_source_agent",
            final_top_k=2,
            latency_ms=120,
            status="success",
            error_message=error_message,
        )

    def test_record_and_list_evidence_roundtrip(self):
        trace_id = self._record_trace(self.owner)
        evidence = [
            {
                "id": "chunk-1",
                "source_name": "report.pdf",
                "score": 0.91,
                "locator": {"document_id": "doc-1", "chunk_id": "chunk-1"},
                "content_kind": "doc",
                "processor_kind": "ragflow",
                "content": "正文片段" * 50,
                "metadata": {"ragflow_document_id": "doc-1"},
            },
            {
                "id": "chunk-2",
                "source_name": "spec.xlsx",
                "score": 0.42,
                "locator": {},
                "content_kind": "spreadsheet",
                "processor_kind": "spreadsheet",
                "content": "短内容",
                "metadata": {},
            },
        ]
        self.service.record_retrieved_evidence(trace_id, evidence)

        rows = self.service.list_evidence(self.owner, trace_id)
        self.assertEqual([r.rank for r in rows], [1, 2])
        self.assertEqual(rows[0].file_name, "report.pdf")
        self.assertEqual(rows[0].document_id, "doc-1")
        self.assertEqual(rows[0].chunk_id, "chunk-1")
        self.assertAlmostEqual(rows[0].rerank_score, 0.91)
        self.assertIsNone(rows[0].vector_score)
        self.assertIsNone(rows[0].bm25_score)
        self.assertEqual(rows[0].text_preview, "正文片段" * 50)  # owner sees full preview
        self.assertEqual(rows[1].text_preview, "短内容")

    def test_text_preview_truncated_to_200(self):
        trace_id = self._record_trace(self.owner)
        long_content = "x" * 500
        self.service.record_retrieved_evidence(trace_id, [
            {"id": "c1", "source_name": "f", "score": 1.0, "locator": {}, "content_kind": "",
             "processor_kind": "", "content": long_content, "metadata": {}}
        ])
        rows = self.service.list_evidence(self.owner, trace_id)
        self.assertEqual(len(rows[0].text_preview), 200)

    def test_non_owner_query_trace_redacted(self):
        self._record_trace(self.owner, original_query="敏感问题", error_message="Error: boom 含敏感")
        traces = self.service.list_query_traces(self.other)
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].original_query, "[redacted]")
        self.assertEqual(traces[0].rewritten_query, "[redacted]")
        self.assertEqual(traces[0].error_message, "[redacted]")
        # final_top_k / latency / status 等非敏感字段保留
        self.assertEqual(traces[0].final_top_k, 2)
        self.assertEqual(traces[0].status, "success")

    def test_owner_sees_own_query_trace_plain(self):
        self._record_trace(self.owner, original_query="敏感问题", error_message="Error: boom")
        traces = self.service.list_query_traces(self.owner)
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].original_query, "敏感问题")
        self.assertEqual(traces[0].rewritten_query, "改写后问题")
        self.assertEqual(traces[0].error_message, "Error: boom")

    def test_non_owner_evidence_preview_redacted(self):
        trace_id = self._record_trace(self.owner)
        self.service.record_retrieved_evidence(trace_id, [
            {"id": "c1", "source_name": "secret.pdf", "score": 0.8, "locator": {},
             "content_kind": "", "processor_kind": "", "content": "机密正文", "metadata": {}}
        ])
        rows = self.service.list_evidence(self.other, trace_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].file_name, "secret.pdf")  # 来源保留供排障
        self.assertEqual(rows[0].text_preview, "[redacted]")  # 正文隐藏

    def test_keyword_does_not_match_original_query(self):
        # 侧信道：非 owner 不能用查询原文关键词探测某条 trace 是否存在。
        self._record_trace(self.owner, original_query="独占关键词XYZ")
        traces = self.service.list_query_traces(self.other, keyword="独占关键词XYZ")
        self.assertEqual(traces, [])

    def test_keyword_matches_username(self):
        self._record_trace(self.owner)
        traces = self.service.list_query_traces(self.other, keyword="user101")
        self.assertEqual(len(traces), 1)


if __name__ == "__main__":
    unittest.main()
