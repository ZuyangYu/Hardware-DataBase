from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.evaluation import history_import
from src.evaluation.history_import import (
    ImportApplyError,
    apply_import_plan,
    discover_imports,
)


WORKTREE = Path(__file__).resolve().parents[2]


def _write_report(
    root: Path,
    directory_name: str,
    sample_ids: tuple[str, ...] = ("sample-a",),
    *,
    summary_run_id: str | None = None,
    optional: bool = False,
    nested: bool = False,
) -> Path:
    run_dir = root / directory_name
    run_dir.mkdir(parents=True)
    run_id = summary_run_id or directory_name
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "sample_count": len(sample_ids),
                "successful_samples": len(sample_ids),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "results.jsonl").write_text(
        "".join(json.dumps({"sample_id": sample_id}) + "\n" for sample_id in sample_ids),
        encoding="utf-8",
    )
    if optional:
        (run_dir / "summary.csv").write_text("sample_id\n" + "\n".join(sample_ids) + "\n", encoding="utf-8")
        (run_dir / "report.html").write_text("<html>report</html>\n", encoding="utf-8")
    if nested:
        (run_dir / "snapshots").mkdir()
        (run_dir / "snapshots" / "secret.json").write_text("must not copy", encoding="utf-8")
        (run_dir / "run_state.json").write_text("must not copy", encoding="utf-8")
        (run_dir / "source_manifest.json").write_text("must not copy", encoding="utf-8")
    return run_dir


