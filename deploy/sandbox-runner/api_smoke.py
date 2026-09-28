#!/usr/bin/env python3
"""API-level smoke test for the deployed sandbox plane (dev-l1 profile).

Drives the real HDB HTTP API end to end:

  admin:   register bundle -> review -> tested -> enable
  employee: create task -> submit -> observe -> collect -> list artifacts
  admin:   register gateway -> review -> test -> enable -> attach -> discover
           -> invoke (real stdio MCP tool)
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import string
import sys
import time

import httpx

import src.settings as s

BASE = os.getenv("HDB_SMOKE_BASE", "http://127.0.0.1:8001")
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
BUNDLE_ENTRYPOINT = os.path.join(HERE, "bundles", "report", "entrypoint.py")
DEPLOYMENTS_FILE = os.path.join(REPO, "deploy", "mcp-gateway", "deployments.json")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"[{'ok' if condition else 'FAIL'}] {name}{' - ' + detail if detail else ''}")


def login(client: httpx.Client, username: str, password: str) -> str:
    response = client.post("/api/v1/login", json={"username": username, "password": password})
    response.raise_for_status()
    return response.json()["token"]


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=120)
    admin = login(client, s.AUTH_DEFAULT_ADMIN_USERNAME, s.AUTH_DEFAULT_ADMIN_PASSWORD)

    username = "sandbox_live_e2e"
    password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
    users = client.get("/api/v1/users", headers=hdr(admin)).json()
    existing = next((item for item in users if item["username"] == username), None)
    if existing:
        uid = existing["id"]
        client.put(f"/api/v1/users/{uid}/password", json={"new_password": password}, headers=hdr(admin))
    else:
        created = client.post(
            "/api/v1/users",
            json={"username": username, "password": password, "role": "employee", "department_id": 47},
            headers=hdr(admin),
        )
        created.raise_for_status()
    employee = login(client, username, password)
    check("employee ready", bool(employee))

    with open(BUNDLE_ENTRYPOINT, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    register = client.post(
        "/api/v1/sandbox/bundles",
        json={
            "bundle_ref": "bundle/report",
            "bundle_digest": digest,
            "profile_ref": "profile/dev-l1",
            "required_capabilities": ["sandbox.execute", "sandbox.write_artifact"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": 80},
                    "rows": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
        headers=hdr(admin),
    )
    check("bundle registered via API", register.status_code == 200, f"{register.status_code} {register.text[:120]}")
    if register.json()["bundle"].get("approval_state") == "enabled" or register.json()["bundle"].get("enabled"):
        check("bundle already enabled (review/tested/enable skipped)", True)
    else:
        for action in ("review", "tested", "enable"):
            response = client.post(f"/api/v1/sandbox/bundles/bundle/report/{action}", headers=hdr(admin))
            check(f"bundle {action}", response.status_code == 200, f"{response.status_code} {response.text[:100]}")

    task = client.post(
        "/api/v1/sandbox/tasks",
        json={
            "idempotency_key": f"api-smoke-{time.time_ns()}",
            "bundle_ref": "bundle/report",
            "bundle_digest": digest,
            "profile_ref": "profile/dev-l1",
            "required_capabilities": ["sandbox.execute", "sandbox.write_artifact"],
            "parameters": {"title": "api-smoke", "rows": 2},
        },
        headers=hdr(employee),
    )
    check("task admitted via API", task.status_code == 202, f"{task.status_code} {task.text[:140]}")
    if task.status_code != 202:
        print(json.dumps({"pass": PASS, "fail": FAIL}, ensure_ascii=False))
        return 1
    task_id = task.json()["task"]["task_id"]
    submitted = client.post(f"/api/v1/sandbox/tasks/{task_id}/submit", headers=hdr(employee))
    if submitted.status_code == 409:
        # The API dispatcher can claim the freshly admitted task before this
        # explicit submit. Observe its real execution instead of failing on a
        # benign claim race.
        for _ in range(20):
            current = client.get(f"/api/v1/sandbox/tasks/{task_id}", headers=hdr(employee))
            record = current.json().get("task") or {} if current.status_code == 200 else {}
            if record.get("execution_id") and record.get("status") in {"running", "collecting", "succeeded"}:
                submitted = current
                break
            time.sleep(0.2)
    check("task submitted to runner", submitted.status_code in {200, 202}, f"{submitted.status_code} {submitted.text[:140]}")
    observed = client.get(f"/api/v1/sandbox/tasks/{task_id}/observe", headers=hdr(employee))
    check("task observed", observed.status_code == 200, f"{observed.status_code}")
    collected = client.post(f"/api/v1/sandbox/tasks/{task_id}/collect", headers=hdr(employee))
    for _ in range(20):
        if collected.status_code == 200:
            break
        if collected.status_code not in {409, 502}:
            break
        time.sleep(0.2)
        collected = client.post(f"/api/v1/sandbox/tasks/{task_id}/collect", headers=hdr(employee))
    check("task collected", collected.status_code == 200, f"{collected.status_code} {collected.text[:140]}")
    artifacts = client.get(f"/api/v1/sandbox/tasks/{task_id}/artifacts", headers=hdr(employee))
    items = (artifacts.json().get("items") if artifacts.status_code == 200 else []) or []
    check("broker artifacts listed", len(items) >= 1, f"{artifacts.status_code} items={len(items)}")
    if items:
        check("artifact committed with sha256", items[0].get("state") in {"committed", "consumed"} and bool(items[0].get("sha256")))

        # Real HTTP isolation check across two employees in different departments.
        departments = client.get("/api/v1/departments", headers=hdr(admin)).json()
        other_department = next(int(item["id"]) for item in departments if int(item["id"]) not in {1, 47})
        other_name = "sandbox_isolation_e2e"
        other_password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
        users = client.get("/api/v1/users", headers=hdr(admin)).json()
        other_user = next((item for item in users if item["username"] == other_name), None)
        if other_user:
            client.put(f"/api/v1/users/{other_user['id']}/password", json={"new_password": other_password}, headers=hdr(admin)).raise_for_status()
        else:
            client.post("/api/v1/users", json={
                "username": other_name, "password": other_password,
                "role": "employee", "department_id": other_department,
            }, headers=hdr(admin)).raise_for_status()
        other = login(client, other_name, other_password)
        denied_task = client.get(f"/api/v1/sandbox/tasks/{task_id}", headers=hdr(other))
        denied_list = client.get(f"/api/v1/sandbox/tasks/{task_id}/artifacts", headers=hdr(other))
        denied_bytes = client.get(f"/api/v1/sandbox/artifacts/{items[0]['artifact_id']}", headers=hdr(other))
        check("other department cannot see task", denied_task.status_code in {403, 404}, f"{denied_task.status_code}")
        check("other department cannot list outputs", denied_list.status_code in {403, 404}, f"{denied_list.status_code}")
        check("other department cannot read artifact bytes", denied_bytes.status_code in {403, 404}, f"{denied_bytes.status_code}")

    # Text-only Skill catalog: declaration, review, tested, enable.
    skill = client.post(
        "/api/v1/sandbox/skills",
        json={
            "skill_ref": "skill/report-style",
            "version": "1.0.0",
            "content": "报告写作规范：标题简洁，表格列名使用中文，数值保留单位。",
            "source_ref": "internal://style-guide",
            "catalog_source": "curated",
        },
        headers=hdr(admin),
    )
    check("skill registered via API", skill.status_code == 200, f"{skill.status_code} {skill.text[:120]}")
    for action in ("review", "tested", "enable"):
        response = client.post(
            f"/api/v1/sandbox/skills/skill/report-style/{action}", headers=hdr(admin)
        )
        # 200 on first deployment; 409 when the state machine already advanced.
        check(f"skill {action}", response.status_code in {200, 409}, f"{response.status_code}")
    skills = client.get("/api/v1/sandbox/skills", headers=hdr(admin))
    items = skills.json().get("items") or [] if skills.status_code == 200 else []
    state = next((item.get("approval_state") for item in items if item.get("skill_ref") == "skill/report-style"), "")
    check("skill listed and enabled", state == "enabled", f"{skills.status_code} state={state}")

    with open(DEPLOYMENTS_FILE, encoding="utf-8") as handle:
        deployment_digest = json.load(handle)["deployments"]["deploy/sandbox-info"]["image_digest"]
    gateway = client.post(
        "/api/v1/sandbox/gateways",
        json={
            "deployment_ref": "deploy/sandbox-info",
            "profile_ref": "profile/dev-l1",
            "endpoint": s.SANDBOX_GATEWAY_URL,
            "digest": deployment_digest,
            "capabilities": ["external_tool.invoke"],
            "tool_allowlist": {
                "sandbox_runtime_info": {
                    "type": "object",
                    "properties": {"label": {"type": "string", "maxLength": 64}},
                    "required": ["label"],
                    "additionalProperties": False,
                }
            },
        },
        headers=hdr(admin),
    )
    check("gateway registered via API", gateway.status_code == 200, f"{gateway.status_code} {gateway.text[:140]}")
    gateway_state = gateway.json()["gateway"].get("approval_state")
    if gateway_state == "enabled" or gateway.json()["gateway"].get("enabled"):
        check("gateway already enabled (review/test/enable skipped)", True)
    elif gateway_state == "disabled":
        response = client.post("/api/v1/sandbox/gateways/deploy/sandbox-info/enable", headers=hdr(admin))
        check("gateway re-enabled after disable", response.status_code == 200, f"{response.status_code} {response.text[:120]}")
    else:
        for action in ("review", "test", "enable"):
            response = client.post(f"/api/v1/sandbox/gateways/deploy/sandbox-info/{action}", headers=hdr(admin))
            check(f"gateway {action}", response.status_code == 200, f"{response.status_code} {response.text[:120]}")

    attached = client.post("/api/v1/sandbox/gateways/deploy/sandbox-info/attach", headers=hdr(admin))
    check("gateway attached", attached.status_code == 200 and attached.json().get("attached") is True, f"{attached.status_code} {attached.text[:140]}")
    discovered = client.get("/api/v1/sandbox/gateways/deploy/sandbox-info/discover", headers=hdr(admin))
    names = [item.get("name") for item in (discovered.json().get("tools") or [])] if discovered.status_code == 200 else []
    check("gateway discover allowlist", names == ["sandbox_runtime_info"], f"{discovered.status_code} {names}")
    invoked = client.post(
        "/api/v1/sandbox/gateways/deploy/sandbox-info/invoke",
        json={"tool_name": "sandbox_runtime_info", "arguments": {"label": "api"}},
        headers=hdr(admin),
    )
    text = ""
    if invoked.status_code == 200:
        result = (invoked.json().get("result") or {})
        content = result.get("content") or [{}]
        text = str(content[0].get("text") or "")
    check("gateway invoke via API", invoked.status_code == 200 and '"label": "api"' in text, f"{invoked.status_code} {text[:80]}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
