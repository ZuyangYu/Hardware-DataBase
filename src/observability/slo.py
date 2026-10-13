"""Pilot chat SLIs from durable accepted/terminal facts, independent of spans.

An explicitly versioned policy is required. Each task freezes that policy on
first observation. Deadline/first-token violations are monotonic; takeover,
late success, or a late cancellation cannot erase a recorded violation.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.quality.store import QualityStore, dumps, sha256


class ChatSloPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=80)
    effective_from: datetime
    deadline_seconds: int = Field(gt=0, le=86400)
    first_token_seconds: int = Field(gt=0, le=86400)
    availability_target: float = Field(gt=0, lt=1)
    minimum_events: int = Field(default=5, ge=1, le=10000)

    @model_validator(mode="after")
    def valid(self):
        if self.effective_from.tzinfo is None:
            raise ValueError("SLO policy effective_from requires timezone")
        self.effective_from = self.effective_from.astimezone(timezone.utc)
        if self.first_token_seconds > self.deadline_seconds:
            raise ValueError("first-token deadline cannot exceed final deadline")
        return self


DDL = (
    "CREATE TABLE IF NOT EXISTS business_slo_schema(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS business_slo_policies(version TEXT PRIMARY KEY, policy_sha256 TEXT NOT NULL, policy_json TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS business_slo_tasks(
        task_id TEXT PRIMARY KEY, workload TEXT NOT NULL, accepted_at TEXT NOT NULL,
        policy_version TEXT NOT NULL, policy_json TEXT NOT NULL, status TEXT NOT NULL,
        first_at TEXT, finished_at TEXT, bad INTEGER NOT NULL, first_bad INTEGER NOT NULL,
        excluded INTEGER NOT NULL, settled INTEGER NOT NULL, reason TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_business_slo_window ON business_slo_tasks(accepted_at, workload)",
)


def utc(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


class ChatSloStore(QualityStore):
    def __init__(self, db_path=None, *, initialize=False):
        super().__init__(db_path, initialize=initialize)
        with self.transaction() as conn:
            if self.backend == "sqlite" or initialize:
                for ddl in DDL:
                    conn.execute(ddl)
                conn.execute("INSERT INTO business_slo_schema VALUES(1, 1) ON CONFLICT(id) DO NOTHING")
            if conn.execute("SELECT version FROM business_slo_schema WHERE id=1").fetchone()[0] != 1:
                raise RuntimeError("business SLO schema version mismatch")
            conn.execute("SELECT task_id, workload, accepted_at, policy_json, status, first_at, finished_at, bad, first_bad, excluded, settled, reason FROM business_slo_tasks LIMIT 0")

    def observe_task(self, task, policy: ChatSloPolicy, *, now=None, authoritative=False):
        now = now or datetime.now(timezone.utc)
        accepted = utc(task["created_at"])
        with self.transaction() as conn:
            if self.backend == "postgres":
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", ("slo:" + task["id"],))
            if authoritative:
                row = conn.execute("""SELECT t.id, t.kb_name, t.created_at, t.finished_at, t.status,
                    (SELECT MIN(e.created_at) FROM chat_turn_events e WHERE e.turn_id=t.id AND e.event_type='delta') AS first_at
                    FROM chat_turns t WHERE t.id=?""", (task["id"],)).fetchone()
                if row is None:
                    return False
                task = dict(row)
                accepted = utc(task["created_at"])
            serialized = dumps(policy.model_dump(mode="json"))
            conn.execute("INSERT INTO business_slo_policies VALUES(?, ?, ?) ON CONFLICT(version) DO NOTHING", (policy.version, sha256(serialized), serialized))
            prior_policy = conn.execute("SELECT policy_sha256 FROM business_slo_policies WHERE version=?", (policy.version,)).fetchone()
            if prior_policy[0] != sha256(serialized):
                raise ValueError("SLO policy versions are immutable; use a new version")
            old = conn.execute("SELECT * FROM business_slo_tasks WHERE task_id=?", (task["id"],)).fetchone()
            if old:
                policy = ChatSloPolicy.model_validate_json(old["policy_json"])
                serialized = old["policy_json"]
            elif accepted < policy.effective_from:
                return False  # Explicit policy does not retrospectively claim coverage.
            first = utc(task["first_at"]) if task.get("first_at") else None
            finished = utc(task["finished_at"]) if task.get("finished_at") else None
            status = task["status"]
            terminal = status in {"completed", "failed", "cancelled"}
            deadline = accepted + timedelta(seconds=policy.deadline_seconds)
            first_deadline = accepted + timedelta(seconds=policy.first_token_seconds)
            # Only an explicit pre-deadline user cancellation is excluded.
            excluded = terminal and status == "cancelled" and finished is not None and finished < min(deadline, first_deadline)
            final_violation = (finished > deadline if terminal and finished else now > deadline)
            first_violation = first > first_deadline if first else terminal or now > first_deadline
            bad = final_violation or (terminal and status in {"failed", "cancelled"} and not excluded) or (terminal and finished is None)
            first_bad = first_violation
            if old:
                bad |= bool(old["bad"])
                first_bad |= bool(old["first_bad"])
                excluded &= not bad and not first_bad
            if excluded:
                bad = first_bad = False
            settled = terminal or bad
            reason = "deadline" if final_violation else "failed" if status == "failed" else "late_cancellation" if status == "cancelled" and not excluded else "missing_terminal_time" if terminal and not finished else ""
            if old and old["reason"]:
                reason = old["reason"]
            conn.execute("""INSERT INTO business_slo_tasks VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, first_at=excluded.first_at,
                finished_at=excluded.finished_at, bad=excluded.bad, first_bad=excluded.first_bad,
                excluded=excluded.excluded, settled=excluded.settled, reason=excluded.reason, updated_at=excluded.updated_at""",
                (task["id"], "general_chat" if task.get("kb_name") == "__general__" else "kb_chat", accepted.isoformat(),
                 policy.version, serialized, status, first.isoformat() if first else None, finished.isoformat() if finished else None,
                 int(bad), int(first_bad), int(excluded), int(settled), reason, now.isoformat()))
        return True

    def reconcile(self, policy, *, now=None):
        now = now or datetime.now(timezone.utc)
        with closing(self.connect()) as conn:
            rows = conn.execute("""SELECT t.id, t.kb_name, t.created_at, t.finished_at, t.status,
                (SELECT MIN(e.created_at) FROM chat_turn_events e WHERE e.turn_id=t.id AND e.event_type='delta') AS first_at
                FROM chat_turns t LEFT JOIN business_slo_tasks b ON b.task_id=t.id
                WHERE (t.created_at>=? OR b.task_id IS NOT NULL)
                AND (b.task_id IS NULL OR b.status NOT IN ('completed', 'failed', 'cancelled') OR b.status<>t.status)""", (policy.effective_from.isoformat(),)).fetchall()
        return sum(self.observe_task(dict(row), policy, now=now, authoritative=True) for row in rows)

    def windows(self, *, now=None):
        now = now or datetime.now(timezone.utc)
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT * FROM business_slo_tasks WHERE accepted_at>=?", ((now - timedelta(hours=6)).isoformat(),)).fetchall()
        output = []
        for workload in ("general_chat", "kb_chat"):
            for label, seconds in (("5m", 300), ("30m", 1800), ("1h", 3600), ("6h", 21600)):
                subset = [r for r in rows if r["workload"] == workload and utc(r["accepted_at"]) > now - timedelta(seconds=seconds)]
                eligible = [r for r in subset if r["settled"] and not r["excluded"]]
                bad = sum(bool(r["bad"]) for r in eligible)
                allowance = sum(1 - ChatSloPolicy.model_validate_json(r["policy_json"]).availability_target for r in eligible)
                minimum = max((ChatSloPolicy.model_validate_json(r["policy_json"]).minimum_events for r in eligible), default=1)
                first_eligible = [r for r in subset if not r["excluded"] and (r["first_at"] or r["first_bad"] or r["settled"])]
                output.append({"workload": workload, "window": label, "accepted": len(subset), "eligible": len(eligible),
                               "bad": bad, "first_eligible": len(first_eligible), "first_bad": sum(bool(r["first_bad"]) for r in first_eligible),
                               "pending": sum(not r["settled"] for r in subset), "excluded": sum(bool(r["excluded"]) for r in subset),
                               "minimum_events": minimum, "burn_rate": bad / allowance if allowance else None,
                               "coverage": "measured" if eligible else "insufficient_traffic", "updated_at": now.isoformat()})
        return output


_WINDOWS = []
_GAUGES = {}


def publish_windows(windows):
    """DB-derived gauges survive restarts and don't double-count worker attempts."""
    global _WINDOWS
    _WINDOWS = windows
    from .metrics import _enabled, _meter
    from opentelemetry.metrics import Observation

    if not _enabled():
        return
    for name, key in (("hdb.slo.eligible", "eligible"), ("hdb.slo.bad", "bad"), ("hdb.slo.minimum_events", "minimum_events"),
                      ("hdb.slo.pending", "pending"), ("hdb.slo.burn_rate", "burn_rate"), ("hdb.slo.first_bad", "first_bad")):
        if name not in _GAUGES:
            def values(_options, key=key):
                return [Observation(w[key], {"workload": w["workload"], "window": w["window"]}) for w in _WINDOWS if w[key] is not None]
            _GAUGES[name] = _meter().create_observable_gauge(name, callbacks=[values])
