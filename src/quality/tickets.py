"""Lightweight support workflow; consent applies only to this submitted Q&A."""
from __future__ import annotations

import json
import uuid
from contextlib import closing
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.auth import ROLE_EMPLOYEE, ROLE_SYSTEM_ADMIN
from src.quality.store import QualityConflict, dumps, now_iso, sha256

TicketStatus = Literal["open", "in_progress", "waiting", "resolved", "closed"]


class TicketUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_version: int = Field(ge=1)
    client_request_id: str = Field(min_length=1, max_length=128)
    action: Literal["reply", "claim", "assign", "wait", "resolve", "reopen", "close"]
    message: str = Field(default="", max_length=4000)
    assignee_user_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def valid_action(self):
        self.message = self.message.strip()
        if self.action in {"reply", "wait", "resolve"} and not self.message:
            raise ValueError("请填写补充说明或解决办法")
        if (self.action == "assign") != (self.assignee_user_id is not None):
            raise ValueError("转派时必须选择处理管理员")
        return self


def _event(conn, ticket_id, viewer, key, digest, kind, message, status):
    conn.execute("""INSERT INTO quality_ticket_events
        (event_id,ticket_id,actor_user_id,actor_username,actor_role,client_request_id,
         request_sha256,kind,message,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        ("ticket-event-" + uuid.uuid4().hex, ticket_id, viewer.id, viewer.username,
         viewer.role, key, digest, kind, message, status, now_iso()))


def active_ticket(conn, snapshot_id):
    return conn.execute("SELECT ticket_id,status,contact,consent_active FROM quality_tickets WHERE snapshot_id=? AND deleted_at IS NULL", (snapshot_id,)).fetchone()


def clear_ticket_material(conn, snapshot_id):
    conn.execute("UPDATE quality_tickets SET comment='',contact='',resolution='' WHERE snapshot_id=?", (snapshot_id,))
    conn.execute("UPDATE quality_ticket_events SET message='' WHERE ticket_id IN (SELECT ticket_id FROM quality_tickets WHERE snapshot_id=?)", (snapshot_id,))


def record_feedback_ticket(store, conn, snapshot, case, viewer, request, event_id):
    """Called inside the feedback transaction after locking the original snapshot."""
    created = False
    row = store.lock(conn, "SELECT * FROM quality_tickets WHERE snapshot_id=? AND deleted_at IS NULL", (snapshot["snapshot_id"],))
    if request.rating == "withdrawn" and row:
        clear_ticket_material(conn, snapshot["snapshot_id"])
        conn.execute("UPDATE quality_tickets SET consent_active=0,status='closed',row_version=row_version+1,updated_at=? WHERE ticket_id=?", (now_iso(), row["ticket_id"]))
        _event(conn, row["ticket_id"], viewer, "feedback:" + event_id, sha256(event_id), "withdraw", "", "closed")
    elif request.rating == "unhelpful" and request.share_with_support:
        stamp = now_iso()
        if row:
            if row["status"] == "closed" and row["consent_active"]:
                raise QualityConflict("工单已关闭，请联系管理员重新打开")
            ticket_id = row["ticket_id"]
            status = "open" if row["status"] == "resolved" or not row["consent_active"] else "in_progress" if row["status"] == "waiting" else row["status"]
            conn.execute("""UPDATE quality_tickets SET feedback_event_id=?,reason_codes_json=?,comment=?,contact=?,
                consent_active=1,status=?,resolution='',row_version=row_version+1,updated_at=? WHERE ticket_id=?""",
                (event_id, dumps(request.reason_codes), request.comment, request.support_contact.strip(), status, stamp, ticket_id))
            kind = "supplement"
        else:
            created = True
            ticket_id, status, kind = "ticket-" + uuid.uuid4().hex, "open", "submit"
            conn.execute("""INSERT INTO quality_tickets
                (ticket_id,snapshot_id,case_id,feedback_event_id,owner_user_id,requester_username,
                 department_id,department_name,reason_codes_json,comment,contact,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ticket_id, snapshot["snapshot_id"], case["case_id"], event_id, viewer.id, viewer.username,
                 viewer.department_id, viewer.department_name or "", dumps(request.reason_codes), request.comment,
                 request.support_contact.strip(), stamp, stamp))
        _event(conn, ticket_id, viewer, "feedback:" + event_id, sha256(event_id), kind, request.comment, status)
    active = active_ticket(conn, snapshot["snapshot_id"])
    return (active["ticket_id"] if active else None), created


