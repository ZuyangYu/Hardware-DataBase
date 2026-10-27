"""File-level operations beyond the basic list/upload/delete in kbs.py.

Currently exposes the parse-result (chunks) endpoint. The other file ops
live in kbs.py / upload.py alongside their KB parents.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt

import src.settings
from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser

from src.api.context import build_context_for_user, build_context_for_knowledge_source
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import ChunkView, ParseResultView
from src.pipelines.document_rag.schemas import ScopedRetrievalError
from src.services.citation_readback import read_citation_content, validate_citation_processing_identity
from src.services.knowledge_scope_models import AuthorizedKnowledgeSource

router = APIRouter(tags=["files"])


def _citation_http_error(exc: ScopedRetrievalError) -> HTTPException:
    """Map typed readback failures without disguising them as missing text."""
    if exc.status == "access_denied":
        return HTTPException(status_code=403, detail="citation_access_denied")
    if exc.status == "source_changed":
        return HTTPException(status_code=409, detail="citation_source_changed")
    if exc.status == "filter_unsupported":
        code = exc.error_code if exc.error_code.startswith("citation_") else f"citation_{exc.error_code}"
        return HTTPException(status_code=422, detail=code)
    if exc.status == "timeout":
        return HTTPException(status_code=504, detail="citation_readback_timeout")
    if exc.status == "source_unavailable" and exc.error_code == "citation_readback_incomplete":
        return HTTPException(status_code=502, detail="citation_readback_incomplete")
    return HTTPException(status_code=503, detail="citation_source_unavailable")


class CitationReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kb_id: StrictInt = Field(gt=0)
    local_record_id: StrictInt = Field(gt=0)
    source_content_hash: str = Field(min_length=1, max_length=128)
    # Optional on the wire to give legacy citations the same explicit 409 as
    # changed sources. Missing stamps must never bind to the current parse.
    index_revision: StrictInt | None = Field(default=None, ge=0)
    processing_artifact_id: str | None = Field(default=None, max_length=512)
    processor_version: str | None = Field(default=None, max_length=512)
    evidence_text_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    locator: dict = Field(default_factory=dict)


@router.post("/citations/resolve")
def resolve_citation(
    body: CitationReadRequest,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    identity = auth.get_knowledge_base_identity(body.kb_id)
    if identity is None or identity.department_id != user.department_id:
        raise HTTPException(status_code=403, detail="citation_access_denied")

    def authorize_and_read():
        try:
            ctx = build_context_for_knowledge_source(user, AuthorizedKnowledgeSource(
                kb_id=identity.kb_id, kb_name=identity.kb_name,
                department_id=str(identity.department_id), role="reference",
            ), auth=auth)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="citation_access_denied") from exc
        reject_system_admin_kb_access(ctx)
        if not ctx.has_kb_permission(identity.kb_name, "read"):
            raise HTTPException(status_code=403, detail="citation_access_denied")
        # AppPipeline owns the backend document store; keep the old direct
        # attribute as a compatibility fallback for lightweight integrations.
        document_store = getattr(pipeline, "document_store", None)
        if document_store is None:
            document_store = getattr(getattr(pipeline, "backend", None), "store", None)
        if document_store is None:
            raise HTTPException(status_code=503, detail="citation_store_unavailable")
        record = document_store.get_document_by_id_scoped(body.local_record_id, identity.department_id)
        if record is None or record.kb_name != identity.kb_name or record.kb_id not in (0, None, identity.kb_id):
            raise HTTPException(status_code=404, detail="citation_source_not_found")
        if not record.content_hash or record.content_hash != body.source_content_hash:
            raise HTTPException(status_code=409, detail="citation_source_changed")
        try:
            validate_citation_processing_identity(record, body.model_dump())
        except ScopedRetrievalError as exc:
            raise HTTPException(status_code=409, detail="citation_source_changed") from exc
        return ctx, record

    ctx, record = authorize_and_read()
    deadline = time.monotonic() + max(1, int(src.settings.RAGFLOW_TIMEOUT_SECONDS))
    try:
        content = read_citation_content(pipeline, record, ctx, body.locator,
                                        expected_evidence_text_hash=body.evidence_text_hash,
                                        deadline=deadline)
    except ScopedRetrievalError as exc:
        raise _citation_http_error(exc) from exc
    authorize_and_read()
    if content is None:
        raise HTTPException(status_code=404, detail="citation_locator_not_found")
    return {"kb_id": identity.kb_id, "kb_name": identity.kb_name, "local_record_id": record.id,
            "source_name": record.document_name, "source_content_hash": record.content_hash,
            "index_revision": body.index_revision, "processing_artifact_id": body.processing_artifact_id,
            "processor_version": body.processor_version, "evidence_text_hash": body.evidence_text_hash,
            "locator": body.locator, "content": content}


@router.get("/kbs/{kb_name}/files/{file_id}/chunks", response_model=ParseResultView)
def get_file_chunks(
    kb_name: str,
    file_id: str,
    user: AuthUser = Depends(current_user),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    """Return the parsed chunks for a document (RAGFlow-parsed content)."""
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, "read"):
        raise HTTPException(status_code=403, detail="read permission required")
    result = None
    deadline = time.monotonic() + max(1, int(src.settings.RAGFLOW_TIMEOUT_SECONDS))
    try:
        result = pipeline.get_parse_result(kb_name, file_id, ctx=ctx, deadline=deadline)
    except ScopedRetrievalError as exc:
        raise _citation_http_error(exc) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="parse result not found")
    return ParseResultView(
        document_id=result.document_id,
        file_name=result.file_name,
        chunk_count=result.chunk_count,
        chunks=[
            ChunkView(index=c.index, content=c.content, metadata=c.metadata)
            for c in result.chunks
        ],
        backend=result.backend,
    )
