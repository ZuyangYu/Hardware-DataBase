import gc
import os
import tempfile
import unittest

import config.settings
from src.core.assets import AssetService, AssetSource, classify_asset_source
from src.core.auth import AuthService, ROLE_DEPT_ADMIN


class AssetServiceTests(unittest.TestCase):
    def setUp(self):
        self.old_password = config.settings.AUTH_DEFAULT_ADMIN_PASSWORD
        config.settings.AUTH_DEFAULT_ADMIN_PASSWORD = "StrongTestPassword123!"
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        self.auth = AuthService(db_path=self.db_path)
        system_admin = self.auth.get_user_by_username(config.settings.AUTH_DEFAULT_ADMIN_USERNAME)
        self.department = self.auth.create_department("hardware")
        self.admin = self.auth.create_user_as(system_admin, "hardware_admin", "password123", ROLE_DEPT_ADMIN, self.department.id)
        self.auth.register_knowledge_base("hardware-kb", owner=self.admin)
        self.kb_id = self.auth.get_knowledge_base_id("hardware-kb", department_id=self.department.id)
        self.service = AssetService(db_path=self.db_path)

    def tearDown(self):
        config.settings.AUTH_DEFAULT_ADMIN_PASSWORD = self.old_password
        self.tmp.cleanup()
        gc.collect()

    def test_candidate_acceptance_creates_asset_with_evidence(self):
        candidate, used_llm = self.service.generate_candidate(
            kb_id=self.kb_id,
            department_id=self.department.id,
            kb_name="hardware-kb",
            source=AssetSource(
                file_id="file-1",
                file_name="Controller_Board_RevA.pdf",
                processor_kind="circuit_design",
                excerpt="",
            ),
        )
        self.assertFalse(used_llm)
        self.assertEqual(candidate["status"], "pending")
        self.assertEqual(candidate["asset_type"], "board")

        asset = self.service.accept_candidate(
            candidate_id=candidate["id"],
            kb_id=self.kb_id,
            department_id=self.department.id,
            actor_user_id=self.admin.id,
            overrides={"name": "Controller Board", "model": "CTRL-A"},
        )
        self.assertEqual(asset["name"], "Controller Board")
        self.assertEqual(asset["evidence_count"], 1)

        detail = self.service.get_asset(asset_id=asset["id"], kb_id=self.kb_id, department_id=self.department.id)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["evidence"][0]["file_name"], "Controller_Board_RevA.pdf")
        self.assertEqual(self.service.list_candidates(kb_id=self.kb_id, department_id=self.department.id), [])
        links = self.service.list_file_links(
            kb_id=self.kb_id,
            department_id=self.department.id,
            files=[{"id": "file-1", "name": "Controller_Board_RevA.pdf", "status": "completed"}],
        )
        self.assertEqual(links[0]["link_status"], "linked")
        self.assertEqual(links[0]["asset_name"], "Controller Board")

    def test_assets_are_scoped_to_knowledge_base_and_department(self):
        candidate, _ = self.service.generate_candidate(
            kb_id=self.kb_id,
            department_id=self.department.id,
            kb_name="hardware-kb",
            source=AssetSource(file_id="file-2", file_name="Power_Board.pdf", processor_kind="circuit_design"),
        )
        self.service.accept_candidate(
            candidate_id=candidate["id"], kb_id=self.kb_id, department_id=self.department.id,
            actor_user_id=self.admin.id,
        )
        self.assertEqual(len(self.service.list_assets(kb_id=self.kb_id, department_id=self.department.id)), 1)
        self.assertEqual(self.service.list_assets(kb_id=self.kb_id, department_id=self.department.id + 999), [])

    def test_source_dispatch_keeps_requirements_out_of_asset_candidates(self):
        requirement = classify_asset_source("ADAS_产品硬件需求规格说明书.xlsx", "spreadsheet_table", "table")
        architecture = classify_asset_source("ADAS_产品硬件架构设计说明书.xlsx", "spreadsheet_table", "table")
        circuit = classify_asset_source("ADAS_SCH_TCN2.EDF", "circuit_design", "circuit")

        self.assertEqual(requirement.category, "hardware_requirement")
        self.assertFalse(requirement.asset_eligible)
        rag_requirement = classify_asset_source("HardwareRequirementSpecification.pdf", "document_rag", "document")
        self.assertFalse(rag_requirement.asset_eligible)
        self.assertEqual(architecture.category, "hardware_architecture")
        self.assertEqual(circuit.category, "circuit_design")


if __name__ == "__main__":
    unittest.main()
