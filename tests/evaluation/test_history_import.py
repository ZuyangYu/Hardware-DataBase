from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
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


def _snapshot(root: Path) -> dict[str, tuple[str, str]]:
    """Capture content and path type without following symlinks."""

    result: dict[str, tuple[str, str]] = {}
    if not os.path.lexists(root):
        return {".": ("missing", "")}

    def visit(path: Path, relative: str) -> None:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            result[relative] = ("symlink", os.readlink(path))
            return
        if stat.S_ISDIR(mode):
            result[relative] = ("directory", "")
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child, child.name if relative == "." else f"{relative}/{child.name}")
            return
        if stat.S_ISREG(mode):
            result[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
            return
        result[relative] = ("other", oct(stat.S_IFMT(mode)))

    visit(root, ".")
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
    (target / "existing-directory").mkdir()
    (target / "existing-link").symlink_to("existing.txt")
    (source / "source-link").symlink_to("run-1", target_is_directory=True)
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
        original = history_import._copy_artifact

        def fail_copy(source_fd, destination_directory_fd, name):
            if name == "results.jsonl":
                raise OSError("injected copy failure")
            return original(source_fd, destination_directory_fd, name)

        monkeypatch.setattr(history_import, "_copy_artifact", fail_copy)
    elif failure == "hash":
        original = history_import._sha256_fd
        calls = 0

        def fail_hash(fd):
            nonlocal calls
            calls += 1
            if calls > len(plan.candidates[0].files):
                raise OSError("injected hash failure")
            return original(fd)

        monkeypatch.setattr(history_import, "_sha256_fd", fail_hash)
    else:
        def fail_rename_noreplace(*args, **kwargs):
            raise OSError("injected rename failure")

        monkeypatch.setattr(history_import, "_rename_noreplace", fail_rename_noreplace)

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


def test_atomic_publication_preserves_target_created_immediately_before_rename(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    _write_report(source, "run-1")
    plan = discover_imports(source, target)
    original = history_import._rename_noreplace

    def race_with_target(*args, **kwargs):
        final = target / "run-1"
        final.mkdir()
        (final / "attacker-marker").write_text("preserve", encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(history_import, "_rename_noreplace", race_with_target)

    with pytest.raises(ImportApplyError, match="target appeared"):
        apply_import_plan(plan)

    assert (target / "run-1" / "attacker-marker").read_text(encoding="utf-8") == "preserve"
    assert not any(path.name.startswith(".import-") for path in target.iterdir())


def test_apply_rejects_source_run_directory_symlink_swap(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    run_dir = _write_report(source, "run-1")
    plan = discover_imports(source, target)
    original = source / "original-run"
    run_dir.rename(original)
    run_dir.symlink_to(original, target_is_directory=True)

    with pytest.raises(ImportApplyError, match="source run directory"):
        apply_import_plan(plan)

    assert not (target / "run-1").exists()


def test_apply_rejects_source_artifact_symlink_swap(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    run_dir = _write_report(source, "run-1")
    plan = discover_imports(source, target)
    original = run_dir / "original-summary.json"
    (run_dir / "summary.json").rename(original)
    (run_dir / "summary.json").symlink_to(original.name)

    with pytest.raises(ImportApplyError, match="source artifact"):
        apply_import_plan(plan)

    assert not (target / "run-1").exists()


def test_apply_reads_anchored_artifact_if_path_is_swapped_during_copy(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    run_dir = _write_report(source, "run-1")
    plan = discover_imports(source, target)
    expected_hash = _candidate(plan, "run-1").source_hashes["summary.json"]
    original_copy = history_import._copy_artifact
    swapped = False

    def swap_then_copy(source_fd, destination_directory_fd, name):
        nonlocal swapped
        if name == "summary.json" and not swapped:
            swapped = True
            (run_dir / name).rename(run_dir / "original-summary.json")
            (run_dir / "attacker-summary.json").write_text(
                json.dumps({"run_id": "attacker", "sample_count": 0}) + "\n",
                encoding="utf-8",
            )
            (run_dir / name).symlink_to("attacker-summary.json")
        return original_copy(source_fd, destination_directory_fd, name)

    monkeypatch.setattr(history_import, "_copy_artifact", swap_then_copy)

    result = apply_import_plan(plan)

    published_summary = result.published[0] / "summary.json"
    assert hashlib.sha256(published_summary.read_bytes()).hexdigest() == expected_hash
    assert json.loads(published_summary.read_text(encoding="utf-8"))["run_id"] == "run-1"


def test_apply_rejects_target_root_symlink_redirection(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    redirected = tmp_path / "redirected"
    _write_report(source, "run-1")
    target.mkdir()
    redirected.mkdir()
    plan = discover_imports(source, target)
    target.rename(tmp_path / "original-target")
    target.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(ImportApplyError, match="target root"):
        apply_import_plan(plan)

    assert list(redirected.iterdir()) == []


def test_apply_revalidates_stale_skip_equal_as_conflict(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source_run = _write_report(source, "run-1")
    target_run = target / "run-1"
    target_run.mkdir(parents=True)
    for name in ("summary.json", "results.jsonl"):
        (target_run / name).write_bytes((source_run / name).read_bytes())
    plan = discover_imports(source, target)
    assert _candidate(plan, "run-1").status == "skip_equal"
    (target_run / "results.jsonl").write_text(
        json.dumps({"sample_id": "changed"}) + "\n", encoding="utf-8"
    )

    result = apply_import_plan(plan)

    assert result.skipped == []
    assert result.conflicts == [target_run]
    assert _candidate(plan, "run-1").status == "conflict"


def test_file_fsync_error_propagates_and_cleans_staging(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    _write_report(source, "run-1")
    plan = discover_imports(source, target)
    original = history_import.os.fsync

    def fail_regular_file_fsync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("injected file fsync failure")
        return original(fd)

    monkeypatch.setattr(history_import.os, "fsync", fail_regular_file_fsync)

    with pytest.raises(ImportApplyError, match="file fsync failure"):
        apply_import_plan(plan)

    assert not (target / "run-1").exists()
    assert not any(path.name.startswith(".import-") for path in target.iterdir())


def test_unexpected_directory_fsync_error_propagates(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    _write_report(source, "run-1")
    plan = discover_imports(source, target)
    original = history_import.os.fsync

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected directory fsync failure")
        return original(fd)

    monkeypatch.setattr(history_import.os, "fsync", fail_directory_fsync)

    with pytest.raises(ImportApplyError, match="directory fsync failure"):
        apply_import_plan(plan)

    assert not (target / "run-1").exists()


def test_explicitly_unsupported_directory_fsync_is_tolerated(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    _write_report(source, "run-1")
    plan = discover_imports(source, target)
    original = history_import.os.fsync

    def unsupported_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(history_import.errno.EINVAL, "directory fsync unsupported")
        return original(fd)

    monkeypatch.setattr(history_import.os, "fsync", unsupported_directory_fsync)

    result = apply_import_plan(plan)

    assert result.published == [target / "run-1"]


def test_cleanup_failure_is_surfaced_with_primary_apply_error(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    _write_report(source, "run-1")
    plan = discover_imports(source, target)

    def fail_rename(*args, **kwargs):
        raise OSError("injected publication failure")

    def fail_cleanup(*args, **kwargs):
        raise PermissionError("injected cleanup failure")

    monkeypatch.setattr(history_import, "_rename_noreplace", fail_rename)
    monkeypatch.setattr(history_import.shutil, "rmtree", fail_cleanup)

    with pytest.raises(ImportApplyError, match="cleanup failed.*injected cleanup failure"):
        apply_import_plan(plan)

    assert any(path.name.startswith(".import-") for path in target.iterdir())


def test_missing_renameat2_fails_closed_and_cleans_staging(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    _write_report(source, "run-1")
    plan = discover_imports(source, target)

    class LibcWithoutRenameat2:
        pass

    monkeypatch.setattr(
        history_import.ctypes,
        "CDLL",
        lambda *args, **kwargs: LibcWithoutRenameat2(),
    )

    with pytest.raises(ImportApplyError, match="no-replace publication is unavailable"):
        apply_import_plan(plan)

    assert not (target / "run-1").exists()
    assert not any(path.name.startswith(".import-") for path in target.iterdir())
