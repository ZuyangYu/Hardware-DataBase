"""RAGAS evaluation run endpoints (system_admin only).

List/create/control runs and read summaries + baseline comparison.
All endpoints require system_admin. Runs execute on a background thread; the
client polls ``GET /evaluation/runs/{run_id}`` for status (pull-based).

This route is a thin HTTP wrapper over :class:`EvaluationRunController` and a
few pure helpers defined below (``list_evaluation_runs`` /
``load_evaluation_summary``). It holds no evaluation logic of its own.
"""
from __future__ import annotations

import threading

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import ValidationError

import config.settings

from src.core.auth import AuthUser
from src.core.app_logs import AppLogService
from src.evaluation.dataset_loader import load_dataset, validate_dataset
from src.evaluation.run_control import EvaluationRunController
from src.evaluation.schemas import EvaluationSample, EvaluationSummary, EvaluationRunState, SampleResult

from src.api.deps import get_auth_service, require_system_admin
from src.api.schemas import CreateEvaluationRunRequest

router = APIRouter(tags=["evaluation"])

DEFAULT_OUTPUT_ROOT = Path("storage/evaluations")


def load_evaluation_summary(path: str | Path) -> EvaluationSummary:
    return EvaluationSummary.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))


def list_evaluation_runs(root: str | Path = DEFAULT_OUTPUT_ROOT) -> list[Path]:
    root = Path(root)
    if not root.exists():
        return []
    return sorted(
        [
            path
            for path in root.iterdir()
            if path.is_dir()
            and (
                (path / "summary.json").is_file()
                or (path / "run_state.json").is_file()
            )
        ],
        key=lambda path: path.name,
        reverse=True,
    )

# Runs live under the evaluations storage root; dataset/snapshot inputs must
# also stay under a known root. Anchor these to STORAGE_DIR / BASE_DIR (both
# absolute, derived from __file__) instead of resolving relative paths at
# import time -- otherwise a server launched from a non-repo cwd would compute
# roots that no longer match the runtime `Path.resolve()` in _check_output_root.
_EVAL_ROOT = (Path(config.settings.STORAGE_DIR) / "evaluations").resolve()
_DATASET_ROOT = (Path(config.settings.BASE_DIR) / "evaluation" / "datasets").resolve()
_SNAPSHOT_ROOT = _EVAL_ROOT
_BASE_DIR = Path(config.settings.BASE_DIR).resolve()


def _within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent)
        return True
    except ValueError:
        return False


def _resolve_user_path(value: str | Path) -> Path:
    p = Path(value)
    if not p.is_absolute():
        p = _BASE_DIR / p
    return p.resolve()


def _check_output_root(output_root: str) -> str:
    """output_root must be DEFAULT_OUTPUT_ROOT or a subdirectory of it."""
    p = _resolve_user_path(output_root)
    if not _within(p, _EVAL_ROOT):
        raise HTTPException(status_code=400, detail="output_root must be under the evaluations storage root")
    return str(p)


def _check_input_path(value: str, parent: Path, label: str) -> str:
    p = _resolve_user_path(value)
    if not _within(p, parent):
        raise HTTPException(status_code=400, detail=f"{label} must be under {parent}")
    return str(p)


def _check_dataset_path(value: str, output_root: str) -> str:
    p = _resolve_user_path(value)
    upload_root = (Path(output_root) / "uploads").resolve()
    if _within(p, _DATASET_ROOT) or _within(p, upload_root):
        return str(p)
    raise HTTPException(
        status_code=400,
        detail=f"dataset_path must be under {_DATASET_ROOT} or {upload_root}",
    )


def _filter_samples(
    samples: list[EvaluationSample],
    sample_ids: list[str] | None,
    tags: list[str] | None,
) -> list[EvaluationSample]:
    filtered = samples
    if sample_ids:
        selected_ids = set(sample_ids)
        filtered = [sample for sample in filtered if sample.id in selected_ids]
    if tags:
        selected_tags = set(tags)
        filtered = [sample for sample in filtered if selected_tags.intersection(sample.tags)]
    if not filtered:
        raise HTTPException(status_code=400, detail="no samples match the selected filters")
    return filtered


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
        # EvaluationService is imported inline so the module stays importable
        # without the eval extras installed at import time; the controller
        # instantiates services lazily per run.
        from src.evaluation.service import EvaluationService as _EvaluationService

        controller = EvaluationRunController(_EvaluationService, output_root)
        _controllers[output_root] = controller
        return controller


def _state_dict(state: EvaluationRunState) -> dict[str, Any]:
    """Serialise run state for JSON, coercing non-string enums to values."""
    dump = state.model_dump()
    for key in ("status", "stage", "mode"):
        if key in dump and not isinstance(dump[key], str):
            dump[key] = getattr(dump[key], "value", str(dump[key]))
    return dump


