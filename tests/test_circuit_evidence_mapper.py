import unittest

from src.circuit.evidence_mapper import CircuitEvidenceMapper


class CircuitEvidenceMapperTests(unittest.TestCase):
    def test_pin_mapping_preserves_normalized_connected_and_unconnected_pins(self):
        evidence = CircuitEvidenceMapper().build(
            kind="pin_mapping",
            row={
                "design_id": "design-1",
                "refdes": "J1",
                "pins": [
                    {"name": "&1", "net_name": "CAN_H"},
                    {"name": "&2", "net_name": None},
                    {"name": "&3", "net_name": "PGND"},
                ],
            },
            metadata={"record_id": 17, "kb_name": "kb-1", "department_id": "dep-1"},
            source_name="board.edf",
            score=0.9,
        )

        self.assertIn("1 -> CAN_H", evidence.content)
        self.assertIn("2 -> NC（源文件未声明网络连接）", evidence.content)
        self.assertIn("3 -> PGND", evidence.content)
        self.assertNotIn("&1", evidence.content)
        self.assertEqual(
            evidence.metadata["pin_mappings"],
            [
                {
                    "raw_pin_name": "&1",
                    "pin_name": "1",
                    "net_name": "CAN_H",
                    "connection_state": "connected",
                },
                {
                    "raw_pin_name": "&2",
                    "pin_name": "2",
                    "net_name": None,
                    "connection_state": "unconnected",
                },
                {
                    "raw_pin_name": "&3",
                    "pin_name": "3",
                    "net_name": "PGND",
                    "connection_state": "connected",
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
