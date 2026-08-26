import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from hashlib import sha256

from src.agents.state import Evidence
from src.circuit.graph_store import GraphIndexResult, GraphStore
from src.circuit.index_service import CircuitIndexService
from src.circuit.models import CircuitDesign, CircuitModule, ComponentInstance, Net, Pin, PinRef
from src.circuit.query_engine import CircuitQueryEngine
from src.circuit.store import CircuitStore
from src.circuit.vector_index import CircuitVectorHit, CircuitVectorIndexStatus
from src.pipelines.document_rag.schemas import RequestContext


class CircuitIndexServiceTests(unittest.TestCase):
    def test_index_file_persists_queryable_circuit_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "main_board.edf")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("(edif main_board)")
            calls = []
            store = _RecordingStore(os.path.join(tmp, "circuits"), calls)
            graph_store = _GraphStore(calls)
            vector_index = _VectorIndex(calls)
            service = CircuitIndexService(
                store=store,
                parser_factory=lambda path, progress_callback=None: _Parser(),
                graph_store=graph_store,
                vector_index=vector_index,
            )

            result = service.index_file(
                kb_name="kb_hw",
                record_id=7,
                file_path=source,
                original_name="main_board.edf",
                department_id="dept_hw",
                uploaded_by="alice",
            )
            hits = service.query(
                kb_name="kb_hw",
                query="U1200 CAN0 connection",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
                top_k=5,
            )
            graph_artifact_exists = os.path.exists(calls[1][3])

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "indexed")
        self.assertEqual(result.stats["instance_count"], 4)
        self.assertEqual(result.stats["graph_node_count"], 12)
        self.assertEqual(result.stats["graph_edge_count"], 14)
        self.assertEqual(result.stats["vector_document_count"], 9)
        self.assertEqual([call[0] for call in calls], ["structured", "graph", "vector"])
        self.assertEqual(calls[1][1].design_id, result.design_id)
        self.assertEqual(calls[2][1].design_id, result.design_id)
        self.assertEqual(calls[1][2], store.design_dir("kb_hw", result.design_id))
        self.assertTrue(graph_artifact_exists)
        self.assertTrue(hits)
        self.assertTrue(all(isinstance(hit, Evidence) for hit in hits))
        self.assertEqual(hits[0].source_name, "main_board.edf")
        self.assertEqual(hits[0].content_kind, "circuit_design")
        self.assertEqual(hits[0].processor_kind, "circuit_design")
        self.assertEqual(hits[0].metadata["kb_name"], "kb_hw")
        self.assertEqual(hits[0].metadata["department_id"], "dept_hw")
        self.assertEqual(hits[0].locator["record_id"], 7)
        self.assertIn("CAN0", hits[0].content)

    def test_index_file_treats_unconfigured_vector_index_as_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, vector_index=_UnavailableVectorIndex())

            result = service.index_file(
                kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "main_board.edf"),
                original_name="main_board.edf", department_id="dept_hw",
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "indexed")
        self.assertEqual(result.stats["vector_document_count"], 0)
        self.assertFalse(any("vector" in warning.casefold() for warning in result.warnings))

    def test_index_file_returns_degraded_when_graph_persistence_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, graph_store=_FailingGraphStore(), vector_index=_VectorIndex())

            result = service.index_file(
                kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "main_board.edf"),
                original_name="main_board.edf", department_id="dept_hw",
            )
            loaded = service.store.load("kb_hw", result.design_id)
            metadata = service._read_metadata("kb_hw", result.design_id)
            hits = service.query(kb_name="kb_hw", query="CAN0", ctx=None)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.stats["graph_node_count"], 0)
        self.assertTrue(any("graph" in warning.casefold() for warning in result.warnings))
        self.assertIsNotNone(loaded)
        self.assertEqual(metadata["record_id"], 7)
        self.assertTrue(hits)

    def test_index_file_returns_degraded_when_configured_vector_index_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, vector_index=_FailingVectorIndex())

            result = service.index_file(
                kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "main_board.edf"),
                original_name="main_board.edf", department_id="dept_hw",
            )
            loaded = service.store.load("kb_hw", result.design_id)
            metadata = service._read_metadata("kb_hw", result.design_id)
            hits = service.query(kb_name="kb_hw", query="CAN0", ctx=None)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.stats["vector_document_count"], 0)
        self.assertTrue(any("vector" in warning.casefold() for warning in result.warnings))
        self.assertFalse(any("private payload" in warning for warning in result.warnings))
        self.assertIsNotNone(loaded)
        self.assertEqual(metadata["record_id"], 7)
        self.assertTrue(hits)

    def test_parser_failure_preserves_previous_design_and_derived_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser_factory = _ToggleParserFactory()
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=parser_factory,
                graph_store=GraphStore(),
                vector_index=_UnavailableVectorIndex(),
            )
            source = os.path.join(tmp, "main_board.edf")
            service.index_file(
                kb_name="kb_hw", record_id=7, file_path=source,
                original_name="main_board.edf", department_id="dept_hw",
            )
            design_dir = service.store.design_dir("kb_hw", "main_board")
            artifact_hashes = {
                name: _file_hash(os.path.join(design_dir, name))
                for name in ("circuit_state.json", "pipeline_metadata.json", "connectivity_graph.gpickle")
            }
            parser_factory.raise_error = True

            with self.assertRaisesRegex(ValueError, "invalid EDF"):
                service.index_file(
                    kb_name="kb_hw", record_id=99, file_path=source,
                    original_name="main_board.edf", department_id="other",
                )

            final_hashes = {
                name: _file_hash(os.path.join(design_dir, name))
                for name in artifact_hashes
            }

        self.assertEqual(final_hashes, artifact_hashes)

    def test_reader_waits_for_cross_department_publication_before_authorizing_new_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "circuits")
            first = os.path.join(tmp, "generation-a.edf")
            second = os.path.join(tmp, "generation-b.edf")
            for path, generation in ((first, "A"), (second, "B")):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(generation)
            graph_store = _BlockingGenerationGraphStore()
            service = CircuitIndexService(
                storage_root=root,
                parser_factory=_FileGenerationParserFactory(),
                graph_store=graph_store,
                vector_index=_UnavailableVectorIndex(),
            )
            service.index_file(
                kb_name="kb_hw",
                record_id=1,
                file_path=first,
                original_name="same_board.edf",
                department_id="dept_a",
                uploaded_by="A",
            )
            graph_store.block_generation = "B"
            writer_outcomes = []
            writer = threading.Thread(
                target=lambda: writer_outcomes.append(service.index_file(
                    kb_name="kb_hw",
                    record_id=2,
                    file_path=second,
                    original_name="same_board.edf",
                    department_id="dept_b",
                    uploaded_by="B",
                )),
            )
            writer.start()
            self.assertTrue(graph_store.writer_blocked.wait(3))

            reader_hits = []
            reader_finished = threading.Event()

            def read_as_old_department():
                try:
                    reader_hits.extend(service.query(
                        kb_name="kb_hw",
                        query="U200",
                        ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_a"}),
                    ))
                finally:
                    reader_finished.set()

            reader = threading.Thread(target=read_as_old_department)
            reader.start()
            try:
                self.assertFalse(reader_finished.wait(0.2))
            finally:
                graph_store.release_writer.set()
                writer.join(5)
                reader.join(5)

            new_department_hits = service.query(
                kb_name="kb_hw",
                query="U200",
                ctx=RequestContext(user_id="bob", metadata={"department_id": "dept_b"}),
            )

        self.assertFalse(writer.is_alive())
        self.assertFalse(reader.is_alive())
        self.assertEqual([result.status for result in writer_outcomes], ["indexed"])
        self.assertEqual(reader_hits, [])
        self.assertTrue(any(hit.locator["entity_id"] == "U200" for hit in new_department_hits))

    def test_metadata_publication_failure_restores_previous_coherent_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "circuits")
            first = os.path.join(tmp, "generation-a.edf")
            second = os.path.join(tmp, "generation-b.edf")
            for path, generation in ((first, "A"), (second, "B")):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(generation)
            service = CircuitIndexService(
                storage_root=root,
                parser_factory=_FileGenerationParserFactory(),
                graph_store=GraphStore(),
                vector_index=_UnavailableVectorIndex(),
            )
            service.index_file(
                kb_name="kb_hw",
                record_id=1,
                file_path=first,
                original_name="same_board.edf",
                department_id="dept_a",
                uploaded_by="A",
            )
            design_dir = service.store.design_dir("kb_hw", "same_board")
            paths = [
                service.store.state_path("kb_hw", "same_board"),
                os.path.join(design_dir, "pipeline_metadata.json"),
                os.path.join(design_dir, "connectivity_graph.gpickle"),
                service.store.index_path(),
            ]
            before = {path: _file_hash(path) for path in paths}
            original_write_metadata = service._write_metadata
            service._write_metadata = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("metadata full"))
            try:
                with self.assertRaisesRegex(OSError, "metadata full"):
                    service.index_file(
                        kb_name="kb_hw",
                        record_id=2,
                        file_path=second,
                        original_name="same_board.edf",
                        department_id="dept_b",
                        uploaded_by="B",
                    )
            finally:
                service._write_metadata = original_write_metadata

            after = {path: _file_hash(path) for path in paths}
            restored = service.store.load("kb_hw", "same_board")
            old_department_hits = service.query(
                kb_name="kb_hw",
                query="U100",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_a"}),
            )
            new_department_hits = service.query(
                kb_name="kb_hw",
                query="U200",
                ctx=RequestContext(user_id="bob", metadata={"department_id": "dept_b"}),
            )

        self.assertEqual(after, before)
        self.assertEqual(restored.instances[0].refdes, "U100")
        self.assertTrue(any(hit.locator["entity_id"] == "U100" for hit in old_department_hits))
        self.assertEqual(new_department_hits, [])

    def test_failed_graph_replacement_never_returns_unremovable_stale_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "generation-a.edf")
            second = os.path.join(tmp, "generation-b.edf")
            for path, generation in ((first, "A"), (second, "B")):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(generation)
            graph_store = _UnremovableFailingGraphStore()
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=_FileGenerationParserFactory(),
                graph_store=graph_store,
                vector_index=_UnavailableVectorIndex(),
            )
            service.index_file(
                kb_name="kb_hw",
                record_id=1,
                file_path=first,
                original_name="same_board.edf",
                department_id="dept_hw",
            )
            graph_store.fail = True
            result = service.index_file(
                kb_name="kb_hw",
                record_id=2,
                file_path=second,
                original_name="same_board.edf",
                department_id="dept_hw",
            )

            hits = service.query(
                kb_name="kb_hw",
                query="FANOUT connection",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
            )

        self.assertEqual(result.status, "degraded")
        self.assertFalse(any(hit.locator["entity_type"] == "graph_relationship" for hit in hits))

    def test_legacy_graph_metadata_can_be_safely_republished_before_rollout(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CircuitStore(root=os.path.join(tmp, "circuits"))
            design = CircuitDesign(
                design_id="legacy",
                kb_name="kb_hw",
                instances=[ComponentInstance(refdes="U1", pins=[Pin(name="1", net="LEGACY_NET")])],
                nets=[Net(name="LEGACY_NET", connections=[PinRef(refdes="U1", pin="1")])],
            )
            store.save(design)
            GraphStore().save(design, store.design_dir("kb_hw", "legacy"))
            service = CircuitIndexService(store=store, vector_index=_UnavailableVectorIndex())
            service._write_metadata(
                "kb_hw",
                "legacy",
                {
                    "record_id": 1,
                    "department_id": "dept_hw",
                    "original_name": "legacy.edf",
                },
            )
            ctx = RequestContext(user_id="alice", metadata={"department_id": "dept_hw"})

            before = service.query(kb_name="kb_hw", query="LEGACY_NET", ctx=ctx)
            result = service.reindex_stored_design("kb_hw", "legacy")
            after = service.query(kb_name="kb_hw", query="LEGACY_NET", ctx=ctx)

        self.assertFalse(any(hit.locator["entity_type"] == "graph_relationship" for hit in before))
        self.assertEqual(result.status, "indexed")
        self.assertTrue(any(hit.locator["entity_type"] == "graph_relationship" for hit in after))

    def test_query_returns_empty_for_kb_without_indexed_circuits(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CircuitIndexService(storage_root=os.path.join(tmp, "circuits"))

            hits = service.query(
                kb_name="missing_kb",
                query="U1200 CAN0 connection",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
            )

            self.assertEqual(hits, [])

    def test_typed_delete_removes_design_through_locked_service_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, vector_index=_UnavailableVectorIndex())
            service.index_file(
                kb_name="kb_hw",
                record_id=1,
                file_path=os.path.join(tmp, "board.edf"),
                original_name="board.edf",
                department_id="dept_hw",
            )

            removed = service.delete_design("kb_hw", "board")

        self.assertTrue(removed)
        self.assertIsNone(service.store.load("kb_hw", "board"))

    def test_pin_to_net_query_returns_compact_pin_mapping_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "main_board.edf")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("(edif main_board)")
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=lambda path, progress_callback=None: _Parser(),
            )
            service.index_file(
                kb_name="kb_hw",
                record_id=7,
                file_path=source,
                original_name="main_board.edf",
                department_id="dept_hw",
            )

            hits = service.query(
                kb_name="kb_hw",
                query="U1200 的引脚连接到哪个网络？",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
                top_k=5,
            )

        pin_mapping = next(hit for hit in hits if hit.locator["entity_type"] == "pin_mapping")
        self.assertIn("1 -> CAN0", pin_mapping.content)
        self.assertNotIn("J3.2", pin_mapping.content)

    def test_list_pin_mapping_evidence_limits_results_to_requested_connector_refdes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "main_board.edf")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("(edif main_board)")
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=lambda path, progress_callback=None: _Parser(),
            )
            service.index_file(
                kb_name="kb_hw", record_id=7, file_path=source,
                original_name="main_board.edf", department_id="dept_hw",
            )

            hits = service.list_pin_mapping_evidence(
                "kb_hw", ["main_board.edf"],
                RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
                refdes=["J3"],
            )

        self.assertEqual([hit.locator["entity_id"] for hit in hits], ["J3"])

    def test_plain_component_query_does_not_expand_pin_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "main_board.edf")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("(edif main_board)")
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=lambda path, progress_callback=None: _Parser(),
            )
            service.index_file(
                kb_name="kb_hw",
                record_id=7,
                file_path=source,
                original_name="main_board.edf",
                department_id="dept_hw",
            )

            hits = service.query(
                kb_name="kb_hw",
                query="U1200 是什么器件？",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
                top_k=5,
            )

        self.assertNotIn("pin_mapping", {hit.locator["entity_type"] for hit in hits})

    def test_query_source_filter_limits_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "main_board.edf")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("(edif main_board)")
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=lambda path, progress_callback=None: _Parser(),
            )
            service.index_file(
                kb_name="kb_hw",
                record_id=7,
                file_path=source,
                original_name="main_board.edf",
                department_id="dept_hw",
            )

            hits = service.query(
                kb_name="kb_hw",
                query="CAN0",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
                filters={"source_name": "other.edf"},
            )

        self.assertEqual(hits, [])

    def test_query_excludes_other_department_circuit_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "main_board.edf")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("(edif main_board)")
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=lambda path, progress_callback=None: _Parser(),
            )
            service.index_file(
                kb_name="kb_hw",
                record_id=7,
                file_path=source,
                original_name="main_board.edf",
                department_id="dept_a",
            )

            hits = service.query(
                kb_name="kb_hw",
                query="CAN0",
                ctx=RequestContext(user_id="bob", metadata={"department_id": "dept_b"}),
            )

        self.assertEqual(hits, [])

    def test_query_excludes_missing_department_metadata_when_context_is_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "main_board.edf")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("(edif main_board)")
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=lambda path, progress_callback=None: _Parser(),
            )
            service.index_file(
                kb_name="kb_hw",
                record_id=7,
                file_path=source,
                original_name="main_board.edf",
                department_id="",
            )

            hits = service.query(
                kb_name="kb_hw",
                query="CAN0",
                ctx=RequestContext(user_id="bob", metadata={"department_id": "dept_b"}),
            )

        self.assertEqual(hits, [])

    def test_exact_search_applies_authorized_designs_before_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CircuitStore(root=os.path.join(tmp, "circuits"))
            service = CircuitIndexService(store=store, vector_index=_UnavailableVectorIndex())
            for index in range(6):
                design_id = f"a_disallowed_{index}"
                store.save(CircuitDesign(
                    design_id=design_id,
                    kb_name="kb_hw",
                    instances=[ComponentInstance(refdes="Y900", value="stale")],
                ))
                service._write_metadata("kb_hw", design_id, {
                    "record_id": index + 1,
                    "department_id": "dept_other",
                    "original_name": f"{design_id}.edf",
                })
            store.save(CircuitDesign(
                design_id="z_allowed",
                kb_name="kb_hw",
                instances=[ComponentInstance(refdes="Y900", value="20MHz")],
            ))
            service._write_metadata("kb_hw", "z_allowed", {
                "record_id": 99,
                "department_id": "dept_hw",
                "original_name": "z_allowed.edf",
            })

            hits = service.query(
                kb_name="kb_hw",
                query="Y900",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
                top_k=1,
            )

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].source_name, "z_allowed.edf")
        self.assertEqual(hits[0].score, 0.92)
        self.assertIn("20MHz", hits[0].content)

    def test_topology_search_applies_authorized_designs_before_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CircuitStore(root=os.path.join(tmp, "circuits"))
            service = CircuitIndexService(store=store, vector_index=_UnavailableVectorIndex())
            for index in range(6):
                design_id = f"a_disallowed_{index}"
                store.save(_bias_design(design_id, refdes=f"R{index + 1}", value="1K"))
                service._write_metadata("kb_hw", design_id, {
                    "record_id": index + 1,
                    "department_id": "dept_other",
                    "original_name": f"{design_id}.edf",
                })
            store.save(_bias_design("z_allowed", refdes="R99", value="100K"))
            service._write_metadata("kb_hw", "z_allowed", {
                "record_id": 99,
                "department_id": "dept_hw",
                "original_name": "z_allowed.edf",
            })

            hits = service.query(
                kb_name="kb_hw",
                query="pull up resistor",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
                top_k=1,
            )

        self.assertEqual([hit.locator["entity_id"] for hit in hits], ["pull_up:R99"])
        self.assertEqual(hits[0].source_name, "z_allowed.edf")

    def test_query_maps_engine_connection_and_power_rows_to_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "main_board.edf")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("(edif main_board)")
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=lambda path, progress_callback=None: _Parser(),
                query_engine=_QueryEngine(),
            )
            service.index_file(
                kb_name="kb_hw",
                record_id=7,
                file_path=source,
                original_name="main_board.edf",
                department_id="dept_hw",
            )

            hits = service.query(
                kb_name="kb_hw",
                query="power module connection",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
                top_k=5,
            )

        self.assertEqual(
            [hit.locator["entity_type"] for hit in hits],
            ["module_connection", "module_power"],
        )
        self.assertIn("Power", hits[0].content)
        self.assertIn("VDD", hits[0].content)
        self.assertIn("VDD", hits[1].content)
        self.assertEqual(hits[0].locator["record_id"], 7)

    def test_query_returns_real_module_connection_and_power_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "main_board.edf")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("(edif main_board)")
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=lambda path, progress_callback=None: _Parser(),
            )
            service.index_file(
                kb_name="kb_hw",
                record_id=7,
                file_path=source,
                original_name="main_board.edf",
                department_id="dept_hw",
            )

            hits = service.query(
                kb_name="kb_hw",
                query="Power module connection",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
                top_k=10,
            )

        entity_types = {hit.locator["entity_type"] for hit in hits}
        self.assertIn("module_connection", entity_types)
        self.assertIn("module_power", entity_types)

    def test_power_path_query_returns_direct_conversion_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "power_board.edf")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("(edif power_board)")
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=lambda path, progress_callback=None: _PowerPathParser(),
            )
            service.index_file(
                kb_name="kb_hw",
                record_id=8,
                file_path=source,
                original_name="power_board.edf",
                department_id="dept_hw",
            )

            hits = service.query(
                kb_name="kb_hw",
                query="Ethernet PHY 1.0V power path",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
                top_k=5,
            )

        topology = next(hit for hit in hits if hit.locator["entity_type"] == "power_topology")
        self.assertIn("VCC3V3 -> U1500", topology.content)
        self.assertIn("VCC3V3_ETH", topology.content)
        self.assertIn("U1501", topology.content)
        self.assertIn("VCC1V0_ETH", topology.content)
        self.assertNotIn("Module", topology.content)

    def test_load_switch_query_expands_all_matching_pin_mappings(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "switch_board.edf")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("(edif switch_board)")
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=lambda path, progress_callback=None: _PowerSwitchParser(),
            )
            service.index_file(
                kb_name="kb_hw",
                record_id=9,
                file_path=source,
                original_name="switch_board.edf",
                department_id="dept_hw",
            )

            hits = service.query(
                kb_name="kb_hw",
                query="TPS22918 input output enable",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
                top_k=8,
            )

        mappings = [hit for hit in hits if hit.locator["entity_type"] == "pin_mapping"]
        self.assertEqual({hit.locator["entity_id"] for hit in mappings}, {"U1500", "U1802", "U1803"})
        self.assertTrue(all("VIN" in hit.content and "ON" in hit.content for hit in mappings))

    def test_exact_component_facts_precede_semantic_evidence_without_semantic_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            vector_index = _SemanticVectorIndex()
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=lambda path, progress_callback=None: _EvaluationParser(),
                vector_index=vector_index,
            )
            service.index_file(
                kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "board.edf"),
                original_name="board.edf", department_id="dept_hw",
            )
            ctx = RequestContext(user_id="alice", metadata={"department_id": "dept_hw"})

            for refdes, expected in (("Y900", "20MHz"), ("Y600", "30MHz"), ("R1205", "100K"), ("U1600", "LN10046FSQ1LQR")):
                with self.subTest(refdes=refdes):
                    hits = service.query(kb_name="kb_hw", query=f"{refdes} value", ctx=ctx, top_k=5)
                    direct = next(hit for hit in hits if hit.locator["entity_type"] == "instance" and hit.locator["entity_id"] == refdes)
                    self.assertIn(expected, direct.content)
                    self.assertGreater(direct.score, 0.70)

            self.assertEqual(vector_index.search_calls, [])

    def test_graph_relationship_evidence_has_stable_entity_pin_and_net_locators(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=lambda path, progress_callback=None: _EvaluationParser(),
                vector_index=_UnavailableVectorIndex(),
            )
            service.index_file(
                kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "board.edf"),
                original_name="board.edf", department_id="dept_hw",
            )
            hits = service.query(
                kb_name="kb_hw", query="U1600 enable signal neighbors", top_k=12,
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
            )

        graph_hits = [hit for hit in hits if hit.locator["entity_type"] == "graph_relationship"]
        self.assertTrue(graph_hits)
        self.assertTrue(any("EN_SYNC" in hit.content and "D1611" in hit.content for hit in graph_hits))
        self.assertTrue(all({"entity_id", "pin", "net"}.issubset(hit.locator) for hit in graph_hits))

    def test_semantic_fallback_ranks_below_direct_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            vector_index = _SemanticVectorIndex()
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=lambda path, progress_callback=None: _EvaluationParser(),
                vector_index=vector_index,
            )
            service.index_file(
                kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "board.edf"),
                original_name="board.edf", department_id="dept_hw",
            )
            ctx = RequestContext(user_id="alice", metadata={"department_id": "dept_hw"})
            direct = service.query(kb_name="kb_hw", query="Y900 value", ctx=ctx)[0]
            semantic = service.query(kb_name="kb_hw", query="unmatched conceptual intent", ctx=ctx)[0]

        self.assertGreater(direct.score, semantic.score)
        self.assertEqual(semantic.locator["entity_type"], "semantic_instance")

    def test_exact_stage_does_not_use_query_engine_semantic_supplement(self):
        with tempfile.TemporaryDirectory() as tmp:
            vector_index = _SemanticVectorIndex()
            store = CircuitStore(root=os.path.join(tmp, "circuits"))
            service = CircuitIndexService(
                store=store,
                query_engine=CircuitQueryEngine(store, vector_index=vector_index),
                parser_factory=lambda path, progress_callback=None: _EvaluationParser(),
                vector_index=vector_index,
            )
            service.index_file(kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "board.edf"), original_name="board.edf", department_id="dept_hw")
            hits = service.query(kb_name="kb_hw", query="Y900", ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}))

        self.assertTrue(any(hit.locator["entity_id"] == "Y900" for hit in hits))
        self.assertEqual(vector_index.search_calls, [])

    def test_original_part_number_and_net_questions_expand_graph_relationships(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CircuitIndexService(storage_root=os.path.join(tmp, "circuits"), parser_factory=lambda path, progress_callback=None: _EvaluationParser(), vector_index=_UnavailableVectorIndex())
            service.index_file(kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "board.edf"), original_name="board.edf", department_id="dept_hw")
            ctx = RequestContext(user_id="alice", metadata={"department_id": "dept_hw"})
            by_part = service.query(kb_name="kb_hw", query="LN10046 的使能信号来源有哪些？请逐项列举。", ctx=ctx, top_k=8)
            by_net = service.query(kb_name="kb_hw", query="ECU_EN 连接到哪里？", ctx=ctx, top_k=8)

        for hits in (by_part, by_net):
            graph_hits = [hit for hit in hits if hit.locator["entity_type"] == "graph_relationship"]
            self.assertTrue(graph_hits)
            self.assertTrue(any("D1611" in hit.content for hit in graph_hits))

    def test_graph_lookup_failure_preserves_structured_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CircuitIndexService(storage_root=os.path.join(tmp, "circuits"), parser_factory=lambda path, progress_callback=None: _EvaluationParser(), vector_index=_UnavailableVectorIndex())
            service.index_file(kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "board.edf"), original_name="board.edf", department_id="dept_hw")
            service.graph_store = _ExplodingLookupGraphStore()
            hits = service.query(kb_name="kb_hw", query="U1600 enable", ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}))

        self.assertTrue(any(hit.locator["entity_type"] == "instance" and hit.locator["entity_id"] == "U1600" for hit in hits))

    def test_bias_retrieval_filters_to_requested_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CircuitIndexService(storage_root=os.path.join(tmp, "circuits"), parser_factory=lambda path, progress_callback=None: _EvaluationParser(), vector_index=_UnavailableVectorIndex())
            service.index_file(kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "board.edf"), original_name="board.edf", department_id="dept_hw")
            hits = service.query(kb_name="kb_hw", query="CAN0 RXD 上拉电阻位号和阻值", ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}), top_k=8)

        topology_ids = {hit.locator["entity_id"] for hit in hits if hit.locator["entity_type"] == "topology"}
        self.assertEqual(topology_ids, {"pull_up:R1205"})

    def test_graph_relationship_survives_small_top_k_before_keyword_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CircuitIndexService(storage_root=os.path.join(tmp, "circuits"), parser_factory=lambda path, progress_callback=None: _EvaluationParser(), vector_index=_UnavailableVectorIndex())
            service.index_file(kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "board.edf"), original_name="board.edf", department_id="dept_hw")
            hits = service.query(kb_name="kb_hw", query="U1600 ECU_EN enable", ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}), top_k=2)

        self.assertTrue(any(hit.locator["entity_type"] == "graph_relationship" for hit in hits))

    def test_high_fanout_graph_evidence_has_bounded_count_and_aggregate_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CircuitStore(root=os.path.join(tmp, "circuits"))
            design = CircuitDesign(
                design_id="fanout",
                kb_name="kb_hw",
                instances=[
                    ComponentInstance(refdes=f"U{index}", pins=[Pin(name="1", net="FANOUT")])
                    for index in range(500)
                ],
                nets=[Net("FANOUT", [PinRef(f"U{index}", "1") for index in range(500)])],
            )
            store.save(design)
            design_dir = store.design_dir("kb_hw", "fanout")
            GraphStore().save(design, design_dir)
            service = CircuitIndexService(store=store, vector_index=_UnavailableVectorIndex())
            metadata = {
                "record_id": 1,
                "department_id": "dept_hw",
                "graph_index_status": "indexed",
            }

            hits = service._graph_evidence(
                "kb_hw",
                "U0 connection",
                {"fanout": (metadata, "fanout.edf")},
                top_k=5,
            )

        self.assertLessEqual(len(hits), 5)
        self.assertLessEqual(sum(len(hit.content) for hit in hits), 2048)
        self.assertTrue(any("FANOUT" in hit.content and "U0.1" in hit.content for hit in hits))

    def test_high_fanout_graph_evidence_keeps_late_queried_endpoint_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CircuitStore(root=os.path.join(tmp, "circuits"))
            design = CircuitDesign(
                design_id="fanout",
                kb_name="kb_hw",
                instances=[
                    ComponentInstance(refdes=f"U{index}", pins=[Pin(name="1", net="FANOUT")])
                    for index in range(500)
                ],
                nets=[Net("FANOUT", [PinRef(f"U{index}", "1") for index in range(500)])],
            )
            store.save(design)
            GraphStore().save(design, store.design_dir("kb_hw", "fanout"))
            service = CircuitIndexService(store=store, vector_index=_UnavailableVectorIndex())

            hits = service._graph_evidence(
                "kb_hw",
                "U499 connection",
                {
                    "fanout": ({
                        "record_id": 1,
                        "department_id": "dept_hw",
                        "graph_index_status": "indexed",
                    }, "fanout.edf"),
                },
                top_k=1,
            )

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].locator["entity_id"], "U499")
        self.assertEqual(hits[0].locator["pin"], "1")
        self.assertEqual(hits[0].locator["net"], "FANOUT")
        self.assertIn("U499.1", hits[0].content)

    def test_concurrent_same_design_publication_is_one_coherent_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "circuits")
            first = os.path.join(tmp, "generation-a.edf")
            second = os.path.join(tmp, "generation-b.edf")
            for path, generation in ((first, "A"), (second, "B")):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(generation)
            context = multiprocessing.get_context("fork")
            barrier = context.Barrier(2)
            outcomes = context.Queue()
            processes = [
                context.Process(
                    target=_index_generation,
                    args=(root, path, generation, barrier, outcomes),
                )
                for path, generation in ((first, "A"), (second, "B"))
            ]
            try:
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(10)
                self.assertTrue(all(not process.is_alive() for process in processes))
                self.assertEqual([process.exitcode for process in processes], [0, 0])
                self.assertEqual(sorted(outcomes.get(timeout=2) for _ in processes), ["A:indexed", "B:indexed"])
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(2)

            store = CircuitStore(root=root)
            design = store.load("kb_hw", "same_board")
            design_dir = store.design_dir("kb_hw", "same_board")
            with open(os.path.join(design_dir, "pipeline_metadata.json"), encoding="utf-8") as fh:
                metadata = json.load(fh)
            graph = GraphStore().load(design_dir)
            graph_nodes = GraphStore._iter_nodes(graph)

        self.assertIsNotNone(design)
        generation = metadata["uploaded_by"]
        self.assertIn(generation, {"A", "B"})
        self.assertEqual(design.instances[0].value, generation)
        self.assertIn(f"component:U_{generation}", graph_nodes)
        self.assertEqual(graph_nodes[f"component:U_{generation}"]["value"], generation)

    def test_original_crystal_question_returns_both_frequency_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CircuitIndexService(storage_root=os.path.join(tmp, "circuits"), parser_factory=lambda path, progress_callback=None: _EvaluationParser(), vector_index=_UnavailableVectorIndex())
            service.index_file(kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "board.edf"), original_name="board.edf", department_id="dept_hw")
            hits = service.query(kb_name="kb_hw", query="MCU 和 SOC 使用的晶振频率分别是多少？请给出型号和位号，并判断是否满足手册要求。", ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}), top_k=8)

        facts = {hit.locator["entity_id"]: hit.content for hit in hits if hit.locator["entity_type"] == "instance"}
        self.assertIn("20MHz", facts["Y900"])
        self.assertIn("30MHz", facts["Y600"])

    def test_original_enable_question_traverses_all_diode_source_nets(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CircuitIndexService(storage_root=os.path.join(tmp, "circuits"), parser_factory=lambda path, progress_callback=None: _EvaluationParser(), vector_index=_UnavailableVectorIndex())
            service.index_file(kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "board.edf"), original_name="board.edf", department_id="dept_hw")
            hits = service.query(kb_name="kb_hw", query="LN10046 的使能信号来源有哪些？请逐项列举。", ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}), top_k=20)

        source_nets = {hit.locator.get("net") for hit in hits if hit.locator["entity_type"] == "graph_relationship"} - {"ECU_EN"}
        self.assertEqual(source_nets, {"CAN0_INH", "CAN1_INH", "CAN2_INH", "CAN3_INH", "ETH_INH", "L_S_WKUP"})

    def test_bias_missing_signal_returns_no_unrelated_topology(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CircuitIndexService(storage_root=os.path.join(tmp, "circuits"), parser_factory=lambda path, progress_callback=None: _EvaluationParser(), vector_index=_UnavailableVectorIndex())
            service.index_file(kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "board.edf"), original_name="board.edf", department_id="dept_hw")
            hits = service.query(kb_name="kb_hw", query="MISSING_RXD 上拉电阻位号和阻值", ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}), top_k=8)

        self.assertFalse(any(hit.locator["entity_type"] == "topology" for hit in hits))

    def test_spaced_english_pull_direction_retrieval_ignores_generic_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CircuitIndexService(storage_root=os.path.join(tmp, "circuits"), parser_factory=lambda path, progress_callback=None: _EvaluationParser(), vector_index=_UnavailableVectorIndex())
            service.index_file(kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "board.edf"), original_name="board.edf", department_id="dept_hw")
            ctx = RequestContext(user_id="alice", metadata={"department_id": "dept_hw"})
            pull_up = service.query(kb_name="kb_hw", query="pull up resistor", ctx=ctx, top_k=8)
            pull_down = service.query(kb_name="kb_hw", query="pull down resistor", ctx=ctx, top_k=8)

        self.assertEqual({hit.locator["entity_id"] for hit in pull_up if hit.locator["entity_type"] == "topology"}, {"pull_up:R1205", "pull_up:R1210"})
        self.assertEqual({hit.locator["entity_id"] for hit in pull_down if hit.locator["entity_type"] == "topology"}, {"pull_down:R1211"})

    def test_exact_stage_internal_type_error_never_retries_nonempty_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CircuitStore(root=os.path.join(tmp, "circuits"))
            engine = _InternalTypeErrorQueryEngine(store)
            vector_index = _SemanticVectorIndex()
            service = CircuitIndexService(store=store, query_engine=engine, parser_factory=lambda path, progress_callback=None: _EvaluationParser(), vector_index=vector_index)
            service.index_file(kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "board.edf"), original_name="board.edf", department_id="dept_hw")
            hits = service.query(kb_name="kb_hw", query="Y900", ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}))

        self.assertTrue(any(hit.locator["entity_id"] == "Y900" for hit in hits))
        self.assertEqual(engine.net_queries, [("", ("Y900",))])
        self.assertEqual(vector_index.search_calls, [])

    def test_exact_retriever_with_only_kwargs_is_not_treated_as_filter_aware(self):
        engine = _KwargsIgnoringQueryEngine()
        service = CircuitIndexService(query_engine=engine, vector_index=_UnavailableVectorIndex())

        rows = service._exact_search(
            "search_instances",
            "kb_hw",
            "Y900",
            1,
            frozenset({"z_allowed"}),
        )

        self.assertEqual(rows, [])
        self.assertEqual(engine.calls, [])

    def test_topology_retriever_with_only_kwargs_is_not_treated_as_filter_aware(self):
        engine = _KwargsIgnoringTopologyQueryEngine()
        service = CircuitIndexService(query_engine=engine, vector_index=_UnavailableVectorIndex())

        service._structured_evidence(
            "kb_hw",
            "pull up resistor",
            {"z_allowed": ({"record_id": 9, "department_id": "dept_hw"}, "allowed.edf")},
            1,
        )

        self.assertEqual(engine.topology_calls, [])

    def test_semantic_retriever_with_only_kwargs_is_not_treated_as_filter_aware(self):
        vector_index = _KwargsIgnoringSemanticIndex()
        service = CircuitIndexService(vector_index=vector_index)

        hits = service._semantic_evidence(
            "kb_hw",
            "conceptual",
            {"z_allowed": ({"record_id": 9, "department_id": "dept_hw"}, "allowed.edf")},
            1,
        )

        self.assertEqual(hits, [])
        self.assertEqual(vector_index.calls, [])

    def test_failed_vector_replacement_never_returns_stale_previous_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "generation-a.edf")
            second = os.path.join(tmp, "generation-b.edf")
            for path, generation in ((first, "A"), (second, "B")):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(generation)
            vector_index = _StaleGenerationVectorIndex()
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=_FileGenerationParserFactory(),
                graph_store=GraphStore(),
                vector_index=vector_index,
            )
            service.index_file(
                kb_name="kb_hw",
                record_id=1,
                file_path=first,
                original_name="same_board.edf",
                department_id="dept_hw",
            )
            result = service.index_file(
                kb_name="kb_hw",
                record_id=2,
                file_path=second,
                original_name="same_board.edf",
                department_id="dept_hw",
            )
            vector_index.search_calls.clear()

            hits = service.query(
                kb_name="kb_hw",
                query="unmatched conceptual intent",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
            )

        self.assertEqual(result.status, "degraded")
        self.assertEqual(hits, [])
        self.assertEqual(vector_index.search_calls, [])


