"""Immutable file-backed storage for whole-document comparison reports.

Reports can be large but contain only hashes, alignments, changed fragments and
references.  Each report is written atomically once and verified by its
content hash on every read, so a later model answer cannot rewrite history.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import src.settings as settings
from src.services.document_comparison import ComparisonError
from src.services.document_comparison_models import (
    ComparisonReport,
    legacy_report_hash,
    report_hash,
)


class DocumentComparisonStore:
    def __init__(self, root: str | None = None):
        self.root = Path(root or settings.DOCUMENT_COMPARISON_STORAGE_DIR).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, report_id: str) -> Path:
        safe = "".join(ch for ch in str(report_id or "") if ch.isalnum() or ch in {"-", "_"})
        if not safe or safe != str(report_id):
            raise ComparisonError("invalid_report_id", "report id contains unsafe characters")
        return self.root / f"{safe}.json"

    def save(self, report: ComparisonReport, *, owner_user_id: int | None = None) -> dict:
        payload = report.to_dict()
        target = self._path(report.report_id)
        envelope = {
            "schema": "hdb-document-comparison-store-v1",
            "owner_user_id": owner_user_id,
            "report": payload,
        }
        serialized = json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2)
        if target.exists():
            existing = self.get(report.report_id, owner_user_id=owner_user_id)
            # The report id is derived from the frozen source signatures and
            # normalized content.  A rerun after a code change can produce
            # different internal item ids while still describing the same two
            # sources; that is not a content conflict.  Only a source-signature
            # mismatch is treated as a conflict.
            def _signature(payload: dict) -> tuple:
                return self._source_signature(payload)

            if _signature(existing) != _signature(report.to_dict()):
                raise ComparisonError(
                    "comparison_report_conflict",
                    "a report with this id already exists for different sources",
                )
            return existing
        fd, tmp_path = tempfile.mkstemp(prefix=".comparison-", suffix=".tmp", dir=str(self.root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return payload

    @staticmethod
    def _source_signature(payload: dict) -> tuple:
        return tuple(
            str(payload.get(side, {}).get(key) or "")
            for side in ("left", "right")
            for key in ("source_type", "source_id", "content_hash", "document_version")
        )

    def find_matching(
        self,
        report: ComparisonReport,
        *,
        owner_user_id: int | None = None,
    ) -> dict | None:
        """Find a frozen report for the same source versions.

        This is a recovery path for reports written by an older implementation
        whose content-addressed id differs from the current parser output.  It
        still requires the immutable source type/id/hash/version signature and
        the normal owner check; unrelated or tampered reports are ignored.
        """

        target = self._source_signature(report.to_dict())
        target_sides = {target[:4], target[4:]}
        for path in sorted(self.root.glob("*.json")):
            report_id = path.stem
            try:
                payload = self.get(report_id, owner_user_id=owner_user_id)
            except ComparisonError:
                continue
            signature = self._source_signature(payload)
            if {signature[:4], signature[4:]} == target_sides:
                return payload
        return None

    def _read_envelope(self, report_id: str) -> dict:
        path = self._path(report_id)
        if not path.exists():
            raise ComparisonError("comparison_report_not_found", "report not found")
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ComparisonError("comparison_report_corrupt", str(exc)[:200]) from exc
        if not isinstance(envelope, dict):
            raise ComparisonError("comparison_report_corrupt", "report envelope is not an object")
        return envelope

    def get(self, report_id: str, *, owner_user_id: int | None = None) -> dict:
        envelope = self._read_envelope(report_id)
        report = envelope.get("report")
        if not isinstance(report, dict):
            raise ComparisonError("comparison_report_corrupt", "report payload is missing")
        expected = str(report.get("immutable_hash") or "")
        actual = report_hash(report)
        if not expected or (expected != actual and expected != legacy_report_hash(report)):
            raise ComparisonError("comparison_report_tampered", "report hash mismatch")
        owner = envelope.get("owner_user_id")
        if owner_user_id is not None and owner not in {None, int(owner_user_id)}:
            raise ComparisonError("comparison_report_denied", "report belongs to another user")
        return report

    def owner_user_id(self, report_id: str) -> int | None:
        value = self._read_envelope(report_id).get("owner_user_id")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def export_path(self, report_id: str) -> Path:
        return self._path(report_id)


__all__ = ["DocumentComparisonStore"]
