"""Tests for the WeKnora-style wiki service (pipeline, links, revisions)."""
from __future__ import annotations

import gc
import os
import sqlite3
import tempfile
import unittest

import src.settings
from src.core.auth import AuthService, ROLE_EMPLOYEE
from src.core.wiki import WikiService


class WikiServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_password = src.settings.AUTH_DEFAULT_ADMIN_PASSWORD
        src.settings.AUTH_DEFAULT_ADMIN_PASSWORD = "StrongTestPassword123!"
        self._tmp = tempfile.TemporaryDirectory(prefix="_test_wiki_")
        self.db_path = os.path.join(self._tmp.name, "wiki_test.db")
        self.auth = AuthService(db_path=self.db_path)
        system_admin = self.auth.get_user_by_username(src.settings.AUTH_DEFAULT_ADMIN_USERNAME)
        self.department = self.auth.create_department("hardware")
        self.admin = self.auth.create_user_as(
            system_admin, "hardware_admin", "password123", ROLE_EMPLOYEE, self.department.id
        )
        self.auth.register_knowledge_base("KB-A", owner=self.admin)
        self.kb_a = self.auth.get_knowledge_base_id("KB-A", department_id=self.department.id)
        self.service = WikiService(db_path=self.db_path)

    def tearDown(self) -> None:
        src.settings.AUTH_DEFAULT_ADMIN_PASSWORD = self.old_password
        self._tmp.cleanup()
        gc.collect()

    @staticmethod
    def _fake_chat(responses: dict[str, str]):
        def chat_fn(prompt: str) -> str:
            for marker, reply in responses.items():
                if marker in prompt:
                    return reply
            return responses.get("_default", "{}")
        return chat_fn

    CANDIDATE_JSON = (
        '{"entities": ['
        '{"name": "TCAN1145DMTRQ1", "slug": "entity/tcan1145dmtrq1", "aliases": ["TCAN1145"], "description": "CAN 收发器"},'
        '{"name": "R1618", "slug": "entity/r1618", "aliases": [], "description": "调试拆除的电阻"}],'
        '"concepts": ['
        '{"name": "看门狗", "slug": "concept/watchdog", "aliases": ["watchdog"], "description": "MCU 看门狗机制"}]}'
    )
    CITATION_JSON = (
        '{"citations": {"entity/tcan1145dmtrq1": ["c000"], "entity/r1618": ["c000"], "concept/watchdog": []}, '
        '"new_slugs": [{"type": "entity", "name": "TP2449", "slug": "entity/tp2449", '
        '"aliases": [], "description": "SPI 波形测试点", "source_chunks": ["c000", "c999"]}]}'
    )
    PAGE_REPLY = "## 概述\nTCAN1145DMTRQ1 是 CAN 收发器, 见 [[entity/r1618]]。\n\n## 来源\n[1] 测试记录"

    def test_pipeline_distills_candidates_into_linked_pages(self) -> None:
        chat_fn = self._fake_chat({
            "抽取": self.CANDIDATE_JSON,
            "候选词条": self.CITATION_JSON,
            "_default": self.PAGE_REPLY,
        })
        stats = self.service.ingest(
            kb_id=self.kb_a, kb_name="KB-A", department_id=self.department.id,
            documents=[{"doc_id": "34", "name": "HWDebug.xlsx", "chunks": [
                "SPI 波形测试: TP2449 测试点, 使用 TCAN1145DMTRQ1 收发器; 调试中拆除 R1618; 验证看门狗功能。",
            ]}],
            chat_fn=chat_fn, granularity="standard",
        )
        self.assertEqual(stats["documents"], 1)
        self.assertGreaterEqual(stats["pages_written"], 3)
        # 未知 handle c999 被丢弃
        self.assertEqual(stats["errors"], [])
        pages = {p["slug"]: p for p in self.service.list_pages(kb_id=self.kb_a, department_id=self.department.id)}
        self.assertIn("entity/tcan1145dmtrq1", pages)
        self.assertIn("entity/tp2449", pages)
        # concept/watchdog 无实质引用 → 跳过, 不产空壳页
        self.assertNotIn("concept/watchdog", pages)
        self.assertIn("index", pages)
        # 引文块级溯源
        detail = self.service.get_page(kb_id=self.kb_a, department_id=self.department.id, slug="entity/tcan1145dmtrq1")
        self.assertEqual(detail["chunk_refs"], ["entity/tcan1145dmtrq1#chunk-1"])
        self.assertEqual(detail["source_refs"], ["34|HWDebug.xlsx"])
        # 互链: 页面正文提及 [[entity/r1618]] → 出链; r1618 反链
        self.assertIn("entity/r1618", detail["out_links"])
        other = self.service.get_page(kb_id=self.kb_a, department_id=self.department.id, slug="entity/r1618")
        self.assertIn("entity/tcan1145dmtrq1", other["in_links"])

    def test_unknown_handles_dropped_and_citation_slugs_filtered(self) -> None:
        chat_fn = self._fake_chat({
            "抽取": self.CANDIDATE_JSON,
            "候选词条": '{"citations": {"entity/tcan1145dmtrq1": ["c005", "c000"]}, "new_slugs": []}',
            "_default": self.PAGE_REPLY,
        })
        self.service.ingest(
            kb_id=self.kb_a, kb_name="KB-A", department_id=self.department.id,
            documents=[{"doc_id": "1", "name": "a.txt", "chunks": ["TCAN1145DMTRQ1 收发器内容"]}],
            chat_fn=chat_fn, granularity="standard",
        )
        detail = self.service.get_page(kb_id=self.kb_a, department_id=self.department.id, slug="entity/tcan1145dmtrq1")
        self.assertEqual(detail["chunk_refs"], ["entity/tcan1145dmtrq1#chunk-1"])

    def test_citation_material_keeps_original_chunk_mapping(self) -> None:
        """回归: 引用批次曾用"全文重新分段"的索引去截原始块, 导致张冠李戴。"""

        def chat_fn(prompt: str) -> str:
            if "抽取" in prompt:
                return self.CANDIDATE_JSON
            if "候选词条" in prompt:
                return '{"citations": {"entity/tcan1145dmtrq1": ["c000"]}, "new_slugs": []}'
            return "MATERIAL>>>" + prompt

        docs = [{"doc_id": "9", "name": "two.xlsx", "chunks": [
            "封面信息 " + "A" * 7000,
            "TCAN1145DMTRQ1 的真实材料在这里",
        ]}]
        self.service.ingest(
            kb_id=self.kb_a, kb_name="KB-A", department_id=self.department.id,
            documents=docs, chat_fn=chat_fn, granularity="standard",
        )
        page = self.service.get_page(
            kb_id=self.kb_a, department_id=self.department.id, slug="entity/tcan1145dmtrq1")
        assert page is not None
        # c000 批次包含两个原始块 → 第二块的内容必须进入页面材料
        self.assertIn("TCAN1145DMTRQ1 的真实材料在这里", page["content"])

    def test_ingest_reports_progress(self) -> None:
        chat_fn = self._fake_chat({
            "抽取": self.CANDIDATE_JSON,
            "候选词条": self.CITATION_JSON,
            "_default": self.PAGE_REPLY,
        })
        messages: list[str] = []
        self.service.ingest(
            kb_id=self.kb_a, kb_name="KB-A", department_id=self.department.id,
            documents=[{"doc_id": "34", "name": "HWDebug.xlsx", "chunks": ["TCAN1145DMTRQ1 收发器内容"]}],
            chat_fn=chat_fn, granularity="standard", progress_fn=messages.append,
        )
        self.assertTrue(any("蒸馏 1/1" in m for m in messages), messages)
        self.assertTrue(any(m.startswith("生成词条页") for m in messages), messages)
        self.assertTrue(any(m.startswith("写入词条") for m in messages), messages)

        # 进度回调抛错不能影响蒸馏
        def broken(_message: str) -> None:
            raise RuntimeError("progress broken")

        stats = self.service.ingest(
            kb_id=self.kb_a, kb_name="KB-A", department_id=self.department.id,
            documents=[{"doc_id": "35", "name": "b.xlsx", "chunks": ["TCAN1145DMTRQ1 收发器内容"]}],
            chat_fn=chat_fn, granularity="standard", progress_fn=broken,
        )
        self.assertEqual(stats["errors"], [])

    def test_manual_edit_snapshots_revision_and_reverts(self) -> None:
        created = self.service.upsert_manual(
            kb_id=self.kb_a, department_id=self.department.id, kb_name="KB-A",
            actor_user_id=self.admin.id, title="TCAN1145", page_type="entity",
            content="v1 内容", summary="v1",
        )
        self.assertEqual(created["version"], 1)
        updated = self.service.update_page(
            kb_id=self.kb_a, department_id=self.department.id, slug=created["slug"],
            actor_user_id=self.admin.id, fields={"content": "v2 内容"},
        )
        self.assertEqual(updated["version"], 2)
        detail = self.service.get_page(kb_id=self.kb_a, department_id=self.department.id, slug=created["slug"])
        self.assertEqual(len(detail["revisions"]), 1)
        self.assertEqual(detail["revisions"][0]["version"], 1)
        reverted = self.service.revert_page(
            kb_id=self.kb_a, department_id=self.department.id, slug=created["slug"],
            version=1, actor_user_id=self.admin.id,
        )
        self.assertIn("v1 内容", reverted["content"])
        self.assertEqual(reverted["version"], 3)

    def test_optimistic_version_guard(self) -> None:
        created = self.service.upsert_manual(
            kb_id=self.kb_a, department_id=self.department.id, kb_name="KB-A",
            actor_user_id=self.admin.id, title="看门狗", page_type="concept", content="内容",
        )
        self.service.update_page(
            kb_id=self.kb_a, department_id=self.department.id, slug=created["slug"],
            actor_user_id=self.admin.id, fields={"content": "其他人改过"},
        )
        from src.core.wiki import WikiError
        with self.assertRaises(WikiError):
            self.service.update_page(
                kb_id=self.kb_a, department_id=self.department.id, slug=created["slug"],
                actor_user_id=self.admin.id, fields={"content": "过期客户端"},
                expect_version=1,
            )

    def test_dead_links_cleaned(self) -> None:
        self.service.upsert_manual(
            kb_id=self.kb_a, department_id=self.department.id, kb_name="KB-A",
            actor_user_id=self.admin.id, title="R1618", page_type="entity",
            content="提及 [[entity/ghost-page]]。",
        )
        removed = self.service.cleanup_dead_links(kb_id=self.kb_a)
        self.assertGreaterEqual(removed, 1)
        detail = self.service.get_page(kb_id=self.kb_a, department_id=self.department.id, slug="entity/r1618")
        self.assertNotIn("[[entity/ghost-page]]", detail["content"])
        self.assertNotIn("entity/ghost-page", detail["out_links"])

    def test_scope_isolation(self) -> None:
        self.service.upsert_manual(
            kb_id=self.kb_a, department_id=self.department.id, kb_name="KB-A",
            actor_user_id=self.admin.id, title="TCAN1145", page_type="entity", content="x",
        )
        self.assertEqual(self.service.list_pages(kb_id=999999, department_id=self.department.id), [])
        self.assertIsNone(
            self.service.get_page(kb_id=self.kb_a, department_id=999999, slug="entity/tcan1145")
        )

    def test_reingest_skips_user_edited_pages(self) -> None:
        docs = [{"doc_id": "34", "name": "HWDebug.xlsx", "chunks": [
            "SPI 波形测试: TP2449 测试点, 使用 TCAN1145DMTRQ1 收发器; 调试中拆除 R1618。",
        ]}]
        first = self._fake_chat({
            "抽取": self.CANDIDATE_JSON,
            "候选词条": self.CITATION_JSON,
            "_default": self.PAGE_REPLY,
        })
        self.service.ingest(
            kb_id=self.kb_a, kb_name="KB-A", department_id=self.department.id,
            documents=docs, chat_fn=first, granularity="standard",
        )
        # 人工修正其中一页(署名 user → 锁定)
        self.service.update_page(
            kb_id=self.kb_a, department_id=self.department.id, slug="entity/tcan1145dmtrq1",
            actor_user_id=self.admin.id, fields={"content": "人工修正内容"},
        )
        second = self._fake_chat({
            "抽取": self.CANDIDATE_JSON,
            "候选词条": self.CITATION_JSON,
            "_default": "NEW PIPELINE CONTENT [[entity/r1618]]",
        })
        stats = self.service.ingest(
            kb_id=self.kb_a, kb_name="KB-A", department_id=self.department.id,
            documents=docs, chat_fn=second, granularity="standard",
        )
        # tcan1145 + r1618? 不: 只有人工改过的 tcan1145 被跳过; r1618 是管线页, 被覆盖
        self.assertEqual(stats["skipped_user_edited"], 1)
        locked = self.service.get_page(
            kb_id=self.kb_a, department_id=self.department.id, slug="entity/tcan1145dmtrq1")
        assert locked is not None
        self.assertEqual(locked["content"], "人工修正内容")
        # 管线页正常被新内容覆盖
        summary = self.service.get_page(
            kb_id=self.kb_a, department_id=self.department.id, slug="summary/34")
        assert summary is not None
        self.assertIn("NEW PIPELINE CONTENT", summary["content"])