class _Parser:
    warnings = ["parser warning"]

    def parse(self):
        return (
            [
                ComponentInstance(refdes="U1200", library_cell="CAN_PHY", pins=[Pin(name="1", net="CAN0")]),
                ComponentInstance(refdes="J3", library_cell="CONNECTOR", pins=[Pin(name="2", net="CAN0")]),
                ComponentInstance(refdes="U1", library_cell="REGULATOR", pins=[Pin(name="OUT", net="VDD"), Pin(name="GND", net="GND")]),
                ComponentInstance(refdes="U2", library_cell="MCU", pins=[Pin(name="VDD", net="VDD"), Pin(name="GND", net="GND")]),
            ],
            [
                Net(name="CAN0", connections=[PinRef(refdes="U1200", pin="1"), PinRef(refdes="J3", pin="2")]),
                Net(name="VDD", connections=[PinRef(refdes="U1", pin="OUT"), PinRef(refdes="U2", pin="VDD")], net_type="power"),
                Net(name="GND", connections=[PinRef(refdes="U1", pin="GND"), PinRef(refdes="U2", pin="GND")], net_type="ground"),
            ],
            [
                CircuitModule(module_id="power", name="Power", strategy="fixture", instances=["U1"], nets=["VDD", "GND"]),
                CircuitModule(module_id="mcu", name="MCU", strategy="fixture", instances=["U2"], nets=["VDD", "GND"]),
            ],
        )


