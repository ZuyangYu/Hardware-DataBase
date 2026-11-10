"""Durable directory-import batches (P1).

A batch stages uploaded bytes once, records one row per logical file, and lets
the parse worker submit each file through the normal ingestion pipeline.  The
regular synchronous upload path remains unchanged for small folders; this
service is the reliable background path for large folders and per-file retry.
"""
from __future__ import annotations

import os
import shutil
import uuid

from src.core.auth import AuthService
from src.core.logger import error, log
from src.ingestion.source_groups import SOURCE_GROUPS
from src.pipelines.document_store import PipelineDocumentStore
from src.services.pipeline_directories import (
    PipelineDirectoryService,
    parse_relative_path,
)


def _import_root() -> str:
    import src.settings as settings

    root = os.path.join(settings.STORAGE_DIR, "directory_imports")
    os.makedirs(root, exist_ok=True)
    return root


def create_staging_dir() -> str:
    return os.path.join(_import_root(), uuid.uuid4().hex)


def cleanup_staging_dir(path: str) -> None:
    from pathlib import Path

    root = Path(os.path.realpath(_import_root()))
    target = Path(os.path.realpath(str(path or "")))
    try:
        target.relative_to(root)
    except ValueError:
        return
    if target == root:
        return
    shutil.rmtree(target, ignore_errors=True)


