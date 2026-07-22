"""Output rendering: human-readable by default, structured JSON with --json."""
from __future__ import annotations

import json as _json


def print_json(obj) -> None:
    print(_json.dumps(obj, ensure_ascii=False, indent=2))


def render_user(user: dict, as_json: bool = False) -> None:
    if as_json:
        print_json(user)
        return
    print(f"用户: {user['username']}")
    print(f"角色: {user['role']}")
    dept = user.get("department_name") or user.get("department_id")
    if dept:
        print(f"部门: {dept}")


def render_kbs(kbs: list, as_json: bool = False) -> None:
    if as_json:
        print_json(kbs)
        return
    if not kbs:
        print("无可访问知识库")
        return
    for kb in kbs:
        dept = kb.get("department_name") or "-"
        perm = kb.get("permission") or "-"
        print(f"{kb['name']}\t{dept}\t{perm}")


def render_files(files: list, as_json: bool = False) -> None:
    if as_json:
        print_json(files)
        return
    if not files:
        print("(空)")
        return
    for f in files:
        print(f"{f['name']}\t{f.get('status', '')}\t{f.get('processor_kind', '')}")


def render_upload(ack: dict, as_json: bool = False) -> None:
    if as_json:
        print_json(ack)
        return
    print(f"状态: {ack['status']} (成功 {ack['success_count']}/{ack['total_count']})")
    for m in ack.get("messages", []):
        print(f"  {m}")