class _RecordingStore(CircuitStore):
    def __init__(self, root, calls):
        super().__init__(root=root)
        self.calls = calls

    def save(self, design):
        self.calls.append(("structured", design))
        return super().save(design)


class _GraphStore:
    def __init__(self, calls=None):
        self.calls = calls

    def save(self, design, design_dir):
        path = os.path.join(design_dir, "connectivity_graph.gpickle")
        with open(path, "wb") as fh:
            fh.write(b"graph")
        if self.calls is not None:
            self.calls.append(("graph", design, design_dir, path))
        return GraphIndexResult(path=path, node_count=12, edge_count=14)


class _FailingGraphStore:
    def save(self, design, design_dir):
        raise RuntimeError("private payload")


class _ExplodingLookupGraphStore:
    def load(self, design_dir):
        return {"nodes": {}, "edges": []}

    def connected_entities(self, graph, **kwargs):
        raise RuntimeError("graph lookup failed")


class _VectorIndex:
    def __init__(self, calls=None):
        self.calls = calls

    def reindex_design_with_status(self, design):
        if self.calls is not None:
            self.calls.append(("vector", design))
        return CircuitVectorIndexStatus(available=True, indexed_count=9)


class _UnavailableVectorIndex:
    def reindex_design_with_status(self, design):
        return CircuitVectorIndexStatus(available=False, indexed_count=0)