def _load_sample_results(run_dir: Path) -> tuple[list[dict[str, Any]], str]:
    """Load display diagnostics without making the run detail endpoint fragile."""
    results_path = run_dir / "results.jsonl"
    if not results_path.is_file():
        return [], ""
    try:
        rows = [
            SampleResult.model_validate_json(line).model_dump(mode="json")
            for line in results_path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    except (OSError, ValidationError, ValueError) as exc:
        return [], f"样本诊断不可用：{exc}"
    return rows, ""


@router.get("/evaluation/runs")
def list_runs(
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> list[dict[str, Any]]:
    """List evaluation runs (newest first). Returns a lightweight summary per
    run: run_id, status, and whether a summary.json exists."""
    output_root = _check_output_root(output_root)
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
    auth=Depends(get_auth_service),
) -> dict[str, Any]:
    """Create an evaluation run. ``dataset_path`` is required; ``mode`` selects
    online (default) vs offline (requires ``snapshot_path``). ``score_enabled``
    toggles RAGAS scoring for online runs."""
    output_root = _check_output_root(output_root)
    dataset_path = _check_dataset_path(body.dataset_path, output_root)
    errors = validate_dataset(dataset_path)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    samples = _filter_samples(load_dataset(dataset_path), body.sample_ids, body.tags)

    # Explicit KB authorization boundary: online samples run pipeline queries,
    # so every referenced KB must be registered and the access is audited.
    # This replaces silent dataset-driven access with a declared, reviewed
    # allow-list (system_admin is a governance role and must not browse KB
    # content interactively; a batch evaluation run is the sanctioned exception).
    if body.mode == "online":
        referenced_kbs = sorted({str(s.kb_name) for s in samples if getattr(s, "kb_name", "")})
        registered = set(auth.list_registered_knowledge_bases())
        unknown = [kb for kb in referenced_kbs if kb not in registered]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"数据集引用了未注册的知识库: {', '.join(unknown)}",
            )
        try:
            AppLogService().record_audit(
                action="evaluation_run_authorize_kbs",
                actor=_actor,
                target_type="evaluation_run",
                target_id=dataset_path,
                metadata={"kb_names": referenced_kbs, "source": "api"},
            )
        except Exception:
            pass  # fail-soft: audit must not block run creation

    if body.mode == "offline":
        if not body.snapshot_path:
            raise HTTPException(status_code=400, detail="snapshot_path is required for offline mode")
        snapshot_path = _check_input_path(body.snapshot_path, _SNAPSHOT_ROOT, "snapshot_path")
    else:
        snapshot_path = None

    controller = _controller(output_root)
    try:
        if body.mode == "offline":
            state = controller.create_offline_run(
                dataset_path=dataset_path,
                output_root=output_root,
                samples=samples,
                snapshot_path=snapshot_path,
                sample_ids=body.sample_ids,
                tags=body.tags,
            )
        else:
            state = controller.create_online_run(
                dataset_path=dataset_path,
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


@router.post("/evaluation/datasets")
async def upload_dataset(
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    file: UploadFile = File(...),
    _actor: AuthUser = Depends(require_system_admin),
) -> dict[str, Any]:
    """Upload a JSONL dataset into the evaluations upload area."""
    output_root = _check_output_root(output_root)
    filename = Path(file.filename or "dataset.jsonl").name
    if Path(filename).suffix.lower() != ".jsonl":
        raise HTTPException(status_code=400, detail="dataset file must be a .jsonl file")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="dataset file is empty")

    upload_dir = Path(output_root) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / filename
    target.write_bytes(content)
    errors = validate_dataset(target)
    if errors:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="; ".join(errors))
    return {
        "dataset_path": str(target),
        "file_name": filename,
        "sample_count": len(load_dataset(target)),
    }


@router.post("/evaluation/runs/{run_id}/start")
def start_run(
    run_id: str,
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> dict[str, Any]:
    output_root = _check_output_root(output_root)
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
    output_root = _check_output_root(output_root)
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
    output_root = _check_output_root(output_root)
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
    output_root = _check_output_root(output_root)
    controller = _controller(output_root)
    try:
        state = controller.cancel(run_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _state_dict(state)


@router.delete("/evaluation/runs/{run_id}")
def delete_run(
    run_id: str,
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> dict[str, Any]:
    """Delete a failed or cancelled evaluation run and its stored artifacts."""
    output_root = _check_output_root(output_root)
    controller = _controller(output_root)
    try:
        state = controller.delete(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to delete evaluation run: {exc}") from exc
    return {"ok": True, "run_id": run_id, "status": state.status}


@router.get("/evaluation/runs/{run_id}")
def get_run(
    run_id: str,
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> dict[str, Any]:
    """Return the run state plus its summary.json (if present)."""
    output_root = _check_output_root(output_root)
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
    run_dir = Path(output_root) / run_id
    summary_path = run_dir / "summary.json"
    results_dir = run_dir
    if not summary_path.is_file():
        checkpoint_dir = run_dir / ".checkpoint"
        checkpoint_summary = checkpoint_dir / "summary.json"
        if checkpoint_summary.is_file():
            summary_path = checkpoint_summary
            results_dir = checkpoint_dir
    if summary_path.is_file():
        try:
            summary: EvaluationSummary = load_evaluation_summary(summary_path)
            result["summary"] = summary.model_dump()
        except Exception:
            result["summary"] = None
    else:
        result["summary"] = None
    if result["summary"] is not None:
        result["sample_results"], result["sample_results_error"] = _load_sample_results(results_dir)
    else:
        result["sample_results"] = []
        result["sample_results_error"] = ""
    return result


@router.get("/evaluation/runs/{run_id}/compare")
def compare_run(
    run_id: str,
    baseline: str = Query(..., description="baseline run_id to compare against"),
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> dict[str, Any]:
    """Load summaries for the current run and a baseline run for comparison."""
    output_root = _check_output_root(output_root)
    current_summary_path = Path(output_root) / run_id / "summary.json"
    baseline_summary_path = Path(output_root) / baseline / "summary.json"
    if not current_summary_path.is_file():
        raise HTTPException(status_code=404, detail=f"current run {run_id} has no summary")
    if not baseline_summary_path.is_file():
        raise HTTPException(status_code=404, detail=f"baseline run {baseline} has no summary")
    current = load_evaluation_summary(current_summary_path).model_dump()
    baseline_summary = load_evaluation_summary(baseline_summary_path).model_dump()
    return {"current": current, "baseline": baseline_summary}
