"""Regression tests for the durable result-export follow-up work."""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import src.settings

from src.core.conversation import ConversationService
from src.result_exports.content import envelope_from_turn
from src.result_exports.models import ResultEnvelope
from src.result_exports.renderers import render_result
from src.result_exports.store import ResultExportStore
from src.result_exports.worker import ResultExportWorker
from tests._api_stub import make_auth


def infer_export_intent(query: str):
    """Keep the RED test importable until the server intent module exists."""
    import src.result_exports

    resolver = getattr(src.result_exports, "infer_export_intent", lambda _query: None)
    return resolver(query)


def _envelope() -> ResultEnvelope:
    return ResultEnvelope(
        title="结构化结果",
        query="查询器件",
        answer="找到结果。",
        tables=[
            {
                "name": "器件",
                "columns": ["型号", "数量", "启用", "日期"],
                "rows": [["U1", 2, True, date(2026, 9, 2)]],
            }
        ],
        citations=[{"index": 1, "title": "parts.xlsx", "locator": "Sheet1!A2"}],
    )


def test_server_export_intent_requires_explicit_output_language_and_normalizes_formats():
    plan = infer_export_intent("请把检索结果输出成 Excel 和 PDF")

    assert plan is not None
    assert plan.formats == ("xlsx", "pdf")
    assert plan.content_shape == "report"
    assert infer_export_intent("PDF 中的电源规格是什么？") is None
    assert infer_export_intent("不要导出成 Excel") is None


def test_envelope_citations_keep_stable_evidence_identity():
    turn = SimpleNamespace(
        query="查询器件",
        answer="找到结果",
        footer="",
        summary={
            "evidence": [
                {
                    "id": "evidence-7",
                    "file_name": "parts.xlsx",
                    "text": "U1",
                    "metadata": {"sheet_name": "Sheet1", "row_index": 1},
                }
            ]
        },
        kb_name="shared",
        session_id=3,
        id="turn-identity",
        query_mode="deep",
    )

    citation = envelope_from_turn(turn).citations[0]

    assert citation["evidence_id"] == "evidence-7"
    assert citation["locator"] == "Sheet1 · 第 2 行"
    assert "evidence-7" in render_result(envelope_from_turn(turn), "md").content.decode("utf-8")


def test_server_export_intent_omits_disabled_formats(monkeypatch):
    import src.settings as settings

    monkeypatch.setattr(settings, "RESULT_EXPORT_XLSX_ENABLED", False)

    plan = infer_export_intent("请把检索结果输出成 Excel 和 PDF")

    assert plan is not None
    assert plan.formats == ("pdf",)


def test_store_rejects_disabled_format_for_manual_export(tmp_path, monkeypatch):
    import src.settings as settings

    monkeypatch.setattr(settings, "RESULT_EXPORT_PDF_ENABLED", False)
    store = ResultExportStore(str(tmp_path / "auth.db"), storage_dir=str(tmp_path / "exports"))
    snapshot = store.create_snapshot(
        owner_user_id=7,
        tenant_id="default",
        session_id=3,
        turn_id="turn-disabled-format",
        envelope=_envelope(),
    )

    with pytest.raises(ValueError, match="disabled"):
        store.create_export_job(
            owner_user_id=7,
            tenant_id="default",
            session_id=3,
            snapshot_id=snapshot.snapshot_id,
            format="pdf",
            client_request_id="disabled-format",
        )


def test_completed_turn_persists_export_plan_and_enqueues_jobs_without_browser_callback(tmp_path):
    db_path = str(tmp_path / "auth.db")
    _auth, _department, _admin, user = make_auth(db_path)
    conversation = ConversationService(db_path)
    session = conversation.create_session(user.id, "shared")

    turn = conversation.create_turn(
        user.id,
        session.id,
        "请把检索结果输出成 Excel 和 PDF",
        client_request_id="durable-export-turn",
    )
    assert turn.export_plan["formats"] == ["xlsx", "pdf"]

    completed = conversation.complete_turn(user.id, turn.id, "答案", {"evidence": []})
    store = ResultExportStore(db_path, storage_dir=str(tmp_path / "exports"))
    jobs = store.list_export_jobs(user.id, session_id=session.id)

    assert completed.status == "completed"
    assert {job.format for job in jobs} == {"xlsx", "pdf"}
    assert all(job.snapshot_id for job in jobs)


def test_snapshot_has_version_and_content_hash_and_preserves_typed_table_values(tmp_path):
    store = ResultExportStore(str(tmp_path / "auth.db"), storage_dir=str(tmp_path / "exports"))
    snapshot = store.create_snapshot(
        owner_user_id=7,
        tenant_id="tenant-a",
        session_id=3,
        turn_id="turn-typed",
        envelope=_envelope(),
    )

    assert snapshot.schema_version == "v1"
    assert len(snapshot.source_hash) == 64
    assert snapshot.envelope.tables[0]["value_types"] == ["text", "number", "boolean", "date"]


def test_persisted_snapshot_keeps_date_cells_typed_for_xlsx(tmp_path):
    store = ResultExportStore(str(tmp_path / "auth.db"), storage_dir=str(tmp_path / "exports"))
    snapshot = store.create_snapshot(
        owner_user_id=7,
        tenant_id="tenant-a",
        session_id=3,
        turn_id="turn-persisted-date",
        envelope=_envelope(),
    )

    persisted = store.get_snapshot(7, snapshot.snapshot_id)
    assert persisted is not None
    rendered = render_result(persisted.envelope, "xlsx")

    with zipfile.ZipFile(io.BytesIO(rendered.content)) as archive:
        sheet = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")

    assert 's="1"' in sheet


