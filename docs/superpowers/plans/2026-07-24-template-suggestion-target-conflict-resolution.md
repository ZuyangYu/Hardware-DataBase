# Template Suggestion Target Conflict Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve duplicate LLM template targets deterministically by confidence before automatic activation while preserving strict validation for unknown, non-writable, and still-duplicated targets.

**Architecture:** Split analysis validation into target-safety validation and the existing full validation contract. The LLM provider validates target safety before applying a pure, stable conflict resolver, then runs full validation before mutating the analysis. Activation keeps its existing uniqueness guard as defense in depth.

**Tech Stack:** Python 3.12, Pydantic v2 models, pytest, SQLite-backed document authoring service.

## Global Constraints

- A template target may appear in at most one final suggestion.
- Higher `confidence` wins; equal confidence uses original merged order.
- Retained suggestions and target IDs are emitted in their original order.
- Unknown and non-writable targets must fail before conflict resolution and must never be hidden by deduplication.
- Conflict resolution must not mutate the input list or suggestion objects.
- Conflict resolution must not add LLM requests or modify historical persisted analyses.
- Preserve the activation-time `_regions_and_bindings()` uniqueness check.
- The worktree already contains unrelated uncommitted changes; stage only the exact conflict-resolution hunks and never stage whole unrelated files.

---

### Task 1: Separate Target-Safety and Full Uniqueness Validation

**Files:**
- Modify: `src/document_authoring/template_analysis.py:35-42`
- Test: `tests/test_template_analysis_contracts.py:48-73`

**Interfaces:**
- Produces: `TemplateAnalysis.validate_suggestion_targets() -> None`, which checks target existence and writability only.
- Produces: `TemplateAnalysis.validate_suggestions() -> None`, which calls target-safety validation and additionally rejects a target used more than once across or within suggestions.
- Consumes: `TemplateAnalysis.units` and `TemplateAnalysis.suggestions`.

- [ ] **Step 1: Write the failing duplicate-target contract test**

Add this test after the existing non-writable-target test:

```python
def test_template_analysis_rejects_duplicate_suggestion_targets():
    analysis = TemplateAnalysis(
        analysis_id="analysis-1",
        template_version_id="template-1",
        content_hash="a" * 64,
        format="docx",
        status="ready_for_confirmation",
        units=[
            TemplateAnalysisUnit(
                unit_id="paragraph-1",
                locator={"paragraph_index": 1},
                writable=True,
            ),
        ],
        suggestions=[
            TemplateAnalysisSuggestion(
                semantic_unit_id="summary",
                label="摘要",
                target_unit_ids=["paragraph-1"],
                confidence=0.9,
            ),
            TemplateAnalysisSuggestion(
                semantic_unit_id="detail",
                label="详情",
                target_unit_ids=["paragraph-1"],
                confidence=0.8,
            ),
        ],
    )

    with pytest.raises(ValueError, match="suggestion target may only be used once: paragraph-1"):
        analysis.validate_suggestions()
```

- [ ] **Step 2: Run the contract test and verify RED**

Run:

```bash
uv run pytest tests/test_template_analysis_contracts.py::test_template_analysis_rejects_duplicate_suggestion_targets -q
```

Expected: FAIL because `validate_suggestions()` currently accepts the repeated writable target.

- [ ] **Step 3: Implement the two validation layers**

Replace the current method with:

```python
    def validate_suggestion_targets(self) -> None:
        units = {unit.unit_id: unit for unit in self.units}
        for suggestion in self.suggestions:
            for unit_id in suggestion.target_unit_ids:
                if unit_id not in units:
                    raise ValueError(f"suggestion references unknown analysis unit: {unit_id}")
                if not units[unit_id].writable:
                    raise PermissionError(f"suggestion targets non-writable analysis unit: {unit_id}")

    def validate_suggestions(self) -> None:
        self.validate_suggestion_targets()
        seen_targets: set[str] = set()
        for suggestion in self.suggestions:
            for unit_id in suggestion.target_unit_ids:
                if unit_id in seen_targets:
                    raise ValueError(f"suggestion target may only be used once: {unit_id}")
                seen_targets.add(unit_id)
```

- [ ] **Step 4: Run focused analysis tests and verify GREEN**

Run:

