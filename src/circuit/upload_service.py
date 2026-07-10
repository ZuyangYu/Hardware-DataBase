from __future__ import annotations

import os
import shutil
import time
from typing import Callable

import config.settings
from src.ingestion.source_groups import (
    NETLIST_GROUP,
    SCHEMATIC_GROUP,
    classify_source_group,
    expand_source_group_for_file,
    safe_source_group,
)
from src.pipelines.document_rag.schemas import IngestResult


CIRCUIT_SOURCE_GROUPS = {NETLIST_GROUP, SCHEMATIC_GROUP}


def is_circuit_source_group(source_group: str | None) -> bool:
    return source_group in CIRCUIT_SOURCE_GROUPS


def resolve_upload_source_group(source_group: str | None, filename: str) -> str:
    """Resolve the concrete upload group for a file before backend routing."""

    if source_group:
        return safe_source_group(expand_source_group_for_file(source_group, filename))
    return safe_source_group(classify_source_group(filename).group)


def would_route_to_circuit(source_group: str | None, filename: str) -> bool:
    """Return True when a file belongs to the dedicated circuit pipeline."""

    return is_circuit_source_group(resolve_upload_source_group(source_group, filename))


def ingest_circuit_files(
    kb_name: str,
    files: list[tuple[str, str]],
    progress_callback: Callable[[int, str], None] | None = None,
) -> IngestResult:
    """Archive and parse circuit files without involving the active RAG backend."""

    messages: list[str] = []
    success_count = 0
    failed_count = 0
    archive_root = os.path.join(config.settings.STORAGE_DIR, "circuit_uploads", kb_name)
    os.makedirs(archive_root, exist_ok=True)

    for index, (source_path, source_group) in enumerate(files, start=1):
        filename = os.path.basename(source_path)
        try:
            group_dir = os.path.join(archive_root, source_group)
            os.makedirs(group_dir, exist_ok=True)
            archived_name = filename
            archived_path = os.path.join(group_dir, archived_name)
            if os.path.exists(archived_path):
                base, ext = os.path.splitext(filename)
                archived_name = f"{base}_{int(time.time())}{ext}"
                archived_path = os.path.join(group_dir, archived_name)
            shutil.copy2(source_path, archived_path)

            if progress_callback:
                progress_callback(10, f"{filename}: 已归档到本地电路解析目录")

            messages.append(f"[成功] 本地电路文件已归档: {archived_name}")
            success_count += 1
            if progress_callback:
                progress_callback(100, f"{filename}: 电路文件归档完成（{index}/{len(files)}）")
        except Exception as exc:
            failed_count += 1
            messages.append(f"[失败] 电路解析失败: {filename}: {exc}")

    return IngestResult(
        success_count=success_count,
        total_count=len(files),
        failed_count=failed_count,
        messages=messages,
        backend="circuit-local",
    )
