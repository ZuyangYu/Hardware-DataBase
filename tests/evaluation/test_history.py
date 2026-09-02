import json
import tempfile
import unittest
from pathlib import Path

from src.evaluation.history import (
    cohort_fingerprint,
    compatible_baselines,
    compatibility_report,
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

    def test_compatibility_report_requires_kb_cohort_metrics_and_model_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fingerprint = cohort_fingerprint(["sample-a"])

            def write(name: str, **updates):
                run_dir = self._write_run(root, name, ["sample-a"])
                summary = {
                    "run_id": name,
                    "sample_count": 1,
                    "successful_samples": 1,
                    "kb_id": 7,
                    "kb_name": "shared",
                    "department_id": 47,
                    "cohort_fingerprint": fingerprint,
                    "metric_scores": {"faithfulness": 0.8},
                    "llm_model": "judge-a",
                    "embedding_model": "embed-a",
                    "evaluation_config": {"llm_model": "judge-a", "embedding_model": "embed-a"},
                    "snapshot_sha256": f"snapshot-{name}",
                    "snapshot_ownership_verified": True,
                }
                summary.update(updates)
                (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
                return load_history_run(run_dir)

            selected = write("selected")
            matching = write("matching")
            different_kb = write("different-kb", kb_id=8)
            different_model = write("different-model", llm_model="judge-b")

            result = compatibility_report(selected, matching)
            self.assertTrue(result["compatible"])
            self.assertTrue(result["compatibility"]["snapshot_ownership"]["match"])

            kb_result = compatibility_report(selected, different_kb)
            self.assertFalse(kb_result["compatible"])
            self.assertFalse(kb_result["compatibility"]["kb_id"]["match"])
            self.assertTrue(kb_result["warnings"])

            model_result = compatibility_report(selected, different_model)
            self.assertFalse(model_result["compatible"])
            self.assertFalse(model_result["compatibility"]["model_config"]["match"])

    def test_unverified_snapshot_cannot_be_a_strict_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fingerprint = cohort_fingerprint(["sample-a"])

            def write(name: str, verified: bool):
                run_dir = self._write_run(root, name, ["sample-a"])
                (run_dir / "summary.json").write_text(
                    json.dumps(
                        {
                            "run_id": name,
                            "sample_count": 1,
                            "successful_samples": 1,
                            "kb_id": 7,
                            "kb_name": "shared",
                            "department_id": 47,
                            "cohort_fingerprint": fingerprint,
                            "metric_scores": {"faithfulness": 0.8},
                            "llm_model": "judge-a",
                            "embedding_model": "embed-a",
                            "evaluation_config": {
                                "llm_model": "judge-a",
                                "embedding_model": "embed-a",
                            },
                            "snapshot_ownership_verified": verified,
                        }
                    ),
                    encoding="utf-8",
                )
                return load_history_run(run_dir)

            verified = write("verified", True)
            legacy = write("legacy", False)

            result = compatibility_report(verified, legacy)

            self.assertFalse(result["compatible"])
            self.assertFalse(result["compatibility"]["snapshot_ownership"]["match"])
            self.assertTrue(any("快照归属" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
