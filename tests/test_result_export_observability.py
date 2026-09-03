import os
import subprocess
import sys
from unittest.mock import patch


EXPORT_FLAGS = (
    "RESULT_EXPORT_ENABLED",
    "RESULT_EXPORT_MD_ENABLED",
    "RESULT_EXPORT_XLSX_ENABLED",
    "RESULT_EXPORT_DOCX_ENABLED",
    "RESULT_EXPORT_PDF_ENABLED",
    "RESULT_EXPORT_PPTX_ENABLED",
)


def test_export_flags_have_compatible_defaults():
    env = os.environ.copy()
    for key in EXPORT_FLAGS:
        env.pop(key, None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import src.settings as s; "
                "assert s.RESULT_EXPORT_ENABLED is True; "
                "assert all(getattr(s, key) is True for key in "
                "('RESULT_EXPORT_MD_ENABLED', 'RESULT_EXPORT_XLSX_ENABLED', "
                "'RESULT_EXPORT_DOCX_ENABLED', 'RESULT_EXPORT_PDF_ENABLED', "
                "'RESULT_EXPORT_PPTX_ENABLED'))"
            ),
        ],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_reload_settings_parses_global_and_per_format_export_flags():
    import src.settings as settings

    previous = {key: getattr(settings, key) for key in EXPORT_FLAGS}
    values = {
        "RESULT_EXPORT_ENABLED": "off",
        "RESULT_EXPORT_MD_ENABLED": "0",
        "RESULT_EXPORT_XLSX_ENABLED": "false",
        "RESULT_EXPORT_DOCX_ENABLED": "no",
        "RESULT_EXPORT_PDF_ENABLED": "on",
        "RESULT_EXPORT_PPTX_ENABLED": "1",
    }
    try:
        with patch.dict(os.environ, values), patch("src.settings.load_dotenv"):
            settings.reload_settings()

        assert settings.RESULT_EXPORT_ENABLED is False
        assert settings.RESULT_EXPORT_MD_ENABLED is False
        assert settings.RESULT_EXPORT_XLSX_ENABLED is False
        assert settings.RESULT_EXPORT_DOCX_ENABLED is False
        assert settings.RESULT_EXPORT_PDF_ENABLED is True
        assert settings.RESULT_EXPORT_PPTX_ENABLED is True
        assert all(settings.DEFAULT_VALUES[key] == "true" for key in EXPORT_FLAGS)
    finally:
        for key, value in previous.items():
            setattr(settings, key, value)


def test_export_metrics_record_low_cardinality_job_render_bytes_download_and_queue(monkeypatch):
    import src.observability.metrics as metrics

    calls = []
    monkeypatch.setattr(metrics, "_enabled", lambda: True)
    monkeypatch.setattr(
        metrics,
        "counter",
        lambda name, **kwargs: calls.append(("counter", name, kwargs)),
    )
    monkeypatch.setattr(
        metrics,
        "histogram",
        lambda name, value, **kwargs: calls.append(("histogram", name, value, kwargs)),
    )
    monkeypatch.setattr(metrics, "set_queue_state", lambda **kwargs: calls.append(("queue", kwargs)))

    metrics.record_export_job(
        format="XLSX",
        status="succeeded",
        duration_s=2.5,
        queue_s=0.4,
        job_id="secret-job-id",
    )
    metrics.record_export_job(
        format="pdf",
        status="dead_letter",
        duration_s=3.0,
        job_id="secret-dead-letter-id",
    )
    metrics.record_export_render(format="xlsx", status="succeeded", duration_s=1.2)
    metrics.record_export_bytes(format="xlsx", byte_count=2048)
    metrics.record_export_download(format="xlsx", status="succeeded")
    metrics.set_export_queue_state(queue="result-export", depth=3, oldest_age_s=4.5)

    assert metrics.metric_attributes({"format": "xlsx", "job_id": "secret-job-id"}) == {"format": "xlsx"}
    assert metrics.metric_attributes({"format": "never-seen"}) == {"format": "never-seen"}
    assert ("counter", "hdb.export.jobs", {"attributes": {"format": "xlsx", "status": "succeeded"}, "description": "Export jobs"}) in calls
    assert any(
        item[:3] == ("counter", "hdb.export.jobs", {"attributes": {"format": "pdf", "status": "dead_letter"}, "description": "Export jobs"})
        for item in calls
    )
    assert any(item[:3] == ("histogram", "hdb.export.render.duration", 1.2) for item in calls)
    assert any(item[:3] == ("histogram", "hdb.export.artifact.bytes", 2048) for item in calls)
    assert any(item[0:2] == ("counter", "hdb.export.downloads") for item in calls)
    assert ("queue", {"queue": "result-export", "depth": 3, "oldest_age_s": 4.5}) in calls
    assert all("secret-job-id" not in repr(item) for item in calls)
