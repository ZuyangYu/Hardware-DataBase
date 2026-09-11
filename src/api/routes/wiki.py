"""Wiki knowledge layer endpoints (WeKnora-style distillation browsing)."""
from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.context import build_context_for_user
from src.api.deps import current_user, get_auth_service, get_pipeline, reject_system_admin_kb_access
from src.api.schemas import (
    OkResponse,
    WikiGraphView,
    WikiIngestRequest,
    WikiPageDetailView,
    WikiPageUpdateRequest,
    WikiPageUpsertRequest,
    WikiPageView,
)
from src.core.auth import AuthService, AuthUser
from src.core.llm_governor import PRIORITY_BATCH
from src.core.model_factory import create_chat_model
from src.core.wiki import WIKI_JOB_STATE, WikiService, _WIKI_JOB_LOCK

router = APIRouter(tags=["wiki"])


def _scope(user: AuthUser, kb_name: str, permission: str, auth: AuthService):
    ctx = build_context_for_user(user, kb_name, auth=auth)
    reject_system_admin_kb_access(ctx)
    if not ctx.has_kb_permission(kb_name, permission):
        raise HTTPException(status_code=403, detail=f"{permission} permission required")
    kb_id = ctx.metadata.get("kb_id")
    department_id = ctx.metadata.get("resource_department_id")
    if kb_id is None or department_id is None:
        raise HTTPException(status_code=404, detail="knowledge base scope not found")
    return ctx, int(kb_id), int(department_id)


def _service() -> WikiService:
    return WikiService()


def _gather_documents(pipeline, ctx, kb_name: str, kb_id: int, department_id: int) -> list[dict]:
    return WikiService().gather_documents(
        pipeline, kb_name=kb_name, ctx=ctx, kb_id=kb_id, department_id=department_id
    )


def _run_ingest_job(
    kb_id: int, kb_name: str, department_id: int, pipeline, ctx,
    granularity: str, max_pages: int,
) -> None:
    with _WIKI_JOB_LOCK:
        WIKI_JOB_STATE[kb_id] = {"running": True, "stage": "准备", "started_at": _now(), "error": ""}
    try:
        service = _service()
        chat_model = create_chat_model(priority=PRIORITY_BATCH)

        def chat_fn(prompt: str) -> str:
            return str(chat_model.invoke(prompt).content)

        documents = _gather_documents(pipeline, ctx, kb_name, kb_id=kb_id, department_id=department_id)
        with _WIKI_JOB_LOCK:
            WIKI_JOB_STATE[kb_id]["stage"] = f"蒸馏 {len(documents)} 份文档"

        def progress(message: str) -> None:
            with _WIKI_JOB_LOCK:
                state = WIKI_JOB_STATE.get(kb_id)
                if state and state.get("running"):
                    state["stage"] = message

        stats = service.ingest(
            kb_id=kb_id, kb_name=kb_name, department_id=department_id,
            documents=documents, chat_fn=chat_fn, granularity=granularity,
            max_pages_per_ingest=max_pages, progress_fn=progress,
        )
        with _WIKI_JOB_LOCK:
            WIKI_JOB_STATE[kb_id] = {
                "running": False, "stage": "完成", "error": "",
                "stats": stats, "finished_at": _now(),
            }
    except Exception as exc:  # noqa: BLE001
        with _WIKI_JOB_LOCK:
            WIKI_JOB_STATE[kb_id] = {"running": False, "stage": "失败", "error": str(exc)}


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


@router.post("/kbs/{kb_name}/wiki/ingest", response_model=OkResponse)
def ingest_wiki(
    kb_name: str,
    body: WikiIngestRequest,
    user: AuthUser = Depends(current_user),
    pipeline=Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
):
    ctx, kb_id, department_id = _scope(user, kb_name, "write", auth)
    with _WIKI_JOB_LOCK:
        state = WIKI_JOB_STATE.get(kb_id, {})
        if state.get("running"):
            raise HTTPException(status_code=409, detail="该知识库的 Wiki 生成正在进行中")
        WIKI_JOB_STATE[kb_id] = {"running": True, "stage": "排队中"}
    thread = threading.Thread(
        target=_run_ingest_job,
        args=(kb_id, kb_name, department_id, pipeline, ctx, body.granularity, body.max_pages_per_ingest),
        daemon=True,
    )
    thread.start()
    return OkResponse(ok=True, message="Wiki 生成已启动")


