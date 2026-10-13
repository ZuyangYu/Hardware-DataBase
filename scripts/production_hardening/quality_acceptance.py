"""Isolated browser fixture server: real quality/auth API, fixed KB answer.

Run from the repository root with uv run python. All storage is disposable;
HDB_ENV_PATH is disabled before importing application configuration.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8211)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    with tempfile.TemporaryDirectory(prefix="hdb-quality-browser-", dir="/tmp/opencode" if Path("/tmp/opencode").exists() else None) as root:
        os.environ.update({"HDB_ENV_PATH": os.devnull, "STORAGE_DIR": root, "AUTH_DB_PATH": str(Path(root) / "auth.db"),
                           "OBS_ENABLED": "false", "REDIS_REQUIRED": "false", "REDIS_URL": "", "MEMORY_ENABLED": "false",
                           "AUTH_DEFAULT_ADMIN_USERNAME": "quality_admin", "AUTH_DEFAULT_ADMIN_PASSWORD": "BrowserTestPassword123!", "QUALITY_SAMPLE_RATE": "0"})
        for prefix in ("CONTROL_PLANE", "EVALUATION_STORE", "MEMORY_STORE", "AGENT_CHECKPOINT", "DOCUMENT_AUTHORING_CHECKPOINTER", "RESOURCE_GOVERNOR", "PROJECT_STORE", "PIPELINE_DOCUMENT_STORE", "AUTHORING_STORE", "AUTH_STORE", "COMPATIBILITY_STORE"):
            os.environ[f"{prefix}_BACKEND"] = "sqlite"
        from src.api.app import create_app
        from src.api.deps import get_auth_service, get_pipeline
        from src.core.auth import AuthService
        from src.core.conversation import ConversationService
        from tests._api_stub import StubPipeline
        import uvicorn

        auth = AuthService()
        admin = auth.get_user_by_username("quality_admin")
        department = auth.create_department("Quality acceptance")
        owner = auth.create_user_as(admin, "quality_owner", "BrowserTestPassword123!", department_id=department.id)
        auth.create_user_as(admin, "quality_reviewer", "BrowserTestPassword123!", department_id=department.id)
        auth.register_knowledge_base("shared", owner=owner, is_shared=True)
        conv = ConversationService()
        session = conv.create_session(owner.id, "shared", "Fixed quality case", department_id=department.id)
        turn = conv.create_turn(owner.id, session.id, "验收：电阻取值依据是什么？")
        conv.complete_turn(owner.id, turn.id, "验收固定回答：电阻应为 10 kΩ。", {"evidence": [{"source_name": "acceptance.pdf", "content": "冻结证据：参考设计使用 4.7 kΩ。"}]})
        from src.costs.store import CostStore
        from src.costs.budgets import BudgetVersion
        from src.quality.workflow import WorkflowStore
        from tests.costs.test_accounting import fixture_event, fixture_price

        WorkflowStore()
        cost_store = CostStore()
        cost_store.add_price(fixture_price())
        cost_store.ingest(fixture_event(owner_user_id=owner.id, department_id=department.id, task_id=turn.id))
        cost_store.ingest(fixture_event(invocation_id="call-unknown", owner_user_id=owner.id, department_id=department.id, task_id=turn.id, usage_status="missing", input_tokens=None, output_tokens=None))
        cost_store.ingest(fixture_event(invocation_id="call-cross-department", owner_user_id=99999, department_id=99999, task_id="cross-department-task"))
        from src.core.auth import AuthUser

        cost_store.add_budget(AuthUser(0, "fixture-budget-admin", "system_admin", True), BudgetVersion(
            budget_version="fixture-department-budget", department_id=department.id, currency="USD", amount_limit="0.005",
            effective_from="2026-01-01T00:00:00Z", effective_to="2027-01-01T00:00:00Z"))
        app = create_app()
        stub = StubPipeline()
        app.dependency_overrides[get_auth_service] = lambda: auth
        app.dependency_overrides[get_pipeline] = lambda: stub
        uvicorn.run(app, host="127.0.0.1", port=args.port, lifespan="off")


if __name__ == "__main__":
    main()
