"""Task 3: structure overview and resolved role read-model over circuit indexes."""

import os
import tempfile
import unittest

from src.circuit.index_service import CircuitIndexService
from src.circuit.models import CircuitModule, ComponentInstance, Net, Pin, PinRef
from src.pipelines.document_rag.schemas import RequestContext


class _UnavailableVectorIndex:
    available = False

    def reindex_design_with_status(self, design):
        from src.circuit.vector_index import CircuitVectorIndexStatus

        return CircuitVectorIndexStatus(available=False, indexed_count=0)


class _RoleDesignParser:
    """Two-role design: U900D (SoC) + U800M (MCU), plus plain U100."""

    warnings: list[str] = []

    def parse(self):
        instances = [
            ComponentInstance(
                refdes="U900D",
                library_cell="ARM_SOC",
                pins=[Pin(name="VDD", net="VDD_3V3"), Pin(name="PA0", net="CAN0_TX")],
                properties={"Device Role": "SoC"},
            ),
            ComponentInstance(
                refdes="U800M",
                library_cell="CORTEX_MCU",
                pins=[Pin(name="VDD", net="VDD_3V3")],
                properties={"Device Role": "MCU"},
            ),
            ComponentInstance(refdes="U100", library_cell="RES", pins=[Pin(name="1", net="VDD_3V3")]),
        ]
        nets = [
            Net(
                name="VDD_3V3",
                net_type="power",
                connections=[
                    PinRef(refdes="U900D", pin="VDD"),
                    PinRef(refdes="U800M", pin="VDD"),
                    PinRef(refdes="U100", pin="1"),
                ],
            ),
            Net(
                name="CAN0_TX",
                connections=[PinRef(refdes="U900D", pin="PA0")],
            ),
        ]
        modules = [
            CircuitModule(module_id="m_soc", name="SOC_CORE", strategy="refdes_page", instances=["U900D"], nets=["CAN0_TX"]),
            CircuitModule(module_id="m_mcu", name="MCU_CORE", strategy="refdes_page", instances=["U800M"], nets=["VDD_3V3"]),
        ]
        return instances, nets, modules


class _PlainParser:
    """No role assertions at all; module names still contain MCU/SoC labels."""

    warnings: list[str] = []

    def parse(self):
        instances = [
            ComponentInstance(refdes="U1", library_cell="TC397XE", pins=[Pin(name="VDD", net="VDD")]),
            ComponentInstance(refdes="R1", library_cell="RES", value="10k", pins=[Pin(name="1", net="VDD")]),
        ]
        nets = [Net(name="VDD", net_type="power", connections=[PinRef(refdes="U1", pin="VDD"), PinRef(refdes="R1", pin="1")])]
        modules = [CircuitModule(module_id="m_mcu", name="MCU_POWER_DOMAIN", strategy="refdes_page", instances=["U1"], nets=["VDD"])]
        return instances, nets, modules


def _service(tmp, parser):
    return CircuitIndexService(
        storage_root=os.path.join(tmp, "circuits"),
        parser_factory=lambda path, progress_callback=None: parser,
        vector_index=_UnavailableVectorIndex(),
    )


def _index(service, tmp, name, record_id, department_id="dept_hw"):
    source = os.path.join(tmp, name)
    with open(source, "w", encoding="utf-8") as fh:
        fh.write("(edif board)")
    service.index_file(
        kb_name="kb_hw",
        record_id=record_id,
        file_path=source,
        original_name=name,
        department_id=department_id,
    )
    return RequestContext(user_id="alice", metadata={"department_id": department_id})


