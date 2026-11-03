#!/usr/bin/env python3
"""Acceptance of a reviewed Skill through the live HDB API and Docker Runner.

Uses one official PyPI wheel with a verified SHA-256 to exercise the optional
dependency path. Test employee accounts are disabled afterwards; the source,
approved bundle and image intentionally remain for administrator inspection.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import secrets
import sys
import time
import zipfile

import httpx

import src.settings as settings


BASE = os.getenv("HDB_SMOKE_BASE", "http://127.0.0.1:8001")


def assert_response(response: httpx.Response, status: int) -> dict:
    if response.status_code != status:
        raise AssertionError(f"{response.request.method} {response.request.url.path}: "
                             f"expected {status}, got {response.status_code}: {response.text[:300]}")
    return response.json()


def token(client: httpx.Client, username: str, password: str) -> dict[str, str]:
    data = assert_response(
        client.post("/api/v1/login", json={"username": username, "password": password}), 200
    )
    return {"Authorization": "Bearer " + data["token"]}


def package(wheel_sha256: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", "# 输出版本报告\n给定 label，生成含 six 版本的文本报告。\n")
        archive.writestr("entrypoint.py", (
            "import json\nimport six\nfrom pathlib import Path\n"
            "params = json.loads(Path('/work/in/parameters.json').read_text())\n"
            "Path('/work/out/report.txt').write_text("
            "f\"{params['label']} | six={six.__version__}\\n\")\n"
        ))
        archive.writestr("requirements.lock", f"six==1.17.0 --hash=sha256:{wheel_sha256}\n")
    return buffer.getvalue()


def main() -> int:
    suffix = secrets.token_hex(4)
    created_users: list[int] = []
    with httpx.Client(base_url=BASE, timeout=720, trust_env=False) as client:
        admin = token(
            client, os.getenv("SKILL_SMOKE_ADMIN_USERNAME") or settings.AUTH_DEFAULT_ADMIN_USERNAME,
            os.getenv("SKILL_SMOKE_ADMIN_PASSWORD") or settings.AUTH_DEFAULT_ADMIN_PASSWORD,
        )
        try:
            departments = assert_response(client.get("/api/v1/departments", headers=admin), 200)
            non_system = [row for row in departments if row["name"] != "system"]
            if len(non_system) < 2:
                for _ in range(2 - len(non_system)):
                    non_system.append(assert_response(client.post("/api/v1/departments", headers=admin,
                        json={"name": f"skill-acceptance-{suffix}-{len(non_system)}"}), 200))
            primary, other = (int(item["id"]) for item in non_system[:2])

            employees = []
            for department in (primary, other):
                username, password = f"skill_acceptance_{suffix}_{department}", secrets.token_urlsafe(18)
                user = assert_response(client.post("/api/v1/users", headers=admin, json={
                    "username": username, "password": password, "role": "employee",
                    "department_id": department,
                }), 200)
                created_users.append(int(user["id"]))
                employees.append((user, token(client, username, password)))
            (employee, employee_auth), (_, outsider_auth) = employees

            assert_response(client.put("/api/v1/sandbox/skill-dependencies", headers=admin,
                                       json={"index_url": "https://pypi.org/simple/"}), 200)
            metadata = assert_response(httpx.get("https://pypi.org/pypi/six/1.17.0/json",
                                                  timeout=15, trust_env=False), 200)
            wheel = next(item for item in metadata["urls"] if item["filename"].endswith(".whl"))
            raw = package(wheel["digests"]["sha256"])
            skill_ref = f"skill/acceptance-{suffix}"
            skill_path = f"/api/v1/sandbox/skills/{skill_ref}"
            assert_response(client.post("/api/v1/sandbox/skills/packages", headers=employee_auth,
                                        json={}), 403)
            assert_response(client.get(skill_path + "/source", headers=employee_auth), 403)
            assert_response(client.put("/api/v1/sandbox/skill-dependencies", headers=employee_auth,
                                       json={"index_url": "https://pypi.org/simple/"}), 403)
            uploaded = assert_response(client.post("/api/v1/sandbox/skills/packages", headers=admin, json={
                "skill_ref": skill_ref, "version": "v1", "department_id": str(primary),
                "package_base64": base64.b64encode(raw).decode(),
                "input_schema": {"type": "object", "properties": {
                    "label": {"type": "string", "maxLength": 80}}, "required": ["label"],
                    "additionalProperties": False},
            }), 200)["skill"]
            assert uploaded["package_build_state"] == "built"
            source = client.get(skill_path + "/source", headers=admin)
            assert source.status_code == 200 and hashlib.sha256(source.content).digest() == hashlib.sha256(raw).digest()
            bundle_ref = uploaded["execution_bundle_ref"]
            bundle = next(row for row in assert_response(
                client.get("/api/v1/sandbox/bundles", headers=admin), 200)["items"]
                if row["bundle_ref"] == bundle_ref)
            assert bundle["metadata"]["image_digest"].startswith("sha256:")
            for kind, reference in (("bundles", bundle_ref), ("skills", skill_ref)):
                for action in ("review", "tested", "enable"):
                    assert_response(client.post(f"/api/v1/sandbox/{kind}/{reference}/{action}", headers=admin), 200)

            available = assert_response(client.get("/api/v1/sandbox/skills/available", headers=employee_auth), 200)
            assert any(item["skill_ref"] == skill_ref for item in available["items"])
            outsider = assert_response(client.get("/api/v1/sandbox/skills/available", headers=outsider_auth), 200)
            assert all(item["skill_ref"] != skill_ref for item in outsider["items"])
            assert_response(client.post(skill_path + "/subscribe", headers=outsider_auth), 403)
            assert_response(client.post(skill_path + "/run", headers=employee_auth,
                                        json={"idempotency_key": suffix, "parameters": {"label": "sample"}}), 403)
            assert_response(client.post(skill_path + "/subscribe", headers=employee_auth), 200)
            task = assert_response(client.post(skill_path + "/run", headers=employee_auth,
                                               json={"idempotency_key": suffix, "parameters": {"label": "sample"}}), 202)["task"]
            task_id = task["task_id"]
            assert client.get(f"/api/v1/sandbox/tasks/{task_id}", headers=outsider_auth).status_code in {403, 404}
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                task = assert_response(client.get(f"/api/v1/sandbox/tasks/{task_id}", headers=employee_auth), 200)["task"]
                if task["status"] in {"succeeded", "failed", "rejected", "cancelled", "reconcile_required"}:
                    break
                time.sleep(0.5)
            assert task["status"] == "succeeded", task
            items = assert_response(client.get(f"/api/v1/sandbox/tasks/{task_id}/artifacts",
                                               headers=employee_auth), 200)["items"]
            assert len(items) == 1 and items[0]["state"] == "committed", items
            artifact_id = items[0]["artifact_id"]
            assert client.get(f"/api/v1/sandbox/artifacts/{artifact_id}", headers=outsider_auth).status_code in {403, 404}
            result = client.get(f"/api/v1/sandbox/artifacts/{artifact_id}", headers=employee_auth)
            assert result.status_code == 200 and result.content == b"sample | six=1.17.0\n", result.text[:100]

            from src.sandbox.agent_tools import make_skill_tools

            class _AgentRuntime:
                def __init__(self):
                    self.events: list[tuple[str, dict]] = []
                    self._seq = 0

                def check_cancel(self):
                    return None

                def emit(self, kind, payload):
                    self.events.append((kind, payload))

                def next_invocation_id(self):
                    self._seq += 1
                    return f"smoke-{self._seq}"

                def tool_started(self, **_kwargs):
                    self.events.append(("tool_started", _kwargs))

                def tool_result(self, **_kwargs):
                    self.events.append(("tool_result", _kwargs))

            rt = _AgentRuntime()
            events = rt.events
            tools = make_skill_tools(rt, user_id=int(employee["id"]), department_id=primary)
            skill_tool = next(tool for tool in tools if tool.name.startswith("skill__"))
            agent_result = json.loads(skill_tool.invoke({"parameters_json": '{"label":"agent"}'}))
            assert agent_result["status"] == "succeeded", agent_result
            assert agent_result["artifacts"][0]["untrusted_preview"] == "agent | six=1.17.0\n"
            assert [kind for kind, _ in events] == ["tool_started", "tool_result"]
            assert_response(client.delete(skill_path + "/subscribe", headers=employee_auth), 200)
            assert all(tool.name != skill_tool.name for tool in make_skill_tools(
                rt, user_id=int(employee["id"]), department_id=primary))
            print(json.dumps({"status": "passed", "skill_ref": skill_ref, "bundle_ref": bundle_ref,
                              "http_task_id": task_id, "agent_task_id": agent_result["task_id"]}))
        finally:
            for user_id in created_users:
                try:
                    client.put(f"/api/v1/users/{user_id}/active", headers=admin,
                               json={"is_active": False}).raise_for_status()
                except Exception:
                    pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AssertionError, httpx.HTTPError) as exc:
        print(f"skill acceptance failed: {exc}", file=sys.stderr)
        sys.exit(1)
