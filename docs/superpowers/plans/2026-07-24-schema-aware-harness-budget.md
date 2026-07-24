# Schema-Aware Harness Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create bounded, schema-sized internal-Harness policies so a 122-field approved schema no longer fails before automatic generation starts.

**Architecture:** Add a per-unit retrieval-attempt limit while preserving `max_retrieval_rounds` as the global counter. When no explicit policy ID is selected, the service calculates, persists, and freezes a policy from the approved schema's unit count.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, SQLite.

## Global Constraints

- Existing policies default to two retrieval attempts per semantic unit.
- Explicit policy IDs retain their exact stored policy and version.
- Derived policies reject schemas above 200 units, 400 total retrieval calls, or 1,000 steps.
- The failed work order remains blocked; a new work order receives a new frozen policy.
- Stage only this feature's hunks because the worktree is already dirty.

---

### Task 1: Split retry and global retrieval budgets

**Files:**

- Modify: `src/document_authoring/models.py:324-350`
- Modify: `src/document_authoring/harness/graph.py:186-203`
- Modify: `src/document_authoring/harness/runtime.py:54-87`
- Test: `tests/test_document_authoring_p2a.py`

**Interfaces:**

- Produces `HarnessPolicy.max_retrieval_attempts_per_unit: int = 2`.
- Produces `AuthoringRunManifest.max_retrieval_attempts_per_unit: int | None`.
- Keeps `HarnessPolicy.max_retrieval_rounds` as the global retrieval-call cap.

- [ ] **Step 1: Write failing retry tests**

```python
def test_harness_limits_retrieval_attempts_per_unit():
    # Create a one-field internal-harness work order with a policy whose
    # max_retrieval_attempts_per_unit is 2 and global budget is 4.
    attempts = []
    def retrieve(_requirement, attempt):
        attempts.append(attempt)
        return RetrievalOutcome(requirement_id="requirement", status="retrieval_failed")

    service.run_internal_harness(ctx, order.work_order_id, retrieve=retrieve)

    assert attempts == [1, 2]
```

Add a two-field test with global budget 4 where the first field succeeds on attempt 2 and the second on attempt 1. Assert calls are `[1, 2, 1]` and the manifest records both budgets.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_document_authoring_p2a.py -k 'retrieval_attempt' -q
```

Expected: failure because the model, manifest, and graph have no separate per-unit retry limit.

- [ ] **Step 3: Implement the split**

```python
# HarnessPolicy
max_retrieval_attempts_per_unit: int = 2

# AuthoringGraph._retrieve_with_budget
for attempt in range(1, self.policy.policy.max_retrieval_attempts_per_unit + 1):
    self._step(state, "retrieve_requirement_evidence")
    state["retrieval_round_count"] += 1
    self.policy.require_retrieval_round(state["retrieval_round_count"])
```

Add the field to `AuthoringRunManifest`, validate it is positive with the other policy budgets, and copy it in `InternalDocumentHarnessRuntime.build_manifest()`.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/test_document_authoring_p2a.py -k 'retrieval_attempt' -q
```

Expected: all new retry tests pass.

### Task 2: Derive a bounded policy from an approved schema

**Files:**

- Modify: `src/document_authoring/service.py:538-611,1004-1011`
- Test: `tests/test_template_upload_service.py`

**Interfaces:**

- Produces `DocumentGenerationService._schema_harness_policy(schema: DocumentSchema) -> HarnessPolicy`.
- Uses server-owned caps: 200 units, 400 retrieval calls, and 1,000 steps.

- [ ] **Step 1: Write failing policy tests**

```python
policy = service._schema_harness_policy(schema_with_122_fields)

assert policy.max_units_per_run == 122
assert policy.max_retrieval_attempts_per_unit == 2
assert policy.max_retrieval_rounds == 244
assert policy.max_steps == 612
assert policy.status == "approved"
```

Add a 201-field test that expects `ValueError` matching `schema semantic unit count exceeds automatic-generation capacity`.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest tests/test_template_upload_service.py -k 'schema_harness_policy' -q
```

Expected: failure because the helper does not exist.

- [ ] **Step 3: Implement derivation and selection**

```python
unit_count = len(schema.fields) + len(schema.review_items)
attempts = 2
retrieval_rounds = unit_count * attempts
max_steps = 2 + unit_count * (attempts + 3)
```

Reject any calculated budget over the server-owned caps. Persist an approved policy with ID `schema-{document_schema_id}-{version}-managed-writer` and version `units-{unit_count}-attempts-{attempts}`.

In `_create_frozen_work_order()`, call this helper only when `harness_policy_id` is absent. Keep the existing explicit-policy lookup and the frozen policy version assignment unchanged.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/test_template_upload_service.py -k 'schema_harness_policy or frozen_internal_harness_policy' -q
```

Expected: new tests pass and the explicit-policy compatibility test still freezes version `1`.

### Task 3: Prove a large work order freezes the derived policy

**Files:**

- Test: `tests/test_knowledge_base_document_work_orders.py`

**Interfaces:**

- Consumes `DocumentGenerationService.create_knowledge_base_work_order()`.
- Produces a new work order with a policy sized for 122 units without running a retriever or writer.

- [ ] **Step 1: Write the failing end-to-end test**

```python
order = service.create_knowledge_base_work_order(
    ctx,
    knowledge_base_name="hardware",
    source_names=["spec.pdf"],
    template_version_id=approved_template.template_version_id,
    document_schema_id=large_schema.document_schema_id,
    document_schema_version=large_schema.version,
)
policy = service.store.get_harness_policy(
    order.harness_policy_id, order.harness_policy_version,
)

assert order.harness_policy_version == "units-122-attempts-2"
assert policy.max_units_per_run == 122
assert policy.max_retrieval_rounds == 244
assert policy.max_steps == 612
```

- [ ] **Step 2: Verify RED then GREEN**

```bash
uv run pytest tests/test_knowledge_base_document_work_orders.py -k 'large_schema' -q
```

Expected before implementation: the old default policy is selected. Expected after Tasks 1-2: one passing test.

- [ ] **Step 3: Verify all affected behaviour**

```bash
uv run pytest tests/test_document_authoring_p2a.py tests/test_template_upload_service.py tests/test_knowledge_base_document_work_orders.py -q
uv run ruff check src/document_authoring/models.py src/document_authoring/harness/graph.py src/document_authoring/harness/runtime.py src/document_authoring/service.py tests/test_document_authoring_p2a.py tests/test_template_upload_service.py tests/test_knowledge_base_document_work_orders.py
```

Expected: both commands exit 0.
