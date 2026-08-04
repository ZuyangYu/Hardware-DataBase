# Template Review Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make abnormal workbook-template analysis correctable and confirmable without re-upload, while deterministically removing obvious fixed-content and layout-blank LLM mappings.

**Architecture:** Add a pure proposal-normalization pass between LLM parsing and activation policy, persist its diagnostics in the immutable analysis, and add a Streamlit review form backed by the existing hash-bound correction and confirmation services. Repeating tables remain explicitly unsupported until a typed table schema and renderer are implemented.

**Tech Stack:** Python 3.11+, Pydantic v2, SQLite, Streamlit, pytest.

## Global Constraints

- Preserve existing DOCX behavior.
- Preserve unrelated dirty-worktree changes.
- Fixed labels and layout-only blank cells cannot become automatic targets.
- Non-empty overwrite authorization is explicit and per target.
- Repeating tables cannot activate without an explicit table schema.
- Corrections remain bound to analysis ID, sanitized template hash, and actor.

---

### Task 1: Deterministic Suggestion Normalization

**Files:**
- Modify: `src/document_authoring/template_analysis.py`
- Modify: `src/document_authoring/template_suggester.py`
- Test: `tests/test_template_suggester_safety.py`

**Interfaces:**
- Produces: `TemplateAnalysis.dropped_suggestion_targets`.
- Produces: `normalize_template_suggestions(analysis, suggestions)`.

- [ ] Write failing tests using ICD-like instruction, separator, isolated blank,
      example-value, placeholder, and repeating-table targets.
- [ ] Run `pytest -q tests/test_template_suggester_safety.py` and verify the new
      tests fail for the missing normalization behavior.
- [ ] Implement deterministic normalization and persist dropped-target
      diagnostics without mutating the workbook inventory.
- [ ] Run the focused tests and existing suggester tests.

### Task 2: Actionable Streamlit Review and Correction

**Files:**
- Modify: `src/ui/document_generation_page.py`
- Test: `tests/test_document_generation_review_ui.py`
- Test: `tests/test_document_generation_page.py`

**Interfaces:**
- Consumes: `get_document_template_analysis_for_review`.
- Consumes: `correct_document_template_analysis`.
- Consumes: `confirm_document_template`.

- [ ] Write failing UI tests for loading a pending analysis, selecting supported
      scalar mappings, granting overwrite consent, submitting a correction,
      and confirming the returned revision.
- [ ] Run the focused UI tests and verify RED.
- [ ] Implement a session-backed review panel with stable widget keys and
      localized explanations for every reason code.
- [ ] Keep unsupported repeating tables visibly blocked instead of silently
      accepting them.
- [ ] Run focused UI tests and verify GREEN.

### Task 3: Real Analysis Regression and Compatibility

**Files:**
- Modify: `tests/test_icd_template_regression.py`
- Modify only if required: implementation files from Tasks 1-2.

**Interfaces:**
- Verifies analysis `analysis-a6a0d3ef7f1545a38f782c693758ad54`
  behavior through an equivalent persisted payload.

- [ ] Add a regression proving standalone instruction, separator, and layout
      blank suggestions are removed while intended value and repeating-table
      candidates remain reviewable.
- [ ] Run:
      `pytest -q tests/test_template_suggester_safety.py tests/test_document_generation_review_ui.py tests/test_document_generation_page.py tests/test_icd_template_regression.py`.
- [ ] Run the document-authoring compatibility suite.
- [ ] Run `python -m compileall -q src tests` and `git diff --check`.
- [ ] Request code review and resolve every critical or important finding with
      a failing regression test first.

