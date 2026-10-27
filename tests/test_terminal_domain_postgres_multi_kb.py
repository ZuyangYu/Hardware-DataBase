from types import SimpleNamespace


def test_postgres_terminal_domains_skips_project_memory_for_multi_kb_turn(monkeypatch):
    from src.storage.control_plane.terminal_domain import write_terminal_domains

    monkeypatch.setattr("src.settings.RESULT_EXPORT_ENABLED", False)
    monkeypatch.setattr("src.settings.MEMORY_ENABLED", True)
    monkeypatch.setattr("src.settings.MEMORY_EXTRACTION_ENABLED", True)
    called = []
    monkeypatch.setattr(
        "src.storage.control_plane.memory_jobs.enqueue_project_reflection",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    turn = SimpleNamespace(
        export_plan=None,
        department_id=7,
        kb_id=101,
        knowledge_selection_hash="multi-kb-selection",
        session_id=12,
        id="turn-1",
        assistant_message_id=34,
    )
    write_terminal_domains(object(), turn, owner_user_id=9)

    assert called == []
