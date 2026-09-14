# Conversation-led Document Authoring — Production Pilot Runbook

Status: offline pilot implementation complete; production enablement remains a
separate, operator-signed release gate. Repository defaults must remain disabled
and empty. This runbook does not authorize a production rollout by itself.

Design and implementation plan:

- `docs/superpowers/specs/2026-09-08-conversation-led-document-authoring-design.md`
- `docs/superpowers/plans/2026-09-09-conversation-led-document-authoring-phase3-5.md`

## 1. Safety rules

- Never put credentials, prompts, source content, artifact bytes, or provider
  responses in the evidence bundle or this runbook.
- A plan-backed run must retain the accepted `OutputSpec` and `DocumentPlan`
  identifiers, versions, and hashes. It must not fall back to the legacy graph.
- Model output cannot select package parts, OOXML/XML, macros, formulas,
  relationships, arbitrary styles, coordinates, or unregistered components.
- All rollout flags are kill switches. An empty allowlist means no canary scope;
  wildcards and prefix matches are not valid rollout values.
- Do not drop legacy tables, delete historical artifacts, or overwrite a live
  database during this pilot. Retirement requires a separately approved
  migration after the observation gates below.

## 2. Required operator gates

The release owner may proceed only when all of these records exist:

1. The offline evidence bundle is `passed`, its `evidence_hash` is recorded,
   and the bundle still reports `production_enabled: false`.
2. The target deployment has a frozen commit/config snapshot and an operator
   supplied exactly one canary tenant, document type, format, and (for the
   template-free route) recipe version.
3. The pre-change database backup passes SQLite integrity and row-count checks;
   restore has been exercised into a separate safety target.
4. The worker stop/restart drill, API/UI smoke, failure-boundary smoke, and
   resume/idempotency drill have named operators and timestamps.
5. Compatibility telemetry has two release observations with zero new legacy
   writes, verified backup/restore, and old-field read-only evidence before any
   legacy write boundary is retired or closure is treated as ready.
6. The release owner and an independent reviewer sign the final decision.

The code and the offline pilot cannot self-approve any of these gates.

## 3. Offline evidence bundle

Run from the repository root in an isolated test environment. This command
uses temporary SQLite data only; it does not call a model, enqueue a job, or
change deployment settings.

```bash
mkdir -p /tmp/document-authoring-pilot
uv run python -m src.document_authoring.pilot \
  --output /tmp/document-authoring-pilot/evidence.json
```

Record the JSON `evidence_hash`, commit SHA, command output, and the file's
retention location in the release record. A failed check stops the release.
The command covers planning contracts, unknown-recipe failure boundaries,
DOCX/PDF/XLSX parser and visual baselines, lineage reconstruction, and backup /
restore rollback.

## 4. Configuration contract

Keep every variable below at its repository-safe value until the operator has
approved the canary window. Values shown as `<...>` are deployment inputs, not
values to commit to `.env.example`.

```dotenv
DOCUMENT_PLAN_DAG_EXECUTION_ENABLED=false
DOCUMENT_PLAN_DAG_ALLOWLIST_TENANTS=
DOCUMENT_PLAN_DAG_ALLOWLIST_DOCUMENT_TYPES=
DOCUMENT_PLAN_DAG_ALLOWLIST_FORMATS=

DOCUMENT_TEMPLATE_FREE_EXECUTION_ENABLED=false
DOCUMENT_TEMPLATE_FREE_ALLOWLIST_TENANTS=
DOCUMENT_TEMPLATE_FREE_ALLOWLIST_DOCUMENT_TYPES=
DOCUMENT_TEMPLATE_FREE_ALLOWLIST_FORMATS=
DOCUMENT_TEMPLATE_FREE_ALLOWLIST_RECIPES=

DOCUMENT_AUTHORING_COMPATIBILITY_CLOSURE_ENABLED=false
DOCUMENT_AUTHORING_COMPATIBILITY_DB_PATH=<deployment-local-or-approved-shared-path>
```

