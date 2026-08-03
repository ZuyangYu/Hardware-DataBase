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


if __name__ == "__main__":
    unittest.main()
