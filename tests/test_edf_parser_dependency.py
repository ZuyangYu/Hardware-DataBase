import unittest
from pathlib import Path

from src.circuit.parsers.edf_parser import _repo_spydrnet_path


class EdfParserDependencyTests(unittest.TestCase):
    def test_workspace_spydrnet_checkout_is_available(self):
        repo_path = _repo_spydrnet_path()

        self.assertEqual(repo_path, Path(__file__).resolve().parents[2] / "spydrnet")
        self.assertTrue((repo_path / "spydrnet" / "__init__.py").is_file())
