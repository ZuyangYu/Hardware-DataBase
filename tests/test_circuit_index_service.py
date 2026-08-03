import os
import tempfile
import unittest
from hashlib import sha256

from src.agents.state import Evidence
from src.circuit.graph_store import GraphIndexResult, GraphStore
from src.circuit.index_service import CircuitIndexService
from src.circuit.models import CircuitModule, ComponentInstance, Net, Pin, PinRef
from src.circuit.store import CircuitStore
from src.circuit.vector_index import CircuitVectorIndexStatus
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

    def test_query_returns_empty_for_kb_without_indexed_circuits(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CircuitIndexService(storage_root=os.path.join(tmp, "circuits"))

            hits = service.query(
                kb_name="missing_kb",
                query="U1200 CAN0 connection",
                ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}),
            )

        self.assertEqual(hits, [])

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
                query="Power MCU connection",
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


class _QueryEngine:
    def search_net_connections(self, kb_name, query, limit):
        return []

    def search_instances(self, kb_name, query, limit):
        return []

    def search_modules(self, kb_name, query, limit):
        return []

    def search_module_connections(self, kb_name, query, limit):
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

    def search_module_power_nets(self, kb_name, query, limit):
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