class _FailingVectorIndex:
    def reindex_design_with_status(self, design):
        return CircuitVectorIndexStatus(available=True, indexed_count=0, error="private payload")


class _ToggleParserFactory:
    def __init__(self):
        self.raise_error = False

    def __call__(self, path, progress_callback=None):
        if self.raise_error:
            return _RaisingParser()
        return _Parser()


class _RaisingParser:
    def parse(self):
        raise ValueError("invalid EDF")


def _bias_design(design_id: str, *, refdes: str, value: str) -> CircuitDesign:
    return CircuitDesign(
        design_id=design_id,
        kb_name="kb_hw",
        instances=[ComponentInstance(
            refdes=refdes,
            library_cell="RES",
            value=value,
            pins=[Pin(name="1", net="SIGNAL_RXD"), Pin(name="2", net="VCC3V3")],
        )],
        nets=[
            Net("SIGNAL_RXD", [PinRef(refdes, "1")]),
            Net("VCC3V3", [PinRef(refdes, "2")], net_type="power"),
        ],
    )


class _ConcurrentParser:
    def __init__(self, path, barrier):
        self.path = path
        self.barrier = barrier

    def parse(self):
        with open(self.path, encoding="utf-8") as fh:
            generation = fh.read().strip()
        self.barrier.wait(timeout=5)
        if generation == "B":
            time.sleep(0.1)
        refdes = f"U_{generation}"
        return (
            [ComponentInstance(refdes=refdes, value=generation, pins=[Pin("1", f"NET_{generation}")])],
            [Net(f"NET_{generation}", [PinRef(refdes, "1")])],
            [],
        )


