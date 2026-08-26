import os
import tempfile
import unittest

from src.circuit.index_service import CircuitIndexService
from src.circuit.models import CircuitDesign, CircuitStatus, ComponentInstance, DesignFile, Net, Pin, PinRef
from src.circuit.store import CircuitStore
from src.agents.graph import _derived_datasheet_calls


class CircuitTopologyQueryTests(unittest.TestCase):
    def _service(self, root):
        store = CircuitStore(root=os.path.join(root, "circuits"))
        store.save(
            CircuitDesign(
                design_id="main_board",
                kb_name="kb_hw",
                status=CircuitStatus.COMPLETE,
                files=[DesignFile("main_board.edf", "edf", "circuit_design", "main_board.edf")],
                instances=[
                    ComponentInstance("R1", library_cell="RES0402", value="4.7K", pins=[Pin("1", "SIGNAL_A"), Pin("2", "VCC3V3")]),
                    ComponentInstance("R2", library_cell="RES0402", value="10K", pins=[Pin("1", "SIGNAL_B"), Pin("2", "GND")]),
                    ComponentInstance("R3", library_cell="RES0402", value="0R", pins=[Pin("1", "SIGNAL_C"), Pin("2", "VCC3V3")]),
                    ComponentInstance("D1", library_cell="TVS", part_number="SMBJ33CA", pins=[Pin("A", "GND"), Pin("B", "SIGNAL_A")]),
                    ComponentInstance("D2", library_cell="DIODE", part_number="1N4148", pins=[Pin("A", "VCC3V3"), Pin("B", "SIGNAL_B")]),
                    ComponentInstance("U1", library_cell="LOADSW", part_number="TPS22919", pins=[Pin("IN", "VCC3V3"), Pin("OUT", "VOUT_A"), Pin("GND", "GND")]),
                ],
                nets=[
                    Net("VCC3V3", [PinRef("R1", "2"), PinRef("R3", "2")], "power"),
                    Net("GND", [PinRef("R2", "2"), PinRef("D1", "A")], "ground"),
                    Net("SIGNAL_A", [PinRef("R1", "1"), PinRef("D1", "B")]),
                    Net("SIGNAL_B", [PinRef("R2", "1")]),
                    Net("SIGNAL_C", [PinRef("R3", "1")]),
                    Net("VOUT_A", [PinRef("U1", "OUT")], "power"),
                ],
            )
        )
        return CircuitIndexService(store=store)

    def test_bias_question_returns_pullup_but_excludes_zero_ohm_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = self._service(tmp).query(kb_name="kb_hw", query="输入输出信号是否共用上拉电源？", ctx=None, top_k=10)

        self.assertEqual([hit.locator["entity_id"] for hit in hits], ["pull_up:R1"])
        self.assertEqual(hits[0].metadata["evidence_kind"], "derived_topology")
        self.assertIn("SIGNAL_A", hits[0].content)
        self.assertIn("VCC3V3", hits[0].content)

    def test_protection_question_returns_observed_tvs_topology(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = self._service(tmp).query(kb_name="kb_hw", query="电源输出电路是否有短地保护？", ctx=None, top_k=10)

        tvs = next(hit for hit in hits if hit.locator["entity_id"] == "protection_tvs:D1")
        self.assertEqual(tvs.metadata["evidence_kind"], "derived_topology")
        self.assertEqual(tvs.metadata["part_numbers"], ["SMBJ33CA"])
        self.assertIn("does not confirm", tvs.content)

    def test_power_control_candidate_is_the_only_manual_followup_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = self._service(tmp).query(kb_name="kb_hw", query="电源输出电路是否有短地保护？", ctx=None, top_k=10)

        candidate = next(hit for hit in hits if hit.locator["entity_id"] == "power_control_candidate:U1")
        self.assertIn("VCC3V3", candidate.content)
        self.assertIn("VOUT_A", candidate.content)
        # M1 gate: without a verified component-datasheet link no document
        # lookup is derived from the topology candidate.
        calls = _derived_datasheet_calls("电源输出电路是否有短地保护？", [hit.model_dump() for hit in hits])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()


class LinkObservabilityTests(unittest.TestCase):
    def test_verified_link_followup_diagnostics_are_bounded_and_scoped(self):
        from src.agents.graph import retrieve_evidence

        class _Doc:
            def __init__(self):
                self.filters = []

            def run(self, query, *args, filters=None, **kwargs):
                self.filters.append(filters)
                return []

        class _Stub:
            def run(self, *args, **kwargs):
                from src.agents.state import Evidence

                return [
                    Evidence(
                        id="circuit:7:topology:t:U1",
                        content="Observed candidate.",
                        source_name="board.edf",
                        content_kind="circuit_design",
                        processor_kind="circuit_design",
                        metadata={
                            "evidence_kind": "derived_topology",
                            "capability_candidate": True,
                            "part_numbers": ["TPS22919"],
                        },
                    )
                ]

        state = {
            "kb_name": "kb_hw",
            "user_query": "电源输出电路是否有短地保护？",
            "source_plan": {"source_plan": [{"tool_calls": [{"tool_name": "circuit_query", "query": "x"}]}]},
            "evidence": [],
            "trace": [],
            "_verified_datasheet_links": [
                {"refdes": "U1", "part_number": "TPS22919", "record_ids": [42]}
            ],
        }
        document = _Doc()
        result = retrieve_evidence(state, {"circuit_query": _Stub(), "document_rag": document})

        followups = [
            item
            for item in result["retrieval_diagnostics"]
            if item.get("derived_from") == "circuit_part_number"
        ]
        self.assertEqual(len(followups), 1)
        item = followups[0]
        self.assertEqual(item["datasheet_link_status"], "verified")
        self.assertEqual(item["datasheet_match_method"], "exact_mpn")
        self.assertEqual(item["allowed_record_ids_count"], 1)
