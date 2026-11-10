from __future__ import annotations

import sqlite3

from src.pipelines.document_store import PipelineDocumentStore


def _write_legacy_database(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE pipeline_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kb_id INTEGER NOT NULL DEFAULT 0,
                kb_name TEXT NOT NULL,
                document_name TEXT NOT NULL,
                original_file_name TEXT NOT NULL DEFAULT '',
                dataset_kind TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                source_group TEXT NOT NULL DEFAULT '',
                department_id TEXT NOT NULL DEFAULT '',
                uploaded_by TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'uploaded',
                content_kind TEXT NOT NULL DEFAULT 'document_text',
                processor_kind TEXT NOT NULL DEFAULT 'ragflow',
                shared_read_only INTEGER NOT NULL DEFAULT 0,
                local_path TEXT NOT NULL DEFAULT '',
                file_size INTEGER NOT NULL DEFAULT 0,
                content_hash TEXT NOT NULL DEFAULT '',
                upload_status TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                ragflow_error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            INSERT INTO pipeline_documents (
                kb_name, document_name, original_file_name, dataset_kind,
                dataset_id, document_id, source_group, department_id,
                uploaded_by, status, content_kind, processor_kind, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "kb", "legacy.pdf", "legacy.pdf", "design", "dataset",
                "remote-legacy", "文档资料", "dept", "tester", "parsed",
                "document_text", "ragflow", "legacy-hash",
            ),
        )


def test_legacy_rows_are_backfilled_to_root_and_migration_is_repeatable(tmp_path):
    db_path = tmp_path / "legacy.db"
    _write_legacy_database(db_path)

    first = PipelineDocumentStore(db_path=str(db_path))
    record = first.get_document("kb", "legacy.pdf", "design", department_id="dept")
    assert record.directory_id == 0
    assert first.get_directory(0, "kb", "dept")["id"] == 0
    del first

    second = PipelineDocumentStore(db_path=str(db_path))
    record_again = second.get_document("kb", "legacy.pdf", "design", department_id="dept")
    assert record_again.id == record.id
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_documents)")}
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pipeline_documents'"
        ).fetchone()[0]
        directory_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_directories'"
        ).fetchall()
    assert "directory_id" in columns
    assert "UNIQUE(kb_name, department_id, directory_id, document_name)" in table_sql
    assert directory_tables == [("pipeline_directories",)]
