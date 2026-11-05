"""Comprehensive functional matrix against the AAA knowledge base.

Covers: precise netlist lookup, table/SQL query, KB overview, cited artifact
publish, multi-turn artifact versioning, anti-hallucination, artifact
download, and the artifact -> assistant-turn association used by the chat UI.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE = os.getenv("HDB_API_BASE", "http://127.0.0.1:8002/api/v1").rstrip("/")
USER = os.getenv("HDB_E2E_USERNAME", "")
PASSWORD = os.getenv("HDB_E2E_PASSWORD", "")
KB = "AAA"
if not USER or not PASSWORD:
    print("set HDB_E2E_USERNAME/HDB_E2E_PASSWORD", file=sys.stderr)
    raise SystemExit(2)

RESULTS: list[dict] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"case": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def login() -> tuple[httpx.Client, dict]:
    c = httpx.Client(timeout=180)
    r = c.post(f"{BASE}/login", json={"username": USER, "password": PASSWORD})
    r.raise_for_status()
    return c, {"Authorization": f"Bearer {r.json()['token']}"}


def new_workspace(c: httpx.Client, h: dict, name: str) -> str:
    r = c.post(
        f"{BASE}/workspaces",
        json={"name": name, "kind": "knowledge", "kb_name": KB},
        headers=h,
    )
    r.raise_for_status()
    return str(r.json()["id"])


def new_session(
    c: httpx.Client, h: dict, title: str, *, workspace_id: str | None = None
) -> int:
    payload = {"kb_name": KB, "title": title}
    if workspace_id:
        payload["workspace_id"] = workspace_id
    r = c.post(f"{BASE}/conversations", json=payload, headers=h)
    r.raise_for_status()
    return int(r.json()["id"])


def run_turn(c: httpx.Client, h: dict, session_id: int, query: str, cap: int = 180) -> dict:
    r = c.post(
        f"{BASE}/conversations/{session_id}/turns",
        json={"query": query, "source_scope": "knowledge_base_only"},
        headers=h,
    )
    r.raise_for_status()
    turn = r.json()["turn"]
    turn_id = turn["id"]
    c.post(f"{BASE}/turns/{turn_id}/start", headers=h).raise_for_status()
    deadline = time.monotonic() + cap
    while time.monotonic() < deadline:
        time.sleep(2)
        current = c.get(f"{BASE}/turns/{turn_id}", headers=h).json()
        if current.get("status") in {"completed", "failed", "cancelled"}:
            return current
    return c.get(f"{BASE}/turns/{turn_id}", headers=h).json()


def artifacts(c: httpx.Client, h: dict, session_id: int) -> list[dict]:
    return c.get(f"{BASE}/conversations/{session_id}/artifacts", headers=h).json().get("items") or []


def messages(c: httpx.Client, h: dict, session_id: int) -> list[dict]:
    data = c.get(f"{BASE}/conversations/{session_id}/messages", headers=h).json()
    return data if isinstance(data, list) else data.get("items") or []


def download_text(c: httpx.Client, h: dict, artifact: dict) -> str:
    return c.get(f"http://127.0.0.1:8002{artifact['download_url']}", headers=h).text


def case_precise(c, h):
    sid = new_session(c, h, "AAA precise")
    turn = run_turn(c, h, sid, "TCN2 网表里 U1800 的型号是什么？给出引用编号。")
    answer = turn.get("answer") or ""
    evidence = (turn.get("summary") or {}).get("evidence") or []
    ok = turn.get("status") == "completed" and "TPS62872" in answer and bool(evidence)
    record("KB 精确位号（U1800）", ok, f"status={turn.get('status')} evidence={len(evidence)} answer={answer[:60]!r}")


def case_table(c, h):
    sid = new_session(c, h, "AAA table")
    turn = run_turn(
        c,
        h,
        sid,
        "测试覆盖表里一共有多少条记录？请给出数字和来源。",
    )
    answer = turn.get("answer") or ""
    ok = turn.get("status") == "completed" and any(ch.isdigit() for ch in answer)
    record("KB 表格统计", ok, f"status={turn.get('status')} answer={answer[:80]!r}")


def case_overview(c, h):
    sid = new_session(c, h, "AAA overview")
    turn = run_turn(c, h, sid, "这个知识库里都有哪些资料？帮我概览一下。")
    answer = turn.get("answer") or ""
    ok = (
        turn.get("status") == "completed"
        and ("文档" in answer or "表格" in answer)
        and ("电路" in answer or "网表" in answer)
    )
    record("KB 资料概览", ok, f"status={turn.get('status')} answer={answer[:80]!r}")


def case_publish_and_continue(c, h):
    wid = new_workspace(c, h, f"AAA artifact {time.time_ns()}")
    sid = new_session(c, h, "AAA artifact", workspace_id=wid)
    first = run_turn(
        c,
        h,
        sid,
        "查询 TCN2 网表里 U1800 的型号和封装，用 workspace_publish_artifact 发布 u1800.md，"
        "内容必须包含型号、封装和引用编号；不要在回答里写 out/ 或磁盘路径。",
    )
    first_answer = first.get("answer") or ""
    first_arts = artifacts(c, h, sid)
    first_art = next((a for a in first_arts if a["name"] == "u1800.md"), None)
    first_content = download_text(c, h, first_art) if first_art else ""
    publish_ok = (
        first.get("status") == "completed"
        and first_art is not None
        and "TPS62872" in first_content
        and "[1]" in first_content
        and first_art.get("turn_id") == first.get("id")
        and "out/" not in first_answer
    )
    record(
        "KB 检索 + 发布带引用产物",
        publish_ok,
        f"status={first.get('status')} artifact={first_art and first_art['name']} turn_match={first_art and first_art.get('turn_id') == first.get('id')}",
    )

    second = run_turn(
        c,
        h,
        sid,
        "把 u1800.md 更新为包含厂家 TI 和引用编号，保留文件名。",
    )
    second_answer = second.get("answer") or ""
    all_arts = artifacts(c, h, sid)
    versions = sorted(a["version"] for a in all_arts if a["name"] == "u1800.md")
    latest = max(
        (a for a in all_arts if a["name"] == "u1800.md"),
        key=lambda a: a["version"],
        default=None,
    )
    latest_content = download_text(c, h, latest) if latest else ""
    continue_ok = (
        second.get("status") == "completed"
        and versions == [1, 2]
        and "TPS62872" in latest_content
        and "TI" in latest_content
        and "[1]" in latest_content
        and latest.get("turn_id") == second.get("id")
        and "out/" not in second_answer
    )
    record(
        "KB 多轮继续 + 产物版本",
        continue_ok,
        f"status={second.get('status')} versions={versions} latest_turn_match={latest and latest.get('turn_id') == second.get('id')}",
    )

    # The chat UI groups artifacts by the assistant message's turn_id.
    assistant_turns = [
        m.get("turn_id") for m in messages(c, h, sid) if m.get("role") == "assistant"
    ]
    association_ok = bool(
        first_art
        and first_art.get("turn_id") in assistant_turns
        and latest
        and latest.get("turn_id") in assistant_turns
    )
    record(
        "产物挂到对应 assistant 消息",
        association_ok,
        f"assistant_turns={assistant_turns}",
    )


def case_negative(c, h):
    sid = new_session(c, h, "AAA negative")
    turn = run_turn(c, h, sid, "AAA 知识库里有没有型号 U9999 的资料？")
    answer = turn.get("answer") or ""
    honest = any(word in answer for word in ("未找到", "没有", "待确认", "证据不足", "未出现"))
    ok = turn.get("status") == "completed" and honest
    record("KB 反编造（不存在的型号）", ok, f"status={turn.get('status')} answer={answer[:80]!r}")


def main() -> int:
    c, h = login()
    case_precise(c, h)
    case_table(c, h)
    case_overview(c, h)
    case_publish_and_continue(c, h)
    case_negative(c, h)
    passed = sum(1 for item in RESULTS if item["ok"])
    print(f"\n===> {passed}/{len(RESULTS)} passed")
    Path(os.getenv("HDB_AAA_RESULTS", "/tmp/functional_aaa.json")).write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
