"""Task 2: evidence-backed component identity projection."""

import os
import tempfile
import unittest

from src.circuit.component_identity import (
    CONTROLLED_ROLES,
    CuratedCatalogEntry,
    EntityResolutionResult,
    build_component_identities,
    resolve_entity_mention,
)
from src.circuit.graph_store import GraphStore
from src.circuit.index_service import CircuitIndexService
from src.circuit.models import (
    CircuitDesign,
    CircuitModule,
    ComponentInstance,
    Net,
    Pin,
)
from src.circuit.store import CircuitStore


class _UnavailableVectorIndex:
    available = False

    def reindex_design_with_status(self, design):
        from src.circuit.vector_index import CircuitVectorIndexStatus

        return CircuitVectorIndexStatus(available=False, indexed_count=0)


class _Parser:
    warnings: list[str] = []

    def __init__(self, instances, nets, modules):
        self._instances = instances
        self._nets = nets
        self._modules = modules

    def parse(self):
        return self._instances, self._nets, self._modules


def _instance(refdes, **overrides):
    data = {
        "refdes": refdes,
        "library_cell": None,
        "part_number": None,
        "value": None,
        "erp_number": None,
        "pins": [],
        "properties": {},
    }
    data.update(overrides)
    return ComponentInstance(**data)


def _design(instances, modules=None, design_id="main_board"):
    return CircuitDesign(
        design_id=design_id,
        kb_name="kb_hw",
        files=[],
        instances=list(instances),
        nets=[],
        modules=list(modules or []),
    )


class BuildIdentityProjectionTests(unittest.TestCase):
    def test_edf_fields_become_namespaced_traceable_identifiers(self):
        design = _design(
            [
                _instance(
                    "U900D",
                    part_number="120000181",
                    erp_number="801201810",
                    value="100nF",
                    properties={
                        "Manufacturer Part Number": "GCM155R71C104KA55D/CL05B104KO5VPNC",
                        "ERP NUM": "801201810",
                    },
                )
            ]
        )

        identities = build_component_identities(design)

        self.assertEqual(len(identities), 1)
        identity = identities[0]
        by_ns = {}
        for identifier in identity.identifiers:
            by_ns.setdefault(identifier.namespace, []).append(identifier)
        self.assertIn("refdes", by_ns)
        self.assertIn("internal_part_number", by_ns)
        self.assertIn("manufacturer_part_number", by_ns)
        self.assertIn("erp_number", by_ns)
        mpn = by_ns["manufacturer_part_number"][0]
        # Raw multi-MPN value is preserved verbatim; normalization is separate.
        self.assertEqual(mpn.raw_value, "GCM155R71C104KA55D/CL05B104KO5VPNC")
        self.assertNotEqual(mpn.normalized_value, mpn.raw_value)
        self.assertEqual(
            mpn.source_locator.get("property_key"), "Manufacturer Part Number"
        )
        # Every alias traces back to an identifier of the same identity.
        identifier_keys = {
            f"{item.namespace}:{item.raw_value}" for item in identity.identifiers
        }
        self.assertTrue(identity.aliases)
        for alias in identity.aliases:
            self.assertEqual(alias.origin_kind, "identifier")
            self.assertIn(alias.origin_key, identifier_keys)

    def test_role_assertions_come_only_from_controlled_sources(self):
        design = _design(
            [
                _instance(
                    "U1",
                    properties={"Device Role": "SoC"},
                ),
                _instance(
                    "U2",
                    library_cell="CORTEXM4_MC",
                    value="STM32",
                    properties={
                        "Description": "MCU microcontroller unit",
                        "Part Type": "Microcontroller",
                    },
                ),
                _instance("U3"),
            ],
            modules=[
                CircuitModule(
                    module_id="m_mcu_power",
                    name="MCU_POWER_SoC",
                    strategy="refdes_page",
                    instances=["U2"],
                )
            ],
        )

        identities = {item.refdes: item for item in build_component_identities(design)}

        u1_roles = identities["U1"].roles
        self.assertEqual([role.role_id for role in u1_roles], ["system_on_chip"])
        assertion = u1_roles[0]
        self.assertEqual(assertion.assertion_mode, "explicit")
        self.assertEqual(assertion.source_kind, "edf_property")
        self.assertEqual(assertion.source_locator.get("property_key"), "Device Role")
        self.assertEqual(assertion.source_locator.get("property_value"), "SoC")
        # Heuristic labels (library cell, description, module names) never assert.
        self.assertEqual(identities["U2"].roles, [])
        self.assertEqual(identities["U3"].roles, [])

    def test_curated_catalog_match_records_version_and_mode(self):
        design = _design(
            [
                _instance("U10", part_number="120000181"),
                _instance("U11", part_number="120000181"),
            ]
        )
        entry = CuratedCatalogEntry(
            entry_id="cat-mcu-1",
            catalog_version="2026.08-v3",
            source_file="catalog/pdn_catalog.json",
            match_namespace="internal_part_number",
            match_raw_value="120000181",
            role_id="mcu",
            display_name="MCU",
        )
        from src.circuit.component_identity import build_component_identities as build

        identities = build(design, catalog_entries=[entry])

        for refdes in ("U10", "U11"):
            roles = next(i for i in identities if i.refdes == refdes).roles
            self.assertEqual([role.role_id for role in roles], ["mcu"])
            self.assertEqual(roles[0].assertion_mode, "catalog_match")
            self.assertEqual(roles[0].source_kind, "curated_catalog")
            self.assertEqual(roles[0].source_locator.get("catalog_entry_id"), "cat-mcu-1")
            self.assertEqual(
                roles[0].source_locator.get("catalog_version"), "2026.08-v3"
            )


