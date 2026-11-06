"""Functional multi-turn matrix: general chat, KB follow-up, draw.io files.

Run against a live API:

    HDB_E2E_USERNAME=aaa_admin HDB_E2E_PASSWORD=... \
      uv run python scripts/functional_multi_turn.py
"""

from __future__ import annotations

import hashlib
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
    client = httpx.Client(timeout=180)
    response = client.post(
        f"{BASE}/login", json={"username": USER, "password": PASSWORD}
    )
    response.raise_for_status()
    return client, {"Authorization": f"Bearer {response.json()['token']}"}


def new_session(client: httpx.Client, headers: dict[str, str], *, kb: str, title: str) -> int:
    response = client.post(
        f"{BASE}/conversations",
        json={"kb_name": kb, "title": title},
        headers=headers,
    )
    response.raise_for_status()
    return int(response.json()["id"])


def run_turn(
    client: httpx.Client,
    headers: dict[str, str],
    session_id: int,
    query: str,
    *,
    attachment_ids: list[str] | None = None,
    cap: int = 240,
) -> dict[str, Any]:
    body: dict[str, Any] = {"query": query}
    if attachment_ids:
        body["attachment_ids"] = attachment_ids
    response = client.post(
        f"{BASE}/conversations/{session_id}/turns", json=body, headers=headers
    )
    response.raise_for_status()
    turn_id = response.json()["turn"]["id"]
    started = client.post(f"{BASE}/turns/{turn_id}/start", headers=headers)
    started.raise_for_status()
    deadline = time.monotonic() + cap
    while time.monotonic() < deadline:
        time.sleep(2)
        current = client.get(f"{BASE}/turns/{turn_id}", headers=headers).json()
        if current.get("status") in {"completed", "failed", "cancelled"}:
            return current
    return client.get(f"{BASE}/turns/{turn_id}", headers=headers).json()


def upload_drawio(
    client: httpx.Client, headers: dict[str, str], session_id: int, filename: str, xml: str
) -> str:
    response = client.post(
        f"{BASE}/conversations/{session_id}/attachments",
        headers=headers,
        files={"file": (filename, xml.encode("utf-8"), "application/vnd.jgraph.mxfile")},
        params={"usage_hint": "reference"},
    )
    response.raise_for_status()
    return str(response.json()["attachment_id"])


def wait_attachment_ready(
    client: httpx.Client, headers: dict[str, str], session_id: int, attachment_id: str
) -> dict[str, Any] | None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        items = client.get(
            f"{BASE}/conversations/{session_id}/attachments", headers=headers
        ).json()
        if isinstance(items, dict):
            items = items.get("items") or []
        record = next(
            (item for item in items if item.get("attachment_id") == attachment_id),
            None,
        )
        if record and record.get("parse_status") in {"ready", "degraded", "failed"}:
            return record
        time.sleep(2)
    return None


def write_proposals(
    client: httpx.Client, headers: dict[str, str], statuses: str = ""
) -> list[dict[str, Any]]:
    suffix = f"?statuses={statuses}" if statuses else ""
    response = client.get(f"{BASE}/plugins/mcp/write-proposals{suffix}", headers=headers)
    response.raise_for_status()
    return response.json().get("items") or []


