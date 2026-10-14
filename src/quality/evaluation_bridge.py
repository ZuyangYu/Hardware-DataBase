"""Governed production datasets use the existing leased evaluation worker."""
from __future__ import annotations

import shutil
import tempfile
import hashlib
import time
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

import src.settings as settings
from src.core.auth import AuthService
from src.evaluation.reproducibility import EvaluationReproducibilityError, execution_content_hash
from src.evaluation.schemas import AnswerSnapshot, EvaluationRunState, EvaluationSample
from src.pipelines.document_rag.schemas import kb_scope_key
from .workflow import WorkflowStore


class DatasetEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["online", "offline"] = "offline"
    system_prompt: str = Field(default="仅依据给定冻结证据回答问题。证据不足时明确说明，不虚构事实。", min_length=1, max_length=4000)


def generator_identity():
    from src.core.model_factory import resolve_chat_model_config

    config = asdict(resolve_chat_model_config(priority="batch"))
    config.pop("api_key")
    config["endpoint_sha256"] = hashlib.sha256(config.pop("endpoint").encode()).hexdigest()
    return config


class FixedEvidenceRunner:
    """Prompt/model repair experiment; uses no live KB and no gold reference."""
    def __init__(self, binding):
        self.binding = binding

    def collect(self, sample):
        from src.core.model_factory import create_chat_model
        from src.core.chat_model_runtime import invoke_text
        from .store import now_iso

        if generator_identity() != self.binding["generator_identity"]:
            raise EvaluationReproducibilityError("fixed-evidence generation settings changed; create a new run")
        start, started_at = time.monotonic(), now_iso()
        response = invoke_text(create_chat_model(priority="batch"),
            [{"role": "system", "content": self.binding["system_prompt"]},
             {"role": "user", "content": "冻结证据：\n" + "\n\n".join(sample.reference_contexts) + "\n\n问题：" + sample.question}],
            operation="fixed_evidence_answering", profile="default")
        return AnswerSnapshot(sample_id=sample.id, question=sample.question, kb_name=sample.kb_name,
            response=response.text, retrieved_contexts=sample.reference_contexts,
            started_at=started_at, finished_at=now_iso(), duration_seconds=time.monotonic()-start,
            access_check={"expected": "allowed", "observed": "allowed", "reason": "authorized frozen evidence"}, metadata={"dataset_version_id": self.binding["dataset_version_id"],
                "evaluation_adapter": "fixed_evidence_answering", "source_policy": "fixed_retrieved_evidence"})


def validate_binding(state, *, viewer=None, workflow=None, auth=None):
    binding = state.quality_binding
    if not binding:
        raise PermissionError("run is not a governed production dataset run")
    if binding.get("material_invalidated"):
        raise EvaluationReproducibilityError("production dataset material was invalidated")
    auth = auth or AuthService()
    actor = auth.get_user_by_id(binding["actor_user_id"])
    if actor is None or not actor.is_active:
        raise EvaluationReproducibilityError("production dataset actor is unavailable")
    try:
        store = workflow or WorkflowStore()
        version = store.version(binding["dataset_version_id"], actor, materialize=True)
        if viewer is not None:
            if not viewer.is_active:
                raise PermissionError("production dataset viewer is unavailable")
            version = store.version(binding["dataset_version_id"], viewer, materialize=True)
    except (PermissionError, KeyError, ValueError) as exc:
        raise EvaluationReproducibilityError("production dataset material authorization is no longer valid") from exc
    digest = execution_content_hash([EvaluationSample.model_validate(s) for s in version["samples"]])
    if version["manifest_sha256"] != binding["manifest_sha256"] or digest != state.execution_content_sha256:
        raise EvaluationReproducibilityError("production dataset provenance no longer matches run inputs")
    return version


