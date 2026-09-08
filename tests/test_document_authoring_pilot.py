from __future__ import annotations

import json

from src.document_authoring.pilot import main, run_offline_pilot


def test_offline_pilot_emits_complete_signed_input_ready_evidence_bundle(tmp_path):
    output = tmp_path / "pilot-evidence.json"
    bundle = run_offline_pilot(output_path=output)

    assert bundle["status"] == "passed"
    assert bundle["production_enabled"] is False
    assert bundle["operator_signoff_required"] is True
    check_names = {check["name"] for check in bundle["checks"]}
    assert {
        "planning_contracts",
        "failure_boundaries",
        "parser_visual_baselines",
        "lineage_reconstruction",
        "backup_restore_rollback",
    } <= check_names
    assert all(check["status"] == "passed" for check in bundle["checks"])
    assert bundle["evidence_hash"].startswith("sha256:")
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["evidence_hash"] == bundle["evidence_hash"]


def test_pilot_cli_writes_json_without_enabling_production(tmp_path):
    output = tmp_path / "pilot-cli.json"
    assert main(["--output", str(output)]) == 0
    bundle = json.loads(output.read_text(encoding="utf-8"))
    assert bundle["production_enabled"] is False
