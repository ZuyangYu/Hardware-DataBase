# Schema-Aware Harness Budget Design

## Problem

The approved `检查单_checklist` schema contains 122 semantic fields.  The
default internal Harness policy allows 20 fields, 40 graph steps, and two
total retrieval rounds.  Automatic generation therefore stops before any
retrieval or draft call with `schema semantic unit count exceeds harness
policy`.

The current `max_retrieval_rounds` setting is used for two different limits:
the retry loop for one semantic unit and the global counter for the whole
run.  Raising only `max_units_per_run` would still fail at the third unit;
raising the shared value would also permit an unintentionally large retry loop
for one failing unit.

## Goal

Create an auditable, schema-sized Harness policy for each internal-harness
work order while retaining hard limits.  A 122-field approved schema can be
executed without weakening per-field retry limits or allowing unbounded work.

## Non-goals

- Do not rerun or mutate the failed work order; it remains an audit record.
- Do not remove the global unit, retrieval, or graph-step limits.
- Do not change evidence validation, document rendering, or automatic-release
  conditions.
- Do not batch or merge semantic fields; every approved field remains a
  separately auditable unit.

## Policy Model

`HarnessPolicy.max_retrieval_rounds` becomes the global total retrieval-call
budget.  Add `max_retrieval_attempts_per_unit`, defaulting to 2, for the
per-unit retry loop.  Existing persisted policies remain valid: their current
`max_retrieval_rounds` continues to be the global cap and receives the new
default of two attempts per unit.

For a schema with `N` semantic units and a configured per-unit retry limit
`A`, the generated policy budgets are:

- `max_units_per_run = N`
- `max_retrieval_attempts_per_unit = A`
- `max_retrieval_rounds = N * A`
- `max_steps = 2 + N * (A + 3)`

The step formula includes graph initialization and cross-unit validation, all
retrieval attempts, plus draft, validation, and contamination checks for every
unit.  For the current 122-unit checklist and `A = 2`, this produces 122
units, 244 retrieval calls, and 612 steps.

Before saving a generated policy, the service rejects it if it exceeds the
server-owned caps of 200 units, 400 total retrieval calls, or 1,000 steps.
The error explains that the approved schema exceeds the automatic-generation
capacity.  Those caps bound both cost and execution time.

## Work-Order Policy Selection

When creating an internal-harness work order without an explicit policy ID,
the service derives a policy from the approved schema rather than selecting
the first policy in storage.  The persisted policy ID is deterministic for the
template schema ID and version, and its policy version contains the computed
budget profile.  The work order and its manifest continue to freeze the exact
policy ID and version.

An explicit `harness_policy_id` remains authoritative for existing API users
and tests.  It is not automatically resized because it is an operator-selected
policy.

## Runtime Behaviour

`AuthoringGraph._retrieve_with_budget()` retries each requirement at most
`max_retrieval_attempts_per_unit` times.  It increments the same global
counter on every attempt and the policy continues to reject the run once that
counter exceeds `max_retrieval_rounds`.

The run manifest records the global retrieval budget and the new per-unit
retry limit so that an audit can reproduce the execution policy.

## Validation

- A 122-unit schema receives an approved derived policy of 122 units, 244
  global retrieval calls, and 612 steps.
- A policy with `max_retrieval_attempts_per_unit=2` calls a failing retriever
  no more than twice for one unit.
- Two successful units execute with a global budget of two calls; the former
  third-unit failure is prevented by an appropriately sized policy.
- A schema over the server-owned 200-unit cap is rejected before a work order
  or LLM call is created.
- An explicit policy ID still freezes that exact policy and is not resized.
- The failed existing work order remains blocked; a newly created work order
  uses the derived policy.
