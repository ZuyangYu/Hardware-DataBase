#!/usr/bin/env python
"""回答延迟基准：跑一个问题，输出耗时/token/工具调用分解（用于 A/B 配置对比）。

用法::

    uv run python scripts/bench_answer_latency.py "问题"
    uv run python scripts/bench_answer_latency.py "问题" --kb AAA --user aaa_admin --password xxx

输出 JSON：总耗时、首字耗时、LLM 调用次数与 token 数（取 llm_usage_ledger 前后差值）、
逐工具耗时。改 `.env`（如 `AGENT_CUSTOM_MAX_TOKENS` / 模型 / 温度）后重启再跑即可对比。

注意：
- 需要 API 在跑（默认 127.0.0.1:8002，可用 HDB_API_BASE 覆盖）且 worker-chat 在线；
- 会用真实模型额度；
- 同一问题多次运行结果有波动（agent 路径不确定），建议每个配置至少跑 2~3 次取趋势。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
import uuid
from contextlib import closing
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import src.settings as settings  # noqa: E402

BASE = os.getenv("HDB_API_BASE", "http://127.0.0.1:8002")


def _call(path: str, payload: dict | None = None, token: str | None = None, timeout: int = 120):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
    )
    return json.load(urllib.request.urlopen(request, timeout=timeout))


def _usage() -> tuple[int, int, int]:
    model = str(getattr(settings, "AGENT_CUSTOM_MODEL", "") or "")
    with closing(sqlite3.connect(f"file:{settings.AUTH_DB_PATH}?mode=ro", uri=True)) as conn:
        row = conn.execute(
            "select calls, prompt_tokens, completion_tokens from llm_usage_ledger "
            "where channel='interactive' and (? = '' or model = ?) "
            "order by calls desc limit 1",
            (model, model),
        ).fetchone()
    return (row or (0, 0, 0))[:3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?", default="知识库里有哪些 ADAS 相关的文档？简单说明每个文档大概讲什么。")
    parser.add_argument("--kb", default="AAA", help="知识库名（默认 AAA）")
    parser.add_argument("--user", default=os.getenv("HDB_BENCH_USER", "aaa_admin"))
    parser.add_argument("--password", default=os.getenv("HDB_BENCH_PASSWORD", ""))
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)

    if not args.password:
        parser.error("需要 --password 或环境变量 HDB_BENCH_PASSWORD")

    token = _call("/api/v1/login", {"username": args.user, "password": args.password})["token"]
    session = _call("/api/v1/conversations", {"kb_name": args.kb, "title": "延迟基准"}, token=token)
    before = _usage()
    turn = _call(
        f"/api/v1/conversations/{session['id']}/turns",
        {"query": args.question, "client_request_id": f"bench-{uuid.uuid4().hex[:8]}"},
        token=token,
    )
    turn_id = turn.get("turn", {}).get("id") or turn.get("id")
    _call(f"/api/v1/turns/{turn_id}/start", {}, token=token)

    started = time.monotonic()
    view: dict = {}
    status = None
    while time.monotonic() - started < args.timeout:
        time.sleep(2)
        view = _call(f"/api/v1/turns/{turn_id}", token=token)
        status = view.get("status")
        if status in {"completed", "failed", "cancelled"}:
            break

    after = _usage()
    metrics = view.get("metrics") or {}
    calls = metrics.get("tool_calls") or []
    print(json.dumps({
        "question": args.question,
        "status": status,
        "wall_s": round(time.monotonic() - started, 1),
        "total_ms": metrics.get("total_ms"),
        "first_token_ms": metrics.get("first_token_ms"),
        "query_mode": metrics.get("query_mode"),
        "tool_calls": len(calls),
        "tool_ms_sum": sum(int(item.get("latency_ms") or 0) for item in calls),
        "slowest_tool_ms": max((int(item.get("latency_ms") or 0) for item in calls), default=0),
        "tools": [item.get("tool") for item in calls],
        "llm_calls": after[0] - before[0],
        "prompt_tokens": after[1] - before[1],
        "completion_tokens": after[2] - before[2],
        "answer_chars": len(view.get("answer") or ""),
        "turn_id": turn_id,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
