"""把底层异常折叠成可对用户展示的稳定文案，细节只进日志。"""
from __future__ import annotations


def friendly_error_message(exc: BaseException) -> str:
    raw = str(exc)
    lowered = raw.lower()
    if "429" in raw or "rate limit" in lowered or "usage limit" in lowered:
        return "模型服务调用过于频繁或额度受限，请稍后重试。"
    if "timeout" in lowered or "timed out" in lowered:
        return "模型服务响应超时，请稍后重试。"
    if "connection" in lowered or "unavailable" in lowered:
        return "无法连接模型服务，请检查网络或稍后重试。"
    if isinstance(exc, PermissionError):
        return "没有执行该操作的权限。"
    return "系统错误，请稍后重试；若持续出现请联系管理员。"
