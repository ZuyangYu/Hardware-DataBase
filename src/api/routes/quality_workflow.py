from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import src.settings as settings
from src.api.deps import get_auth_service, get_pipeline, require_dashboard_access, require_employee
from src.api.routes.quality import _audit, _call
from src.core.auth import AuthUser
from src.costs.store import CostStore
from src.quality.workflow import ChangeRequest, FreezeDatasetRequest, WorkflowStore
from src.quality.evaluation_bridge import DatasetEvaluationRequest, create_dataset_run, validate_binding

router = APIRouter(tags=["quality"])


def _store():
    return WorkflowStore()


def _controller():
    from src.evaluation.run_control import EvaluationRunController
    from src.evaluation.service import EvaluationService

    root = Path(os.getenv("EVAL_OUTPUT_ROOT", str(Path(settings.STORAGE_DIR) / "evaluations")))
    return EvaluationRunController(lambda: EvaluationService(pipeline_factory=get_pipeline), root)


@router.post("/quality/dataset-versions/{version_id}/evaluation-runs")
def evaluate(version_id: str, body: DatasetEvaluationRequest, viewer=Depends(require_employee), store=Depends(_store), controller=Depends(_controller), auth=Depends(get_auth_service)):
    from src.api.admission import admission_model_name, enforce_admission

    enforce_admission(viewer, viewer.department_id, resource="evaluation", model=admission_model_name())
    state = _call(create_dataset_run, store, version_id, viewer, body, controller, auth=auth)
    _audit("quality.dataset_evaluate", viewer, state.run_id)
    _call(controller.start, state.run_id)
    return {"run_id": state.run_id, "mode": state.mode, "status": state.status, "quality_binding": state.quality_binding}


@router.get("/quality/evaluation-runs/{run_id}")
def evaluation_run(run_id: str, viewer=Depends(require_employee), store=Depends(_store), controller=Depends(_controller), auth=Depends(get_auth_service)):
    state = _call(lambda: controller._store(run_id).load())
    _call(validate_binding, state, viewer=viewer, workflow=store, auth=auth)
    result = state.model_dump(mode="json")
    from src.evaluation.history import load_history_run

    if (controller.state_root / run_id / "summary.json").is_file():
        history = _call(load_history_run, controller.state_root / run_id)
        result.update(summary=history.summary.model_dump(mode="json"), results=[r.model_dump(mode="json") for r in history.results])
    return result


@router.post("/quality/evaluation-runs/{run_id}/controls/{operation}")
def control_evaluation(run_id: str, operation: str, viewer=Depends(require_employee), store=Depends(_store), controller=Depends(_controller), auth=Depends(get_auth_service)):
    state = _call(lambda: controller._store(run_id).load())
    _call(validate_binding, state, viewer=viewer, workflow=store, auth=auth)
    if viewer.id != state.quality_binding["actor_user_id"]:
        raise HTTPException(403, "only the evaluation initiator may control this run")
    if operation not in {"pause", "cancel", "resume", "score", "recover"}:
        raise HTTPException(422, "unsupported evaluation operation")
    method = {"score": "start_scoring", "recover": "recover_orphan"}.get(operation, operation)
    _call(getattr(controller, method), run_id)
    if operation == "resume":
        _call(controller.start, run_id)
    _audit("quality.evaluation_control", viewer, run_id)
    return {"run_id": run_id, "operation": operation}


@router.post("/quality/datasets/{dataset_id}/versions")
def freeze(dataset_id: str, body: FreezeDatasetRequest, viewer: AuthUser = Depends(require_employee), store=Depends(_store)):
    result = _call(store.freeze, dataset_id, viewer, body)
    _audit("quality.dataset_freeze", viewer, result["version_id"])
    return result


@router.get("/quality/dataset-versions/{version_id}")
def version(version_id: str, materialize: bool = False, viewer: AuthUser = Depends(require_dashboard_access), store=Depends(_store)):
    return _call(store.version, version_id, viewer, materialize=materialize)


@router.post("/quality/cases/{case_id}/changes")
def change(case_id: str, body: ChangeRequest, viewer: AuthUser = Depends(require_employee), store=Depends(_store)):
    root = Path(os.getenv("EVAL_OUTPUT_ROOT", str(Path(settings.STORAGE_DIR) / "evaluations")))
    result = _call(store.link_change, case_id, viewer, body, evaluation_root=root)
    _audit("quality.change_link", viewer, case_id)
    return result


@router.get("/quality/cases/{case_id}/changes")
def changes(case_id: str, viewer: AuthUser = Depends(require_dashboard_access), store=Depends(_store)):
    return _call(store.changes, case_id, viewer)


class AnalysisRequest(BaseModel):
    client_request_id: str = Field(min_length=1, max_length=128)


@router.post("/quality/analysis-runs")
def analysis(body: AnalysisRequest, viewer: AuthUser = Depends(require_dashboard_access), store=Depends(_store)):
    result = _call(store.analyze, viewer, body.client_request_id, CostStore())
    _audit("quality.analysis", viewer, result["report_id"])
    return result


@router.get("/quality/reports")
def reports(viewer: AuthUser = Depends(require_dashboard_access), store=Depends(_store)):
    return store.reports(viewer)


@router.get("/quality/reports/{report_id}")
def report(report_id: str, viewer: AuthUser = Depends(require_dashboard_access), store=Depends(_store)):
    return _call(store.reports, viewer, report_id)[0]
