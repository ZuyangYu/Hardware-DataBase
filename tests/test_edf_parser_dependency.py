import logging
import sys
import tomllib
import types
import unittest
from pathlib import Path

from src.circuit.parsers.edf_parser import _load_spydrnet, _repo_spydrnet_path


class EdfParserDependencyTests(unittest.TestCase):
    def test_parser_uses_vendored_spydrnet_root(self):
        expected_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "circuit"
            / "vendor"
            / "spydrnet"
        )

        self.assertEqual(_repo_spydrnet_path(), expected_path)

    def test_vendored_spydrnet_package_is_available(self):
        repo_path = _repo_spydrnet_path()

        self.assertTrue((repo_path / "spydrnet" / "__init__.py").is_file())
        self.assertFalse((repo_path / ".git").exists())

    def test_project_does_not_depend_on_pypi_spydrnet(self):
        project_root = Path(__file__).resolve().parents[1]
        with (project_root / "pyproject.toml").open("rb") as project_file:
            dependencies = tomllib.load(project_file)["project"]["dependencies"]

        self.assertFalse(
            any(item.lower().startswith("spydrnet") for item in dependencies)
        )

    def test_pytest_excludes_vendored_spydrnet_tests(self):
        project_root = Path(__file__).resolve().parents[1]
        with (project_root / "pyproject.toml").open("rb") as project_file:
            pytest_options = tomllib.load(project_file)["tool"]["pytest"]["ini_options"]

        self.assertTrue(
            {"build", "src/circuit/vendor/spydrnet"}.issubset(
                pytest_options["norecursedirs"]
            )
        )

    def test_vendor_packaging_excludes_upstream_tests_and_docs(self):
        project_root = Path(__file__).resolve().parents[1]
        with (project_root / "pyproject.toml").open("rb") as project_file:
            setuptools = tomllib.load(project_file)["tool"]["setuptools"]

        excluded = set(setuptools["packages"]["find"]["exclude"])
        self.assertTrue(
            {
                "src.circuit.vendor.spydrnet.docs*",
                "src.circuit.vendor.spydrnet.examples*",
                "src.circuit.vendor.spydrnet.example_netlists*",
                "src.circuit.vendor.spydrnet.tests*",
                "src.circuit.vendor.spydrnet.spydrnet_extension*.tests*",
            }.issubset(excluded)
        )
        self.assertEqual(
            setuptools["package-data"][
                "src.circuit.vendor.spydrnet.spydrnet.support_files"
            ],
            ["*.tcl", "architecture_libraries/*.zip"],
        )

    def test_loader_replaces_preloaded_non_vendor_spydrnet(self):
        fake_spydrnet = types.ModuleType("spydrnet")
        fake_spydrnet.__file__ = "C:/site-packages/spydrnet/__init__.py"
        original_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "spydrnet" or name.startswith("spydrnet.")
        }
        previous_disable_level = logging.root.manager.disable
        try:
            logging.disable(logging.CRITICAL)
            sys.modules["spydrnet"] = fake_spydrnet
            loaded = _load_spydrnet()
            loaded_path = Path(loaded.__file__).resolve()
        finally:
            logging.disable(previous_disable_level)
            for name in tuple(sys.modules):
                if name == "spydrnet" or name.startswith("spydrnet."):
                    sys.modules.pop(name, None)
            sys.modules.update(original_modules)

        self.assertTrue(loaded_path.is_relative_to(_repo_spydrnet_path()))


class ComponentIdentityDependencyTests(unittest.TestCase):
    def test_identity_module_has_no_network_or_vector_dependencies(self):
        module_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "circuit"
            / "component_identity.py"
        )
        source = module_path.read_text(encoding="utf-8")

        for banned in ("requests", "urllib", "httpx", "socket", "vector_index", "ragflow"):
            self.assertNotIn(banned, source)
