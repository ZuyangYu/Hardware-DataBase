"""Per-task filesystem workspace with strict root confinement.

The general agent gets read/write access to exactly one directory tree per
task.  Paths are always relative; ``..`` segments, absolute paths and
symlinks that resolve outside the root are rejected.  This is the smallest
safe substitute for a shell when the execution sandbox is not available yet.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

MAX_READ_BYTES = 2 * 1024 * 1024
MAX_WRITE_BYTES = 2 * 1024 * 1024
MAX_LIST_ENTRIES = 500
MAX_GLOB_MATCHES = 200
MAX_GREP_MATCHES = 200


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

    def edit(
        self,
        relative_path: str,
        old_text: str,
        new_text: str,
        *,
        replace_all: bool = False,
    ) -> dict[str, Any]:
        """Exact-text replacement with the same safety rule as apply_patch."""

        old = str(old_text or "")
        if not old:
            raise WorkspaceError("old_text must not be empty")
        target = self.resolve(relative_path)
        if not target.is_file():
            raise WorkspaceError("file does not exist")
        if target.stat().st_size > MAX_READ_BYTES:
            raise WorkspaceError("file is too large to edit")
        text = target.read_text(encoding="utf-8")
        occurrences = text.count(old)
        if occurrences == 0:
            raise WorkspaceError("old_text was not found")
        if occurrences > 1 and not replace_all:
            raise WorkspaceError(
                f"old_text occurs {occurrences} times; pass replace_all=true or add more context"
            )
        updated = text.replace(old, str(new_text or "")) if replace_all else text.replace(old, str(new_text or ""), 1)
        if len(updated.encode("utf-8")) > MAX_WRITE_BYTES:
            raise WorkspaceError("edited content would exceed the workspace size limit")
        target.write_text(updated, encoding="utf-8")
        return {
            "path": self._rel(target),
            "occurrences": occurrences,
            "replaced": occurrences if replace_all else 1,
            "size_bytes": target.stat().st_size,
        }

    def glob(self, pattern: str, relative_path: str = ".") -> dict[str, Any]:
        raw = str(pattern or "").strip().replace("\\", "/")
        if not raw or raw.startswith("/") or ".." in raw.split("/"):
            raise WorkspaceError("invalid glob pattern")
        base = self.resolve(relative_path)
        if not base.is_dir():
            raise WorkspaceError("glob base must be a directory")
        matches: list[str] = []
        for candidate in base.glob(raw):
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved != self.root and self.root not in resolved.parents:
                continue
            if resolved.is_file():
                matches.append(self._rel(resolved))
                if len(matches) >= MAX_GLOB_MATCHES:
                    break
        return {
            "base": self._rel(base),
            "pattern": raw,
            "matches": sorted(matches),
            "truncated": len(matches) >= MAX_GLOB_MATCHES,
        }

    def grep(
        self,
        pattern: str,
        relative_path: str = ".",
        *,
        max_results: int = 100,
    ) -> dict[str, Any]:
        try:
            regex = re.compile(str(pattern or ""))
        except re.error as exc:
            raise WorkspaceError(f"invalid regular expression: {exc}") from exc
        base = self.resolve(relative_path)
        if not base.exists():
            raise WorkspaceError("path does not exist")
        files: list[Path] = []
        if base.is_file():
            files = [base]
        else:
            for current_root, _dirs, names in os.walk(base):
                for name in names:
                    files.append(Path(current_root) / name)
        limit = max(1, min(int(max_results), MAX_GREP_MATCHES))
        matches: list[dict[str, Any]] = []
        for candidate in files:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved != self.root and self.root not in resolved.parents:
                continue
            if not resolved.is_file() or resolved.stat().st_size > MAX_READ_BYTES:
                continue
            try:
                text = resolved.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(
                        {
                            "path": self._rel(resolved),
                            "line": line_number,
                            "text": line[:400],
                        }
                    )
                    if len(matches) >= limit:
                        return {
                            "pattern": str(pattern),
                            "matches": matches,
                            "truncated": True,
                        }
        return {"pattern": str(pattern), "matches": matches, "truncated": False}

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root)).replace(os.sep, "/") or "."
        except ValueError as exc:  # pragma: no cover - resolve() already guards
            raise WorkspaceError("path escapes the workspace") from exc


__all__ = ["Workspace", "WorkspaceError", "MAX_READ_BYTES", "MAX_WRITE_BYTES"]