def newest_proposal(
    client: httpx.Client, headers: dict[str, str], *, after: str = ""
) -> dict[str, Any] | None:
    items = write_proposals(client, headers, "proposed")
    candidates = [
        item
        for item in items
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
    while time.monotonic() < deadline:
        time.sleep(2)
        items = write_proposals(client, headers)
        final = next(
            (item for item in items if item.get("proposal_id") == proposal_id), None
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
    for item in write_proposals(client, headers, "proposed"):
        client.post(
            f"{BASE}/plugins/mcp/write-proposals/{item['proposal_id']}/reject",
            headers=headers,
        )


def case_general_follow_up(client: httpx.Client, headers: dict[str, str]) -> None:
    sid = new_session(client, headers, kb="__general__", title="multi general")
    first = run_turn(
        client,
        headers,
        sid,
        "记住：我的项目代号是 ORION-7。请只回答“收到”。",
    )
    second = run_turn(client, headers, sid, "我刚才说的项目代号是什么？")
    answer = second.get("answer") or ""
    record(
        "普通对话多轮记忆",
        first.get("status") == "completed"
        and second.get("status") == "completed"
        and "ORION-7" in answer,
        f"first={first.get('status')} second={second.get('status')} answer={answer[:120]!r}",
    )

    third = run_turn(client, headers, sid, "能不能优化一下")
    follow_answer = third.get("answer") or ""
    record(
        "普通对话缺少对象时追问而非 KB 兜底",
        third.get("status") == "completed"
        and "知识库中没有找到相关上下文" not in follow_answer
        and any(
            word in follow_answer
            for word in ("哪个", "哪份", "什么", "文件", "对象", "内容", "图")
        ),
        f"status={third.get('status')} answer={follow_answer[:180]!r}",
    )


def case_kb_follow_up(client: httpx.Client, headers: dict[str, str]) -> None:
    sid = new_session(client, headers, kb=KB, title="multi kb follow-up")
    first = run_turn(
        client,
        headers,
        sid,
        "TCN2 网表里 U1800 的型号是什么？给出引用编号。",
    )
    second = run_turn(client, headers, sid, "它的封装是什么？")
    first_answer = first.get("answer") or ""
    second_answer = second.get("answer") or ""
    evidence = (second.get("summary") or {}).get("evidence") or []
    record(
        "知识库多轮追问（型号→封装）",
        first.get("status") == "completed"
        and second.get("status") == "completed"
        and "TPS62872" in first_answer
        and "PQFN" in second_answer,
        f"first={first.get('status')} second={second.get('status')} "
        f"evidence={len(evidence)} answer={second_answer[:120]!r}",
    )


def _drawio_xml(nodes: list[tuple[str, str]], *, horizontal: bool = False) -> str:
    cells: list[str] = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
    previous: str | None = None
    for index, (node_id, label) in enumerate(nodes, start=2):
        x = 40 + (index - 2) * 180 if horizontal else 40
        y = 125 if horizontal else 40 + (index - 2) * 110
        cells.append(
            f'<mxCell id="{node_id}" value="{label}" style="rounded=1;" vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="120" height="60" as="geometry"/></mxCell>'
        )
        if previous:
            cells.append(
                f'<mxCell id="e{index}" edge="1" parent="1" source="{previous}" target="{node_id}">'
                '<mxGeometry relative="1" as="geometry"/></mxCell>'
            )
        previous = node_id
    return (
        '<mxfile><diagram name="MultiTurn"><mxGraphModel><root>'
        + "".join(cells)
        + "</root></mxGraphModel></diagram></mxfile>"
    )


def _root_text(content: bytes) -> str:
    return content.decode("utf-8", errors="ignore")


def case_drawio_multi_turn(client: httpx.Client, headers: dict[str, str]) -> None:
    sid = new_session(client, headers, kb="__general__", title="multi drawio")
    stamp = str(time.time_ns())
    source_name = f"multi-login-{stamp}.drawio"
    source_xml = _drawio_xml(
        [("start", "开始"), ("process", "处理"), ("end", "结束")]
    )
    attachment_id = upload_drawio(client, headers, sid, source_name, source_xml)
    attachment = wait_attachment_ready(client, headers, sid, attachment_id)
    record(
        "drawio 附件上传与解析",
        bool(attachment) and attachment.get("parse_status") == "ready",
        f"attachment={attachment_id} status={attachment and attachment.get('parse_status')}",
    )
    if not attachment or attachment.get("parse_status") != "ready":
        return

    after_first = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    first = run_turn(
        client,
        headers,
        sid,
        "把这个图优化成横向排列，并生成 optimized.drawio 文件。",
        attachment_ids=[attachment_id],
    )
    proposal = newest_proposal(client, headers, after=after_first)
    first_answer = first.get("answer") or ""
    record(
        "drawio 多轮第 1 轮创建文件提案",
        first.get("status") == "completed" and proposal is not None,
        f"status={first.get('status')} proposal={proposal and proposal.get('proposal_id')} "
        f"answer={first_answer[:160]!r}",
    )
    if proposal is None:
        reject_pending(client, headers)
        return

    final, content = confirm_and_download(client, headers, proposal["proposal_id"])
    root = None
    try:
        root = ET.fromstring(content) if content else None
    except ET.ParseError:
        root = None
    text = _root_text(content)
    record(
        "drawio 第 1 轮确认后下载合法文件",
        bool(final)
        and final.get("status") == "completed"
        and root is not None
        and root.tag == "mxfile"
        and "开始" in text
        and "结束" in text,
        f"status={final and final.get('status')} bytes={len(content)} "
        f"sha={hashlib.sha256(content).hexdigest()[:12]}",
    )

    after_second = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    second = run_turn(
        client,
        headers,
        sid,
        "还是这个图：在刚才优化的基础上再加一个“审计”节点，生成 optimized-v2.drawio 文件。",
        attachment_ids=[attachment_id],
    )
    proposal2 = newest_proposal(client, headers, after=after_second)
    second_answer = second.get("answer") or ""
    record(
        "drawio 多轮第 2 轮沿用上下文创建提案",
        second.get("status") == "completed" and proposal2 is not None,
        f"status={second.get('status')} proposal={proposal2 and proposal2.get('proposal_id')} "
        f"answer={second_answer[:180]!r}",
    )
    if proposal2 is None:
        reject_pending(client, headers)
        return

    final2, content2 = confirm_and_download(client, headers, proposal2["proposal_id"])
    text2 = _root_text(content2)
    try:
        root2 = ET.fromstring(content2) if content2 else None
    except ET.ParseError:
        root2 = None
    record(
        "drawio 多轮第 2 轮确认后包含新节点",
        bool(final2)
        and final2.get("status") == "completed"
        and root2 is not None
        and "审计" in text2
        and "开始" in text2
        and "结束" in text2,
        f"status={final2 and final2.get('status')} bytes={len(content2)} "
        f"answer={second_answer[:120]!r}",
    )
    reject_pending(client, headers)


def main() -> int:
    if not USER or not PASSWORD:
        print("set HDB_E2E_USERNAME/HDB_E2E_PASSWORD", file=sys.stderr)
        return 2
    client, headers = login()
    case_general_follow_up(client, headers)
    case_kb_follow_up(client, headers)
    case_drawio_multi_turn(client, headers)
    reject_pending(client, headers)
    passed = sum(1 for item in RESULTS if item["ok"])
    print(f"\n===> {passed}/{len(RESULTS)} passed")
    Path(os.getenv("HDB_MULTI_RESULTS", "/tmp/functional_multi_turn.json")).write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
