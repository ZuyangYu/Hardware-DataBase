"""Task 7: staged identity/coverage backfill and semantic-query gray rollout."""

import json
import os
import tempfile
import unittest

from src.circuit.backfill_semantic_indexes import backfill_semantic_indexes
from src.circuit.index_service import CircuitIndexService
from src.pipelines.document_rag.schemas import RequestContext

from tests.test_circuit_structure_query import (
    _PlainParser,
    _RoleDesignParser,
    _UnavailableVectorIndex,
)


def _harness(tmp):
    events = []
    service = CircuitIndexService(
        storage_root=os.path.join(tmp, "circuits"),
        parser_factory=lambda path, progress_callback=None: (
            _RoleDesignParser() if path.endswith(("board.edf", "legacy.edf")) else _PlainParser()
        ),
        vector_index=_UnavailableVectorIndex(),
        observability_sink=events.append,
    )
    return service, events


def _index(service, tmp, name, rid, dept):
    source = os.path.join(tmp, name)
    with open(source, "w", encoding="utf-8") as fh:
        fh.write("(edif)")
    return service.index_file(
        kb_name="kb_hw",
        record_id=rid,
        file_path=source,
        original_name=name,
        department_id=dept,
    )


CTX = lambda dept="dept_hw": RequestContext(  # noqa: E731
    user_id="alice", metadata={"department_id": dept}
)


class BackfillTests(unittest.TestCase):
    def test_backfill_requires_department_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = _harness(tmp)
            _index(service, tmp, "board.edf", 7, "dept_hw")

            with self.assertRaises(ValueError):
                backfill_semantic_indexes("kb_hw", service=service)

    def test_backfill_processes_only_authorized_department_designs(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = _harness(tmp)
            r1 = _index(service, tmp, "board.edf", 7, "dept_hw")
            r2 = _index(service, tmp, "plain.edf", 8, "dept_b")

            report = backfill_semantic_indexes("kb_hw", department_id="dept_hw", service=service)

            self.assertEqual(report["processed"], [r1.design_id])
            self.assertNotIn(r2.design_id, report["processed"])
            # The out-of-scope design keeps working state and no backfill mark.
            other_meta = service._read_metadata("kb_hw", r2.design_id)
            self.assertNotIn("backfill", other_meta)

    def test_backfill_failure_keeps_previous_generation_and_marks_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = _harness(tmp)
            result = _index(service, tmp, "board.edf", 7, "dept_hw")
            design_id = result.design_id
            good_generation = service._read_metadata("kb_hw", design_id)["generation_id"]

            import src.circuit.index_service as ism

            original = ism.build_component_identities

            def broken(design, *args, **kwargs):
                raise RuntimeError("identity builder exploded")

            ism.build_component_identities = broken
            try:
                report = backfill_semantic_indexes(
                    "kb_hw", department_id="dept_hw", service=service
                )
            finally:
                ism.build_component_identities = original

            failures = report["failures"]
            self.assertIn(design_id, failures)
            self.assertIn("identity builder exploded", failures[design_id])

            loaded = service.store.load("kb_hw", design_id)
            self.assertEqual(len(loaded.instances), len(_RoleDesignParser().parse()[0]))
            self.assertTrue(loaded.component_identities)  # previous generation intact
            self.assertEqual(
                service._read_metadata("kb_hw", design_id)["generation_id"], good_generation
            )
            meta = service._read_metadata("kb_hw", design_id)
            self.assertNotIn("backfill_error", meta)  # publish rollback restored old meta

    def test_legacy_state_gets_coverage_but_roles_and_links_stay_empty_without_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = _harness(tmp)
            # plain.edf carries NO governed role properties at all.
            result = _index(service, tmp, "plain.edf", 9, "dept_b")
            design_dir = service.store.design_dir("kb_hw", result.design_id)

            # Simulate a legacy on-disk state: strip derived fields entirely.
            state_path = os.path.join(design_dir, "circuit_state.json")
            data = json.load(open(state_path, encoding="utf-8"))
            data.pop("component_identities", None)
            data.pop("structure_coverage", None)
            json.dump(data, open(state_path, "w", encoding="utf-8"), ensure_ascii=False)
            # And drop derived link artifacts if any.
            links_path = os.path.join(design_dir, "datasheet_links.json")
            if os.path.exists(links_path):
                os.unlink(links_path)

            report = backfill_semantic_indexes(
                "kb_hw", department_id="dept_b", service=service
            )
            self.assertIn(result.design_id, report["processed"])

            reloaded = service.store.load("kb_hw", result.design_id)
            from src.circuit.models import Availability

            coverage = reloaded.structure_coverage
            self.assertIsNotNone(coverage)
            # Coverage summary produced even without BOM/catalog/datasheets.
            self.assertIn(
                coverage.netlist_connectivity,
                {Availability.AVAILABLE, Availability.UNAVAILABLE},
            )
            self.assertEqual(coverage.module_partition_strategy, "refdes_page_heuristic")
            # No governed role sources → identities exist but roles stay empty.
            self.assertTrue(all(not i.roles for i in reloaded.component_identities))
            # No document store wired → no datasheet links (empty artifact).
            on_disk = service.datasheet_link_index.load_links("kb_hw", result.design_id)
            self.assertEqual(on_disk, [])
            self.assertEqual(report.get("link_counts", {}).get(result.design_id), 0)


class GrayRolloutTests(unittest.TestCase):
    def test_semantic_toggle_disabled_restores_legacy_pipeline(self):
        import src.settings

        with tempfile.TemporaryDirectory() as tmp:
            service, _ = _harness(tmp)
            _index(service, tmp, "board.edf", 7, "dept_hw")

            previous = getattr(src.settings, "CIRCUIT_SEMANTIC_QUERY_ENABLED", True)
            try:
                src.settings.CIRCUIT_SEMANTIC_QUERY_ENABLED = False
                hits = service.query(
                    kb_name="kb_hw",
                    query="SoC 的连接关系",
                    ctx=CTX(),
                    top_k=10,
                )
                # Exact identifier queries stay available while disabled.
                exact_hits = service.query(
                    kb_name="kb_hw", query="U100 连接", ctx=CTX(), top_k=8
                )
            finally:
                src.settings.CIRCUIT_SEMANTIC_QUERY_ENABLED = previous

            kinds = {hit.locator["entity_type"] for hit in hits}
            self.assertNotIn("component_identity", kinds)
            # Legacy keyword paths still answer through instances/modules.
            self.assertTrue(kinds & {"instance", "module", "net"})
            self.assertTrue(any(h.locator["entity_type"] == "pin_mapping" for h in exact_hits))

    def test_exact_queries_unaffected_by_toggle(self):
        import src.settings

        with tempfile.TemporaryDirectory() as tmp:
            service, _ = _harness(tmp)
            _index(service, tmp, "board.edf", 7, "dept_hw")
            ctx = CTX()

            for enabled in (True, False):
                with self.subTest(enabled=enabled):
                    previous = getattr(src.settings, "CIRCUIT_SEMANTIC_QUERY_ENABLED", True)
                    src.settings.CIRCUIT_SEMANTIC_QUERY_ENABLED = enabled
                    try:
                        hits = service.query(
                            kb_name="kb_hw", query="U100 连接", ctx=ctx, top_k=8
                        )
                    finally:
                        src.settings.CIRCUIT_SEMANTIC_QUERY_ENABLED = previous
                    self.assertTrue(any(h.locator["entity_type"] == "pin_mapping" for h in hits))


if __name__ == "__main__":
    unittest.main()