class StructureQueryTests(unittest.TestCase):
    def test_structure_questions_return_overview_and_module_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, _RoleDesignParser())
            ctx = _index(service, tmp, "board.edf", 7)

            overview_hits = service.query(kb_name="kb_hw", query="原理图的结构信息是什么", ctx=ctx, top_k=5)
            profile_hits = service.query(kb_name="kb_hw", query="查看设计概况", ctx=ctx, top_k=5)
            module_hits = service.query(kb_name="kb_hw", query="列出所有模块", ctx=ctx, top_k=5)

            for hits in (overview_hits, profile_hits):
                self.assertTrue(hits)
                kinds = {hit.locator["entity_type"] for hit in hits}
                self.assertIn("circuit_overview", kinds)
            self.assertTrue(module_hits)
            self.assertIn("module_list", {hit.locator["entity_type"] for hit in module_hits})
            joined = " ".join(hit.content for hit in module_hits)
            self.assertIn("SOC_CORE", joined)
            self.assertIn("refdes_page", joined)

    def test_overview_reports_counts_strategy_and_data_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, _RoleDesignParser())
            ctx = _index(service, tmp, "board.edf", 7)

            hits = service.query(kb_name="kb_hw", query="设计结构信息", ctx=ctx, top_k=5)

            overview = next(hit for hit in hits if hit.locator["entity_type"] == "circuit_overview")
            content = overview.content
            self.assertIn("3", content)  # instance count
            self.assertIn("2", content)  # net/module count
            self.assertIn("refdes_page", content)
            self.assertIn("启发式", content)
            # Explicit data-boundary statements instead of silent zero hits.
            self.assertIn("不具备", content)
            coverage = overview.metadata.get("coverage") or {}
            self.assertEqual(coverage.get("schematic_pages"), "unavailable")
            self.assertEqual(coverage.get("coordinates"), "unavailable")
            self.assertEqual(coverage.get("title_block"), "unavailable")

    def test_multiple_designs_are_grouped_without_cross_design_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=lambda path, progress_callback=None: (
                    _RoleDesignParser() if path.endswith("a_board.edf") else _PlainParser()
                ),
                vector_index=_UnavailableVectorIndex(),
            )

            for name in ("a_board.edf", "b_board.edf"):
                source = os.path.join(tmp, name)
                with open(source, "w", encoding="utf-8"):
                    pass
                service.index_file(
                    kb_name="kb_hw",
                    record_id=len(name),
                    file_path=source,
                    original_name=name,
                    department_id="dept_hw",
                )
            ctx = RequestContext(user_id="alice", metadata={"department_id": "dept_hw"})

            hits = service.query(kb_name="kb_hw", query="原理图的结构信息是什么", ctx=ctx, top_k=10)

            overviews = [hit for hit in hits if hit.locator["entity_type"] == "circuit_overview"]
            self.assertEqual(len({hit.locator["circuit_id"] for hit in overviews}), 2)