class _ConcurrentParserFactory:
    def __init__(self, barrier):
        self.barrier = barrier

    def __call__(self, path, progress_callback=None):
        return _ConcurrentParser(path, self.barrier)


class _DelayedGenerationGraphStore(GraphStore):
    def save(self, design, design_dir):
        if design.instances[0].value == "A":
            time.sleep(0.4)
        return super().save(design, design_dir)


class _FileGenerationParser:
    def __init__(self, path):
        self.path = path

    def parse(self):
        with open(self.path, encoding="utf-8") as fh:
            generation = fh.read().strip()
        refdes = "U100" if generation == "A" else "U200"
        return (
            [ComponentInstance(refdes=refdes, value=generation, pins=[Pin(name="1", net="FANOUT")])],
            [Net(name="FANOUT", connections=[PinRef(refdes=refdes, pin="1")])],
            [],
        )


class _FileGenerationParserFactory:
    def __call__(self, path, progress_callback=None):
        return _FileGenerationParser(path)


class _BlockingGenerationGraphStore(GraphStore):
    def __init__(self):
        self.block_generation = ""
        self.writer_blocked = threading.Event()
        self.release_writer = threading.Event()

    def save(self, design, design_dir):
        if design.instances[0].value == self.block_generation:
            self.writer_blocked.set()
            if not self.release_writer.wait(5):
                raise TimeoutError("writer release timed out")
        return super().save(design, design_dir)


