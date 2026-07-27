"""RAGAS evaluation run endpoints (system_admin only).

Mirrors the Streamlit evaluation tab so a future frontend can fully own
evaluation: list/create/control runs and read summaries + baseline comparison.
All endpoints require system_admin. Runs execute on a background thread; the
client polls ``GET /evaluation/runs/{run_id}`` for status (the UI used a 2s
``@st.fragment`` auto-refresh -- the API is pull-based instead).

This route is a thin HTTP wrapper over :class:`EvaluationRunController` and the
pure helpers in :mod:`src.ui.evaluation_page` (``list_evaluation_runs`` /
``load_evaluation_summary``). It holds no evaluation logic of its own.
"""
from __future__ import annotations

import threading

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError

import config.settings

from src.core.auth import AuthUser
from src.evaluation.dataset_loader import load_dataset, validate_dataset
from src.evaluation.run_control import EvaluationRunController
from src.evaluation.schemas import EvaluationSummary, EvaluationRunState
from src.ui.evaluation_page import (
    DEFAULT_OUTPUT_ROOT,
    list_evaluation_runs,
    load_evaluation_summary,
)

from src.api.deps import require_system_admin
from src.api.schemas import CreateEvaluationRunRequest

router = APIRouter(tags=["evaluation"])

# Runs live under the evaluations storage root; dataset/snapshot inputs must
# also stay under a known root. Anchor these to STORAGE_DIR / BASE_DIR (both
# absolute, derived from __file__) instead of resolving relative paths at
# import time -- otherwise a server launched from a non-repo cwd would compute
# roots that no longer match the runtime `Path.resolve()` in _check_output_root.
_EVAL_ROOT = (Path(config.settings.STORAGE_DIR) / "evaluations").resolve()
_DATASET_ROOT = (Path(config.settings.BASE_DIR) / "evaluation" / "datasets").resolve()
_SNAPSHOT_ROOT = _EVAL_ROOT


def _within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent)
        return True
    except ValueError:
        return False


def _check_output_root(output_root: str) -> str:
    """output_root must be DEFAULT_OUTPUT_ROOT or a subdirectory of it."""
    if output_root == str(DEFAULT_OUTPUT_ROOT):
        return output_root
    p = Path(output_root)
    if not _within(p, _EVAL_ROOT):
        raise HTTPException(status_code=400, detail="output_root must be under the evaluations storage root")
    return output_root


def _check_input_path(value: str, parent: Path, label: str) -> str:
    p = Path(value)
    if not _within(p, parent):
        raise HTTPException(status_code=400, detail=f"{label} must be under {parent}")
    return value


# Process-wide controller cache keyed by output_root. EvaluationRunController
# keeps an in-memory _threads registry to prevent concurrent workers on the same
# run; if we built a fresh controller per request that guard would be defeated
# (a double-click on /start would spawn two workers). Mirror get_pipeline's
# singleton pattern so start/pause/cancel share one thread registry.
_controller_lock = threading.Lock()
_controllers: dict[str, EvaluationRunController] = {}


def _controller(output_root: str) -> EvaluationRunController:
    with _controller_lock:
        cached = _controllers.get(output_root)
        if cached is not None:
            return cached
        # evaluation_service_factory() returns the EvaluationService class; the
        # controller instantiates services lazily per run. Imported inline so the
        # module stays importable without the eval extras installed at import time.
        from src.ui.evaluation_page import evaluation_service_factory

        controller = EvaluationRunController(evaluation_service_factory(), output_root)
        _controllers[output_root] = controller
        return controller


def _state_dict(state: EvaluationRunState) -> dict[str, Any]:
    """Serialise run state for JSON, coercing non-string enums to values."""
    dump = state.model_dump()
    for key in ("status", "stage", "mode"):
        if key in dump and not isinstance(dump[key], str):
            dump[key] = getattr(dump[key], "value", str(dump[key]))
    return dump


