import unittest

from src.ingestion.source_groups import (
    DESIGN_GROUP,
    NETLIST_GROUP,
    SCHEMATIC_GROUP,
    classify_source_group,
    display_source_group,
    expand_source_group_for_file,
)
from src.pipelines.registry import CONTENT_KIND_CIRCUIT, PROCESSOR_KIND_CIRCUIT, route_file


class CircuitPipelineRoutingTests(unittest.TestCase):
    def test_design_umbrella_routes_edf_to_internal_netlist_group(self):
        self.assertEqual(expand_source_group_for_file(DESIGN_GROUP, "main_board.edf"), NETLIST_GROUP)
        self.assertEqual(classify_source_group("main_board.edif").group, NETLIST_GROUP)
        self.assertEqual(display_source_group(NETLIST_GROUP), "设计数据")

    def test_design_umbrella_routes_schematic_pdf_to_internal_schematic_group(self):
        self.assertEqual(expand_source_group_for_file(DESIGN_GROUP, "camera_schematic.pdf"), SCHEMATIC_GROUP)
        self.assertEqual(classify_source_group("camera_schematic.pdf").group, SCHEMATIC_GROUP)
        self.assertEqual(display_source_group(SCHEMATIC_GROUP), "设计数据")

    def test_pipeline_registry_routes_edf_to_circuit_processor(self):
        route = route_file("/tmp/main_board.edf")
        self.assertTrue(route.supported)
        self.assertEqual(route.spec.processor_kind, PROCESSOR_KIND_CIRCUIT)
        self.assertEqual(route.spec.content_kind, CONTENT_KIND_CIRCUIT)


if __name__ == "__main__":
    unittest.main()
