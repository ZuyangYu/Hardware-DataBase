import hashlib
import importlib.util
from pathlib import Path
import sqlite3


def migration():
    path = Path(__file__).parents[1] / 'scripts/production_hardening/migrate_multi_kb_conflict_review.py'
    spec = importlib.util.spec_from_file_location('conflict_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_conflict_migration_is_read_only_by_default_and_apply_is_idempotent(tmp_path, capsys):
    path = tmp_path / 'authoring.db'
    sqlite3.connect(path).close()
    before = path.read_bytes()
    tool = migration()
    assert tool.main(['--db', str(path)]) == 1
    assert path.read_bytes() == before
    assert tool.main(['--db', str(path), '--apply']) == 0
    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
    assert tool.main(['--db', str(path), '--verify']) == 0
    assert tool.main(['--db', str(path), '--apply']) == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == fingerprint
    assert '"ready": true' in capsys.readouterr().out


def test_conflict_migration_refuses_duplicate_legacy_cases_without_changing_history(tmp_path, capsys):
    path = tmp_path / 'authoring.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''CREATE TABLE authoring_conflict_cases (
            conflict_id TEXT PRIMARY KEY, tenant_id TEXT, work_order_id TEXT,
            fingerprint TEXT, payload_json TEXT
        );''')
        for case_id in ('a', 'b'):
            conn.execute('INSERT INTO authoring_conflict_cases VALUES (?, ?, ?, ?, ?)', (case_id, 't', 'w', 'f', '{"snapshot_hash":"s"}'))
    before = path.read_bytes()
    assert migration().main(['--db', str(path), '--apply']) == 1
    assert path.read_bytes() == before
    assert 'duplicate_case_groups' in capsys.readouterr().out
