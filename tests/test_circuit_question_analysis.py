import unittest

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


if __name__ == "__main__":
    unittest.main()
