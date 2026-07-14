from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config.settings
from src.circuit.query_context import CircuitSessionContext, CircuitToolResponse, ResolvedEntity
from src.ingestion.kb_paths import validate_kb_name


_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_session_id(session_id: str) -> str:
    value = _SESSION_ID_RE.sub("_", (session_id or "anonymous").strip())[:128]
    return value or "anonymous"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SessionContextStore:
    """File-backed circuit query session context store."""

    def __init__(self, root: str | None = None):
        self.root = root or os.path.join(config.settings.STORAGE_DIR, "query_sessions")

    def _path(self, session_id: str) -> str:
        session = _safe_session_id(session_id)
        path = os.path.abspath(os.path.join(self.root, session, "circuit_context.json"))
        root_abs = os.path.abspath(self.root)
        if os.path.commonpath([root_abs, path]) != root_abs:
            raise ValueError("Resolved session context path escapes storage root.")
        return path

    def load(self, session_id: str, kb_name: str) -> CircuitSessionContext:
        kb_name = validate_kb_name(kb_name)
        data = self._read(session_id)
        ctx = data.get("contexts", {}).get(kb_name, {}) if isinstance(data, dict) else {}
        return CircuitSessionContext(
            session_id=_safe_session_id(session_id),
            kb_name=kb_name,
            current_circuit_id=ctx.get("current_circuit_id"),
            last_entities=[ResolvedEntity(**item) for item in ctx.get("last_entities", []) if isinstance(item, dict)],
            last_query_intent=ctx.get("last_query_intent"),
            last_answer_summary=ctx.get("last_answer_summary"),
            updated_at=ctx.get("updated_at"),
            current_circuit_version=ctx.get("current_circuit_version"),
        )

    def save(self, context: CircuitSessionContext) -> None:
        kb_name = validate_kb_name(context.kb_name)
        path = self._path(context.session_id)
        data = self._read(context.session_id)
        if not isinstance(data, dict):
            data = {}
        data["session_id"] = _safe_session_id(context.session_id)
        contexts = data.setdefault("contexts", {})
        context.updated_at = context.updated_at or _utcnow_iso()
        contexts[kb_name] = {
            "current_circuit_id": context.current_circuit_id,
            "last_entities": [entity.to_dict() for entity in context.last_entities],
            "last_query_intent": context.last_query_intent,
            "last_answer_summary": context.last_answer_summary,
            "updated_at": context.updated_at,
            "current_circuit_version": context.current_circuit_version,
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def update_after_response(
        self,
        context: CircuitSessionContext,
        response: CircuitToolResponse,
        scope,
        intent,
        circuit_version: str | None = None,
    ) -> CircuitSessionContext:
        if (
            getattr(scope, "scope_type", None) == "single_circuit"
            and response.answer_mode == "direct_answer"
            and getattr(scope, "confidence", 0.0) >= 0.8
            and getattr(scope, "circuit_ids", None)
        ):
            context.current_circuit_id = scope.circuit_ids[0]
            context.current_circuit_version = circuit_version

        if response.resolved_entities:
            context.last_entities = response.resolved_entities[:20]
        context.last_query_intent = getattr(intent, "intent", None)
        context.last_answer_summary = response.answer[:200] if response.answer else None
        context.updated_at = _utcnow_iso()
        self.save(context)
        return context

    def _read(self, session_id: str) -> dict[str, Any]:
        path = self._path(session_id)
        if not os.path.exists(path):
            return {"session_id": _safe_session_id(session_id), "contexts": {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("contexts", {})
                return data
        except (OSError, ValueError):
            pass
        return {"session_id": _safe_session_id(session_id), "contexts": {}}
