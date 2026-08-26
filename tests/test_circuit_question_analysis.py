import unittest
import json
from pathlib import Path

from src.circuit.question_analysis import analyze_question


class CircuitQuestionAnalysisTests(unittest.TestCase):
    def test_bias_question_selects_bias_operation(self):
        plan = analyze_question("输入输出信号是否共用上拉电源？")

        self.assertEqual(plan.operations, ("bias",))
        self.assertFalse(plan.requires_datasheet)

    def test_output_protection_question_requires_topology_and_datasheet(self):
        plan = analyze_question("电源输出电路是否有短电源和短地保护？")

        self.assertEqual(plan.operations, ("power_path", "protection"))
        self.assertTrue(plan.requires_datasheet)

    def test_connection_question_selects_connection_operation(self):
        self.assertEqual(analyze_question("CAN0 连接到哪里？").operations, ("connection",))

    def test_unknown_question_has_no_circuit_operations(self):
        self.assertEqual(analyze_question("今天天气如何？").operations, ())

    def test_evaluation_questions_select_structural_operations(self):
        dataset = Path(__file__).resolve().parents[1] / "evaluation" / "datasets" / "ai_database_test.jsonl"
        if not dataset.exists():
            self.skipTest(f"missing evaluation dataset: {dataset.name}")
        expected_operations = {
            "ai-db-v1-power-9-12v-to-3v3": {"component_selection", "power_path"},
            "ai-db-v1-i2c-buses-and-pullups": {"i2c", "bias", "connection"},
            "ai-db-v1-ln10046-enable-sources": {"enable", "connection"},
            "ai-db-v1-mcu-soc-crystals": {"clock", "component_selection"},
            "ai-db-v1-can0-rxd-pullup": {"bias", "connection", "value"},
        }

        for line in dataset.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            with self.subTest(row_id=row["id"]):
                plan = analyze_question(row["question"])
                self.assertTrue(expected_operations[row["id"]].issubset(plan.operations))

    def test_bare_refdes_and_net_identifiers_select_connection(self):
        self.assertIn("entity_lookup", analyze_question("U1600").operations)
        self.assertIn("connection", analyze_question("ECU_EN").operations)

    def test_generic_substrings_do_not_create_structural_operations(self):
        for question in ("explain the data model", "nearly complete", "Tuesday status", "muscle test", "value proposition"):
            with self.subTest(question=question):
                self.assertEqual(analyze_question(question).operations, ())

    def test_power_path_tokens_and_english_variants_are_boundary_safe(self):
        self.assertIn("power_path", analyze_question("VIN to VOUT power path").operations)
        self.assertIn("bias", analyze_question("pull up and pull down").operations)
        self.assertIn("enable", analyze_question("wake-up source").operations)
        for question in ("saving costs", "devout response"):
            with self.subTest(question=question):
                self.assertNotIn("power_path", analyze_question(question).operations)


class StructureAndRoleIntentTests(unittest.TestCase):
    def test_structure_questions_select_overview_and_module_operations(self):
        overview = analyze_question("原理图的结构信息是什么")
        profile = analyze_question("查看设计概况")
        modules = analyze_question("列出所有模块")

        for plan in (overview, profile):
            self.assertIn("structure_overview", plan.operations)
        self.assertIn("module_list", modules.operations)
        # Analysis describes capability only; it never names a device.
        self.assertIsNone(overview.role_term)

    def test_visual_structure_capability_is_described_not_asserted(self):
        plan = analyze_question("原理图页面和坐标数据有哪些")

        self.assertIn("visual_structure", plan.operations)
        self.assertNotIn("structure_overview", plan.operations)

    def test_role_terms_map_to_entity_role_with_term_capture(self):
        soc = analyze_question("SoC 的连接关系")
        mcu = analyze_question("MCU 连接")
        controller = analyze_question("主控的供电路径")

        self.assertIn("entity_role", soc.operations)
        self.assertEqual(soc.role_term.casefold(), "soc")
        self.assertIn("entity_role", mcu.operations)
        self.assertEqual(mcu.role_term.casefold(), "mcu")
        self.assertIn("entity_role", controller.operations)
        self.assertEqual(controller.role_term, "主控")
        # Family words are retrieval intent only.
        self.assertFalse(controller.requires_datasheet)

    def test_non_role_words_do_not_trigger_entity_role(self):
        for question in ("CAN0 连接到哪里", "U900D 引脚", "上拉电阻阻值"):
            with self.subTest(question=question):
                plan = analyze_question(question)
                self.assertNotIn("entity_role", plan.operations)
                self.assertIsNone(plan.role_term)


if __name__ == "__main__":
    unittest.main()
