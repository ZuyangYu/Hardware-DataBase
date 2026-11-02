"""Runner-side Runtime Contract v1 execution tests (no OCI runtime is started)."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner(tmp_path, monkeypatch):
    module = _load(ROOT / "deploy/sandbox-runner/runner.py", "hdb_runtime_contract_runner")
    monkeypatch.setattr(module, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(module, "WORKSPACE_ROOT", str(tmp_path / "workspace"))
    module._init_db()
    return module


def _task(module, *, attempt: int = 1):
    artifact_id = "art-" + "a" * 32
    content = b"pin,function\n1,gpio\n"
    module._insert_task(
        task_id="task-" + "b" * 24,
        bundle_ref="bundle/skill-diagram",
        bundle_digest="c" * 64,
        profile_ref="profile/dev-l1",
        execution_id="exec-" + "d" * 24,
        attempt=attempt,
        parameters={"title": "demo"},
        input_manifest=[
            {
                "artifact_id": artifact_id,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        ],
    )
    return module._get_task("task-" + "b" * 24), artifact_id, content


def test_runner_stages_inputs_and_persists_result_and_events(runner, monkeypatch):
    module = runner
    task, artifact_id, content = _task(module)
    bundle = {
        "bundle_ref": "bundle/skill-diagram",
        "entrypoint_path": "/opt/bundle/x/entrypoint.py",
        "image_digest": "sha256:" + "e" * 64,
    }

    def fake_run(task_id, workspace, entrypoint, image, *, timeout, attempt=1):
        asset = Path(workspace) / "in" / "assets" / artifact_id / "pins.csv"
        assert asset.read_bytes() == content
        assert ((Path(workspace) / "in" / "parameters.json").read_text(encoding="utf-8"))
        out = Path(workspace) / "out"
        (out / "nested").mkdir(parents=True, exist_ok=True)
        (out / "result.json").write_text(
            json.dumps(
                {
                    "runtime_contract_version": "v1",
                    "result_status": "completed",
                    "artifact_paths": ["spec.json", "nested/diagram.svg"],
                    "checkpoint_path": "spec.json",
                }
            ),
            encoding="utf-8",
        )
        (out / "spec.json").write_text('{"nodes": []}', encoding="utf-8")
        (out / "nested" / "diagram.svg").write_text("<svg></svg>", encoding="utf-8")
        events = Path(workspace) / "events"
        events.mkdir(parents=True, exist_ok=True)
        payloads = [
            {
                "runtime_contract_version": "v1",
                "event_id": "evt-1",
                "seq": 1,
                "task_id": task_id,
                "attempt": 1,
                "step_id": "build_spec",
                "event": "step_started",
                "status": "running",
            },
            {
                "runtime_contract_version": "v1",
                "event_id": "evt-2",
                "seq": 2,
                "task_id": task_id,
                "attempt": 1,
                "step_id": "build_spec",
                "event": "step_completed",
                "status": "ok",
                "progress": 0.5,
            },
        ]
        (events / "events.jsonl").write_text(
            "\n".join(json.dumps(item) for item in payloads) + "\n",
            encoding="utf-8",
        )
        return 0, b""

    monkeypatch.setattr(module, "_run_container", fake_run)
    module._execute_task(task, bundle, {"title": "demo"}, [{
        "artifact_id": artifact_id,
        "filename": "pins.csv",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "content": content,
    }])

    updated = module._get_task(task["task_id"])
    assert updated["status"] == "succeeded"
    assert updated["result"]["result_status"] == "completed"
    assert [item["seq"] for item in updated["events"]] == [1, 2]
    filenames = {item["filename"] for item in updated["manifest"]["items"]}
    assert filenames == {"result.json", "spec.json", "nested__diagram.svg"}
    by_relative = {
        item["relative_path"]: item["artifact_id"] for item in updated["manifest"]["items"]
    }
    assert updated["result"]["artifact_ids"] == [
        by_relative["spec.json"],
        by_relative["nested/diagram.svg"],
    ]
    assert updated["result"]["checkpoint_artifact_id"] == by_relative["spec.json"]
    assert all("content_base64" not in item for item in updated["manifest"]["items"])
    collected = module._inline_manifest(updated["manifest"])
    by_name = {item["filename"]: item for item in collected["items"]}
    assert base64.b64decode(by_name["nested__diagram.svg"]["content_base64"]) == b"<svg></svg>"


def test_runner_rejects_non_monotonic_event_stream(runner, monkeypatch):
    module = runner
    task, artifact_id, content = _task(module)
    bundle = {
        "bundle_ref": "bundle/skill-diagram",
        "entrypoint_path": "/opt/bundle/x/entrypoint.py",
        "image_digest": "sha256:" + "e" * 64,
    }

    def fake_run(task_id, workspace, entrypoint, image, *, timeout, attempt=1):
        out = Path(workspace) / "out"
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.json").write_text(
            json.dumps({"runtime_contract_version": "v1", "result_status": "completed"}),
            encoding="utf-8",
        )
        events = Path(workspace) / "events"
        events.mkdir(parents=True, exist_ok=True)
        duplicate = {
            "runtime_contract_version": "v1",
            "event_id": "evt-same",
            "seq": 1,
            "task_id": task_id,
            "attempt": 1,
            "step_id": "step",
            "event": "step_started",
        }
        (events / "events.jsonl").write_text(
            json.dumps(duplicate) + "\n" + json.dumps(duplicate) + "\n",
            encoding="utf-8",
        )
        return 0, b""

    monkeypatch.setattr(module, "_run_container", fake_run)
    module._execute_task(task, bundle, {"title": "demo"}, [{
        "artifact_id": artifact_id,
        "filename": "pins.csv",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "content": content,
    }])

    updated = module._get_task(task["task_id"])
    assert updated["status"] == "failed"
    assert updated["error_code"] == "runtime_event_invalid"
    assert updated["result"]["result_status"] == "completed"


def test_nonzero_exit_preserves_validation_report_and_result(runner, monkeypatch):
    module = runner
    task, artifact_id, content = _task(module)
    bundle = {
        "bundle_ref": "bundle/skill-diagram",
        "entrypoint_path": "/opt/bundle/x/entrypoint.py",
        "image_digest": "sha256:" + "e" * 64,
    }

    def fake_run(task_id, workspace, entrypoint, image, *, timeout, attempt=1):
        out = Path(workspace) / "out"
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.json").write_text(
            json.dumps(
                {
                    "runtime_contract_version": "v1",
                    "result_status": "validation_failed",
                    "issues": [{"code": "missing_interface"}],
                    "artifact_paths": ["report.json"],
                }
            ),
            encoding="utf-8",
        )
        (out / "report.json").write_text('{"error": "missing_interface"}', encoding="utf-8")
        return 1, b""

    monkeypatch.setattr(module, "_run_container", fake_run)
    module._execute_task(task, bundle, {"title": "demo"}, [{
        "artifact_id": artifact_id,
        "filename": "pins.csv",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "content": content,
    }])

    updated = module._get_task(task["task_id"])
    assert updated["status"] == "failed"
    assert updated["error_code"] == "bundle_failed"
    assert updated["result"]["result_status"] == "validation_failed"
    filenames = {item["filename"] for item in updated["manifest"]["items"]}
    assert {"result.json", "report.json"} <= filenames


def test_output_collection_rejects_symlinks_and_unknown_extensions(runner, tmp_path):
    module = runner
    workspace = tmp_path / "workspace"
    out = workspace / "out"
    out.mkdir(parents=True)
    target = tmp_path / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    (out / "link.txt").symlink_to(target)
    with pytest.raises(HTTPException) as linked:
        module._collect_outputs(str(workspace), "exec-1", "task-" + "a" * 24)
    assert linked.value.detail["error"]["code"] == "artifact_path_invalid"

    (out / "link.txt").unlink()
    (out / "payload.exe").write_bytes(b"MZ")
    with pytest.raises(HTTPException) as extension:
        module._collect_outputs(str(workspace), "exec-1", "task-" + "a" * 24)
    assert extension.value.detail["error"]["code"] == "artifact_extension_denied"


def test_running_task_projects_live_step_events(runner, tmp_path):
    module = runner
    task, _, _ = _task(module)
    workspace = tmp_path / "live-workspace"
    events = workspace / "events"
    events.mkdir(parents=True)
    (events / "events.jsonl").write_text(
        json.dumps(
            {
                "runtime_contract_version": "v1",
                "event_id": "evt-live-1",
                "seq": 1,
                "task_id": task["task_id"],
                "attempt": 1,
                "step_id": "render",
                "event": "step_progress",
                "progress": 0.4,
                "message": "rendering",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    module._sync_runtime_events(task["task_id"], str(workspace), 1)

    updated = module._get_task(task["task_id"])
    assert updated["events"][0]["step_id"] == "render"
    assert updated["events"][0]["progress"] == 0.4


def test_runner_bounds_stdout_consumption_without_blocking_on_flood(runner, monkeypatch):
    module = runner

    class FloodProcess:
        returncode = 0

        def __init__(self, *_args, **_kwargs):
            self.stdout = io.BytesIO(b"x" * (module.MAX_STDOUT_BYTES * 3))

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(module.subprocess, "Popen", FloodProcess)
    code, stdout = module._run_container(
        "task-" + "1" * 24,
        "/tmp/workspace",
        "/opt/bundle/entrypoint.py",
        "sha256:" + "a" * 64,
        timeout=1,
    )

    assert code == 0
    assert len(stdout) == module.MAX_STDOUT_BYTES


def test_runner_task_admission_returns_while_long_execution_is_running(runner, monkeypatch):
    module = runner
    monkeypatch.setattr(module, "_reconcile_tasks", lambda: {"reconciled": True})
    monkeypatch.setattr(module, "_authenticate", lambda *_args, **_kwargs: None)
    digest = "b" * 64
    monkeypatch.setattr(
        module,
        "_load_bundle",
        lambda _ref: {
            "digest": digest,
            "profile_ref": "profile/dev-l1",
            "input_schema": {},
        },
    )
    started = threading.Event()
    release = threading.Event()

    def slow_execute(task, _bundle, _parameters, _inputs=None):
        started.set()
        assert release.wait(5)
        module._update_task(
            task["task_id"],
            expected_statuses=("running",),
            status="succeeded",
            observed_state="succeeded",
            cleanup_state="complete",
        )

    monkeypatch.setattr(module, "_execute_task", slow_execute)
    headers = {
        "Authorization": "Bearer test",
        "X-Audience": "sandbox-runner",
        "Idempotency-Key": "long-task-one",
    }
    payload = {
        "task_id": "pending",
        "bundle_ref": "bundle/skill-long",
        "bundle_digest": digest,
        "profile_ref": "profile/dev-l1",
        "parameters": {},
    }
    from fastapi.testclient import TestClient

    with TestClient(module.app) as client:
        began = time.monotonic()
        first = client.post("/v1/tasks", json=payload, headers=headers)
        elapsed = time.monotonic() - began
        assert first.status_code == 202, first.text
        assert elapsed < 1.0
        assert started.wait(2)

        second = client.post(
            "/v1/tasks",
            json={**payload, "task_id": "pending"},
            headers={**headers, "Idempotency-Key": "long-task-two"},
        )
        assert second.status_code == 202, second.text
        release.set()


def test_bundle_source_manifest_digest_detects_auxiliary_file_changes(runner, monkeypatch, tmp_path):
    module = runner
    root = tmp_path / "bundles"
    directory = "source-identity"
    bundle_dir = root / directory
    bundle_dir.mkdir(parents=True)
    entrypoint = b"print('entrypoint')\n"
    step = b"VALUE = 1\n"
    entrypoint_digest = hashlib.sha256(entrypoint).hexdigest()
    profile_digest = module._profile_digest()
    files = [
        {"path": "entrypoint.py", "sha256": entrypoint_digest},
        {"path": "steps/render.py", "sha256": hashlib.sha256(step).hexdigest()},
    ]
    source_digest = module._source_digest(
        files,
        entrypoint_digest=entrypoint_digest,
        dependency_index_digest="",
        profile_digest=profile_digest,
    )
    (bundle_dir / "entrypoint.py").write_bytes(entrypoint)
    manifest = {
        "bundle_ref": f"bundle/{directory}",
        "profile_ref": "profile/dev-l1",
        "profile_digest": profile_digest,
        "image_digest": "sha256:" + "a" * 64,
        "entrypoint_digest": entrypoint_digest,
        "source_digest": source_digest,
        "source_manifest": files,
        "entrypoint_path": f"/opt/bundle/{directory}/entrypoint.py",
    }
    (bundle_dir / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(module, "BUNDLE_ROOT", str(root))
    monkeypatch.setattr(module, "_inspect_image", lambda *args: None)

    loaded = module._load_bundle(f"bundle/{directory}")
    assert loaded["digest"] == source_digest

    manifest["source_manifest"][1]["sha256"] = "0" * 64
    (bundle_dir / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(HTTPException) as changed:
        module._load_bundle(f"bundle/{directory}")
    assert changed.value.detail["error"]["code"] == "digest_mismatch"


def test_input_transfer_rejects_digest_mismatch_and_traversal(runner):
    module = runner
    artifact_id = "art-" + "f" * 32
    with pytest.raises(HTTPException) as mismatch:
        module._decode_input_artifacts(
            {
                "input_artifact_ids": [artifact_id],
                "input_artifacts": [
                    {
                        "artifact_id": artifact_id,
                        "filename": "pins.csv",
                        "sha256": "0" * 64,
                        "content_base64": base64.b64encode(b"x").decode("ascii"),
                    }
                ],
            }
        )
    assert mismatch.value.detail["error"]["code"] == "input_digest_mismatch"

    content = b"x"
    with pytest.raises(HTTPException) as traversal:
        module._decode_input_artifacts(
            {
                "input_artifact_ids": [artifact_id],
                "input_artifacts": [
                    {
                        "artifact_id": artifact_id,
                        "filename": "../pins.csv",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "content_base64": base64.b64encode(content).decode("ascii"),
                    }
                ],
            }
        )
    assert traversal.value.detail["error"]["code"] == "invalid_input"