class DirectoryImportService:
    def __init__(
        self,
        *,
        store: PipelineDocumentStore,
        pipeline,
        auth: AuthService | None = None,
    ):
        self.store = store
        self.pipeline = pipeline
        self.auth = auth or AuthService()
        self.directories = PipelineDirectoryService(store=store)

    def create_job(
        self,
        *,
        kb_name: str,
        department_id: str,
        kb_id: int | None,
        directory_id: int,
        created_by: str,
        entries: list[dict],
    ) -> dict:
        if not entries:
            raise ValueError("导入批次不能为空")
        self.directories.get(
            kb_name=kb_name, department_id=department_id, directory_id=directory_id
        )
        normalized: list[dict] = []
        incoming: dict[int, tuple[int, int]] = {}
        for entry in entries:
            group = str(entry.get("source_group") or "")
            if group and group not in SOURCE_GROUPS:
                raise ValueError(f"source_group 不合法: {group}")
            segments, filename = parse_relative_path(str(entry["relative_path"]))
            resolved_id = (
                self.directories.ensure_path(
                    kb_name=kb_name,
                    department_id=department_id,
                    kb_id=kb_id,
                    parent_id=directory_id,
                    segments=segments,
                    created_by=created_by,
                )
                if segments
                else int(directory_id)
            )
            relative = "/".join(
                part
                for part in (
                    self.directories.path_for_directory(
                        kb_name=kb_name,
                        department_id=department_id,
                        directory_id=int(directory_id),
                    ),
                    *segments,
                    filename,
                )
                if part
            )
            count, size = incoming.get(resolved_id, (0, 0))
            incoming[resolved_id] = (
                count + 1,
                size + max(0, int(entry.get("file_size", 0) or 0)),
            )
            normalized.append(
                {
                    "directory_id": resolved_id,
                    "relative_path": relative,
                    "source_group": str(entry.get("source_group") or ""),
                    "staged_path": str(entry.get("staged_path") or ""),
                    "original_name": filename,
                    "file_size": max(0, int(entry.get("file_size", 0) or 0)),
                }
            )
        self.directories.assert_quota(
            kb_name=kb_name, department_id=department_id, incoming=incoming
        )
        return self.store.create_directory_import_job(
            kb_id=kb_id,
            kb_name=kb_name,
            department_id=department_id,
            directory_id=int(directory_id),
            created_by=created_by,
            files=normalized,
        )

    def run_once(self, worker_id: str) -> bool:
        """Claim and process one staged file; return whether work was found."""
        claimed = self.store.claim_directory_import_file(worker_id)
        if claimed is None:
            return False
        job_id = int(claimed["job_id"])
        file_id = int(claimed["id"])
        try:
            if bool(claimed.get("cancel_requested")):
                self.store.finish_directory_import_file(
                    job_id=job_id,
                    file_id=file_id,
                    status="cancelled",
                    error_code="cancelled",
                    error_message="导入批次已取消",
                )
                return True
            user = self.auth.get_user_by_username(str(claimed.get("created_by") or ""))
            if user is None or not getattr(user, "is_active", False):
                self.store.finish_directory_import_file(
                    job_id=job_id,
                    file_id=file_id,
                    status="failed",
                    error_code="permission_denied",
                    error_message="导入创建者账号不可用",
                )
                return True
            from src.services.request_context import build_context_for_user

            ctx = build_context_for_user(
                user, str(claimed["kb_name"]), auth=self.auth
            )
            if not ctx.has_kb_permission(str(claimed["kb_name"]), "write"):
                self.store.finish_directory_import_file(
                    job_id=job_id,
                    file_id=file_id,
                    status="failed",
                    error_code="permission_denied",
                    error_message="创建者已失去该知识库的写权限",
                )
                return True
            if not self._directory_still_exists(claimed):
                self.store.finish_directory_import_file(
                    job_id=job_id,
                    file_id=file_id,
                    status="failed",
                    error_code="directory_not_found",
                    error_message="目标目录已不存在",
                )
                return True
            staged_path = str(claimed.get("staged_path") or "")
            if not staged_path or not os.path.isfile(staged_path):
                self.store.finish_directory_import_file(
                    job_id=job_id,
                    file_id=file_id,
                    status="failed",
                    error_code="raw_archive_failed",
                    error_message="暂存文件不存在，无法导入",
                )
                return True
            result = self.pipeline.upload_files(
                [staged_path],
                str(claimed["kb_name"]),
                ctx=ctx,
                source_group=str(claimed.get("source_group") or "") or None,
                directory_id=int(claimed.get("directory_id") or 0),
                file_metadata=[
                    {
                        "directory_id": int(claimed.get("directory_id") or 0),
                        "relative_path": str(claimed.get("relative_path") or ""),
                        "source_group": str(claimed.get("source_group") or ""),
                    }
                ],
            )
            file_result = result.file_results[0] if result.file_results else None
            status = str(getattr(file_result, "status", "") or "failed")
            if status not in {"success", "reused", "failed", "skipped"}:
                status = "failed"
            self.store.finish_directory_import_file(
                job_id=job_id,
                file_id=file_id,
                status=status,
                record_id=getattr(file_result, "record_id", None),
                error_code=str(getattr(file_result, "error_code", "") or ""),
                error_message=str(getattr(file_result, "message", "") or ""),
            )
            if status in {"success", "reused"}:
                _remove_staged_file(staged_path)
        except Exception as exc:  # noqa: BLE001 - one file must not stop the batch
            error(f"Directory import file {file_id} failed: {exc}")
            self.store.finish_directory_import_file(
                job_id=job_id,
                file_id=file_id,
                status="failed",
                error_code="parse_submission_failed",
                error_message=str(exc),
            )
        job = self.store.get_directory_import_job(job_id, str(claimed["kb_name"]), claimed["department_id"])
        if job and job["status"] in {"completed", "cancelled", "failed", "partial"}:
            self.cleanup_job_staging(job_id, str(claimed["kb_name"]), claimed["department_id"])
        log(
            f"Directory import job {job_id} file {file_id} processed "
            f"({(job or {}).get('status', '')})"
        )
        return True

    def _directory_still_exists(self, claimed: dict) -> bool:
        try:
            self.directories.get(
                kb_name=str(claimed["kb_name"]),
                department_id=str(claimed["department_id"]),
                directory_id=int(claimed.get("directory_id") or 0),
            )
            return True
        except LookupError:
            return False

    def cleanup_job_staging(self, job_id: int, kb_name: str, department_id: str) -> None:
        files = self.store.list_directory_import_files(job_id, kb_name, department_id, limit=5000)
        removed_roots: set[str] = set()
        remaining = False
        for row in files:
            staged = str(row.get("staged_path") or "")
            if not staged:
                continue
            if str(row.get("status") or "") in {"success", "reused", "cancelled"}:
                _remove_staged_file(staged)
                removed_roots.add(os.path.dirname(staged))
            else:
                # Failed/skipped rows keep their staged bytes so the user can
                # retry the file without uploading the folder again.
                remaining = True
        if not remaining:
            for root in removed_roots:
                cleanup_staging_dir(root)


def _remove_staged_file(path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


__all__ = [
    "DirectoryImportService",
    "cleanup_staging_dir",
    "create_staging_dir",
]