class ResolveEntityMentionTests(unittest.TestCase):
    def test_two_role_candidates_both_returned_in_fixed_order(self):
        design = _design(
            [
                _instance("U2", properties={"Device Role": "SOC"}),
                _instance("U1", properties={"Device Role": "SoC"}),
            ]
        )

        result = resolve_entity_mention("SoC", [design])

        self.assertIsInstance(result, EntityResolutionResult)
        self.assertEqual(result.resolution_status, "ambiguous")
        self.assertEqual(result.candidate_count, 2)
        self.assertEqual(result.returned_candidate_count, 2)
        self.assertEqual(
            [(c.design_id, c.refdes) for c in result.candidates],
            [("main_board", "U1"), ("main_board", "U2")],
        )

    def test_refdes_without_any_role_source_returns_no_evidence(self):
        design = _design([_instance("U100")])

        result = resolve_entity_mention("SoC", [design])

        self.assertEqual(result.resolution_status, "no_evidence")
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.candidate_count, 0)

    def test_module_or_library_labels_do_not_create_role_candidates(self):
        design = _design(
            [_instance("U5", library_cell="SOIC8")],
            modules=[
                CircuitModule(
                    module_id="m_mcu",
                    name="MCU_CORE",
                    strategy="refdes_page",
                    instances=["U5"],
                )
            ],
        )

        result = resolve_entity_mention("MCU", [design])

        self.assertEqual(result.resolution_status, "no_evidence")

    def test_exact_terms_do_not_substring_match(self):
        design = _design([_instance("U900D")])

        result = resolve_entity_mention("U900", [design])

        self.assertEqual(result.resolution_status, "no_evidence")

    def test_identifier_resolution_matches_namespaces_exactly(self):
        design = _design(
            [
                _instance("U1", part_number="120000181"),
                _instance(
                    "U2",
                    properties={"Manufacturer Part Number": "GCM155R71C104KA55D"},
                ),
            ]
        )

        by_refdes = resolve_entity_mention("u1", [design])
        self.assertEqual(by_refdes.resolution_status, "unique")
        self.assertEqual(by_refdes.candidates[0].matched_by, "refdes_exact")
        self.assertEqual(by_refdes.candidates[0].refdes, "U1")

        by_mpn = resolve_entity_mention("gcm155r71c104ka55d", [design])
        self.assertEqual(by_mpn.resolution_status, "unique")
        self.assertEqual(by_mpn.candidates[0].matched_by, "identifier_exact")
        self.assertEqual(by_mpn.candidates[0].refdes, "U2")

    def test_uniqueness_is_judged_across_all_authorized_designs(self):
        d1 = _design([_instance("U1", properties={"Device Role": "MCU"})], design_id="board_a")
        d2 = _design([_instance("U9", properties={"Device Role": "MCU"})], design_id="board_b")

        result = resolve_entity_mention("MCU", [d1, d2])

        self.assertEqual(result.resolution_status, "ambiguous")
        self.assertEqual(result.candidate_count, 2)
        self.assertEqual(
            [(c.design_id, c.refdes) for c in result.candidates],
            [("board_a", "U1"), ("board_b", "U9")],
        )

    def test_intent_vocabulary_maps_soc_mcu_and_controller_family(self):
        soc = resolve_entity_mention("SoC", [])
        mcu = resolve_entity_mention("MCU", [])
        controller = resolve_entity_mention("主控", [])

        self.assertEqual(soc.role_query, "system_on_chip")
        self.assertIsNone(soc.controller_family_roles)
        self.assertEqual(mcu.role_query, "mcu")
        self.assertEqual(controller.role_query, "controller_family")
        self.assertEqual(
            controller.controller_family_roles, ("system_on_chip", "mcu")
        )
        for result in (soc, mcu, controller):
            self.assertEqual(result.intent_kind, "role")

    def test_controller_family_keeps_real_roles_never_upgraded_to_soc(self):
        design = _design([_instance("U7", properties={"Device Role": "MCU"})])

        result = resolve_entity_mention("主控", [design])

        self.assertEqual(result.resolution_status, "unique")
        candidate = result.candidates[0]
        self.assertEqual(candidate.matched_role_ids, ("mcu",))
        role_ids = [role.role_id for role in candidate.roles]
        self.assertIn("mcu", role_ids)
        self.assertNotIn("system_on_chip", role_ids)