def create_dataset_run(workflow, version_id, viewer, body, controller, *, auth=None):
    version = workflow.version(version_id, viewer, materialize=True)
    samples = [EvaluationSample.model_validate(s) for s in version["samples"]]
    if len({s.kb_name for s in samples}) != 1:
        raise ValueError("one evaluation run must target exactly one KB")
    auth = auth or AuthService()
    # A designated reviewer can read a copy, but cannot impersonate its owner
    # to launch business calls. Live regeneration is explicitly owner-bound.
    if body.mode == "online" and any(s.request_context["user_id"] != viewer.id for s in samples):
        raise PermissionError("live repair evaluation must be launched by the original answer owner")
    name = samples[0].kb_name
    kb_id = auth.get_knowledge_base_id(name, department_id=viewer.department_id)
    permission = auth.get_kb_permissions_for_user(viewer).get(kb_scope_key(name, viewer.department_id))
    if kb_id is None or permission not in {"read", "write", "admin"}:
        raise PermissionError("dataset knowledge base is not currently accessible")
    binding = {"dataset_version_id": version_id, "manifest_sha256": version["manifest_sha256"],
               "actor_user_id": viewer.id, "schema_version": 1}
    if body.mode == "online":
        binding.update(generator_identity=generator_identity(), system_prompt=body.system_prompt,
                       evaluation_adapter="fixed_evidence_answering")
    private = Path(settings.STORAGE_DIR) / "quality-evaluation-inputs"
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="dataset-", dir=private))
    dataset = directory / "dataset.jsonl"
    snapshot = directory / "snapshot.jsonl"
    kwargs = dict(dataset_path=dataset, output_root=controller.state_root, samples=samples, kb_id=kb_id,
                  kb_name=name, department_id=viewer.department_id, created_by=viewer.username,
                  snapshot_ownership_verified=True, quality_binding=binding)
    try:
        dataset.write_text("".join(s.model_dump_json() + '\n' for s in samples), encoding="utf-8")
        snapshot.write_text("".join(AnswerSnapshot.model_validate(s).model_dump_json() + '\n' for s in version["snapshots"]), encoding="utf-8")
        dataset.chmod(0o600)
        snapshot.chmod(0o600)
        state = controller.create_online_run(**kwargs) if body.mode == "online" else controller.create_offline_run(snapshot_path=snapshot, **kwargs)
        run_dir = controller.state_root / state.run_id
        run_dir.chmod(0o700)
        # Keep every frozen derivative in the governed run directory, never in
        # the admin upload/snapshot areas. Initial materialization is temporary.
        if body.mode == "offline":
            target = run_dir / "production-snapshot.jsonl"
            shutil.copyfile(snapshot, target)
            target.chmod(0o600)
            controller._store(state.run_id).mutate(lambda current: current.model_copy(update={"snapshot_path": str(target)}))
            controller._record_snapshot_manifest(controller._store(state.run_id))
        controller._store(state.run_id).mutate(lambda current: current.model_copy(update={"source_dataset_path": "quality://" + version_id}))
        return controller._store(state.run_id).load()
    finally:
        shutil.rmtree(directory)


def purge_run_material(run_dir, state):
    """Retain non-content state/provenance; remove invalid derived materials."""
    from src.evaluation.run_control import RunStateStore

    run_dir = Path(run_dir).resolve()
    store = RunStateStore(run_dir / "run_state.json")
    # The lock also holds execution_write_guard in a worker context. Verify
    # the fencing token before deleting any artifact belonging to a successor.
    with store._lock:
        _purge_material_files(run_dir)
        store.mutate(lambda current: current.model_copy(update={
            "current_question": "", "error_message": "production dataset authorization invalidated",
            "status": "failed", "report_path": "",
            "quality_binding": {**current.quality_binding, "material_invalidated": True},
        }))


def _purge_material_files(run_dir):
    managed = {"execution_dataset.jsonl", "production-snapshot.jsonl", "snapshot.jsonl", "summary.json",
               "results.jsonl", "summary.csv", "report.html", "report_complete.json", "manifest.json", "snapshot.manifest.json", "collection_qc.json", ".checkpoint"}
    for path in run_dir.iterdir():
        if path.name not in managed:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
def reconcile_invalid_runs(controller, *, auth=None, workflow=None):
    """Called by the quality worker after revocation/expiration/source deletion."""
    invalidated = 0
    from src.evaluation.run_control import discover_run_dirs

    for path in discover_run_dirs(controller.state_root):
        try:
            state = EvaluationRunState.model_validate_json((path / "run_state.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            continue  # Legacy reports and independently deleted/invalid state.
        if not state.quality_binding or state.quality_binding.get("material_invalidated"):
            continue
        try:
            validate_binding(state, auth=auth, workflow=workflow)
        except EvaluationReproducibilityError:
            # Active workers will check at the next sample/checkpoint and purge
            # in their fenced owner context. Do not overwrite a live owner.
            if controller.ownership.has_live_owner(state.run_id):
                controller.cancel(state.run_id)
                continue
            store = controller._store(state.run_id)
            try:
                with store._lock, controller.ownership.idle_guard(state.run_id):
                    purge_run_material(path, store.load())
            except RuntimeError:
                continue  # A worker claimed between the read and idle guard.
            invalidated += 1
    return invalidated
