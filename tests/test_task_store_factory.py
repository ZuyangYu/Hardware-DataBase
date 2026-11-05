"""Task store backend selection."""

from __future__ import annotations

import pytest

import src.settings
from src.core.task_store import TaskStore, get_task_store


def test_default_backend_is_sqlite(monkeypatch):
    monkeypatch.setattr(src.settings, "TASK_STORE_BACKEND", "sqlite")
    store = get_task_store()
    assert isinstance(store, TaskStore)


def test_postgres_backend_requires_dsn(monkeypatch):
    monkeypatch.setattr(src.settings, "TASK_STORE_BACKEND", "postgres")
    monkeypatch.setattr(src.settings, "TASK_DB_URL", "")
    monkeypatch.setattr(src.settings, "CONTROL_PLANE_DATABASE_URL", "")
    with pytest.raises(RuntimeError):
        get_task_store()