class SpreadsheetBlocksTests(unittest.TestCase):
    """_spreadsheet_blocks 三层取材过滤: 样板 sheet 名 / 跨文档重复块 / 低密度 + 审批行。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="_test_wiki_blocks_")
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.kb_name = "KB-X"
        db_dir = os.path.join(
            "storage", "table_indexes", "departments", "7", "kbs", self.kb_name)
        os.makedirs(db_dir)
        self.db_path = os.path.join(db_dir, "table_indexes.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE table_sheets (record_id INTEGER, sheet_name TEXT, non_empty_cell_count INTEGER)")
        conn.execute(
            "CREATE TABLE table_text_blocks (record_id INTEGER, sheet_name TEXT, block_index INTEGER, block_text TEXT)")
        # doc 1: 数据大表(高密度) + 封面样板 + 审批行块
        conn.execute("INSERT INTO table_sheets VALUES (1, '硬件需求', 4895)")
        conn.execute("INSERT INTO table_sheets VALUES (1, '封面Cover', 12)")
        conn.execute("INSERT INTO table_sheets VALUES (1, '备注', 3)")
        conn.execute("INSERT INTO table_sheets VALUES (1, 'Example', 86)")
        conn.execute("INSERT INTO table_sheets VALUES (1, 'Change History', 30)")
        conn.execute("INSERT INTO table_text_blocks VALUES (1, '硬件需求', 0, 'Sheet: 硬件需求 Rows: 2-7 需求ID REQ-001 功能描述')")
        conn.execute("INSERT INTO table_text_blocks VALUES (1, '硬件需求', 1, 'Sheet: 硬件需求 Rows: 8-14 批准人/Approver: 张三 保存期限: 产品生命周期')")
        conn.execute("INSERT INTO table_text_blocks VALUES (1, '硬件需求', 2, 'Sheet: 硬件需求 Rows: 15-20 项目适配说明: 按实际项目裁剪本页')")
        conn.execute("INSERT INTO table_text_blocks VALUES (1, '封面Cover', 0, 'Sheet: 封面Cover Rows: 1-5 产品硬件方案设计说明书 密级: 内部')")
        conn.execute("INSERT INTO table_text_blocks VALUES (1, '备注', 0, 'Sheet: 备注 Rows: 1-2 口头说明')")
        conn.execute("INSERT INTO table_text_blocks VALUES (1, 'Example', 0, 'Sheet: Example Rows: 2-34 产品名称/内部编号: TBOX基础型/600600653')")
        conn.execute("INSERT INTO table_text_blocks VALUES (1, 'Change History', 0, 'Sheet: Change History Rows: 1-6 变更历史 Change History 序号 版本')")
        # doc 2/3/4: 同一份模板使用说明块(跨文档重复 → 通用样板识别)
        for doc_id in (2, 3, 4):
            conn.execute("INSERT INTO table_sheets VALUES (?, '数据表', 500)", (doc_id,))
            conn.execute(
                "INSERT INTO table_text_blocks VALUES (?, '数据表', 0, 'Sheet: 数据表 Rows: 1-3 黑色斜体文字为参考样例')",
                (doc_id,))
            conn.execute(
                "INSERT INTO table_text_blocks VALUES (?, '数据表', 1, ?)",
                (doc_id, f"Sheet: 数据表 Rows: 4-6 项目{doc_id}真实数据"))
        conn.commit()
        conn.close()
        self.service = WikiService(db_path=os.path.join(self._tmp.name, "wiki.db"))

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        self._tmp.cleanup()
        gc.collect()

    def test_template_sheets_and_low_density_excluded(self) -> None:
        blocks = self.service._spreadsheet_blocks("1", kb_id=9, department_id=7, kb_name=self.kb_name)
        joined = "\n".join(blocks)
        self.assertIn("REQ-001", joined)
        self.assertNotIn("封面Cover", joined)
        self.assertNotIn("口头说明", joined)

    def test_approval_rows_excluded(self) -> None:
        blocks = self.service._spreadsheet_blocks("1", kb_id=9, department_id=7, kb_name=self.kb_name)
        self.assertNotIn("批准人", "\n".join(blocks))

    def test_example_change_history_and_instruction_blocks_excluded(self) -> None:
        blocks = self.service._spreadsheet_blocks("1", kb_id=9, department_id=7, kb_name=self.kb_name)
        joined = "\n".join(blocks)
        self.assertNotIn("TBOX基础型", joined)
        self.assertNotIn("变更历史", joined)
        self.assertNotIn("项目适配说明", joined)

    def test_blocks_sampled_across_long_sheet_not_only_header(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO table_sheets VALUES (5, '长表', 500)")
        for i in range(12):
            conn.execute(
                "INSERT INTO table_text_blocks VALUES (5, '长表', ?, ?)",
                (i, f"Sheet: 长表 Rows: {i * 5 + 1}-{i * 5 + 5} 条目{i}真实数据"),
            )
        conn.commit()
        conn.close()
        blocks = self.service._spreadsheet_blocks("5", kb_id=9, department_id=7, kb_name=self.kb_name)
        self.assertIn("条目10", "\n".join(blocks))

    def test_cross_doc_duplicate_blocks_are_boilerplate(self) -> None:
        blocks = self.service._spreadsheet_blocks("2", kb_id=9, department_id=7, kb_name=self.kb_name)
        joined = "\n".join(blocks)
        self.assertNotIn("参考样例", joined)
        self.assertIn("项目2真实数据", joined)


class GatherDocumentsRouteTests(unittest.TestCase):
    """回归: 路由曾把 ctx 当 kb_name 传给 gather_documents; 采集又用本地主键调
    get_parse_result, 两处都会让蒸馏在采集阶段失败/丢文档。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="_test_wiki_gather_")
        self.wiki_path = os.path.join(self._tmp.name, "wiki.db")
        self.ctx = object()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        gc.collect()

    def test_route_helper_forwards_kb_name_and_canonical_file_id(self) -> None:
        from unittest import mock

        from src.api.routes import wiki as wiki_routes
        from src.pipelines.document_rag.schemas import DocumentInfo, ParsedChunk, ParseResult

        seen: dict[str, object] = {}

        class Pipeline:
            def list_file_infos(self, kb_name, ctx=None):
                seen["kb_name"] = kb_name
                seen["list_ctx"] = ctx
                return [DocumentInfo(
                    id="ragflow:37", name="需求.docx", processor_kind="ragflow",
                    status="parsed", metadata={"store_id": 37},
                )]

            def get_parse_result(self, kb_name, document_id, ctx=None):
                seen["parse_document_id"] = document_id
                seen["parse_ctx"] = ctx
                return ParseResult(
                    document_id=document_id, file_name="需求.docx", chunk_count=1,
                    chunks=[ParsedChunk(index=0, content="内容块", metadata={})],
                )

        with mock.patch.object(wiki_routes, "WikiService", lambda: WikiService(db_path=self.wiki_path)):
            docs = wiki_routes._gather_documents(Pipeline(), self.ctx, "KB-A", kb_id=9, department_id=7)

        self.assertEqual(seen["kb_name"], "KB-A")
        self.assertIs(seen["list_ctx"], self.ctx)
        # 规范化 file id 给解析端口, 本地主键留给页面 slug/来源
        self.assertEqual(seen["parse_document_id"], "ragflow:37")
        self.assertIs(seen["parse_ctx"], self.ctx)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["doc_id"], "37")
        self.assertEqual(docs[0]["name"], "需求.docx")
        self.assertEqual(docs[0]["chunks"], ["内容块"])

    def test_spreadsheet_branch_uses_store_id(self) -> None:
        from unittest import mock

        from src.api.routes import wiki as wiki_routes
        from src.pipelines.document_rag.schemas import DocumentInfo

        class Pipeline:
            def list_file_infos(self, kb_name, ctx=None):
                return [DocumentInfo(
                    id="ragflow:30", name="表.xlsx", processor_kind="spreadsheet_table",
                    status="indexed", metadata={"store_id": 30},
                )]

        with mock.patch.object(wiki_routes, "WikiService", lambda: WikiService(db_path=self.wiki_path)), \
                mock.patch.object(WikiService, "_spreadsheet_blocks", return_value=["块A"]) as blocks:
            docs = wiki_routes._gather_documents(Pipeline(), self.ctx, "KB-A", kb_id=9, department_id=7)

        blocks.assert_called_once_with("30", 9, 7, "KB-A")
        self.assertEqual(docs[0]["doc_id"], "30")
        self.assertEqual(docs[0]["chunks"], ["块A"])

    def test_circuit_branch_builds_structured_digest(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from src.api.routes import wiki as wiki_routes
        from src.pipelines.document_rag.schemas import DocumentInfo

        class Engine:
            def list_designs(self, kb_name):
                return [{
                    "design_id": "edf-abc",
                    "files": ["net.edf"],
                    "instance_count": 3,
                    "net_count": 2,
                    "module_count": 1,
                }]

            def get_circuit_overview(self, kb_name, design_id):
                return {
                    "design_id": design_id,
                    "instance_count": 3,
                    "net_count": 2,
                    "module_count": 1,
                    "modules": [{"name": "Power", "instance_count": 3, "net_count": 2}],
                    "power_nets": ["VCC3V3", "GND"],
                    "clock_nets": ["CLK_24M"],
                    "warnings": ["unresolved cell"],
                }

        class Store:
            def load(self, kb_name, design_id):
                return SimpleNamespace(
                    instances=[
                        SimpleNamespace(refdes="U1", part_number="TPS62872", library_cell="TPS62872"),
                        SimpleNamespace(refdes="U2", part_number="TPS62872", library_cell="TPS62872"),
                        SimpleNamespace(refdes="X1", part_number=None, library_cell="CONN_20P"),
                    ],
                    nets=[
                        SimpleNamespace(name="VCC3V3", net_type="power"),
                        SimpleNamespace(name="CLK_24M", net_type="clock"),
                    ],
                    modules=[SimpleNamespace(name="Power", module_id="m1", instances=["U1", "U2"], nets=["VCC3V3"])],
                    parse_warnings=[],
                )

        class CircuitService:
            query_engine = Engine()
            store = Store()

        class Pipeline:
            circuit_service = CircuitService()

            def list_file_infos(self, kb_name, ctx=None):
                return [DocumentInfo(
                    id="ragflow:35", name="net.edf", processor_kind="circuit_design",
                    status="indexed", metadata={"store_id": 35},
                )]

        with mock.patch.object(wiki_routes, "WikiService", lambda: WikiService(db_path=self.wiki_path)):
            docs = wiki_routes._gather_documents(Pipeline(), self.ctx, "KB-A", kb_id=9, department_id=7)

        self.assertEqual(len(docs), 1)
        chunks = docs[0]["chunks"]
        self.assertEqual(len(chunks), 2)
        self.assertIn("edf-abc", chunks[0])
        self.assertIn("Power", chunks[0])
        self.assertIn("VCC3V3", chunks[0])
        self.assertIn("时钟网", chunks[0])
        self.assertIn("TPS62872 × 2", chunks[1])
        self.assertIn("X1", chunks[1])
        self.assertIn("unresolved cell", chunks[1])


    def test_ragflow_template_chunks_filtered_from_material(self) -> None:
        from unittest import mock

        from src.api.routes import wiki as wiki_routes
        from src.pipelines.document_rag.schemas import DocumentInfo, ParsedChunk, ParseResult

        class Pipeline:
            def list_file_infos(self, kb_name, ctx=None):
                return [DocumentInfo(
                    id="ragflow:37", name="接口文档.docx", processor_kind="ragflow",
                    status="parsed", metadata={"store_id": 37},
                )]

            def get_parse_result(self, kb_name, document_id, ctx=None):
                contents = [
                    "2025年02月24日发布 Released on 24/02/2025 北京经纬恒润科技股份有限公司",
                    "Sheet名称：0_Instruction Manual; 项目适配说明：需要根据实际项目更新编号",
                    "模板使用说明Template instructions 黑色斜体文字为参考样例",
                    "ID：83; SM Name：CAN communication E2E mechanism 真实正文",
                    "编制： Author: 王寿林 签名： Sign: 日期： 2025-02-24",
                ]
                return ParseResult(
                    document_id=document_id, file_name="接口文档.docx", chunk_count=len(contents),
                    chunks=[ParsedChunk(index=i, content=c, metadata={}) for i, c in enumerate(contents)],
                )

        with mock.patch.object(wiki_routes, "WikiService", lambda: WikiService(db_path=self.wiki_path)):
            docs = wiki_routes._gather_documents(Pipeline(), self.ctx, "KB-A", kb_id=9, department_id=7)

        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["chunks"], ["ID：83; SM Name：CAN communication E2E mechanism 真实正文"])


if __name__ == "__main__":
    unittest.main()
