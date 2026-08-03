"""Safe, one-time importing of immutable evaluation report history.

Discovery is read-only.  Apply reopens every source object through anchored
directory handles, copies only the four named report artifacts, and publishes
with Linux ``renameat2(RENAME_NOREPLACE)``.  There is deliberately no unsafe
check-then-replace fallback when the no-clobber primitive is unavailable.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
from typing import Any, Literal

from .history import cohort_fingerprint
from .schemas import EvaluationSummary, SampleResult


MANDATORY_ARTIFACTS = ("summary.json", "results.jsonl")
OPTIONAL_ARTIFACTS = ("summary.csv", "report.html")
REPORT_ARTIFACTS = MANDATORY_ARTIFACTS + OPTIONAL_ARTIFACTS
ImportStatus = Literal["copy", "skip_equal", "conflict", "invalid"]
IMPORT_MANIFEST_SCHEMA_VERSION = 1
RENAME_NOREPLACE = 1
_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = {
    errno.EINVAL,
    errno.ENOSYS,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}
FileIdentity = tuple[int, int]


class ImportApplyError(RuntimeError):
    """Raised when an eligible report cannot be published safely."""


class ImportPublishedUncertainError(ImportApplyError):
    """Raised after publication when durability or pathname state is uncertain."""

    def __init__(
        self,
        published_path: Path,
        *,
        durability_uncertain: bool = False,
        path_identity_uncertain: bool = False,
        cleanup_uncertain: bool = False,
        details: list[str] | None = None,
    ) -> None:
        states: list[str] = []
        if durability_uncertain:
            states.append("published but durability is uncertain")
        if path_identity_uncertain:
            states.append("published but target path identity is uncertain")
        if cleanup_uncertain:
            states.append("published but cleanup is uncertain")
        message = "; ".join(states) or "published with uncertain state"
        if details:
            message = f"{message}: {'; '.join(details)}"
        super().__init__(f"{published_path}: {message}")
        self.published_path = published_path
        self.durability_uncertain = durability_uncertain
        self.path_identity_uncertain = path_identity_uncertain
        self.cleanup_uncertain = cleanup_uncertain


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
    source_identity: FileIdentity | None = None

    @property
    def classification(self) -> ImportStatus:
        return self.status

    @property
    def file_sha256(self) -> dict[str, str]:
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
    source_root_identity: FileIdentity | None = None
    target_root_identity: FileIdentity | None = None

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
    """Outcome of applying a plan."""

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


@dataclass
class _PrivateStaging:
    name: str
    payload_path: Path
    parent_fd: int
    parent_identity: FileIdentity
    payload_fd: int | None


def _identity(fd: int) -> FileIdentity:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _close_fds(fds: list[int] | tuple[int, ...]) -> None:
    first_error: OSError | None = None
    for fd in fds:
        try:
            os.close(fd)
        except OSError as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def _absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _directory_open_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError(errno.ENOTSUP, "secure no-follow directory opens are unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_directory_path(
    path: Path,
    *,
    create: bool = False,
    final_must_not_exist: bool = False,
) -> int:
    """Open an absolute directory path one no-follow component at a time."""

    absolute = _absolute_path(path)
    flags = _directory_open_flags()
    current_fd = os.open(os.sep, flags)
    try:
        parts = absolute.parts[1:]
        for index, part in enumerate(parts):
            is_final = index == len(parts) - 1
            try:
                child_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                created = False
                try:
                    os.mkdir(part, mode=0o755, dir_fd=current_fd)
                    created = True
                except FileExistsError:
                    if is_final and final_must_not_exist:
                        raise
                if created:
                    _fsync_directory_fd(current_fd)
                child_fd = os.open(part, flags, dir_fd=current_fd)
            else:
                if is_final and final_must_not_exist:
                    os.close(child_fd)
                    raise FileExistsError(errno.EEXIST, "directory appeared after discovery", part)
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_directory_at(parent_fd: int, name: str) -> int:
    fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
    except BaseException:
        os.close(fd)
        raise
    if not stat.S_ISDIR(info.st_mode):
        os.close(fd)
        raise NotADirectoryError(errno.ENOTDIR, "not a directory", name)
    return fd


def _regular_open_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError(errno.ENOTSUP, "secure no-follow file opens are unavailable")
    return os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_regular_at(parent_fd: int, name: str, *, optional: bool = False) -> int | None:
    try:
        fd = os.open(name, _regular_open_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if optional:
            return None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, f"{name} is not a regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return b"".join(chunks)


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _fsync_file(fd: int) -> None:
    os.fsync(fd)


def _fsync_directory_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            return
        raise


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def _write_json_at(parent_fd: int, name: str, payload: dict[str, Any]) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
    try:
        data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        _write_all(fd, data)
        _fsync_file(fd)
    finally:
        os.close(fd)


def _read_summary_bytes(payload: bytes) -> EvaluationSummary:
    try:
        text = payload.decode("utf-8-sig")
        return EvaluationSummary.model_validate_json(text)
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid summary.json: {exc}") from exc


def _read_results_bytes(payload: bytes) -> list[SampleResult]:
    try:
        lines = payload.decode("utf-8-sig").splitlines()
    except UnicodeError as exc:
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


def _candidate_base(target_root: Path, source_path: Path) -> ImportCandidate:
    return ImportCandidate(
        source_path=source_path,
        target_path=target_root / source_path.name,
        status="invalid",
    )


def _target_matches_at(candidate: ImportCandidate, target_root_fd: int) -> bool:
    try:
        target_fd = _open_directory_at(target_root_fd, candidate.target_path.name)
    except OSError:
        return False
    try:
        expected_names = set(candidate.files)
        for name in REPORT_ARTIFACTS:
            try:
                info = os.stat(name, dir_fd=target_fd, follow_symlinks=False)
            except FileNotFoundError:
                present = False
            except OSError:
                return False
            else:
                present = stat.S_ISREG(info.st_mode)
                if not present:
                    return False
            if present != (name in expected_names):
                return False

        for name, expected_hash in candidate.source_hashes.items():
            try:
                artifact_fd = _open_regular_at(target_fd, name)
                assert artifact_fd is not None
            except OSError:
                return False
            try:
                if _sha256_fd(artifact_fd) != expected_hash:
                    return False
            except OSError:
                return False
            finally:
                os.close(artifact_fd)
        return True
    finally:
        os.close(target_fd)


def _target_entry_exists(target_root_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=target_root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _classify_target(candidate: ImportCandidate, target_root_fd: int | None) -> None:
    if target_root_fd is None or not _target_entry_exists(target_root_fd, candidate.target_path.name):
        candidate.status = "copy"
        return
    if _target_matches_at(candidate, target_root_fd):
        candidate.status = "skip_equal"
        return
    candidate.status = "conflict"
    candidate.reason = f"target already exists with different content: {candidate.target_path}"


def discover_imports(source_root: str | Path, target_root: str | Path) -> ImportPlan:
    """Discover eligible direct-child reports without writing either root."""

    source_root_path = _absolute_path(source_root)
    target_root_path = _absolute_path(target_root)
    if target_root_path == source_root_path or source_root_path in target_root_path.parents:
        raise ValueError(
            f"source and target roots overlap; target must be outside source: "
            f"{source_root_path} -> {target_root_path}"
        )
    try:
        source_root_fd = _open_directory_path(source_root_path)
    except OSError as exc:
        raise ValueError(f"source root is not a safe directory: {source_root_path}: {exc}") from exc

    target_root_fd: int | None = None
    try:
        try:
            target_root_fd = _open_directory_path(target_root_path)
        except FileNotFoundError:
            target_root_identity = None
        except OSError as exc:
            raise ValueError(f"target root is not a safe directory: {target_root_path}: {exc}") from exc
        else:
            target_root_identity = _identity(target_root_fd)
            if target_root_identity == _identity(source_root_fd):
                raise ValueError(
                    f"source and target roots overlap by identity: "
                    f"{source_root_path} -> {target_root_path}"
                )

        candidates: list[ImportCandidate] = []
        try:
            names = sorted(os.listdir(source_root_fd))
        except OSError as exc:
            raise ValueError(f"unable to list source root: {exc}") from exc

        for name in names:
            source_path = source_root_path / name
            candidate = _candidate_base(target_root_path, source_path)
            try:
                entry_info = os.stat(name, dir_fd=source_root_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(entry_info.st_mode):
                candidate.reason = "source run directory must not be a symlink"
                candidates.append(candidate)
                continue
            if not stat.S_ISDIR(entry_info.st_mode):
                continue
            try:
                run_fd = _open_directory_at(source_root_fd, name)
            except OSError as exc:
                candidate.reason = f"unable to safely open source run directory: {exc}"
                candidates.append(candidate)
                continue
            try:
                candidate.source_identity = _identity(run_fd)
                if target_root_identity == candidate.source_identity:
                    continue

                artifact_fds: dict[str, int] = {}
                try:
                    for artifact in MANDATORY_ARTIFACTS:
                        try:
                            fd = _open_regular_at(run_fd, artifact)
                            assert fd is not None
                        except OSError as exc:
                            candidate.reason = f"{artifact} is missing or is not a safe regular file: {exc}"
                            break
                        artifact_fds[artifact] = fd
                    if candidate.reason:
                        candidates.append(candidate)
                        continue

                    files: list[str] = list(MANDATORY_ARTIFACTS)
                    for artifact in OPTIONAL_ARTIFACTS:
                        try:
                            fd = _open_regular_at(run_fd, artifact, optional=True)
                        except OSError as exc:
                            candidate.reason = f"{artifact} is present but is not a safe regular file: {exc}"
                            break
                        if fd is not None:
                            artifact_fds[artifact] = fd
                            files.append(artifact)
                    if candidate.reason:
                        candidates.append(candidate)
                        continue

                    try:
                        artifact_payloads = {
                            artifact: _read_fd(artifact_fds[artifact]) for artifact in files
                        }
                    except OSError as exc:
                        candidate.reason = f"unable to read report artifact: {exc}"
                        candidates.append(candidate)
                        continue

                    try:
                        summary = _read_summary_bytes(artifact_payloads["summary.json"])
                        results = _read_results_bytes(artifact_payloads["results.jsonl"])
                    except ValueError as exc:
                        candidate.reason = str(exc)
                        candidates.append(candidate)
                        continue

                    hashes = {
                        artifact: hashlib.sha256(artifact_payloads[artifact]).hexdigest()
                        for artifact in files
                    }

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
                    _classify_target(candidate, target_root_fd)
                    candidates.append(candidate)
                finally:
                    _close_fds(list(artifact_fds.values()))
            finally:
                os.close(run_fd)

        return ImportPlan(
            source_root=source_root_path,
            target_root=target_root_path,
            candidates=candidates,
            source_root_identity=_identity(source_root_fd),
            target_root_identity=target_root_identity,
        )
    finally:
        _close_fds(
            [fd for fd in (target_root_fd, source_root_fd) if fd is not None]
        )


def _open_apply_source_root(plan: ImportPlan) -> int:
    try:
        fd = _open_directory_path(plan.source_root)
    except OSError as exc:
        raise ImportApplyError(f"source root is no longer a safe directory: {exc}") from exc
    try:
        current_identity = _identity(fd)
    except BaseException:
        os.close(fd)
        raise
    if plan.source_root_identity is None or current_identity != plan.source_root_identity:
        os.close(fd)
        raise ImportApplyError(f"source root identity changed after discovery: {plan.source_root}")
    return fd


def _open_apply_target_root(plan: ImportPlan) -> int:
    try:
        fd = _open_directory_path(
            plan.target_root,
            create=plan.target_root_identity is None,
            final_must_not_exist=plan.target_root_identity is None,
        )
    except OSError as exc:
        raise ImportApplyError(f"target root changed or is not a safe directory: {exc}") from exc
    try:
        current_identity = _identity(fd)
    except BaseException:
        os.close(fd)
        raise
    if plan.target_root_identity is not None and current_identity != plan.target_root_identity:
        os.close(fd)
        raise ImportApplyError(f"target root identity changed after discovery: {plan.target_root}")
    return fd


def _validate_target_root_path(plan: ImportPlan, target_root_fd: int) -> None:
    try:
        current_fd = _open_directory_path(plan.target_root)
    except OSError as exc:
        raise ImportApplyError(f"target root path changed during apply: {exc}") from exc
    try:
        if _identity(current_fd) != _identity(target_root_fd):
            raise ImportApplyError(f"target root identity changed during apply: {plan.target_root}")
    finally:
        os.close(current_fd)


def _open_validated_source_files(
    source_root_fd: int, candidate: ImportCandidate
) -> tuple[int, dict[str, int]]:
    if candidate.summary is None or not candidate.files or candidate.source_identity is None:
        raise ImportApplyError(f"candidate is not eligible for import: {candidate.source_path}")
    try:
        run_fd = _open_directory_at(source_root_fd, candidate.source_path.name)
    except OSError as exc:
        raise ImportApplyError(
            f"source run directory changed or is not safe: {candidate.source_path}: {exc}"
        ) from exc
    try:
        run_identity = _identity(run_fd)
    except BaseException:
        os.close(run_fd)
        raise
    if run_identity != candidate.source_identity:
        os.close(run_fd)
        raise ImportApplyError(
            f"source run directory identity changed after discovery: {candidate.source_path}"
        )

    artifact_fds: dict[str, int] = {}
    try:
        expected_names = set(candidate.files)
        for name in OPTIONAL_ARTIFACTS:
            try:
                fd = _open_regular_at(run_fd, name, optional=True)
            except OSError as exc:
                raise ImportApplyError(f"source artifact changed or is not safe: {candidate.source_path / name}: {exc}") from exc
            present = fd is not None
            if present != (name in expected_names):
                if fd is not None:
                    os.close(fd)
                raise ImportApplyError(
                    f"source artifact set changed after discovery: {candidate.source_path} ({name})"
                )
            if fd is not None:
                artifact_fds[name] = fd

        for name in MANDATORY_ARTIFACTS:
            try:
                fd = _open_regular_at(run_fd, name)
                assert fd is not None
            except OSError as exc:
                raise ImportApplyError(f"source artifact changed or is not safe: {candidate.source_path / name}: {exc}") from exc
            artifact_fds[name] = fd

        for name, expected_hash in candidate.source_hashes.items():
            try:
                actual_hash = _sha256_fd(artifact_fds[name])
            except OSError as exc:
                raise ImportApplyError(f"unable to hash source artifact {candidate.source_path / name}: {exc}") from exc
            if actual_hash != expected_hash:
                raise ImportApplyError(f"source artifact changed after discovery: {candidate.source_path / name}")
        return run_fd, artifact_fds
    except BaseException:
        _close_source_files(run_fd, artifact_fds)
        raise


def _close_source_files(run_fd: int, artifact_fds: dict[str, int]) -> None:
    _close_fds([*artifact_fds.values(), run_fd])


def _remove_private_staging(
    staging: _PrivateStaging, target_root_fd: int
) -> None:
    try:
        shutil.rmtree("payload", dir_fd=staging.parent_fd)
    except FileNotFoundError:
        pass
    _fsync_directory_fd(staging.parent_fd)
    try:
        visible = os.stat(
            staging.name,
            dir_fd=target_root_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise OSError(
            errno.ESTALE,
            f"private staging parent path changed; refusing cleanup: {staging.name}: {exc}",
        ) from exc
    visible_identity = (visible.st_dev, visible.st_ino)
    if not stat.S_ISDIR(visible.st_mode) or visible_identity != staging.parent_identity:
        raise OSError(
            errno.ESTALE,
            f"private staging parent identity changed; refusing cleanup: {staging.name}",
        )
    os.rmdir(staging.name, dir_fd=target_root_fd)
    if os.fstat(staging.parent_fd).st_nlink != 0:
        raise OSError(
            errno.ESTALE,
            f"private staging parent identity changed during cleanup: {staging.name}",
        )
    _fsync_directory_fd(target_root_fd)


def _private_staging(
    target_root: Path, target_root_fd: int, run_name: str
) -> _PrivateStaging:
    for _ in range(16):
        name = f".import-{run_name}-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=target_root_fd)
        except FileExistsError:
            continue
        parent_fd: int | None = None
        parent_identity: FileIdentity | None = None
        payload_fd: int | None = None
        try:
            parent_fd = _open_directory_at(target_root_fd, name)
            parent_identity = _identity(parent_fd)
            _fsync_directory_fd(target_root_fd)
            os.mkdir("payload", mode=0o700, dir_fd=parent_fd)
            _fsync_directory_fd(parent_fd)
            payload_fd = _open_directory_at(parent_fd, "payload")
            return _PrivateStaging(
                name=name,
                payload_path=target_root / name / "payload",
                parent_fd=parent_fd,
                parent_identity=parent_identity,
                payload_fd=payload_fd,
            )
        except BaseException as exc:
            close_error: OSError | None = None
            if payload_fd is not None:
                try:
                    os.close(payload_fd)
                except OSError as caught:
                    close_error = caught
            if parent_identity is None:
                if parent_fd is not None:
                    try:
                        os.close(parent_fd)
                    except OSError as caught:
                        if close_error is None:
                            close_error = caught
                detail = f"{exc}; cleanup identity unavailable for {name}"
                if close_error is not None:
                    detail += f"; close failed: {close_error}"
                raise ImportApplyError(detail) from exc
            cleanup_staging = _PrivateStaging(
                name=name,
                payload_path=target_root / name / "payload",
                parent_fd=parent_fd,
                parent_identity=parent_identity,
                payload_fd=None,
            )
            try:
                _remove_private_staging(cleanup_staging, target_root_fd)
            except BaseException as cleanup_exc:
                try:
                    os.close(parent_fd)
                except OSError as caught:
                    if close_error is None:
                        close_error = caught
                detail = f"{exc}; cleanup failed: {cleanup_exc}"
                if close_error is not None:
                    detail += f"; close failed: {close_error}"
                raise ImportApplyError(detail) from exc
            try:
                os.close(parent_fd)
            except OSError as caught:
                if close_error is None:
                    close_error = caught
            if close_error is not None:
                raise ImportApplyError(f"{exc}; close failed: {close_error}") from exc
            raise
    raise ImportApplyError(f"unable to allocate temporary import directory under {target_root}")


def _copy_artifact(source_fd: int, destination_directory_fd: int, name: str) -> int:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    destination_fd = os.open(name, flags, 0o600, dir_fd=destination_directory_fd)
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            _write_all(destination_fd, chunk)
        os.lseek(source_fd, 0, os.SEEK_SET)
        _fsync_file(destination_fd)
        return destination_fd
    except BaseException:
        os.close(destination_fd)
        raise


def _manifest(candidate: ImportCandidate, plan: ImportPlan) -> dict[str, Any]:
    assert candidate.summary is not None
    return {
        "schema_version": IMPORT_MANIFEST_SCHEMA_VERSION,
        "source_root": str(plan.source_root),
        "source_path": str(candidate.source_path),
        "source_directory_name": candidate.source_path.name,
        "summary_run_id": candidate.summary.run_id,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "file_sha256": dict(candidate.source_hashes),
        "sample_ids": list(candidate.sample_ids),
        "cohort_fingerprint": candidate.cohort_fingerprint,
        "validation_warnings": list(candidate.validation_warnings),
    }


def _rename_noreplace(
    source_name: str,
    target_name: str,
    source_parent_fd: int,
    target_parent_fd: int,
) -> None:
    """Atomically rename between anchored parents without replacing target."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ImportApplyError("atomic no-replace publication is unavailable (renameat2 missing)")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent_fd,
        os.fsencode(source_name),
        target_parent_fd,
        os.fsencode(target_name),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {
        errno.ENOSYS,
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }:
        raise ImportApplyError(
            f"atomic no-replace publication is unavailable: {os.strerror(error_number)}"
        )
    raise OSError(error_number, os.strerror(error_number), target_name)


