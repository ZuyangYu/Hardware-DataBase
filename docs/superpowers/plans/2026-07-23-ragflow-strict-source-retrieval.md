# RAGFlow Strict Source Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-closed RAGFlow retrieval path that can supply document-generation work orders only with evidence from one frozen RAGFlow source at a time.

**Architecture:** Preserve `RAGFlowBackend.retrieve()` for conversational retrieval, including its compatibility fallback. Add a narrow client primitive that requires a server-side `ragflow_document_id` metadata condition, then add a project-domain adapter that resolves its only allowable remote locator from a frozen `ProcessingArtifact`, verifies every returned chunk and policy locator, and returns an `EvidenceEnvelope` only after those checks. The existing `ProjectEvidenceRetrievalService` remains the authority for outcome aggregation and final source-set checks.

**Tech Stack:** Python 3.12, dataclasses, Pydantic v2, SQLite-backed project store, RAGFlow HTTP API, pytest.

## Global Constraints

- The ordinary `RAGFlowBackend.retrieve()` fallback behavior must not change.
- The strict path accepts remote identity only from a frozen `ProcessingArtifact.backend_locator` with `dataset_id`, `document_id` and `strict_filter_version=1`.
- The strict request uses server-side `ragflow_document_id == document_id`; a missing or unsupported condition is a failure, not a reason to query broadly.
- V1 accepts only a whole-document allow Region Policy whose locator identifies the frozen `document_id`; section/page/range policies produce `filter_unsupported` because the current RAGFlow API cannot filter them before top-k.
- Only a verified empty result may become `success_empty`; unavailable, retrieval, access and filter failures preserve their distinct source-outcome values.
- No UI, writer, Agent or external caller may provide an arbitrary document ID, query filter or filesystem path.
- Real RAGFlow tests are opt-in and must skip without `RAGFLOW_STRICT_POC=1`.

---

## File structure

- Modify `src/pipelines/document_rag/ragflow_backend.py`: exact remote-ID metadata plus a no-fallback client query.
- Create `src/projects/ragflow_retrieval.py`: frozen-locator, chunk and Region Policy validation.
- Modify `src/projects/retrieval.py`: requirement-bound adapter entrypoint.
- Modify `src/document_authoring/service.py`: work-order-bound RAGFlow evidence entrypoint.
- Create `tests/test_ragflow_strict_retrieval.py`: offline protocol and document-boundary tests.
- Modify `tests/test_ragflow_metadata_fallback.py`: explicit conversational fallback regression.
- Create `tests/test_ragflow_strict_poc.py`: opt-in live-server smoke test.
- Create `docs/poc_ragflow_strict_source_retrieval.md`: P0.5-B go/no-go record.

### Task 1: Add the exact RAGFlow metadata and no-fallback query primitive

**Files:**
- Modify: `src/pipelines/document_rag/ragflow_backend.py:505-519, 651-663, 739-754`
- Test: `tests/test_ragflow_strict_retrieval.py`

**Interfaces:**
- Produces: `RAGFlowClient.retrieve_document_strict(question: str, dataset_id: str, document_id: str, top_k: int) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_strict_client_uses_exact_document_metadata_without_fallback():
    client = _RecordingClient([{"data": {"chunks": []}}])

    assert client.retrieve_document_strict("CAN interface", "dataset-a", "remote-a", 7) == []
    assert client.calls[0][2]["metadata_condition"] == {
        "logical_operator": "and",
        "conditions": [{"name": "ragflow_document_id", "comparison_operator": "=", "value": "remote-a"}],
    }
    assert len(client.calls) == 1


def test_submission_stores_remote_document_id_as_metadata():
    backend, client = _submission_backend()
    backend._submit_archived_document("dataset-a", "kb", "spec.pdf", "/tmp/spec.pdf", "docs")
    assert client.updated_metadata["ragflow_document_id"] == "remote-a"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q tests/test_ragflow_strict_retrieval.py -k 'strict_client or submission_stores'`

Expected: FAIL because `retrieve_document_strict` does not exist and upload metadata lacks `ragflow_document_id`.

- [ ] **Step 3: Implement the minimal client and metadata change**