def test_snapshot_hash_rejects_tampered_payload(tmp_path):
    db_path = str(tmp_path / "auth.db")
    store = ResultExportStore(db_path, storage_dir=str(tmp_path / "exports"))
    snapshot = store.create_snapshot(
        owner_user_id=7,
        tenant_id="tenant-a",
        session_id=3,
        turn_id="turn-tampered",
        envelope=_envelope(),
    )
    import sqlite3

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE result_snapshots SET envelope_json = ? WHERE snapshot_id = ?",
            ('{"answer":"tampered"}', snapshot.snapshot_id),
        )

    with pytest.raises(ValueError, match="integrity"):
        store.get_snapshot(7, snapshot.snapshot_id)


def test_artifact_read_rejects_tampered_file_and_history_retains_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(src.settings, "RESULT_EXPORT_RETENTION_DAYS", 1, raising=False)
    store = ResultExportStore(str(tmp_path / "auth.db"), storage_dir=str(tmp_path / "exports"))
    snapshot = store.create_snapshot(
        owner_user_id=7,
        tenant_id="tenant-a",
        session_id=3,
        turn_id="turn-artifact-integrity",
        envelope=_envelope(),
    )
    job = store.create_export_job(
        owner_user_id=7,
        tenant_id="tenant-a",
        session_id=3,
        snapshot_id=snapshot.snapshot_id,
        format="md",
        client_request_id="artifact-integrity",
    )
    assert ResultExportWorker(store=store, worker_id="integrity-worker").run_once() is True
    completed = store.get_export_job(7, job.export_job_id)
    assert completed is not None and completed.artifact_id
    artifact = store.get_artifact(7, completed.artifact_id)
    assert artifact is not None
    path = store.storage_dir / artifact.storage_ref
    path.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="integrity"):
        store.read_artifact(7, artifact.artifact_id)

    # The history API keeps the immutable metadata even after the binary is
    # expired/removed from the live download surface.
    store.cleanup_expired(now=datetime.now(timezone.utc) + timedelta(days=2))
    history = store.list_artifact_history(7, session_id=3)
    assert len(history) == 1
    assert history[0].artifact.artifact_id == artifact.artifact_id
    assert history[0].available is False
    assert history[0].artifact.sha256 == artifact.sha256


def test_renderer_reports_and_validates_content_shape():
    rendered = render_result(_envelope(), "md", content_shape="data")

    assert rendered.preview["content_shape"] == "data"
    content = rendered.content.decode("utf-8")
    assert "找到结果" not in content
    assert "型号" in content
    with pytest.raises(ValueError, match="content shape"):
        render_result(_envelope(), "md", content_shape="unknown")


def test_xlsx_renderer_keeps_number_boolean_and_date_cell_types(tmp_path):
    rendered = render_result(_envelope(), "xlsx")

    with zipfile.ZipFile(io.BytesIO(rendered.content)) as archive:
        sheet = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
        styles = archive.read("xl/styles.xml").decode("utf-8")

    assert '<c r="B2"><v>2</v></c>' in sheet
    assert '<c r="C2" t="b"><v>1</v></c>' in sheet
    assert 's="1"' in sheet
    assert "yyyy-mm-dd" in styles


def test_export_worker_renews_lease_before_rendering(tmp_path, monkeypatch):
    store = ResultExportStore(str(tmp_path / "auth.db"), storage_dir=str(tmp_path / "exports"))
    snapshot = store.create_snapshot(
        owner_user_id=7,
        tenant_id="default",
        session_id=3,
        turn_id="turn-heartbeat",
        envelope=_envelope(),
    )
    job = store.create_export_job(
        owner_user_id=7,
        tenant_id="default",
        session_id=3,
        snapshot_id=snapshot.snapshot_id,
        format="md",
        client_request_id="heartbeat-export",
    )
    original_heartbeat = store.heartbeat
    calls = []

    def heartbeat(*args, **kwargs):
        calls.append((args, kwargs))
        return original_heartbeat(*args, **kwargs)

    monkeypatch.setattr(store, "heartbeat", heartbeat)

    assert ResultExportWorker(store=store, worker_id="heartbeat-worker").run_once() is True
    assert calls
    assert store.get_export_job(7, job.export_job_id).status == "succeeded"


def test_export_worker_records_job_render_and_artifact_metrics(tmp_path, monkeypatch):
    import src.observability.metrics as metrics

    store = ResultExportStore(str(tmp_path / "auth.db"), storage_dir=str(tmp_path / "exports"))
    snapshot = store.create_snapshot(
        owner_user_id=7,
        tenant_id="default",
        session_id=3,
        turn_id="turn-export-metrics",
        envelope=_envelope(),
    )
    job = store.create_export_job(
        owner_user_id=7,
        tenant_id="default",
        session_id=3,
        snapshot_id=snapshot.snapshot_id,
        format="md",
        client_request_id="export-metrics",
    )
    calls = []
    monkeypatch.setattr(
        metrics,
        "record_export_job",
        lambda **kwargs: calls.append(("job", kwargs)),
    )
    monkeypatch.setattr(
        metrics,
        "record_export_render",
        lambda **kwargs: calls.append(("render", kwargs)),
    )
    monkeypatch.setattr(
        metrics,
        "record_export_bytes",
        lambda **kwargs: calls.append(("bytes", kwargs)),
    )

    assert ResultExportWorker(store=store, worker_id="metrics-worker").run_once() is True
    assert store.get_export_job(7, job.export_job_id).status == "succeeded"
    assert any(kind == "job" and item["status"] == "succeeded" for kind, item in calls)
    assert any(kind == "render" and item["status"] == "succeeded" for kind, item in calls)
    assert any(kind == "bytes" and item["byte_count"] > 0 for kind, item in calls)
