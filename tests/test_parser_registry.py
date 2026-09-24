import ast
import unittest
from pathlib import Path


class ParserRegistryImportTests(unittest.TestCase):
    def test_parser_registry_does_not_import_llama_index(self):
        # The live agent import chain (agents.graph -> ingestion.parser_registry)
        # must not hard-depend on LlamaIndex (architecture doc §8 says it must
        # not be reintroduced). Assert at the AST level so the check cannot be
        # fooled by modules already loaded into sys.modules or by explanatory
        # comments that mention the name.
        import src.ingestion.parser_registry as parser_registry

        tree = ast.parse(Path(parser_registry.__file__).read_text(encoding="utf-8"))
        top_level_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                top_level_modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level_modules.add(node.module.split(".")[0])
        self.assertNotIn(
            "llama_index",
            top_level_modules,
            f"parser_registry must not import llama_index; imports: {sorted(top_level_modules)}",
        )


class ParserRegistryCapabilityTests(unittest.TestCase):
    def test_fresh_registry_has_no_parsers_or_capabilities(self):
        from src.ingestion.parser_registry import ParserRegistry

        registry = ParserRegistry()
        self.assertEqual(registry.capabilities_for("any_group"), ())
        self.assertEqual(registry.implemented_groups(), set())
        self.assertIsNone(registry.get_parser("any_group"))

    def test_capabilities_for_returns_registered(self):
        # Live path: agents.graph calls capabilities_for *after* a domain
        # manifest has been registered. Registering a manifest must make its
        # capabilities and parser retrievable, while unknown groups stay empty.
        from src.ingestion.evidence_capability import EvidenceCapability
        from src.ingestion.parser_registry import DomainManifest, ParserRegistry

        def _fake_parser(path, kb, group, progress):
            return []

        capability = EvidenceCapability(
            name="entity_lookup",
            content_kinds=["test_data"],
            direct_fact=True,
        )
        manifest = DomainManifest(
            name="test_register",
            source_groups=("test_group",),
            parser_factories={"test_group": _fake_parser},
            capabilities={"test_group": (capability,)},
        )

        registry = ParserRegistry()
        registry.register_manifest(manifest)

        self.assertEqual(registry.capabilities_for("test_group"), (capability,))
        self.assertIn("test_group", registry.implemented_groups())
        self.assertIs(registry.get_parser("test_group"), _fake_parser)
        # Unknown groups are unaffected by the registration.
        self.assertEqual(registry.capabilities_for("other_group"), ())


if __name__ == "__main__":
    unittest.main()