```python
class RAGFlowClient:
    def retrieve_document_strict(
        self, question: str, dataset_id: str, document_id: str, top_k: int,
    ) -> list[dict]:
        if not dataset_id or not document_id:
            raise ValueError("strict retrieval requires dataset_id and document_id")
        return self.retrieve(
            question, dataset_ids=[dataset_id], top_k=top_k,
            metadata_condition={"logical_operator": "and", "conditions": [{
                "name": "ragflow_document_id", "comparison_operator": "=", "value": document_id,
            }]},
        )


# RAGFlowBackend._submit_archived_document, immediately before update_document_metadata:
metadata = self._metadata(kb_name, file_name, source_group, ctx)
metadata["ragflow_document_id"] = document_id
client.update_document_metadata(dataset_id, document_id, metadata)
```

Do not add a retry or a metadata-free fallback to this method.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `.venv/bin/pytest -q tests/test_ragflow_strict_retrieval.py -k 'strict_client or submission_stores'`

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/pipelines/document_rag/ragflow_backend.py tests/test_ragflow_strict_retrieval.py
git commit -m "feat: add strict RAGFlow document query"
```

### Task 2: Build the project-scoped strict RAGFlow adapter

**Files:**
- Create: `src/projects/ragflow_retrieval.py`
- Test: `tests/test_ragflow_strict_retrieval.py`

**Interfaces:**
- Consumes: `ProjectService`, `RAGFlowClient.retrieve_document_strict`, `InformationRequirement`, a frozen `ProcessingArtifact.backend_locator`, and `SourceRegionPolicy`.
- Produces: `StrictRAGFlowRetrievalAdapter.callback(ctx, requirement) -> Callable[[str, list[str], dict[str, str]], list[EvidenceEnvelope]]`.

- [ ] **Step 1: Write failing adapter tests**

```python
def test_adapter_uses_only_frozen_locator_and_returns_envelope(project_fixture):
    outcome = project_fixture.retrieve_strict([
        {"id": "chunk-a", "document_id": "remote-a", "content": "CAN_H",
         "locator": {"section": "CAN"}, "similarity": 0.9},
    ])

    assert outcome.status == "success_with_hits"
    assert outcome.evidences[0].source_version_id == "version-a"
    assert outcome.evidences[0].processing_artifact_id == "artifact-a"
    assert outcome.evidences[0].locator == {"section": "CAN"}
    assert project_fixture.client.strict_calls == [("CAN", "dataset-a", "remote-a", 20)]


@pytest.mark.parametrize("chunk", [
    {"id": "wrong-doc", "document_id": "remote-b", "content": "bad", "locator": {"section": "CAN"}},
    {"id": "no-locator", "document_id": "remote-a", "content": "bad"},
])
def test_adapter_rejects_unverifiable_chunk_scope(project_fixture, chunk):
    outcome = project_fixture.retrieve_strict([chunk])
    assert outcome.status == "retrieval_failed"
    assert outcome.source_outcomes[0].status == "filter_unsupported"


def test_adapter_rejects_region_that_cannot_match_chunk_locator(project_fixture):
    outcome = project_fixture.retrieve_strict([
        {"id": "chunk-a", "document_id": "remote-a", "content": "CAN_H", "locator": {"section": "POWER"}},
    ])
    assert outcome.source_outcomes[0].status == "filter_unsupported"
```

- [ ] **Step 2: Run adapter tests to verify failure**

Run: `.venv/bin/pytest -q tests/test_ragflow_strict_retrieval.py -k adapter`

Expected: FAIL with `ModuleNotFoundError: src.projects.ragflow_retrieval`.

- [ ] **Step 3: Implement frozen locator, region and chunk validation**

```python
class StrictRAGFlowRetrievalAdapter:
    def __init__(self, projects: ProjectService, client: RAGFlowClient, top_k: int = 20):
        self.projects = projects
        self.client = client
        self.top_k = top_k

    def callback(self, ctx: RequestContext, requirement: InformationRequirement):
        def retrieve_one(version_id: str, artifact_ids: list[str], policy_versions: dict[str, str]):
            if len(artifact_ids) != 1:
                raise RetrievalFailedError("strict RAGFlow requires exactly one frozen artifact")
            return self._retrieve_one(ctx, requirement, version_id, artifact_ids[0], policy_versions)
        return retrieve_one

    def _retrieve_one(self, ctx, requirement, version_id, artifact_id, policy_versions):
        artifact = self.projects.store.get_processing_artifact(artifact_id, ctx.tenant_id or "default")
        remote = dict((artifact.backend_locator if artifact else {}) or {})
        dataset_id, document_id = str(remote.get("dataset_id") or ""), str(remote.get("document_id") or "")
        if not artifact or artifact.processor_kind != "ragflow" or not dataset_id or not document_id:
            raise SourceUnavailableError("frozen artifact has no strict RAGFlow locator")
        if remote.get("strict_filter_version") != "1":
            raise FilterUnsupportedError("source has no verified strict RAGFlow metadata")
        policies = self.projects.store.allowed_region_policies(version_id, artifact_id)
        if not policies or any(policy_versions.get(p.region_policy_id) != p.policy_version for p in policies):
            raise RetrievalFailedError("frozen region policy is unavailable")
        if any(dict(policy.locator) not in ({"document_id": document_id}, {"ragflow_document_id": document_id}) for policy in policies):
            raise FilterUnsupportedError("region policy cannot be filtered by RAGFlow before top-k")
        chunks = self.client.retrieve_document_strict(requirement.subject, dataset_id, document_id, self.top_k)
        return [self._to_envelope(chunk, ctx, requirement, version_id, artifact_id, document_id, policies)
                for chunk in chunks]
