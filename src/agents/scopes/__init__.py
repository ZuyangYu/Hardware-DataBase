"""Scope resolution separating "what may this turn read" from domain services.

Design §9.2/§10: spreadsheet and circuit domain services keep one parser and
one query engine; only the *scope* differs between knowledge-base ingestion
and chat attachments. Resolvers produce frozen scope objects; tools consume
them without ever deriving paths from user input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SpreadsheetScope:
    """Authorized spreadsheet query scope (KB department or chat attachment)."""

    source_type: str  # knowledge_base | chat_attachment
    db_path: str
    allowed_record_ids: frozenset[int] = field(default_factory=frozenset)
    department_id: str | None = None
    kb_name: str = ""
    attachment_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CircuitScope:
    """Authorized circuit query scope."""

    source_type: str
    store_root: str
    kb_name: str = ""
    attachment_ids: tuple[str, ...] = ()


class KnowledgeBaseSpreadsheetScopeResolver:
    """Existing KB behaviour: department context is mandatory."""

    def __init__(self, db_path_resolver):
        # db_path_resolver(department_id, kb_name) -> str (SpreadsheetIndexService)
        self._db_path_resolver = db_path_resolver

    def resolve(self, *, kb_name: str, department_id: str | None) -> SpreadsheetScope:
        if department_id in (None, ""):
            raise PermissionError("KB spreadsheet queries require department context")
        if not kb_name:
            raise PermissionError("KB spreadsheet queries require a knowledge base")
        return SpreadsheetScope(
            source_type="knowledge_base",
            db_path=self._db_path_resolver(department_id, kb_name),
            department_id=str(department_id),
            kb_name=kb_name,
        )


class AttachmentSpreadsheetScopeResolver:
    """Attachment behaviour: scope is the frozen, ACL-verified asset set."""

    def __init__(self, store):
        self._store = store

    def resolve(self, *, refs: list[Any]) -> SpreadsheetScope:
        attachment_ids: list[str] = []
        record_ids: set[int] = set()
        db_paths: set[str] = set()
        for ref in refs or []:
            asset = self._store.get_asset(getattr(ref, "asset_id", ""))
            if asset is None or asset.extension not in {".xlsx", ".xlsm"}:
                continue
            manifest = asset.manifest or {}
            db_path = str(manifest.get("index_db_path") or "")
            record_id = manifest.get("record_id")
            if not db_path or record_id is None:
                continue
            db_paths.add(db_path)
            record_ids.add(int(record_id))
            attachment_ids.append(str(getattr(ref, "attachment_id", "")))
        if not db_paths:
            return SpreadsheetScope(
                source_type="chat_attachment", db_path="", allowed_record_ids=frozenset()
            )
        # One attachment asset owns exactly one index database; multiple xlsx
        # attachments each keep their own db, so multi-asset SQL is not merged.
        if len(db_paths) > 1:
            raise ValueError("multiple spreadsheet attachments cannot be queried with one SQL statement")
        return SpreadsheetScope(
            source_type="chat_attachment",
            db_path=next(iter(db_paths)),
            allowed_record_ids=frozenset(record_ids),
            attachment_ids=tuple(attachment_ids),
        )


class KnowledgeBaseCircuitScopeResolver:
    """Existing KB behaviour: department context is mandatory."""

    def resolve(self, *, kb_name: str, department_id: str | None) -> CircuitScope:
        if department_id in (None, ""):
            raise PermissionError("KB circuit queries require department context")
        if not kb_name:
            raise PermissionError("KB circuit queries require a knowledge base")
        return CircuitScope(
            source_type="knowledge_base", store_root="", kb_name=kb_name
        )


class AttachmentCircuitScopeResolver:
    """Attachment behaviour: one dedicated CircuitStore root per session."""

    def __init__(self, store):
        self._store = store

    def resolve(self, *, refs: list[Any]) -> CircuitScope:
        attachment_ids: list[str] = []
        roots: set[str] = set()
        for ref in refs or []:
            asset = self._store.get_asset(getattr(ref, "asset_id", ""))
            if asset is None or asset.extension not in {".edf", ".edif"}:
                continue
            root = str((asset.manifest or {}).get("circuit_root") or "")
            if not root:
                continue
            roots.add(root)
            attachment_ids.append(str(getattr(ref, "attachment_id", "")))
        return CircuitScope(
            source_type="chat_attachment",
            store_root=next(iter(roots)) if len(roots) == 1 else "",
            attachment_ids=tuple(attachment_ids),
        )
