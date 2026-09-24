import importlib.util
import unittest


class RagasRuntimeTests(unittest.TestCase):
    @unittest.skipIf(importlib.util.find_spec("ragas") is None, "optional eval dependencies are not installed")
    def test_installed_ragas_imports_with_resolved_langchain_dependencies(self):
        import ragas
        import ragas.metrics.collections as metrics

        self.assertTrue(ragas.__version__.startswith("0.4."))
        self.assertTrue(hasattr(metrics, "Faithfulness"))


if __name__ == "__main__":
    unittest.main()
