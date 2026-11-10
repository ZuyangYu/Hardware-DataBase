"""Knowledge-base directory tree service.

Directories are logical Pipeline-domain records.  The root is the stable
sentinel ``directory_id=0``; physical archives use directory ids and therefore
do not need to move when a user renames a directory.
"""
from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterable
from importlib import import_module
from typing import Any

ROOT_DIRECTORY_ID = 0
MAX_DIRECTORY_DEPTH = 32
MAX_DIRECTORY_NAME_LENGTH = 255
MAX_PATH_SEGMENTS = 32
DIRECTORY_MAX_FILES = max(0, int(os.getenv("HDB_DIRECTORY_MAX_FILES", "0") or 0))
DIRECTORY_MAX_BYTES = max(0, int(os.getenv("HDB_DIRECTORY_MAX_BYTES", "0") or 0))
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class DirectoryValidationError(ValueError):
    """Raised when a logical directory name/path is unsafe or invalid."""


def normalize_directory_name(value: str) -> tuple[str, str]:
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not raw:
        raise DirectoryValidationError("目录名不能为空")
    if raw in {".", ".."}:
        raise DirectoryValidationError("目录名不能为 . 或 ..")
    if raw.endswith((".", " ")) or raw.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise DirectoryValidationError("目录名使用了平台保留名称")
    if len(raw) > MAX_DIRECTORY_NAME_LENGTH:
        raise DirectoryValidationError("目录名过长")
    if any(ord(char) < 32 for char in raw):
        raise DirectoryValidationError("目录名不能包含控制字符")
    if "/" in raw or "\\" in raw or "\x00" in raw:
        raise DirectoryValidationError("目录名不能包含路径分隔符或 NUL")
    return raw, raw.casefold()


def validate_relative_segments(segments: Iterable[str]) -> list[str]:
    values = [str(segment or "") for segment in segments]
    if not values or len(values) > MAX_PATH_SEGMENTS:
        raise DirectoryValidationError("文件夹层级超出限制")
    normalized: list[str] = []
    for value in values:
        name, _normalized_name = normalize_directory_name(value)
        normalized.append(name)
    return normalized


def parse_relative_path(value: str) -> tuple[list[str], str]:
    """Validate a browser-provided relative path and split directories/name."""
    raw = unicodedata.normalize("NFKC", str(value or ""))
    if not raw or raw.startswith(('/', '\\')) or re.match(r"^[A-Za-z]:", raw):
        raise DirectoryValidationError("文件相对路径必须是相对路径")
    if "\\" in raw or "\x00" in raw:
        raise DirectoryValidationError("文件相对路径包含非法分隔符")
    if any(ord(char) < 32 for char in raw):
        raise DirectoryValidationError("文件相对路径不能包含控制字符")
    segments = raw.split("/")
    if any(not segment or segment in {".", ".."} for segment in segments):
        raise DirectoryValidationError("文件相对路径包含非法目录段")
    if len(raw) > MAX_DIRECTORY_NAME_LENGTH * MAX_PATH_SEGMENTS:
        raise DirectoryValidationError("文件相对路径过长")
    clean_segments = validate_relative_segments(segments)
    if len(clean_segments) < 1:
        raise DirectoryValidationError("文件名不能为空")
    return clean_segments[:-1], clean_segments[-1]


