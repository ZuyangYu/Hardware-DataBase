"""Per-task filesystem workspace with strict root confinement.

The general agent gets read/write access to exactly one directory tree per
task.  Paths are always relative; ``..`` segments, absolute paths and
symlinks that resolve outside the root are rejected.  This is the smallest
safe substitute for a shell when the execution sandbox is not available yet.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

MAX_READ_BYTES = 2 * 1024 * 1024
MAX_WRITE_BYTES = 2 * 1024 * 1024
MAX_LIST_ENTRIES = 500


class WorkspaceError(ValueError):
    """Raised when a path is invalid or escapes the workspace root."""


class Workspace:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def out_dir(self) -> Path:
        path = self.root / "out"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve(self, relative_path: str) -> Path:
        raw = str(relative_path or "").strip().replace("\\", "/")
        if not raw:
            raw = "."
        if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
            raise WorkspaceError("absolute paths are not allowed")
        if raw == ".." or raw.startswith("../") or "/../" in raw:
            raise WorkspaceError("path escapes the workspace")
        candidate = (self.root / raw).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise WorkspaceError("path escapes the workspace")
        return candidate

    def list(self, relative_path: str = ".") -> dict[str, Any]:
        target = self.resolve(relative_path)
        if not target.exists():
            raise WorkspaceError("path does not exist")
        if target.is_file():
            return {
                "path": self._rel(target),
                "type": "file",
                "size_bytes": target.stat().st_size,
            }
        entries: list[dict[str, Any]] = []
        for child in sorted(target.iterdir(), key=lambda item: item.name)[:MAX_LIST_ENTRIES]:
            is_dir = child.is_dir()
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if is_dir else "file",
                    "size_bytes": 0 if is_dir else child.stat().st_size,
                }
            )
        return {"path": self._rel(target), "type": "dir", "entries": entries}

    def read(self, relative_path: str) -> dict[str, Any]:
        target = self.resolve(relative_path)
        if not target.is_file():
            raise WorkspaceError("file does not exist")
        if target.stat().st_size > MAX_READ_BYTES:
            raise WorkspaceError(
                f"file is larger than {MAX_READ_BYTES} bytes; read a smaller file"
            )
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError("only UTF-8 text files can be read") from exc
        return {
            "path": self._rel(target),
            "size_bytes": target.stat().st_size,
            "content": content,
        }

    def write(self, relative_path: str, content: str) -> dict[str, Any]:
        payload = str(content or "")
        encoded = payload.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            raise WorkspaceError(
                f"content is larger than {MAX_WRITE_BYTES} bytes; write a smaller file"
            )
        target = self.resolve(relative_path)
        if target == self.root or target.is_dir():
            raise WorkspaceError("target must be a file path")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        return {
            "path": self._rel(target),
            "size_bytes": target.stat().st_size,
            "written": True,
        }

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root)).replace(os.sep, "/") or "."
        except ValueError as exc:  # pragma: no cover - resolve() already guards
            raise WorkspaceError("path escapes the workspace") from exc


__all__ = ["Workspace", "WorkspaceError", "MAX_READ_BYTES", "MAX_WRITE_BYTES"]
