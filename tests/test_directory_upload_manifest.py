from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.api.routes.upload import _build_file_metadata
from src.ingestion.source_groups import DESIGN_GROUP, DOCS_GROUP, MATERIAL_GROUP
from src.pipelines.document_store import PipelineDocumentStore
from src.services.pipeline_directories import DirectoryValidationError, PipelineDirectoryService


def test_manifest_resolves_each_file_to_its_own_directory_and_source_group(tmp_path):
    store = PipelineDocumentStore(db_path=str(tmp_path / "pipeline.db"))
    directories = PipelineDirectoryService(store=store)
    base = directories.create(
        kb_name="kb", department_id="dept", kb_id=7, parent_id=0,
        name="incoming", created_by="tester",
    )
    files = [
        SimpleNamespace(filename="spec.pdf"),
        SimpleNamespace(filename="bom.xlsx"),
        SimpleNamespace(filename="netlist.edf"),
    ]
    manifest = json.dumps({
        "entries": [
            {"index": 0, "relative_path": "docs/spec.pdf", "source_group": DOCS_GROUP},
            {"index": 1, "relative_path": "bom/bom.xlsx", "source_group": MATERIAL_GROUP},
            {"index": 2, "relative_path": "design/netlist.edf", "source_group": DESIGN_GROUP},
        ]
    }, ensure_ascii=False)

    metadata = _build_file_metadata(
        files,
        manifest=manifest,
        base_directory_id=base["id"],
        source_group=None,
        directories=directories,
        kb_name="kb",
        department_id="dept",
        kb_id=7,
        created_by="tester",
    )

    assert [item["source_group"] for item in metadata] == [DOCS_GROUP, MATERIAL_GROUP, DESIGN_GROUP]
    assert [item["relative_path"] for item in metadata] == [
        "incoming/docs/spec.pdf",
        "incoming/bom/bom.xlsx",
        "incoming/design/netlist.edf",
    ]
    assert len({item["directory_id"] for item in metadata}) == 3


def test_manifest_path_is_validated_before_upload_metadata_is_returned(tmp_path):
    store = PipelineDocumentStore(db_path=str(tmp_path / "pipeline.db"))
    directories = PipelineDirectoryService(store=store)
    files = [SimpleNamespace(filename="escape.pdf")]
    manifest = json.dumps({"entries": [{"index": 0, "relative_path": "../escape.pdf"}]})

    with pytest.raises(DirectoryValidationError):
        _build_file_metadata(
            files,
            manifest=manifest,
            base_directory_id=0,
            source_group=None,
            directories=directories,
            kb_name="kb",
            department_id="dept",
            kb_id=7,
            created_by="tester",
        )
