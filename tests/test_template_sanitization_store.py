from hashlib import sha256
from pathlib import Path

import pytest

from src.document_authoring.models import (
    TemplateSanitizationReport,
    TemplateSecurityReport,
    TemplateVersion,
)
from src.document_authoring.work_order_store import DocumentAuthoringStore


def _template(*, content_hash: str, format: str) -> TemplateVersion:
    return TemplateVersion(
        template_version_id="template-1",
        template_id="review-template",
        format=format,
        content_hash=content_hash,
        template_schema_id="schema-1",
        template_schema_version="1",
        renderer_policy_id="policy-1",
    )


def _clean_security_report(content: bytes) -> TemplateSecurityReport:
    return TemplateSecurityReport(
        report_id="security-1",
        content_hash=sha256(content).hexdigest(),
        format="xlsx",
    )


def _sanitization_report(template_version_id: str, *, source_hash: str) -> TemplateSanitizationReport:
    return TemplateSanitizationReport(
        template_version_id=template_version_id,
        source_format="xlsm",
        source_content_hash=source_hash,
        source_storage_ref="",
        sanitized_format="xlsx",
        sanitized_content_hash=sha256(b"safe").hexdigest(),
        removed_parts=["xl/vbaProject.bin"],
        removed_relationships=[],
        status="sanitized",
    )


def test_save_sanitized_template_keeps_raw_source_separate_from_render_template(tmp_path):
    store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "files"))
    template = _template(content_hash=sha256(b"safe").hexdigest(), format="xlsx")
    report = _sanitization_report(template.template_version_id, source_hash=sha256(b"raw").hexdigest())

    saved = store.save_sanitized_template(
        template, b"raw", "xlsm", b"safe", _clean_security_report(b"safe"), report,
    )

    saved_report = store.get_template_sanitization_report(saved.template_version_id)
    assert store.read_template_content(saved.template_version_id) == b"safe"
    assert saved_report is not None
    assert Path(saved_report.source_storage_ref).read_bytes() == b"raw"
    assert saved.content_hash != saved_report.source_content_hash


def test_save_sanitized_template_rejects_hashes_that_do_not_match_persisted_bytes(tmp_path):
    store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "files"))
    template = _template(content_hash=sha256(b"not-safe").hexdigest(), format="xlsx")
    report = _sanitization_report(template.template_version_id, source_hash=sha256(b"raw").hexdigest())

    with pytest.raises(ValueError, match="sanitized content hash"):
        store.save_sanitized_template(
            template, b"raw", "xlsm", b"safe", _clean_security_report(b"safe"), report,
        )


def test_save_sanitized_template_rejects_unsupported_source_to_safe_conversion(tmp_path):
    store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "files"))
    template = _template(content_hash=sha256(b"safe").hexdigest(), format="docx")
    report = _sanitization_report(template.template_version_id, source_hash=sha256(b"raw").hexdigest())

    with pytest.raises(ValueError, match="unsupported sanitization conversion"):
        store.save_sanitized_template(
            template, b"raw", "xlsm", b"safe", _clean_security_report(b"safe"), report,
        )


def test_save_sanitized_template_rejects_duplicate_template_ids(tmp_path):
    store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "files"))
    template = _template(content_hash=sha256(b"safe").hexdigest(), format="xlsx")
    report = _sanitization_report(template.template_version_id, source_hash=sha256(b"raw").hexdigest())
    store.save_sanitized_template(
        template, b"raw", "xlsm", b"safe", _clean_security_report(b"safe"), report,
    )

    with pytest.raises(ValueError, match="template already exists"):
        store.save_sanitized_template(
            template, b"raw", "xlsm", b"safe", _clean_security_report(b"safe"), report,
        )