def _raise_publish_error(candidate: ImportCandidate, exc: BaseException) -> None:
    if isinstance(exc, FileExistsError):
        candidate.status = "conflict"
        candidate.reason = f"target appeared during atomic publication: {candidate.target_path}"
        raise ImportApplyError(candidate.reason) from exc
    if isinstance(exc, ImportApplyError):
        raise exc
    raise ImportApplyError(f"unable to publish {candidate.source_path.name}: {exc}") from exc


def _publish_candidate(
    candidate: ImportCandidate,
    plan: ImportPlan,
    target_root_fd: int,
    artifact_fds: dict[str, int],
) -> Path:
    if _target_entry_exists(target_root_fd, candidate.target_path.name):
        if _target_matches_at(candidate, target_root_fd):
            candidate.status = "skip_equal"
            return candidate.target_path
        candidate.status = "conflict"
        candidate.reason = f"target appeared or changed during import: {candidate.target_path}"
        raise ImportApplyError(candidate.reason)

    staging: _PrivateStaging | None = None
    prepublish_error: BaseException | None = None
    durability_error: BaseException | None = None
    path_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    close_error: BaseException | None = None
    published = False
    try:
        staging = _private_staging(
            plan.target_root, target_root_fd, candidate.source_path.name
        )
        assert staging.payload_fd is not None
        for name in candidate.files:
            destination_fd = _copy_artifact(
                artifact_fds[name], staging.payload_fd, name
            )
            try:
                copied_hash = _sha256_fd(destination_fd)
            finally:
                os.close(destination_fd)
            if copied_hash != candidate.source_hashes[name]:
                raise ImportApplyError(
                    f"copied artifact hash mismatch: {staging.payload_path / name}"
                )

        _write_json_at(
            staging.payload_fd,
            "import_manifest.json",
            _manifest(candidate, plan),
        )
        _write_json_at(
            staging.payload_fd,
            "report_complete.json",
            {"run_id": candidate.summary.run_id if candidate.summary else ""},
        )
        _fsync_directory_fd(staging.payload_fd)
        os.close(staging.payload_fd)
        staging.payload_fd = None
        _validate_target_root_path(plan, target_root_fd)
        _rename_noreplace(
            "payload",
            candidate.target_path.name,
            staging.parent_fd,
            target_root_fd,
        )
        published = True
        candidate.status = "copy"
    except BaseException as exc:
        prepublish_error = exc

    if published:
        try:
            _fsync_directory_fd(target_root_fd)
        except BaseException as exc:
            durability_error = exc

    if staging is not None:
        if staging.payload_fd is not None:
            try:
                os.close(staging.payload_fd)
            except OSError as close_exc:
                close_error = close_exc
            staging.payload_fd = None
        try:
            _remove_private_staging(staging, target_root_fd)
        except BaseException as exc:
            cleanup_error = exc
        try:
            os.close(staging.parent_fd)
        except OSError as exc:
            if close_error is None:
                close_error = exc

    if published:
        try:
            _validate_target_root_path(plan, target_root_fd)
        except BaseException as exc:
            path_error = exc
        details = [
            str(error)
            for error in (
                durability_error,
                path_error,
                cleanup_error,
                close_error,
            )
            if error is not None
        ]
        if details:
            raise ImportPublishedUncertainError(
                candidate.target_path,
                durability_uncertain=durability_error is not None,
                path_identity_uncertain=path_error is not None,
                cleanup_uncertain=(cleanup_error is not None or close_error is not None),
                details=details,
            )
        return candidate.target_path

    assert prepublish_error is not None
    if cleanup_error is not None or close_error is not None:
        cleanup_details = "; ".join(
            str(error)
            for error in (cleanup_error, close_error)
            if error is not None
        )
        raise ImportApplyError(
            f"{prepublish_error}; cleanup failed: {cleanup_details}"
        ) from prepublish_error
    _raise_publish_error(candidate, prepublish_error)
    raise AssertionError("unreachable")


