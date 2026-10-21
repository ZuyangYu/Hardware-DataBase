# Architecture gate

The gate freezes **new** structural erosion. It does not try to clean up the
current graph in one pass.

## What it checks

- no new module joins a circular dependency (SCC)
- the number of import edges inside existing cycles does not grow
- no new reverse-layer import (for example `src.core → src.api`)
- large files do not grow past their checked-in line budget + a 25-line
  allowance (small fixes are allowed; new features should move to a service)
- runtime `ALTER TABLE` occurrences in non-`migrations/` source do not increase
  (comments/docstrings are ignored; versioned migrations are the sanctioned
  place for schema changes)

Current budgets and baselines live in `architecture_baseline.json`.

## Run

CI (`.github/workflows/verify.yml`) runs the gate in the `architecture` job:

```bash
uv run ruff check .
uv run python scripts/architecture_check.py
```

The same check is in `tests/test_architecture_gate.py`, so the backend test
suite also enforces it. The detector test uses a synthetic reverse-layer edge
to prove the gate is not vacuous.

There is intentionally **no local git hook**: it is bypassable and gives a
false sense of enforcement. Run the command manually when you want fast local
feedback; the required CI check is the authority.

## Required checks

In GitHub branch protection for `develop` (and `main`), require:

- `architecture`
- `backend`
- `frontend`

Without those required checks, the workflow is still advisory.

## Updating the baseline

Only when the change is deliberate and reviewed:

```bash
.venv/bin/python scripts/architecture_check.py --write-baseline
```

A baseline update is an architecture decision. Prefer shrinking the graph
(removing an edge, splitting a file, moving a type to `src/contracts`) over
raising a budget.

## Scope

This is a ratchet, not a full import-linter contract yet. It checks static
imports; dynamic `importlib.import_module(...)` wiring and string-based plugin
loading are not visible to it. The next step is to add explicit layered
contracts and migrate the existing `scc_modules` baseline downward one
aggregate at a time.
