"""Task 5b: governed EDF → datasheet link index."""

import os
import tempfile
import unittest

from src.circuit.component_identity import build_component_identities
from src.circuit.datasheet_link_index import (
    ComponentDatasheetLinkIndex,
    DocumentProfileSnapshot,
    DatasheetCatalogEntry,
    split_mpn_candidates,
)
from src.circuit.index_service import CircuitIndexService
from src.circuit.models import ComponentInstance
from src.circuit.store import CircuitStore, circuit_generation_id

from tests.test_circuit_structure_query import (
    _RoleDesignParser,
    _UnavailableVectorIndex,
)


def _profile(
    record_id=42,
    mpn=("GCM155R71C104KA55D",),
    manufacturer="Murata",
    revision="r3",
    content_hash="hash-1",
    source_version_id="v1",
    department_id="dept_hw",
) -> DocumentProfileSnapshot:
    return DocumentProfileSnapshot(
        record_id=record_id,
        kb_name="kb_hw",
        department_id=department_id,
        remote_document_id=f"remote-{record_id}",
        parse_status="parsed",
        content_hash=content_hash,
        source_version_id=source_version_id,
        revision=revision,
        mpn_values=tuple(mpn),
        manufacturer=manufacturer,
    )


def _design(**instance_overrides):
    properties = {
        "Manufacturer Part Number": "GCM155R71C104KA55D/CL05B104KO5VPNC",
        "Manufacturer": "Murata",
    }
    properties.update(instance_overrides.pop("properties", {}))
    instances, nets, modules = _RoleDesignParser().parse()
    instances = list(instances)
    instances.append(
        ComponentInstance(refdes="C100", value="100nF", pins=[], properties=dict(properties))
    )
    return instances, nets, modules


def _index(tmp, document_store=None):
    store = CircuitStore(root=os.path.join(tmp, "circuits"))
    instances, nets, modules = _design()

    class _Parser:
        warnings: list[str] = []

        def parse(self):
            return instances, nets, modules

    service = CircuitIndexService(
        store=store,
        parser_factory=lambda path: _Parser(),
        vector_index=_UnavailableVectorIndex(),
        document_store=document_store,
    )
    source = os.path.join(tmp, "board.edf")
    with open(source, "w", encoding="utf-8") as fh:
        fh.write("(edif)")
    result = service.index_file(
        kb_name="kb_hw",
        record_id=7,
        file_path=source,
        original_name="board.edf",
        department_id="dept_hw",
    )
    design = service.store.load("kb_hw", result.design_id)
    return service, store, design, result


class SplitCandidateTests(unittest.TestCase):
    def test_split_preserves_raw_candidates_per_field_rule(self):
        raw = "GCM155R71C104KA55D/CL05B104KO5VPNC"
        self.assertEqual(
            split_mpn_candidates(raw, "manufacturer_part_number"),
            ["GCM155R71C104KA55D", "CL05B104KO5VPNC"],
        )
        # Internal PNs never split.
        self.assertEqual(split_mpn_candidates("120000181", "internal_part_number"), ["120000181"])


class BuildLinkTests(unittest.TestCase):
    def _identities(self, instances):
        design = type("_D", (), {"instances": instances, "component_identities": [], "files": [], "design_id": "board"})()
        identities = build_component_identities(design)
        design.component_identities = identities
        return design

    def test_exact_unique_match_with_manufacturer_and_revision_is_verified(self):
        instances, _, _ = _design()
        design = self._identities(instances)

        links = ComponentDatasheetLinkIndex(store=None).build_links_for_design(
            design, "gen-1", [_profile()], []
        )

        verified = [link for link in links if link.link_status == "verified"]
        self.assertTrue(verified)
        link = verified[0]
        self.assertEqual(link.refdes, "C100")
        self.assertEqual(link.match_method, "exact_mpn")
        self.assertEqual(link.datasheet_record_id, 42)
        self.assertEqual(link.circuit_generation_id, "gen-1")
        self.assertEqual(link.document_fingerprint, "hash-1")
        self.assertEqual(link.document_revision, "r3")
        self.assertEqual(link.remote_document_id, "remote-42")
        locator = link.source_locator
        self.assertEqual(locator["identifier_namespace"], "manufacturer_part_number")
        self.assertIn("raw_value", locator)

    def test_multi_value_all_candidates_same_document_stays_verified(self):
        # Both split candidates map to the SAME profile → verified.
        profile = _profile(mpn=("GCM155R71C104KA55D", "CL05B104KO5VPNC"))
        instances, _, _ = _design()
        design = self._identities(instances)

        links = ComponentDatasheetLinkIndex(store=None).build_links_for_design(
            design, "gen-1", [profile], []
        )

        statuses = {link.link_status for link in links if link.refdes == "C100"}
        self.assertEqual(statuses, {"verified"})

    def test_cross_candidate_hits_degrade_to_candidates(self):
        p1 = _profile(record_id=1, mpn=("GCM155R71C104KA55D",))
        p2 = _profile(record_id=2, mpn=("CL05B104KO5VPNC",))
        instances, _, _ = _design()
        design = self._identities(instances)

        links = ComponentDatasheetLinkIndex(store=None).build_links_for_design(
            design, "gen-1", [p1, p2], []
        )

        c100 = [link for link in links if link.refdes == "C100"]
        self.assertTrue(c100)
        self.assertTrue(all(link.link_status == "candidate" for link in c100))
        self.assertEqual({link.datasheet_record_id for link in c100}, {1, 2})

    def test_manufacturer_missing_conflict_or_revision_missing_degrade(self):
        for kwargs, reason in (
            ({"manufacturer": ""}, "manufacturer_missing"),
            ({"manufacturer": "Yageo"}, "manufacturer_conflict"),
            ({"revision": ""}, "revision_missing"),
        ):
            with self.subTest(reason=reason):
                instances, _, _ = _design()
                design = self._identities(instances)
                links = ComponentDatasheetLinkIndex(store=None).build_links_for_design(
                    design, "gen-1", [_profile(**kwargs)], []
                )
                self.assertTrue(all(link.link_status == "candidate" for link in links))

    def test_internal_pn_requires_governed_catalog_mapping(self):
        instances = [
            ComponentInstance(refdes="U5", part_number="120000181", properties={"Manufacturer": "Murata"}),
        ]
        design = self._identities(instances)

        unmapped = ComponentDatasheetLinkIndex(store=None).build_links_for_design(
            design, "gen-1", [_profile()], []
        )
        self.assertEqual(unmapped, [])

        entry = DatasheetCatalogEntry(
            entry_id="cat-1",
            catalog_version="2026.08-v2",
            source_file="catalog/pdn.json",
            internal_pn="120000181",
            mpn="STM32F103",
        )
        mapped = ComponentDatasheetLinkIndex(store=None).build_links_for_design(
            design,
            "gen-1",
            [_profile(mpn=("STM32F103",), record_id=9)],
            [entry],
        )
        self.assertEqual(len(mapped), 1)
        self.assertEqual(mapped[0].match_method, "internal_pdn_catalog")
        self.assertEqual(mapped[0].link_status, "verified")