class PipelineDirectoryService:
    def __init__(self, store: Any | None = None):
        if store is None:
            store = import_module("src.pipelines.document_store").PipelineDocumentStore()
        self.store = store

    @staticmethod
    def _scope_department(department_id: str | int | None) -> str:
        if department_id in (None, ""):
            raise DirectoryValidationError("department_id is required")
        return str(department_id)

    def get(self, *, kb_name: str, department_id: str | int, directory_id: int) -> dict:
        department_id = self._scope_department(department_id)
        directory = self._get_raw(
            kb_name=kb_name,
            department_id=department_id,
            directory_id=directory_id,
        )
        if directory is None:
            raise LookupError("目录不存在")
        return self._decorate(directory, kb_name, department_id)

    def _get_raw(self, *, kb_name: str, department_id: str, directory_id: int) -> dict | None:
        return self.store.get_directory(int(directory_id), kb_name, department_id)

    def list_children(self, *, kb_name: str, department_id: str | int, parent_id: int = 0) -> list[dict]:
        department_id = self._scope_department(department_id)
        parent_id = int(parent_id or 0)
        if parent_id:
            self.get(kb_name=kb_name, department_id=department_id, directory_id=parent_id)
        return [
            self._decorate(row, kb_name, department_id)
            for row in self.store.list_directories(kb_name, department_id, parent_id=parent_id)
        ]

    def list_all(self, *, kb_name: str, department_id: str | int) -> list[dict]:
        department_id = self._scope_department(department_id)
        # The store returns only child pages publicly, so walk the tree.  This
        # remains bounded by MAX_DIRECTORY_DEPTH and avoids backend-specific
        # recursive SQL in the request layer.
        result: list[dict] = []
        pending = [ROOT_DIRECTORY_ID]
        while pending:
            parent_id = pending.pop()
            children = self.store.list_directories(kb_name, department_id, parent_id=parent_id)
            result.extend(self._decorate(row, kb_name, department_id) for row in children)
            pending.extend(int(row["id"]) for row in children)
        return result

    def descendant_ids(
        self,
        *,
        kb_name: str,
        department_id: str | int,
        directory_id: int,
    ) -> set[int]:
        target = int(directory_id or 0)
        if target == ROOT_DIRECTORY_ID:
            return {int(row["id"]) for row in self.list_all(kb_name=kb_name, department_id=department_id)} | {0}
        self.get(kb_name=kb_name, department_id=department_id, directory_id=target)
        descendants = {target}
        pending = [target]
        while pending:
            parent_id = pending.pop()
            children = self.store.list_directories(
                kb_name, self._scope_department(department_id), parent_id=parent_id
            )
            for child in children:
                child_id = int(child["id"])
                if child_id not in descendants:
                    descendants.add(child_id)
                    pending.append(child_id)
        return descendants

    def create(
        self,
        *,
        kb_name: str,
        department_id: str | int,
        kb_id: int | None,
        parent_id: int,
        name: str,
        created_by: str,
    ) -> dict:
        department_id = self._scope_department(department_id)
        parent_id = int(parent_id or 0)
        parent = self.get(kb_name=kb_name, department_id=department_id, directory_id=parent_id)
        if parent_id and parent["depth"] >= MAX_DIRECTORY_DEPTH:
            raise DirectoryValidationError("目录层级超出限制")
        clean_name, normalized_name = normalize_directory_name(name)
        if any(
            row["normalized_name"] == normalized_name
            for row in self.store.list_directories(kb_name, department_id, parent_id=parent_id)
        ):
            raise DirectoryValidationError("同级目录已存在")
        try:
            directory = self.store.create_directory(
                kb_id=kb_id,
                kb_name=kb_name,
                department_id=department_id,
                parent_id=parent_id,
                name=clean_name,
                normalized_name=normalized_name,
                depth=int(parent["depth"]) + 1,
                created_by=str(created_by or ""),
            )
        except Exception as exc:
            # PostgreSQL/SQLite can race after the preflight check; expose a
            # product error instead of leaking a backend-specific constraint.
            if "UNIQUE" in str(exc).upper() or "duplicate" in str(exc).lower():
                raise DirectoryValidationError("同级目录已存在") from exc
            raise
        return self._decorate(directory, kb_name, department_id)

    def ensure_path(
        self,
        *,
        kb_name: str,
        department_id: str | int,
        kb_id: int | None,
        parent_id: int,
        segments: Iterable[str],
        created_by: str,
    ) -> int:
        current_id = int(parent_id or 0)
        for segment in validate_relative_segments(segments):
            existing = next(
                (
                    row
                    for row in self.store.list_directories(
                        kb_name, department_id, parent_id=current_id
                    )
                    if row["normalized_name"] == normalize_directory_name(segment)[1]
                ),
                None,
            )
            if existing is None:
                existing = self.create(
                    kb_name=kb_name,
                    department_id=department_id,
                    kb_id=kb_id,
                    parent_id=current_id,
                    name=segment,
                    created_by=created_by,
                )
            current_id = int(existing["id"])
        return current_id

    def rename(
        self,
        *,
        kb_name: str,
        department_id: str | int,
        directory_id: int,
        name: str,
    ) -> dict:
        department_id = self._scope_department(department_id)
        directory = self.get(kb_name=kb_name, department_id=department_id, directory_id=directory_id)
        if int(directory["id"]) == ROOT_DIRECTORY_ID:
            raise DirectoryValidationError("根目录不能重命名")
        clean_name, normalized_name = normalize_directory_name(name)
        siblings = self.store.list_directories(
            kb_name, department_id, parent_id=int(directory["parent_id"])
        )
        if any(
            int(row["id"]) != int(directory_id)
            and row["normalized_name"] == normalized_name
            for row in siblings
        ):
            raise DirectoryValidationError("同级目录已存在")
        try:
            updated = self.store.rename_directory(
                directory_id,
                kb_name,
                department_id,
                name=clean_name,
                normalized_name=normalized_name,
            )
        except Exception as exc:
            if "UNIQUE" in str(exc).upper() or "duplicate" in str(exc).lower():
                raise DirectoryValidationError("同级目录已存在") from exc
            raise
        if updated is None:
            raise LookupError("目录不存在")
        return self._decorate(updated, kb_name, department_id)

    def move(
        self,
        *,
        kb_name: str,
        department_id: str | int,
        directory_id: int,
        parent_id: int,
    ) -> dict:
        department_id = self._scope_department(department_id)
        directory = self.get(kb_name=kb_name, department_id=department_id, directory_id=directory_id)
        parent_id = int(parent_id or 0)
        parent = self.get(kb_name=kb_name, department_id=department_id, directory_id=parent_id)
        descendants = self.descendant_ids(
            kb_name=kb_name, department_id=department_id, directory_id=int(directory_id)
        )
        if parent_id in descendants:
            raise DirectoryValidationError("不能移动目录到自身或其子目录")
        siblings = self.store.list_directories(kb_name, department_id, parent_id=parent_id)
        if any(
            int(row["id"]) != int(directory_id)
            and row["normalized_name"] == directory["normalized_name"]
            for row in siblings
        ):
            raise DirectoryValidationError("同级目录已存在")
        new_depth = int(parent["depth"]) + 1
        subtree = self._subtree_rows(kb_name, department_id, int(directory_id))
        depth_delta = new_depth - int(directory["depth"])
        if any(int(row["depth"]) + depth_delta > MAX_DIRECTORY_DEPTH for row in subtree):
            raise DirectoryValidationError("目录层级超出限制")
        updated = self.store.move_directory(
            int(directory_id), kb_name, department_id, parent_id=parent_id, depth=new_depth
        )
        for row in subtree:
            if int(row["id"]) != int(directory_id):
                self.store.update_directory_depth(
                    int(row["id"]), kb_name, department_id,
                    depth=int(row["depth"]) + depth_delta,
                )
        if updated is None:
            raise LookupError("目录不存在")
        return self._decorate(updated, kb_name, department_id)

    def _subtree_rows(self, kb_name: str, department_id: str, directory_id: int) -> list[dict]:
        rows: list[dict] = []
        pending = [int(directory_id)]
        while pending:
            current = pending.pop()
            row = self.store.get_directory(current, kb_name, department_id)
            if row is None:
                continue
            rows.append(row)
            children = self.store.list_directories(kb_name, department_id, parent_id=current)
            pending.extend(int(child["id"]) for child in children)
        return rows

    def quota_snapshot(
        self,
        *,
        kb_name: str,
        department_id: str | int,
        directory_id: int,
    ) -> dict[str, int]:
        records = self.store.list_documents(
            kb_name, self._scope_department(department_id), directory_id=int(directory_id)
        )
        return {
            "files": len(records),
            "bytes": sum(max(0, int(getattr(record, "file_size", 0) or 0)) for record in records),
            "max_files": DIRECTORY_MAX_FILES,
            "max_bytes": DIRECTORY_MAX_BYTES,
        }

    def assert_quota(
        self,
        *,
        kb_name: str,
        department_id: str | int,
        incoming: dict[int, tuple[int, int]],
    ) -> None:
        if DIRECTORY_MAX_FILES <= 0 and DIRECTORY_MAX_BYTES <= 0:
            return
        for directory_id, (file_count, byte_count) in incoming.items():
            current = self.quota_snapshot(
                kb_name=kb_name, department_id=department_id, directory_id=directory_id
            )
            if DIRECTORY_MAX_FILES and current["files"] + int(file_count) > DIRECTORY_MAX_FILES:
                raise DirectoryValidationError(
                    f"目录文件数超过配额（最多 {DIRECTORY_MAX_FILES} 个）"
                )
            if DIRECTORY_MAX_BYTES and current["bytes"] + int(byte_count) > DIRECTORY_MAX_BYTES:
                raise DirectoryValidationError(
                    f"目录容量超过配额（最多 {DIRECTORY_MAX_BYTES} 字节）"
                )

    def delete_empty(self, *, kb_name: str, department_id: str | int, directory_id: int) -> None:
        department_id = self._scope_department(department_id)
        directory = self.get(kb_name=kb_name, department_id=department_id, directory_id=directory_id)
        if int(directory["id"]) == ROOT_DIRECTORY_ID:
            raise DirectoryValidationError("根目录不能删除")
        if self.store.directory_file_count(directory_id, kb_name, department_id):
            raise DirectoryValidationError("目录不为空，请先删除其中的文件")
        if self.store.directory_child_count(directory_id, kb_name, department_id):
            raise DirectoryValidationError("目录包含子目录，请先删除子目录")
        if not self.store.delete_directory(directory_id, kb_name, department_id):
            raise LookupError("目录不存在")

    def path_for_directory(self, *, kb_name: str, department_id: str | int, directory_id: int) -> str:
        department_id = self._scope_department(department_id)
        current_id = int(directory_id or 0)
        segments: list[str] = []
        visited: set[int] = set()
        while current_id:
            if current_id in visited:
                raise RuntimeError("目录树存在循环")
            visited.add(current_id)
            if len(visited) > MAX_DIRECTORY_DEPTH:
                raise RuntimeError("目录树深度异常")
            row = self._get_raw(
                kb_name=kb_name,
                department_id=department_id,
                directory_id=current_id,
            )
            if row is None:
                raise LookupError("目录不存在")
            segments.append(str(row["name"]))
            current_id = int(row["parent_id"] or 0)
        return "/".join(reversed(segments))

    def _decorate(self, directory: dict, kb_name: str, department_id: str) -> dict:
        result = dict(directory)
        result["path"] = self.path_for_directory(
            kb_name=kb_name,
            department_id=department_id,
            directory_id=int(directory["id"]),
        )
        result["child_directory_count"] = self.store.directory_child_count(
            int(directory["id"]), kb_name, department_id
        )
        result["file_count"] = self.store.directory_file_count(
            int(directory["id"]), kb_name, department_id
        )
        return result


