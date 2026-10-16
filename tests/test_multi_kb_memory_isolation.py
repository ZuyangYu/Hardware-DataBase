from __future__ import annotations

from contextlib import closing

import src.settings as settings
from src.core.auth import AuthService, ROLE_EMPLOYEE
from src.core.conversation import ConversationService
from src.services.knowledge_scope import KnowledgeScopeResolver
from src.services.knowledge_scope_models import KnowledgeSelectionInput


def test_multi_kb_completion_does_not_enqueue_project_reflection(tmp_path, monkeypatch):
    db_path = str(tmp_path / "auth.db")
    monkeypatch.setattr(settings, "AUTH_DEFAULT_ADMIN_PASSWORD", "test-admin-password-123")
    monkeypatch.setattr(settings, "AUTH_DEFAULT_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(settings, "AUTH_DB_PATH", db_path)
    monkeypatch.setattr(settings, "MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_EXTRACTION_ENABLED", True)

    auth = AuthService(db_path=db_path)
    admin = auth.get_user_by_username("admin")
    department = auth.create_department("hardware")
    owner = auth.create_user_as(admin, "owner", "password-123", ROLE_EMPLOYEE, department.id)
    user = auth.create_user_as(admin, "engineer", "password-123", ROLE_EMPLOYEE, department.id)
    auth.register_knowledge_base("target", owner=owner, is_shared=True)
    auth.register_knowledge_base("reference", owner=owner, is_shared=True)
    target_id = auth.get_knowledge_base_id("target", department_id=department.id)
    reference_id = auth.get_knowledge_base_id("reference", department_id=department.id)

    selection = KnowledgeSelectionInput(
        purpose="answer",
        sources=[
            {"kb_id": target_id, "role": "primary"},
            {"kb_id": reference_id, "role": "reference"},
        ],
    )
    scope = KnowledgeScopeResolver(auth).resolve(user, "target", selection)
    conversation = ConversationService(db_path=db_path)
    session = conversation.create_session(
        user.id,
        "target",
        department_id=department.id,
        kb_id=target_id,
        knowledge_selection=selection.model_dump(mode="json"),
    )
    turn = conversation.create_turn(user.id, session.id, "compare values", knowledge_scope=scope)
    conversation.complete_turn(user.id, turn.id, "answer", {"status": "ok"})

    with closing(conversation._connect()) as connection:
        row = connection.execute(
            "SELECT 1 FROM memory_jobs WHERE session_id = ? AND job_kind = 'project_reflection'",
            (session.id,),
        ).fetchone()
    assert row is None
