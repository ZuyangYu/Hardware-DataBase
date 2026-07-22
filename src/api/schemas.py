"""HTTP-boundary DTOs for the API layer.

Thin transport models only -- business data still crosses layer boundaries as
the dataclasses in src.pipelines.document_rag.schemas / src.core.auth. We do
not redefine business schemas here.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class UserInfo(BaseModel):
    username: str
    role: str
    department_id: int | None = None
    department_name: str | None = None


class LoginResponse(BaseModel):
    token: str
    user: UserInfo


class OkResponse(BaseModel):
    ok: bool = True
    message: str = ""


class CreateKbRequest(BaseModel):
    name: str


class KbView(BaseModel):
    name: str
    kb_id: int | None = None
    department_id: int | None = None
    department_name: str | None = None
    permission: str | None = None
    registered: bool = True


class FileView(BaseModel):
    id: str
    name: str
    status: str = ""
    processor_kind: str = ""
    dataset_kind: str = ""


class UploadAck(BaseModel):
    success_count: int
    total_count: int
    failed_count: int = 0
    skipped_count: int = 0
    status: str
    messages: list[str] = Field(default_factory=list)


class QueryRequest(BaseModel):
    kb_name: str
    query: str
    history: list[list[str]] = Field(default_factory=list)