```bash
uv run pytest \
  tests/test_template_analysis_contracts.py::test_template_analysis_rejects_suggested_location_not_in_inventory \
  tests/test_template_analysis_contracts.py::test_template_analysis_rejects_non_writable_suggestion_target \
  tests/test_template_analysis_contracts.py::test_template_analysis_rejects_duplicate_suggestion_targets -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit only Task 1 hunks**

Inspect and stage only the new contract test and validation methods:

```bash
git diff -- src/document_authoring/template_analysis.py tests/test_template_analysis_contracts.py
git add -p -- src/document_authoring/template_analysis.py tests/test_template_analysis_contracts.py
git diff --cached --check
git commit -m "fix: reject duplicate template suggestion targets"
```

Expected: no pre-existing unrelated hunks are staged.

---

### Task 2: Resolve LLM Target Conflicts by Stable Confidence Priority

**Files:**
- Modify: `src/document_authoring/template_suggester.py:71-101`
- Modify: `src/document_authoring/template_suggester.py` near `_build_suggestion_batches()`
- Test: `tests/test_template_suggester.py`

**Interfaces:**
- Consumes: `list[TemplateAnalysisSuggestion]`.
- Produces: `_resolve_target_conflicts(suggestions: list[TemplateAnalysisSuggestion]) -> list[TemplateAnalysisSuggestion]`.
- Uses: `TemplateAnalysis.validate_suggestion_targets()` from Task 1 before conflict resolution.
- Guarantees: fresh retained suggestion objects, unique final targets, stable original output order.

- [ ] **Step 1: Add a response-driven conflict test covering priority, partial retention, empty removal, and stable order**

Add:

```python
def test_llm_suggester_resolves_duplicate_targets_by_confidence_without_reordering():
    analysis = _analysis_with_writable_units(4)
    response = json.dumps([
        {
            "semantic_unit_id": "partial",
            "label": "Partial",
            "target_unit_ids": ["sheet:Budget!A1", "sheet:Budget!A2"],
            "retrieval_terms": [],
            "confidence": 0.5,
        },
        {
            "semantic_unit_id": "winner",
            "label": "Winner",
            "target_unit_ids": ["sheet:Budget!A1"],
            "retrieval_terms": [],
            "confidence": 0.9,
        },
        {
            "semantic_unit_id": "removed",
            "label": "Removed",
            "target_unit_ids": ["sheet:Budget!A1"],
            "retrieval_terms": [],
            "confidence": 0.4,
        },
        {
            "semantic_unit_id": "untouched",
            "label": "Untouched",
            "target_unit_ids": ["sheet:Budget!A3"],
            "retrieval_terms": [],
            "confidence": 0.5,
        },
    ])

    suggestions = LLMTemplateSuggestionProvider(RecordingClient(response)).suggest(analysis)

    assert [item.semantic_unit_id for item in suggestions] == [
        "partial",
        "winner",
        "untouched",
    ]
    assert [item.target_unit_ids for item in suggestions] == [
        ["sheet:Budget!A2"],
        ["sheet:Budget!A1"],
        ["sheet:Budget!A3"],
    ]
    assert analysis.suggestions == suggestions
```

- [ ] **Step 2: Run the conflict test and verify RED**

Run:

```bash
uv run pytest tests/test_template_suggester.py::test_llm_suggester_resolves_duplicate_targets_by_confidence_without_reordering -q
```

Expected: FAIL with `TemplateSuggestionTechnicalFailure` caused by duplicate-target validation.

- [ ] **Step 3: Add equal-confidence and immutability tests**

Add:

```python
def test_target_conflict_resolution_uses_original_order_for_equal_confidence():
    suggestions = [
        TemplateAnalysisSuggestion(
            semantic_unit_id="first",
            label="First",
            target_unit_ids=["unit-a"],
            confidence=0.7,
        ),
        TemplateAnalysisSuggestion(
            semantic_unit_id="second",
            label="Second",
            target_unit_ids=["unit-a", "unit-b"],
            confidence=0.7,
        ),
    ]

    resolved = _resolve_target_conflicts(suggestions)

    assert [item.semantic_unit_id for item in resolved] == ["first", "second"]
    assert [item.target_unit_ids for item in resolved] == [["unit-a"], ["unit-b"]]
    assert suggestions[1].target_unit_ids == ["unit-a", "unit-b"]
    assert all(result is not original for result, original in zip(resolved, suggestions))
