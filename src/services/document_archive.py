import os
import re
import shutil
import time
import uuid
from typing import Any

import src.settings
from src.core.logger import error
from src.ingestion.container_inspector import inspect_container_file
from src.ingestion.kb_paths import safe_child_path, validate_kb_name
from src.ingestion.source_groups import safe_source_group


class DocumentArchiveManager:
    """Manage archived source files for document processing pipelines."""

    def archive_root(self, create: bool = False) -> str:
        root = os.path.abspath(src.settings.PIPELINE_ARCHIVE_ROOT)
        if create:
            os.makedirs(root, exist_ok=True)
        return root

    def department_path(self, department_id: str | int | None, create: bool = False) -> str:
        department_part = _safe_scope_part(department_id or "unknown")
        return safe_child_path(
            self.archive_root(create=create),
            "departments",
            department_part,
            create=create,
        )

    def kb_path(
        self,
        kb_name: str,
        create: bool = False,
        department_id: str | int | None = None,
    ) -> str:
        if department_id is None:
            return safe_child_path(self.archive_root(create=create), validate_kb_name(kb_name), create=create)
        return safe_child_path(
            self.department_path(department_id, create=create),
            "kbs",
            validate_kb_name(kb_name),
            create=create,
        )

    def resolve_record_path(self, record: Any) -> str:
        path = record.local_path or os.path.join(record.source_group, record.document_name)
        if os.path.isabs(path):
            return path
        department_id = getattr(record, "department_id", None)
        if department_id not in (None, ""):
            archive_path = os.path.join(self.kb_path(record.kb_name, department_id=department_id), path)
            if os.path.exists(archive_path):
                return archive_path
        legacy_archive_path = os.path.join(self.kb_path(record.kb_name), path)
        if os.path.exists(legacy_archive_path):
            return legacy_archive_path
        return legacy_archive_path

    def archive_source_file(
        self,
        kb_name: str,
        file_path: str,
        source_group: str | None,
        department_id: str | int | None = None,
    ) -> tuple[str, str, str]:
        archived_group = safe_source_group(source_group)
        filename = os.path.basename(file_path)
        target_dir = os.path.join(
            self.kb_path(kb_name, create=True, department_id=department_id),
            archived_group,
        )
        os.makedirs(target_dir, exist_ok=True)
        base, ext = os.path.splitext(filename)
        for attempt in range(20):
            candidate_name = filename
            if attempt:
                candidate_name = f"{base}_{time.time_ns()}_{uuid.uuid4().hex[:8]}{ext}"
            target_path = os.path.join(target_dir, candidate_name)
            try:
                with open(file_path, "rb") as source, open(target_path, "xb") as target:
                    shutil.copyfileobj(source, target)
                shutil.copystat(file_path, target_path, follow_symlinks=True)
                return target_path, candidate_name, archived_group
            except FileExistsError:
                continue
            except Exception:
                if os.path.exists(target_path):
                    try:
                        os.remove(target_path)
                    except OSError:
                        pass
                raise
        raise FileExistsError(f"Could not allocate unique archive path for {filename}")

    def remove_record_archive(self, record: Any):
        path = self.resolve_record_path(record)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            error(f"Failed to remove local document archive {path}: {exc}")

    def record_archive_exists(self, record: Any) -> bool:
        return os.path.exists(self.resolve_record_path(record))

    def inspect_record_archive(self, record: Any) -> dict:
        path = self.resolve_record_path(record)
        if not os.path.exists(path):
            return {}
        return inspect_container_file(path).to_metadata()


def _safe_scope_part(value: str | int | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip("._-")
    return cleaned[:80] or "unknown"
