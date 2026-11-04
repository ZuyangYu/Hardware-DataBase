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

    workspace_list.__name__ = "workspace_list"
    workspace_read.__name__ = "workspace_read"
    workspace_write.__name__ = "workspace_write"
    return [workspace_list, workspace_read, workspace_write]


__all__ = ["build_workspace_tools"]