class TicketService:
    def __init__(self, store, auth=None):
        self.store, self.auth = store, auth

    def _row(self, conn, ticket_id, viewer, *, lock=False):
        if not viewer.is_active or viewer.role not in {ROLE_EMPLOYEE, ROLE_SYSTEM_ADMIN}:
            raise PermissionError("当前账号无权处理工单")
        query = "SELECT * FROM quality_tickets WHERE ticket_id=? AND deleted_at IS NULL"
        row = self.store.lock(conn, query, (ticket_id,)) if lock else conn.execute(query, (ticket_id,)).fetchone()
        if row is None or (viewer.role != ROLE_SYSTEM_ADMIN and viewer.id != row["owner_user_id"]):
            raise KeyError("工单不存在或无权访问")
        return row

    def _view(self, conn, row, *, detail=True):
        snapshot = conn.execute("SELECT * FROM quality_snapshots WHERE snapshot_id=?", (row["snapshot_id"],)).fetchone()
        available = bool(row["consent_active"] and snapshot and self.store.source_valid(conn, snapshot))
        content = json.loads(snapshot["content_json"]) if available else {}
        result = {key: row[key] for key in ("ticket_id", "case_id", "owner_user_id", "requester_username",
                  "department_id", "department_name", "assignee_user_id", "assignee_username", "status", "row_version", "created_at", "updated_at")}
        result.update(reason_codes=json.loads(row["reason_codes_json"]), consent_active=bool(row["consent_active"]),
                      content_available=available, summary=(row["comment"] or content.get("query", ""))[:160] if available else "材料已撤销或失效")
        if detail:
            events = conn.execute("""SELECT event_id,actor_user_id,actor_username,actor_role,kind,message,status,created_at
                FROM quality_ticket_events WHERE ticket_id=? ORDER BY created_at,event_id""", (row["ticket_id"],)).fetchall()
            result.update(content={k: content[k] for k in ("query", "answer") if k in content},
                          comment=row["comment"] if available else "", contact=row["contact"] if available else "",
                          resolution=row["resolution"] if available else "",
                          events=[{**dict(event), "message": event["message"] if available else ""} for event in events])
        return result

    def get(self, ticket_id, viewer):
        with closing(self.store.connect()) as conn:
            return self._view(conn, self._row(conn, ticket_id, viewer))

    def list(self, viewer, *, status=None, department_id=None, search="", limit=50, offset=0):
        if not viewer.is_active or viewer.role not in {ROLE_EMPLOYEE, ROLE_SYSTEM_ADMIN}:
            raise PermissionError("当前账号无权查看工单")
        if status and status not in {"open", "in_progress", "waiting", "resolved", "closed"}:
            raise ValueError("无效的工单状态")
        clauses, params = ["deleted_at IS NULL"], []
        if viewer.role != ROLE_SYSTEM_ADMIN:
            clauses.append("owner_user_id=?")
            params.append(viewer.id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if department_id is not None:
            clauses.append("department_id=?")
            params.append(department_id)
        if search.strip():
            # Search only metadata. Revoked/private content cannot affect search results.
            clauses.append("(requester_username LIKE ? OR department_name LIKE ? OR ticket_id LIKE ?)")
            params.extend(["%" + search.strip() + "%"] * 3)
        where = " AND ".join(clauses)
        with closing(self.store.connect()) as conn:
            total = conn.execute("SELECT COUNT(*) FROM quality_tickets WHERE " + where, tuple(params)).fetchone()[0]
            rows = conn.execute("SELECT * FROM quality_tickets WHERE " + where + " ORDER BY updated_at DESC,ticket_id DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
            return {"items": [self._view(conn, row, detail=False) for row in rows], "total": total}

    def update(self, viewer, ticket_id, request):
        digest = sha256([ticket_id, request.model_dump()])
        with self.store.transaction() as conn:
            self.store.request_lock(conn, "ticket", viewer.id, request.client_request_id)
            row = self._row(conn, ticket_id, viewer, lock=True)
            prior = conn.execute("SELECT request_sha256 FROM quality_ticket_events WHERE actor_user_id=? AND client_request_id=?", (viewer.id, request.client_request_id)).fetchone()
            if prior:
                if prior["request_sha256"] != digest:
                    raise QualityConflict("请求编号已用于其他工单操作")
                return {**self._view(conn, row), "deduplicated": True}
            admin = viewer.role == ROLE_SYSTEM_ADMIN
            if not admin and (request.action not in {"reply", "reopen", "close"} or (request.action == "reopen" and row["status"] == "closed")):
                raise PermissionError("该操作需要处理管理员权限")
            snapshot = conn.execute("SELECT * FROM quality_snapshots WHERE snapshot_id=?", (row["snapshot_id"],)).fetchone()
            if not row["consent_active"] or not self.store.source_valid(conn, snapshot):
                raise QualityConflict("工单材料已撤销或失效")
            if request.row_version != row["row_version"]:
                raise QualityConflict("工单已更新，请刷新后重试")
            status, assignee, username, resolution = row["status"], row["assignee_user_id"], row["assignee_username"], row["resolution"]
            if request.action == "reopen":
                if status not in {"resolved", "closed"}:
                    raise QualityConflict("只有已解决或已关闭工单可以重新打开")
                status, resolution = "open", ""
            elif status == "closed":
                raise QualityConflict("工单已关闭，管理员重新打开后才能处理")
            elif request.action == "close":
                status = "closed"
            elif request.action == "resolve":
                if status == "resolved":
                    raise QualityConflict("工单已解决")
                status, resolution = "resolved", request.message
                if assignee is None:
                    assignee, username = viewer.id, viewer.username
            elif status == "resolved":
                raise QualityConflict("工单已解决，请先重新打开")
            elif request.action in {"claim", "assign"}:
                if request.action == "assign":
                    if self.auth is None:
                        from src.core.auth import AuthService
                        self.auth = AuthService()
                    target = self.auth.get_user_by_id(request.assignee_user_id)
                    if target is None or not target.is_active or target.role != ROLE_SYSTEM_ADMIN:
                        raise ValueError("只能转派给启用的系统管理员")
                    assignee, username = target.id, target.username
                else:
                    assignee, username = viewer.id, viewer.username
                status = "in_progress"
            elif request.action == "wait":
                status = "waiting"
                if assignee is None:
                    assignee, username = viewer.id, viewer.username
            elif request.action == "reply":
                if admin and assignee is None:
                    assignee, username = viewer.id, viewer.username
                if status in {"open", "waiting"}:
                    status = "in_progress" if assignee is not None else "open"
            conn.execute("""UPDATE quality_tickets SET status=?,assignee_user_id=?,assignee_username=?,resolution=?,
                row_version=row_version+1,updated_at=? WHERE ticket_id=?""", (status, assignee, username, resolution, now_iso(), ticket_id))
            event_message = request.message
            if request.action == "assign":
                event_message = f"转派给 {username}" + (f"：{request.message}" if request.message else "")
            _event(conn, ticket_id, viewer, request.client_request_id, digest, request.action, event_message, status)
            return {**self._view(conn, self._row(conn, ticket_id, viewer)), "deduplicated": False}

    def delete(self, viewer, ticket_id, *, row_version):
        if viewer.role != ROLE_SYSTEM_ADMIN or not viewer.is_active:
            raise PermissionError("只有管理员可以删除工单")
        with self.store.transaction() as conn:
            row = self._row(conn, ticket_id, viewer, lock=True)
            if row["row_version"] != row_version:
                raise QualityConflict("工单已更新，请刷新后再删除")
            stamp = now_iso()
            conn.execute("""UPDATE quality_tickets SET deleted_at=?,consent_active=0,comment='',contact='',resolution='',
                row_version=row_version+1,updated_at=? WHERE ticket_id=?""", (stamp, stamp, ticket_id))
            conn.execute("UPDATE quality_ticket_events SET message='' WHERE ticket_id=?", (ticket_id,))
            return {"ok": True}
