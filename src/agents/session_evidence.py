"""Session-level evidence catalogue built from persisted turn summaries.

The catalogue is deliberately a *pointer* store: it keeps compact evidence rows
from recent turns so the model can pull an old source back through
``session_recall`` and then ``evidence_read``.  It never promotes evidence and
never reuses an old ``[n]`` label — citation numbers are always assigned anew
by the current turn's evidence ledger.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.agents.schemas import Evidence
from src.core.query_tokens import tokenize_hardware_query

_MAX_GAPS = 5
_GAP_TURN_LIMIT = 4
_DEFAULT_TURN_LIMIT = 12
_DEFAULT_ITEM_LIMIT = 40
_MAX_SEARCH_RESULTS = 24


@dataclass(frozen=True)
class SessionEvidenceEntry:
    """One evidence row plus the turn that produced it."""

    evidence: Evidence
    turn_id: str
    turn_query: str
    turn_created_at: str
    recency: int


@dataclass
class SessionEvidenceStore:
    """Dormant, session-scoped evidence pointers."""

    history: list[Any] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    entries: list[SessionEvidenceEntry] = field(default_factory=list)

    @classmethod
    def from_turns(
        cls,
        turns: Iterable[Any],
        *,
        max_items: int = _DEFAULT_ITEM_LIMIT,
        gap_turn_limit: int = _GAP_TURN_LIMIT,
    ) -> "SessionEvidenceStore":
        history = [
            turn
            for turn in turns
            if isinstance(getattr(turn, "summary", None), dict) and turn.summary
        ]
        gaps: list[str] = []
        # Gaps stay on the previous (short) horizon; evidence search can reach
        # further back without waking stale unresolved-gap prompts.
        for turn in history[: max(1, int(gap_turn_limit))]:
            summary = turn.summary if isinstance(turn.summary, dict) else {}
            verification = summary.get("verification") or {}
            for gap in [
                *(verification.get("answer_gaps") or []),
                *(verification.get("gaps") or []),
            ]:
                text = str(gap or "").strip()
                if text and text not in gaps:
                    gaps.append(text[:240])
                if len(gaps) >= _MAX_GAPS:
                    break
            if len(gaps) >= _MAX_GAPS:
                break

        entries: list[SessionEvidenceEntry] = []
        seen: set[str] = set()
        for recency, turn in enumerate(history):
            summary = turn.summary if isinstance(turn.summary, dict) else {}
            for row in summary.get("evidence") or []:
                if not isinstance(row, dict) or not row.get("id") or not row.get("content"):
                    continue
                evidence_id = str(row.get("id"))
                if evidence_id in seen:
                    # Newest turn wins; keep the fresher row and its metadata.
                    continue
                seen.add(evidence_id)
                metadata = dict(row.get("metadata") or {})
                # Old turn labels are never reusable; re-registration assigns
                # the current turn's citation number.
                metadata.pop("citation_number", None)
                entries.append(
                    SessionEvidenceEntry(
                        evidence=Evidence(
                            id=evidence_id,
                            content=str(row.get("content")),
                            source_name=str(row.get("source_name") or ""),
                            content_kind=str(row.get("content_kind") or "document_text"),
                            processor_kind=str(row.get("processor_kind") or "ragflow"),
                            score=float(row.get("score") or 0.0),
                            locator=dict(row.get("locator") or {}),
                            metadata=metadata,
                        ),
                        turn_id=str(getattr(turn, "id", "") or ""),
                        turn_query=str(getattr(turn, "query", "") or ""),
                        turn_created_at=str(getattr(turn, "created_at", "") or ""),
                        recency=recency,
                    )
                )
                if len(entries) >= max_items:
                    break
            if len(entries) >= max_items:
                break
        return cls(history=history, gaps=gaps, entries=entries)

    def recent(self, limit: int = 12) -> list[Evidence]:
        """Newest-first evidence pointers."""

        capped = max(1, min(int(limit or 12), _MAX_SEARCH_RESULTS))
        return [entry.evidence for entry in self.entries[:capped]]

    def search(self, query: str, *, limit: int = 12) -> list[Evidence]:
        """Rank session pointers by lexical relevance, newest-first on ties."""

        text = " ".join(str(query or "").split())
        if not text:
            return self.recent(limit)
        capped = max(1, min(int(limit or 12), _MAX_SEARCH_RESULTS))
        terms = tokenize_hardware_query(text, max_tokens=16)
        if not terms:
            terms = [text.casefold()]
        scored: list[tuple[int, int, Evidence]] = []
        for entry in self.entries:
            evidence = entry.evidence
            haystack = " ".join(
                [
                    evidence.content,
                    evidence.source_name,
                    entry.turn_query,
                    json.dumps(evidence.locator, ensure_ascii=False, default=str),
                ]
            ).casefold()
            hits = sum(1 for term in terms if term in haystack)
            if not hits:
                continue
            # Recency is a tie-breaker, not a substitute for relevance.
            score = hits * 10 + max(0, 4 - entry.recency)
            scored.append((score, entry.recency, evidence))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [evidence for _score, _recency, evidence in scored[:capped]]


def load_session_evidence(
    user_id: int | None,
    session_id: int | None,
    *,
    turn_limit: int = _DEFAULT_TURN_LIMIT,
    service: Any | None = None,
) -> SessionEvidenceStore:
    """Load recent turn summaries into a dormant session catalogue."""

    if not user_id or not session_id:
        return SessionEvidenceStore()
    try:
        if service is None:
            from src.core.conversation import ConversationService

            service = ConversationService()
        turns = service.list_recent_turns(
            int(user_id),
            int(session_id),
            limit=max(4, int(turn_limit)),
        )
    except Exception:
        return SessionEvidenceStore()
    return SessionEvidenceStore.from_turns(turns)


__all__ = [
    "SessionEvidenceEntry",
    "SessionEvidenceStore",
    "load_session_evidence",
]
