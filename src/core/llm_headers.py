"""opencode zen Go 端点的会话路由头。

opencode.ai 的 Go 订阅端点要求每个请求携带 ``x-opencode-session``
（用于路由到稳定的后端会话），缺失会被 400 MissingSessionID 拒绝。
其他 OpenAI 兼容端点不受影响，返回空 dict 即可。

会话 ID 生成规则：进程内常驻一个 UUID（路由粘性、减少后端切换），
可用环境变量 ``HDB_OPENCODE_SESSION_ID`` 显式固定。
"""

from __future__ import annotations

import os
import uuid

_OPENCODE_HOST_MARK = "opencode.ai"
_SESSION_HEADER = "x-opencode-session"
_session_id: str | None = None


def _get_session_id() -> str:
    global _session_id
    if _session_id is None:
        _session_id = os.getenv("HDB_OPENCODE_SESSION_ID") or str(uuid.uuid4())
    return _session_id


def opencode_extra_headers(base_url: str | None) -> dict[str, str]:
    """返回需要附加到 OpenAI 兼容客户端的额外请求头。"""

    if not base_url or _OPENCODE_HOST_MARK not in str(base_url):
        return {}
    return {_SESSION_HEADER: _get_session_id()}
