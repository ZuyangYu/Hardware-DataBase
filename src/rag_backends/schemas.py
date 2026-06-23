from dataclasses import dataclass, field
from typing import Any


@dataclass
class RequestContext:
    user_id: str = "anonymous"
    session_id: str = ""
    roles: list[str] = field(default_factory=list)
    allowed_kbs: list[str] = field(default_factory=list)
    kb_permissions: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_system_admin(self) -> bool:
        return "system_admin" in self.roles

    def can_access_kb(self, kb_name: str) -> bool:
        return self.has_kb_permission(kb_name, "read")

    def has_kb_permission(self, kb_name: str, required: str = "read") -> bool:
        if self.is_system_admin():
            return False
        levels = {"read": 1, "write": 2, "admin": 3}
        required_level = levels.get(required, 1)
        permission = self.kb_permissions.get(kb_name)
        if permission is None and kb_name in self.allowed_kbs:
            permission = "read"
        return levels.get(permission or "", 0) >= required_level


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
    failed_count: int = 0
    skipped_count: int = 0

    @property
    def ok(self) -> bool:
        return self.total_count > 0 and self.success_count == self.total_count and self.failed_count == 0 and self.skipped_count == 0

    def to_message(self) -> str:
        if self.ok:
            prefix = "全部处理成功"
        elif self.success_count > 0:
            prefix = "部分处理完成"
        else:
            prefix = "未处理成功"
        summary = (
            f"{prefix}: 成功 {self.success_count}, "
            f"失败 {self.failed_count}, 跳过 {self.skipped_count}, 总计 {self.total_count}"
        )
        details = "\n".join(self.messages)
        return f"{summary}\n{details}" if details else summary


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
