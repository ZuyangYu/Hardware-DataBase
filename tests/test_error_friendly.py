"""friendly_error_message / failure_category 的稳定文案与安全类目测试。"""

import unittest

from src.core.app_logs import failure_category
from src.core.error_friendly import friendly_error_message


class FriendlyErrorMessageTests(unittest.TestCase):
    def test_upstream_stream_failure_gets_actionable_message(self):
        exc = RuntimeError(
            "Streaming response failed: [server_error] upstream service failed"
        )
        message = friendly_error_message(exc)
        self.assertEqual(message, "模型服务暂时不可用，请稍后重新发送本问题。")
        self.assertEqual(failure_category(message), "model_unavailable")

    def test_gateway_errors_do_not_blame_the_user_network(self):
        for raw in ("502 Bad Gateway", "503 Service Unavailable", "504 Gateway Timeout"):
            message = friendly_error_message(RuntimeError(raw))
            self.assertEqual(message, "模型服务暂时不可用，请稍后重新发送本问题。")

    def test_rate_limit_timeout_connection_permission_keep_their_buckets(self):
        self.assertEqual(
            friendly_error_message(RuntimeError("429 Too Many Requests")),
            "模型服务调用过于频繁或额度受限，请稍后重试。",
        )
        self.assertEqual(
            friendly_error_message(RuntimeError("request timeout")),
            "模型服务响应超时，请稍后重试。",
        )
        self.assertEqual(
            friendly_error_message(RuntimeError("connection refused")),
            "无法连接模型服务，请检查网络或稍后重试。",
        )
        self.assertEqual(
            friendly_error_message(PermissionError("nope")),
            "没有执行该操作的权限。",
        )

    def test_unknown_error_keeps_generic_fallback(self):
        message = friendly_error_message(ValueError("boom"))
        self.assertEqual(message, "系统错误，请稍后重试；若持续出现请联系管理员。")
        self.assertEqual(failure_category(message), "system_error")


if __name__ == "__main__":
    unittest.main()