class _UnremovableFailingGraphStore(GraphStore):
    def __init__(self):
        self.fail = False

    def save(self, design, design_dir):
        if self.fail:
            raise OSError("graph write failed")
        return super().save(design, design_dir)

    def remove(self, design_dir):
        if self.fail:
            raise OSError("graph remove failed")
        return super().remove(design_dir)


def _index_generation(root, path, generation, barrier, outcomes):
    service = CircuitIndexService(
        storage_root=root,
        parser_factory=_ConcurrentParserFactory(barrier),
        graph_store=_DelayedGenerationGraphStore(),
        vector_index=_UnavailableVectorIndex(),
    )
    result = service.index_file(
        kb_name="kb_hw",
        record_id=1 if generation == "A" else 2,
        file_path=path,
        original_name="same_board.edf",
        department_id="dept_hw",
        uploaded_by=generation,
    )
    outcomes.put(f"{generation}:{result.status}")


def _service(tmp, *, graph_store=None, vector_index=None):
    return CircuitIndexService(
        storage_root=os.path.join(tmp, "circuits"),
        parser_factory=lambda path, progress_callback=None: _Parser(),
        graph_store=graph_store,
        vector_index=vector_index,
    )


def _file_hash(path):
    with open(path, "rb") as fh:
        return sha256(fh.read()).hexdigest()