```

Update imports:

```python
from src.document_authoring.template_analysis import (
    TemplateAnalysis,
    TemplateAnalysisSuggestion,
    TemplateAnalysisUnit,
)
from src.document_authoring.template_suggester import (
    LLMTemplateSuggestionProvider,
    TemplateSuggestionTechnicalFailure,
    _resolve_target_conflicts,
)
```

- [ ] **Step 4: Run the equal-confidence test and verify RED**

Run:

```bash
uv run pytest tests/test_template_suggester.py::test_target_conflict_resolution_uses_original_order_for_equal_confidence -q
```

Expected: collection ERROR because `_resolve_target_conflicts` does not exist.

- [ ] **Step 5: Implement the pure stable resolver**

Add near the other module-level suggestion helpers:

```python
def _resolve_target_conflicts(
    suggestions: list[TemplateAnalysisSuggestion],
) -> list[TemplateAnalysisSuggestion]:
    resolved: list[TemplateAnalysisSuggestion | None] = [None] * len(suggestions)
    claimed_targets: set[str] = set()
    prioritized = sorted(
        enumerate(suggestions),
        key=lambda item: (-item[1].confidence, item[0]),
    )
    for original_index, suggestion in prioritized:
        target_unit_ids = [
            unit_id
            for unit_id in suggestion.target_unit_ids
            if unit_id not in claimed_targets
        ]
        claimed_targets.update(target_unit_ids)
        if target_unit_ids:
            resolved[original_index] = suggestion.model_copy(
                update={"target_unit_ids": target_unit_ids},
            )
    return [suggestion for suggestion in resolved if suggestion is not None]
```

- [ ] **Step 6: Integrate pre-validation, resolution, and full post-validation**

Change the end of `LLMTemplateSuggestionProvider.suggest()` to:

```python
        try:
            candidate = analysis.model_copy(update={"suggestions": suggestions})
            candidate.validate_suggestion_targets()
            suggestions = _resolve_target_conflicts(suggestions)
            candidate = analysis.model_copy(update={"suggestions": suggestions})
            candidate.validate_suggestions()
        except (ValueError, PermissionError) as exc:
            raise TemplateSuggestionTechnicalFailure("LLM suggestion validation failed") from exc
        analysis.suggestions = suggestions
        return suggestions
```

- [ ] **Step 7: Run resolver and existing invalid-target tests**

Run:

```bash
uv run pytest \
  tests/test_template_suggester.py::test_llm_suggester_resolves_duplicate_targets_by_confidence_without_reordering \
  tests/test_template_suggester.py::test_target_conflict_resolution_uses_original_order_for_equal_confidence \
  tests/test_template_suggester.py::test_invalid_target_is_a_terminal_technical_failure \
  tests/test_template_suggester.py::test_non_writable_target_is_a_terminal_technical_failure -q
```

Expected: `4 passed`.

- [ ] **Step 8: Run the full suggester test module**

Run:

```bash
uv run pytest tests/test_template_suggester.py -q
```

Expected: all tests pass with no additional LLM client calls in existing batching assertions.

- [ ] **Step 9: Commit only Task 2 hunks**

```bash
git diff -- src/document_authoring/template_suggester.py tests/test_template_suggester.py
git add -p -- src/document_authoring/template_suggester.py tests/test_template_suggester.py
git diff --cached --check
git commit -m "fix: resolve template target conflicts by confidence"
```

Expected: no pre-existing progress/resilience hunks are staged.

---

### Task 3: Prove Automatic Activation Creates Unique Bindings

**Files:**
- Test: `tests/test_template_upload_service.py:174-205`

**Interfaces:**
- Consumes: `LLMTemplateSuggestionProvider`, `DocumentGenerationService.analyze_and_activate_uploaded_template()`, and `DocumentAuthoringStore.list_unit_bindings(schema_id, version)`.
- Produces: an end-to-end regression proving a duplicate LLM response is resolved before activation.

- [ ] **Step 1: Add a duplicate-target LLM test client**

Add beside the existing test clients:

```python
class _DuplicateTargetClient:
    def chat(self, messages, **_kwargs):
        unit_id = json.loads(messages[1]["content"])["units"][0]["unit_id"]
        return json.dumps([
            {
                "semantic_unit_id": "low-confidence",
                "label": "Low confidence",
                "target_unit_ids": [unit_id],
                "retrieval_terms": [],
                "confidence": 0.5,
            },
            {
                "semantic_unit_id": "high-confidence",
                "label": "High confidence",
                "target_unit_ids": [unit_id],
                "retrieval_terms": [],
                "confidence": 0.9,
            },
        ])
