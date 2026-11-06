"""Skill loading functional test.

Checks the Skill catalog, materialized SKILL.md metadata, a real agent turn
that reads the Skill body, and a control turn that must not load it.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

BASE = os.getenv("HDB_API_BASE", "http://127.0.0.1:8002/api/v1").rstrip("/")
USER = os.getenv("HDB_E2E_USERNAME", "")
PASSWORD = os.getenv("HDB_E2E_PASSWORD", "")
KB = "AAA"
SKILL_REF = "skill/key-device-list"
SKILL_NAME = "skill-key-device-list"
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


def run_turn(c: httpx.Client, h: dict, session_id: int, query: str, cap: int = 300) -> dict:
    r = c.post(
        f"{BASE}/conversations/{session_id}/turns",
        json={"query": query, "source_scope": "knowledge_base_only"},
        headers=h,
    )
    r.raise_for_status()
    turn_id = r.json()["turn"]["id"]
    c.post(f"{BASE}/turns/{turn_id}/start", headers=h).raise_for_status()
    deadline = time.monotonic() + cap
    while time.monotonic() < deadline:
        time.sleep(2)
        current = c.get(f"{BASE}/turns/{turn_id}", headers=h).json()
        if current.get("status") in {"completed", "failed", "cancelled"}:
            return current
    return c.get(f"{BASE}/turns/{turn_id}", headers=h).json()


def catalog_checks() -> None:
    from src.agents.skill_materializer import _description
    from src.core.auth import AuthService
    from src.sandbox.skills import SkillCatalog

    catalog = SkillCatalog()
    actor = AuthService().get_user_by_username(USER)
    if actor is None:
        record("测试用户存在", False, f"user={USER}")
        return
    principal_id = actor.id
    department_id = actor.department_id
    enabled = {item.skill_ref for item in catalog.list(enabled_only=True)}
    record(
        "Skill 目录已启用",
        SKILL_REF in enabled,
        f"enabled={sorted(enabled)}",
    )
    key = catalog.require(SKILL_REF)
    record(
        "key-device-list 是文本 Skill",
        key.approval_state == "enabled" and not key.execution_bundle_ref,
        f"state={key.approval_state} bundle={bool(key.execution_bundle_ref)}",
    )
    subscribed = {
        item.skill_ref
        for item in catalog.subscribed_text(
            principal_id=principal_id,
            department_id=department_id,
        )
    }
    record(
        "用户已订阅 key-device-list",
        SKILL_REF in subscribed,
        f"principal={principal_id} department={department_id} "
        f"subscribed={sorted(subscribed)}",
    )
    content = catalog.content(SKILL_REF)
    description = _description(content, SKILL_REF)
    record(
        "Skill 描述解析正确",
        description.startswith("关键器件清单") and not description.startswith("name:"),
        f"description={description[:80]!r}",
    )
    executable = [
        item.skill_ref
        for item in catalog.list(enabled_only=True)
        if item.execution_bundle_ref
    ]
    record(
        "可执行 Skill 已登记（沙箱）",
        bool(executable),
        f"executable={sorted(executable)}",
    )


def skill_turn(c: httpx.Client, h: dict) -> None:
    from src.core.conversation import ConversationService

    session = c.post(
        f"{BASE}/conversations", json={"kb_name": KB, "title": "skill load"}, headers=h
    ).json()
    session_id = int(session["id"])
    turn = run_turn(
        c,
        h,
        session_id,
        "请先调用 read_file 读取 /skill-key-device-list/SKILL.md 的正文，"
        "然后告诉我《关键器件清单》固定 5 列是什么，以及开跑前必须确认哪些输入。",
    )
    answer = turn.get("answer") or ""
    events = ConversationService().list_turn_events(session_id, turn["id"])
    started: dict[str, dict] = {}
    results: dict[str, dict] = {}
    mounted: dict = {}
    for event in events:
        payload = dict(event.payload or {})
        if event.event_type == "stage" and payload.get("key") == "skill_loaded":
            mounted = payload
        elif event.event_type == "tool_started":
            if payload.get("tool_name") == "read_file":
                started[str(payload.get("call_id") or "")] = payload
        elif event.event_type == "tool_result":
            if payload.get("tool_name") == "read_file":
                results[str(payload.get("call_id") or "")] = payload
    skill_reads = {
        call_id: payload
        for call_id, payload in started.items()
        if str(payload.get("query") or "").endswith("/SKILL.md")
    }
    read_skill = bool(skill_reads) and all(
        (results.get(call_id) or {}).get("status") == "done"
        and int((results.get(call_id) or {}).get("result_bytes") or 0) > 0
        for call_id in skill_reads
    )
    skill_mounted = bool(mounted) and SKILL_NAME in list(mounted.get("skills") or [])
    columns = all(
        word in answer for word in ("序号", "器件", "型号", "厂家", "单机用量")
    )
    inputs = "开跑前" in answer or "确认" in answer
    ok = (
        turn.get("status") == "completed"
        and skill_mounted
        and read_skill
        and columns
        and inputs
    )
    read_detail = [
        {
            "path": payload.get("query"),
            "skill_ref": payload.get("skill_ref"),
            "bytes": (results.get(call_id) or {}).get("result_bytes"),
        }
        for call_id, payload in skill_reads.items()
    ]
    record(
        "对话中加载 Skill 正文",
        ok,
        f"status={turn.get('status')} mounted={skill_mounted} "
        f"read_skill={read_skill} reads={read_detail} "
        f"columns={columns} inputs={inputs}",
    )

    files = glob.glob(
        os.path.join("storage", "skill_materialized", "*", SKILL_NAME, "SKILL.md")
    )
    if not files:
        record("物化 SKILL.md 存在", False, "no materialized file")
        return
    newest = max(files, key=os.path.getmtime)
    text = Path(newest).read_text(encoding="utf-8")
    match = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
    description = match.group(1).strip().strip("'\"") if match else ""
    record(
        "物化 description 不是 name:",
        description.startswith("关键器件清单") and not description.startswith("name:"),
        f"path={newest} description={description[:80]!r}",
    )


def control_turn(c: httpx.Client, h: dict) -> None:
    from src.core.conversation import ConversationService

    session = c.post(
        f"{BASE}/conversations", json={"kb_name": KB, "title": "skill control"}, headers=h
    ).json()
    session_id = int(session["id"])
    turn = run_turn(c, h, session_id, "TCN2 网表里 U1800 的型号是什么？")
    answer = turn.get("answer") or ""
    events = ConversationService().list_turn_events(session_id, turn["id"])
    reads = [
        str((event.payload or {}).get("query") or "")
        for event in events
        if event.event_type == "tool_started"
        and (event.payload or {}).get("tool_name") == "read_file"
    ]
    mounted = [
        event
        for event in events
        if event.event_type == "stage"
        and (event.payload or {}).get("key") == "skill_loaded"
    ]
    # Progressive disclosure deliberately mounts Skill metadata on KB turns;
    # the control group is about not eagerly reading the body.
    loaded_body = bool(reads)
    ok = (
        turn.get("status") == "completed"
        and "TPS62872" in answer
        and not loaded_body
    )
    record(
        "控制组：普通 KB 问题不读取 Skill 正文",
        ok,
        f"status={turn.get('status')} mounted={bool(mounted)} "
        f"reads={reads}",
    )


def main() -> int:
    catalog_checks()
    c, h = login()
    skill_turn(c, h)
    control_turn(c, h)
    passed = sum(1 for item in RESULTS if item["ok"])
    print(f"\n===> {passed}/{len(RESULTS)} passed")
    Path(os.getenv("HDB_SKILL_RESULTS", "/tmp/functional_skills.json")).write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
