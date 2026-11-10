from __future__ import annotations

from types import SimpleNamespace

from src.ingestion.source_groups import DOCS_GROUP
from src.services.document_archive import DocumentArchiveManager


def test_directory_archive_uses_directory_id_and_resolves_legacy_relative_path(tmp_path, monkeypatch):
    import src.settings

    monkeypatch.setattr(src.settings, "PIPELINE_ARCHIVE_ROOT", str(tmp_path / "archives"))
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf-bytes")
    archive = DocumentArchiveManager()

    archived_path, remote_name, group = archive.archive_source_file(
        "kb",
        str(source),
        DOCS_GROUP,
        department_id="dept",
        directory_id=12,
        logical_filename="spec.pdf",
        remote_filename="hash12__spec.pdf",
    )
    assert group == DOCS_GROUP
    assert remote_name == "hash12__spec.pdf"
    assert "/directories/12/" in archived_path.replace("\\", "/")

    relative_path = archived_path.split("/kbs/kb/", 1)[-1]
    record = SimpleNamespace(
        kb_name="kb",
        department_id="dept",
        directory_id=12,
        source_group=DOCS_GROUP,
        document_name="spec.pdf",
        local_path=relative_path,
    )
    assert archive.resolve_record_path(record) == archived_path
    assert archive.record_archive_exists(record)

    archive.remove_record_archive(record)
    assert not archive.record_archive_exists(record)


def test_directory_archive_allocates_unique_remote_names_for_same_logical_basename(tmp_path, monkeypatch):
    import src.settings

    monkeypatch.setattr(src.settings, "PIPELINE_ARCHIVE_ROOT", str(tmp_path / "archives"))
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf-bytes")
    archive = DocumentArchiveManager()

    first_path, first_name, _ = archive.archive_source_file(
        "kb", str(source), DOCS_GROUP, department_id="dept", directory_id=12,
        logical_filename="spec.pdf", remote_filename="unique-spec.pdf",
    )
    second_path, second_name, _ = archive.archive_source_file(
        "kb", str(source), DOCS_GROUP, department_id="dept", directory_id=12,
        logical_filename="spec.pdf", remote_filename="unique-spec.pdf",
    )
    assert first_name == "unique-spec.pdf"
    assert second_name != first_name
    assert second_path != first_path
