"""Inspect/apply/verify R3 conflict tables. Default inspection never writes."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.document_authoring.conflicts.store import ConflictDecisionStore  # noqa: E402

REQUIRED = {
    'authoring_conflict_cases': {
        'conflict_id', 'tenant_id', 'work_order_id', 'fingerprint', 'source_snapshot_hash',
        'evidence_set_hash', 'row_version', 'status', 'payload_json', 'created_at', 'updated_at',
    },
    'authoring_conflict_decisions': {
        'decision_id', 'conflict_id', 'tenant_id', 'actor_id', 'idempotency_key',
        'request_fingerprint', 'payload_json', 'created_at',
    },
}


def inspect(conn, backend: str) -> dict:
    missing = {}
    actual = {}
    for table, expected in REQUIRED.items():
        if backend == 'postgres':
            columns = {row['column_name'] for row in conn.execute(
                'SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = ?', (table,),
            ).fetchall()}
        else:
            columns = {row['name'] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}
        actual[table] = columns
        if expected - columns:
            missing[table] = sorted(expected - columns)
    duplicates = 0
    columns = actual['authoring_conflict_cases']
    if {'tenant_id', 'work_order_id', 'fingerprint', 'payload_json'} <= columns:
        snapshot = "COALESCE(json_extract(payload_json, '$.snapshot_hash'), '')"
        if 'source_snapshot_hash' in columns:
            snapshot = f"COALESCE(NULLIF(source_snapshot_hash, ''), {snapshot})"
        groups = conn.execute(f'''SELECT COUNT(*) AS n FROM (
            SELECT tenant_id, work_order_id, fingerprint, {snapshot} AS snapshot
            FROM authoring_conflict_cases GROUP BY tenant_id, work_order_id, fingerprint, {snapshot}
            HAVING COUNT(*) > 1
        ) AS duplicates''').fetchone()
        duplicates = int(groups['n'])
    if backend == 'postgres':
        indexes = {row['indexname'] for row in conn.execute(
            'SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND tablename = ?',
            ('authoring_conflict_cases',),
        ).fetchall()}
    else:
        indexes = {row['name'] for row in conn.execute('PRAGMA index_list(authoring_conflict_cases)').fetchall()}
    indexed = 'idx_authoring_conflicts_fingerprint' in indexes
    return {'ready': not missing and not duplicates and indexed, 'missing_columns': missing,
            'duplicate_case_groups': duplicates, 'fingerprint_index': indexed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('sqlite', 'postgres'), default='sqlite')
    parser.add_argument('--db', default='')
    parser.add_argument('--dsn', default='')
    modes = parser.add_mutually_exclusive_group()
    for mode in ('check', 'apply', 'verify'):
        modes.add_argument(f'--{mode}', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.backend == 'sqlite':
            path = Path(args.db)
            if not args.db or not path.is_file():
                raise ValueError('sqlite_database_required')

            def connect():
                conn = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)
                conn.row_factory = sqlite3.Row
                return conn
        else:
            from src.storage.domain_pg import open_pg_connection

            dsn = args.dsn or os.getenv('AUTHORING_STORE_DATABASE_URL') or os.getenv('CONTROL_PLANE_DATABASE_URL', '')
            if not dsn:
                raise ValueError('postgres_dsn_required')

            def connect():
                conn = open_pg_connection(dsn)
                conn.execute('BEGIN READ ONLY')
                return conn
        with closing(connect()) as conn:
            report = inspect(conn, args.backend)
        if args.apply and not report['duplicate_case_groups']:
            if args.backend == 'sqlite':
                ConflictDecisionStore(str(path))
            else:
                backend = SimpleNamespace(_backend='postgres', db_path=None, _connect=lambda: open_pg_connection(dsn))
                ConflictDecisionStore(backend_store=backend)
            with closing(connect()) as conn:
                report = inspect(conn, args.backend)
        print(json.dumps({'backend': args.backend, 'mode': 'apply' if args.apply else 'verify' if args.verify else 'check', **report}, sort_keys=True))
        return 0 if report['ready'] else 1
    except Exception as exc:
        # Driver errors may include connection credentials. Keep CLI output bounded.
        print(json.dumps({'ready': False, 'error': type(exc).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