```

- [ ] **Step 2: Add the automatic-activation regression test**

Add after `test_auto_activation_approves_template_with_valid_suggestions`:

```python
def test_auto_activation_resolves_duplicate_llm_targets_before_creating_bindings(
    authoring_service,
    author_ctx,
):
    authoring_service.template_suggester = LLMTemplateSuggestionProvider(
        _DuplicateTargetClient(),
    )

    template = authoring_service.analyze_and_activate_uploaded_template(
        author_ctx,
        filename="review.docx",
        content=_docx_with_text("Project Summary"),
        template_name="Review",
    )

    analysis = authoring_service.store.get_template_analysis(template.template_version_id)
    bindings = authoring_service.store.list_unit_bindings(
        template.template_schema_id,
        template.template_schema_version,
    )
    assert template.status == "approved"
    assert analysis is not None
    assert [item.semantic_unit_id for item in analysis.suggestions] == ["high-confidence"]
    assert [item.semantic_unit_id for item in bindings] == ["high-confidence"]
    assert len(bindings[0].target_region_ids) == 1
```

- [ ] **Step 3: Temporarily revert the resolver integration and verify the regression test fails**

Before claiming this is a regression test, temporarily restore the pre-fix `suggest()` validation block, run:

```bash
uv run pytest tests/test_template_upload_service.py::test_auto_activation_resolves_duplicate_llm_targets_before_creating_bindings -q
```

Expected: FAIL with `TemplateSuggestionTechnicalFailure` or the original duplicate-binding error. Restore the Task 2 implementation immediately afterward.

- [ ] **Step 4: Run the restored implementation and verify GREEN**

Run:

```bash
uv run pytest tests/test_template_upload_service.py::test_auto_activation_resolves_duplicate_llm_targets_before_creating_bindings -q
```

Expected: `1 passed`.

- [ ] **Step 5: Run the complete affected suite**

Run:

```bash
uv run pytest \
  tests/test_template_analysis_contracts.py \
  tests/test_template_suggester.py \
  tests/test_template_upload_service.py \
  tests/test_document_generation_page.py -q
```

Expected: all affected tests pass.

- [ ] **Step 6: Run lint on changed Python files**

Run:

```bash
uv run ruff check \
  src/document_authoring/template_analysis.py \
  src/document_authoring/template_suggester.py \
  tests/test_template_analysis_contracts.py \
  tests/test_template_suggester.py \
  tests/test_template_upload_service.py
```

Expected: exit code 0 with no diagnostics.

- [ ] **Step 7: Commit only the Task 3 regression test**

```bash
git diff -- tests/test_template_upload_service.py
git add -p -- tests/test_template_upload_service.py
git diff --cached --check
git commit -m "test: cover automatic activation target conflicts"
```

Expected: no pre-existing upload progress or resilience test hunks are staged.

---

### Task 4: Final Verification and Failure-Record Check

**Files:**
- Verify only; no production file changes expected.

**Interfaces:**
- Consumes: all outputs from Tasks 1–3.
- Produces: fresh evidence that the original failure class is prevented without weakening safety checks.

- [ ] **Step 1: Run all document-authoring tests**

Run:

```bash
uv run pytest tests/test_template_*.py tests/test_document_generation_page.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Confirm the final code keeps all three safety layers**

Run:

```bash
rg -n "validate_suggestion_targets|validate_suggestions|suggestion target may only be used once|suggested template targets may only be bound once" \
  src/document_authoring/template_analysis.py \
  src/document_authoring/template_suggester.py \
  src/document_authoring/service.py
```

Expected: pre-resolution target validation in the suggester, full uniqueness validation in analysis, and the existing activation-time guard in the service.

- [ ] **Step 3: Inspect the final diff for scope**

Run:

```bash
git diff HEAD~3 -- \
  src/document_authoring/template_analysis.py \
  src/document_authoring/template_suggester.py \
  tests/test_template_analysis_contracts.py \
  tests/test_template_suggester.py \
  tests/test_template_upload_service.py
```

Expected: only target-safety validation, deterministic conflict resolution, and their regression tests; no unrelated feature changes.