@router.get("/kbs/{kb_name}/wiki/status")
def wiki_status(
    kb_name: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _ctx, kb_id, _department_id = _scope(user, kb_name, "read", auth)
    with _WIKI_JOB_LOCK:
        state = dict(WIKI_JOB_STATE.get(kb_id, {"running": False, "stage": "从未运行"}))
    return state


@router.get("/kbs/{kb_name}/wiki/pages", response_model=list[WikiPageView])
def list_wiki_pages(
    kb_name: str,
    query: str = "",
    page_type: str = Query(default="", pattern="^(summary|entity|concept|index)?$"),
    status: str = Query(default="published", pattern="^(draft|published|archived)?$"),
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _ctx, kb_id, department_id = _scope(user, kb_name, "read", auth)
    return [
        WikiPageView(**row)
        for row in _service().list_pages(kb_id=kb_id, department_id=department_id, query=query, page_type=page_type, status=status)
    ]


@router.get("/kbs/{kb_name}/wiki/pages/{slug:path}", response_model=WikiPageDetailView)
def get_wiki_page(
    kb_name: str, slug: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _ctx, kb_id, department_id = _scope(user, kb_name, "read", auth)
    page = _service().get_page(kb_id=kb_id, department_id=department_id, slug=slug)
    if page is None:
        raise HTTPException(status_code=404, detail="wiki page not found")
    return WikiPageDetailView(**page)


@router.post("/kbs/{kb_name}/wiki/pages", response_model=WikiPageView)
def create_wiki_page(
    kb_name: str, body: WikiPageUpsertRequest,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _ctx, kb_id, department_id = _scope(user, kb_name, "write", auth)
    try:
        page = _service().upsert_manual(
            kb_id=kb_id, department_id=department_id, kb_name=kb_name, actor_user_id=user.id,
            title=body.title, page_type=body.page_type, slug=body.slug, content=body.content,
            summary=body.summary, aliases=body.aliases, status=body.status,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WikiPageView(**page)


@router.patch("/kbs/{kb_name}/wiki/pages/{slug:path}", response_model=WikiPageView)
def update_wiki_page(
    kb_name: str, slug: str, body: WikiPageUpdateRequest,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _ctx, kb_id, department_id = _scope(user, kb_name, "write", auth)
    try:
        page = _service().update_page(
            kb_id=kb_id, department_id=department_id, slug=slug, actor_user_id=user.id,
            fields=body.model_dump(exclude_none=True, exclude_unset=True),
            expect_version=body.version,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WikiPageView(**page)


@router.post("/kbs/{kb_name}/wiki/pages/{slug:path}/archive", response_model=WikiPageView)
def archive_wiki_page(
    kb_name: str, slug: str,
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _ctx, kb_id, department_id = _scope(user, kb_name, "write", auth)
    try:
        page = _service().archive_page(kb_id=kb_id, department_id=department_id, slug=slug, actor_user_id=user.id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WikiPageView(**page)


@router.post("/kbs/{kb_name}/wiki/pages/{slug:path}/revert", response_model=WikiPageView)
def revert_wiki_page(
    kb_name: str, slug: str, version: int = Query(ge=1),
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _ctx, kb_id, department_id = _scope(user, kb_name, "write", auth)
    try:
        page = _service().revert_page(
            kb_id=kb_id, department_id=department_id, slug=slug, version=version, actor_user_id=user.id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WikiPageView(**page)


@router.get("/kbs/{kb_name}/wiki/graph", response_model=WikiGraphView)
def wiki_graph(
    kb_name: str, limit: int = Query(default=200, ge=1, le=1000),
    user: AuthUser = Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
):
    _ctx, kb_id, department_id = _scope(user, kb_name, "read", auth)
    return WikiGraphView(**_service().get_graph(kb_id=kb_id, department_id=department_id, limit=limit))
