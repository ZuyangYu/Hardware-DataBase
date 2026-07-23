# Task 1 implementation report

## Delivered

Commit `f30a0cd01d312f818a57cc6663742903bee1f74f` adds a hash-bound
`TemplateSanitizationReport` and persists raw source bytes separately from the
safe renderable template. `save_sanitized_template` verifies source/sanitized
hashes, conversion compatibility, a clean security report, and uniqueness of
the template version before storing the safe derivative under `templates/` and
the audit-only source under `template_sources/`. The sanitization record is
retrievable by template version ID.

## Changed files

- `src/document_authoring/models.py`
- `src/document_authoring/work_order_store.py`
- `tests/test_template_sanitization_store.py`

## TDD and verification

- RED: `.venv/bin/python -m pytest tests/test_template_sanitization_store.py -q`
  failed during collection because `TemplateSanitizationReport` did not exist.
- GREEN: `.venv/bin/python -m pytest tests/test_template_sanitization_store.py tests/test_template_analysis_contracts.py -q`
  passed: `13 passed in 1.42s`.
- Lint: `.venv/bin/ruff check src/document_authoring/models.py src/document_authoring/work_order_store.py tests/test_template_sanitization_store.py`
  passed: `All checks passed!`.
- Syntax: `.venv/bin/python -m compileall -q src/document_authoring/models.py src/document_authoring/work_order_store.py`
  passed (exit 0).
- Diff review: `git diff --check` passed (exit 0).

## Concerns

None. The two pre-existing user-owned modifications in `src/evaluation/ragas_adapter.py`
and `tests/evaluation/test_ragas_adapter.py` were left unstaged and were not
included in the commit.

## Reviewer-finding fix

Commit pending: `save_sanitized_template` now rejects a security report marked
`clean` when it still lists macro, external-link, or embedded parts. Source and
safe template files are created exclusively with an atomic hard-link publish;
if any database write fails, the transaction is rolled back and only files
created by that call are removed. An existing source file is never replaced.

### Tests

- RED: `.venv/bin/python -m pytest tests/test_template_sanitization_store.py -q`
  produced the five expected regression failures before the implementation.
- GREEN: `.venv/bin/python -m pytest tests/test_template_sanitization_store.py tests/test_template_analysis_contracts.py -q`
  passed: `18 passed in 2.14s`.
- Lint: `.venv/bin/ruff check src/document_authoring/work_order_store.py tests/test_template_sanitization_store.py`
  passed: `All checks passed!`.
- Whitespace: `git diff --check` passed.
