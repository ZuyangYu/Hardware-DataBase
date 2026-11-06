"""Long project-mounted workflow: KB facts -> draw.io file -> iterate -> report.

Run against a live API:

    HDB_E2E_USERNAME=aaa_admin HDB_E2E_PASSWORD=... \
      uv run python scripts/functional_project_long.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

BASE = os.getenv("HDB_API_BASE", "http://127.0.0.1:8002/api/v1").rstrip("/")
USER = os.getenv("HDB_E2E_USERNAME", "")
PASSWORD = os.getenv("HDB_E2E_PASSWORD", "")
KB = os.getenv("HDB_E2E_KB", "AAA")
RESULTS: list[dict[str, Any]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"case": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def login() -> tuple[httpx.Client, dict[str, str]]:
    client = httpx.Client(timeout=240)
    response = client.post(
        f"{BASE}/login", json={"username": USER, "password": PASSWORD}
    )
    response.raise_for_status()
    return client, {"Authorization": f"Bearer {response.json()['token']}"}


def run_turn(
    client: httpx.Client,
    headers: dict[str, str],
    session_id: int,
    query: str,
    *,
    cap: int = 600,
) -> dict[str, Any]:
    response = client.post(
        f"{BASE}/conversations/{session_id}/turns",
        json={"query": query},
        headers=headers,
    )
    response.raise_for_status()
    turn_id = response.json()["turn"]["id"]
    client.post(f"{BASE}/turns/{turn_id}/start", headers=headers).raise_for_status()
    deadline = time.monotonic() + cap
    while time.monotonic() < deadline:
        time.sleep(2)
        current = client.get(f"{BASE}/turns/{turn_id}", headers=headers).json()
        if current.get("status") in {"completed", "failed", "cancelled"}:
            return current
    return client.get(f"{BASE}/turns/{turn_id}", headers=headers).json()


def proposals(client: httpx.Client, headers: dict[str, str]) -> list[dict[str, Any]]:
    response = client.get(
        f"{BASE}/plugins/mcp/write-proposals?statuses=proposed", headers=headers
    )
    response.raise_for_status()
    return response.json().get("items") or []


def newest_proposal(
    client: httpx.Client, headers: dict[str, str], *, after: str
) -> dict[str, Any] | None:
    candidates = [
        item
        for item in proposals(client, headers)
        if item.get("tool_name") == "create_drawio_file"
        and str(item.get("created_at") or "") >= after
    ]
    return max(candidates, key=lambda item: item.get("created_at") or "", default=None)


def confirm_and_download(
    client: httpx.Client, headers: dict[str, str], proposal_id: str
) -> tuple[dict[str, Any] | None, bytes]:
    response = client.post(
        f"{BASE}/plugins/mcp/write-proposals/{proposal_id}/confirm", headers=headers
    )
    response.raise_for_status()
    deadline = time.monotonic() + 180
    final: dict[str, Any] | None = None
    all_items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        time.sleep(2)
        all_items = client.get(
            f"{BASE}/plugins/mcp/write-proposals", headers=headers
        ).json().get("items") or []
        final = next(
            (item for item in all_items if item.get("proposal_id") == proposal_id),
            None,
        )
        if final and final.get("status") in {
            "completed",
            "failed",
            "expired",
            "rejected",
        }:
            break
    if not final or final.get("status") != "completed":
        return final, b""
    response = client.get(
        f"{BASE}/plugins/mcp/write-proposals/{proposal_id}/artifact", headers=headers
    )
    response.raise_for_status()
    return final, response.content


def reject_pending(client: httpx.Client, headers: dict[str, str]) -> None:
    for item in proposals(client, headers):
        client.post(
            f"{BASE}/plugins/mcp/write-proposals/{item['proposal_id']}/reject",
            headers=headers,
        )


def parse_mxfile(content: bytes) -> ET.Element | None:
    try:
        return ET.fromstring(content) if content else None
    except ET.ParseError:
        return None


def case_project_mount(client: httpx.Client, headers: dict[str, str]) -> tuple[int, str] | None:
    stamp = str(time.time_ns())
    workspace = client.post(
        f"{BASE}/workspaces",
        headers=headers,
        json={"name": f"project-long-{stamp}", "kind": "knowledge", "kb_name": KB},
    )
    workspace.raise_for_status()
    workspace_id = str(workspace.json()["id"])
    session = client.post(
        f"{BASE}/conversations",
        headers=headers,
        json={"kb_name": KB, "title": f"project-long-{stamp}", "workspace_id": workspace_id},
    )
    session.raise_for_status()
    session_id = int(session.json()["id"])
    loaded = client.get(f"{BASE}/conversations/{session_id}", headers=headers)
    loaded.raise_for_status()
    listed = client.get(
        f"{BASE}/conversations", headers=headers, params={"workspace_id": workspace_id}
    )
    listed.raise_for_status()
    listed_ids = {int(item["id"]) for item in listed.json()}
    ok = (
        loaded.json().get("workspace_id") == workspace_id
        and session.json().get("workspace_id") == workspace_id
        and session_id in listed_ids
    )
    record(
        "项目工作区挂载与恢复",
        ok,
        f"workspace={workspace_id} session={session_id} "
        f"created={session.json().get('workspace_id')} loaded={loaded.json().get('workspace_id')}",
    )
    return (session_id, workspace_id) if ok else None


def case_project_workflow(
    client: httpx.Client, headers: dict[str, str], session_id: int, workspace_id: str
) -> None:
    first = run_turn(
        client,
        headers,
        session_id,
        "项目资料里 TCN2 网表的 U1800 型号和封装是什么？给出引用编号。",
    )
    first_answer = first.get("answer") or ""
    first_evidence = (first.get("summary") or {}).get("evidence") or []
    record(
        "项目多轮 1：知识库事实",
        first.get("status") == "completed"
        and "TPS62872" in first_answer
        and "PQFN" in first_answer
        and bool(first_evidence),
        f"status={first.get('status')} evidence={len(first_evidence)} answer={first_answer[:120]!r}",
    )

    stamp = str(time.time_ns())
    after = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    second = run_turn(
        client,
        headers,
        session_id,
        "基于刚才查到的 U1800 电源事实，画一条从输入电源到 U1800 再到负载的电源链路，"
        f"并生成 {stamp}-project-power.drawio 文件。",
    )
    proposal = newest_proposal(client, headers, after=after)
    second_answer = second.get("answer") or ""
    record(
        "项目多轮 2：基于项目数据创建图文件",
        second.get("status") == "completed" and proposal is not None,
        f"status={second.get('status')} proposal={proposal and proposal.get('proposal_id')} "
        f"answer={second_answer[:180]!r}",
    )
    if proposal is None:
        reject_pending(client, headers)
        return
    final, content = confirm_and_download(client, headers, proposal["proposal_id"])
    root = parse_mxfile(content)
    text = content.decode("utf-8", errors="ignore")
    record(
        "项目多轮 2：确认下载合法图且含项目位号",
        bool(final)
        and final.get("status") == "completed"
        and root is not None
        and "U1800" in text,
        f"status={final and final.get('status')} bytes={len(content)}",
    )

    after = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    third = run_turn(
        client,
        headers,
        session_id,
        "在刚才的项目电源链路上增加一个“负载”节点，保留 U1800，生成 "
        f"{stamp}-project-power-v2.drawio 文件。",
    )
    proposal2 = newest_proposal(client, headers, after=after)
    third_answer = third.get("answer") or ""
    record(
        "项目多轮 3：沿用项目图继续迭代",
        third.get("status") == "completed" and proposal2 is not None,
        f"status={third.get('status')} proposal={proposal2 and proposal2.get('proposal_id')} "
        f"answer={third_answer[:180]!r}",
    )
    if proposal2 is None:
        reject_pending(client, headers)
        return
    final2, content2 = confirm_and_download(client, headers, proposal2["proposal_id"])
    root2 = parse_mxfile(content2)
    text2 = content2.decode("utf-8", errors="ignore")
    record(
        "项目多轮 3：迭代图包含新节点",
        bool(final2)
        and final2.get("status") == "completed"
        and root2 is not None
        and "负载" in text2
        and "U1800" in text2,
        f"status={final2 and final2.get('status')} bytes={len(content2)}",
    )

    fourth = run_turn(
        client,
        headers,
        session_id,
        "请用 workspace_publish_artifact 发布 project-summary.md，内容包含 U1800 型号、封装、"
        "刚才两张图的文件名和至少一个引用编号；回答不要写 out/ 或磁盘路径。",
    )
    fourth_answer = fourth.get("answer") or ""
    artifacts_response = client.get(
        f"{BASE}/conversations/{session_id}/artifacts", headers=headers
    )
    artifacts_response.raise_for_status()
    artifact_items = artifacts_response.json().get("items") or []
    summary_artifact = next(
        (item for item in artifact_items if item.get("name") == "project-summary.md"),
        None,
    )
    summary_content = ""
    if summary_artifact:
        download = client.get(
            f"http://127.0.0.1:8002{summary_artifact['download_url']}", headers=headers
        )
        download.raise_for_status()
        summary_content = download.content.decode("utf-8", errors="ignore")
    record(
        "项目多轮 4：发布项目摘要产物",
        fourth.get("status") == "completed"
        and summary_artifact is not None
        and "TPS62872" in summary_content
        and "U1800" in summary_content
        and "[" in summary_content
        and "out/" not in fourth_answer,
        f"status={fourth.get('status')} artifact={summary_artifact and summary_artifact['name']} "
        f"bytes={len(summary_content)} answer={fourth_answer[:120]!r}",
    )

    fifth = run_turn(
        client,
        headers,
        session_id,
        "综合这个项目目前的事实、图和摘要，下一步最需要确认的三件事是什么？",
    )
    fifth_answer = fifth.get("answer") or ""
    record(
        "项目多轮 5：长流程上下文延续",
        fifth.get("status") == "completed"
        and any(token in fifth_answer for token in ("U1800", "TPS62872", "封装", "图", "摘要")),
        f"status={fifth.get('status')} answer={fifth_answer[:220]!r}",
    )

    # Project isolation: the session is still in the workspace and its artifacts
    # are scoped to the same session/workspace.
    listed = client.get(
        f"{BASE}/workspaces/{workspace_id}/sessions", headers=headers
    )
    listed.raise_for_status()
    session_ids = {int(item["id"]) for item in listed.json()}
    record(
        "项目工作区会话归属",
        session_id in session_ids,
        f"workspace={workspace_id} sessions={sorted(session_ids)}",
    )


def main() -> int:
    if not USER or not PASSWORD:
        print("set HDB_E2E_USERNAME/HDB_E2E_PASSWORD", file=sys.stderr)
        return 2
    client, headers = login()
    mounted = case_project_mount(client, headers)
    if mounted:
        case_project_workflow(client, headers, mounted[0], mounted[1])
    else:
        record("项目长流程", False, "workspace/session mount failed")
    reject_pending(client, headers)
    passed = sum(1 for item in RESULTS if item["ok"])
    print(f"\n===> {passed}/{len(RESULTS)} passed")
    Path(os.getenv("HDB_PROJECT_RESULTS", "/tmp/functional_project_long.json")).write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
