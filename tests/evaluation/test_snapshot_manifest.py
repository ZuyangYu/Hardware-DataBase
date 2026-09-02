from src.evaluation.snapshot_manifest import (
    load_snapshot_manifest,
    snapshot_sha256,
    validate_snapshot_manifest,
    write_snapshot_manifest,
)


def test_snapshot_manifest_records_digest_and_rejects_wrong_kb_or_cohort(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    snapshot_path = tmp_path / "shared-snapshot.jsonl"
    snapshot_path.write_text('{"sample_id":"a"}\n', encoding="utf-8")

    manifest = write_snapshot_manifest(
        run_dir,
        snapshot_path=snapshot_path,
        kb_id=2,
        kb_name="ADAS",
        department_id=100,
        cohort_fingerprint="cohort-a",
    )

    assert manifest["snapshot_sha256"] == snapshot_sha256(snapshot_path)
    assert manifest["ownership_verified"] is True
    assert load_snapshot_manifest(run_dir)["kb_id"] == 2
    assert validate_snapshot_manifest(
        manifest,
        kb_id=2,
        kb_name="ADAS",
        department_id=100,
        cohort_fingerprint="cohort-a",
    ) == []
    errors = validate_snapshot_manifest(
        manifest,
        kb_id=1,
        kb_name="ADAS",
        department_id=47,
        cohort_fingerprint="cohort-b",
    )
    assert any("kb_id" in error for error in errors)
    assert any("cohort" in error for error in errors)


def test_snapshot_manifest_rejects_non_boolean_ownership_flag(tmp_path):
    snapshot_path = tmp_path / "snapshot.jsonl"
    snapshot_path.write_text('{"sample_id":"a"}\n', encoding="utf-8")
    manifest = {
        "kb_id": 2,
        "kb_name": "ADAS",
        "department_id": 100,
        "cohort_fingerprint": "cohort-a",
        "snapshot_sha256": snapshot_sha256(snapshot_path),
        "ownership_verified": "false",
    }

    errors = validate_snapshot_manifest(
        manifest,
        kb_id=2,
        kb_name="ADAS",
        department_id=100,
        cohort_fingerprint="cohort-a",
    )

    assert any("ownership_verified" in error for error in errors)
