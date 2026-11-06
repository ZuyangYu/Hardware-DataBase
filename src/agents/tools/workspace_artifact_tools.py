"""Workspace artifact publishing for the general agent."""

from __future__ import annotations

import json
from typing import Any

from src.agents.workspace import Workspace, WorkspaceError


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def build_workspace_artifact_tool(
    workspace_id: str,
    store: Any,
    rt: Any = None,
    workspace_dir: str = "",
    session_id: int = 0,
    turn_id: str = "",
) -> Any:
    """Return ``workspace_publish_artifact`` bound to one workspace."""

    def workspace_publish_artifact(
        name: str, content: str, kind: str = ""
    ) -> str:
        """发布一个交付物到当前工作区；用户可在会话中直接下载。

        name 是展示名称（如 关键器件清单.md），content 是文本内容。
        只写工作区 out/ 目录，不暴露磁盘路径。
        """
        if rt is not None:
            try:
                rt.check_cancel()
            except Exception:
                raise
        try:
            safe = str(name or "").strip().replace("\\", "/")
            if (
                not safe
                or safe.startswith("/")
                or ".." in safe.split("/")
                or not workspace_dir
            ):
                raise ValueError("invalid artifact name or workspace")
            workspace = Workspace(workspace_dir)
            relative = f"out/{safe}"
            workspace.write(relative, str(content or ""))
            artifact = store.register_artifact(
                workspace_id,
                str(workspace.resolve(relative)),
                session_id=int(session_id or 0),
                turn_id=str(turn_id or ""),
                name=safe,
                kind=(str(kind).strip() or None) if kind else None,
            )
            return _json(
                {
                    "artifact_id": artifact.id,
                    "name": artifact.name,
                    "kind": artifact.kind,
                    "version": artifact.version,
                    "download_url": f"/api/v1/artifacts/{artifact.id}/download",
                }
            )
        except (WorkspaceError, ValueError, OSError) as exc:
            return _json({"error": str(exc)})

    workspace_publish_artifact.__name__ = "workspace_publish_artifact"
    return workspace_publish_artifact


__all__ = ["build_workspace_artifact_tool"]
