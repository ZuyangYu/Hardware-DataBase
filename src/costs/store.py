"""Append-only usage events and a monotonic leaf-call projection."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from decimal import Decimal
from datetime import datetime, timezone

import src.settings as settings
from src.core.auth import ROLE_SYSTEM_ADMIN
from src.quality.store import dumps, now_iso, sha256
from .pricing import PriceVersion, price
from .budgets import BudgetVersion

DDL = (
    "CREATE TABLE IF NOT EXISTS llm_cost_schema(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS llm_price_versions(
       price_version TEXT PRIMARY KEY, match_json TEXT NOT NULL, price_json TEXT NOT NULL,
       effective_from TEXT NOT NULL, effective_to TEXT, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS llm_usage_events(
       event_id TEXT PRIMARY KEY, invocation_id TEXT NOT NULL, sequence INTEGER NOT NULL,
       event_sha256 TEXT NOT NULL, event_json TEXT NOT NULL, created_at TEXT NOT NULL,
       UNIQUE(invocation_id, sequence))""",
    """CREATE TABLE IF NOT EXISTS llm_invocations(
       invocation_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL,
       owner_user_id BIGINT, department_id BIGINT, task_type TEXT NOT NULL, task_id TEXT NOT NULL,
       purpose TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
       status TEXT NOT NULL, cost_status TEXT NOT NULL, currency TEXT,
       cost_amount TEXT, invocation_json TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_cost_scope_time ON llm_invocations(department_id, started_at)",
    "CREATE INDEX IF NOT EXISTS ix_cost_task ON llm_invocations(task_type, task_id)",
    """CREATE TABLE IF NOT EXISTS llm_cost_budget_versions(
        budget_version TEXT PRIMARY KEY, scope_json TEXT NOT NULL, budget_json TEXT NOT NULL,
        effective_from TEXT NOT NULL, effective_to TEXT NOT NULL, created_at TEXT NOT NULL)""",
)


class CostConflict(ValueError):
    pass


class CostStore:
    def __init__(self, db_path: str | None = None, *, initialize: bool = False, backend: str | None = None, dsn: str | None = None):
        self.backend = backend or ("sqlite" if db_path else settings.CONTROL_PLANE_BACKEND)
        self.dsn = settings.CONTROL_PLANE_DATABASE_URL if dsn is None else dsn
        if self.backend not in {"sqlite", "postgres"} or (self.backend == "postgres" and not self.dsn):
            raise ValueError("valid cost storage backend and PostgreSQL DSN required")
        self.db_path = os.path.abspath(db_path or os.path.join(settings.STORAGE_DIR, "llm-costs.db"))
        if self.backend == "sqlite":
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self.transaction() as conn:
            if self.backend == "sqlite" or initialize:
                for ddl in DDL:
                    # PG monetary authority uses NUMERIC; SQLite keeps an exact
                    # canonical decimal string rather than an IEEE-754 float.
                    conn.execute(ddl.replace("cost_amount TEXT", "cost_amount NUMERIC") if self.backend == "postgres" else ddl)
                conn.execute("INSERT INTO llm_cost_schema(id, version) VALUES(1, 1) ON CONFLICT(id) DO NOTHING")
            self.assert_schema(conn)

    @staticmethod
    def assert_schema(conn):
        try:
            row = conn.execute("SELECT version FROM llm_cost_schema WHERE id=1").fetchone()
            if row is None or int(row[0]) != 1:
                raise RuntimeError("cost schema version mismatch")
            for sql in (
                "SELECT price_version, match_json, price_json, effective_from, effective_to, created_at FROM llm_price_versions LIMIT 0",
                "SELECT event_id, invocation_id, sequence, event_sha256, event_json, created_at FROM llm_usage_events LIMIT 0",
                "SELECT invocation_id, sequence, owner_user_id, department_id, task_type, task_id, purpose, started_at, finished_at, status, cost_status, currency, cost_amount, invocation_json, updated_at FROM llm_invocations LIMIT 0",
                "SELECT budget_version, scope_json, budget_json, effective_from, effective_to, created_at FROM llm_cost_budget_versions LIMIT 0",
            ):
                conn.execute(sql)
        except Exception as exc:
            raise RuntimeError("cost schema not ready; run python -m src.storage.bootstrap") from exc

    def connect(self):
        if self.backend == "postgres":
            from src.storage.domain_pg import open_pg_connection
            from psycopg.conninfo import make_conninfo

            conn = open_pg_connection(make_conninfo(self.dsn, connect_timeout=2))
            conn.execute("SET statement_timeout = '5s'")
            return conn
        conn = sqlite3.connect(self.db_path, timeout=2, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
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

    def add_price(self, version: PriceVersion) -> None:
        match = dumps([version.endpoint_alias, version.provider, version.requested_model, version.response_model])
        payload = dumps(version.model_dump(mode="json"))
        with self.transaction() as conn:
            if self.backend == "postgres":
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", ("cost-price:" + match,))
            prior = conn.execute("SELECT price_json FROM llm_price_versions WHERE price_version=?", (version.price_version,)).fetchone()
            if prior:
                if prior[0] != payload:
                    raise CostConflict("price version is immutable")
                return
            existing = conn.execute("SELECT effective_from, effective_to FROM llm_price_versions WHERE match_json=?", (match,)).fetchall()
            if any((r["effective_to"] is None or version.effective_from < r["effective_to"])
                   and (version.effective_to is None or r["effective_from"] < version.effective_to) for r in existing):
                raise CostConflict("overlapping price intervals")
            conn.execute("INSERT INTO llm_price_versions VALUES(?, ?, ?, ?, ?, ?)", (version.price_version, match, payload, version.effective_from, version.effective_to, now_iso()))

    @staticmethod
    def matching_price(conn, event):
        match = dumps([event.get(k, "") for k in ("endpoint_alias", "provider", "requested_model", "response_model")])
        row = conn.execute("SELECT price_json FROM llm_price_versions WHERE match_json=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>?)", (match, event["started_at"], event["started_at"])).fetchone()
        return PriceVersion.model_validate_json(row[0]) if row else None

    def ingest(self, event: dict) -> bool:
        if event["sequence"] not in (0, 1) or event["event_id"] != f"{event['invocation_id']}:{event['sequence']}":
            raise ValueError("invalid cost event identity")
        digest = sha256(event)
        with self.transaction() as conn:
            if self.backend == "postgres":
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", ("cost-event:" + event["invocation_id"],))
            existing = conn.execute("SELECT event_sha256 FROM llm_usage_events WHERE event_id=?", (event["event_id"],)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise CostConflict("cost event identity reused for different content")
                return False
            conn.execute("INSERT INTO llm_usage_events VALUES(?, ?, ?, ?, ?, ?)", (event["event_id"], event["invocation_id"], event["sequence"], digest, dumps(event), now_iso()))
            priced = price(event, self.matching_price(conn, event)) if event["sequence"] else {"cost_status": "unknown_usage", "cost_amount": None, "currency": None, "price_version": None, "pricing_algorithm_version": None}
            projected = {**event, **priced}
            conn.execute("""INSERT INTO llm_invocations(invocation_id, sequence, owner_user_id, department_id,
                task_type, task_id, purpose, started_at, finished_at, status, cost_status, currency,
                cost_amount, invocation_json, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(invocation_id) DO UPDATE SET sequence=excluded.sequence,
                finished_at=excluded.finished_at, status=excluded.status, cost_status=excluded.cost_status,
                currency=excluded.currency, cost_amount=excluded.cost_amount, invocation_json=excluded.invocation_json,
                updated_at=excluded.updated_at WHERE llm_invocations.sequence < excluded.sequence""",
                         (event["invocation_id"], event["sequence"], event.get("owner_user_id"), event.get("department_id"),
                          event["task_type"], event["task_id"], event["purpose"], event["started_at"], event.get("finished_at", ""),
                          event["status"], priced["cost_status"], priced["currency"], priced["cost_amount"], dumps(projected), now_iso()))
            return True

    @staticmethod
    def filters(viewer, *, start=None, end=None, purpose=None, task_type=None, task_id=None, model_alias=None, endpoint_alias=None, department_id=None, project_id=None):
        clauses, params = [], []
        if viewer.role != ROLE_SYSTEM_ADMIN:
            clauses.append("(owner_user_id=? OR department_id=?)")
            params.extend([viewer.id, viewer.department_id])
        for column, op, value in (("started_at", ">=", start), ("started_at", "<", end), ("purpose", "=", purpose), ("task_type", "=", task_type), ("task_id", "=", task_id), ("department_id", "=", department_id)):
            if value is not None:
                clauses.append(f"{column}{op}?")
                params.append(value)
        for name, value in (("model_alias", model_alias), ("endpoint_alias", endpoint_alias), ("project_id", project_id)):
            if value is not None:
                clauses.append(f"json_extract(invocation_json, '$.{name}')=?")
                params.append(value)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    def invocations(self, viewer, *, limit=100, offset=0, **filters) -> list[dict]:
        where, params = self.filters(viewer, **filters)
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT invocation_json FROM llm_invocations" + where + " ORDER BY started_at DESC, invocation_id LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        # No prompts, response bodies, endpoints or keys are stored in the ledger.
        return [json.loads(row[0]) for row in rows]

    def summary(self, viewer, **filters):
        where, params = self.filters(viewer, **filters)
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT cost_status, currency, cost_amount, invocation_json, updated_at FROM llm_invocations" + where, params).fetchall()
        return self._summarize_rows(viewer, rows)

    @staticmethod
    def _summarize_rows(viewer, rows):
        currencies, purposes = {}, {}
        tokens = {k: 0 for k in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")}
        reported = priced = logical = unfinished = unattributed = 0
        for row in rows:
            event = json.loads(row["invocation_json"])
            reported += event.get("usage_status") == "reported"
            priced += row["cost_status"] == "priced"
            logical += event.get("attempt_visibility") == "logical_only"
            unfinished += event["status"] == "started"
            unattributed += event["task_type"] == "unattributed"
            for k in tokens:
                tokens[k] += event.get(k) or 0
            p = purposes.setdefault(event["purpose"], {"calls": 0, "known_amounts": {}})
            p["calls"] += 1
            if row["cost_amount"] is not None:
                currency = row["currency"]
                amount = Decimal(str(row["cost_amount"]))
                currencies[currency] = currencies.get(currency, Decimal(0)) + amount
                p["known_amounts"][currency] = p["known_amounts"].get(currency, Decimal(0)) + amount
        n = len(rows)
        for p in purposes.values():
            p["known_amounts"] = {k: str(v) for k, v in p["known_amounts"].items()}
        return {"calls": n, "known_amounts": {k: str(v) for k, v in currencies.items()},
                "reported_usage_calls": reported, "priced_calls": priced, "unknown_cost_calls": n - priced,
                "usage_coverage": reported / n if n else None, "price_coverage": priced / n if n else None,
                "logical_only_calls": logical, "unfinished_calls": unfinished, "unattributed_calls": unattributed,
                "tokens": tokens, "by_purpose": purposes, "updated_at": max((r["updated_at"] for r in rows), default=None),
                "scope": "global" if viewer.role == ROLE_SYSTEM_ADMIN else "department_and_owned_tasks",
                "amount_kind": "application_token_estimate", "settlement_status": "not_reconciled",
                "coverage_denominator": "recorded_invocations", "accounting_completeness": "not_proven",
                "accounting_loss_signal": "hdb_cost_sink_events_total{status=dropped|corrupt}"}

    def trends(self, viewer, *, bucket="day", **filters):
        if bucket not in {"day", "hour"}:
            raise ValueError("cost time bucket must be day or hour")
        where, params = self.filters(viewer, **filters)
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT cost_status, currency, cost_amount, invocation_json, updated_at FROM llm_invocations" + where + " ORDER BY started_at, invocation_id LIMIT 10001", params).fetchall()
        if len(rows) > 10000:
            raise ValueError("narrow the time window; detailed trends support at most 10000 calls")
        buckets, models = {}, {}
        for row in rows:
            event = json.loads(row["invocation_json"])
            started = datetime.fromisoformat(event["started_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
            rounded = started.replace(minute=0, second=0, microsecond=0, **({"hour": 0} if bucket == "day" else {}))
            buckets.setdefault(rounded.isoformat(), []).append(row)
            key = (event.get("endpoint_alias", "unregistered"), event.get("model_alias", "unregistered"), event.get("requested_model", ""), event.get("response_model", ""))
            models.setdefault(key, []).append(row)
        model_totals = [{"endpoint_alias": key[0], "model_alias": key[1], "requested_model": key[2], "response_model": key[3],
                         **self._summarize_rows(viewer, values)} for key, values in sorted(models.items())]
        currencies = {currency for item in model_totals for currency in item["known_amounts"]}
        rankings = {currency: sorted(
            [{"endpoint_alias": item["endpoint_alias"], "model_alias": item["model_alias"], "requested_model": item["requested_model"],
              "response_model": item["response_model"], "known_amount": item["known_amounts"][currency], "calls": item["calls"],
              "unknown_cost_calls": item["unknown_cost_calls"], "price_coverage": item["price_coverage"]}
             for item in model_totals if currency in item["known_amounts"]], key=lambda item: Decimal(item["known_amount"]), reverse=True)
            for currency in sorted(currencies)}
        return {"bucket": bucket, "timezone": "UTC", "summary": self._summarize_rows(viewer, rows),
                "buckets": [{"bucket_start": key, **self._summarize_rows(viewer, values)} for key, values in sorted(buckets.items())],
                "models": model_totals, "rankings_by_currency": rankings, "empty_bucket_policy": "omitted_not_assumed_zero"}

    def add_budget(self, viewer, version: BudgetVersion):
        if viewer.role != ROLE_SYSTEM_ADMIN:
            raise PermissionError("only a system administrator may register budgets")
        scope = dumps([version.department_id, version.purpose, version.model_alias, version.currency])
        payload = dumps(version.model_dump(mode="json"))
        with self.transaction() as conn:
            if self.backend == "postgres":
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", ("cost-budget:" + scope,))
            prior = conn.execute("SELECT budget_json FROM llm_cost_budget_versions WHERE budget_version=?", (version.budget_version,)).fetchone()
            if prior:
                if prior[0] != payload:
                    raise CostConflict("budget versions are immutable")
                return
            overlaps = conn.execute("SELECT budget_version FROM llm_cost_budget_versions WHERE scope_json=? AND effective_from<? AND effective_to>?",
                                    (scope, version.effective_to, version.effective_from)).fetchone()
            if overlaps:
                raise CostConflict("overlapping budget windows for the same scope/currency")
            conn.execute("INSERT INTO llm_cost_budget_versions VALUES(?, ?, ?, ?, ?, ?)",
                         (version.budget_version, scope, payload, version.effective_from, version.effective_to, now_iso()))

    def budget_statuses(self, viewer, *, now=None, active_only=False):
        now = now or datetime.now(timezone.utc)
        stamp = now.astimezone(timezone.utc).isoformat()
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT budget_json FROM llm_cost_budget_versions ORDER BY effective_from DESC LIMIT 1001").fetchall()
        if len(rows) > 1000:
            raise ValueError("budget policy listing exceeds supported limit")
        output = []
        for row in rows:
            policy = BudgetVersion.model_validate_json(row[0])
            if viewer.role != ROLE_SYSTEM_ADMIN and (viewer.department_id is None or policy.department_id != viewer.department_id):
                continue
            active = policy.effective_from <= stamp < policy.effective_to
            if active_only and not active:
                continue
            # A department budget counts that department only, not the viewer's
            # separately owned cross-department tasks. The policy defines its scope.
            summary = self.summary(viewer, start=policy.effective_from, end=min(policy.effective_to, stamp),
                                   department_id=policy.department_id, purpose=policy.purpose, model_alias=policy.model_alias)
            known = Decimal(summary["known_amounts"].get(policy.currency, "0"))
            exceeded = known >= policy.amount_limit
            status = "exceeded_known_amount" if exceeded else "no_recorded_calls" if not summary["calls"] else "insufficient_cost_coverage" if summary["unknown_cost_calls"] else "below_limit_known_amount"
            output.append({"policy": policy.model_dump(mode="json"), "active": active, "known_amount": str(known),
                           "known_utilization": str(known / policy.amount_limit), "status": status, "summary": summary,
                           "budget_kind": "observational_application_token_estimate", "completeness": "recorded_calls_only_not_proven",
                           "period_status": "future" if stamp < policy.effective_from else "active" if active else "ended"})
        return output
