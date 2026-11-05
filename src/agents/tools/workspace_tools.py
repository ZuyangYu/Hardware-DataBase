"""Agent-facing workspace tools.

These are the general-agent "hands": list, read and write files inside the
task workspace.  They are read/write confined to the workspace root and do
not execute anything (shell arrives with the sandbox phase).
"""

from __future__ import annotations

import json
from typing import Any

from src.agents.workspace import Workspace, WorkspaceError


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def build_workspace_tools(workspace_dir: str, rt: Any = None) -> list[Any]:
    """Return closures bound to one task workspace."""

    workspace = Workspace(workspace_dir)

    def _check() -> None:
        if rt is not None:
            try:
                rt.check_cancel()
            except Exception:
                raise

    def workspace_list(path: str = ".") -> str:
        """列出任务工作区中的文件和目录。path 是相对路径，默认当前目录。"""
        _check()
        try:
            return _json(workspace.list(path))
        except WorkspaceError as exc:
            return _json({"error": str(exc)})

    def workspace_read(path: str) -> str:
        """读取任务工作区中的一个 UTF-8 文本文件。path 是相对路径。"""
        _check()
        try:
            return _json(workspace.read(path))
        except WorkspaceError as exc:
            return _json({"error": str(exc)})

    def workspace_write(path: str, content: str) -> str:
        """把文本写入任务工作区的相对路径；父目录会自动创建。

        交付物（表格/报告/清单）必须写到 out/ 目录下，例如
        out/component_list.md。写入后该文件会在本轮结束时登记为可下载产物。
        """
        _check()
        try:
            return _json(workspace.write(path, content))
        except WorkspaceError as exc:
            return _json({"error": str(exc)})

    def workspace_edit(
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> str:
        """精确替换文件中的一段文本（类似 apply_patch）。

        默认只允许唯一匹配；出现多次时必须显式 replace_all=true，避免误改。
        """
        _check()
        try:
            return _json(
                workspace.edit(
                    path, old_text, new_text, replace_all=bool(replace_all)
                )
            )
        except WorkspaceError as exc:
            return _json({"error": str(exc)})

    def workspace_glob(pattern: str, path: str = ".") -> str:
        """按 glob 模式列出工作区中的文件，例如 **/*.md 或 out/*.csv。"""
        _check()
        try:
            return _json(workspace.glob(pattern, path))
        except WorkspaceError as exc:
            return _json({"error": str(exc)})

    def workspace_grep(pattern: str, path: str = ".", max_results: int = 100) -> str:
        """在工作区文本文件中按正则搜索，返回路径、行号和该行内容。"""
        _check()
        try:
            return _json(
                workspace.grep(pattern, path, max_results=max_results)
            )
        except WorkspaceError as exc:
            return _json({"error": str(exc)})

    workspace_list.__name__ = "workspace_list"
    workspace_read.__name__ = "workspace_read"
    workspace_write.__name__ = "workspace_write"
    workspace_edit.__name__ = "workspace_edit"
    workspace_glob.__name__ = "workspace_glob"
    workspace_grep.__name__ = "workspace_grep"
    return [
        workspace_list,
        workspace_read,
        workspace_write,
        workspace_edit,
        workspace_glob,
        workspace_grep,
    ]


__all__ = ["build_workspace_tools"]
