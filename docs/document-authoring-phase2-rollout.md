# Conversation-led document authoring Phase 2 rollout

Status: pilot-ready in offline/test mode. Production execution remains
disabled until the manual failure-boundary record is approved. This runbook
covers the template-backed XLSX/XLSM plan route only; legacy Work Orders and
conversational export keep their existing paths.

Related records:

- Design: `docs/superpowers/specs/2026-09-08-conversation-led-document-authoring-design.md`
- Implementation plan: `docs/superpowers/plans/2026-09-08-conversation-led-document-authoring-phase2.md`
- Parity thresholds: `docs/superpowers/specs/2026-09-08-document-authoring-phase2-parity-thresholds.md`
- Threshold input: `tests/fixtures/document_authoring/icd_pin_definition/baseline_thresholds.json`

## Safe defaults and allowlisting

The deployment defaults are:

```dotenv
DOCUMENT_PLAN_DAG_EXECUTION_ENABLED=false
DOCUMENT_PLAN_DAG_ALLOWLIST_TENANTS=
DOCUMENT_PLAN_DAG_ALLOWLIST_DOCUMENT_TYPES=
DOCUMENT_PLAN_DAG_ALLOWLIST_FORMATS=
```

The same values are present in `.env.example` and `src/settings.py`'s
`DEFAULT_VALUES`. An accepted plan reference is retained for audit while the
flag is off; it does not change legacy route selection. When the flag is on,
the runtime requires all three exact allowlist matches: tenant, OutputSpec
document type, and the single primary deliverable format. A missing or partial
plan binding, stale/non-accepted OutputSpec or plan, hash mismatch, template
layout mismatch, or failed source binding fails closed before retrieval and
never falls back to the legacy graph.

Do not add an allowlist entry until the parity threshold command below passes,
the fixture provenance has been reviewed, and the manual smoke table has an
operator/date/result entry. The threshold file is a required, versioned gate
input; missing, mismatched, or inconclusive metrics are failures, not passes.

For a controlled pilot, set values only in the deployment environment (do not
edit committed `.env` secrets):

```dotenv
DOCUMENT_PLAN_DAG_EXECUTION_ENABLED=true
DOCUMENT_PLAN_DAG_ALLOWLIST_TENANTS=pilot-tenant
DOCUMENT_PLAN_DAG_ALLOWLIST_DOCUMENT_TYPES=icd
DOCUMENT_PLAN_DAG_ALLOWLIST_FORMATS=xlsx
```

Start with XLSX. Add `xlsm` only after package-preservation and macro-policy
checks pass for the exact renderer/template policy used by the pilot.

## Identity, persistence and worker ordering

An enabled run records one immutable `CompiledTaskGraph` and one
`HarnessRun`/`AuthoringRunManifest` bound to the accepted OutputSpec,
DocumentPlan, frozen source snapshot, template/schema, policy, adapter and
renderer hashes. The graph order is:

```text
preflight → ready unit fan-out → declared barriers → aggregate
  → pre-render review → render → post-render review → release
```

Only ready independent units are fanned out, bounded by
`HarnessPolicy.max_parallel_units`. Structural/barrier nodes run in stable
graph order. A committed unit Receipt is written with its validated draft
before the graph Receipt, so a worker replacement cannot re-call the Writer
for a unit whose business fact is already committed. Receipt replay resumes
the same run and graph; it does not create another Work Order, run, draft, or
artifact. Lease ownership and fencing remain the authority for writes.

The current supported production deployment is the existing single-process
SQLite mode. A multi-worker deployment requires the already documented shared
transactional-store/checkpointer migration; do not scale this route by merely
starting another process against local SQLite.

## Review, rework and release states

Coverage is computed from the immutable plan contract, including required
scalar/paragraph requirements, typed table row keys, required columns,
row/cell evidence ownership, and cross-unit requirements. Unit review may
return `pass`, `rework`, `needs_human`, or `blocked`; attempts are bounded by
the task policy. Document review is independent at `pre_render` and
`post_render`; release is allowed only when both reports are bound to the
same model/plan/artifact hashes and have no required issue.

Required missing/duplicate/unexpected rows, unsupported or foreign evidence,
source-policy violations, fixed/formula/merged-cell changes, malformed OOXML,
unsafe XLSM package changes, and unresolved review issues must not
auto-release. A candidate may be stored as `review_candidate` for Gate 2,
but it is not an approved release. A pure layout issue may rerun binding and
rendering; a localized semantic issue may rerun its affected unit path. An
unlocalized or exhausted issue becomes human review/blocked rather than an
unbounded loop.

## Parity gate

Run the offline benchmark from the repository root:

```bash
./.venv/bin/pytest \
  tests/test_document_authoring_parity.py \
  tests/test_icd_pin_definition_benchmark.py -q
```

