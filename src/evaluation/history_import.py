"""Safe, one-time importing of immutable evaluation report history.

The importer intentionally has a very small source surface: only the report
artifacts (``summary.json``, ``results.jsonl`` and optional ``summary.csv`` /
``report.html``) are read and copied.  Discovery is read-only and the command
line entry point therefore defaults to a dry-run.  Applying a plan publishes
each eligible report through a same-filesystem temporary directory followed
by one atomic rename.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
from typing import Any, Literal

from .history import cohort_fingerprint
from .schemas import EvaluationSummary, SampleResult


MANDATORY_ARTIFACTS = ("summary.json", "results.jsonl")
OPTIONAL_ARTIFACTS = ("summary.csv", "report.html")
REPORT_ARTIFACTS = MANDATORY_ARTIFACTS + OPTIONAL_ARTIFACTS
ImportStatus = Literal["copy", "skip_equal", "conflict", "invalid"]
IMPORT_MANIFEST_SCHEMA_VERSION = 1


class ImportApplyError(RuntimeError):
    """Raised when an eligible report cannot be published atomically."""


@dataclass
class ImportCandidate:
    """Discovery result for one immediate child of the source root."""

    source_path: Path
    target_path: Path
    status: ImportStatus
    files: tuple[str, ...] = ()
    source_hashes: dict[str, str] = field(default_factory=dict)
    summary: EvaluationSummary | None = None
    results: tuple[SampleResult, ...] = ()
    sample_ids: list[str] = field(default_factory=list)
    cohort_fingerprint: str = ""
    validation_warnings: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def classification(self) -> ImportStatus:
        """Alias used by callers that call a plan item a classification."""

        return self.status

    @property
    def file_sha256(self) -> dict[str, str]:
        """Manifest-compatible alias for the discovery hashes."""

        return self.source_hashes

    @property
    def error(self) -> str:
        return self.reason

    @property
    def run_id(self) -> str | None:
        return self.summary.run_id if self.summary is not None else None


@dataclass
class ImportPlan:
    """A read-only description of all report directories to import."""

    source_root: Path
    target_root: Path
    candidates: list[ImportCandidate] = field(default_factory=list)

    @property
    def copy_candidates(self) -> list[ImportCandidate]:
        return [candidate for candidate in self.candidates if candidate.status == "copy"]

    @property
    def skip_equal(self) -> list[ImportCandidate]:
        return [candidate for candidate in self.candidates if candidate.status == "skip_equal"]

    @property
    def conflicts(self) -> list[ImportCandidate]:
        return [candidate for candidate in self.candidates if candidate.status == "conflict"]

    @property
    def invalid(self) -> list[ImportCandidate]:
        return [candidate for candidate in self.candidates if candidate.status == "invalid"]

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


@dataclass
class ImportResult:
    """Outcome of applying a plan.

    Invalid and conflicting candidates are reported in the result and are not
    touched.  A copy/manifest/rename failure raises :class:`ImportApplyError`;
    the failing report's temporary directory has been removed before that
    exception escapes.
    """

    published: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    conflicts: list[Path] = field(default_factory=list)
    invalid: list[Path] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.conflicts)

    @property
    def ok(self) -> bool:
        return not self.failed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    """Flush a file when the platform exposes fsync, without requiring it."""

    try:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    except Exception:
        # Some filesystems/platforms do not permit fsync.  Atomic rename still
        # provides the publication boundary in those environments.
        return


def _fsync_directory(path: Path) -> None:
    try:
        handle = os.open(path, os.O_RDONLY)
    except Exception:
        return
    try:
        os.fsync(handle)
    except Exception:
        pass
    finally:
        try:
            os.close(handle)
        except Exception:
            pass


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _fsync_file(path)


def _read_summary(path: Path) -> EvaluationSummary:
    try:
        return EvaluationSummary.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid summary.json: {exc}") from exc


def _read_results(path: Path) -> list[SampleResult]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"unable to read results.jsonl: {exc}") from exc
    if not any(line.strip() for line in lines):
        raise ValueError("results.jsonl must be nonempty")

    results: list[SampleResult] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            result = SampleResult.model_validate_json(line)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid results.jsonl record at line {line_number}: {exc}"
            ) from exc
        if not result.sample_id.strip():
            raise ValueError(
                f"invalid results.jsonl record at line {line_number}: sample_id must not be blank"
            )
        results.append(result)
    if not results:
        raise ValueError("results.jsonl must be nonempty")
    return results


def _is_safe_artifact(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _candidate_base(source_root: Path, target_root: Path, source_path: Path) -> ImportCandidate:
    return ImportCandidate(
        source_path=source_path,
        target_path=target_root / source_path.name,
        status="invalid",
    )


def _target_matches(candidate: ImportCandidate) -> bool:
    target = candidate.target_path
    if not target.is_dir() or target.is_symlink():
        return False
    expected_names = set(candidate.files)
    # An optional report present on only one side is different canonical
    # content.  Generated import sidecars and arbitrary legacy files are not
    # part of the canonical report and are intentionally ignored.
    for name in REPORT_ARTIFACTS:
        present = (target / name).exists()
        if present != (name in expected_names):
            return False
    for name, expected_hash in candidate.source_hashes.items():
        path = target / name
        if not _is_safe_artifact(path):
            return False
        try:
            if _sha256_file(path) != expected_hash:
                return False
        except Exception:
            return False
    return True


def _classify_target(candidate: ImportCandidate) -> None:
    if not candidate.target_path.exists():
        candidate.status = "copy"
        return
    if _target_matches(candidate):
        candidate.status = "skip_equal"
        return
    candidate.status = "conflict"
    candidate.reason = f"target already exists with different content: {candidate.target_path}"


def discover_imports(source_root: str | Path, target_root: str | Path) -> ImportPlan:
    """Discover eligible direct-child reports without writing either root."""

    source_root = Path(source_root)
    target_root = Path(target_root)
    if not source_root.exists() or not source_root.is_dir():
        raise ValueError(f"source root is not a directory: {source_root}")
    if target_root.exists() and not target_root.is_dir():
        raise ValueError(f"target root is not a directory: {target_root}")

    target_resolved = target_root.resolve()
    candidates: list[ImportCandidate] = []
    try:
        entries = sorted(source_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise ValueError(f"unable to list source root: {exc}") from exc

    for source_path in entries:
        if not source_path.is_dir():
            continue
        # A target nested in the source root is a destination, not a historical
        # candidate.  Skipping it avoids classifying importer output as input.
        try:
            if source_path.resolve() == target_resolved:
                continue
        except OSError:
            pass
        candidate = _candidate_base(source_root, target_root, source_path)
        if source_path.is_symlink():
            candidate.reason = "source run directory must not be a symlink"
            candidates.append(candidate)
            continue

        summary_path = source_path / "summary.json"
        results_path = source_path / "results.jsonl"
        if not _is_safe_artifact(summary_path):
            candidate.reason = "summary.json is missing or is not a regular file"
            candidates.append(candidate)
            continue
        if not _is_safe_artifact(results_path):
            candidate.reason = "results.jsonl is missing or is not a regular file"
            candidates.append(candidate)
            continue

        try:
            summary = _read_summary(summary_path)
            results = _read_results(results_path)
        except ValueError as exc:
            candidate.reason = str(exc)
            candidates.append(candidate)
            continue

        files: list[str] = list(MANDATORY_ARTIFACTS)
        optional_error = ""
        for name in OPTIONAL_ARTIFACTS:
            path = source_path / name
            if path.exists():
                if not _is_safe_artifact(path):
                    optional_error = f"{name} is present but is not a regular file"
                    break
                files.append(name)
        if optional_error:
            candidate.reason = optional_error
            candidates.append(candidate)
            continue

        hashes: dict[str, str] = {}
        try:
            for name in files:
                hashes[name] = _sha256_file(source_path / name)
        except Exception as exc:
            candidate.reason = f"unable to hash report artifact: {exc}"
            candidates.append(candidate)
            continue

        sample_ids = sorted({result.sample_id.strip() for result in results})
        warnings: list[str] = []
        if source_path.name != summary.run_id:
            warnings.append(
                f"run directory name {source_path.name!r} differs from summary.run_id {summary.run_id!r}"
            )
        candidate.files = tuple(files)
        candidate.source_hashes = hashes
        candidate.summary = summary
        candidate.results = tuple(results)
        candidate.sample_ids = sample_ids
        candidate.cohort_fingerprint = cohort_fingerprint(sample_ids)
        candidate.validation_warnings = warnings
        _classify_target(candidate)
        candidates.append(candidate)

    return ImportPlan(source_root=source_root, target_root=target_root, candidates=candidates)


def _source_hashes_match(candidate: ImportCandidate) -> None:
    if candidate.summary is None or not candidate.files:
        raise ImportApplyError(f"candidate is not eligible for import: {candidate.source_path}")
    expected_names = set(candidate.files)
    for name in OPTIONAL_ARTIFACTS:
        present = (candidate.source_path / name).exists()
        if present != (name in expected_names):
            raise ImportApplyError(
                f"source artifact set changed after discovery: {candidate.source_path} ({name})"
            )
    for name, expected_hash in candidate.source_hashes.items():
        path = candidate.source_path / name
        if not _is_safe_artifact(path):
            raise ImportApplyError(f"source artifact changed or disappeared: {path}")
        try:
            actual_hash = _sha256_file(path)
        except Exception as exc:
            raise ImportApplyError(f"unable to hash source artifact {path}: {exc}") from exc
        if actual_hash != expected_hash:
            raise ImportApplyError(f"source artifact changed after discovery: {path}")


def _temp_directory(target_root: Path, run_name: str) -> Path:
    target_root.mkdir(parents=True, exist_ok=True)
    for _ in range(16):
        temporary = target_root / f".import-{run_name}-{secrets.token_hex(8)}"
        try:
            temporary.mkdir()
            return temporary
        except FileExistsError:
            continue
    raise ImportApplyError(f"unable to allocate temporary import directory under {target_root}")


def _manifest(candidate: ImportCandidate, plan: ImportPlan) -> dict[str, Any]:
    assert candidate.summary is not None
    return {
        "schema_version": IMPORT_MANIFEST_SCHEMA_VERSION,
        "source_root": str(plan.source_root.resolve()),
        "source_path": str(candidate.source_path.resolve()),
        "source_directory_name": candidate.source_path.name,
        "summary_run_id": candidate.summary.run_id,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "file_sha256": dict(candidate.source_hashes),
        "sample_ids": list(candidate.sample_ids),
        "cohort_fingerprint": candidate.cohort_fingerprint,
        "validation_warnings": list(candidate.validation_warnings),
    }


def _publish_candidate(candidate: ImportCandidate, plan: ImportPlan) -> Path:
    _source_hashes_match(candidate)
    target = candidate.target_path
    if target.exists():
        if _target_matches(candidate):
            candidate.status = "skip_equal"
            return target
        candidate.status = "conflict"
        raise ImportApplyError(f"target appeared or changed during import: {target}")

    temporary: Path | None = None
    try:
        temporary = _temp_directory(plan.target_root, candidate.source_path.name)
        for name in candidate.files:
            source = candidate.source_path / name
            destination = temporary / name
            shutil.copy2(source, destination)
            _fsync_file(destination)
            try:
                copied_hash = _sha256_file(destination)
            except Exception as exc:
                raise ImportApplyError(f"unable to hash copied artifact {destination}: {exc}") from exc
            if copied_hash != candidate.source_hashes[name]:
                raise ImportApplyError(f"copied artifact hash mismatch: {destination}")

        _write_json(temporary / "import_manifest.json", _manifest(candidate, plan))
        _write_json(
            temporary / "report_complete.json",
            {"run_id": candidate.summary.run_id if candidate.summary else ""},
        )
        _fsync_directory(temporary)
        # The final target was checked above.  os.replace is an atomic rename
        # on the supported filesystems; a failure leaves the source untouched
        # and the temporary directory is removed in the finally block.
        os.replace(temporary, target)
        temporary = None
        _fsync_directory(plan.target_root)
        candidate.status = "copy"
        return target
    except ImportApplyError:
        raise
    except Exception as exc:
        raise ImportApplyError(f"unable to publish {candidate.source_path.name}: {exc}") from exc
    finally:
        if temporary is not None and temporary.exists():
            try:
                shutil.rmtree(temporary, ignore_errors=True)
            except Exception:
                # Cleanup is best-effort; the original apply error is more
                # useful than masking it with a cleanup implementation error.
                pass


def apply_import_plan(plan: ImportPlan) -> ImportResult:
    """Apply eligible candidates and publish each one atomically.

    Discovery's invalid/conflict decisions are preserved.  Eligible candidates
    are re-hashed before any copy so a changed source cannot be imported under
    stale metadata.  A failure for one candidate raises after cleaning that
    candidate's temporary directory; already published independent candidates
    are intentionally not overwritten or rolled back.
    """

    if not isinstance(plan, ImportPlan):
        raise TypeError("plan must be an ImportPlan")
    result = ImportResult()
    result.skipped.extend(candidate.target_path for candidate in plan.candidates if candidate.status == "skip_equal")
    result.conflicts.extend(candidate.target_path for candidate in plan.candidates if candidate.status == "conflict")
    result.invalid.extend(candidate.target_path for candidate in plan.candidates if candidate.status == "invalid")

    for candidate in plan.copy_candidates:
        published = _publish_candidate(candidate, plan)
        if candidate.status == "skip_equal":
            result.skipped.append(published)
        else:
            result.published.append(published)
    return result


__all__ = [
    "ImportApplyError",
    "ImportCandidate",
    "ImportPlan",
    "ImportResult",
    "apply_import_plan",
    "discover_imports",
]