class ResolvedRoleQueryTests(unittest.TestCase):
    def test_unique_role_returns_identity_evidence_then_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, _RoleDesignParser())
            ctx = _index(service, tmp, "board.edf", 7)

            hits = service.query(kb_name="kb_hw", query="SoC 的连接关系", ctx=ctx, top_k=8)

            kinds = [hit.locator["entity_type"] for hit in hits]
            self.assertEqual(kinds[0], "component_identity")
            identity_hit = hits[0]
            self.assertEqual(identity_hit.locator["refdes"], "U900D")
            self.assertEqual(identity_hit.metadata["resolution_status"], "unique")
            self.assertEqual(identity_hit.metadata["source_kind"], "edf_property")
            connection_kinds = {"pin_mapping", "graph_relationship"} & set(kinds)
            self.assertTrue(connection_kinds)
            for hit in hits[1:]:
                self.assertEqual(hit.locator["circuit_id"], identity_hit.locator["circuit_id"])

    def test_multi_candidate_role_query_returns_candidates_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, _RoleDesignParser())
            ctx = _index(service, tmp, "board.edf", 7)

            # Both SoC and MCU map into controller_family → two candidates.
            hits = service.query(kb_name="kb_hw", query="主控的连接关系", ctx=ctx, top_k=8)

            self.assertTrue(hits)
            kinds = {hit.locator["entity_type"] for hit in hits}
            self.assertNotIn("pin_mapping", kinds)
            self.assertNotIn("net", kinds)
            self.assertNotIn("graph_relationship", kinds)
            status_hit = next(
                hit for hit in hits if hit.metadata.get("resolution_status") == "ambiguous"
            )
            self.assertIn("U900D", status_hit.content)
            self.assertIn("U800M", status_hit.content)

    def test_no_role_candidates_yield_no_evidence_status_not_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, _PlainParser())
            ctx = _index(service, tmp, "board.edf", 7)

            hits = service.query(kb_name="kb_hw", query="SoC 的连接关系", ctx=ctx, top_k=8)

            self.assertTrue(hits)
            status_hits = [hit for hit in hits if hit.metadata.get("resolution_status") == "no_evidence"]
            self.assertTrue(status_hits)
            joined = " ".join(hit.content for hit in status_hits)
            self.assertNotIn("U1 是 SoC", joined)
            self.assertNotIn("U1 is the SoC", joined)

    def test_top_k_does_not_downgrade_ambiguous_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, _RoleDesignParser())
            ctx = _index(service, tmp, "board.edf", 7)

            hits = service.query(kb_name="kb_hw", query="主控的连接关系", ctx=ctx, top_k=1)

            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].metadata["resolution_status"], "ambiguous")
            self.assertGreaterEqual(hits[0].metadata["candidate_count"], 2)

    def test_department_and_record_filters_apply_to_role_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CircuitIndexService(
                storage_root=os.path.join(tmp, "circuits"),
                parser_factory=lambda path, progress_callback=None: _RoleDesignParser(),
                vector_index=_UnavailableVectorIndex(),
            )
            ctx_a = _index(service, tmp, "a_board.edf", 11, department_id="dept_a")
            ctx_b = _index(service, tmp, "b_board.edf", 22, department_id="dept_b")

            hits_b = service.query(kb_name="kb_hw", query="SoC 的连接关系", ctx=ctx_b, top_k=8)
            self.assertTrue(hits_b)
            record_ids = {hit.locator.get("record_id") for hit in hits_b if hit.locator.get("record_id")}
            self.assertEqual(record_ids, {22})
            circuit_ids = {hit.locator["circuit_id"] for hit in hits_b}
            self.assertNotIn("a_board", circuit_ids)
            joined = " ".join(hit.content for hit in hits_b)
            # Cross-department identical refdes must never leak its evidence.
            self.assertNotIn("a_board", joined)

            hits_a = service.query(kb_name="kb_hw", query="SoC 的连接关系", ctx=ctx_a, top_k=8)
            record_ids_a = {hit.locator.get("record_id") for hit in hits_a if hit.locator.get("record_id")}
            self.assertEqual(record_ids_a, {11})

    def test_module_labels_never_produce_role_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, _PlainParser())
            ctx = _index(service, tmp, "board.edf", 7)

            hits = service.query(kb_name="kb_hw", query="MCU 的连接关系", ctx=ctx, top_k=8)

            self.assertTrue(hits)
            kinds = {hit.locator["entity_type"] for hit in hits}
            self.assertNotIn("pin_mapping", kinds)
            self.assertNotIn("module_connection", kinds)
            self.assertNotIn("instance", kinds)
            status = next(hit for hit in hits if hit.metadata.get("resolution_status"))
            self.assertEqual(status.metadata["resolution_status"], "no_evidence")


class LegacyBehaviorPreservationTests(unittest.TestCase):
    def test_mcu_query_no_longer_falls_back_to_tc3_string_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, _PlainParser())
            ctx = _index(service, tmp, "board.edf", 7)

            hits = service.query(kb_name="kb_hw", query="MCU 的连接关系", ctx=ctx, top_k=8)

            joined = " ".join(hit.content for hit in hits)
            self.assertNotIn("TC397XE", joined)  # library_cell contains TC3… yet must not match

    def test_compound_clock_question_keeps_precise_crystal_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, _RoleDesignParser())
            ctx = _index(service, tmp, "board.edf", 7)

            hits = service.query(
                kb_name="kb_hw",
                query="MCU 和 SOC 使用的晶振频率分别是多少",
                ctx=ctx,
                top_k=8,
            )

            # Clock intent outranks the incidental role words; legacy precise
            # paths stay intact (here: no crystals exist, so no fabricated facts).
            self.assertFalse(any(hit.locator["entity_type"] == "component_identity" for hit in hits))

    def test_exact_refdes_queries_still_return_pin_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp, _RoleDesignParser())
            ctx = _index(service, tmp, "board.edf", 7)

            hits = service.query(kb_name="kb_hw", query="U100 连接", ctx=ctx, top_k=8)

            self.assertTrue(any(
                hit.locator["entity_type"] == "pin_mapping" and hit.locator.get("refdes") == "U100"
                for hit in hits
            ))


if __name__ == "__main__":
    unittest.main()
