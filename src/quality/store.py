"""Quality facts share the conversation database and its transaction boundary.

PostgreSQL DDL is an explicit bootstrap operation. SQLite uses the same DDL;
neither an external annotation nor an LLM call occurs in these transactions.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import src.settings as settings
from src.core.auth import ROLE_EMPLOYEE, ROLE_SYSTEM_ADMIN

SCHEMA_VERSION = 1
DDL = (
    """CREATE TABLE IF NOT EXISTS quality_schema (
        id INTEGER PRIMARY KEY CHECK(id = 1), version INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS quality_snapshots (
        snapshot_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL UNIQUE,
        assistant_message_id INTEGER NOT NULL, answer_revision TEXT NOT NULL,
        owner_user_id INTEGER NOT NULL, department_id INTEGER,
        content_json TEXT NOT NULL, manifest_json TEXT NOT NULL,
        content_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
        FOREIGN KEY(turn_id) REFERENCES chat_turns(id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS quality_cases (
        case_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL UNIQUE,
        trigger_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
        sampling_json TEXT NOT NULL DEFAULT '{}', row_version INTEGER NOT NULL DEFAULT 1,
        share_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY(snapshot_id) REFERENCES quality_snapshots(snapshot_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS quality_feedback_events (
        event_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, actor_user_id INTEGER NOT NULL,
        client_request_id TEXT NOT NULL, request_sha256 TEXT NOT NULL,
        rating TEXT NOT NULL CHECK(rating IN ('helpful', 'unhelpful', 'withdrawn')),
        reason_codes_json TEXT NOT NULL, comment TEXT NOT NULL, sequence INTEGER NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(actor_user_id, client_request_id),
        UNIQUE(case_id, sequence),
        FOREIGN KEY(case_id) REFERENCES quality_cases(case_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS quality_reviews (
        review_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, reviewer_user_id INTEGER NOT NULL,
        client_request_id TEXT NOT NULL, request_sha256 TEXT NOT NULL,
        judgment TEXT NOT NULL, root_cause TEXT NOT NULL, content_json TEXT NOT NULL,
        supersedes TEXT, created_at TEXT NOT NULL, UNIQUE(reviewer_user_id, client_request_id),
        FOREIGN KEY(case_id) REFERENCES quality_cases(case_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS quality_review_requests (
        request_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
        requester_user_id BIGINT NOT NULL, owner_user_id BIGINT NOT NULL,
        department_id BIGINT, client_request_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        decision_note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, decided_at TEXT,
        UNIQUE(requester_user_id, client_request_id),
        FOREIGN KEY(case_id) REFERENCES quality_cases(case_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS quality_outbox (
        job_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, kind TEXT NOT NULL,
        dedup_key TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', owner TEXT NOT NULL DEFAULT '',
        lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
        fencing_token INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
        available_at DOUBLE PRECISION NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY(snapshot_id) REFERENCES quality_snapshots(snapshot_id) ON DELETE CASCADE)""",
    "CREATE INDEX IF NOT EXISTS ix_quality_outbox_pending ON quality_outbox(status, available_at, lease_until)",
    "CREATE INDEX IF NOT EXISTS ix_quality_feedback_case ON quality_feedback_events(case_id, sequence)",
    "CREATE INDEX IF NOT EXISTS ix_quality_review_requests_owner ON quality_review_requests(owner_user_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_quality_review_requests_case ON quality_review_requests(case_id, status, created_at)",
)


class QualityConflict(ValueError):
    pass


class QualityLeaseLost(RuntimeError):
    pass


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def sha256(value: Any) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_schema(conn) -> None:
    for ddl in DDL:
        conn.execute(ddl)
    # Compatibility with review-request tables created before the digest was
    # added. PostgreSQL applies the additive change only during bootstrap.
    if isinstance(conn, sqlite3.Connection):
        columns = {row[1] for row in conn.execute("PRAGMA table_info(quality_review_requests)").fetchall()}
        if "request_sha256" not in columns:
            conn.execute("ALTER TABLE quality_review_requests ADD COLUMN request_sha256 TEXT NOT NULL DEFAULT ''")
    else:
        conn.execute("ALTER TABLE quality_review_requests ADD COLUMN IF NOT EXISTS request_sha256 TEXT NOT NULL DEFAULT ''")
    conn.execute("INSERT INTO quality_schema(id, version) VALUES(1, ?) ON CONFLICT(id) DO NOTHING", (SCHEMA_VERSION,))
    assert_schema(conn)


def assert_schema(conn) -> None:
    try:
        row = conn.execute("SELECT version FROM quality_schema WHERE id = 1").fetchone()
        if row is None or int(row[0]) != SCHEMA_VERSION:
            raise RuntimeError("quality schema version mismatch")
        # Check the columns used at runtime as well as the version marker.
        for query in (
            "SELECT snapshot_id, turn_id, assistant_message_id, answer_revision, owner_user_id, department_id, content_json, manifest_json, content_sha256, created_at FROM quality_snapshots LIMIT 0",
            "SELECT case_id, snapshot_id, trigger_json, status, sampling_json, row_version, share_json, created_at, updated_at FROM quality_cases LIMIT 0",
            "SELECT event_id, case_id, actor_user_id, client_request_id, request_sha256, rating, reason_codes_json, comment, sequence, created_at FROM quality_feedback_events LIMIT 0",
            "SELECT review_id, case_id, reviewer_user_id, client_request_id, request_sha256, judgment, root_cause, content_json, supersedes, created_at FROM quality_reviews LIMIT 0",
            "SELECT request_id, case_id, requester_user_id, owner_user_id, department_id, client_request_id, request_sha256, message, status, decision_note, created_at, updated_at, decided_at FROM quality_review_requests LIMIT 0",
            "SELECT job_id, snapshot_id, kind, dedup_key, payload_json, status, owner, lease_until, fencing_token, attempts, available_at, last_error, created_at, updated_at FROM quality_outbox LIMIT 0",
        ):
            conn.execute(query)
        if not isinstance(conn, sqlite3.Connection):
            text_payloads = {
                "quality_snapshots": {"content_json", "manifest_json"},
                "quality_cases": {"trigger_json", "sampling_json", "share_json"},
                "quality_feedback_events": {"reason_codes_json"},
                "quality_reviews": {"content_json"},
                "quality_review_requests": {"message", "decision_note"},
                "quality_outbox": {"payload_json"},
            }
            for table, columns in text_payloads.items():
                actual = {r["column_name"]: r["data_type"] for r in conn.execute(
                    "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = 'public' AND table_name = ?", (table,),
                ).fetchall()}
                if any(actual.get(column) != "text" for column in columns):
                    raise RuntimeError(f"quality TEXT payload shape mismatch: {table}")
            required_unique = {
                "quality_snapshots": {("snapshot_id",), ("turn_id",)},
                "quality_cases": {("case_id",), ("snapshot_id",)},
                "quality_feedback_events": {("event_id",), ("actor_user_id", "client_request_id"), ("case_id", "sequence")},
                "quality_reviews": {("review_id",), ("reviewer_user_id", "client_request_id")},
                "quality_review_requests": {("request_id",), ("requester_user_id", "client_request_id")},
                "quality_outbox": {("job_id",), ("dedup_key",)},
            }
            for table, keys in required_unique.items():
                indexes = conn.execute("""SELECT array_agg(a.attname ORDER BY key.ordinality) AS columns
                    FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid
                    JOIN pg_namespace n ON n.oid = t.relnamespace
                    CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS key(attnum, ordinality)
                    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = key.attnum
                    WHERE n.nspname = 'public' AND t.relname = ? AND i.indisunique AND i.indisvalid
                    AND i.indpred IS NULL GROUP BY i.indexrelid""", (table,)).fetchall()
                if not keys.issubset({tuple(r["columns"]) for r in indexes}):
                    raise RuntimeError(f"quality uniqueness shape mismatch: {table}")
    except Exception as exc:
        raise RuntimeError("quality schema not ready; run python -m src.storage.bootstrap") from exc


def capture_completed_turn(conn, turn, owner_user_id: int) -> str | None:
    """Called inside the authoritative completion transaction (no commit)."""
    if not bool(getattr(settings, "QUALITY_ENABLED", True)) or turn.status != "completed":
        return None
    identity = (turn.metrics or {}).get("quality_trace_identity") or {}
    evidence_keys = (
        "text", "text_preview", "file_name", "document_id", "chunk_id", "source_type",
        "content_kind", "source_id", "attachment_id", "content_hash", "parser_version",
        "page", "sheet_name", "row_number", "locator", "title", "id", "processor_kind",
    )
    evidence = []
    for raw in (turn.summary or {}).get("evidence", []):
        if not isinstance(raw, dict):
            continue
        # AgentRunner emits content/source_name and nested metadata; log views
        # emit text/file_name. Preserve the actual retrieved bytes in either form.
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        e = {k: raw[k] if k in raw else metadata[k] for k in evidence_keys if k in raw or k in metadata}
        e["text"] = raw.get("content", raw.get("text", raw.get("text_preview", "")))
        e["file_name"] = raw.get("source_name", raw.get("file_name", metadata.get("file_name", "")))
        e["retrieved_content_sha256"] = sha256(e["text"])
        evidence.append(e)
    content = {"query": turn.query, "answer": turn.answer, "evidence": evidence}
    revision = sha256({"turn_id": turn.id, "message_id": turn.assistant_message_id, "content": content})
    snapshot_id = f"snapshot-{sha256([turn.id, revision])}"
    manifest = {
        "schema_version": 1, "task_type": "chat", "task_id": turn.id,
        "session_id": turn.session_id, "kb_name": turn.kb_name, "kb_id": turn.kb_id,
        "query_mode": turn.query_mode, "source_scope": turn.source_scope,
        "execution_attempt_id": turn.attempt_id, "fencing_token": turn.fencing_token,
        "trace_id": identity.get("trace_id", ""), "result_span_id": identity.get("span_id", ""),
        "sampling_status": identity.get("sampling_status", "unknown"),
        "code_revision": getattr(settings, "OBS_SERVICE_VERSION", ""),
        "runtime_manifest": (turn.summary or {}).get("runtime_manifest", {}),
        "source_manifest": (turn.summary or {}).get("source_manifest") or {"evidence": [{k: e[k] for k in ("id", "file_name", "content_hash", "parser_version", "retrieved_content_sha256") if k in e} for e in evidence]},
        "capture_origin": "terminal_transaction",
        "has_attachments": bool(turn.attachments or turn.required_attachment_ids),
        "content_available": bool(getattr(settings, "QUALITY_CAPTURE_CONTENT", True)),
        "diagnostic_flags": [name for name in ("tool_failure", "qc_failed", "version_conflict") if (turn.summary or {}).get(name)],
    }
    if (turn.summary or {}).get("status") in ("partial_failure", "failed"):
        manifest["diagnostic_flags"].append("tool_failure")
    manifest["has_attachments"] = manifest["has_attachments"] or bool(conn.execute("SELECT 1 FROM turn_attachments WHERE turn_id = ? LIMIT 1", (turn.id,)).fetchone())
    if not manifest["content_available"]:
        manifest["runtime_manifest"] = {k: v for k, v in manifest["runtime_manifest"].items()
                                        if k in {"model_alias", "profile", "prompt_sha256", "retrieval_config_sha256", "code_revision"}}
        manifest["source_manifest"] = {"retrieved_content_sha256": [e["retrieved_content_sha256"] for e in evidence]}
    state = conn.execute("SELECT active_attempt_id, fencing_token FROM conversation_execution_state WHERE session_id = ? AND active_turn_id = ?", (turn.session_id, turn.id)).fetchone()
    if state:
        manifest["execution_attempt_id"] = state["active_attempt_id"]
        manifest["fencing_token"] = int(state["fencing_token"])
    conn.execute(
        """INSERT INTO quality_snapshots(snapshot_id, turn_id, assistant_message_id, answer_revision,
           owner_user_id, department_id, content_json, manifest_json, content_sha256, created_at)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(turn_id) DO NOTHING""",
        (snapshot_id, turn.id, turn.assistant_message_id, revision, owner_user_id, turn.department_id,
         dumps(content if manifest["content_available"] else {}), dumps(manifest), sha256(content), now_iso()),
    )
    saved = conn.execute("SELECT snapshot_id, answer_revision FROM quality_snapshots WHERE turn_id = ?", (turn.id,)).fetchone()
    if saved["answer_revision"] != revision:
        raise QualityConflict("completed quality snapshot is immutable")
    enqueue(conn, saved["snapshot_id"], "collect", f"collect:{saved['snapshot_id']}", {})
    return str(saved["snapshot_id"])


def enqueue(conn, snapshot_id: str, kind: str, dedup_key: str, payload: dict) -> None:
    now = now_iso()
    conn.execute(
        """INSERT INTO quality_outbox(job_id, snapshot_id, kind, dedup_key, payload_json, created_at, updated_at)
           VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(dedup_key) DO NOTHING""",
        (f"job-{uuid.uuid4().hex}", snapshot_id, kind, dedup_key, dumps(payload), now, now),
    )


def purge_snapshot_content(conn, snapshot_id: str, reason: str) -> None:
    row = conn.execute("SELECT manifest_json FROM quality_snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
    manifest = json.loads(row[0])
    manifest.update(content_available=False, invalidated_reason=reason)
    # Remove titles/manifests alongside the answer; retain fingerprints/IDs.
    manifest["source_manifest"] = {}
    manifest["runtime_manifest"] = {}
    conn.execute("UPDATE quality_snapshots SET content_json = '{}', manifest_json = ? WHERE snapshot_id = ?", (dumps(manifest), snapshot_id))
    conn.execute("UPDATE quality_cases SET share_json = '{}', row_version = row_version + 1, updated_at = ? WHERE snapshot_id = ?", (now_iso(), snapshot_id))
    conn.execute("UPDATE quality_feedback_events SET comment = '' WHERE case_id IN (SELECT case_id FROM quality_cases WHERE snapshot_id = ?)", (snapshot_id,))
    conn.execute("UPDATE quality_reviews SET content_json = '{}' WHERE case_id IN (SELECT case_id FROM quality_cases WHERE snapshot_id = ?)", (snapshot_id,))
    conn.execute(
        """UPDATE quality_review_requests
           SET status = 'rejected', decision_note = ?, decided_at = ?, updated_at = ?
           WHERE case_id IN (SELECT case_id FROM quality_cases WHERE snapshot_id = ?)
             AND status = 'pending'""",
        ("固定复核材料已失效", now_iso(), now_iso(), snapshot_id),
    )


def redact_message_in_connection(conn, message_id: int) -> None:
    """Remove all quality content derived from a redacted source atomically."""
    rows = conn.execute("""SELECT q.snapshot_id FROM quality_snapshots q JOIN chat_turns t ON t.id = q.turn_id
                         WHERE t.user_message_id = ? OR t.assistant_message_id = ?""", (message_id, message_id)).fetchall()
    for row in rows:
        purge_snapshot_content(conn, row["snapshot_id"], "message_redacted")


class QualityStore:
    def __init__(self, db_path: str | None = None, *, initialize: bool = False):
        self.backend = "sqlite" if db_path else str(settings.CONTROL_PLANE_BACKEND)
        self.dsn = str(settings.CONTROL_PLANE_DATABASE_URL)
        self.db_path = os.path.abspath(db_path or settings.AUTH_DB_PATH)
        if self.backend not in {"sqlite", "postgres"}:
            raise ValueError("unsupported quality backend")
        if self.backend == "sqlite":
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self.transaction() as conn:
            if self.backend == "sqlite" or initialize:
                initialize_schema(conn)
            else:
                assert_schema(conn)

    def connect(self):
        if self.backend == "postgres":
            from src.storage.domain_pg import open_pg_connection

            return open_pg_connection(self.dsn)
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def transaction(self):
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def lock(self, conn, sql, params=()):
        return conn.execute(sql + (" FOR UPDATE" if self.backend == "postgres" else ""), params).fetchone()

    def db_time(self, conn) -> float:
        sql = "SELECT EXTRACT(EPOCH FROM clock_timestamp())" if self.backend == "postgres" else "SELECT (julianday('now') - 2440587.5) * 86400.0"
        return float(conn.execute(sql).fetchone()[0])

    def request_lock(self, conn, kind: str, actor_id: int, key: str) -> None:
        # A request key is actor-global, even when concurrent calls refer to
        # different sessions/cases. Row locks on one target cannot protect it.
        if self.backend == "postgres":
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", (dumps(["quality", kind, actor_id, key]),))

    def target(self, conn, turn_id: str, user_id: int, *, lock: bool = False):
        sql = """SELECT q.* FROM quality_snapshots q JOIN chat_turns t ON t.id = q.turn_id
                 JOIN chat_sessions s ON s.id = t.session_id
                 WHERE q.turn_id = ? AND s.user_id = ? AND t.status = 'completed'"""
        # Source messages redacted later invalidate the stored content at read time.
        row = self.lock(conn, sql, (turn_id, user_id)) if lock else conn.execute(sql, (turn_id, user_id)).fetchone()
        if row is None:
            raise KeyError("feedback target not found")
        return row

    @staticmethod
    def source_valid(conn, snapshot) -> bool:
        if json.loads(snapshot["manifest_json"]).get("invalidated_reason"):
            return False
        row = conn.execute(
            """SELECT COUNT(*) FROM chat_messages m JOIN chat_turns t
               ON m.id = t.user_message_id OR m.id = t.assistant_message_id
               WHERE t.id = ? AND m.redacted = TRUE""", (snapshot["turn_id"],),
        ).fetchone()
        return int(row[0]) == 0

    def ensure_case(self, conn, snapshot_id: str, trigger: str, sampling: dict | None = None):
        stamp = now_iso()
        conn.execute(
            """INSERT INTO quality_cases(case_id, snapshot_id, trigger_json, sampling_json, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(snapshot_id) DO NOTHING""",
            (f"case-{uuid.uuid4().hex}", snapshot_id, dumps([trigger]), dumps(sampling or {}), stamp, stamp),
        )
        row = self.lock(conn, "SELECT * FROM quality_cases WHERE snapshot_id = ?", (snapshot_id,))
        triggers = json.loads(row["trigger_json"])
        if trigger not in triggers:
            conn.execute("UPDATE quality_cases SET trigger_json = ?, sampling_json = ?, row_version = row_version + 1, updated_at = ? WHERE case_id = ?", (dumps([*triggers, trigger]), dumps(sampling or json.loads(row["sampling_json"])), stamp, row["case_id"]))
            row = conn.execute("SELECT * FROM quality_cases WHERE case_id = ?", (row["case_id"],)).fetchone()
        return row

    @staticmethod
    def visible(viewer, snapshot) -> bool:
        return (viewer.role == ROLE_SYSTEM_ADMIN or viewer.id == int(snapshot["owner_user_id"])
                or (viewer.role == ROLE_EMPLOYEE and viewer.department_id is not None
                    and viewer.department_id == snapshot["department_id"]))

    @staticmethod
    def shared(viewer, case) -> bool:
        share = json.loads(case["share_json"])
        return bool(share.get("active") and viewer.id in share.get("reviewer_user_ids", [])
                    and now_iso() < share.get("expires_at", ""))

    def case_rows(self, conn, case_id: str, viewer, *, lock: bool = False):
        sql = "SELECT * FROM quality_cases WHERE case_id = ?"
        case = self.lock(conn, sql, (case_id,)) if lock else conn.execute(sql, (case_id,)).fetchone()
        if case is None:
            raise KeyError("case not found")
        snapshot = conn.execute("SELECT * FROM quality_snapshots WHERE snapshot_id = ?", (case["snapshot_id"],)).fetchone()
        if snapshot is None or not self.visible(viewer, snapshot):
            raise KeyError("case not found")
        return case, snapshot

    def view(self, conn, case, snapshot, viewer) -> dict:
        owner = viewer.id == int(snapshot["owner_user_id"])
        valid = self.source_valid(conn, snapshot)
        shared = self.shared(viewer, case) and viewer.role == ROLE_EMPLOYEE and self.visible(viewer, snapshot)
        share = json.loads(case["share_json"])
        full = json.loads(snapshot["content_json"]) if valid and (owner or shared) else {}
        content = full if owner else {field: full[field] for field in share.get("fields", []) if field in full}
        if shared and not owner:
            content["evidence"] = [full.get("evidence", [])[i] for i in share.get("evidence_indices", []) if 0 <= i < len(full.get("evidence", []))]
        feedback_history_rows = conn.execute("SELECT * FROM quality_feedback_events WHERE case_id = ? ORDER BY sequence", (case["case_id"],)).fetchall()
        feedback = feedback_history_rows[-1] if feedback_history_rows else None
        shared_comment = None
        if not owner and share.get("comment_event_id"):
            shared_comment = conn.execute("SELECT comment FROM quality_feedback_events WHERE event_id = ? AND case_id = ?", (share["comment_event_id"], case["case_id"])).fetchone()
        reviews = conn.execute("SELECT * FROM quality_reviews WHERE case_id = ? ORDER BY created_at, review_id", (case["case_id"],)).fetchall()
        comments_allowed = bool(valid and (owner or (shared and "comment" in share.get("fields", []))))
        review_content_allowed = bool(valid and (owner or (shared and "answer" in content)))
        manifest = json.loads(snapshot["manifest_json"])
        # Runtime/source manifests may include private titles or prompt text.
        safe_manifest = {k: manifest.get(k) for k in (
            "schema_version", "task_type", "task_id", "session_id", "query_mode",
            "execution_attempt_id", "fencing_token", "trace_id", "result_span_id",
            "sampling_status", "code_revision", "capture_origin", "content_available",
        )}
        review_views = []
        for review in reviews:
            review_content = json.loads(review["content_json"])
            review_views.append({
                "review_id": review["review_id"], "judgment": review["judgment"],
                "root_cause": review["root_cause"], "supersedes": review["supersedes"],
                "created_at": review["created_at"],
                # Correctness is a structured governance fact. Keep it outside
                # ``content`` so metadata-only viewers can understand the
                # disposition without receiving notes, gold answers, or rubric.
                "correctness": review_content.get("correctness"),
                "content": review_content if review_content_allowed else {},
            })
        request_rows = []
        if owner or viewer.role == ROLE_SYSTEM_ADMIN:
            request_rows = [{
                "request_id": row["request_id"], "status": row["status"],
                "message": row["message"], "created_at": row["created_at"],
                "updated_at": row["updated_at"], "decided_at": row["decided_at"],
            } for row in conn.execute(
                "SELECT request_id, status, message, created_at, updated_at, decided_at FROM quality_review_requests WHERE case_id = ? ORDER BY created_at DESC",
                (case["case_id"],),
            ).fetchall()]
        return {
            "case_id": case["case_id"], "snapshot_id": snapshot["snapshot_id"],
            "turn_id": snapshot["turn_id"], "assistant_message_id": snapshot["assistant_message_id"],
            "answer_revision": snapshot["answer_revision"], "department_id": snapshot["department_id"],
            "status": case["status"], "row_version": case["row_version"],
            "triggers": json.loads(case["trigger_json"]), "sampling": json.loads(case["sampling_json"]),
            "manifest": safe_manifest, "content": content,
            "content_access": "owner" if valid and owner else "shared" if valid and shared else "hidden",
            "source_valid": valid, "share": share if owner else {"active": shared},
            "feedback": None if feedback is None else {
                "event_id": feedback["event_id"], "rating": feedback["rating"],
                "reason_codes": json.loads(feedback["reason_codes_json"]),
                "comment": (feedback["comment"] if owner else shared_comment[0] if shared_comment else "") if comments_allowed else "", "created_at": feedback["created_at"],
            },
            # Keep append-only feedback history visible as metadata. Comments
            # remain subject to the same owner/explicit-share check as above.
            "feedback_history": [{
                "event_id": event["event_id"], "sequence": event["sequence"],
                "rating": event["rating"], "reason_codes": json.loads(event["reason_codes_json"]),
                "created_at": event["created_at"],
            } for event in feedback_history_rows],
            "reviews": review_views,
            "review_requests": request_rows,
            "created_at": case["created_at"], "updated_at": case["updated_at"],
            "projection_status": [{"status": row["status"], "count": row["count"]} for row in conn.execute("SELECT status, COUNT(*) AS count FROM quality_outbox WHERE snapshot_id = ? AND kind = 'phoenix_annotation' GROUP BY status", (snapshot["snapshot_id"],)).fetchall()],
        }

    def feedback_state(self, user_id: int, turn_id: str) -> dict:
        with closing(self.connect()) as conn:
            snapshot = self.target(conn, turn_id, user_id)
            case = conn.execute("SELECT * FROM quality_cases WHERE snapshot_id = ?", (snapshot["snapshot_id"],)).fetchone()
            event = None if case is None else conn.execute("SELECT * FROM quality_feedback_events WHERE case_id = ? ORDER BY sequence DESC LIMIT 1", (case["case_id"],)).fetchone()
            review_requests = []
            if case is not None and int(snapshot["owner_user_id"]) == user_id:
                review_requests = [{
                    "request_id": row["request_id"], "status": row["status"],
                    "message": row["message"], "created_at": row["created_at"],
                    "updated_at": row["updated_at"], "decided_at": row["decided_at"],
                } for row in conn.execute(
                    "SELECT request_id, status, message, created_at, updated_at, decided_at FROM quality_review_requests WHERE case_id = ? ORDER BY created_at DESC",
                    (case["case_id"],),
                ).fetchall()]
            return {
                "assistant_message_id": snapshot["assistant_message_id"], "answer_revision": snapshot["answer_revision"],
                "case_id": case["case_id"] if case else None, "status": case["status"] if case else None,
                "row_version": case["row_version"] if case else None,
                "content_available": self.source_valid(conn, snapshot) and bool(json.loads(snapshot["content_json"])),
                "evidence_count": len(json.loads(snapshot["content_json"]).get("evidence", [])),
                "rating": event["rating"] if event else None,
                "reason_codes": json.loads(event["reason_codes_json"]) if event else [],
                "comment": event["comment"] if event and self.source_valid(conn, snapshot) else "",
                "share": json.loads(case["share_json"]) if case else {},
                "review_requests": review_requests,
            }

    def submit_feedback(self, viewer, turn_id: str, request) -> dict:
        payload_hash = sha256([turn_id, request.model_dump()])
        with self.transaction() as conn:
            self.request_lock(conn, "feedback", viewer.id, request.client_request_id)
            snapshot = self.target(conn, turn_id, viewer.id, lock=True)
            if not self.source_valid(conn, snapshot):
                raise QualityConflict("source messages have been redacted")
            if (request.assistant_message_id != snapshot["assistant_message_id"]
                    or request.answer_revision != snapshot["answer_revision"]):
                raise QualityConflict("feedback answer revision mismatch")
            case = self.ensure_case(conn, snapshot["snapshot_id"], "feedback")
            prior = conn.execute("SELECT * FROM quality_feedback_events WHERE actor_user_id = ? AND client_request_id = ?", (viewer.id, request.client_request_id)).fetchone()
            if prior:
                if prior["request_sha256"] != payload_hash:
                    raise QualityConflict("client_request_id was already used for different feedback")
                return {"event_id": prior["event_id"], "case_id": case["case_id"], "deduplicated": True}
            event_id = f"feedback-{uuid.uuid4().hex}"
            sequence = int(conn.execute("SELECT COALESCE(MAX(sequence), 0) FROM quality_feedback_events WHERE case_id = ?", (case["case_id"],)).fetchone()[0]) + 1
            conn.execute(
                """INSERT INTO quality_feedback_events(event_id, case_id, actor_user_id, client_request_id,
                   request_sha256, rating, reason_codes_json, comment, sequence, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, case["case_id"], viewer.id, request.client_request_id, payload_hash, request.rating,
                 dumps(request.reason_codes), request.comment, sequence, now_iso()),
            )
            conn.execute("UPDATE quality_cases SET row_version = row_version + 1, updated_at = ? WHERE case_id = ?", (now_iso(), case["case_id"]))
            enqueue(conn, snapshot["snapshot_id"], "phoenix_annotation", f"feedback:{event_id}", {
                "identifier": f"feedback:{event_id}", "name": "user_feedback", "annotator_kind": "HUMAN",
                "label": request.rating, "score": 1 if request.rating == "helpful" else 0 if request.rating == "unhelpful" else None,
                "metadata": {"case_id": case["case_id"], "event_id": event_id, "revision": snapshot["answer_revision"], "reason_codes": request.reason_codes},
            })
            return {"event_id": event_id, "case_id": case["case_id"], "deduplicated": False}

    def create_review_request(self, viewer, case_id: str, request) -> dict:
        if viewer.role != ROLE_SYSTEM_ADMIN:
            raise PermissionError("system administrator role required")
        payload_hash = sha256([case_id, request.model_dump()])
        with self.transaction() as conn:
            self.request_lock(conn, "review_request", viewer.id, request.client_request_id)
            case, snapshot = self.case_rows(conn, case_id, viewer, lock=True)
            prior = conn.execute(
                "SELECT request_id, case_id, status, owner_user_id, request_sha256, message FROM quality_review_requests WHERE requester_user_id = ? AND client_request_id = ?",
                (viewer.id, request.client_request_id),
            ).fetchone()
            if prior:
                if (prior["case_id"] != case_id or prior["message"] != request.message
                        or (prior["request_sha256"] and prior["request_sha256"] != payload_hash)):
                    raise QualityConflict("client_request_id was already used for a different review request")
                return {"request_id": prior["request_id"], "status": prior["status"], "created": False, "owner_user_id": prior["owner_user_id"]}
            if int(case["row_version"]) != request.row_version:
                raise QualityConflict("case changed; refresh before requesting review")
            if not self.source_valid(conn, snapshot) or not json.loads(snapshot["content_json"]):
                raise QualityConflict("fixed review content is unavailable")
            if json.loads(snapshot["manifest_json"]).get("has_attachments"):
                raise PermissionError("attachment review sharing requires a supported source adapter")
            pending = conn.execute(
                "SELECT request_id, owner_user_id FROM quality_review_requests WHERE case_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                (case_id,),
            ).fetchone()
            if pending:
                return {"request_id": pending["request_id"], "status": "pending", "created": False, "owner_user_id": pending["owner_user_id"]}
            stamp = now_iso()
            request_id = f"review-request-{uuid.uuid4().hex}"
            conn.execute(
                """INSERT INTO quality_review_requests(
                    request_id, case_id, requester_user_id, owner_user_id, department_id,
                    client_request_id, request_sha256, message, status, created_at, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (request_id, case_id, viewer.id, snapshot["owner_user_id"], snapshot["department_id"],
                 request.client_request_id, payload_hash, request.message, stamp, stamp),
            )
            conn.execute("UPDATE quality_cases SET row_version = row_version + 1, updated_at = ? WHERE case_id = ?", (stamp, case_id))
            return {"request_id": request_id, "status": "pending", "created": True, "owner_user_id": snapshot["owner_user_id"]}

    def approve_review_request(self, viewer, case_id: str, request_id: str, request) -> dict:
        if viewer.role != ROLE_EMPLOYEE:
            raise PermissionError("employee role required")
        with self.transaction() as conn:
            case, snapshot = self.case_rows(conn, case_id, viewer, lock=True)
            if viewer.id != int(snapshot["owner_user_id"]):
                raise PermissionError("only the original answer owner may approve review sharing")
            pending = self.lock(conn, "SELECT * FROM quality_review_requests WHERE request_id = ? AND case_id = ?", (request_id, case_id))
            if pending is None:
                raise KeyError("review request not found")
            if pending["owner_user_id"] != viewer.id:
                raise PermissionError("only the original answer owner may approve review sharing")
            if pending["status"] != "pending":
                raise QualityConflict("review request is no longer pending")
            if int(case["row_version"]) != request.row_version:
                raise QualityConflict("case changed; refresh before approving review")
            share = self.build_share(conn, snapshot, case_id, viewer, request)
            stamp = now_iso()
            conn.execute("UPDATE quality_cases SET share_json = ?, row_version = row_version + 1, updated_at = ? WHERE case_id = ?", (dumps(share), stamp, case_id))
            conn.execute("UPDATE quality_review_requests SET status = 'approved', decision_note = '', decided_at = ?, updated_at = ? WHERE request_id = ?", (stamp, stamp, request_id))
        return self.get_case(case_id, viewer)

    def reject_review_request(self, viewer, case_id: str, request_id: str, request) -> dict:
        if viewer.role != ROLE_EMPLOYEE:
            raise PermissionError("employee role required")
        with self.transaction() as conn:
            case, snapshot = self.case_rows(conn, case_id, viewer, lock=True)
            if viewer.id != int(snapshot["owner_user_id"]):
                raise PermissionError("only the original answer owner may reject review sharing")
            pending = self.lock(conn, "SELECT * FROM quality_review_requests WHERE request_id = ? AND case_id = ?", (request_id, case_id))
            if pending is None:
                raise KeyError("review request not found")
            if pending["owner_user_id"] != viewer.id:
                raise PermissionError("only the original answer owner may reject review sharing")
            if pending["status"] != "pending":
                raise QualityConflict("review request is no longer pending")
            if int(case["row_version"]) != request.row_version:
                raise QualityConflict("case changed; refresh before rejecting review")
            stamp = now_iso()
            conn.execute("UPDATE quality_review_requests SET status = 'rejected', decision_note = ?, decided_at = ?, updated_at = ? WHERE request_id = ?", (request.decision_note, stamp, stamp, request_id))
            conn.execute("UPDATE quality_cases SET row_version = row_version + 1, updated_at = ? WHERE case_id = ?", (stamp, case_id))
        return self.get_case(case_id, viewer)

    def get_case(self, case_id: str, viewer) -> dict:
        with closing(self.connect()) as conn:
            case, snapshot = self.case_rows(conn, case_id, viewer)
            return self.view(conn, case, snapshot, viewer)

    def list_cases(self, viewer, *, status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        where, params = [], []
        if viewer.role != ROLE_SYSTEM_ADMIN:
            where.append("(q.owner_user_id = ? OR q.department_id = ?)")
            params.extend([viewer.id, viewer.department_id if viewer.role == ROLE_EMPLOYEE else None])
        if status:
            where.append("c.status = ?")
            params.append(status)
        sql = "SELECT c.case_id FROM quality_cases c JOIN quality_snapshots q ON q.snapshot_id = c.snapshot_id"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY c.updated_at DESC, c.case_id LIMIT ? OFFSET ?"
        with closing(self.connect()) as conn:
            ids = conn.execute(sql, (*params, min(200, max(1, limit)), max(0, offset))).fetchall()
            return [self.view(conn, *self.case_rows(conn, r["case_id"], viewer), viewer) for r in ids]

    def build_share(self, conn, snapshot, case_id: str, viewer, request) -> dict:
        """The same frozen-copy guards apply to both sharing entry points."""
        if not self.source_valid(conn, snapshot) or not json.loads(snapshot["content_json"]):
            raise QualityConflict("fixed review content is unavailable")
        if json.loads(snapshot["manifest_json"]).get("has_attachments"):
            raise PermissionError("attachment review sharing requires a supported source adapter")
        content = json.loads(snapshot["content_json"])
        if len(set(request.fields)) != len(request.fields) or len(set(request.evidence_indices)) != len(request.evidence_indices):
            raise ValueError("shared fields and evidence indices must be unique")
        if any(i < 0 or i >= len(content.get("evidence", [])) for i in request.evidence_indices):
            raise ValueError("evidence index is outside the frozen snapshot")
        share = {"active": True, "fields": request.fields, "evidence_indices": request.evidence_indices,
                 "reviewer_user_ids": sorted(set(request.reviewer_user_ids)), "shared_by": viewer.id,
                 "expires_at": (datetime.now(timezone.utc) + timedelta(days=request.retention_days)).isoformat()}
        if "comment" in request.fields:
            event = conn.execute("SELECT event_id FROM quality_feedback_events WHERE case_id = ? ORDER BY sequence DESC LIMIT 1", (case_id,)).fetchone()
            share["comment_event_id"] = event[0] if event else None
        return share

    def change_share(self, viewer, case_id: str, request=None, *, row_version: int | None = None) -> dict:
        with self.transaction() as conn:
            case, snapshot = self.case_rows(conn, case_id, viewer, lock=True)
            if viewer.id != int(snapshot["owner_user_id"]) or viewer.role != ROLE_EMPLOYEE:
                raise PermissionError("only the conversation owner may share review material")
            expected = request.row_version if request else row_version
            if int(case["row_version"]) != expected:
                raise QualityConflict("case changed; refresh before editing")
            if request:
                share = self.build_share(conn, snapshot, case_id, viewer, request)
            else:
                share = {"active": False, "revoked_at": now_iso()}
            conn.execute("UPDATE quality_cases SET share_json = ?, row_version = row_version + 1, updated_at = ? WHERE case_id = ?", (dumps(share), now_iso(), case_id))
        return self.get_case(case_id, viewer)

    def submit_review(self, viewer, case_id: str, request) -> dict:
        payload_hash = sha256([case_id, request.model_dump()])
        with self.transaction() as conn:
            self.request_lock(conn, "review", viewer.id, request.client_request_id)
            case, snapshot = self.case_rows(conn, case_id, viewer, lock=True)
            view = self.view(conn, case, snapshot, viewer)
            if viewer.role != ROLE_EMPLOYEE or not view["source_valid"] or "answer" not in view["content"]:
                raise PermissionError("an active authorized answer copy is required for review")
            prior = conn.execute("SELECT * FROM quality_reviews WHERE reviewer_user_id = ? AND client_request_id = ?", (viewer.id, request.client_request_id)).fetchone()
            if prior:
                if prior["request_sha256"] != payload_hash:
                    raise QualityConflict("client_request_id was already used for a different review")
                return {"review_id": prior["review_id"], "deduplicated": True}
            if int(case["row_version"]) != request.row_version:
                raise QualityConflict("case changed; refresh before reviewing")
            if request.supersedes:
                previous = conn.execute("SELECT review_id FROM quality_reviews WHERE review_id = ? AND case_id = ?", (request.supersedes, case_id)).fetchone()
                if not previous:
                    raise ValueError("supersedes must refer to a review of this case")
            review_id = f"review-{uuid.uuid4().hex}"
            conn.execute(
                """INSERT INTO quality_reviews(review_id, case_id, reviewer_user_id, client_request_id,
                   request_sha256, judgment, root_cause, content_json, supersedes, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (review_id, case_id, viewer.id, request.client_request_id, payload_hash, request.judgment,
                 request.root_cause, dumps({"reference_answer": request.reference_answer, "notes": request.notes, "rubric": request.rubric, "correctness": request.correctness}), request.supersedes, now_iso()),
            )
            status = {"confirmed": "reviewed", "not_an_error": "rejected", "needs_information": "needs_information"}[request.judgment]
            conn.execute("UPDATE quality_cases SET status = ?, row_version = row_version + 1, updated_at = ? WHERE case_id = ?", (status, now_iso(), case_id))
            enqueue(conn, snapshot["snapshot_id"], "phoenix_annotation", f"review:{review_id}", {
                "identifier": f"review:{review_id}", "name": "expert_correctness", "annotator_kind": "HUMAN",
                "label": request.correctness, "score": 0 if request.correctness == "incorrect" else 1 if request.correctness == "correct" else None,
                "metadata": {"case_id": case_id, "review_id": review_id, "root_cause": request.root_cause, "judgment": request.judgment},
            })
            return {"review_id": review_id, "deduplicated": False}

    def claim(self, owner: str, *, lease_seconds: float = 60) -> dict | None:
        if not owner or lease_seconds <= 0:
            raise ValueError("owner and positive lease required")
        with self.transaction() as conn:
            clock = self.db_time(conn)
            sql = """SELECT * FROM quality_outbox WHERE available_at <= ? AND
                     (status = 'pending' OR (status = 'running' AND lease_until <= ?))
                     ORDER BY available_at, created_at, job_id LIMIT 1"""
            if self.backend == "postgres":
                sql += " FOR UPDATE SKIP LOCKED"
            row = conn.execute(sql, (clock, clock)).fetchone()
            if row is None:
                return None
            conn.execute("""UPDATE quality_outbox SET status = 'running', owner = ?, lease_until = ?,
                         fencing_token = fencing_token + 1, attempts = attempts + 1, updated_at = ? WHERE job_id = ?""",
                         (owner, clock + lease_seconds, now_iso(), row["job_id"]))
            return dict(conn.execute("SELECT * FROM quality_outbox WHERE job_id = ?", (row["job_id"],)).fetchone())

    def guarded_job(self, conn, claim):
        row = self.lock(conn, "SELECT * FROM quality_outbox WHERE job_id = ?", (claim["job_id"],))
        if (row is None or row["status"] != "running" or row["owner"] != claim["owner"]
                or int(row["fencing_token"]) != int(claim["fencing_token"])
                or float(row["lease_until"]) <= self.db_time(conn)):
            raise QualityLeaseLost("quality job ownership expired or was superseded")
        return row

    def finish(self, claim, *, status: str = "completed", retry_after: float = 0, error: str = "") -> None:
        if status not in {"completed", "no_span", "disabled", "pending", "failed"}:
            raise ValueError("invalid job status")
        with self.transaction() as conn:
            self.guarded_job(conn, claim)
            conn.execute("""UPDATE quality_outbox SET status = ?, owner = '', lease_until = 0,
                         available_at = ?, last_error = ?, updated_at = ? WHERE job_id = ?""",
                         (status, self.db_time(conn) + max(0, retry_after), error[:200], now_iso(), claim["job_id"]))

    def collect(self, claim) -> None:
        with self.transaction() as conn:
            self.guarded_job(conn, claim)
            snapshot = conn.execute("SELECT * FROM quality_snapshots WHERE snapshot_id = ?", (claim["snapshot_id"],)).fetchone()
            manifest = json.loads(snapshot["manifest_json"])
            probability = min(1.0, max(0.0, float(getattr(settings, "QUALITY_SAMPLE_RATE", 0.01))))
            selected = int(sha256(["quality-sample-v1", snapshot["turn_id"]])[:16], 16) / 2**64 < probability
            flags = manifest.get("diagnostic_flags", [])
            if flags:
                self.ensure_case(conn, snapshot["snapshot_id"], "diagnostic")
            if selected:
                # Serialize the daily cap across PostgreSQL consumers, too.
                self.lock(conn, "SELECT * FROM quality_schema WHERE id = 1")
                count = conn.execute("SELECT COUNT(*) FROM quality_cases WHERE sampling_json != '{}' AND created_at >= ?", (now_iso()[:10],)).fetchone()[0]
                if int(count) < max(0, int(getattr(settings, "QUALITY_SAMPLE_DAILY_LIMIT", 100))):
                    self.ensure_case(conn, snapshot["snapshot_id"], "random_sample", {"policy": "quality-sample-v1", "probability": probability, "cohort": manifest.get("query_mode")})
            conn.execute("UPDATE quality_outbox SET status = 'completed', owner = '', lease_until = 0, updated_at = ? WHERE job_id = ?", (now_iso(), claim["job_id"]))

    def backfill(self, *, limit: int = 100) -> int:
        """Explicit bounded repair; GET never constructs a historical snapshot.

        The durable turn answer/summary are used, never the latest KB or message.
        Old inputs edited/redacted before collection cannot be reconstructed.
        """
        from src.core.conversation import row_to_turn

        count = 0
        with self.transaction() as conn:
            rows = conn.execute("""SELECT t.*, s.user_id AS quality_owner FROM chat_turns t
                                 JOIN chat_sessions s ON s.id = t.session_id
                                 LEFT JOIN quality_snapshots q ON q.turn_id = t.id
                                 WHERE t.status = 'completed' AND q.snapshot_id IS NULL
                                 AND NOT EXISTS (SELECT 1 FROM chat_message_edits e WHERE e.message_id IN (t.user_message_id, t.assistant_message_id))
                                 ORDER BY t.finished_at, t.id LIMIT ?""", (max(1, min(1000, limit)),)).fetchall()
            for row in rows:
                turn = row_to_turn(row)
                captured = capture_completed_turn(conn, turn, int(row["quality_owner"]))
                if captured:
                    snapshot = conn.execute("SELECT manifest_json FROM quality_snapshots WHERE snapshot_id = ?", (captured,)).fetchone()
                    manifest = json.loads(snapshot[0])
                    manifest["capture_origin"] = "terminal_record_backfill"
                    # The current session owner can belong to a later turn.
                    manifest["execution_attempt_id"] = ""
                    manifest["fencing_token"] = 0
                    conn.execute("UPDATE quality_snapshots SET manifest_json = ? WHERE snapshot_id = ?", (dumps(manifest), captured))
                    count += 1
        return count

    def invalidate_source(self, *, kb_name: str, department_id: int, kb_id: int | None = None, filename: str | None = None) -> int:
        count = 0
        with self.transaction() as conn:
            rows = conn.execute("""SELECT q.* FROM quality_snapshots q JOIN chat_turns t ON t.id = q.turn_id
                                 WHERE t.kb_name = ? AND q.department_id = ?""", (kb_name, department_id)).fetchall()
            for row in rows:
                manifest = json.loads(row["manifest_json"])
                if kb_id is not None and manifest.get("kb_id") is not None and manifest["kb_id"] != kb_id:
                    continue
                if filename is not None and not any(e.get("file_name") == filename for e in json.loads(row["content_json"]).get("evidence", [])):
                    continue
                purge_snapshot_content(conn, row["snapshot_id"], "source_deleted")
                count += 1
        return count
