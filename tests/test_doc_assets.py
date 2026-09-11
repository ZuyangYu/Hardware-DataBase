"""Tests for the document asset service (version chain, lifecycle, links)."""
from __future__ import annotations

import gc
import os
import tempfile
import unittest

import src.settings
from src.core.auth import AuthService, ROLE_EMPLOYEE
from src.core.doc_assets import (
    DocumentAssetError,
    DocumentAssetService,
)


class DocAssetServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_password = src.settings.AUTH_DEFAULT_ADMIN_PASSWORD
        src.settings.AUTH_DEFAULT_ADMIN_PASSWORD = "StrongTestPassword123!"
        self._tmp = tempfile.TemporaryDirectory(prefix="_test_doc_assets_")
        self.db_path = os.path.join(self._tmp.name, "doc_assets_test.db")
        self.auth = AuthService(db_path=self.db_path)
        system_admin = self.auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
        self.department = self.auth.create_department("hardware")
        self.admin = self.auth.create_user_as(
            system_admin, "hardware_admin", "password123", ROLE_EMPLOYEE, self.department.id
        )
        self.auth.register_knowledge_base("KB-A", owner=self.admin)
        self.auth.register_knowledge_base("KB-B", owner=self.admin)
        self.kb_a = self.auth.get_knowledge_base_id("KB-A", department_id=self.department.id)
        self.kb_b = self.auth.get_knowledge_base_id("KB-B", department_id=self.department.id)
        self.service = DocumentAssetService(db_path=self.db_path)
        self.dept_id = self.department.id
        self.actor = self.admin.id

    def tearDown(self) -> None:
        src.settings.AUTH_DEFAULT_ADMIN_PASSWORD = self.old_password
        self._tmp.cleanup()
        gc.collect()

    def _create(self, *, title: str = "控制器设计说明书", kb_id: int | None = None, kb_name: str = "KB-A", **kwargs):
        return self.service.create_asset(
            kb_id=kb_id if kb_id is not None else self.kb_a,
            kb_name=kb_name,
            department_id=self.dept_id,
            actor_user_id=self.actor,
            title=title,
            doc_no="HW-DESIGN-001",
            category=kwargs.pop("category", "design_doc"),
            project="AAA",
            tags=["mcu", "tc367"],
            **kwargs,
        )

    def test_create_without_version_starts_in_draft(self) -> None:
        asset = self._create()
        self.assertEqual(asset["lifecycle_status"], "draft")
        self.assertIsNone(asset["current_version_id"])
        self.assertEqual(asset["version_count"], 0)
        self.assertEqual(asset["tags"], ["mcu", "tc367"])

    def test_version_registered_is_effective_on_arrival(self) -> None:
        asset = self._create(
            initial_version={"file_id": "f1", "file_name": "a.docx", "content_hash": "h1", "parse_status": "completed"}
        )
        self.assertEqual(asset["lifecycle_status"], "effective")
        self.assertEqual(asset["effective_version_no"], 1)
        detail = self.service.get_asset(asset_id=asset["id"], kb_id=self.kb_a, department_id=self.dept_id)
        self.assertEqual(detail["versions"][0]["state"], "effective")
        self.assertIn("created", [e["event"] for e in detail["events"]])

    def test_new_version_immediately_supersedes(self) -> None:
        asset = self._create(
            initial_version={"file_id": "f1", "file_name": "a.docx", "content_hash": "h1"}
        )
        updated = self.service.add_version(
            asset_id=asset["id"], kb_id=self.kb_a, department_id=self.dept_id, actor_user_id=self.actor,
            file_id="f2", file_name="a-v2.docx", content_hash="h2", note="修订引言",
        )
        self.assertEqual(updated["lifecycle_status"], "effective")
        self.assertEqual(updated["effective_version_no"], 2)
        detail = self.service.get_asset(asset_id=asset["id"], kb_id=self.kb_a, department_id=self.dept_id)
        states = {v["version_no"]: v["state"] for v in detail["versions"]}
        self.assertEqual(states[1], "superseded")
        self.assertEqual(states[2], "effective")
        events = [e["event"] for e in detail["events"]]
        self.assertIn("superseded", events)

    def test_duplicate_file_and_content_versions_rejected(self) -> None:
        asset = self._create(
            initial_version={"file_id": "f1", "file_name": "a.docx", "content_hash": "same"}
        )
        with self.assertRaises(DocumentAssetError):
            self.service.add_version(
                asset_id=asset["id"], kb_id=self.kb_a, department_id=self.dept_id, actor_user_id=self.actor,
                file_id="f1", file_name="a.docx",
            )
        with self.assertRaises(DocumentAssetError):
            self.service.add_version(
                asset_id=asset["id"], kb_id=self.kb_a, department_id=self.dept_id, actor_user_id=self.actor,
                file_id="f9", file_name="copy.docx", content_hash="same",
            )

    def test_version_file_names_and_file_deletion_endgame(self) -> None:
        from src.core.doc_assets import sync_file_deleted

        asset = self._create(initial_version={"file_id": "f1", "file_name": "a.docx"})
        self.service.add_version(
            asset_id=asset["id"], kb_id=self.kb_a, department_id=self.dept_id, actor_user_id=self.actor,
            file_id="f2", file_name="a-v2.docx",
        )
        names = self.service.version_file_names(asset_id=asset["id"], kb_id=self.kb_a, department_id=self.dept_id)
        self.assertEqual(sorted(names), ["a-v2.docx", "a.docx"])
        # 删生效版本 v2 → v1 回归生效
        old_db = src.settings.AUTH_DB_PATH
        src.settings.AUTH_DB_PATH = self.db_path
        try:
            sync_file_deleted(kb_id=self.kb_a, file_id="f2")
        finally:
            src.settings.AUTH_DB_PATH = old_db
        detail = self.service.get_asset(asset_id=asset["id"], kb_id=self.kb_a, department_id=self.dept_id)
        self.assertEqual(detail["versions"][0]["state"], "effective")
        # 再删 v1 → 文件删光, 资产记录随之删除(账随物走)
        src.settings.AUTH_DB_PATH = self.db_path
        try:
            sync_file_deleted(kb_id=self.kb_a, file_id="f1")
        finally:
            src.settings.AUTH_DB_PATH = old_db
        self.assertIsNone(
            self.service.get_asset(asset_id=asset["id"], kb_id=self.kb_a, department_id=self.dept_id)
        )

    def test_update_info_fields_and_audit(self) -> None:
        asset = self._create()
        updated = self.service.update_info(
            asset_id=asset["id"], kb_id=self.kb_a, department_id=self.dept_id, actor_user_id=self.actor,
            fields={"title": "控制器设计说明书 V2", "project": "BBB", "tags": ["v2"]},
        )
        self.assertEqual(updated["title"], "控制器设计说明书 V2")
        self.assertEqual(updated["project"], "BBB")
        self.assertEqual(updated["tags"], ["v2"])
        detail = self.service.get_asset(asset_id=asset["id"], kb_id=self.kb_a, department_id=self.dept_id)
        self.assertIn("info_updated", [e["event"] for e in detail["events"]])
        with self.assertRaises(DocumentAssetError):
            self.service.update_info(
                asset_id=asset["id"], kb_id=self.kb_a, department_id=self.dept_id, actor_user_id=self.actor,
                fields={"title": " "},
            )
        with self.assertRaises(DocumentAssetError):
            self.service.update_info(
                asset_id=asset["id"], kb_id=self.kb_a, department_id=self.dept_id, actor_user_id=self.actor,
                fields={"hack": 1},
            )

    def test_links_validation_and_removal(self) -> None:
        a = self._create(title="设计文档")
        b = self._create(title="网表文件", kb_id=self.kb_b, kb_name="KB-B")
        link = self.service.add_link(
            department_id=self.dept_id, from_asset_id=a["id"], to_asset_id=b["id"],
            rel_type="companion", actor_user_id=self.actor,
        )
        self.assertEqual(link["status"], "confirmed")
        with self.assertRaises(DocumentAssetError):
            self.service.add_link(
                department_id=self.dept_id, from_asset_id=a["id"], to_asset_id=b["id"],
                rel_type="companion", actor_user_id=self.actor,
            )
        with self.assertRaises(DocumentAssetError):
            self.service.add_link(
                department_id=self.dept_id, from_asset_id=a["id"], to_asset_id=a["id"],
                rel_type="references", actor_user_id=self.actor,
            )
        with self.assertRaises(DocumentAssetError):
            self.service.add_link(
                department_id=999999, from_asset_id=a["id"], to_asset_id=b["id"],
                rel_type="references", actor_user_id=self.actor,
            )
        detail = self.service.get_asset(asset_id=a["id"], kb_id=self.kb_a, department_id=self.dept_id)
        self.assertEqual(len(detail["links_out"]), 1)
        other = self.service.get_asset(asset_id=b["id"], kb_id=self.kb_b, department_id=self.dept_id)
        self.assertEqual(len(other["links_in"]), 1)
        self.assertEqual(other["links_in"][0]["to_title"], "设计文档")
        self.assertTrue(self.service.remove_link(department_id=self.dept_id, link_id=link["id"]))
        self.assertFalse(self.service.remove_link(department_id=self.dept_id, link_id=link["id"]))

    def test_scope_isolation_between_kb_and_department(self) -> None:
        asset = self._create()
        with self.assertRaises(LookupError):
            self.service.update_info(
                asset_id=asset["id"], kb_id=999999, department_id=self.dept_id,
                actor_user_id=self.actor, fields={"title": "跨域"},
            )
        with self.assertRaises(LookupError):
            self.service.update_info(
                asset_id=asset["id"], kb_id=self.kb_a, department_id=999999,
                actor_user_id=self.actor, fields={"title": "跨域"},
            )
        foreign = self.service.get_asset(asset_id=asset["id"], kb_id=999999, department_id=self.dept_id)
        self.assertIsNone(foreign)

    def test_list_filters(self) -> None:
        self._create(title="设计说明书")
        effective = self._create(
            title="测试报告", category="test_report",
            initial_version={"file_id": "f1", "file_name": "report.xlsx"},
        )
        rows = self.service.list_assets(kb_id=self.kb_a, department_id=self.dept_id, status="effective")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "测试报告")
        by_query = self.service.list_assets(kb_id=self.kb_a, department_id=self.dept_id, query="说明书")
        self.assertEqual(len(by_query), 1)
        other_kb = self.service.list_assets(kb_id=self.kb_b, department_id=self.dept_id)
        self.assertEqual(other_kb, [])
        self.assertEqual(effective["effective_version_no"], 1)

    def test_backfill_shadow_assets_idempotent(self) -> None:
        files = [
            {"file_id": "f1", "file_name": "ADAS_产品硬件需求规格说明书.xlsx", "processor_kind": "spreadsheet_table",
             "content_hash": "h1", "parse_status": "completed"},
            {"file_id": "f2", "file_name": "ADAS_SCH_TCN2.EDF", "processor_kind": "circuit_design",
             "content_hash": "h2", "parse_status": "completed"},
        ]
        result = self.service.backfill_shadow_assets(
            kb_id=self.kb_a, kb_name="KB-A", department_id=self.dept_id,
            actor_user_id=self.actor, files=files,
        )
        self.assertEqual(result, {"created": 2, "skipped": 0})
        rows = self.service.list_assets(kb_id=self.kb_a, department_id=self.dept_id)
        self.assertEqual(len(rows), 2)
        by_title = {r["title"]: r for r in rows}
        self.assertIn("ADAS_产品硬件需求规格说明书", by_title)
        self.assertEqual(by_title["ADAS_产品硬件需求规格说明书"]["category"], "requirement")
        self.assertEqual(by_title["ADAS_SCH_TCN2"]["category"], "netlist")
        self.assertEqual(by_title["ADAS_SCH_TCN2"]["lifecycle_status"], "effective")
        self.assertEqual(by_title["ADAS_SCH_TCN2"]["effective_version_no"], 1)
        again = self.service.backfill_shadow_assets(
            kb_id=self.kb_a, kb_name="KB-A", department_id=self.dept_id,
            actor_user_id=self.actor, files=files,
        )
        self.assertEqual(again, {"created": 0, "skipped": 2})

    def test_claim_strips_shadow_tag(self) -> None:
        self.service.backfill_shadow_assets(
            kb_id=self.kb_a, kb_name="KB-A", department_id=self.dept_id,
            actor_user_id=self.actor,
            files=[{"file_id": "f1", "file_name": "ADAS_ICD.xlsx", "processor_kind": "spreadsheet_table"}],
        )
        asset = self.service.list_assets(kb_id=self.kb_a, department_id=self.dept_id)[0]
        self.assertIn("影子档", asset["tags"])
        updated = self.service.update_info(
            asset_id=asset["id"], kb_id=self.kb_a, department_id=self.dept_id,
            actor_user_id=self.actor,
            fields={"title": "ADAS 接口控制文档", "category": "other", "project": "AAA"},
        )
        self.assertNotIn("影子档", updated["tags"])
        self.assertEqual(updated["title"], "ADAS 接口控制文档")

    def test_absorb_pure_shadow_but_leave_claimed_assets(self) -> None:
        main = self._create(title="主资产", initial_version={"file_id": "m1", "file_name": "main.docx"})
        self.service.backfill_shadow_assets(
            kb_id=self.kb_a, kb_name="KB-A", department_id=self.dept_id,
            actor_user_id=self.actor,
            files=[{"file_id": "s1", "file_name": "same_doc_v2.docx", "processor_kind": "ragflow"}],
        )
        shadow = self.service.list_assets(kb_id=self.kb_a, department_id=self.dept_id, query="same_doc_v2")[0]
        absorbed = self.service.absorb_shadow_for_file(
            kb_id=self.kb_a, department_id=self.dept_id, file_id="s1", exclude_asset_id=main["id"]
        )
        self.assertEqual(absorbed, {"id": shadow["id"], "title": "same_doc_v2"})
        self.assertIsNone(
            self.service.get_asset(asset_id=shadow["id"], kb_id=self.kb_a, department_id=self.dept_id)
        )
        claimed = self.service.create_asset(
            kb_id=self.kb_a, kb_name="KB-A", department_id=self.dept_id, actor_user_id=self.actor,
            title="人工建的档", initial_version={"file_id": "s1", "file_name": "same_doc_v2.docx"},
        )
        self.assertIsNone(
            self.service.absorb_shadow_for_file(
                kb_id=self.kb_a, department_id=self.dept_id, file_id="s1", exclude_asset_id=main["id"]
            )
        )
        still = self.service.get_asset(asset_id=claimed["id"], kb_id=self.kb_a, department_id=self.dept_id)
        self.assertEqual(still["lifecycle_status"], "effective")

    def test_backfill_skips_same_content_different_file(self) -> None:
        self.service.backfill_shadow_assets(
            kb_id=self.kb_a, kb_name="KB-A", department_id=self.dept_id,
            actor_user_id=self.actor,
            files=[{"file_id": "f1", "file_name": "a.docx", "processor_kind": "ragflow",
                    "content_hash": "same-content"}],
        )
        result = self.service.backfill_shadow_assets(
            kb_id=self.kb_a, kb_name="KB-A", department_id=self.dept_id,
            actor_user_id=self.actor,
            files=[{"file_id": "f2", "file_name": "a-重新上传.docx", "processor_kind": "ragflow",
                    "content_hash": "same-content"}],
        )
        self.assertEqual(result, {"created": 0, "skipped": 1})
        self.assertEqual(len(self.service.list_assets(kb_id=self.kb_a, department_id=self.dept_id)), 1)

    def test_parse_completion_hook_creates_shadow_asset_once(self) -> None:
        from src.pipelines.document_store_sqlite import PipelineDocumentStore

        parse_db = os.path.join(self._tmp.name, "pipeline_documents.db")
        store = PipelineDocumentStore(db_path=parse_db)
        store.upsert_document(
            kb_name="KB-A",
            document_name="ADAS_HWDebug.xlsx",
            dataset_kind="table",
            dataset_id="ds-1",
            document_id="remote-1",
            source_group="测试数据",
            department_id=str(self.department.id),
            uploaded_by="hw_admin",
            kb_id=self.kb_a,
            status="processing",
            original_file_name="ADAS_HWDebug.xlsx",
            content_hash="hash-1",
            processor_kind="spreadsheet_table",
        )
        record = store.get_document("KB-A", "ADAS_HWDebug.xlsx", department_id=self.department.id)
        self.assertIsNotNone(record)
        record_id = record.id
        from src.core.doc_assets import ensure_shadow_asset_for_record

        old_db = src.settings.AUTH_DB_PATH
        src.settings.AUTH_DB_PATH = self.db_path
        try:
            self.assertFalse(ensure_shadow_asset_for_record(record_id, store=store))
            store.update_document_status_by_id(record_id, "completed")
            self.assertTrue(ensure_shadow_asset_for_record(record_id, store=store))
        finally:
            src.settings.AUTH_DB_PATH = old_db
        rows = self.service.list_assets(kb_id=self.kb_a, department_id=self.dept_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "ADAS_HWDebug")
        self.assertEqual(rows[0]["lifecycle_status"], "effective")
        detail = self.service.get_asset(asset_id=rows[0]["id"], kb_id=self.kb_a, department_id=self.dept_id)
        self.assertEqual(detail["versions"][0]["file_id"], str(record_id))
        store.update_document_status_by_id(record_id, "completed")
        rows = self.service.list_assets(kb_id=self.kb_a, department_id=self.dept_id)
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
