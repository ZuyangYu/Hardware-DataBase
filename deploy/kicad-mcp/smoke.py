#!/usr/bin/env python
"""Live smoke test for the governed MCP read + write channel.

Drives the full operator/employee flow against a running HDB API and the
reference KiCad MCP service (``deploy/kicad-mcp/server.py``):

  register -> test connection -> enable -> write whitelist -> subscribe
  -> real chat turn calling the read tool
  -> real chat turn creating a write proposal
  -> confirm -> worker executes -> download candidate artifact (SHA-256 check)

Secrets are read from arguments or environment variables and never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from typing import Any

import httpx

READ_PROMPT = (
    "请调用外部插件工具 mcp__{slug}__kicad_analyze_schematic，"
    '参数 title="live-smoke"、symbol_count=4；只调用一次，然后用一句话总结返回内容。'
)
WRITE_PROMPT = (
    "请调用外部插件写入工具 mcp__{slug}__kicad_create_schematic，"
    '参数 title="live-smoke"、symbol_count=6；只调用一次，然后说明提案状态。'
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8001")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--slug", default="kicad")
    parser.add_argument("--admin-username", default=os.getenv("AUTH_DEFAULT_ADMIN_USERNAME", "admin"))
    parser.add_argument("--admin-password", default=os.getenv("AUTH_DEFAULT_ADMIN_PASSWORD", ""))
    parser.add_argument("--employee-username", required=True)
    parser.add_argument("--employee-password", default=os.getenv("MCP_SMOKE_EMPLOYEE_PASSWORD", ""))
    parser.add_argument("--department-ids", default="", help="逗号分隔；留空则部门范围=all")
    parser.add_argument("--turn-timeout", type=int, default=300)
    parser.add_argument("--proposal-timeout", type=int, default=180)
    return parser.parse_args()


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(client: httpx.Client, username: str, password: str) -> str:
    if not password:
        raise SystemExit(f"missing password for {username}")
    resp = client.post("/api/v1/login", json={"username": username, "password": password})
    resp.raise_for_status()
    return resp.json()["token"]


def register_server(
    client: httpx.Client, admin: str, args: argparse.Namespace
) -> dict[str, Any]:
    department_ids = [
        int(item) for item in str(args.department_ids).split(",") if item.strip()
    ]
    payload = {
        "slug": args.slug,
        "name": "KiCad 格式示例生成器（参考服务）",
        "transport": "streamable_http",
        "url": args.mcp_url,
        "description": (
            "MCP 参考实现：只按参数返回示例摘要并生成简单 KiCad 格式候选文件；"
            "不读取或分析真实原理图，不调用 KiCad，也不运行 ERC。"
        ),
        "department_scope": "departments" if department_ids else "all",
        "department_ids": department_ids,
        "tags": ["EDA", "kicad"],
    }
    resp = client.post("/api/v1/plugins/mcp", json=payload, headers=hdr(admin))
    if resp.status_code == 409:
        resp = client.get(f"/api/v1/plugins/mcp/{args.slug}", headers=hdr(admin))
    resp.raise_for_status()
    return resp.json()["server"]


def run_turn(client: httpx.Client, token: str, prompt: str, timeout: int) -> dict[str, Any]:
    conv = client.post(
        "/api/v1/conversations",
        json={"kb_name": "__general__", "title": "MCP live smoke"},
        headers=hdr(token),
    )
    conv.raise_for_status()
    session_id = conv.json()["id"]
    turn = client.post(
        f"/api/v1/conversations/{session_id}/turns",
        json={"query": prompt, "client_request_id": f"mcp-smoke-{time.time_ns()}"},
        headers=hdr(token),
    )
    turn.raise_for_status()
    turn_id = turn.json()["turn"]["id"]
    client.post(f"/api/v1/turns/{turn_id}/start", headers=hdr(token))
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = client.get(f"/api/v1/turns/{turn_id}", headers=hdr(token)).json()
        if view.get("status") in {"completed", "failed", "cancelled"}:
            return {
                "turn_id": turn_id,
                "session_id": session_id,
                "status": view["status"],
                "answer": str(view.get("answer") or "")[:400],
                "error": str(view.get("error_message") or ""),
            }
        time.sleep(2)
    return {"turn_id": turn_id, "session_id": session_id, "status": "timeout", "answer": "", "error": ""}


def latest_proposal(client: httpx.Client, token: str, slug: str) -> dict[str, Any] | None:
    resp = client.get("/api/v1/plugins/mcp/write-proposals", headers=hdr(token))
    resp.raise_for_status()
    items = [item for item in resp.json()["items"] if item["slug"] == slug]
    return items[0] if items else None


def main() -> int:
    args = parse_args()
    client = httpx.Client(base_url=args.base, timeout=60)

    admin = login(client, args.admin_username, args.admin_password)
    employee = login(client, args.employee_username, args.employee_password)

    server = register_server(client, admin, args)
    print(f"[1] registered slug={server['slug']} enabled={server['enabled']}")

    tested = client.post(f"/api/v1/plugins/mcp/{args.slug}/test", headers=hdr(admin))
    print(f"[2] test connection: {tested.status_code} {tested.json().get('message')} "
          f"tools={tested.json().get('tool_count')}")
    tested.raise_for_status()

    enabled = client.put(
        f"/api/v1/plugins/mcp/{args.slug}/status",
        json={"enabled": True},
        headers=hdr(admin),
    )
    print(f"[3] enable: {enabled.status_code}")
    enabled.raise_for_status()

    write_tools = client.put(
        f"/api/v1/plugins/mcp/{args.slug}/write-tools",
        json={"write_tools": ["kicad_create_schematic"]},
        headers=hdr(admin),
    )
    print(f"[4] write whitelist: {write_tools.status_code}")
    write_tools.raise_for_status()

    tools = client.get(
        f"/api/v1/plugins/mcp/{args.slug}/tools", headers=hdr(admin)
    ).json()["items"]
    for tool in tools:
        print(
            f"    tool {tool['name']}: read_only={tool['read_only']} "
            f"write_allowed={tool['write_allowed']} blocked={tool['blocked_reason'] or '-'}"
        )

    subscribed = client.post(
        f"/api/v1/plugins/mcp/{args.slug}/subscribe",
        json={"allow_write": True},
        headers=hdr(employee),
    )
    print(f"[5] subscribe (allow_write): {subscribed.status_code}")
    subscribed.raise_for_status()

    read_turn = run_turn(client, employee, READ_PROMPT.format(slug=args.slug), args.turn_timeout)
    print(f"[6] read turn: {read_turn['status']} answer={read_turn['answer'][:160]!r}")

    write_turn = run_turn(client, employee, WRITE_PROMPT.format(slug=args.slug), args.turn_timeout)
    print(f"[7] write turn: {write_turn['status']} answer={write_turn['answer'][:160]!r}")

    proposal = latest_proposal(client, employee, args.slug)
    if proposal is None:
        print("!! no write proposal was created (model did not call the write tool)")
        return 2
    print(f"[8] proposal {proposal['proposal_id']} status={proposal['status']}")

    if proposal["status"] == "proposed":
        confirmed = client.post(
            f"/api/v1/plugins/mcp/write-proposals/{proposal['proposal_id']}/confirm",
            headers=hdr(employee),
        )
        print(f"[9] confirm: {confirmed.status_code}")

    deadline = time.time() + args.proposal_timeout
    while time.time() < deadline:
        proposal = latest_proposal(client, employee, args.slug) or proposal
        if proposal["status"] in {"completed", "failed", "expired", "rejected"}:
            break
        time.sleep(2)
    print(f"[10] proposal final status={proposal['status']} error={proposal.get('error_code') or '-'}")
    if proposal["status"] != "completed":
        return 3

    artifact = client.get(
        f"/api/v1/plugins/mcp/write-proposals/{proposal['proposal_id']}/artifact",
        headers=hdr(employee),
    )
    artifact.raise_for_status()
    digest = hashlib.sha256(artifact.content).hexdigest()
    print(
        f"[11] artifact {proposal['artifact_filename']} bytes={len(artifact.content)} "
        f"sha256_match={digest == proposal['artifact_sha256']}"
    )

    audits = client.get(
        "/api/v1/logs/audit",
        params={"action": "plugins.mcp.call", "limit": 20},
        headers=hdr(admin),
    ).json()
    print(f"[12] plugins.mcp.call audit rows: {len(audits)}")
    return 0 if digest == proposal["artifact_sha256"] else 4


if __name__ == "__main__":
    sys.exit(main())
