import json
import tempfile
import unittest
from pathlib import Path

from src.evaluation.history import (
    cohort_fingerprint,
    compatible_baselines,
    load_history_run,
)


class EvaluationHistoryTests(unittest.TestCase):
    def _write_run(
        self,
        root: Path,
        name: str,
        sample_ids: list[str],
        *,
        origin: str | None = None,
        validation_warnings: list[str] | None = None,
        malformed_results: bool = False,
    ) -> Path:
        run_dir = root / name
        run_dir.mkdir()
        (run_dir / "summary.json").write_text(
            json.dumps(
                {
                    "run_id": name,
                    "sample_count": len(sample_ids),
                    "successful_samples": len(sample_ids),
                }
            ),
            encoding="utf-8",
        )
        if malformed_results:
            (run_dir / "results.jsonl").write_text("{not-json}\n", encoding="utf-8")
        else:
            (run_dir / "results.jsonl").write_text(
                "".join(
                    json.dumps({"sample_id": sample_id}) + "\n"
                    for sample_id in sample_ids
                ),
                encoding="utf-8",
            )
        if origin is not None:
            (run_dir / "import_manifest.json").write_text(
                json.dumps(
                    {
                        "origin": origin,
                        "validation_warnings": validation_warnings or [],
                    }
                ),
                encoding="utf-8",
            )
        return run_dir

    def test_fingerprint_is_stable_for_order_duplicates_and_blanks(self):
        self.assertEqual(
            cohort_fingerprint([" sample-b ", "sample-a", "sample-b", "", "  "]),
            cohort_fingerprint(["sample-a", "sample-b"]),
        )

    def test_different_cohorts_have_different_fingerprints(self):
        self.assertNotEqual(cohort_fingerprint(["sample-a"]), cohort_fingerprint(["sample-b"]))

    def test_legacy_run_without_import_sidecar_is_local(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = self._write_run(Path(temp_dir), "legacy", ["sample-a", "sample-b"])

            history = load_history_run(run_dir)

            self.assertEqual(history.origin, "local")
            self.assertEqual(history.sample_ids, ["sample-a", "sample-b"])
            self.assertEqual(history.sample_count, 2)
            self.assertTrue(history.cohort_fingerprint)
            self.assertEqual(history.validation_warnings, [])

    def test_imported_run_reads_origin_and_validation_warnings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = self._write_run(
                Path(temp_dir),
                "imported",
                ["sample-a"],
                origin="imported",
                validation_warnings=["旧版报告缺少门禁字段"],
            )

            history = load_history_run(run_dir)

            self.assertEqual(history.origin, "imported")
            self.assertEqual(history.validation_warnings, ["旧版报告缺少门禁字段"])

    def test_import_manifest_provenance_fields_identify_imported_origin(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = self._write_run(Path(temp_dir), "imported", ["sample-a"])
            (run_dir / "import_manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source_root": "/source/evaluations",
                        "source_path": "/source/evaluations/imported",
                        "source_directory_name": "imported",
                        "imported_at": "2026-08-03T00:00:00Z",
                        "file_sha256": {"summary.json": "digest"},
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(load_history_run(run_dir).origin, "imported")

    def test_explicit_local_origin_wins_over_import_provenance_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = self._write_run(Path(temp_dir), "local", ["sample-a"])
            (run_dir / "import_manifest.json").write_text(
                json.dumps(
                    {
                        "origin": "local",
                        "source_root": "/source/evaluations",
                        "source_path": "/source/evaluations/local",
                        "imported_at": "2026-08-03T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(load_history_run(run_dir).origin, "local")

    def test_malformed_results_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = self._write_run(
                Path(temp_dir), "malformed", ["sample-a"], malformed_results=True
            )

            with self.assertRaises(ValueError):
                load_history_run(run_dir)

    def test_compatible_baselines_require_same_nonempty_cohort_and_different_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            selected = load_history_run(self._write_run(root, "selected", ["sample-a"]))
            matching = load_history_run(self._write_run(root, "matching", ["sample-a"]))
            different = load_history_run(self._write_run(root, "different", ["sample-b"]))
            empty = load_history_run(self._write_run(root, "empty", []))

            baselines = compatible_baselines(selected, [selected, matching, different, empty])

            self.assertEqual([run.run_name for run in baselines], ["matching"])


if __name__ == "__main__":
    unittest.main()