@router.get("/evaluation/runs")
def list_runs(
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> list[dict[str, Any]]:
    """List evaluation runs (newest first). Returns a lightweight summary per
    run: run_id, status, and whether a summary.json exists."""
    _check_output_root(output_root)
    runs = []
    for path in list_evaluation_runs(output_root):
        run_id = path.name
        has_summary = (path / "summary.json").is_file()
        status = ""
        state_path = path / "run_state.json"
        if state_path.is_file():
            try:
                status = EvaluationRunState.model_validate_json(
                    state_path.read_text(encoding="utf-8-sig")
                ).status
                if not isinstance(status, str):
                    status = status.value
            except Exception:
                # Corrupt run_state.json -- surface it explicitly so the
                # frontend can flag it instead of showing "no status".
                status = "invalid"
        runs.append({"run_id": run_id, "status": status, "has_summary": has_summary})
    return runs


@router.post("/evaluation/runs")
def create_run(
    body: CreateEvaluationRunRequest,
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> dict[str, Any]:
    """Create an evaluation run. ``dataset_path`` is required; ``mode`` selects
    online (default) vs offline (requires ``snapshot_path``). ``score_enabled``
    toggles RAGAS scoring for online runs."""
    _check_output_root(output_root)
    _check_input_path(body.dataset_path, _DATASET_ROOT, "dataset_path")
    errors = validate_dataset(body.dataset_path)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    samples = load_dataset(body.dataset_path)

    if body.mode == "offline":
        if not body.snapshot_path:
            raise HTTPException(status_code=400, detail="snapshot_path is required for offline mode")
        _check_input_path(body.snapshot_path, _SNAPSHOT_ROOT, "snapshot_path")

    controller = _controller(output_root)
    try:
        if body.mode == "offline":
            state = controller.create_offline_run(
                dataset_path=body.dataset_path,
                output_root=output_root,
                samples=samples,
                snapshot_path=body.snapshot_path,
                sample_ids=body.sample_ids,
                tags=body.tags,
            )
        else:
            state = controller.create_online_run(
                dataset_path=body.dataset_path,
                output_root=output_root,
                samples=samples,
                score_enabled=body.score_enabled,
                sample_ids=body.sample_ids,
                tags=body.tags,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _state_dict(state)


@router.post("/evaluation/runs/{run_id}/start")
def start_run(
    run_id: str,
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> dict[str, Any]:
    _check_output_root(output_root)
    controller = _controller(output_root)
    try:
        controller.start(run_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "run_id": run_id}


@router.post("/evaluation/runs/{run_id}/pause")
def pause_run(
    run_id: str,
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> dict[str, Any]:
    _check_output_root(output_root)
    controller = _controller(output_root)
    try:
        state = controller.pause(run_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _state_dict(state)


@router.post("/evaluation/runs/{run_id}/resume")
def resume_run(
    run_id: str,
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> dict[str, Any]:
    _check_output_root(output_root)
    controller = _controller(output_root)
    try:
        state = controller.resume(run_id)
        # Mirrors the UI: resume re-starts the worker thread.
        controller.start(run_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _state_dict(state)


@router.post("/evaluation/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> dict[str, Any]:
    _check_output_root(output_root)
    controller = _controller(output_root)
    try:
        state = controller.cancel(run_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _state_dict(state)


@router.get("/evaluation/runs/{run_id}")
def get_run(
    run_id: str,
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> dict[str, Any]:
    """Return the run state plus its summary.json (if present)."""
    _check_output_root(output_root)
    controller = _controller(output_root)
    try:
        state = controller.load_for_display(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (ValidationError, ValueError) as exc:
        # A corrupt run_state.json is a server-side problem, not "not found".
        raise HTTPException(status_code=500, detail=f"run state invalid: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    result = _state_dict(state)
    summary_path = Path(output_root) / run_id / "summary.json"
    if summary_path.is_file():
        try:
            summary: EvaluationSummary = load_evaluation_summary(summary_path)
            result["summary"] = summary.model_dump()
        except Exception:
            result["summary"] = None
    else:
        result["summary"] = None
    return result


@router.get("/evaluation/runs/{run_id}/compare")
def compare_run(
    run_id: str,
    baseline: str = Query(..., description="baseline run_id to compare against"),
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> dict[str, Any]:
    """Load summaries for the current run and a baseline run for comparison."""
    _check_output_root(output_root)
    current_summary_path = Path(output_root) / run_id / "summary.json"
    baseline_summary_path = Path(output_root) / baseline / "summary.json"
    if not current_summary_path.is_file():
        raise HTTPException(status_code=404, detail=f"current run {run_id} has no summary")
    if not baseline_summary_path.is_file():
        raise HTTPException(status_code=404, detail=f"baseline run {baseline} has no summary")
    current = load_evaluation_summary(current_summary_path).model_dump()
    baseline_summary = load_evaluation_summary(baseline_summary_path).model_dump()
    return {"current": current, "baseline": baseline_summary}
