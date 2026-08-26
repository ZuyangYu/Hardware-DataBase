from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ConversationTurn:
    """One detected dialogue turn. Offlets reference the original raw text."""

    role: str  # "user" | "assistant" | "document"
    content: str
    ts: str = ""
    start_offset: int = 0
    end_offset: int = 0


@dataclass
class ExternalConversation:
    conversation_id: str
    kb_name: str
    department_id: str
    title: str = ""
    source_file: str = ""
    content_hash: str = ""
    origin: str = "upload"  # "upload" | "chat_deposit"
    source_group: str = ""
    kb_id: int = 0
    turns: list[ConversationTurn] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    created_at: str = ""
    # AI-derived extraction (optional, fail-open)
    summary: str = ""
    key_points: list[str] = field(default_factory=list)
    summary_generated_at: str = ""

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExternalConversation":
        turns = [ConversationTurn(**t) for t in data.get("turns", [])]
        return cls(
            conversation_id=data["conversation_id"],
            kb_name=data["kb_name"],
            department_id=data.get("department_id", ""),
            title=data.get("title", ""),
            source_file=data.get("source_file", ""),
            content_hash=data.get("content_hash", ""),
            origin=data.get("origin", "upload"),
            source_group=data.get("source_group", ""),
            kb_id=int(data.get("kb_id") or 0),
            turns=turns,
            blocks=list(data.get("blocks", [])),
            created_at=data.get("created_at", ""),
            summary=data.get("summary", ""),
            key_points=[str(k) for k in (data.get("key_points") or [])],
            summary_generated_at=data.get("summary_generated_at", ""),
        )