For the canary, the release owner supplies exact normalized values for the
same tenant/document-type/format tuple in both routes. The template-free
recipe value is exact `recipe_id@version` (for example, `generic_report@1`),
never a bare recipe id. Do not configure a second tenant, format, document
type, or recipe until the current observation window is closed.

The plan DAG and template-free flags are independent. Enabling one does not
enable the other. Compatibility closure is a separate write-boundary gate and
must not be used as a shortcut for rollout approval.

## 5. Pre-change backup and worker fencing

1. Freeze the target commit and save a redacted config snapshot. Confirm that
   the compatibility database path is explicit and is not a test or temporary
   path.
2. Stop every document-authoring worker that can write the target database.
   Record the service-manager command, worker IDs, and the time at which the
   last lease expired. Do not begin the backup while a worker is writing.
3. Create a pre-change backup using the compatibility service's atomic helper
   (or the deployment's equivalent SQLite backup procedure):

   ```python
   from src.document_authoring.compatibility import create_sqlite_backup

   report = create_sqlite_backup(
       database_path="<target-db>",
       backup_path="<target-db>.pilot.pre.db",
   )
   assert report.verified
   print(report.model_dump_json())
   ```

4. Record the backup SHA-256, `integrity_check`, table row counts, database
   path, and backup path. Keep the backup immutable and separately readable by
   the rollback operator.
5. Start the API with all new flags still disabled and verify the health probe.
   Start one canary worker only after the API and database checks pass.

## 6. Canary enablement sequence

Use a deployment secret/configuration store; do not commit the canary values.

1. Deploy the frozen commit with the safe defaults and run the offline pilot.
2. Apply only the operator-approved exact allowlists. Set
   `DOCUMENT_PLAN_DAG_EXECUTION_ENABLED=true` for the approved plan route.
   Set `DOCUMENT_TEMPLATE_FREE_EXECUTION_ENABLED=true` only when the approved
   canary explicitly includes a registered recipe and template-free format.
3. Reload/restart the API and worker so both processes have the same config
   snapshot. Verify the effective values from the redacted configuration
   endpoint/log; never log secrets.
4. Before serving a user, submit a negative request outside each allowlist.
   It must fail closed without creating a Work Order, job, run, or artifact.
5. Serve one authenticated canary user in the approved tenant. Keep automatic
   confirmation and automatic release disabled unless a separately signed
   low-risk policy explicitly allows them.

## 7. Worker, API, and UI smoke

Run the following in order and record request IDs, entity IDs, status codes,
hashes, and timestamps. Redact source content and credentials.

### API smoke

- `GET /health` succeeds after restart.
- `GET /api/v1/document-generation/options?kb=<canary-kb>` succeeds for the
  canary user and does not expose another tenant's options.
- Create a generation session, request a plan proposal, and verify the returned
  card contains the frozen source snapshot, accepted `OutputSpec`/`DocumentPlan`
  references, and hashes.
- Confirm through
  `POST /api/v1/document-generation/sessions/{session_id}/confirm-plan?kb=...`.
  Verify that a plan-backed submission is created only for the accepted hashes.
- Create/submit the Work Order through the existing
  `/api/v1/document-generation/work-orders` route, then call
  `POST /api/v1/document-generation/work-orders/{work_order_id}/generate`.
  Verify the target format, recipe (if applicable), tenant, and plan hashes.
- Read status, preview, and download through the corresponding
  `/api/v1/document-generation/work-orders/{id}/status` and
  `/api/v1/document-generation/artifacts/{id}/preview|download` routes.
  Verify parser/active-content checks and the artifact lineage before approval.

Negative API checks must cover an unknown recipe, unsupported format, stale
plan hash, changed source snapshot, and a direct schema/GenerationBrief or
Work Order write when compatibility closure is enabled. Each must return a
stable client error and leave no partial business fact.

### UI smoke

- Open the document-generation workbench as the canary user and confirm the
  visible tenant/knowledge-base scope is correct.
- Confirm the plan card shows purpose, audience, source snapshot, deliverable,
  layout/recipe, required coverage, approval policy, and the visible hashes.
- Confirm the user can accept the plan, observe progress, open the preview,
  see review/release conditions, and download only the canary artifact.
- Verify a blocked or stale plan displays a recoverable explanation and does
  not offer a misleading generate/approve action.

### Worker restart/idempotency smoke

Stop the canary worker after an accepted plan is persisted and before the final
artifact is acknowledged. Restart the worker and resume the same Work Order.
The result must have one run identity, one artifact fact, deterministic hashes,
and no duplicate release or receipt. Exercise pause/cancel once if the target
worker supports those controls.

## 8. Compatibility closure and observation windows

During every release, collect the durable compatibility counter snapshot and
the legacy marker/read-only projection. At minimum record:

- `legacy_direct_execution`
- `plan_backed_execution`
- `auto_confirmation_attempt`
- `new_write`
- `legacy_write`

Record a release observation with `backup_verified`, `restore_verified`,
`old_fields_read_only`, and the legacy/new write counts. Closure readiness is
not satisfied until two distinct release labels have all of the following:

- zero `legacy_write` events in each release;
- verified backup and restore in each release; and
- old fields/tables are read-only in the final observation.

Historical sessions, Work Orders, artifacts, and runs may receive a legacy
marker with their original source/content fingerprint. A marker must not gain
a fabricated plan ID, version, or hash. Historical rows remain readable for
the full observation window.

## 9. Kill switch and rollback

Rollback is reversible and starts with worker fencing:

1. Stop the canary worker and block new submissions at the deployment edge.
2. Set `DOCUMENT_TEMPLATE_FREE_EXECUTION_ENABLED=false` and
   `DOCUMENT_PLAN_DAG_EXECUTION_ENABLED=false`; clear their allowlists. Reload
   or restart every API and worker process and verify the effective values.
3. Confirm that new canary requests fail closed, while historical reads,
   previews, and audit/compatibility counters remain available. Do not delete
   accepted plans or artifacts.
4. If the incident requires database recovery, create a safety backup of the
   current database and restore the verified pre-change backup:

   ```python
   from src.document_authoring.compatibility import restore_sqlite_backup

   report = restore_sqlite_backup(
       database_path="<target-db>",
       backup_path="<target-db>.pilot.pre.db",
       safety_backup_path="<target-db>.pilot.failed-release.db",
   )
   assert report.verified
   print(report.model_dump_json())
   ```

   Record both hashes and the post-restore row-count/integrity comparison.
5. Restart the API and worker only after the database check passes, then run
   `/health` and the read-only UI smoke. Keep the safety backup until the
   incident review signs off.

Disabling the two execution flags does not authorize reopening legacy writes.
If a rollback needs `DOCUMENT_AUTHORING_COMPATIBILITY_CLOSURE_ENABLED=false`,
that is a separate incident decision requiring a named approver and an updated
write-boundary observation; otherwise keep closure enabled and preserve the
read-only historical path.

## 10. Sign-off record

Store this record outside the repository with the redacted evidence bundle:

| Field | Required value |
|---|---|
| Release / commit | Exact label and SHA |
| Pilot evidence | Path, `evidence_hash`, status |
| Effective config | Flags and exact allowlists; no secrets |
| Backup / restore | Paths, SHA-256, integrity and row-count checks |
| Worker drill | Stop/restart/resume operators and timestamps |
| API/UI smoke | Request IDs, artifact/plan hashes, pass/fail |
| Compatibility | Both release observations and readiness report |
| Rollback | Kill-switch result or reason not exercised |
| Decision | Approver, independent reviewer, timestamp, expiry |

Until every field is complete and signed, production enablement is **not
complete**. The repository must continue to report disabled defaults and the
offline pilot must continue to report `production_enabled: false`.
