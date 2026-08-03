import os
import tempfile
import unittest
from hashlib import sha256

from src.agents.state import Evidence
from src.circuit.graph_store import GraphIndexResult, GraphStore
from src.circuit.index_service import CircuitIndexService
from src.circuit.models import CircuitModule, ComponentInstance, Net, Pin, PinRef
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

        source_nets = {hit.locator.get("net") for hit in hits if hit.locator["entity_type"] == "graph_relationship"}
        self.assertTrue({"CAN0_INH", "CAN1_INH", "CAN2_INH", "CAN3_INH", "ETH_INH", "L_S_WKUP"}.issubset(source_nets))
        self.assertNotIn("UNRELATED", source_nets)

    def test_bias_missing_signal_returns_no_unrelated_topology(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CircuitIndexService(storage_root=os.path.join(tmp, "circuits"), parser_factory=lambda path, progress_callback=None: _EvaluationParser(), vector_index=_UnavailableVectorIndex())
            service.index_file(kb_name="kb_hw", record_id=7, file_path=os.path.join(tmp, "board.edf"), original_name="board.edf", department_id="dept_hw")
            hits = service.query(kb_name="kb_hw", query="MISSING_RXD 上拉电阻位号和阻值", ctx=RequestContext(user_id="alice", metadata={"department_id": "dept_hw"}), top_k=8)

        self.assertFalse(any(hit.locator["entity_type"] == "topology" for hit in hits))

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
                ComponentInstance(refdes="U1600", library_cell="LN10046", part_number="LN10046FSQ1LQR", pins=[Pin(name="EN_SYNC", net="ECU_EN")]),
                ComponentInstance(refdes="D1611", library_cell="DIODE", pins=[Pin(name="K", net="ECU_EN"), Pin(name="A", net="CAN0_INH")]),
                ComponentInstance(refdes="D1612", library_cell="DIODE", pins=[Pin(name="K", net="ECU_EN"), Pin(name="A", net="CAN1_INH")]),
                ComponentInstance(refdes="D1613", library_cell="DIODE", pins=[Pin(name="K", net="ECU_EN"), Pin(name="A", net="CAN2_INH")]),
                ComponentInstance(refdes="D1614", library_cell="DIODE", pins=[Pin(name="K", net="ECU_EN"), Pin(name="A", net="CAN3_INH")]),
                ComponentInstance(refdes="D1615", library_cell="DIODE", pins=[Pin(name="K", net="ECU_EN"), Pin(name="A", net="ETH_INH")]),
                ComponentInstance(refdes="D1608", library_cell="DIODE", pins=[Pin(name="K", net="ECU_EN"), Pin(name="A", net="L_S_WKUP")]),
                ComponentInstance(refdes="X1", library_cell="TEST", pins=[Pin(name="IN", net="CAN0_INH"), Pin(name="OUT", net="UNRELATED")]),
            ],
            [
                Net(name="ECU_EN", connections=[PinRef(refdes="U1600", pin="EN_SYNC"), *[PinRef(refdes=refdes, pin="K") for refdes in ("D1608", "D1611", "D1612", "D1613", "D1614", "D1615")]]),
                *[Net(name=net, connections=[PinRef(refdes=refdes, pin="A")]) for refdes, net in (("D1611", "CAN0_INH"), ("D1612", "CAN1_INH"), ("D1613", "CAN2_INH"), ("D1614", "CAN3_INH"), ("D1615", "ETH_INH"), ("D1608", "L_S_WKUP"))],
                Net(name="CAN0_INH", connections=[PinRef(refdes="D1611", pin="A"), PinRef(refdes="X1", pin="IN")]),
                Net(name="UNRELATED", connections=[PinRef(refdes="X1", pin="OUT")]),
                Net(name="CAN0_RXD", connections=[PinRef(refdes="R1205", pin="1")]),
                Net(name="LIN_RXD", connections=[PinRef(refdes="R1210", pin="1")]),
                Net(name="VCC3V3", connections=[PinRef(refdes="R1205", pin="2")], net_type="power"),
            ],
            [],
        )


class _SemanticVectorIndex(_VectorIndex):
    def __init__(self):
        super().__init__()
        self.search_calls = []

    def semantic_search(self, kb_name, query, top_k=20, kinds=None):
        self.search_calls.append((kb_name, query, top_k, tuple(kinds or ())))
        return [
            CircuitVectorHit(
                kind="instance", design_id="board", natural_id="Y900", score=0.42,
                metadata={"kind": "instance", "design_id": "board", "natural_id": "Y900"},
                document="semantic oscillator candidate",
            )
        ]

    def is_available(self):
        return True


class _InternalTypeErrorQueryEngine(CircuitQueryEngine):
    def __init__(self, store):
        super().__init__(store)
        self.net_queries = []

    def search_net_connections(self, kb_name, query="", keywords=None, limit=10):
        self.net_queries.append((query, tuple(keywords or ())))
        raise TypeError("internal query-engine type error")


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
