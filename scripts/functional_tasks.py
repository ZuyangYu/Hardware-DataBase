"""Live functional test matrix for the general-agent task layer.

Usage (services must be running):

    HDB_E2E_USERNAME=aaa_admin \
    HDB_E2E_PASSWORD=... \
    uv run python scripts/functional_tasks.py

Cases: write+edit, continue/versioning, plan tool, stop, external definition,
task events, KB-mounted task with citations, and the MCP bridge.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE = os.getenv("HDB_API_BASE", "http://127.0.0.1:8002/api/v1").rstrip("/")
USER = os.getenv("HDB_E2E_USERNAME", "")
PASSWORD = os.getenv("HDB_E2E_PASSWORD", "")
if not USER or not PASSWORD:
    print("set HDB_E2E_USERNAME/HDB_E2E_PASSWORD", file=sys.stderr)
    raise SystemExit(2)
RESULTS: list[dict] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"case": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def client() -> tuple[httpx.Client, dict]:
    c = httpx.Client(timeout=120)
    r = c.post(f"{BASE}/login", json={"username": USER, "password": PASSWORD})
    r.raise_for_status()
    return c, {"Authorization": f"Bearer {r.json()['token']}"}


def create_task(c: httpx.Client, h: dict, query: str, kb: str = "__general__", **extra) -> str:
    body = {"query": query, "kb_name": kb, **extra}
    r = c.post(f"{BASE}/tasks", json=body, headers=h)
    r.raise_for_status()
    return r.json()["task"]["id"]


def wait_task(c: httpx.Client, h: dict, task_id: str, cap: int = 180) -> dict:
    deadline = time.monotonic() + cap
    while time.monotonic() < deadline:
        time.sleep(2)
        task = c.get(f"{BASE}/tasks/{task_id}", headers=h).json()
        turn = task.get("current_turn") or {}
        if turn.get("status") in {"completed", "failed", "cancelled"}:
            return task
    return c.get(f"{BASE}/tasks/{task_id}", headers=h).json()


def artifact_bytes(c: httpx.Client, h: dict, task: dict, name: str) -> tuple[int, str]:
    items = [a for a in task.get("artifacts") or [] if a["name"] == name]
    if not items:
        return 0, ""
    latest = max(items, key=lambda item: item["version"])
    url = latest["download_url"]
    response = c.get("http://127.0.0.1:8002" + url, headers=h)
    return latest["version"], response.text


def case_write_edit(c, h):
    query = (
        "请直接使用工作区工具完成，不要委派子代理："
        "先写 out/func.md 内容为 v1 alpha，再用 workspace_edit 把 alpha 改成 beta，最后确认。"
    )
    task_id = create_task(c, h, query)
    task = wait_task(c, h, task_id)
    version, content = artifact_bytes(c, h, task, "func.md")
    ok = (
        task.get("status") == "completed"
        and version == 1
        and "v1 beta" in content
        and (task.get("verification") or {}).get("status") == "pass"
    )
    record("任务：写入 + 编辑 + 产物 + 验收", ok, f"status={task.get('status')} v={version} content={content[:30]!r}")
    return task_id


def case_continue(c, h, task_id):
    r = c.post(
        f"{BASE}/tasks/{task_id}/messages",
        json={"query": "把 out/func.md 的内容改成 v2 gamma，保留文件名，然后确认。"},
        headers=h,
    )
    r.raise_for_status()
    task = wait_task(c, h, task_id)
    version, content = artifact_bytes(c, h, task, "func.md")
    versions = sorted(a["version"] for a in task.get("artifacts") or [] if a["name"] == "func.md")
    ok = (
        task.get("status") == "completed"
        and version >= 2
        and "v2 gamma" in content
        and versions == [1, 2]
    )
    record("任务：继续修改 + 产物版本", ok, f"status={task.get('status')} versions={versions} content={content[:30]!r}")


def case_plan(c, h):
    query = (
        "请先调用 task_plan_update 把计划设为：收集资料(done)、整理(done)、输出(running)；"
        "然后在工作区 out/plan.txt 写入 done，并确认。"
    )
    task_id = create_task(c, h, query)
    task = wait_task(c, h, task_id)
    plan = task.get("plan") or []
    ok = task.get("status") == "completed" and len(plan) >= 2
    record("任务：计划工具 + 持久化", ok, f"status={task.get('status')} plan={[(p.get('title'), p.get('status')) for p in plan]}")


def case_stop(c, h):
    task_id = create_task(
        c,
        h,
        "把 AAA 知识库里所有文档逐篇总结，输出到 out/all.md，不要遗漏。",
        kb="AAA",
    )
    r = c.post(f"{BASE}/tasks/{task_id}/stop", headers=h)
    r.raise_for_status()
    task = r.json()["task"]
    ok = task.get("status") == "cancelled"
    record("任务：停止", ok, f"status={task.get('status')}")


def case_definition(c, h):
    root = Path("storage/task_definitions")
    root.mkdir(parents=True, exist_ok=True)
    path = root / "functional-check.yaml"
    path.write_text(
        "id: functional-check\ntitle: 功能测试定义\ngoal: 验证外部定义加载\n"
        "deliverable: out/definition.md\ncoverage: [产物存在]\n",
        encoding="utf-8",
    )
    try:
        defs = c.get(f"{BASE}/task-definitions", headers=h).json().get("items") or []
        found = any(item.get("id") == "functional-check" for item in defs)
        task_id = create_task(
            c, h, "在 out/definition.md 写入 defined。", definition_id="functional-check"
        )
        task = wait_task(c, h, task_id)
        ok = found and task.get("definition_id") == "functional-check"
        record("任务：外部定义加载 + 绑定", ok, f"found={found} task_def={task.get('definition_id')} status={task.get('status')}")
    finally:
        path.unlink(missing_ok=True)


def case_kb(c, h):
    task_id = create_task(
        c,
        h,
        "TCN2 网表里 U1800 的型号是什么？给出引用编号。",
        kb="AAA",
    )
    task = wait_task(c, h, task_id, cap=240)
    answer = (task.get("current_turn") or {}).get("answer") or ""
    checks = (task.get("verification") or {}).get("checks") or []
    citation_check = next((x for x in checks if x.get("id") == "citations"), {})
    ok = (
        task.get("status") == "completed"
        and "TPS62872" in answer
        and citation_check.get("status") == "pass"
    )
    record("任务：挂载知识库 + 引用验收", ok, f"status={task.get('status')} citations={citation_check.get('status')} answer={answer[:50]!r}")


def case_events(c, h):
    task_id = create_task(c, h, "在工作区 out/events.txt 写入 events，然后确认。")
    task = wait_task(c, h, task_id)
    events = c.get(f"{BASE}/tasks/{task_id}/events", headers=h).json().get("items") or []
    keys = [e.get("payload", {}).get("key") for e in events if e.get("type") == "stage"]
    ok = task.get("status") == "completed" and "task_context" in keys
    record("任务：执行记录（task_context）", ok, f"status={task.get('status')} stages={keys}")


async def case_mcp():
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = {
            **os.environ,
            "HDB_API_BASE": BASE,
            "HDB_API_TOKEN": os.environ.get("HDB_API_TOKEN", ""),
            "HDB_E2E_USERNAME": USER,
            "HDB_E2E_PASSWORD": PASSWORD,
        }
        # Reuse username/password if no token is provided.
        if not env.get("HDB_API_TOKEN"):
            env["HDB_USERNAME"] = USER
            env["HDB_PASSWORD"] = PASSWORD
        params = StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "src.mcp_bridge.server"],
            env=env,
            cwd=os.getcwd(),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                result = await session.call_tool("list_kbs", {})
                created = await session.call_tool(
                    "create_task",
                    {"query": "在工作区 out/mcp.md 写入 mcp，然后确认。", "kb_name": "__general__"},
                )
                created_data = json.loads(created.content[0].text)
                task_id = created_data["task"]["id"]
                status = ""
                for _ in range(40):
                    got = await session.call_tool("get_task", {"task_id": task_id})
                    info = json.loads(got.content[0].text)
                    status = str((info.get("current_turn") or {}).get("status") or "")
                    if status in {"completed", "failed", "cancelled"}:
                        break
                    await asyncio.sleep(2)
                arts = await session.call_tool("list_task_artifacts", {"task_id": task_id})
                arts_data = json.loads(arts.content[0].text)
                ask = await session.call_tool(
                    "ask_knowledge_base",
                    {"question": "TCN2 网表里 U1800 的型号是什么？", "kb_name": "AAA"},
                )
                ask_data = json.loads(ask.content[0].text)
                ok = (
                    "create_task" in names
                    and "ask_knowledge_base" in names
                    and not result.isError
                    and status == "completed"
                    and bool(arts_data.get("items"))
                    and "TPS62872" in (ask_data.get("answer") or "")
                )
                record(
                    "MCP：list_kbs/create/get/artifacts/ask",
                    ok,
                    f"tools={len(names)} task={status} artifacts={len(arts_data.get('items') or [])}",
                )
    except Exception as exc:  # pragma: no cover - live diagnostic
        record("MCP：工具列表 + list_kbs", False, f"{type(exc).__name__}: {str(exc)[:120]}")



def case_api_negative(c, h):
    checks = []
    # Missing auth
    r = c.get(f"{BASE}/tasks")
    checks.append(("auth", r.status_code in {401, 403}))
    # Unknown task
    r = c.get(f"{BASE}/tasks/does-not-exist", headers=h)
    checks.append(("not_found", r.status_code == 404))
    # Unknown definition
    r = c.post(f"{BASE}/tasks", json={"query": "x", "definition_id": "no-such"}, headers=h)
    checks.append(("definition", r.status_code == 400))
    # Empty query
    r = c.post(f"{BASE}/tasks", json={"query": "   "}, headers=h)
    checks.append(("empty", r.status_code == 422))
    # Invalid session
    r = c.post(f"{BASE}/tasks", json={"query": "x", "session_id": 999999}, headers=h)
    checks.append(("session", r.status_code == 404))
    ok = all(value for _, value in checks)
    record("API：鉴权/404/400/422/会话校验", ok, str({name: value for name, value in checks}))


def case_ownership(c, h):
    from src.core.task_store import get_task_store

    store = get_task_store()
    foreign = store.create_task(
        user_id=1,
        session_id=1,
        kb_name="__general__",
        title="foreign-task",
        definition_id="",
        query="foreign",
    )
    r = c.get(f"{BASE}/tasks/{foreign.id}", headers=h)
    ok = r.status_code == 404
    record("API：任务归属隔离（他人任务 404）", ok, f"status={r.status_code}")


def case_session_binding(c, h):
    first = c.post(f"{BASE}/tasks", json={"query": "会话绑定一", "kb_name": "__general__"}, headers=h).json()
    sid = first["task"]["session_id"]
    second = c.post(
        f"{BASE}/tasks",
        json={"query": "会话绑定二", "kb_name": "__general__", "session_id": sid},
        headers=h,
    )
    ok = second.status_code == 201 and second.json()["task"]["session_id"] == sid
    record("API：复用已有会话", ok, f"session={sid} status={second.status_code}")
    # Avoid leaving two expensive turns running: stop both tasks.
    for task_id in (first["task"]["id"], second.json()["task"]["id"]):
        c.post(f"{BASE}/tasks/{task_id}/stop", headers=h)


def case_list_limit(c, h):
    r = c.get(f"{BASE}/tasks?limit=2", headers=h)
    items = r.json().get("items") or []
    ok = r.status_code == 200 and len(items) <= 2
    record("API：任务列表 limit", ok, f"count={len(items)}")


def case_stop_completed(c, h, task_id):
    r = c.post(f"{BASE}/tasks/{task_id}/stop", headers=h)
    ok = r.status_code == 200 and r.json()["task"]["status"] == "completed"
    record("API：停止已完成任务为 no-op", ok, f"status={r.json().get('task',{}).get('status')}")


def case_isolation(c, h):
    a = create_task(c, h, "在工作区 out/same.md 写入 AAA 内容，然后确认。")
    b = create_task(c, h, "在工作区 out/same.md 写入 BBB 内容，然后确认。")
    ta = wait_task(c, h, a)
    tb = wait_task(c, h, b)
    va, ca = artifact_bytes(c, h, ta, "same.md")
    vb, cb = artifact_bytes(c, h, tb, "same.md")
    ok = (
        ta.get("status") == "completed"
        and tb.get("status") == "completed"
        and "AAA" in ca
        and "BBB" in cb
        and va == 1
        and vb == 1
    )
    record("任务：多任务工作区隔离", ok, f"a={ca[:20]!r} b={cb[:20]!r}")


def case_kb_artifact(c, h):
    task_id = create_task(
        c,
        h,
        "查询 TCN2 网表里 U1800 的型号和封装，把结果写入 out/u1800.md，"
        "内容必须包含型号、封装和引用编号。",
        kb="AAA",
    )
    task = wait_task(c, h, task_id, cap=240)
    version, content = artifact_bytes(c, h, task, "u1800.md")
    citations = next(
        (x for x in (task.get("verification") or {}).get("checks") or [] if x.get("id") == "citations"),
        {},
    )
    ok = (
        task.get("status") == "completed"
        and version == 1
        and "TPS62872" in content
        and "[1]" in content
        and citations.get("status") == "pass"
    )
    record("任务：KB 检索 + 写入产物 + 引用", ok, f"status={task.get('status')} content={content[:60]!r}")


def case_stop_running(c, h):
    task_id = create_task(
        c,
        h,
        "逐篇总结 AAA 知识库中的所有文档，每篇至少 20 个要点，输出到 out/all.md，不要遗漏。",
        kb="AAA",
    )
    # Let the worker claim it before stopping.
    for _ in range(10):
        time.sleep(1)
        current = c.get(f"{BASE}/tasks/{task_id}", headers=h).json()
        if (current.get("current_turn") or {}).get("status") in {"streaming", "cancelling", "running"}:
            break
    r = c.post(f"{BASE}/tasks/{task_id}/stop", headers=h)
    task = r.json().get("task") or {}
    ok = r.status_code == 200 and task.get("status") in {"cancelled", "completed"}
    record("任务：运行中停止", ok, f"status={task.get('status')}")


def case_postgres_store():
    import tempfile
    import uuid

    import psycopg

    from src.core.task_store_pg import PostgresTaskStore

    base = "postgresql://hdb:22c8f866eadcd7f287cab6bd1d2120df@127.0.0.1:55433/hdb"
    dbname = "hdb_func_" + uuid.uuid4().hex[:8]
    admin = psycopg.connect(base, autocommit=True)
    admin.execute(f'CREATE DATABASE "{dbname}"')
    dsn = base.rsplit("/", 1)[0] + "/" + dbname
    tmp = tempfile.mkdtemp()
    try:
        store = PostgresTaskStore(dsn, workspace_root=os.path.join(tmp, "ws"))
        task = store.create_task(
            user_id=4, session_id=1, kb_name="AAA", title="pg", definition_id="", query="q"
        )
        store.set_plan(task.id, [{"id": "s1", "title": "p", "status": "done"}])
        store.set_verification(task.id, {"status": "pass"})
        out = os.path.join(task.workspace_dir, "out")
        with open(os.path.join(out, "r.md"), "w", encoding="utf-8") as handle:
            handle.write("v1")
        first = store.scan_workspace_artifacts(task.id)
        with open(os.path.join(out, "r.md"), "w", encoding="utf-8") as handle:
            handle.write("v2")
        second = store.scan_workspace_artifacts(task.id)
        store.set_status(task.id, "completed")
        ok = (
            store.get_task(task.id).status == "completed"
            and store.get_task(task.id).plan[0]["status"] == "done"
            and len(first) == 1
            and len(second) == 1
            and store.list_artifacts(task.id)[-1].version == 2
        )
        record("Postgres：任务/计划/验收/产物版本", ok, f"artifacts={len(store.list_artifacts(task.id))}")
    finally:
        admin.execute(f'DROP DATABASE "{dbname}"')
        admin.close()


def main() -> int:
    c, h = client()
    case_api_negative(c, h)
    case_ownership(c, h)
    case_session_binding(c, h)
    case_list_limit(c, h)
    task_id = case_write_edit(c, h)
    case_stop_completed(c, h, task_id)
    case_continue(c, h, task_id)
    case_plan(c, h)
    case_stop(c, h)
    case_definition(c, h)
    case_events(c, h)
    case_kb(c, h)
    case_kb_artifact(c, h)
    case_isolation(c, h)
    case_stop_running(c, h)
    case_postgres_store()
    asyncio.run(case_mcp())
    passed = sum(1 for item in RESULTS if item["ok"])
    print(f"\n===> {passed}/{len(RESULTS)} passed")
    Path(os.getenv("HDB_FUNCTIONAL_RESULTS", "/tmp/functional_tasks.json")).write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
