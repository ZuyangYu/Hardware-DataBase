"""Task 1: identity projection and structure coverage model compatibility."""

import json
import os
import tempfile
import unittest

from src.circuit import store as store_module
from src.circuit.models import (
    Availability,
    CircuitDesign,
    CircuitStructureCoverage,
    ComponentAlias,
    ComponentIdentifier,
    ComponentIdentity,
    ComponentRoleAssertion,
)
from src.circuit.store import CircuitStore


def _identifier(**overrides):
    data = {
        "namespace": "manufacturer_part_number",
        "raw_value": "GCM155R71C104KA55D",
        "normalized_value": "gcm155r71c104ka55d",
        "source_kind": "edf_property",
        "source_locator": {"property_key": "Manufacturer Part Number"},
    }
    data.update(overrides)
    return ComponentIdentifier(**data)


def _role(**overrides):
    data = {
        "role_id": "system_on_chip",
        "display_name": "SoC",
        "source_kind": "edf_property",
        "source_file": "main_board.edf",
        "source_locator": {"property_key": "Device Role", "property_value": "SoC"},
        "confidence": 0.98,
        "assertion_mode": "explicit",
    }
    data.update(overrides)
    return ComponentRoleAssertion(**data)


def _identity(**overrides):
    data = {
        "refdes": "U900D",
        "identifiers": [
            _identifier(),
            _identifier(
                namespace="internal_part_number",
                raw_value="120000181",
                normalized_value="120000181",
                source_locator={"field": "part_number"},
            ),
        ],
        "aliases": [
            ComponentAlias(
                value="GCM155R71C104KA55D",
                origin_kind="identifier",
                origin_key="manufacturer_part_number:GCM155R71C104KA55D",
            )
        ],
        "roles": [
            _role(),
            _role(
                role_id="pmic",
                display_name="PMIC",
                source_kind="curated_catalog",
                assertion_mode="catalog_match",
                confidence=0.9,
            ),
        ],
    }
    data.update(overrides)
    return ComponentIdentity(**data)


def _coverage():
    return CircuitStructureCoverage(
        netlist_connectivity=Availability.AVAILABLE,
        module_partition_strategy="refdes_page_heuristic",
        source_partition_strategy="refdes_page",
        schematic_pages=Availability.UNAVAILABLE,
        schematic_page_count=0,
        title_block=Availability.UNAVAILABLE,
        coordinates=Availability.UNAVAILABLE,
        visual_layout=Availability.UNAVAILABLE,
        notes=["EDF netlist only; no PDF pages parsed."],
    )


def _design(include_projection=True):
    design = CircuitDesign(
        design_id="main_board",
        kb_name="kb_hw",
    )
    if include_projection:
        design.component_identities = [_identity()]
        design.structure_coverage = _coverage()
    return design


class IdentityModelRoundTripTests(unittest.TestCase):
    def test_roundtrip_preserves_identifiers_roles_and_coverage(self):
        design = _design()
        payload = json.loads(json.dumps(design.to_dict(), ensure_ascii=False))
        restored = CircuitDesign.from_dict(payload)

        self.assertEqual(len(restored.component_identities), 1)
        identity = restored.component_identities[0]
        self.assertEqual(identity.refdes, "U900D")
        self.assertEqual(
            [item.namespace for item in identity.identifiers],
            ["manufacturer_part_number", "internal_part_number"],
        )
        raw_identifier = identity.identifiers[0]
        self.assertEqual(raw_identifier.raw_value, "GCM155R71C104KA55D")
        self.assertEqual(raw_identifier.normalized_value, "gcm155r71c104ka55d")
        self.assertEqual(raw_identifier.source_kind, "edf_property")
        self.assertEqual(
            raw_identifier.source_locator,
            {"property_key": "Manufacturer Part Number"},
        )
        self.assertEqual(identity.aliases[0].origin_key, "manufacturer_part_number:GCM155R71C104KA55D")
        self.assertEqual([role.role_id for role in identity.roles], ["system_on_chip", "pmic"])
        self.assertEqual(identity.roles[1].assertion_mode, "catalog_match")
        self.assertAlmostEqual(identity.roles[1].confidence, 0.9)

        coverage = restored.structure_coverage
        self.assertEqual(coverage.netlist_connectivity, Availability.AVAILABLE)
        self.assertEqual(coverage.module_partition_strategy, "refdes_page_heuristic")
        self.assertEqual(coverage.source_partition_strategy, "refdes_page")
        self.assertEqual(coverage.schematic_pages, Availability.UNAVAILABLE)
        self.assertEqual(coverage.title_block, Availability.UNAVAILABLE)
        self.assertEqual(coverage.visual_layout, Availability.UNAVAILABLE)
        self.assertIn("EDF netlist only", coverage.notes[0])

        # Round-trip must be stable so the generation stamp stays deterministic.
        self.assertEqual(json.dumps(restored.to_dict(), sort_keys=True), json.dumps(payload, sort_keys=True))

    def test_rejects_identifier_without_namespace(self):
        payload = _design().to_dict()
        payload["component_identities"][0]["identifiers"][0] = {
            "namespace": "",
            "raw_value": "X",
            "normalized_value": "x",
            "source_kind": "edf_property",
            "source_locator": {},
        }
        with self.assertRaises(ValueError):
            CircuitDesign.from_dict(payload)

    def test_rejects_non_json_serializable_locator(self):
        payload = _design().to_dict()
        payload["component_identities"][0]["identifiers"][0]["source_locator"] = {"keys": {"a", "b"}}
        with self.assertRaises(ValueError):
            CircuitDesign.from_dict(payload)

    def test_construction_rejects_invalid_identifier(self):
        with self.assertRaises(ValueError):
            _identifier(namespace="")
        with self.assertRaises(ValueError):
            _identifier(source_locator={"bad": object()})