```

Implement `_to_envelope` to reject a mismatched `chunk["document_id"]` or missing `chunk["id"]`/`chunk["chunk_id"]`. Raise `FilterUnsupportedError` for each case. Build the stable locator as `{ "document_id": document_id, "chunk_id": chunk_id }`, use `to_evidence_envelope` to make the immutable evidence ID, and copy the verified locator into `quote_span`.

- [ ] **Step 4: Run adapter tests to verify success**

Run: `.venv/bin/pytest -q tests/test_ragflow_strict_retrieval.py -k adapter`

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/projects/ragflow_retrieval.py tests/test_ragflow_strict_retrieval.py
git commit -m "feat: add project-scoped strict RAGFlow adapter"
```

### Task 3: Bind the adapter to document generation without changing Q&A retrieval

**Files:**
- Modify: `src/projects/retrieval.py:20-94`
- Modify: `src/document_authoring/service.py:406-430`
- Test: `tests/test_ragflow_strict_retrieval.py`
- Test: `tests/test_ragflow_metadata_fallback.py`

**Interfaces:**
- Produces: `FilterUnsupportedError`, `ProjectEvidenceRetrievalService.retrieve_with_adapter(ctx, requirement, snapshot_id, adapter) -> RetrievalOutcome`.
- Produces: `DocumentGenerationService.retrieve_ragflow_evidence(ctx, work_order_id, requirement, adapter) -> RetrievalOutcome`.

- [ ] **Step 1: Write failing boundary and regression tests**

```python
def test_document_service_uses_strict_adapter_for_frozen_work_order(authoring_fixture):
    outcome = authoring_fixture.service.retrieve_ragflow_evidence(
        authoring_fixture.ctx, authoring_fixture.work_order.work_order_id,
        authoring_fixture.requirement, authoring_fixture.adapter,
    )
    assert outcome.applied_source_set_snapshot_id == authoring_fixture.work_order.source_set_snapshot_id
    assert outcome.status == "success_with_hits"
    assert authoring_fixture.client.global_fallback_calls == 0


def test_project_retrieval_preserves_filter_unsupported_source_outcome(authoring_fixture):
    outcome = authoring_fixture.retrieve_strict_with_locator({"dataset_id": "dataset-a", "document_id": "remote-a"})
    assert outcome.status == "retrieval_failed"
    assert outcome.source_outcomes[0].status == "filter_unsupported"


def test_conversational_retrieve_still_retries_metadata_free_when_compatibility_requires_it():
    evidence = _backend(_Client([[], [_chunk("remote-design")]])).retrieve(
        "kb", "design question", ctx=RequestContext(metadata={"department_id": "dept_a"}),
    )
    assert [item.id for item in evidence] == ["chunk-1"]
```

- [ ] **Step 2: Run boundary tests to verify failure**

Run: `.venv/bin/pytest -q tests/test_ragflow_strict_retrieval.py -k document_service tests/test_ragflow_metadata_fallback.py`

Expected: FAIL because the two bounded service methods do not exist; the conversational regression remains PASS.

- [ ] **Step 3: Implement the bounded service entrypoints**

```python
class ProjectEvidenceRetrievalService:
    # Define FilterUnsupportedError as a RetrievalFailedError subclass.
    def retrieve_with_adapter(self, ctx, requirement, snapshot_id, adapter):
        return self.retrieve(ctx, requirement, snapshot_id, adapter.callback(ctx, requirement))

    # In retrieve(), catch FilterUnsupportedError before RetrievalFailedError:
    # outcomes.append(RetrievalSourceOutcome(source_version_id=version_id,
    #     status="filter_unsupported", error_code=str(exc), retryable=False))


class DocumentGenerationService:
    def retrieve_ragflow_evidence(self, ctx, work_order_id, requirement, adapter):
        order = self._order(ctx, work_order_id, "run_deterministic_work_order")
        if requirement.project_id and requirement.project_id != order.project_id:
            raise PermissionError("requirement project does not match work order")
        bound = requirement.model_copy(update={"project_id": order.project_id, "baseline_id": order.baseline_id})
        return ProjectEvidenceRetrievalService(self.projects).retrieve_with_adapter(
            ctx, bound, order.source_set_snapshot_id, adapter,
        )
```

