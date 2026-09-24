from __future__ import annotations

import os
from pathlib import Path
from threading import Lock

from .schemas import AnswerSnapshot


class SnapshotStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = Lock()

    def load_all(self) -> list[AnswerSnapshot]:
        if not self.path.exists():
            return []
        snapshots: list[AnswerSnapshot] = []
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                snapshots.append(AnswerSnapshot.model_validate_json(line))
            except Exception as exc:
                raise ValueError(f"invalid snapshot at line {line_number}: {exc}") from exc
        return snapshots

    def completed_ids(self) -> set[str]:
        latest: dict[str, AnswerSnapshot] = {}
        for snapshot in self.load_all():
            latest[snapshot.sample_id] = snapshot
        return {sample_id for sample_id, snapshot in latest.items() if snapshot.status == "success"}

    def append(self, snapshot: AnswerSnapshot) -> None:
        with self._lock:
            snapshots = self.load_all()
            snapshots.append(snapshot)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            content = "\n".join(item.model_dump_json() for item in snapshots) + "\n"
            try:
                temp_path.write_text(content, encoding="utf-8")
                os.replace(temp_path, self.path)
            finally:
                if temp_path.exists():
                    temp_path.unlink()