class LegacyStateCompatibilityTests(unittest.TestCase):
    def test_legacy_state_loads_with_safe_defaults(self):
        legacy = {
            "design_id": "legacy_board",
            "kb_name": "kb_hw",
            "status": "complete",
            "instances": [],
            "nets": [],
            "modules": [],
        }
        design = CircuitDesign.from_dict(legacy)

        self.assertEqual(design.component_identities, [])
        coverage = design.structure_coverage
        self.assertIsNotNone(coverage)
        self.assertEqual(coverage.netlist_connectivity, Availability.UNKNOWN)
        self.assertEqual(coverage.schematic_pages, Availability.UNKNOWN)
        self.assertEqual(coverage.coordinates, Availability.UNKNOWN)
        self.assertEqual(coverage.title_block, Availability.UNKNOWN)
        self.assertEqual(coverage.visual_layout, Availability.UNKNOWN)
        self.assertEqual(coverage.module_partition_strategy, "none")
        self.assertEqual(coverage.source_partition_strategy, "none")

    def test_unknown_identity_fields_are_tolerated(self):
        payload = _design().to_dict()
        payload["component_identities"][0]["future_field"] = {"x": 1}
        payload["component_identities"][0]["identifiers"][0]["also_new"] = 1
        payload["structure_coverage"]["future_flag"] = True
        design = CircuitDesign.from_dict(payload)

        self.assertEqual(len(design.component_identities), 1)
        self.assertEqual(len(design.component_identities[0].identifiers), 2)


class AtomicPublicationTests(unittest.TestCase):
    def test_failed_publish_keeps_previous_complete_generation_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CircuitStore(root=os.path.join(tmp, "circuits"))
            previous = _design(include_projection=False)
            store.save(previous)

            updated = _design()
            original_atomic_write = store_module._atomic_write
            calls = {"count": 0}

            def flaky_atomic_write(path, payload):
                calls["count"] += 1
                if os.path.basename(path) == "circuit_state.json":
                    raise OSError("simulated publish failure")
                original_atomic_write(path, payload)

            store_module._atomic_write = flaky_atomic_write
            try:
                with self.assertRaises(OSError):
                    store.save(updated)
            finally:
                store_module._atomic_write = original_atomic_write

            loaded = store.load("kb_hw", "main_board")
            self.assertEqual(loaded.instances, [])
            self.assertEqual(loaded.component_identities, [])

            store.save(updated)
            reloaded = store.load("kb_hw", "main_board")
            self.assertEqual(len(reloaded.component_identities), 1)
            self.assertEqual(
                reloaded.structure_coverage.module_partition_strategy,
                "refdes_page_heuristic",
            )

    def test_save_does_not_mutate_previously_loaded_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CircuitStore(root=os.path.join(tmp, "circuits"))
            store.save(_design(include_projection=False))
            loaded_before = store.load("kb_hw", "main_board")

            store.save(_design())

            self.assertEqual(loaded_before.component_identities, [])


if __name__ == "__main__":
    unittest.main()