class _PowerPathParser:
    def parse(self):
        return (
            [
                ComponentInstance(
                    refdes="U1500",
                    library_cell="TPS22918",
                    pins=[
                        Pin(name="VIN", net="VCC3V3"),
                        Pin(name="VOUT", net="VCC3V3_ETH"),
                        Pin(name="ON", net="MCU_3V3_ETH_EN"),
                    ],
                ),
                ComponentInstance(
                    refdes="U1501",
                    library_cell="TPS74501",
                    pins=[Pin(name="VIN", net="VCC3V3_ETH"), Pin(name="OUT", net="VCC1V0_ETH")],
                ),
            ],
            [
                Net(name="VCC3V3", connections=[PinRef(refdes="U1500", pin="VIN")], net_type="power"),
                Net(
                    name="VCC3V3_ETH",
                    connections=[PinRef(refdes="U1500", pin="VOUT"), PinRef(refdes="U1501", pin="VIN")],
                    net_type="power",
                ),
                Net(name="VCC1V0_ETH", connections=[PinRef(refdes="U1501", pin="OUT")], net_type="power"),
                Net(name="MCU_3V3_ETH_EN", connections=[PinRef(refdes="U1500", pin="ON")]),
            ],
            [],
        )


class _PowerSwitchParser:
    def parse(self):
        instances = [
            ComponentInstance(
                refdes="U1500",
                library_cell="TPS22918",
                pins=[Pin(name="VIN", net="VCC3V3"), Pin(name="VOUT", net="VCC3V3_ETH"), Pin(name="ON", net="MCU_3V3_ETH_EN")],
            ),
            ComponentInstance(
                refdes="U1802",
                library_cell="TPS22918",
                pins=[Pin(name="VIN", net="VCC3V3"), Pin(name="VOUT", net="VCC3V3_EQ"), Pin(name="ON", net="MCU_3V3_EQ_EN")],
            ),
            ComponentInstance(
                refdes="U1803",
                library_cell="TPS22918",
                pins=[Pin(name="VIN", net="VCC1V8"), Pin(name="VOUT", net="VCC1V8_EQ"), Pin(name="ON", net="MCU_1V8_EQ_EN")],
            ),
        ]
        nets = [
            Net(name=pin.net, connections=[PinRef(refdes=instance.refdes, pin=pin.name)], net_type="power" if pin.name != "ON" else "signal")
            for instance in instances
            for pin in instance.pins
        ]
        return instances, nets, []