The gate is versioned as `icd-pin-definition-v1` with threshold file
`icd-pin-definition-thresholds-v1` and fixture
`icd-pin-definition-baseline-v1`. The recorded synthetic baseline is 1.0 for
row-key precision/recall/F1, required-column completeness, exact/relative
order, cross-field consistency, evidence support, and physical protection;
duplicate, missing, and extra row rates are 0.0. XLSX and XLSM are compared
logically; the synthetic XLSM sentinel is inspected but never executed.

For a direct threshold-file check, use the benchmark test above. Do not
replace a missing baseline with a hand-entered pass or compare customer
workbooks byte-for-byte.

## Manual failure-boundary smoke matrix

Run the automated offline equivalents first:

```bash
./.venv/bin/pytest \
  tests/test_conversation_led_document_planning_e2e.py \
  tests/test_document_plan_confirmation.py \
  tests/test_document_plan_submission_worker.py \
  tests/test_document_authoring_durable_resume.py \
  tests/test_result_export.py \
  tests/test_result_export_api.py -q

./.venv/bin/pytest \
  tests/test_conversation_led_document_authoring_phase2_e2e.py \
  tests/test_document_review_gates.py \
  tests/test_document_artifact_review.py -q
```

Then record the operator-controlled test in the deployment change record:

| Boundary | Required observation | Result/date/operator |
| --- | --- | --- |
| Flag off | Legacy Work Order status, fingerprint and artifact behavior unchanged | Pending production operator sign-off |
| Accepted plan | One accepted spec/plan, graph, run and Work Order lineage | Covered offline; production sign-off pending |
| Worker restart | Same checkpoint/Receipt/run; no duplicate draft or artifact | Covered by durable-resume/E2E tests; production sign-off pending |
| Permission revocation | Dispatch fails closed before retrieval/job execution | Covered by Phase 0–1 worker test; production sign-off pending |
| Plan/template/source mutation | Exact hash mismatch blocks; no legacy fallback | Covered by contract/E2E tests; production sign-off pending |
| Missing/duplicate row | Review or block state; no automatic release | Covered by coverage/review/E2E tests; production sign-off pending |
| Static/formula/merge/package mutation | Artifact gate rejects the change | Covered by renderer/artifact tests; production sign-off pending |
| Semantic vs layout issue | Bounded unit rework vs binding-only rework | Covered by review-gate tests; production sign-off pending |
| Deep links/export | `task`, `session`, `workOrder` restore the same task; current-answer export remains `ExportJob` | Covered by frontend/API regressions; production sign-off pending |
| XLSX/XLSM parity | Threshold file and package policy pass without Excel/macros | Covered offline; production sign-off pending |

The repository verification record below is evidence for the offline/test
mode only. It is intentionally not a claim that a live worker was killed or
that a production API/UI session was manually exercised.

## Verification record (2026-09-08)

- Phase 2 focused matrix: 79 passed.
- Backend compatibility matrix: 258 passed, one pre-existing OpenTelemetry
  `LoggingHandler` deprecation warning.
- Full backend suite: 1,961 passed, 9 skipped, 43 subtests passed, with five
  existing warnings (LangGraph `Send`, OpenTelemetry logging, two fork safety
  warnings, and the duplicate-OOXML-member test warning). The first run
  exposed a legacy minimal-manifest stub boundary; the runtime now defaults a
  missing additive route field to `legacy_schema`, and the clean rerun passed.
- Phase 2 plus settings matrix: 86 passed.
- Frontend Vitest: 151 passed. Existing SSR `useLayoutEffect` warnings were
  emitted by the test renderer.
- Frontend production build: passed; Vite reported the existing large-chunk
  advisory.
- Python compilation and `git diff --check`: passed.
- No external model calls, Excel process, or macro execution are required by
  the parity/E2E fixtures.

## Rollback and remediation

1. Set `DOCUMENT_PLAN_DAG_EXECUTION_ENABLED=false` for new runs and reload
   the service configuration.
2. Keep all allowlists empty while investigating. Existing accepted plan,
   graph, run, Receipt, review, artifact and manifest rows remain readable;
   do not delete them and do not mutate their hashes.
3. Inspect the recorded `execution_route`, graph/run/plan hashes, review
   issue codes, validation report and artifact hash. A stale plan/template or
   source mismatch requires a new plan version and explicit reconfirmation,
   not an in-place edit.
4. For lease/replay problems, stop duplicate local workers, let the lease
   expire or adopt it through the normal fenced worker path, and resume the
   existing run. Verify committed Receipt/draft/artifact counts before
   retrying.
5. Restore the allowlist only after the parity file, manual smoke record and
   remediation evidence are reviewed again. XLSM remains independently
   gated.

## Operator handoff

Phase 3 is not enabled by this document. Do not add template-free
`system_recipe`/`generated_structure` rendering or loosen the XLSX/XLSM
template safety gates as part of rollout. The next change should attach the
manual operator record and a separate Phase 3 plan if broader layout support
is approved.
