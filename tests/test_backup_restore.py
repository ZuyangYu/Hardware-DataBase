"""Exercise restore control flow in a disposable tree, never a real Docker DB."""

import hashlib
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest


@pytest.fixture
def restore_fixture(tmp_path):
    root = tmp_path
    (root / "scripts").mkdir()
    shutil.copy2(Path(__file__).resolve().parents[1] / "scripts/backup.sh", root / "scripts/backup.sh")
    (root / "deploy/docker/storage").mkdir(parents=True)
    (root / "deploy/docker/docker-compose.yml").write_text("services: {}\n")
    (root / "deploy/docker/.env").write_text("")
    (root / "deploy/docker/storage/data.txt").write_text("current-data")
    (root / "fixture/storage").mkdir(parents=True)
    (root / "fixture/storage/data.txt").write_text("backup-data")
    (root / "backups").mkdir()
    archive = root / "backups/storage-20261008-010000.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(root / "fixture/storage", arcname="storage")
    dump = root / "backups/pg-hdb-20261008-010000.dump"
    dump.write_bytes(b"fixture-dump")
    for path in (archive, dump):
        Path(str(path) + ".sha256").write_text(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        )
    (root / "bin").mkdir()
    docker = root / "bin/docker"
    docker.write_text('''#!/bin/bash
printf '%s\n' "$*" >> "$AUDIT_COMMANDS"
case "$*" in
  info) exit 0 ;;
  cp\ *) exit "${COPY_EXIT_CODE:-0}" ;;
  *"ps -q postgres"*) echo fake-postgres ;;
  *"ps --services --status running"*) printf 'api\nworker-chat\n' ;;
  *"pg_restore"*) exit "${RESTORE_EXIT_CODE:-0}" ;;
  *) exit 0 ;;
esac
''')
    docker.chmod(0o700)
    return root, archive


def run_restore(root, archive, **overrides):
    env = dict(os.environ, PATH=str(root / "bin") + ":" + os.environ["PATH"],
               AUDIT_COMMANDS=str(root / "commands.log"), **overrides)
    return subprocess.run(
        ["bash", str(root / "scripts/backup.sh"), "restore", str(archive), "--yes", "--deployment", "docker"],
        env=env, text=True, capture_output=True, timeout=30,
    )


@pytest.mark.parametrize("failure", [{"RESTORE_EXIT_CODE": "1"}, {"COPY_EXIT_CODE": "1"}])
def test_failed_pg_restore_rolls_back_files_and_keeps_writers_stopped(restore_fixture, failure):
    root, archive = restore_fixture
    result = run_restore(root, archive, **failure)
    assert result.returncode != 0
    assert (root / "deploy/docker/storage/data.txt").read_text() == "current-data"
    assert "start api worker-chat" not in (root / "commands.log").read_text()
    assert "应用保持停止" in result.stderr


def test_successful_restore_resumes_original_writers(restore_fixture):
    root, archive = restore_fixture
    result = run_restore(root, archive)
    assert result.returncode == 0, result.stderr
    assert (root / "deploy/docker/storage/data.txt").read_text() == "backup-data"
    assert "start api worker-chat" in (root / "commands.log").read_text()
    previous = list((root / "deploy/docker").glob("storage.pre-restore-*"))
    assert (previous[0] / "data.txt").read_text() == "current-data"


def test_corrupt_archive_is_rejected_before_stopping_services(restore_fixture):
    root, archive = restore_fixture
    archive.write_bytes(b"corrupted-after-backup")
    result = run_restore(root, archive)
    assert result.returncode != 0
    assert (root / "deploy/docker/storage/data.txt").read_text() == "current-data"
    assert "stop api" not in (root / "commands.log").read_text()


def test_unpack_failure_rolls_back_files_without_resuming(restore_fixture):
    root, archive = restore_fixture
    archive.write_bytes(b"valid-checksum-but-not-a-tar")
    Path(str(archive) + ".sha256").write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
    )
    result = run_restore(root, archive)
    assert result.returncode != 0
    assert (root / "deploy/docker/storage/data.txt").read_text() == "current-data"
    commands = (root / "commands.log").read_text()
    assert "stop api worker-chat" in commands
    assert "start api worker-chat" not in commands