def delete_kb_directory_tree(pipeline: Any, kb_name: str, directory_id: int, *, ctx: Any = None) -> dict:
    """AppPipeline-friendly wrapper; keeps the orchestration method thin."""
    store = getattr(getattr(pipeline, "backend", None), "store", None)
    if store is None:
        raise RuntimeError("pipeline document store unavailable")
    department_id = str(
        (ctx.metadata.get("resource_department_id") or ctx.metadata.get("department_id"))
        if ctx is not None else ""
    )
    return delete_directory_tree(
        store=store,
        documents=pipeline.documents,
        kb_name=kb_name,
        department_id=department_id,
        directory_id=int(directory_id),
        ctx=ctx,
    )


def delete_directory_tree(
    *,
    store: Any,
    documents: Any,
    kb_name: str,
    department_id: str,
    directory_id: int,
    ctx: Any = None,
) -> dict:
    """Delete every file and directory below ``directory_id`` with a failure list."""
    service = PipelineDirectoryService(store=store)
    directory_ids = service.descendant_ids(
        kb_name=kb_name, department_id=department_id, directory_id=int(directory_id)
    )
    records = [
        record
        for current_id in sorted(directory_ids)
        if current_id
        for record in store.list_documents(kb_name, department_id, directory_id=current_id)
    ]
    errors: list[str] = []
    deleted_files = 0
    for record in records:
        try:
            result = documents.delete_document(f"ragflow:{record.id}", kb_name, ctx=ctx)
            if not getattr(result, "ok", False):
                errors.append(f"{record.document_name}: {getattr(result, 'message', result)}")
            else:
                deleted_files += 1
        except Exception as exc:  # noqa: BLE001 - return a failure list
            errors.append(f"{record.document_name}: {exc}")
    if errors:
        return {
            "ok": False,
            "message": "递归删除部分失败，目录未自动删除",
            "deleted_files": deleted_files,
            "deleted_directories": 0,
            "errors": errors,
        }
    rows = [
        row
        for current_id in directory_ids
        if (row := store.get_directory(current_id, kb_name, department_id)) is not None
    ]
    deleted_directories = sum(
        1
        for row in sorted(rows, key=lambda item: int(item["depth"]), reverse=True)
        if store.delete_directory(int(row["id"]), kb_name, department_id)
    )
    return {
        "ok": True,
        "message": "目录及其内容已递归删除",
        "deleted_files": deleted_files,
        "deleted_directories": deleted_directories,
        "errors": [],
    }


def document_relative_path(store: Any, record: Any) -> str:
    """Return a record's derived logical path with a legacy-safe fallback."""
    document_name = str(getattr(record, "document_name", "") or "")
    original_file_name = str(getattr(record, "original_file_name", "") or "")
    directory_id = int(getattr(record, "directory_id", 0) or 0)
    if not directory_id:
        # Older pipeline records kept the user-visible filename only in
        # ``original_file_name`` while ``document_name`` could be an internal
        # display alias.  Preserve that root-level behavior; directory-aware
        # records derive their path from the directory tree below.
        return original_file_name or document_name
    try:
        directory_path = PipelineDirectoryService(store=store).path_for_directory(
            kb_name=str(getattr(record, "kb_name", "") or ""),
            department_id=str(getattr(record, "department_id", "") or ""),
            directory_id=directory_id,
        )
        return "/".join(part for part in (directory_path, document_name or original_file_name) if part)
    except Exception:  # pragma: no cover - legacy/corrupt directory fallback
        return document_name or original_file_name
