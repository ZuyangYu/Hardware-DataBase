"""Isolated PG backup/restore of quality provenance, exact prices/costs/budgets.

Requires HDB_TEST_POSTGRES_DSN and the matching fixture container. Creates two
random test databases, archives a synthetic-only dump, and drops both on exit.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import src.settings as settings
from src.core.auth import AuthUser
from src.costs.budgets import BudgetVersion
from src.costs.store import CostStore
from src.observability.slo import ChatSloStore
from src.quality.store import QualityStore
from src.quality.workflow import WorkflowStore
from src.storage.bootstrap import _control_plane
from src.storage.control_plane.conversation_service import PostgresConversationService


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.costs.test_accounting import fixture_event, fixture_price
    from tests.quality.test_slo import NOW, policy, task
    from tests.quality.test_workflow import prepare

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="hdb-quality-p1-pg")
    args = parser.parse_args()
    base = os.environ["HDB_TEST_POSTGRES_DSN"]
    names = ["p2_source_" + uuid.uuid4().hex[:12], "p2_restore_" + uuid.uuid4().hex[:12]]
    artifact = Path(__file__).resolve().parents[2] / "storage/evaluations/production-feedback-p2-20261008"
    artifact.mkdir(parents=True, exist_ok=True)
    keys = ("CONTROL_PLANE_BACKEND", "CONTROL_PLANE_DATABASE_URL", "MEMORY_ENABLED", "QUALITY_CAPTURE_CONTENT",
            "QUALITY_SAMPLE_RATE", "CONVERSATION_SERIALIZE_SAME_THREAD", "STORAGE_DIR")
    old = {key: getattr(settings, key) for key in keys}
    conv = None
    with tempfile.TemporaryDirectory(prefix="hdb-p2-backup-", dir="/tmp/opencode") as tmp:
        try:
            with psycopg.connect(base, autocommit=True) as conn:
                for name in names:
                    conn.execute(f'CREATE DATABASE "{name}"')
            settings.CONTROL_PLANE_BACKEND = "postgres"
            settings.MEMORY_ENABLED = False
            settings.QUALITY_CAPTURE_CONTENT = True
            settings.QUALITY_SAMPLE_RATE = 0
            settings.CONVERSATION_SERIALIZE_SAME_THREAD = True
            settings.STORAGE_DIR = tmp
            values = conninfo_to_dict(base)
            user = values["user"]
            values["dbname"] = names[0]
            settings.CONTROL_PLANE_DATABASE_URL = make_conninfo(**values)
            _control_plane(settings.CONTROL_PLANE_DATABASE_URL)
            store = QualityStore(initialize=True)
            with store.transaction() as conn:
                conn.execute("CREATE TABLE users(id BIGINT PRIMARY KEY, username TEXT, department_id BIGINT)")
                conn.execute("INSERT INTO users VALUES(1, 'owner', NULL)")
            owner = AuthUser(1, "owner", "employee", True, 10)
            reviewer = AuthUser(2, "reviewer", "employee", True, 10)
            admin = AuthUser(4, "admin", "system_admin", True)
            conv = PostgresConversationService(settings.CONTROL_PLANE_DATABASE_URL)
            session = conv.create_session(1, "kb", department_id=10)
            turn = conv.create_turn(1, session.id, "Original question")
            claim = conv.claim_turn(1, turn.id, "backup-worker")
            turn = conv.complete_turn(1, turn.id, "Original answer", {"evidence": [{"text": "Frozen evidence", "file_name": "private.pdf"}]},
                                      worker_id="backup-worker", fencing_token=claim.fencing_token)
            workflow, _case_id, version_id = prepare(SimpleNamespace(store=store, conv=conv, owner=owner, reviewer=reviewer, turn=turn))
            material = workflow.version(version_id, reviewer, materialize=True)
            costs = CostStore()
            costs.add_price(fixture_price())
            costs.ingest(fixture_event())
            costs.add_budget(admin, BudgetVersion(budget_version="backup-budget", department_id=10, currency="USD", amount_limit="0.005",
                                                  effective_from="2026-10-08T00:00:00Z", effective_to="2026-10-09T00:00:00Z"))
            report_id = workflow.analyze(reviewer, "backup-report", costs)["report_id"]
            slo = ChatSloStore(initialize=True)
            slo.observe_task(task(), policy(), now=NOW)
            dump = subprocess.run(["docker", "exec", args.container, "pg_dump", "-U", user, "-d", names[0], "-Fc"], check=True, capture_output=True).stdout
            (artifact / "p2-fixture.dump").write_bytes(dump)
            subprocess.run(["docker", "exec", "-i", args.container, "pg_restore", "-U", user, "-d", names[1]], input=dump, check=True, capture_output=True)
            conv.close()
            conv = None
            values["dbname"] = names[1]
            settings.CONTROL_PLANE_DATABASE_URL = make_conninfo(**values)
            restored = WorkflowStore()
            assert restored.version(version_id, reviewer, materialize=True)["samples"] == material["samples"]
            assert restored.reports(reviewer, report_id)[0]["verified_statistics"]["case_count"] == 1
            restored_costs = CostStore()
            assert restored_costs.summary(owner)["known_amounts"] == {"USD": "0.0068"}
            assert restored_costs.ingest(fixture_event()) is False
            budget = restored_costs.budget_statuses(owner, now=NOW)[0]
            assert budget["status"] == "exceeded_known_amount" and budget["known_amount"] == "0.0068"
            assert len(ChatSloStore().windows(now=NOW)) == 8
            result = {"fixture_only": True, "dataset_manifest_and_authorized_material_preserved": True,
                      "report_input_hash_and_statistics_preserved": True, "immutable_prices_and_decimal_amount_preserved": True,
                      "usage_replay_deduplicated_after_restore": True, "immutable_budget_and_known_lower_bound_preserved": True,
                      "slo_policy_and_task_windows_preserved": True, "dump_bytes": len(dump)}
            (artifact / "backup-restore.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(result))
        finally:
            if conv:
                conv.close()
            for key, value in old.items():
                setattr(settings, key, value)
            with psycopg.connect(base, autocommit=True) as conn:
                for name in names:
                    conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


if __name__ == "__main__":
    main()
