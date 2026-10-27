"""Attachment citation readback has typed failures and exact locators."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from src.agents.schemas import Evidence
from src.pipelines.document_rag.schemas import ScopedRetrievalError
from src.services.citation_readback import read_attachment_citation, read_evidence_citation


class _Store:
    def __init__(self):
        self.record = SimpleNamespace(
            attachment_id="att-1", asset_id="asset-1", session_id=1,
            user_id=7, sha256="a" * 64,
        )
        text = "管脚 2 电压 3.3V"
        self.parts = [SimpleNamespace(
            part_id="part-1", ordinal=1, text_content=text,
            locator={"heading_path": ["HSI", "管脚定义"], "paragraph": 1},
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
        )]
        self.unavailable = False

    def get_attachment(self, *, attachment_id, session_id=None, user_id=None):
        if self.unavailable:
            raise RuntimeError("storage down")
        if attachment_id != "att-1" or user_id not in (None, 7):
            return None
        return self.record

    def list_parts(self, asset_id):
        if self.unavailable:
            raise RuntimeError("storage down")
        return self.parts if asset_id == "asset-1" else []


def _service():
    return SimpleNamespace(store=_Store())


def test_readback_returns_exact_part_under_live_acl():
    text = read_attachment_citation(
        _service(), attachment_id="att-1", user_id=7, session_id=1,
        locator={"paragraph": 1},
        expected_content_hash=hashlib.sha256("管脚 2 电压 3.3V".encode()).hexdigest(),
    )
    assert text == "管脚 2 电压 3.3V"


def test_missing_attachment_is_access_denied_not_empty():
    with pytest.raises(ScopedRetrievalError) as error:
        read_attachment_citation(
            _service(), attachment_id="att-1", user_id=8, session_id=1, locator={"paragraph": 1},
        )
    assert error.value.error_code == "citation_source_denied"


def test_locator_and_hash_failures_are_distinct():
    with pytest.raises(ScopedRetrievalError) as locator:
        read_attachment_citation(
            _service(), attachment_id="att-1", user_id=7, session_id=1,
            locator={"paragraph": 99},
        )
    assert locator.value.error_code == "citation_locator_missing"
    with pytest.raises(ScopedRetrievalError) as changed:
        read_attachment_citation(
            _service(), attachment_id="att-1", user_id=7, session_id=1,
            locator={"paragraph": 1}, expected_content_hash="0" * 64,
        )
    assert changed.value.error_code == "citation_source_changed"


def test_upstream_failure_is_not_reported_as_missing_content():
    service = _service()
    service.store.unavailable = True
    with pytest.raises(ScopedRetrievalError) as unavailable:
        read_attachment_citation(
            service, attachment_id="att-1", user_id=7, session_id=1,
            locator={"paragraph": 1},
        )
    assert unavailable.value.error_code == "citation_source_unavailable"


def test_read_evidence_citation_dispatches_by_source_type():
    evidence = Evidence(
        id="att-1", content="x", source_name="a.docx", content_kind="text",
        processor_kind="local_attachment:lexical",
        locator={"paragraph": 1},
        metadata={"source_type": "attachment", "attachment_id": "att-1"},
    )
    assert read_evidence_citation(
        evidence, attachment_service=_service(), user_id=7, session_id=1,
    ) == "管脚 2 电压 3.3V"
