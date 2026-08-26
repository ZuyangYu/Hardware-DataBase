# Task 2 Report: API 模板+选项端点 (templates + options endpoints)

## Status: DONE_WITH_CONCERNS

## What was implemented

Created the new document-generation API router and wired it into the API layer, thin and delegating to `AppPipeline` exactly per the existing route convention (`build_context_for_user` + `reject_system_admin_kb_access` + `has_kb_permission`).

Files:
- **Created** `src/api/routes/document_generation.py` — `APIRouter(tags=["document-generation"])` with 4 endpoints:
  - `POST /document-generation/templates/analyze` -> `TemplateAnalysisView` (multipart file + `template_name` form; maps an `analysis` object to a safe view that never echoes raw bytes or OOXML locators).
  - `GET /document-generation/templates/{template_version_id}/sanitization`
  - `POST /document-generation/templates/{analysis_id}/confirm` (body `ConfirmTemplateRequest`)
  - `GET /document-generation/options`
  - Shared `_ctx` helper builds a `RequestContext`, rejects system_admin (403), and enforces KB read permission.
- **Modified** `src/api/schemas.py` — appended `TemplateUnitView`, `TemplateSuggestionView`, `TemplateAnalysisView`, `ConfirmTemplateRequest` DTOs.
- **Modified** `src/api/routes/__init__.py` — added `from src.api.routes import document_generation  # noqa: F401`.
- **Modified** `src/api/app.py` — added `document_generation` to the route import block and `app.include_router(document_generation.router, prefix=api_v1)`.
- **Modified** `tests/_api_stub.py` — extended `StubPipeline` with the full document-generation method set (options / analyze / sanitization / confirm / work-orders / run status / prepare / submit / icd scope / feedback / approve / preview / download / pause-harness / cancel-harness) for reuse by later tasks.
- **Created** `tests/test_document_generation_api_templates.py` — 3 tests (analyze safe-view, system_admin blocked, options read-permission).

## TDD results

- **RED**: All 3 tests failed with `404 {"detail":"Not Found"}` (routes not yet registered) — confirmed expected failure before implementation.
- **GREEN**: After wiring, all 3 pass. `uv run python -m pytest tests/test_document_generation_api_templates.py -v` -> `3 passed`.
- Regression: `uv run python -m pytest tests/test_api_routes.py tests/test_document_generation_api_templates.py -q` -> `29 passed`.
- Lint: `uv run ruff check` on all changed files -> `All checks passed!`.

## Key deviation / concern (NEEDS_CONTEXT)

The brief's Step 4 router code and Step 2 test are **internally inconsistent** over the URL path.

- The brief's verbatim router decorators are `/templates/analyze`, `/templates/{version_id}/sanitization`, `/templates/{analysis_id}/confirm`, `/options`, and its Step 5 registration is `app.include_router(document_generation.router, prefix=api_v1)` — which would produce `/api/v1/templates/analyze` and `/api/v1/options`.
- The brief's test (and its "Interfaces" section) clearly require `/api/v1/document-generation/templates/analyze` and `/api/v1/document-generation/options`.

The test is the authoritative spec, so I resolved it by adding the `/document-generation` segment to the router's route decorators (`/document-generation/templates/analyze`, etc.), keeping `prefix=api_v1` exactly as the brief's Step 5 states. This matches the existing convention in `files.py`, where each router embeds its full sub-path in its decorators and is included with `prefix=api_v1`. If the intended design was instead a router-level sub-prefix (e.g. `include_router(..., prefix=f"{api_v1}/document-generation")` keeping the brief's short decorators), the paths are equivalent — but the two differ in code shape, so a reviewer should confirm which was wanted.

## Other notes

- `src/api/app.py` was listed in the brief's commit step and is committed. It carried pre-existing uncommitted worker-spawn/lifespan edits (a "not yours" change) that are now part of this commit, as the brief's commit list implies (app.py is explicitly included). The other dirty files (frontend/*, src/document_authoring/*, tests/test_document_authoring_p2a.py) remain untouched and uncommitted.
- `test_template_analyze_system_admin_blocked` passes with the `setUp` raising `AUTH_DEFAULT_ADMIN_PASSWORD = "StrongTestPassword123!"` before `make_auth`, matching the `test_api_routes.py` pattern — no further login fix needed.
- Commit: `33fddc0 feat: document-generation API templates + options endpoints`.

## Files changed

- `src/api/routes/document_generation.py` (new)
- `src/api/schemas.py`
- `src/api/routes/__init__.py`
- `src/api/app.py`
- `tests/_api_stub.py`
- `tests/test_document_generation_api_templates.py` (new)

## Review follow-up fix — confirm_template permission mapping

Reviewer finding (plan-mandated): `confirm_template` mapped `PermissionError` → HTTP 400, but a permission failure should be 403. Fixed in `src/api/routes/document_generation.py` by splitting the exception handler:

- `PermissionError` → `HTTPException(status_code=403, detail=str(exc))`
- `ValueError` / `KeyError` → `HTTPException(status_code=400, detail=str(exc))`

Verification:
- `uv run python -m pytest tests/test_document_generation_api_templates.py tests/test_api_routes.py -q` -> `29 passed in 25.06s`.
- `uv run ruff check src/api/routes/document_generation.py` -> `All checks passed!`.

Commit: `97dc48e fix: confirm_template permission failure returns 403 not 400` (single file, behavior-only; no other files touched, not pushed).