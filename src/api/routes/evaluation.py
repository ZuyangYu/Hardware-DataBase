"""RAGAS evaluation run endpoints (system_admin only).

List/create/control runs and read summaries + baseline comparison.
All endpoints require system_admin. Runs execute on a background thread; the
client polls ``GET /evaluation/runs/{run_id}`` for status (pull-based).

This route is a thin HTTP wrapper over :class:`EvaluationRunController` and a
few pure helpers defined below (``list_evaluation_runs`` /
``load_evaluation_summary``). It holds no evaluation logic of its own.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import ValidationError

import src.settings

from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, AuthUser
from src.core.app_logs import AppLogService
from src.evaluation.dataset_loader import load_dataset, validate_dataset
from src.evaluation.access import (
    EvaluationSampleSelection,
    KnowledgeBaseBinding,
    resolve_knowledge_base,
    select_evaluation_samples,
)
from src.evaluation.history import compatibility_report, load_history_run
from src.evaluation.run_control import EvaluationRunController
from src.evaluation.schemas import EvaluationSample, EvaluationSummary, EvaluationRunState, SampleResult
from src.evaluation.snapshot_manifest import (
    load_snapshot_manifest,
    validate_snapshot_manifest,
)
from src.evaluation.snapshot_store import SnapshotStore
from src.pipelines.document_rag.schemas import RequestContext

from src.api.deps import get_auth_service, get_pipeline, require_system_admin
from src.api.schemas import (
    CreateEvaluationRunRequest,
    EvaluationCompareResponse,
    EvaluationKnowledgeBaseView,
    EvaluationPreflightResponse,
    EvaluationRunListItemView,
)

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
_EVAL_ROOT = (Path(src.settings.STORAGE_DIR) / "evaluations").resolve()
_DATASET_ROOT = (Path(src.settings.BASE_DIR) / "evaluation" / "datasets").resolve()
_SNAPSHOT_ROOT = _EVAL_ROOT
_BASE_DIR = Path(src.settings.BASE_DIR).resolve()


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
    """Return the single supported evaluation root.

    Run-local directories are created by ``EvaluationRunController``. Keeping
    the API root fixed prevents a caller from redirecting reports, uploads, or
    audit-linked artifacts to an arbitrary subdirectory.
    """
    p = _resolve_user_path(output_root)
    if p != _EVAL_ROOT:
        raise HTTPException(status_code=400, detail="output_root 只能使用 storage/evaluations")
    return str(_EVAL_ROOT)


def _check_input_path(value: str, parent: Path, label: str) -> str:
    p = _resolve_user_path(value)
    if not _within(p, parent):
        raise HTTPException(status_code=400, detail=f"{label} must be under {parent}")
    return str(p)


def _run_dir(output_root: str, run_id: str) -> Path:
    """Resolve one run directory without allowing path traversal."""

    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise HTTPException(status_code=400, detail="invalid evaluation run id")
    path = (Path(output_root) / run_id).resolve()
    if path.parent != Path(output_root).resolve():
        raise HTTPException(status_code=400, detail="invalid evaluation run id")
    return path


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


@dataclass
class _PreparedEvaluation:
    dataset_path: str
    snapshot_path: str | None
    binding: KnowledgeBaseBinding | None
    selection: EvaluationSampleSelection | None
    warnings: list[str]
    errors: list[str]
    snapshot_ownership_verified: bool = False


def _admin_kb_summaries(
    actor: AuthUser,
    pipeline: AppPipeline,
    auth: AuthService,
) -> list:
    """Reuse the governance KB identity source without exposing content."""

    # This context is intentionally only used for the identity-listing call.
    # The endpoint has already required system_admin, and the pipeline's admin
    # listing checks the role before returning backend names.
    ctx = RequestContext(user_id=actor.username, roles=[actor.role])
    existing = pipeline.list_all_knowledge_bases_for_admin(ctx=ctx)
    return auth.list_knowledge_base_summaries(existing)


def _resolve_request_binding(
    body: CreateEvaluationRunRequest,
    samples: list[EvaluationSample],
    *,
    actor: AuthUser,
    pipeline: AppPipeline,
    auth: AuthService,
) -> KnowledgeBaseBinding:
    requested_name = (body.kb_name or "").strip() or None
    if body.kb_id is None and requested_name is None:
        names = sorted({sample.kb_name.strip() for sample in samples if sample.kb_name.strip()})
        if len(names) != 1:
            raise ValueError("必须指定 kb_id；数据集未能唯一推断目标知识库")
        requested_name = names[0]
    return resolve_knowledge_base(
        _admin_kb_summaries(actor, pipeline, auth),
        kb_id=body.kb_id,
        kb_name=requested_name,
    )


def _validate_offline_snapshot(
    snapshot_path: str,
    selection: EvaluationSampleSelection,
    binding: KnowledgeBaseBinding,
    summaries: list,
) -> tuple[list[str], list[str], bool]:
    """Validate snapshot ownership, with a constrained legacy fallback."""

    warnings: list[str] = []
    errors: list[str] = []
    path = Path(snapshot_path)
    if not path.is_file():
        return [], [f"离线快照不存在：{snapshot_path}"], False
    try:
        snapshots = SnapshotStore(path).load_all()
    except ValueError as exc:
        return [], [str(exc)], False
    selected_ids = {sample.id for sample in selection.samples}
    snapshot_ids = {snapshot.sample_id for snapshot in snapshots}
    if snapshot_ids != selected_ids:
        errors.append("离线快照样本范围与本次筛选结果不一致")

    try:
        manifest = load_snapshot_manifest(path.parent)
    except (OSError, UnicodeError, ValueError) as exc:
        return warnings, [f"snapshot.manifest.json 无效：{exc}"], False
    if manifest is not None:
        manifest_errors = validate_snapshot_manifest(
            manifest,
            kb_id=binding.kb_id,
            kb_name=binding.kb_name,
            department_id=binding.department_id,
            cohort_fingerprint=selection.cohort_fingerprint,
            snapshot_path=path,
        )
        errors.extend(manifest_errors)
        ownership_verified = not errors and bool(manifest.get("ownership_verified", True))
        if not errors and not ownership_verified:
            warnings.append("离线快照 manifest 明确标记为未验证，不能作为严格基线")
        return warnings, errors, ownership_verified
    else:
        same_name_count = sum(
            1
            for summary in summaries
            if bool(getattr(summary, "registered", False))
            and str(getattr(summary, "name", "")).strip() == binding.kb_name
        )
        if same_name_count != 1:
            errors.append("旧版离线快照缺少归属清单，且知识库名称不唯一，无法安全使用")
        else:
            warnings.append("离线快照缺少 snapshot.manifest.json，仅按唯一名称和样本范围校验，不能作为严格基线")
    return warnings, errors, False


def _prepare_evaluation(
    body: CreateEvaluationRunRequest,
    *,
    output_root: str,
    actor: AuthUser,
    pipeline: AppPipeline,
    auth: AuthService,
    scan_sources: bool,
    check_scoring: bool,
) -> _PreparedEvaluation:
    dataset_path = _check_dataset_path(body.dataset_path, output_root)
    dataset_errors = validate_dataset(dataset_path)
    if dataset_errors:
        return _PreparedEvaluation(dataset_path, None, None, None, [], dataset_errors)
    try:
        samples = load_dataset(dataset_path)
    except Exception as exc:
        return _PreparedEvaluation(dataset_path, None, None, None, [], [str(exc)])

    summaries = _admin_kb_summaries(actor, pipeline, auth)
    try:
        binding = _resolve_request_binding(
            body,
            samples,
            actor=actor,
            pipeline=pipeline,
            auth=auth,
        )
    except ValueError as exc:
        return _PreparedEvaluation(dataset_path, None, None, None, [], [str(exc)])

    selection = select_evaluation_samples(
        samples,
        binding,
        sample_ids=body.sample_ids,
        tags=body.tags,
    )
    errors: list[str] = []
    warnings: list[str] = []
    if selection.dataset_sample_count == 0:
        errors.append(f"所选知识库 {binding.kb_name} 没有匹配的评估样本")

    # This is deliberately shared by preflight, create, and worker.  Create
    # disables live catalog scanning because the worker repeats that check at
    # start time; context and negative-access validation still happen here.
    from src.evaluation.preflight import EvaluationPreflight

    errors.extend(
        EvaluationPreflight(lambda: pipeline).validate(
            selection.samples,
            scan_sources=scan_sources,
        )
    )
    if check_scoring:
        from src.evaluation.service import EvaluationService

        errors.extend(EvaluationService(pipeline_factory=lambda: pipeline).preflight_scoring())

    snapshot_path: str | None = None
    snapshot_ownership_verified = False
    if body.mode == "offline":
        if not body.snapshot_path:
            errors.append("snapshot_path is required for offline mode")
        else:
            try:
                snapshot_path = _check_input_path(body.snapshot_path, _SNAPSHOT_ROOT, "snapshot_path")
            except HTTPException as exc:
                errors.append(str(exc.detail))
            else:
                snapshot_warnings, snapshot_errors, snapshot_ownership_verified = _validate_offline_snapshot(
                    snapshot_path,
                    selection,
                    binding,
                    summaries,
                )
                warnings.extend(snapshot_warnings)
                errors.extend(snapshot_errors)
    return _PreparedEvaluation(
        dataset_path,
        snapshot_path,
        binding,
        selection,
        warnings,
        errors,
        snapshot_ownership_verified,
    )


def _preflight_response(
    body: CreateEvaluationRunRequest,
    prepared: _PreparedEvaluation,
) -> EvaluationPreflightResponse:
    selection = prepared.selection
    binding = prepared.binding
    return EvaluationPreflightResponse(
        dataset_path=prepared.dataset_path,
        mode=body.mode,
        kb_id=binding.kb_id if binding else body.kb_id,
        kb_name=binding.kb_name if binding else (body.kb_name or ""),
        department_id=binding.department_id if binding else None,
        dataset_total_count=selection.dataset_total_count if selection else 0,
        matched_sample_count=selection.matched_sample_count if selection else 0,
        filtered_sample_count=selection.filtered_sample_count if selection else 0,
        dataset_sample_count=selection.dataset_sample_count if selection else 0,
        normal_sample_count=selection.normal_sample_count if selection else 0,
        expected_denied_sample_count=selection.expected_denied_sample_count if selection else 0,
        cohort_fingerprint=selection.cohort_fingerprint if selection else "",
        warnings=prepared.warnings,
        errors=prepared.errors,
        can_create=not prepared.errors and selection is not None and binding is not None,
    )


def _sha256_path(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_stored_run_before_start(
    run_id: str,
    output_root: str,
    *,
    actor: AuthUser,
    pipeline: AppPipeline,
    auth: AuthService,
) -> None:
    """Recheck immutable run metadata between create and worker start."""

    run_dir = _run_dir(output_root, run_id)
    state_path = run_dir / "run_state.json"
    if not state_path.is_file():
        return  # controller will produce the canonical legacy/not-found error
    try:
        state = EvaluationRunState.model_validate_json(state_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"run state invalid: {exc}") from exc

    if state.kb_id is None or not state.kb_name:
        raise HTTPException(status_code=409, detail="该评估任务缺少知识库绑定元数据，不能启动")
    try:
        binding = resolve_knowledge_base(
            _admin_kb_summaries(actor, pipeline, auth),
            kb_id=state.kb_id,
            kb_name=state.kb_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"启动前知识库校验失败：{exc}") from exc
    if binding.department_id != state.department_id:
        raise HTTPException(status_code=409, detail="启动前知识库部门归属已变化")

    execution_path = Path(state.dataset_path)
    if not execution_path.is_file():
        raise HTTPException(status_code=409, detail="运行输入副本不存在，不能启动")
    if state.execution_dataset_sha256:
        try:
            actual_hash = _sha256_path(execution_path)
        except OSError as exc:
            raise HTTPException(status_code=409, detail=f"无法读取运行输入副本：{exc}") from exc
        if actual_hash != state.execution_dataset_sha256:
            raise HTTPException(status_code=409, detail="运行输入副本哈希已变化，不能启动")
    try:
        samples = load_dataset(execution_path)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"运行输入副本无效：{exc}") from exc
    selection = select_evaluation_samples(
        samples,
        binding,
        sample_ids=state.sample_ids,
        tags=state.tags,
    )
    if state.cohort_fingerprint and selection.cohort_fingerprint != state.cohort_fingerprint:
        raise HTTPException(status_code=409, detail="运行输入副本的样本集指纹已变化，不能启动")
    if state.mode == "offline":
        snapshot = Path(state.snapshot_path)
        if not snapshot.is_file():
            raise HTTPException(status_code=409, detail="离线快照不存在，不能启动")
        if state.snapshot_sha256:
            try:
                snapshot_hash = _sha256_path(snapshot)
            except OSError as exc:
                raise HTTPException(status_code=409, detail=f"无法读取离线快照：{exc}") from exc
            if snapshot_hash != state.snapshot_sha256:
                raise HTTPException(status_code=409, detail="离线快照哈希已变化，不能启动")
        try:
            manifest = load_snapshot_manifest(run_dir)
        except (OSError, UnicodeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=f"snapshot.manifest.json 无效：{exc}") from exc
        if manifest is not None:
            manifest_errors = validate_snapshot_manifest(
                manifest,
                kb_id=binding.kb_id,
                kb_name=binding.kb_name,
                department_id=binding.department_id,
                cohort_fingerprint=selection.cohort_fingerprint,
                snapshot_path=snapshot,
            )
            if manifest_errors:
                raise HTTPException(status_code=409, detail="；".join(manifest_errors))


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


_SUMMARY_METADATA_FIELDS = (
    "kb_id",
    "kb_name",
    "department_id",
    "created_by",
    "source_dataset_path",
    "dataset_sha256",
    "execution_dataset_sha256",
    "dataset_total_count",
    "dataset_sample_count",
    "matched_sample_count",
    "filtered_sample_count",
    "normal_sample_count",
    "expected_denied_sample_count",
    "cohort_fingerprint",
    "snapshot_sha256",
    "snapshot_ownership_verified",
    "validation_warnings",
    "llm_model",
    "embedding_model",
    "evaluation_config",
)


def _summary_dict(summary: EvaluationSummary) -> dict[str, Any]:
    """Serialize legacy summaries without fabricating new metadata fields."""

    result = summary.model_dump()
    if summary.kb_id is None and not summary.cohort_fingerprint and not summary.source_dataset_path:
        for field in _SUMMARY_METADATA_FIELDS:
            result.pop(field, None)
    return result


def _load_sample_results(run_dir: Path) -> tuple[list[dict[str, Any]], str]:
    """Load display diagnostics without making the run detail endpoint fragile."""
    results_path = run_dir / "results.jsonl"
    if not results_path.is_file():
        return [], ""
    try:
        rows = [
            SampleResult.model_validate_json(line).model_dump(mode="json", exclude_none=True)
            for line in results_path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    except (OSError, ValidationError, ValueError) as exc:
        return [], f"样本诊断不可用：{exc}"
    return rows, ""


@router.get(
    "/evaluation/knowledge-bases",
    response_model=list[EvaluationKnowledgeBaseView],
)
def list_evaluation_knowledge_bases(
    _actor: AuthUser = Depends(require_system_admin),
    pipeline: AppPipeline = Depends(get_pipeline),
    auth: AuthService = Depends(get_auth_service),
) -> list[EvaluationKnowledgeBaseView]:
    """List selectable registered KB identities for the system admin UI."""

    return [
        EvaluationKnowledgeBaseView(
            kb_id=int(summary.kb_id),
            kb_name=summary.name,
            department_id=summary.department_id,
            department_name=summary.department_name,
            physical_exists=summary.physical_exists,
            registered=summary.registered,
        )
        for summary in _admin_kb_summaries(_actor, pipeline, auth)
        if summary.registered and summary.kb_id is not None
    ]


# Resolve from the repository location rather than the process CWD.  The API
# is sometimes started by an IDE or supervisor from a different directory.
_DATASETS_DIR = _DATASET_ROOT
_DATASET_SCAN_MAX_BYTES = 20 * 1024 * 1024


@router.get("/evaluation/datasets")
def list_evaluation_datasets(
    _actor: AuthUser = Depends(require_system_admin),
) -> list[dict[str, Any]]:
    """Summarize built-in and previously uploaded datasets for the form.

    Each ``*.jsonl`` under ``evaluation/datasets`` and the evaluation upload
    area is scanned for sample count, knowledge-base bindings, tag vocabulary,
    and denied/critical counts so the UI can offer a picker with meaningful
    previews instead of a free-form path field.
    """

    items: list[dict[str, Any]] = []
    upload_dir = _EVAL_ROOT / "uploads"
    dataset_dirs = [_DATASETS_DIR, upload_dir]
    # An uploaded file may intentionally reuse a built-in filename.  In that
    # case prefer the uploaded copy (the upload directory is scanned last) so
    # the picker does not show two indistinguishable entries.
    paths_by_name: dict[str, Path] = {}
    for directory in dataset_dirs:
        if not directory.is_dir():
            continue
        for path in directory.glob("*.jsonl"):
            paths_by_name[path.name.casefold()] = path.resolve()
    for path in sorted(paths_by_name.values(), key=lambda item: item.name.casefold()):
        try:
            if path.stat().st_size > _DATASET_SCAN_MAX_BYTES:
                continue
            kb_counts: dict[str, int] = {}
            tags: set[str] = set()
            total = denied = critical = malformed = 0
            with path.open("r", encoding="utf-8-sig") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    total += 1
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        malformed += 1
                        continue
                    kb_name = str(record.get("kb_name") or "").strip()
                    if kb_name:
                        kb_counts[kb_name] = kb_counts.get(kb_name, 0) + 1
                    if record.get("expected_access") == "denied":
                        denied += 1
                    if record.get("critical"):
                        critical += 1
                    record_tags = record.get("tags")
                    if isinstance(record_tags, list):
                        tags.update(str(tag) for tag in record_tags if str(tag).strip())
            # Keep built-in paths portable (and compatible with the default
            # value used by the frontend); uploaded paths are absolute because
            # they live under the storage root returned by the upload API.
            display_path = (
                path.as_posix()
                if _within(path, upload_dir)
                else path.relative_to(_BASE_DIR).as_posix()
            )
            items.append(
                {
                    "name": path.name,
                    "path": display_path,
                    "sample_count": total,
                    "malformed_lines": malformed,
                    "kb_bindings": [
                        {"kb_name": name, "count": count}
                        for name, count in sorted(kb_counts.items(), key=lambda kv: -kv[1])
                    ],
                    "tags": sorted(tags),
                    "expected_denied_count": denied,
                    "critical_count": critical,
                }
            )
        except OSError:
            continue
    return items


def _run_list_item(path: Path) -> EvaluationRunListItemView:
    state: EvaluationRunState | None = None
    summary: EvaluationSummary | None = None
    state_path = path / "run_state.json"
    summary_path = path / "summary.json"
    if state_path.is_file():
        try:
            state = EvaluationRunState.model_validate_json(state_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return EvaluationRunListItemView(run_id=path.name, status="invalid", has_summary=summary_path.is_file())
    if summary_path.is_file():
        try:
            summary = load_evaluation_summary(summary_path)
        except Exception:
            summary = None

    source = state or summary
    if source is None:
        return EvaluationRunListItemView(run_id=path.name, status="invalid")
    status = getattr(state, "status", "completed" if summary else "")
    if not isinstance(status, str):
        status = status.value
    kb_id = getattr(source, "kb_id", None)
    cohort = getattr(source, "cohort_fingerprint", "")
    return EvaluationRunListItemView(
        run_id=path.name,
        status=status,
        has_summary=summary is not None,
        legacy=kb_id is None or not cohort,
        kb_id=kb_id,
        kb_name=getattr(source, "kb_name", ""),
        department_id=getattr(source, "department_id", None),
        created_by=getattr(source, "created_by", ""),
        created_at=getattr(source, "created_at", ""),
        dataset_path=getattr(source, "dataset_path", "") if state else "",
        source_dataset_path=getattr(source, "source_dataset_path", ""),
        mode=getattr(source, "mode", "") if state else "",
        report_path=getattr(source, "report_path", "") if state else "",
        dataset_sample_count=getattr(source, "dataset_sample_count", 0),
        normal_sample_count=getattr(source, "normal_sample_count", 0),
        expected_denied_sample_count=getattr(source, "expected_denied_sample_count", 0),
        cohort_fingerprint=cohort,
        llm_model=getattr(source, "llm_model", ""),
        embedding_model=getattr(source, "embedding_model", ""),
        snapshot_ownership_verified=getattr(source, "snapshot_ownership_verified", False),
        validation_warnings=list(getattr(source, "validation_warnings", []) or []),
    )


@router.get("/evaluation/runs", response_model=list[EvaluationRunListItemView])
def list_runs(
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> list[EvaluationRunListItemView]:
    """List evaluation runs (newest first). Returns a lightweight summary per
    run: run_id, status, and whether a summary.json exists."""
    output_root = _check_output_root(output_root)
    return [_run_list_item(path) for path in list_evaluation_runs(output_root)]


@router.post("/evaluation/preflight", response_model=EvaluationPreflightResponse)
def preflight_run(
    body: CreateEvaluationRunRequest,
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
) -> EvaluationPreflightResponse:
    """Run the read-only checks used by the system-admin evaluation form."""

    output_root = _check_output_root(output_root)
    prepared = _prepare_evaluation(
        body,
        output_root=output_root,
        actor=_actor,
        pipeline=pipeline,
        auth=auth,
        scan_sources=body.mode == "online",
        # This endpoint is the form's read-only dataset/scope preflight.
        # RAGAS judge/embedding probes happen in the worker immediately
        # before the scoring phase (after the explicit post-collection
        # "开始评分" action for online runs).
        check_scoring=False,
    )
    return _preflight_response(body, prepared)


@router.post("/evaluation/runs")
def create_run(
    body: CreateEvaluationRunRequest,
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
) -> dict[str, Any]:
    """Create an evaluation run.

    Online runs collect answers first and enter the post-collection QC gate;
    scoring is explicitly started later.  Offline runs score an existing
    snapshot after creation.
    """
    output_root = _check_output_root(output_root)
    prepared = _prepare_evaluation(
        body,
        output_root=output_root,
        actor=_actor,
        pipeline=pipeline,
        auth=auth,
        scan_sources=False,
        check_scoring=False,
    )
    if prepared.errors or prepared.binding is None or prepared.selection is None:
        raise HTTPException(status_code=400, detail="; ".join(prepared.errors))
    dataset_path = prepared.dataset_path
    snapshot_path = prepared.snapshot_path
    binding = prepared.binding
    selection = prepared.selection

    # The selected identity, rather than dataset text, is the authorization
    # boundary recorded for this run.
    try:
        AppLogService().record_audit(
            action="evaluation_run_authorize_kb",
            actor=_actor,
            target_type="evaluation_run",
            target_id=dataset_path,
            kb_name=binding.kb_name,
            metadata={
                "kb_id": binding.kb_id,
                "department_id": binding.department_id,
                "sample_count": selection.dataset_sample_count,
                "cohort_fingerprint": selection.cohort_fingerprint,
                "source": "api",
            },
        )
    except Exception:
        pass  # fail-soft: audit must not block run creation

    controller = _controller(output_root)
    try:
        if body.mode == "offline":
            state = controller.create_offline_run(
                dataset_path=dataset_path,
                output_root=output_root,
                samples=selection.samples,
                snapshot_path=snapshot_path,
                sample_ids=body.sample_ids,
                tags=body.tags,
                kb_id=binding.kb_id,
                kb_name=binding.kb_name,
                department_id=binding.department_id,
                created_by=_actor.username,
                dataset_total_count=selection.dataset_total_count,
                matched_sample_count=selection.matched_sample_count,
                filtered_sample_count=selection.filtered_sample_count,
                cohort_fingerprint_value=selection.cohort_fingerprint,
                validation_warnings=prepared.warnings,
                snapshot_ownership_verified=prepared.snapshot_ownership_verified,
            )
        else:
            state = controller.create_online_run(
                dataset_path=dataset_path,
                output_root=output_root,
                samples=selection.samples,
                sample_ids=body.sample_ids,
                tags=body.tags,
                kb_id=binding.kb_id,
                kb_name=binding.kb_name,
                department_id=binding.department_id,
                created_by=_actor.username,
                dataset_total_count=selection.dataset_total_count,
                matched_sample_count=selection.matched_sample_count,
                filtered_sample_count=selection.filtered_sample_count,
                cohort_fingerprint_value=selection.cohort_fingerprint,
                validation_warnings=prepared.warnings,
                snapshot_ownership_verified=prepared.snapshot_ownership_verified,
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
    auth: AuthService = Depends(get_auth_service),
    pipeline: AppPipeline = Depends(get_pipeline),
) -> dict[str, Any]:
    output_root = _check_output_root(output_root)
    _validate_stored_run_before_start(
        run_id,
        output_root,
        actor=_actor,
        pipeline=pipeline,
        auth=auth,
    )
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
    _run_dir(output_root, run_id)
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
    _run_dir(output_root, run_id)
    controller = _controller(output_root)
    try:
        state = controller.resume(run_id)
        # Mirrors the UI: resume re-starts the worker thread.
        controller.start(run_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _state_dict(state)


@router.get("/evaluation/runs/{run_id}/collection-qc")
def get_collection_qc(
    run_id: str,
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> dict[str, Any]:
    """Return the post-collection quality gate report (recomputing if absent)."""
    output_root = _check_output_root(output_root)
    _run_dir(output_root, run_id)
    controller = _controller(output_root)
    try:
        qc = controller.load_collection_qc(run_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if qc is None:
        raise HTTPException(status_code=404, detail="该运行没有可用的采集质检报告")
    return qc


@router.post("/evaluation/runs/{run_id}/score")
def start_scoring(
    run_id: str,
    force: bool = Query(default=False),
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> dict[str, Any]:
    """Enter the scoring phase from a collected run after its QC gate.

    Rejected while the collection QC reports a ``fail`` verdict unless
    ``force`` is set explicitly.
    """
    output_root = _check_output_root(output_root)
    _run_dir(output_root, run_id)
    controller = _controller(output_root)
    try:
        state = controller.start_scoring(run_id, force=force)
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
    _run_dir(output_root, run_id)
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
    """Delete a completed, failed, or cancelled evaluation run."""
    output_root = _check_output_root(output_root)
    _run_dir(output_root, run_id)
    controller = _controller(output_root)
    try:
        state = controller.delete(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to delete evaluation run: {exc}") from exc
    try:
        AppLogService().record_audit(
            action="delete_evaluation_run",
            actor=_actor,
            target_type="evaluation_run",
            target_id=run_id,
            kb_name=state.kb_name,
            metadata={
                "status": state.status,
                "kb_id": state.kb_id,
                "department_id": state.department_id,
                "dataset_path": state.source_dataset_path or state.dataset_path,
                "snapshot_path": state.snapshot_path,
            },
        )
    except Exception:
        pass  # audit is fail-soft and must not turn a successful delete into a 500
    return {"ok": True, "run_id": run_id, "status": state.status}


@router.get("/evaluation/runs/{run_id}")
def get_run(
    run_id: str,
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    _actor: AuthUser = Depends(require_system_admin),
) -> dict[str, Any]:
    """Return the run state plus its summary.json (if present)."""
    output_root = _check_output_root(output_root)
    _run_dir(output_root, run_id)
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
    run_dir = _run_dir(output_root, run_id)
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
            result["summary"] = _summary_dict(summary)
        except Exception:
            result["summary"] = None
    else:
        result["summary"] = None
    if result["summary"] is not None:
        result["sample_results"], result["sample_results_error"] = _load_sample_results(results_dir)
    else:
        result["sample_results"] = []
        result["sample_results_error"] = ""
    if result.get("mode") == "online":
        try:
            result["collection_qc"] = _controller(output_root).load_collection_qc(run_id)
        except Exception:
            result["collection_qc"] = None
    return result


@router.get("/evaluation/runs/{run_id}/compare", response_model=EvaluationCompareResponse)
def compare_run(
    run_id: str,
    baseline: str = Query(..., description="baseline run_id to compare against"),
    output_root: str = Query(default=str(DEFAULT_OUTPUT_ROOT)),
    strict: bool = Query(default=True),
    _actor: AuthUser = Depends(require_system_admin),
) -> EvaluationCompareResponse:
    """Compare two immutable reports, refusing unsafe baselines by default."""
    output_root = _check_output_root(output_root)
    if run_id == baseline:
        raise HTTPException(status_code=400, detail="current run and baseline must be different")
    current_dir = _run_dir(output_root, run_id)
    baseline_dir = _run_dir(output_root, baseline)
    current_summary_path = current_dir / "summary.json"
    baseline_summary_path = baseline_dir / "summary.json"
    if not current_summary_path.is_file():
        raise HTTPException(status_code=404, detail=f"current run {run_id} has no summary")
    if not baseline_summary_path.is_file():
        raise HTTPException(status_code=404, detail=f"baseline run {baseline} has no summary")
    try:
        current_history = load_history_run(current_dir)
        baseline_history = load_history_run(baseline_dir)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"评估历史不可用：{exc}") from exc
    report = compatibility_report(current_history, baseline_history)
    if strict and not report["compatible"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "严格对比被拒绝：运行元数据不兼容",
                "warnings": report["warnings"],
                "compatibility": report["compatibility"],
            },
        )
    warnings = list(report["warnings"])
    if not strict and not report["compatible"]:
        warnings.insert(0, "仅查看对比：结果不满足严格基线条件，不得据此得出回归结论")
    return EvaluationCompareResponse(
        strict=strict,
        compatible=report["compatible"],
        warnings=warnings,
        compatibility=report["compatibility"],
        current=_summary_dict(current_history.summary),
        baseline=_summary_dict(baseline_history.summary),
    )
