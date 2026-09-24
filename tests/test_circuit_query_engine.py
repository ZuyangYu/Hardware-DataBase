import os
import tempfile
import unittest

from src.circuit.models import CircuitDesign, CircuitModule, ComponentInstance, Net, Pin, PinRef
from src.circuit.query_engine import CircuitQueryEngine
from src.circuit.store import CircuitStore


class CircuitQueryEngineTests(unittest.TestCase):
    def test_search_net_connections_returns_connected_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CircuitStore(root=os.path.join(tmp, "circuits"))
            store.save(
                CircuitDesign(
                    design_id="main_board",
                    kb_name="kb_hw",
                    instances=[
                        ComponentInstance(refdes="U1200", library_cell="CAN_PHY", pins=[Pin(name="1", net="CAN0")]),
                        ComponentInstance(refdes="J3", library_cell="CONNECTOR", pins=[Pin(name="2", net="CAN0")]),
                    ],
                    nets=[Net(name="CAN0", connections=[PinRef(refdes="U1200", pin="1"), PinRef(refdes="J3", pin="2")])],
                    modules=[CircuitModule(module_id="can", name="CAN", strategy="fixture", instances=["U1200", "J3"], nets=["CAN0"])],
                )
            )

            rows = CircuitQueryEngine(store=store).search_net_connections("kb_hw", "CAN0", limit=5)

        self.assertEqual(rows[0]["net_name"], "CAN0")
        self.assertEqual({row["refdes"] for row in rows[0]["connections"]}, {"U1200", "J3"})


if __name__ == "__main__":
    unittest.main()
