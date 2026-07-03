from dataclasses import dataclass
from typing import Any

from src.ingestion.kb_paths import validate_kb_name
from src.pipelines.document_rag.schemas import RequestContext


@dataclass(frozen=True)
class KbScope:
    kb_name: str
    department_id: str = ""
    kb_id: int | None = None

    @property
    def has_department(self) -> bool:
        return bool(self.department_id)

    def require_department(self, action: str = "access") -> "KbScope":
        if not self.department_id:
            raise PermissionError(f"Department context is required to {action} knowledge base {self.kb_name}.")
        return self


def kb_scope_from_context(kb_name: str, ctx: RequestContext | None = None) -> KbScope:
    metadata: dict[str, Any] = ctx.metadata if ctx else {}
    department_id = metadata.get("resource_department_id")
    if department_id in (None, ""):
        department_id = metadata.get("department_id")
    kb_id = metadata.get("kb_id")
    return KbScope(
        kb_name=validate_kb_name(kb_name),
        department_id="" if department_id in (None, "") else str(department_id),
        kb_id=int(kb_id) if str(kb_id or "").isdigit() else None,
    )


def local_kb_scope_key(kb_name: str, ctx: RequestContext | None = None) -> str:
    scope = kb_scope_from_context(kb_name, ctx)
    if not scope.department_id:
        return scope.kb_name
    return validate_kb_name(f"dept_{scope.department_id}__{scope.kb_name}")
