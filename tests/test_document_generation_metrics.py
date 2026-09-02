"""Task 1: document-generation metric aggregation and observation adapters."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.evaluation.document_generation_metrics import (
    INCONCLUSIVE,
    FieldObservation,
    aggregate_document_generation_metrics,
    collect_observations,
)
from src.evaluation.dataset_loader import load_document_generation_dataset
from src.document_authoring.models import AuthoringExecutionEvent, HarnessRun

DATASET = Path("evaluation/datasets/document_generation_v1.jsonl")
FIXTURE_INDEX = Path("evaluation/fixtures/document_generation/fixture_index.json")
FIXTURE_DIR = FIXTURE_INDEX.parent
MANIFEST = Path("evaluation/datasets/document_generation_v1.manifest.json")


def _observation(**overrides) -> FieldObservation:
    payload = dict(record_id="r-1", field_id="f-1", required=True, attempted=True, success=True)
    payload.update(overrides)
    return FieldObservation.model_validate(payload)


# ── dataset / fixture integrity ──────────────────────────────────────────────


def test_dataset_has_at_least_20_unique_records_and_existing_fixtures():
    records = load_document_generation_dataset(DATASET)
    assert len(records) >= 20
    assert len({r.id for r in records}) == len(records)
    index = json.loads(FIXTURE_INDEX.read_text(encoding="utf-8"))["fixtures"]
    for record in records:
        assert record.template_fixture in index
        assert record.field_id in index[record.template_fixture]["fields"]


def test_fixture_files_match_frozen_hashes_and_cover_required_cases():
    index = json.loads(FIXTURE_INDEX.read_text(encoding="utf-8"))["fixtures"]
    import hashlib
    assert len(index) >= 8
    for name, meta in index.items():
        path = FIXTURE_DIR / name
        assert path.exists(), name
        content = path.read_bytes()
        assert hashlib.sha256(content).hexdigest() == meta["sha256"]
        with zipfile.ZipFile(str(path)) as archive:
            assert archive.testzip() is None
    formats = {meta["format"] for meta in index.values()}
    assert {"xlsx", "docx"} <= formats


def test_manifest_fixture_hashes_match_index():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    index = json.loads(FIXTURE_INDEX.read_text(encoding="utf-8"))["fixtures"]
    assert manifest["fixture_hashes"] == {name: meta["sha256"] for name, meta in index.items()}


def test_dataset_covers_missing_conflict_and_scope_cases():
    records = load_document_generation_dataset(DATASET)
    rationales = " ".join(r.evidence_rationale or "" for r in records)
    assert any(r.expected_missing_policy == "keep_blank" for r in records)
    assert "冲突" in rationales
    assert "越界" in rationales
    assert any(r.expected_value_type == "table" for r in records)
    assert any(r.expected_value_type == "date" for r in records)
    assert any(r.expected_value_type == "version" for r in records)


# ── aggregation ──────────────────────────────────────────────────────────────


def test_aggregate_reports_required_optional_denominators():
    observations = [
        _observation(field_id="f-1", record_id="r-1", required=True, success=True, typed_value_ok=True),
        _observation(field_id="f-2", record_id="r-2", required=True, success=False, typed_value_ok=False),
        _observation(field_id="f-3", record_id="r-3", required=False, attempted=True,
                     success=None, optional_missing=True),
    ]
    metrics = aggregate_document_generation_metrics(observations)
    assert set(metrics) >= {
        "field_success_rate", "typed_value_success_rate", "writer_fallback_rate",
        "human_review_rate", "avg_llm_calls_per_field", "avg_duration_seconds_per_field",
        "token_usage", "required_field_success_rate", "optional_field_missing_rate",
    }
    assert metrics["field_success_rate"].denominator == 2
    assert metrics["field_success_rate"].value == pytest.approx(0.5)
    assert metrics["required_field_success_rate"].denominator == 2
    assert metrics["required_field_success_rate"].value == pytest.approx(0.5)
    assert metrics["optional_field_missing_rate"].denominator == 1
    assert metrics["optional_field_missing_rate"].value == 1.0


def test_unknown_telemetry_is_inconclusive_not_success():
    observations = [_observation(success=None, typed_value_ok=None, llm_calls=None,
                                 duration_seconds=None, token_usage={})]
    metrics = aggregate_document_generation_metrics(observations)
    assert metrics["field_success_rate"].status == INCONCLUSIVE
    assert metrics["field_success_rate"].value is None
    assert metrics["field_success_rate"].detail["unknown"] == 1
    assert metrics["typed_value_success_rate"].status == INCONCLUSIVE
    assert metrics["avg_llm_calls_per_field"].status == INCONCLUSIVE
    assert metrics["avg_duration_seconds_per_field"].status == INCONCLUSIVE
    assert metrics["token_usage"].status == INCONCLUSIVE


def test_token_usage_totals_and_writer_fallback_rate():
    observations = [
        _observation(field_id="f-1", token_usage={"prompt": 10, "completion": 5, "total": 15}),
        _observation(field_id="f-2", token_usage={"prompt": 10, "completion": 5, "total": 15},
                     writer_fallback=True),
        _observation(field_id="f-3", token_usage={}),
    ]
    metrics = aggregate_document_generation_metrics(observations)
    assert metrics["token_usage"].value == 30
    assert metrics["token_usage"].detail["unknown_fields"] == 1
    assert metrics["writer_fallback_rate"].value == pytest.approx(1 / 3)
    assert metrics["avg_llm_calls_per_field"].status == INCONCLUSIVE


def test_human_review_rate_counts_waiting_events():
    observations = [
        _observation(field_id="f-1", requires_human=True),
        _observation(field_id="f-2"),
    ]
    metrics = aggregate_document_generation_metrics(observations)
    assert metrics["human_review_rate"].value == 0.5


# ── collect_observations adapter ─────────────────────────────────────────────


def _event(event_type: str, field_id: str | None = None, **overrides) -> AuthoringExecutionEvent:
    payload = dict(
        event_id=f"evt-{event_type}-{field_id}", event_type=event_type, work_order_id="wo-1",
        harness_run_id="run-1", idempotency_key=f"key-{event_type}-{field_id}",
        field_id=field_id,
    )
    payload.update(overrides)
    return AuthoringExecutionEvent.model_validate(payload)


def test_collect_observations_from_run_events_and_drafts():
    run = HarnessRun(harness_run_id="run-1", work_order_id="wo-1", run_manifest_id="rm-1",
                     unit_statuses={"f-1": "committed", "f-2": "requires_human"})
    events = [
        _event("llm_called", "f-1"),
        _event("llm_called", "f-1"),
        _event("human_waiting", "f-2"),
        _event("fallback_started", "f-2"),
    ]
    drafts = [
        {"unit_id": "f-1", "validation_status": "supported", "typed_value": {"value": "10 A"},
         "metadata": {"writer_mode": "structured"}},
        {"unit_id": "f-2", "validation_status": "unsupported", "metadata": {}},
    ]
    observations = collect_observations(
        run, events, [], drafts,
        record_id_by_field={"f-1": "r-1", "f-2": "r-2"},
        field_required={"f-1": True, "f-2": True},
    )
    by_field = {obs.field_id: obs for obs in observations}
    assert by_field["f-1"].success is True
    assert by_field["f-1"].typed_value_ok is True
    assert by_field["f-1"].llm_calls == 2
    assert by_field["f-1"].writer_mode == "structured"
    assert by_field["f-2"].success is False
    assert by_field["f-2"].requires_human is True
    assert by_field["f-2"].writer_fallback is True


def test_collect_observations_marks_optional_missing_legally():
    run = HarnessRun(harness_run_id="run-1", work_order_id="wo-1", run_manifest_id="rm-1",
                     unit_statuses={"f-1": "tbd"})
    observations = collect_observations(run, [], [], [],
                                        field_required={"f-1": False})
    assert observations[0].optional_missing is True
    assert observations[0].success is None


def test_collect_observations_keeps_missing_telemetry_unknown():
    run = HarnessRun(harness_run_id="run-1", work_order_id="wo-1", run_manifest_id="rm-1")
    observations = collect_observations(run, [], [], [])
    assert observations == []
