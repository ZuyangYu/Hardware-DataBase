"""Additive support-ticket tables in the authoritative conversation database."""

DDL = (
    """CREATE TABLE IF NOT EXISTS quality_tickets (
        ticket_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, case_id TEXT NOT NULL,
        feedback_event_id TEXT NOT NULL, owner_user_id BIGINT NOT NULL,
        requester_username TEXT NOT NULL, department_id BIGINT, department_name TEXT NOT NULL DEFAULT '',
        assignee_user_id BIGINT, assignee_username TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','in_progress','waiting','resolved','closed')),
        row_version INTEGER NOT NULL DEFAULT 1, consent_active INTEGER NOT NULL DEFAULT 1,
        reason_codes_json TEXT NOT NULL DEFAULT '[]', comment TEXT NOT NULL DEFAULT '',
        contact TEXT NOT NULL DEFAULT '', resolution TEXT NOT NULL DEFAULT '',
        deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY(snapshot_id) REFERENCES quality_snapshots(snapshot_id) ON DELETE CASCADE,
        FOREIGN KEY(case_id) REFERENCES quality_cases(case_id) ON DELETE CASCADE)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_quality_tickets_active_snapshot ON quality_tickets(snapshot_id) WHERE deleted_at IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_quality_tickets_queue ON quality_tickets(status, updated_at)",
    "CREATE INDEX IF NOT EXISTS ix_quality_tickets_owner ON quality_tickets(owner_user_id, updated_at)",
    """CREATE TABLE IF NOT EXISTS quality_ticket_events (
        event_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, actor_user_id BIGINT NOT NULL,
        actor_username TEXT NOT NULL, actor_role TEXT NOT NULL, client_request_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(actor_user_id, client_request_id),
        FOREIGN KEY(ticket_id) REFERENCES quality_tickets(ticket_id) ON DELETE CASCADE)""",
    "CREATE INDEX IF NOT EXISTS ix_quality_ticket_events_ticket ON quality_ticket_events(ticket_id, created_at)",
)

CHECKS = (
    "SELECT ticket_id, snapshot_id, case_id, feedback_event_id, owner_user_id, requester_username, department_id, department_name, assignee_user_id, assignee_username, status, row_version, consent_active, reason_codes_json, comment, contact, resolution, deleted_at, created_at, updated_at FROM quality_tickets LIMIT 0",
    "SELECT event_id, ticket_id, actor_user_id, actor_username, actor_role, client_request_id, request_sha256, kind, message, status, created_at FROM quality_ticket_events LIMIT 0",
)