class _EvaluationParser:
    def parse(self):
        return (
            [
                ComponentInstance(refdes="Y900", library_cell="CRYSTAL", value="20MHz"),
                ComponentInstance(refdes="Y600", library_cell="CRYSTAL", value="30MHz"),
                ComponentInstance(refdes="R1205", library_cell="RES", value="100K", pins=[Pin(name="1", net="CAN0_RXD"), Pin(name="2", net="VCC3V3")]),
                ComponentInstance(refdes="R1210", library_cell="RES", value="10K", pins=[Pin(name="1", net="LIN_RXD"), Pin(name="2", net="VCC3V3")]),
                ComponentInstance(refdes="U1600", library_cell="LN10046", part_number="LN10046FSQ1LQR", pins=[Pin(name="EN_SYNC", net="ECU_EN"), Pin(name="VIN", net="VCC3V3"), Pin(name="GND", net="GND"), Pin(name="FB", net="FB_NODE"), Pin(name="SW", net="SW_NODE"), Pin(name="TIME", net="TIME_CAP")]),
                ComponentInstance(refdes="D1611", library_cell="DIODE", pins=[Pin(name="K", net="ECU_EN"), Pin(name="A", net="CAN0_INH")]),
                ComponentInstance(refdes="D1612", library_cell="DIODE", pins=[Pin(name="K", net="ECU_EN"), Pin(name="A", net="CAN1_INH")]),
                ComponentInstance(refdes="D1613", library_cell="DIODE", pins=[Pin(name="K", net="ECU_EN"), Pin(name="A", net="CAN2_INH")]),
                ComponentInstance(refdes="D1614", library_cell="DIODE", pins=[Pin(name="K", net="ECU_EN"), Pin(name="A", net="CAN3_INH")]),
                ComponentInstance(refdes="D1615", library_cell="DIODE", pins=[Pin(name="K", net="ECU_EN"), Pin(name="A", net="ETH_INH")]),
                ComponentInstance(refdes="D1608", library_cell="DIODE", pins=[Pin(name="K", net="ECU_EN"), Pin(name="A", net="L_S_WKUP")]),
                ComponentInstance(refdes="X1", library_cell="TEST", pins=[Pin(name="IN", net="CAN0_INH"), Pin(name="OUT", net="UNRELATED")]),
                ComponentInstance(refdes="R1211", library_cell="RES", value="10K", pins=[Pin(name="1", net="LIN_PULLDOWN"), Pin(name="2", net="GND")]),
            ],
            [
                Net(name="ECU_EN", connections=[PinRef(refdes="U1600", pin="EN_SYNC"), *[PinRef(refdes=refdes, pin="K") for refdes in ("D1608", "D1611", "D1612", "D1613", "D1614", "D1615")]]),
                *[Net(name=net, connections=[PinRef(refdes=refdes, pin="A")]) for refdes, net in (("D1611", "CAN0_INH"), ("D1612", "CAN1_INH"), ("D1613", "CAN2_INH"), ("D1614", "CAN3_INH"), ("D1615", "ETH_INH"), ("D1608", "L_S_WKUP"))],
                Net(name="CAN0_INH", connections=[PinRef(refdes="D1611", pin="A"), PinRef(refdes="X1", pin="IN")]),
                Net(name="UNRELATED", connections=[PinRef(refdes="X1", pin="OUT")]),
                Net(name="CAN0_RXD", connections=[PinRef(refdes="R1205", pin="1")]),
                Net(name="LIN_RXD", connections=[PinRef(refdes="R1210", pin="1")]),
                Net(name="VCC3V3", connections=[PinRef(refdes="R1205", pin="2")], net_type="power"),
                Net(name="GND", connections=[PinRef(refdes="U1600", pin="GND"), PinRef(refdes="R1211", pin="2")], net_type="ground"),
                Net(name="FB_NODE", connections=[PinRef(refdes="U1600", pin="FB")]),
                Net(name="SW_NODE", connections=[PinRef(refdes="U1600", pin="SW")]),
                Net(name="TIME_CAP", connections=[PinRef(refdes="U1600", pin="TIME")]),
                Net(name="LIN_PULLDOWN", connections=[PinRef(refdes="R1211", pin="1")]),
            ],
            [],
        )


class _SemanticVectorIndex(_VectorIndex):
    def __init__(self):
        super().__init__()
        self.search_calls = []

    def semantic_search(
        self,
        kb_name,
        query,
        top_k=20,
        kinds=None,
        allowed_design_ids=None,
        allowed_generations=None,
    ):
        self.search_calls.append((kb_name, query, top_k, tuple(kinds or ())))
        generation_id = str((allowed_generations or {}).get("board") or "")
        return [
            CircuitVectorHit(
                kind="instance", design_id="board", natural_id="Y900", score=0.42,
                metadata={
                    "kind": "instance",
                    "design_id": "board",
                    "natural_id": "Y900",
                    "generation_id": generation_id,
                },
                document="semantic oscillator candidate",
            )
        ]

    def is_available(self):
        return True


class _StaleGenerationVectorIndex:
    def __init__(self):
        self.current_hit = None
        self.search_calls = []

    def reindex_design_with_status(self, design):
        if design.instances[0].value == "B":
            return CircuitVectorIndexStatus(available=True, indexed_count=0, error="delete failed")
        self.current_hit = CircuitVectorHit(
            kind="instance",
            design_id=design.design_id,
            natural_id=design.instances[0].refdes,
            score=0.6,
            metadata={"kind": "instance", "design_id": design.design_id},
            document="stale generation A semantic evidence",
        )
        return CircuitVectorIndexStatus(available=True, indexed_count=1)

    def semantic_search(
        self,
        kb_name,
        query,
        top_k=20,
        kinds=None,
        allowed_design_ids=None,
        allowed_generations=None,
    ):
        self.search_calls.append((kb_name, query, allowed_design_ids, allowed_generations))
        return [self.current_hit] if self.current_hit is not None else []


class _InternalTypeErrorQueryEngine(CircuitQueryEngine):
    def __init__(self, store):
        super().__init__(store)
        self.net_queries = []

    def search_net_connections(self, kb_name, query="", keywords=None, limit=10, allowed_design_ids=None):
        self.net_queries.append((query, tuple(keywords or ())))
        raise TypeError("internal query-engine type error")


class _KwargsIgnoringQueryEngine:
    def __init__(self):
        self.calls = []

    def search_instances(self, kb_name, query="", keywords=None, limit=20, **kwargs):
        self.calls.append((kb_name, query, keywords, limit, kwargs))
        return [{"design_id": "a_disallowed", "refdes": "Y900"}]


class _KwargsIgnoringTopologyQueryEngine:
    def __init__(self):
        self.topology_calls = []

    def search_net_connections(self, kb_name, query="", limit=20, allowed_design_ids=None):
        return []

    def search_instances(self, kb_name, query="", limit=20, allowed_design_ids=None):
        return []

    def search_modules(self, kb_name, query="", limit=20, allowed_design_ids=None):
        return []

    def search_module_connections(self, kb_name, query="", limit=20, allowed_design_ids=None):
        return []

    def search_module_power_nets(self, kb_name, query="", limit=20, allowed_design_ids=None):
        return []

    def search_bias_topologies(self, kb_name, limit=20, **kwargs):
        self.topology_calls.append((kb_name, limit, kwargs))
        return [{
            "design_id": "a_disallowed",
            "topology": "pull_up",
            "refdes": "R1",
            "signal_net": "SDA",
            "rail_net": "VDD",
        }]


class _KwargsIgnoringSemanticIndex:
    def __init__(self):
        self.calls = []

    def semantic_search(self, kb_name, query, top_k=20, kinds=None, **kwargs):
        self.calls.append((kb_name, query, top_k, kinds, kwargs))
        return [CircuitVectorHit(
            kind="instance",
            design_id="a_disallowed",
            natural_id="U1",
            score=0.9,
            metadata={"kind": "instance", "design_id": "a_disallowed"},
            document="unauthorized semantic evidence",
        )]


class _QueryEngine:
    def search_net_connections(self, kb_name, query, limit, allowed_design_ids=None):
        return []

    def search_instances(self, kb_name, query, limit, allowed_design_ids=None):
        return []

    def search_modules(self, kb_name, query, limit, allowed_design_ids=None):
        return []

    def search_module_connections(self, kb_name, query, limit, allowed_design_ids=None):
        return [
            {
                "design_id": "main_board",
                "from_module": "Power",
                "to_module": "MCU",
                "net": "VDD",
                "net_type": "power",
                "connection_count": 2,
                "connections": ["U1.1", "U2.2"],
            }
        ]

    def search_module_power_nets(self, kb_name, query, limit, allowed_design_ids=None):
        return [
            {
                "design_id": "main_board",
                "module_id": "power",
                "name": "Power",
                "power_nets": [{"name": "VDD", "role": "power", "connections": ["U1.1", "U2.2"]}],
                "ground_nets": [{"name": "GND", "role": "ground", "connections": ["U1.2", "U2.1"]}],
            }
        ]


if __name__ == "__main__":
    unittest.main()
