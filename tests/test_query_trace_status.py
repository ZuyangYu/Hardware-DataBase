import unittest

from src.core.app_logs import query_trace_status


class QueryTraceStatusTests(unittest.TestCase):
    def test_normal_answer_is_success(self):
        self.assertEqual(query_trace_status("这是正常回答"), ("success", ""))

    def test_direct_exception_response_is_failed(self):
        self.assertEqual(
            query_trace_status("Error: backend exploded"),
            ("failed", "Error: backend exploded"),
        )

    def test_pipeline_system_error_response_is_failed(self):
        self.assertEqual(
            query_trace_status("系统错误: permission denied"),
            ("failed", "系统错误: permission denied"),
        )

    def test_ragflow_generation_error_response_is_failed(self):
        response = "RAGFlow retrieved relevant context, but answer generation failed: model timeout"
        self.assertEqual(query_trace_status(response), ("failed", response))


if __name__ == "__main__":
    unittest.main()
