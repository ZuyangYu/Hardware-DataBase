"""Read-only reconciliation report for pipeline directories and archives."""
from __future__ import annotations

import os

from src.pipelines.document_store import PipelineDocumentStore
from src.services.document_archive import DocumentArchiveManager


def pipeline_schema_report(store: PipelineDocumentStore | None = None) -> dict:
    """Post-migration verification: counts, root backfill and duplicate paths."""
    from contextlib import closing

    store = store or PipelineDocumentStore()
    with closing(store._connect()) as conn:
        documents = int(conn.execute("SELECT COUNT(*) FROM pipeline_documents").fetchone()[0])
        directories = int(conn.execute("SELECT COUNT(*) FROM pipeline_directories").fetchone()[0])
        legacy = int(
            conn.execute(
                "SELECT COUNT(*) FROM pipeline_documents WHERE COALESCE(directory_id, 0) = 0"
            ).fetchone()[0]
        )
        duplicates = [
            dict(row)
            for row in conn.execute(
                """
                SELECT kb_name, department_id, COALESCE(directory_id, 0) AS directory_id,
                       document_name, COUNT(*) AS count
                FROM pipeline_documents
                GROUP BY kb_name, department_id, COALESCE(directory_id, 0), document_name
                HAVING COUNT(*) > 1
                LIMIT 100
                """
            ).fetchall()
        ]
        orphan_directories = [
            dict(row)
            for row in conn.execute(
                """
                SELECT file.id, file.kb_name, file.department_id, file.directory_id
                FROM pipeline_documents AS file
                LEFT JOIN pipeline_directories AS directory
                  ON directory.id = file.directory_id
                 AND directory.kb_name = file.kb_name
                 AND directory.department_id = file.department_id
                WHERE COALESCE(file.directory_id, 0) <> 0
                  AND directory.id IS NULL
                LIMIT 100
                """
            ).fetchall()
        ]
    return {
        "documents": documents,
        "directories": directories,
        "root_backfilled_documents": legacy,
        "duplicate_paths": duplicates,
        "orphan_directories": orphan_directories,
        "ok": not duplicates and not orphan_directories,
    }


def reconcile_pipeline(
    *,
    kb_name: str,
    department_id: str,
    deep: bool = True,
    limit: int = 200,
    store: PipelineDocumentStore | None = None,
    archive: DocumentArchiveManager | None = None,
) -> dict:
    store = store or PipelineDocumentStore()
    archive = archive or DocumentArchiveManager()
    directories = [
        row
        for row in store.list_documents(kb_name, department_id)
    ]
    directory_rows: list[dict] = []
    pending = [0]
    while pending:
        parent_id = pending.pop()
        children = store.list_directories(kb_name, department_id, parent_id=parent_id)
        directory_rows.extend(children)
        pending.extend(int(child["id"]) for child in children)
    directory_ids = {int(row["id"]) for row in directory_rows}

    missing_archives: list[dict] = []
    missing_directories: list[dict] = []
    referenced_paths: set[str] = set()
    for record in directories:
        directory_id = int(getattr(record, "directory_id", 0) or 0)
        if directory_id and directory_id not in directory_ids:
            missing_directories.append(
                {"record_id": record.id, "directory_id": directory_id, "name": record.document_name}
            )
        path = archive.resolve_record_path(record)
        if record.local_path:
            referenced_paths.add(os.path.normpath(str(record.local_path)))
        if not os.path.exists(path):
            missing_archives.append(
                {"record_id": record.id, "directory_id": directory_id, "name": record.document_name}
            )

    orphan_archives: list[str] = []
    if deep:
        kb_root = archive.kb_path(kb_name, department_id=department_id)
        if os.path.isdir(kb_root):
            for root, _dirs, files in os.walk(kb_root):
                for name in files:
                    full = os.path.join(root, name)
                    relative = os.path.relpath(full, kb_root)
                    if os.path.normpath(relative) not in referenced_paths:
                        orphan_archives.append(relative)
                        if len(orphan_archives) >= max(1, int(limit)):
                            break
                if len(orphan_archives) >= max(1, int(limit)):
                    break

    return {
        "kb_name": kb_name,
        "department_id": department_id,
        "documents": len(directories),
        "directories": len(directory_rows),
        "missing_archives": missing_archives[: max(1, int(limit))],
        "missing_directories": missing_directories[: max(1, int(limit))],
        "orphan_archives": orphan_archives,
        "ok": not missing_archives and not missing_directories and not orphan_archives,
    }


__all__ = ["pipeline_schema_report", "reconcile_pipeline"]
