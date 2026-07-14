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

        calls = _derived_datasheet_calls("电源输出电路是否有短地保护？", [hit.model_dump() for hit in hits])
        candidate = next(hit for hit in hits if hit.locator["entity_id"] == "power_control_candidate:U1")
        self.assertIn("VCC3V3", candidate.content)
        self.assertIn("VOUT_A", candidate.content)
        self.assertEqual(len(calls), 1)
        self.assertIn("TPS22919", calls[0]["query"])


if __name__ == "__main__":
    unittest.main()
