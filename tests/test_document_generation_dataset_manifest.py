"""Gate P0.2: the frozen document-generation dataset must stay immutable."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

DATASET = Path("evaluation/datasets/document_generation_v1.jsonl")
MANIFEST = Path("evaluation/datasets/document_generation_v1.manifest.json")


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_dataset_hash_matches_frozen_manifest():
    manifest = _manifest()
    digest = hashlib.sha256(DATASET.read_bytes()).hexdigest()
    assert digest == manifest["dataset_sha256"]


def test_dataset_records_match_frozen_ids():
    manifest = _manifest()
    records = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [r["id"] for r in records] == manifest["record_ids"]
    assert len({r["id"] for r in records}) == len(records)


def test_manifest_pins_metric_denominator_rules():
    manifest = _manifest()
    rules = manifest["metric_denominators"]["rules"]
    assert manifest["metric_denominators"]["primary"] == "attempted_fields"
    assert rules["optional_missing"] == "counted_separately_not_failure"
    assert rules["unknown_telemetry"] == "inconclusive_not_success_not_failure"