CONTROLLED_ROLE_IDS = CONTROLLED_ROLES


class IndexPublicationTests(unittest.TestCase):
    def _service(self, tmp, instances, nets=None, modules=None):
        store = CircuitStore(root=os.path.join(tmp, "circuits"))

        def parser_factory(path):
            return _Parser(instances, nets or [], modules or [])

        return CircuitIndexService(
            store=store,
            parser_factory=parser_factory,
            graph_store=GraphStore(),
            vector_index=_UnavailableVectorIndex(),
        )

    def test_publishes_identity_projection_without_vector_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(
                tmp,
                [
                    _instance(
                        "U1",
                        part_number="120000181",
                        pins=[Pin(name="VDD", net="VDD_3V3")],
                        properties={"Device Role": "MCU"},
                    )
                ],
                nets=[Net(name="VDD_3V3", connections=[])],
            )
            source = os.path.join(tmp, "main_board.edf")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("(edif main_board)")

            result = service.index_file(
                kb_name="kb_hw",
                record_id=7,
                file_path=source,
                original_name="main_board.edf",
                department_id="dept_hw",
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.stats.get("identity_count"), 1)
            loaded = service.store.load("kb_hw", result.design_id)
            self.assertEqual(len(loaded.component_identities), 1)
            identity = loaded.component_identities[0]
            self.assertEqual(identity.refdes, "U1")
            self.assertEqual(identity.roles[0].role_id, "mcu")
            metadata = service._read_metadata("kb_hw", result.design_id)
            self.assertEqual(metadata.get("identity_projection_status"), "indexed")

    def test_identity_generation_failure_rolls_back_whole_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp, [_instance("U1", part_number="P1")])
            source = os.path.join(tmp, "main_board.edf")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("(edif main_board)")
            first = service.index_file(
                kb_name="kb_hw",
                record_id=7,
                file_path=source,
                original_name="main_board.edf",
                department_id="dept_hw",
            )
            good_generation = service._read_metadata("kb_hw", first.design_id)["generation_id"]
            self.assertEqual(len(service.store.load("kb_hw", first.design_id).component_identities), 1)

            import src.circuit.index_service as index_service_module

            original_builder = index_service_module.build_component_identities

            def broken_builder(*args, **kwargs):
                raise RuntimeError("identity projection exploded")

            index_service_module.build_component_identities = broken_builder
            try:
                with self.assertRaises(Exception):
                    service.reindex_stored_design("kb_hw", first.design_id)
            finally:
                index_service_module.build_component_identities = original_builder

            loaded = service.store.load("kb_hw", first.design_id)
            self.assertEqual(len(loaded.instances), 1)
            self.assertEqual(len(loaded.component_identities), 1)
            metadata = service._read_metadata("kb_hw", first.design_id)
            self.assertEqual(metadata["generation_id"], good_generation)


if __name__ == "__main__":
    unittest.main()