def apply_import_plan(plan: ImportPlan) -> ImportResult:
    """Apply eligible candidates with source revalidation and no-clobber publish."""

    if not isinstance(plan, ImportPlan):
        raise TypeError("plan must be an ImportPlan")
    result = ImportResult()
    result.conflicts.extend(
        candidate.target_path for candidate in plan.candidates if candidate.status == "conflict"
    )
    result.invalid.extend(
        candidate.target_path for candidate in plan.candidates if candidate.status == "invalid"
    )
    actionable = [
        candidate for candidate in plan.candidates if candidate.status in {"copy", "skip_equal"}
    ]
    if not actionable:
        return result

    source_root_fd = _open_apply_source_root(plan)
    target_root_fd: int | None = None
    try:
        target_root_fd = _open_apply_target_root(plan)
        for candidate in actionable:
            run_fd, artifact_fds = _open_validated_source_files(source_root_fd, candidate)
            try:
                if candidate.status == "skip_equal":
                    if _target_matches_at(candidate, target_root_fd):
                        result.skipped.append(candidate.target_path)
                    else:
                        candidate.status = "conflict"
                        candidate.reason = (
                            f"target changed after discovery: {candidate.target_path}"
                        )
                        result.conflicts.append(candidate.target_path)
                    continue

                published = _publish_candidate(
                    candidate, plan, target_root_fd, artifact_fds
                )
                if candidate.status == "skip_equal":
                    result.skipped.append(published)
                else:
                    result.published.append(published)
            finally:
                _close_source_files(run_fd, artifact_fds)
        return result
    finally:
        _close_fds(
            [fd for fd in (target_root_fd, source_root_fd) if fd is not None]
        )


__all__ = [
    "ImportApplyError",
    "ImportCandidate",
    "ImportPlan",
    "ImportPublishedUncertainError",
    "ImportResult",
    "apply_import_plan",
    "discover_imports",
]
