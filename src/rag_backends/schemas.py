from dataclasses import dataclass, field
from typing import Any


@dataclass
class RequestContext:
    user_id: str = "anonymous"
    session_id: str = ""
    roles: list[str] = field(default_factory=list)
    allowed_kbs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def can_access_kb(self, kb_name: str) -> bool:
        return not self.allowed_kbs or kb_name in self.allowed_kbs


@dataclass
class Evidence:
    id: str
    content: str
    source_name: str = ""
    source_type: str = "document"
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    backend: str = "local"
    retriever: str = ""


@dataclass
class AnswerResult:
    answer: str
    evidences: list[Evidence] = field(default_factory=list)
    backend: str = "local"
    trace_id: str = ""


@dataclass
class IngestResult:
    success_count: int
    total_count: int
    messages: list[str] = field(default_factory=list)
    backend: str = "local"

    @property
    def ok(self) -> bool:
        return self.success_count == self.total_count

    def to_message(self) -> str:
        return f"✅ 成功处理 {self.success_count}/{self.total_count} 个文件\n" + "\n".join(self.messages)


@dataclass
class BackendResult:
    ok: bool
    message: str
    backend: str = "local"


@dataclass
class DocumentInfo:
    id: str
    name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    backend: str = "local"


@dataclass
class ParsedChunk:
    index: int
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseResult:
    document_id: str
    file_name: str
    chunk_count: int
    chunks: list[ParsedChunk] = field(default_factory=list)
    backend: str = "local"


@dataclass
class BackendHealth:
    ok: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    backend: str = "local"
