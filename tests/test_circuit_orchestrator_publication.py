from __future__ import annotations

import json
import threading

import pytest

from src.circuit.graph_store import GraphStore
from src.circuit.image_cache import ImageCache
from src.circuit.index_service import CircuitIndexService
from src.circuit.models import ComponentInstance, Net, Pin, PinRef
from src.circuit.orchestrator import CircuitOrchestrator
from src.circuit.store import CircuitStore, circuit_generation_id
from src.circuit.vector_index import CircuitVectorIndexStatus
from src.pipelines.document_rag.schemas import RequestContext


class _Parser:
    warnings = []

    def __init__(self, value: str):
        self.value = value

    def parse(self):
        refdes = "U100" if self.value == "A" else "U200"
        return (
            [ComponentInstance(refdes=refdes, value=self.value, pins=[Pin(name="1", net="FANOUT")])],
            [Net(name="FANOUT", connections=[PinRef(refdes=refdes, pin="1")])],
            [],
        )


class _UnavailableVectorIndex:
    def reindex_design_with_status(self, design):
        return CircuitVectorIndexStatus(available=False, indexed_count=0)


class _BlockingGraphStore(GraphStore):
    def __init__(self):
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def save(self, design, design_dir):
        if self.block:
            self.entered.set()
            if not self.release.wait(3):
                raise TimeoutError("graph release timed out")
        return super().save(design, design_dir)


class _RecordingImageCache:
    def __init__(self, events):
        self.events = events

    def replace_pdf(self, kb_name, design_id, source_path):
        self.events.append(("cache", kb_name, design_id, source_path))


def _service(tmp_path, graph_store):
    return CircuitIndexService(
        storage_root=str(tmp_path / "circuits"),
        parser_factory=lambda path, progress_callback=None: _Parser("A"),
        graph_store=graph_store,
        vector_index=_UnavailableVectorIndex(),
    )


def test_legacy_orchestrator_reader_waits_for_complete_publication(tmp_path):
    graph_store = _BlockingGraphStore()
    service = _service(tmp_path, graph_store)
    service.index_file(
        kb_name="kb_hw",
        record_id=1,
        file_path=str(tmp_path / "a.edf"),
        original_name="same_board.edf",
        department_id="dept_a",
    )
    orchestrator = CircuitOrchestrator(
        store=service.store,
        graph_store=graph_store,
        index_service=service,
    )
    graph_store.block = True
    writer = threading.Thread(
        target=lambda: orchestrator.apply_edf_parse(
            "kb_hw",
            "same_board.edf",
            str(tmp_path / "b.edf"),
            [ComponentInstance(refdes="U200", value="B", pins=[Pin(name="1", net="FANOUT")])],
            [Net(name="FANOUT", connections=[PinRef(refdes="U200", pin="1")])],
            [],
            [],
        )
    )
    reader_finished = threading.Event()
    reader_hits = []

    def query_reader():
        try:
            reader_hits.extend(service.query(
                kb_name="kb_hw",
                query="U200",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_a"}),
            ))
        finally:
            reader_finished.set()

    writer.start()
    assert graph_store.entered.wait(1)
    reader = threading.Thread(target=query_reader)
    reader.start()
    try:
        assert not reader_finished.wait(0.2)
    finally:
        graph_store.release.set()
        writer.join(3)
        reader.join(3)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert any(hit.locator["entity_id"] == "U200" for hit in reader_hits)
    metadata_path = service._metadata_path("kb_hw", "same_board")
    with open(metadata_path, encoding="utf-8") as fh:
        metadata = json.load(fh)
    design = service.store.load("kb_hw", "same_board")
    assert metadata["department_id"] == "dept_a"
    assert metadata["generation_id"] == circuit_generation_id(design)


def test_legacy_orchestrator_metadata_failure_rolls_back_generation(tmp_path, monkeypatch):
    graph_store = GraphStore()
    service = _service(tmp_path, graph_store)
    service.index_file(
        kb_name="kb_hw",
        record_id=1,
        file_path=str(tmp_path / "a.edf"),
        original_name="same_board.edf",
        department_id="dept_a",
    )
    orchestrator = CircuitOrchestrator(
        store=service.store,
        graph_store=graph_store,
        index_service=service,
    )
    crop_calls = []
    monkeypatch.setattr(
        orchestrator,
        "_crop_module_screenshots",
        lambda design: crop_calls.append(design.design_id),
    )
    before = service.store.load("kb_hw", "same_board")
    monkeypatch.setattr(
        service,
        "_write_metadata",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("metadata full")),
    )

    with pytest.raises(OSError, match="metadata full"):
        orchestrator.apply_edf_parse(
            "kb_hw",
            "same_board.edf",
            str(tmp_path / "b.edf"),
            [ComponentInstance(refdes="U200", value="B")],
            [],
            [],
            [],
        )

    restored = service.store.load("kb_hw", "same_board")
    assert circuit_generation_id(restored) == circuit_generation_id(before)
    assert restored.instances[0].refdes == "U100"
    assert crop_calls == []


def test_first_legacy_publication_requires_authorization_metadata(tmp_path):
    store = CircuitStore(root=str(tmp_path / "circuits"))
    orchestrator = CircuitOrchestrator(store=store, index_service=CircuitIndexService(store=store))

    with pytest.raises(PermissionError, match="record_id and department_id"):
        orchestrator.apply_edf_parse(
            "kb_hw",
            "new_board.edf",
            str(tmp_path / "new.edf"),
            [ComponentInstance(refdes="U1")],
            [],
            [],
            [],
        )

    assert store.load("kb_hw", "new_board") is None


def test_orchestrator_rejects_index_service_with_different_root(tmp_path):
    store = CircuitStore(root=str(tmp_path / "one"))
    other_service = CircuitIndexService(store=CircuitStore(root=str(tmp_path / "two")))

    with pytest.raises(ValueError, match="same storage root"):
        CircuitOrchestrator(store=store, index_service=other_service)


def test_pdf_cache_replacement_precedes_crop_inside_orchestrator(tmp_path, monkeypatch):
    service = _service(tmp_path, GraphStore())
    service.index_file(
        kb_name="kb_hw",
        record_id=1,
        file_path=str(tmp_path / "a.edf"),
        original_name="same_board.edf",
        department_id="dept_a",
    )
    events = []
    orchestrator = CircuitOrchestrator(
        store=service.store,
        index_service=service,
        image_cache=_RecordingImageCache(events),
    )
    monkeypatch.setattr(
        orchestrator,
        "_crop_module_screenshots",
        lambda design: events.append(("crop", design.kb_name, design.design_id)),
    )

    orchestrator.apply_pdf_parse(
        "kb_hw",
        "same_board.pdf",
        str(tmp_path / "same_board.pdf"),
        [],
        [],
    )

    assert [event[0] for event in events] == ["cache", "crop"]


def test_pdf_cache_replacement_removes_previous_generation(tmp_path):
    store = CircuitStore(root=str(tmp_path / "circuits"))
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    old_pdf = old_dir / "board.pdf"
    new_pdf = new_dir / "board.pdf"
    old_pdf.write_bytes(b"generation A")
    new_pdf.write_bytes(b"generation B")
    cache = ImageCache(store)
    cache.cache_pdf("kb_hw", "board", str(old_pdf))

    replaced = cache.replace_pdf("kb_hw", "board", str(new_pdf))

    cached = store.list_pdf_cache("kb_hw", "board")
    assert cached == [replaced]
    with open(replaced, "rb") as fh:
        assert fh.read() == b"generation B"
