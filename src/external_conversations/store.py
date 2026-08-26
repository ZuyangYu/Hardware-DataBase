"""Department-scoped conversation store.

Physical layout mirrors the spreadsheet index service:
``{root}/departments/{department_id}/kbs/{kb_name}/{conversation_id}/``
with ``conversation.json`` (source of truth) plus the original raw file copy.
"""

from __future__ import annotations

import json
import os
import shutil

import config.settings
from src.ingestion.kb_paths import safe_child_path, validate_kb_name
from src.external_conversations.models import ExternalConversation


def _require_department_id(department_id: str | int | None, action: str) -> str:
    if department_id in (None, ""):
        raise ValueError(f"department_id is required for {action} external conversations")
    return str(department_id)


def _safe_conversation_id(conversation_id: str) -> str:
    """Reject traversal attempts but keep valid ids byte-identical.

    The conversation id produced by ``make_conversation_id`` is already a safe
    path component; sanitizing further could desync directory names from the
    index/ledger keys, so only clearly-bad values are rejected here. Path
    containment is still enforced by ``safe_child_path``.
    """
    value = str(conversation_id or "").strip()
    if not value or ".." in value or "/" in value or "\\" in value:
        raise ValueError("Invalid conversation id.")
    return value


class ExternalConversationStore:
    def __init__(self, root: str | None = None):
        self.root = root or os.path.join(config.settings.STORAGE_DIR, "external_conversations")

    def scope_dir(self, department_id: str | int | None, kb_name: str, create: bool = False) -> str:
        dept = _require_department_id(department_id, "locating")
        kb = validate_kb_name(kb_name)
        return safe_child_path(
            os.path.abspath(self.root),
            "departments",
            _safe_scope_part(dept),
            "kbs",
            kb,
            create=create,
        )

    def conversation_dir(
        self,
        department_id: str | int | None,
        kb_name: str,
        conversation_id: str,
        create: bool = False,
    ) -> str:
        scope = self.scope_dir(department_id, kb_name, create=False)
        return safe_child_path(scope, _safe_conversation_id(conversation_id), create=create)

    def save(self, conversation: ExternalConversation, raw_bytes: bytes | None = None, raw_ext: str = ".md") -> str:
        dept = _require_department_id(conversation.department_id, "saving")
        path = self.conversation_dir(dept, conversation.kb_name, conversation.conversation_id, create=True)
        target = os.path.join(path, "conversation.json")
        with open(target, "w", encoding="utf-8") as f:
            json.dump(conversation.to_dict(), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if raw_bytes is not None:
            ext = raw_ext if raw_ext.startswith(".") else f".{raw_ext}"
            with open(os.path.join(path, f"original{ext}"), "wb") as f:
                f.write(raw_bytes)
                f.flush()
                os.fsync(f.fileno())
        return target

    def load(self, department_id: str | int | None, kb_name: str, conversation_id: str) -> ExternalConversation | None:
        try:
            path = self.conversation_dir(department_id, kb_name, conversation_id, create=False)
        except (ValueError, OSError):
            return None
        target = os.path.join(path, "conversation.json")
        if not os.path.exists(target):
            return None
        with open(target, "r", encoding="utf-8") as f:
            return ExternalConversation.from_dict(json.load(f))

    def list_conversations(self, department_id: str | int | None, kb_name: str) -> list[ExternalConversation]:
        try:
            scope = self.scope_dir(department_id, kb_name, create=False)
        except (ValueError, OSError):
            return []
        if not os.path.isdir(scope):
            return []
        result = []
        for name in sorted(os.listdir(scope)):
            loaded = self.load(department_id, kb_name, name)
            if loaded:
                result.append(loaded)
        return result

    def delete_conversation(self, department_id: str | int | None, kb_name: str, conversation_id: str) -> bool:
        try:
            path = self.conversation_dir(department_id, kb_name, conversation_id, create=False)
        except (ValueError, OSError):
            return False
        if not os.path.isdir(path):
            return False
        shutil.rmtree(path)
        return True

    def delete_kb(self, department_id: str | int | None, kb_name: str) -> bool:
        scope = self.scope_dir(department_id, kb_name, create=False)
        if not os.path.isdir(scope):
            return False
        shutil.rmtree(scope)
        return True


def _safe_scope_part(value: str) -> str:
    import re

    raw = str(value)
    if ".." in raw:
        raise ValueError("Invalid department scope part.")
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_-")
    if not cleaned or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", cleaned):
        raise ValueError("Invalid department scope part.")
    return cleaned[:128]
