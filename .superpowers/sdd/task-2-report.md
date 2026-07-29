# Task 2 report — ICD profile gate and frozen scope

## Delivered

- The KB auto-generation path classifies the immutable template before any ICD evidence retrieval.
- An `icd_sample` template now returns `template_contract_review_required` with a blocking `icd_formal_template_required` issue; retrieval and rendering do not begin.
- A formal `icd` template scopes circuit pin mapping to `IcdTemplateProfile.connector_refdes`. Supporting retrieval and circuit mapping calls still use the work order's frozen source names.
- Generic templates retain the prior schema/evidence/front-view candidate behavior.
- The final validation path independently appends blocking `icd_formal_template_required` for a sample template, protecting direct/finalization entry points that bypass the pipeline gate.
- The UI presents the template-contract stop as an ICD-template requirement rather than claiming that a candidate file exists.

## TDD evidence

RED (`uv run python -m pytest -q tests/test_icd_scope_pipeline.py tests/test_icd_login_flow_regression.py`):

- Three new profile/finalization checks failed before production changes: no template-contract stop, schema-derived connector scope (`J7`) instead of Profile scope, and no finalization blocker.
- The UI regression test then failed before its small presentation branch was added because it reported a candidate file for the new stop stage.

GREEN:

- `uv run python -m pytest -q tests/test_icd_profile.py tests/test_icd_scope_pipeline.py tests/test_icd_login_flow_regression.py` — 35 passed.
- `uv run python -m pytest -q tests/test_icd_validation.py tests/test_document_generation_page.py tests/test_document_authoring_p2a.py tests/test_icd_front_view.py` — 45 passed.
- `uv run ruff check src/core/app_pipeline.py src/document_authoring/service.py src/ui/document_generation_page.py tests/test_icd_scope_pipeline.py tests/test_icd_login_flow_regression.py` — passed.
- `git diff --check` — passed.

## Scope notes

The worktree contained unrelated pre-existing modifications in the files touched by this task. The commit stages only Task 2 hunks plus this report; no unrelated change was reverted or staged.
