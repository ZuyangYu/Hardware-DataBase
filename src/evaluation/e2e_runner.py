"""End-to-end attachment-editing evaluation (live server + real model).

This harness drives the real HTTP API and a real turn worker, so it is opt-in:
set ``HDB_E2E=1`` and have the API reachable.  Each case creates an isolated
conversation, uploads a generated fixture, runs the conversational turn(s) and
checks the *final file and audit chain* — not merely that a tool was called.

Offline pieces (dataset validation and expectation evaluation) are unit
tested without any server.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_DATASET = Path(__file__).resolve().parents[2] / "evaluation/datasets/attachment_editing_v1.jsonl"

_FIXTURE_KINDS = {"txt", "md", "docx"}
_ALLOWED_EXPECT_KEYS = {
    "turn_status",
    "must_have_tools",
    "must_not_have_tools",
    "expect_blocked",
    "expect_no_revision",
    "blocked_error_code",
    "operation",
    "change_summary_contains",
    "child_texts_contain",
    "child_texts_not_contain",
    "parent_texts_contain",
    "release_state",
    "release_request",
    "release_decision",
    "released_texts_contain",
    "min_revisions",
    "chain_versions",
    "answer_contains_any",
}


class E2EDatasetError(ValueError):
    """Raised when the evaluation dataset is malformed."""


@dataclass
class E2ECase:
    case_id: str
    query: str
    fixture: dict[str, Any]
    expect: dict[str, Any]
    description: str = ""
    followup_queries: list[str] = field(default_factory=list)
    source_scope: str = "attachment_only"

    @staticmethod
    def from_dict(raw: dict[str, Any], *, line_number: int) -> "E2ECase":
        prefix = f"line {line_number}"
        if not isinstance(raw, dict):
            raise E2EDatasetError(f"{prefix}: case must be an object")
        case_id = str(raw.get("id") or "").strip()
        query = str(raw.get("query") or "").strip()
        if not case_id or not query:
            raise E2EDatasetError(f"{prefix}: id and query are required")
        fixture = raw.get("fixture")
        if not isinstance(fixture, dict) or fixture.get("kind") not in _FIXTURE_KINDS:
            raise E2EDatasetError(
                f"{prefix}: fixture.kind must be one of {sorted(_FIXTURE_KINDS)}"
            )
        if fixture.get("kind") == "docx":
            lines = fixture.get("lines")
            if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
                raise E2EDatasetError(f"{prefix}: docx fixture needs string 'lines'")
        else:
            content = fixture.get("content")
            if not isinstance(content, str) or not content.strip():
                raise E2EDatasetError(f"{prefix}: text fixture needs non-empty 'content'")
        expect = raw.get("expect")
        if not isinstance(expect, dict) or not expect:
            raise E2EDatasetError(f"{prefix}: expect must be a non-empty object")
        unknown = set(expect) - _ALLOWED_EXPECT_KEYS
        if unknown:
            raise E2EDatasetError(f"{prefix}: unknown expect keys {sorted(unknown)}")
        followups = raw.get("followup_queries") or []
        if not isinstance(followups, list) or not all(isinstance(x, str) for x in followups):
            raise E2EDatasetError(f"{prefix}: followup_queries must be strings")
        return E2ECase(
            case_id=case_id,
            query=query,
            fixture=fixture,
            expect=expect,
            description=str(raw.get("description") or ""),
            followup_queries=[str(item) for item in followups],
            source_scope=str(raw.get("source_scope") or "attachment_only"),
        )


def load_e2e_dataset(path: str | Path) -> list[E2ECase]:
    cases: list[E2ECase] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise E2EDatasetError(f"line {line_number}: invalid JSON: {exc}") from exc
            case = E2ECase.from_dict(raw, line_number=line_number)
            if case.case_id in seen:
                raise E2EDatasetError(f"line {line_number}: duplicate id {case.case_id}")
            seen.add(case.case_id)
            cases.append(case)
    if not cases:
        raise E2EDatasetError("dataset is empty")
    return cases


def evaluate_case_expectations(
    expect: dict[str, Any],
    observation: dict[str, Any],
) -> list[str]:
    """Return human-readable failures; empty means the case passed.

    ``observation`` is assembled by the live driver (and by unit tests) with:
    ``turn_status``, ``tool_names``, ``events``, ``revision`` (last revision
    dict or None), ``child_texts``, ``parent_texts``, ``released_texts``,
    ``release_state``, ``revision_count``.
    """

    failures: list[str] = []

    def _contains(haystack: Any, needle: str, label: str) -> None:
        if needle not in str(haystack or ""):
            failures.append(f"{label}: missing {needle!r}")

    if expect.get("turn_status"):
        if observation.get("turn_status") != expect["turn_status"]:
            failures.append(
                f"turn_status={observation.get('turn_status')!r} expected {expect['turn_status']!r}"
            )

    tool_names = set(observation.get("tool_names") or [])
    has_revision = bool(observation.get("revision"))
    for tool in expect.get("must_have_tools") or []:
        if tool not in tool_names:
            failures.append(f"tool {tool!r} was not called")
    for tool in expect.get("must_not_have_tools") or []:
        if tool in tool_names:
            failures.append(f"tool {tool!r} must not be called")

    blocked_events = [
        event for event in (observation.get("events") or [])
        if event.get("event") == "stage"
        and (event.get("payload") or {}).get("status") == "blocked"
    ]
    if expect.get("expect_no_revision") and has_revision:
        failures.append("expected no candidate revision but one was created")
    if expect.get("expect_blocked"):
        if has_revision:
            failures.append("expected a blocked edit but a candidate was created")
        if not blocked_events:
            failures.append("expected a persisted blocked stage event")
        expected_code = str(expect.get("blocked_error_code") or "")
        if expected_code and not any(
            str((event.get("payload") or {}).get("error_code") or "") == expected_code
            for event in blocked_events
        ):
            failures.append(f"blocked error_code {expected_code!r} not observed")

    revision = observation.get("revision") or {}
    if expect.get("operation") and revision.get("operation") != expect["operation"]:
        failures.append(
            f"operation={revision.get('operation')!r} expected {expect['operation']!r}"
        )
    summary = str(revision.get("change_summary") or "")
    for needle in expect.get("change_summary_contains") or []:
        _contains(summary, needle, "change_summary")

    child_text = "\n".join(observation.get("child_texts") or [])
    for needle in expect.get("child_texts_contain") or []:
        _contains(child_text, needle, "child_texts")
    for needle in expect.get("child_texts_not_contain") or []:
        if needle in child_text:
            failures.append(f"child_texts: unexpectedly contains {needle!r}")
    parent_text = "\n".join(observation.get("parent_texts") or [])
    for needle in expect.get("parent_texts_contain") or []:
        _contains(parent_text, needle, "parent_texts")

    if expect.get("release_state"):
        actual = str(observation.get("release_state") or "")
        if actual != expect["release_state"]:
            failures.append(f"release_state={actual!r} expected {expect['release_state']!r}")
    if expect.get("min_revisions"):
        if int(observation.get("revision_count") or 0) < int(expect["min_revisions"]):
            failures.append(
                f"revision_count={observation.get('revision_count')} "
                f"expected >= {expect['min_revisions']}"
            )
    answer_needles = [
        str(item) for item in (expect.get("answer_contains_any") or []) if str(item)
    ]
    if answer_needles:
        answer = str(observation.get("turn_answer") or "")
        if not any(needle in answer for needle in answer_needles):
            failures.append(
                "turn answer does not explain the block (expected any of "
                f"{answer_needles})"
            )
    if expect.get("chain_versions") and int(observation.get("revision_count") or 0) > 1:
        if not observation.get("version_chain_bound", False):
            failures.append(
                "version chain is not bound: the final revision was not based on the previous candidate"
            )
    released_text = "\n".join(observation.get("released_texts") or [])
    for needle in expect.get("released_texts_contain") or []:
        _contains(released_text, needle, "released_texts")
    return failures


# ---------------------------------------------------------------------------
# Live driver
# ---------------------------------------------------------------------------


def _docx_bytes(lines: list[str]) -> bytes:
    from docx import Document

    document = Document()
    for line in lines:
        stripped = line.rstrip()
        if stripped.startswith("#### "):
            document.add_heading(stripped[5:].strip(), level=4)
        elif stripped.startswith("### "):
            document.add_heading(stripped[4:].strip(), level=3)
        elif stripped.startswith("## "):
            document.add_heading(stripped[3:].strip(), level=2)
        elif stripped.startswith("# "):
            document.add_heading(stripped[2:].strip(), level=1)
        else:
            document.add_paragraph(stripped)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def fixture_bytes(fixture: dict[str, Any], case_id: str) -> tuple[str, bytes, str]:
    kind = fixture["kind"]
    if kind == "docx":
        return f"{case_id}.docx", _docx_bytes(list(fixture["lines"])), (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    content = str(fixture["content"])
    extension = ".md" if kind == "md" else ".txt"
    media = "text/markdown" if kind == "md" else "text/plain"
    return f"{case_id}{extension}", content.encode("utf-8"), media


def _texts_from_bytes(filename: str, content: bytes) -> list[str]:
    if filename.lower().endswith(".docx"):
        from docx import Document

        return [paragraph.text for paragraph in Document(io.BytesIO(content)).paragraphs]
    return content.decode("utf-8", errors="replace").splitlines()


class E2EClient:
    """Thin live-API client used by the orchestrator."""

    def __init__(self, base_url: str, *, timeout: float = 30.0):
        import requests

        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.requests = requests

    def login(self, username: str, password: str) -> str:
        response = self.requests.post(
            f"{self.base_url}/api/v1/login",
            json={"username": username, "password": password},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()["token"]

    def headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def create_session(self, token: str, kb_name: str, title: str) -> int:
        response = self.requests.post(
            f"{self.base_url}/api/v1/conversations",
            headers=self.headers(token),
            json={"kb_name": kb_name, "title": title},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return int(response.json()["id"])

    def upload(
        self, token: str, session_id: int, filename: str, content: bytes, media: str
    ) -> str:
        response = self.requests.post(
            f"{self.base_url}/api/v1/conversations/{session_id}/attachments",
            headers=self.headers(token),
            files={"file": (filename, content, media)},
            data={"client_request_id": f"e2e-upload-{uuid.uuid4().hex[:12]}"},
            timeout=60,
        )
        response.raise_for_status()
        return str(response.json()["attachment_id"])

    def active_attachment_ids(self, token: str, session_id: int) -> list[str]:
        """Every active session attachment, including earlier candidates."""

        response = self.requests.get(
            f"{self.base_url}/api/v1/conversations/{session_id}/attachments",
            headers=self.headers(token),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return [
            str(row["attachment_id"])
            for row in response.json()
            if str(row.get("status") or "active") == "active"
            and str(row.get("parse_status") or "") != "failed"
        ]

    def wait_parsed(self, token: str, session_id: int, attachment_id: str, *, timeout: float = 60.0) -> str:
        deadline = time.time() + timeout
        status = "queued"
        while time.time() < deadline:
            response = self.requests.get(
                f"{self.base_url}/api/v1/conversations/{session_id}/attachments",
                headers=self.headers(token),
                timeout=self.timeout,
            )
            response.raise_for_status()
            record = next(
                (row for row in response.json() if row["attachment_id"] == attachment_id),
                None,
            )
            status = str((record or {}).get("parse_status") or "")
            if status in {"ready", "degraded", "failed"}:
                return status
            time.sleep(1)
        return status

    def run_turn(
        self,
        token: str,
        session_id: int,
        query: str,
        attachment_ids: list[str],
        *,
        source_scope: str,
        timeout: float = 900.0,
    ) -> dict[str, Any]:
        response = self.requests.post(
            f"{self.base_url}/api/v1/conversations/{session_id}/turns",
            headers=self.headers(token),
            json={
                "query": query,
                "client_request_id": f"e2e-turn-{uuid.uuid4().hex[:12]}",
                "query_mode": "deep",
                "attachment_ids": attachment_ids,
                "source_scope": source_scope,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        turn_id = response.json()["turn"]["id"]
        start = self.requests.post(
            f"{self.base_url}/api/v1/turns/{turn_id}/start",
            headers=self.headers(token),
            timeout=self.timeout,
        )
        if start.status_code not in {200, 202}:
            raise RuntimeError(f"turn start failed: {start.status_code} {start.text[:200]}")
        deadline = time.time() + timeout
        turn: dict[str, Any] = {}
        while time.time() < deadline:
            poll = self.requests.get(
                f"{self.base_url}/api/v1/turns/{turn_id}",
                headers=self.headers(token),
                timeout=self.timeout,
            )
            poll.raise_for_status()
            turn = poll.json()
            if turn.get("status") in {"completed", "failed", "cancelled"}:
                return turn
            time.sleep(5)
        raise TimeoutError(f"turn {turn_id} did not finish in {timeout}s")

    def download(self, token: str, session_id: int, attachment_id: str) -> tuple[str, bytes]:
        response = self.requests.get(
            f"{self.base_url}/api/v1/conversations/{session_id}/attachments/{attachment_id}/download",
            headers=self.headers(token),
            timeout=60,
        )
        response.raise_for_status()
        disposition = str(response.headers.get("content-disposition") or "")
        return disposition, response.content

    def request_release(self, token: str, session_id: int, revision_id: str, comment: str) -> dict:
        response = self.requests.post(
            f"{self.base_url}/api/v1/conversations/{session_id}/attachments/revisions/{revision_id}/release-request",
            headers=self.headers(token),
            json={"comment": comment},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def review(
        self, token: str, session_id: int, revision_id: str, decision: str, comment: str
    ) -> dict:
        response = self.requests.post(
            f"{self.base_url}/api/v1/conversations/{session_id}/attachments/revisions/{revision_id}/release-review",
            headers=self.headers(token),
            json={"decision": decision, "comment": comment},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()


def _turn_events(db_path: str, turn_id: str) -> list[dict[str, Any]]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT seq, event_type, payload_json FROM chat_turn_events "
            "WHERE turn_id = ? ORDER BY seq",
            (turn_id,),
        ).fetchall()
    finally:
        connection.close()
    events = []
    for seq, event_type, payload in rows:
        try:
            parsed = json.loads(payload or "{}")
        except ValueError:
            parsed = {}
        events.append({"seq": seq, "event": event_type, "payload": parsed})
    return events


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    failures: list[str]
    observation: dict[str, Any] = field(default_factory=dict)
    session_id: int | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "failures": self.failures,
            "error": self.error,
            "session_id": self.session_id,
            "turn_status": self.observation.get("turn_status"),
            "revision_id": (self.observation.get("revision") or {}).get("revision_id"),
        }


def _passes_gate(passed: int, total: int, min_pass_rate: float) -> bool:
    if total <= 0:
        return False
    return (passed / total) >= float(min_pass_rate)


def _resolve_auth_db() -> str:
    override = os.getenv("HDB_E2E_AUTH_DB", "").strip()
    if override:
        return override
    from src import settings

    path = settings.AUTH_DB_PATH
    if os.path.isabs(path):
        return path
    return os.path.join(settings.BASE_DIR, path)


def _tool_names_from_events(events: list[dict[str, Any]]) -> set[str]:
    names = {
        str((event.get("payload") or {}).get("tool_name") or "")
        for event in events
        if event.get("event") in {"tool_started", "tool_result"}
    }
    if any(event.get("event") == "attachment_revision" for event in events):
        names.add("attachment_edit")
    if any(event.get("event") == "attachment_release" for event in events):
        names.add("request_attachment_release")
    names.discard("")
    return names


def run_case(
    client: "E2EClient",
    case: E2ECase,
    *,
    owner_token: str,
    reviewer_token: str,
    kb_name: str,
    db_path: str,
) -> CaseResult:
    """Execute one case and evaluate the final file + audit chain."""

    session_id: int | None = None
    try:
        filename, content, media = fixture_bytes(case.fixture, case.case_id)
        session_id = client.create_session(owner_token, kb_name, f"E2E {case.case_id}")
        attachment_id = client.upload(owner_token, session_id, filename, content, media)
        parse_status = client.wait_parsed(owner_token, session_id, attachment_id)
        if parse_status == "failed":
            return CaseResult(case.case_id, False, [f"fixture parse failed ({filename})"], session_id=session_id)

        revisions: list[dict[str, Any]] = []
        all_events: list[dict[str, Any]] = []
        turn: dict[str, Any] = {}
        for query in [case.query, *case.followup_queries]:
            # Mirror the UI: every turn sees the current session attachments,
            # including candidates produced by earlier turns.
            turn_attachments = client.active_attachment_ids(owner_token, session_id)
            turn = client.run_turn(
                owner_token,
                session_id,
                query,
                turn_attachments or [attachment_id],
                source_scope=case.source_scope,
            )
            events = _turn_events(db_path, str(turn.get("id") or ""))
            all_events.extend(events)
            for event in events:
                if event.get("event") != "attachment_revision":
                    continue
                payload = dict(event.get("payload") or {})
                revision_id = str(payload.get("revision_id") or "")
                if not revision_id:
                    continue
                view = client.requests.get(
                    f"{client.base_url}/api/v1/conversations/{session_id}/attachments/"
                    f"revisions/{revision_id}",
                    headers=client.headers(owner_token),
                    timeout=client.timeout,
                )
                revision = dict(view.json()) if view.status_code == 200 else {}
                revision.setdefault("revision_id", revision_id)
                revision.setdefault(
                    "child_attachment_id", payload.get("child_attachment_id")
                )
                revision.setdefault(
                    "parent_attachment_id", payload.get("parent_attachment_id")
                )
                revision.setdefault("operation", payload.get("operation"))
                revision.setdefault("change_summary", payload.get("change_summary"))
                revision.setdefault("release_state", payload.get("release_state"))
                revisions.append(revision)

        revision = revisions[-1] if revisions else {}
        revision_payload = dict(revision)
        chain_bound = True
        for previous, current in zip(revisions, revisions[1:]):
            if str(current.get("parent_attachment_id") or "") != str(
                previous.get("child_attachment_id") or ""
            ):
                chain_bound = False
                break

        child_texts: list[str] = []
        if revision_payload.get("child_attachment_id"):
            _disposition, child_bytes = client.download(
                owner_token, session_id, str(revision_payload["child_attachment_id"])
            )
            child_texts = _texts_from_bytes(filename, child_bytes)
        parent_texts = _texts_from_bytes(filename, content)

        release_state = str(
            revision.get("release_state") or revision_payload.get("release_state") or ""
        )
        released_texts: list[str] = []
        if case.expect.get("release_request") and revision_payload.get("revision_id"):
            requested = client.request_release(
                owner_token,
                session_id,
                str(revision_payload["revision_id"]),
                "e2e 申请",
            )
            release_state = str(requested.get("release_state") or "")
            decision = str(case.expect.get("release_decision") or "")
            if decision:
                reviewed = client.review(
                    reviewer_token,
                    session_id,
                    str(revision_payload["revision_id"]),
                    decision,
                    "e2e 审核",
                )
                release_state = str(reviewed.get("release_state") or "")
                released_id = str(reviewed.get("released_attachment_id") or "")
                if released_id:
                    _disposition, released_bytes = client.download(
                        owner_token, session_id, released_id
                    )
                    released_texts = _texts_from_bytes(filename, released_bytes)

        observation = {
            "turn_status": str(turn.get("status") or ""),
            "turn_answer": str(turn.get("answer") or ""),
            "tool_names": sorted(_tool_names_from_events(all_events)),
            "events": all_events,
            "revision": revision,
            "revisions": revisions,
            "revision_count": len(revisions),
            "version_chain_bound": chain_bound,
            "child_texts": child_texts,
            "parent_texts": parent_texts,
            "released_texts": released_texts,
            "release_state": release_state,
        }
        failures = evaluate_case_expectations(case.expect, observation)
        return CaseResult(
            case.case_id,
            not failures,
            failures,
            observation=observation,
            session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001 - the harness reports, never crashes
        return CaseResult(case.case_id, False, [], session_id=session_id, error=str(exc)[:1000])


def _create_identities(
    client: "E2EClient", *, department_id: int
) -> tuple[str, str, str, int, int]:
    run = uuid.uuid4().hex[:6]
    admin_token = client.login(
        os.getenv("HDB_E2E_ADMIN_USERNAME", "admin"),
        os.getenv("HDB_E2E_ADMIN_PASSWORD", "123456"),
    )
    reviewer_name = f"e2e_admin_{run}"
    owner_name = f"e2e_owner_{run}"
    password = os.getenv("HDB_E2E_PASSWORD", "E2ePass!2026")
    # 两级角色模型：系统管理员创建两个部门员工；员工对部门 KB 隐式 admin，
    # 没有逐用户授权步骤（kb_permissions 已随旧模型删除）。
    created_reviewer = client.requests.post(
        f"{client.base_url}/api/v1/users",
        headers=client.headers(admin_token),
        json={
            "username": reviewer_name,
            "password": password,
            "department_id": department_id,
        },
        timeout=client.timeout,
    )
    created_reviewer.raise_for_status()
    reviewer_id = int(created_reviewer.json()["id"])
    reviewer_token = client.login(reviewer_name, password)
    created_owner = client.requests.post(
        f"{client.base_url}/api/v1/users",
        headers=client.headers(admin_token),
        json={
            "username": owner_name,
            "password": password,
            "department_id": department_id,
        },
        timeout=client.timeout,
    )
    created_owner.raise_for_status()
    owner_id = int(created_owner.json()["id"])
    owner_token = client.login(owner_name, password)
    return admin_token, reviewer_token, owner_token, owner_id, reviewer_id


def _cleanup(
    client: "E2EClient",
    *,
    admin_token: str,
    reviewer_token: str,
    owner_token: str,
    owner_id: int,
    reviewer_id: int,
    session_ids: list[int],
) -> None:
    for session_id in session_ids:
        try:
            client.requests.delete(
                f"{client.base_url}/api/v1/conversations/{session_id}",
                headers=client.headers(owner_token),
                timeout=client.timeout,
            )
        except Exception:  # noqa: BLE001
            pass
    for user_id, token in ((owner_id, reviewer_token), (reviewer_id, admin_token)):
        try:
            client.requests.put(
                f"{client.base_url}/api/v1/users/{user_id}/active",
                headers=client.headers(token),
                json={"is_active": False},
                timeout=client.timeout,
            )
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hardware-database-e2e")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--base-url", default=os.getenv("HDB_E2E_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--kb", default=os.getenv("HDB_E2E_KB", "ADAS"))
    parser.add_argument(
        "--department-id",
        type=int,
        default=int(os.getenv("HDB_E2E_DEPARTMENT_ID", "47")),
    )
    parser.add_argument("--output", default="storage/evaluations/e2e")
    parser.add_argument("--case", action="append", default=[], help="run only these case ids")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=float(os.getenv("HDB_E2E_MIN_PASS_RATE", "0.9") or 0.9),
        help="gate: exit non-zero when the pass rate is below this value",
    )
    parser.add_argument("--keep", action="store_true", help="keep sessions/users for debugging")
    parser.add_argument(
        "--force",
        action="store_true",
        help="run even when HDB_E2E=1 is not set (live model calls!)",
    )
    args = parser.parse_args(argv)

    if os.getenv("HDB_E2E") != "1" and not args.force:
        print(
            "Refusing to run: set HDB_E2E=1 (this harness calls a live server and model). "
            "Use --force to override.",
            file=sys.stderr,
        )
        return 2

    cases = load_e2e_dataset(args.dataset)
    if args.case:
        wanted = set(args.case)
        cases = [case for case in cases if case.case_id in wanted]
    if args.limit > 0:
        cases = cases[: args.limit]
    if not cases:
        print("No cases selected.", file=sys.stderr)
        return 2

    client = E2EClient(args.base_url)
    db_path = _resolve_auth_db()
    admin_token, reviewer_token, owner_token, owner_id, reviewer_id = _create_identities(
        client, department_id=args.department_id
    )

    results: list[CaseResult] = []
    session_ids: list[int] = []
    try:
        for case in cases:
            result = run_case(
                client,
                case,
                owner_token=owner_token,
                reviewer_token=reviewer_token,
                kb_name=args.kb,
                db_path=db_path,
            )
            results.append(result)
            if result.session_id:
                session_ids.append(result.session_id)
            status = "PASS" if result.passed else "FAIL"
            print(f"[{status}] {case.case_id} {case.description}")
            for failure in result.failures:
                print(f"        - {failure}")
            if result.error:
                print(f"        ! {result.error}")
    finally:
        if not args.keep:
            _cleanup(
                client,
                admin_token=admin_token,
                reviewer_token=reviewer_token,
                owner_token=owner_token,
                owner_id=owner_id,
                reviewer_id=reviewer_id,
                session_ids=session_ids,
            )

    passed = sum(1 for item in results if item.passed)
    gate_ok = _passes_gate(passed, len(results), args.min_pass_rate)
    summary = {
        "dataset": args.dataset,
        "base_url": args.base_url,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": round(passed / len(results), 4) if results else 0.0,
        "min_pass_rate": float(args.min_pass_rate),
        "gate_passed": gate_ok,
        "cases": [result.to_dict() for result in results],
    }
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    summary_path = output_dir / f"{run_id}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"\nE2E: {passed}/{len(results)} passed "
        f"(rate={summary['pass_rate']:.2%}, gate≥{args.min_pass_rate:.0%}) · "
        f"summary: {summary_path}"
    )
    return 0 if (passed == len(results) and gate_ok) else 1


__all__ = [
    "CaseResult",
    "E2ECase",
    "E2EClient",
    "E2EDatasetError",
    "evaluate_case_expectations",
    "fixture_bytes",
    "load_e2e_dataset",
    "main",
    "run_case",
]


if __name__ == "__main__":
    sys.exit(main())
