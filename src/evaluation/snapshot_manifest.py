"""Safe sidecar metadata for evaluation snapshots."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


MANIFEST_NAME = "snapshot.manifest.json"


def snapshot_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_snapshot_manifest(
    run_dir: str | Path,
    *,
    snapshot_path: str | Path,
    kb_id: int,
    kb_name: str,
    department_id: int | None,
    cohort_fingerprint: str,
    ownership_verified: bool = True,
) -> dict[str, Any]:
    manifest = {
        "schema_version": 1,
        "kb_id": int(kb_id),
        "kb_name": str(kb_name),
        "department_id": department_id,
        "cohort_fingerprint": str(cohort_fingerprint),
        "snapshot_sha256": snapshot_sha256(snapshot_path),
        "snapshot_path": str(snapshot_path),
        # An offline run can create a run-local manifest for an old external
        # snapshot whose original sidecar was missing.  Keep that distinction
        # explicit so the snapshot can be used for a controlled run but never
        # silently promoted to a strict historical baseline.
        "ownership_verified": bool(ownership_verified),
    }
    target = Path(run_dir) / MANIFEST_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def load_snapshot_manifest(run_dir: str | Path) -> dict[str, Any] | None:
    path = Path(run_dir) / MANIFEST_NAME
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("snapshot manifest must contain a JSON object")
    return value


def validate_snapshot_manifest(
    manifest: dict[str, Any],
    *,
    kb_id: int,
    kb_name: str,
    department_id: int | None,
    cohort_fingerprint: str,
    snapshot_path: str | Path | None = None,
) -> list[str]:
    errors: list[str] = []
    ownership_verified = manifest.get("ownership_verified", True)
    if not isinstance(ownership_verified, bool):
        errors.append("snapshot manifest ownership_verified 字段无效")
    if manifest.get("kb_id") != int(kb_id):
        errors.append("snapshot manifest kb_id 与所选知识库不匹配")
    if str(manifest.get("kb_name") or "").strip() != str(kb_name).strip():
        errors.append("snapshot manifest kb_name 与所选知识库不匹配")
    if manifest.get("department_id") != department_id:
        errors.append("snapshot manifest department_id 与所选知识库不匹配")
    if str(manifest.get("cohort_fingerprint") or "") != str(cohort_fingerprint):
        errors.append("snapshot manifest cohort_fingerprint 与本次样本范围不匹配")
    if snapshot_path is not None:
        try:
            actual = snapshot_sha256(snapshot_path)
        except OSError as exc:
            errors.append(f"无法读取离线快照：{exc}")
        else:
            if actual != str(manifest.get("snapshot_sha256") or ""):
                errors.append("snapshot manifest 哈希与快照内容不匹配")
    return errors
