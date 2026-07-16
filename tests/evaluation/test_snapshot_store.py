import tempfile
import unittest
from pathlib import Path

from src.evaluation.schemas import AnswerSnapshot
from src.evaluation.snapshot_store import SnapshotStore


def _snapshot(sample_id: str, status: str = "success") -> AnswerSnapshot:
    return AnswerSnapshot(
        sample_id=sample_id,
        question="Q",
        kb_name="ADAS",
        response="A",
        status=status,
    )


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "snapshot.jsonl"

    def test_append_round_trips_snapshots(self):
        store = SnapshotStore(self.path)

        store.append(_snapshot("q1"))
        store.append(_snapshot("q2", "failed"))

        self.assertEqual([item.sample_id for item in store.load_all()], ["q1", "q2"])

    def test_completed_ids_include_only_latest_success(self):
        store = SnapshotStore(self.path)
        store.append(_snapshot("q1", "failed"))
        store.append(_snapshot("q1", "success"))
        store.append(_snapshot("q2", "failed"))

        self.assertEqual(store.completed_ids(), {"q1"})

    def test_append_leaves_no_temporary_file(self):
        store = SnapshotStore(self.path)
        store.append(_snapshot("q1"))

        self.assertFalse(self.path.with_suffix(self.path.suffix + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