Do not expose document IDs, source IDs or arbitrary retrieval filters on these methods.

- [ ] **Step 4: Run boundary and regression tests to verify success**

Run: `.venv/bin/pytest -q tests/test_ragflow_strict_retrieval.py -k document_service tests/test_ragflow_metadata_fallback.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/projects/retrieval.py src/document_authoring/service.py tests/test_ragflow_strict_retrieval.py tests/test_ragflow_metadata_fallback.py
git commit -m "feat: bind strict RAGFlow retrieval to document work orders"
```

### Task 4: Add the P0.5-B report and an opt-in live capability gate

**Files:**
- Create: `tests/test_ragflow_strict_poc.py`
- Create: `docs/poc_ragflow_strict_source_retrieval.md`
- Test: `tests/test_ragflow_strict_poc.py`

**Interfaces:**
- Consumes: `RAGFLOW_STRICT_POC=1`, `RAGFLOW_STRICT_DATASET_ID`, `RAGFLOW_STRICT_DOCUMENT_ID`, `RAGFLOW_STRICT_QUERY`, and `RAGFlowClient.retrieve_document_strict`.
- Produces: an explicit go/no-go capability record; no live capability means no P0.5-B completion claim.

- [ ] **Step 1: Write the default-skip live smoke test**

```python
pytestmark = pytest.mark.skipif(
    os.getenv("RAGFLOW_STRICT_POC") != "1",
    reason="set RAGFLOW_STRICT_POC=1 to execute against an approved RAGFlow environment",
)


def test_live_ragflow_document_filter_returns_only_selected_document():
    client = RAGFlowClient()
    chunks = client.retrieve_document_strict(
        os.environ["RAGFLOW_STRICT_QUERY"],
        os.environ["RAGFLOW_STRICT_DATASET_ID"],
        os.environ["RAGFLOW_STRICT_DOCUMENT_ID"], 10,
    )
    selected = os.environ["RAGFLOW_STRICT_DOCUMENT_ID"]
    assert all(str(chunk.get("document_id") or "") == selected for chunk in chunks)
```

- [ ] **Step 2: Run the smoke-test module without credentials**

Run: `.venv/bin/pytest -q tests/test_ragflow_strict_poc.py`

Expected: `1 skipped`.

- [ ] **Step 3: Add the result report**

```markdown
# P0.5-B RAGFlow 严格选源 PoC

## Offline protocol result

Offline tests prove that the document-authoring path sends one frozen RAGFlow document constraint, makes no metadata-free retry, and rejects unverifiable chunks.

## Live capability gate

Run `.venv/bin/pytest -q tests/test_ragflow_strict_poc.py` with `RAGFLOW_STRICT_POC=1` and approved identifiers. Record the date, server version, selected document, neighboring control document, returned document IDs, locator fields and result.

## Go/no-go

Go requires every returned chunk to identify the selected document and carry a stable locator compatible with the approved Region Policy. Missing filter enforcement, any cross-document chunk, missing locator or endpoint error is no-go; retain the strict adapter as unavailable.
```

- [ ] **Step 4: Run final offline verification**

Run: `.venv/bin/pytest -q tests/test_ragflow_strict_retrieval.py tests/test_ragflow_metadata_fallback.py tests/test_document_authoring_p2a.py`

Expected: PASS; `tests/test_ragflow_strict_poc.py` skips unless explicitly enabled.

- [ ] **Step 5: Commit Task 4**

```bash
git add tests/test_ragflow_strict_poc.py docs/poc_ragflow_strict_source_retrieval.md
git commit -m "docs: add RAGFlow strict retrieval PoC gate"
```

## Final verification

- [ ] Run `.venv/bin/pytest -q` and require the full suite to pass.
- [ ] Run `git diff --check` and require no output.
- [ ] If an approved live RAGFlow environment is available, run `RAGFLOW_STRICT_POC=1 .venv/bin/pytest -q tests/test_ragflow_strict_poc.py`; otherwise record no-go pending live validation and do not claim P0.5-B complete.