class ReadTimeValidationTests(unittest.TestCase):
    class _FakeDocStore:
        def __init__(self, records):
            self.records = records

        def get_document_by_id_scoped(self, record_id, department_id):

            for item in self.records:
                if item.id == record_id and item.department_id == department_id:
                    return item
            return None

    def _record(self, record_id=42, department_id="dept_hw", content_hash="hash-1"):
        from src.pipelines.document_store import PipelineDocumentRecord

        return PipelineDocumentRecord(
            id=record_id,
            kb_name="kb_hw",
            document_name="ds.pdf",
            original_file_name="ds.pdf",
            dataset_kind="design",
            dataset_id="d",
            document_id=f"remote-{record_id}",
            source_group="设计数据",
            department_id=department_id,
            uploaded_by="u",
            status="parsed",
            processor_kind="ragflow",
            content_hash=content_hash,
            source_version_id="v1",
        )

    def _prepared(self, tmp):
        service, store, design, result = _index(tmp)
        index = service.datasheet_link_index
        generation = circuit_generation_id(design)
        index.save_links("kb_hw", result.design_id, [
            ComponentDatasheetLinkIndex._link(
                design, generation, "C100", _profile(), "GCM155R71C104KA55D",
                "exact_mpn", "verified", 0.95, {},
            )
        ])
        return service, index, result.design_id

    def test_verified_link_survives_when_everything_matches(self):
        with tempfile.TemporaryDirectory() as tmp:

            service, index, design_id = self._prepared(tmp)
            doc_store = self._FakeDocStore([self._record()])
            index.document_store = doc_store

            links = index.get_verified_datasheet_links("kb_hw", "dept_hw", design_id)
            self.assertEqual(len(links), 1)
            self.assertEqual(links[0].datasheet_record_id, 42)

    def test_generation_change_rejects_link_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, index, design_id = self._prepared(tmp)
            index.document_store = self._FakeDocStore([self._record()])

            links = index.get_verified_datasheet_links(
                "kb_hw", "dept_hw", design_id, current_generation_id="new-generation"
            )
            self.assertEqual(links, [])

    def test_replaced_or_deleted_or_cross_department_documents_reject(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, index, design_id = self._prepared(tmp)

            index.document_store = self._FakeDocStore([self._record(content_hash="changed")])
            self.assertEqual(index.get_verified_datasheet_links("kb_hw", "dept_hw", design_id), [])

            index.document_store = self._FakeDocStore([])
            self.assertEqual(index.get_verified_datasheet_links("kb_hw", "dept_hw", design_id), [])

            index.document_store = self._FakeDocStore([self._record(department_id="other")])
            self.assertEqual(index.get_verified_datasheet_links("kb_hw", "dept_hw", design_id), [])

    def test_without_document_store_no_links_are_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, index, design_id = self._prepared(tmp)
            index.document_store = None
            self.assertEqual(index.get_verified_datasheet_links("kb_hw", "dept_hw", design_id), [])


def _prepare(tmp):
    """Module-level helper shared by multiple test classes."""
    return ReadTimeValidationTests()._prepared(tmp)


class _Doc:
    def __init__(self):
        self.queries = []

    def run(self, query, *args, **kwargs):
        self.queries.append(query)
        return []


class _Stub:
    def run(self, *args, **kwargs):
        return []


if __name__ == "__main__":
    unittest.main()