def _snapshot(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _candidate(plan, name: str):
    return next(candidate for candidate in plan.candidates if candidate.source_path.name == name)


def test_discovery_accepts_mandatory_artifacts_and_copies_optional_sidecars(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    _write_report(source, "run-1", optional=True, nested=True)

    plan = discover_imports(source, target)

    candidate = _candidate(plan, "run-1")
    assert candidate.status == "copy"
    assert candidate.files == ("summary.json", "results.jsonl", "summary.csv", "report.html")
    assert candidate.sample_ids == ["sample-a"]
    assert candidate.cohort_fingerprint
    assert candidate.validation_warnings == []


@pytest.mark.parametrize(
    ("case", "expected_fragment"),
    [
        ("missing-summary", "summary.json"),
        ("missing-results", "results.jsonl"),
        ("empty-results", "nonempty"),
        ("invalid-json", "summary.json"),
        ("invalid-row", "results.jsonl"),
    ],
)
def test_discovery_rejects_incomplete_or_schema_invalid_reports(
    tmp_path: Path, case: str, expected_fragment: str
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    run_dir = _write_report(source, case)
    if case == "missing-summary":
        (run_dir / "summary.json").unlink()
    elif case == "missing-results":
        (run_dir / "results.jsonl").unlink()
    elif case == "empty-results":
        (run_dir / "results.jsonl").write_text("\n", encoding="utf-8")
    elif case == "invalid-json":
        (run_dir / "summary.json").write_text("{not-json}\n", encoding="utf-8")
    elif case == "invalid-row":
        (run_dir / "results.jsonl").write_text(json.dumps({"wrong": "field"}) + "\n", encoding="utf-8")

    plan = discover_imports(source, target)

    candidate = _candidate(plan, case)
    assert candidate.status == "invalid"
    assert expected_fragment in candidate.reason


def test_dry_run_does_not_mutate_source_or_target(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    _write_report(source, "run-1", optional=True, nested=True)
    target.mkdir()
    (target / "existing.txt").write_text("unchanged", encoding="utf-8")
    before_source = _snapshot(source)
    before_target = _snapshot(target)

    plan = discover_imports(source, target)
    assert _candidate(plan, "run-1").status == "copy"

    result = subprocess.run(
        [
            sys.executable,
            str(WORKTREE / "scripts" / "import_evaluation_history.py"),
            str(source),
            str(target),
        ],
        cwd=WORKTREE,
        env={**os.environ, "PYTHONPATH": str(WORKTREE)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout
    assert _snapshot(source) == before_source
    assert _snapshot(target) == before_target


def test_equal_content_is_skipped_and_different_content_is_conflict(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    equal_source = _write_report(source, "same")
    different_source = _write_report(source, "different", sample_ids=("sample-a",))
    target.mkdir()
    equal_target = target / "same"
    equal_target.mkdir()
    for name in ("summary.json", "results.jsonl"):
        (equal_target / name).write_bytes((equal_source / name).read_bytes())
    different_target = target / "different"
    different_target.mkdir()
    (different_target / "summary.json").write_text(
        json.dumps({"run_id": "different", "sample_count": 1, "successful_samples": 1, "metadata": {"old": True}}),
        encoding="utf-8",
    )
    (different_target / "results.jsonl").write_bytes((different_source / "results.jsonl").read_bytes())

    plan = discover_imports(source, target)

    assert _candidate(plan, "same").status == "skip_equal"
    assert _candidate(plan, "different").status == "conflict"
    before = _snapshot(different_target)
    result = apply_import_plan(plan)
    assert result.published == []
    assert _snapshot(different_target) == before
    assert _candidate(plan, "different").status == "conflict"


def test_apply_publishes_canonical_report_and_sidecars_atomically(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    run_dir = _write_report(source, "legacy-name", ("b", "a"), optional=True, nested=True, summary_run_id="summary-id")

    plan = discover_imports(source, target)
    result = apply_import_plan(plan)

    assert result.published == [target / "legacy-name"]
    published = target / "legacy-name"
    assert not any(path.name.startswith(".import-") for path in target.iterdir())
    assert set(path.name for path in published.iterdir()) == {
        "summary.json",
        "results.jsonl",
        "summary.csv",
        "report.html",
        "import_manifest.json",
        "report_complete.json",
    }
    for name in ("summary.json", "results.jsonl", "summary.csv", "report.html"):
        assert hashlib.sha256((published / name).read_bytes()).hexdigest() == hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
    manifest = json.loads((published / "import_manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["source_root"] == str(source.resolve())
    assert manifest["source_path"] == str(run_dir.resolve())
    assert manifest["source_directory_name"] == "legacy-name"
    assert manifest["summary_run_id"] == "summary-id"
    assert manifest["sample_ids"] == ["a", "b"]
    assert manifest["cohort_fingerprint"]
    assert manifest["validation_warnings"] == [
        "run directory name 'legacy-name' differs from summary.run_id 'summary-id'"
    ]
    assert json.loads((published / "report_complete.json").read_text(encoding="utf-8"))["run_id"] == "summary-id"


@pytest.mark.parametrize("failure", ["copy", "hash", "rename"])
def test_apply_failure_leaves_no_final_or_temporary_directory(tmp_path: Path, monkeypatch, failure: str):
    source = tmp_path / "source"
    target = tmp_path / "target"
    run_dir = _write_report(source, "run-1", optional=True)
    plan = discover_imports(source, target)
    source_before = _snapshot(source)

    if failure == "copy":
        original = history_import.shutil.copy2

        def fail_copy(src, dst, *args, **kwargs):
            if Path(src).name == "results.jsonl":
                raise OSError("injected copy failure")
            return original(src, dst, *args, **kwargs)

        monkeypatch.setattr(history_import.shutil, "copy2", fail_copy)
    elif failure == "hash":
        original = history_import._sha256_file
        calls = 0

        def fail_hash(path):
            nonlocal calls
            calls += 1
            if calls > len(plan.candidates[0].files):
                raise OSError("injected hash failure")
            return original(path)

        monkeypatch.setattr(history_import, "_sha256_file", fail_hash)
    else:
        original = history_import.os.replace

        def fail_replace(src, dst):
            if Path(src).name.startswith(".import-"):
                raise OSError("injected rename failure")
            return original(src, dst)

        monkeypatch.setattr(history_import.os, "replace", fail_replace)

    with pytest.raises(ImportApplyError):
        apply_import_plan(plan)

    assert not (target / run_dir.name).exists()
    assert not target.exists() or not any(path.name.startswith(".import-") for path in target.iterdir())
    assert _snapshot(source) == source_before


def test_mismatch_retains_directory_identity_and_records_warning(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    _write_report(source, "historical-folder", summary_run_id="actual-run-id")

    plan = discover_imports(source, target)
    candidate = _candidate(plan, "historical-folder")
    assert candidate.status == "copy"
    assert candidate.target_path.name == "historical-folder"
    assert candidate.validation_warnings == [
        "run directory name 'historical-folder' differs from summary.run_id 'actual-run-id'"
    ]
    apply_import_plan(plan)
    assert (target / "historical-folder").is_dir()
    assert not (target / "actual-run-id").exists()


def test_apply_rejects_new_optional_artifact_after_discovery(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    run_dir = _write_report(source, "run-1")
    plan = discover_imports(source, target)
    (run_dir / "report.html").write_text("added after discovery", encoding="utf-8")

    with pytest.raises(ImportApplyError, match="artifact set changed"):
        apply_import_plan(plan)

    assert not (target / "run-1").exists()
