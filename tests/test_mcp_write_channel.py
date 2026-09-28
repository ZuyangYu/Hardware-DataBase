"""T14: governed write channel (four gates, proposals, artifacts, worker).

Covers the phase-2 acceptance matrix: whitelist/consent/channel/capability
gating, proposal creation without any external call, confirm/expire/reject,
worker execution with idempotency, artifact validation (extension/size/hash),
download, and the original-file invariant.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from langchain_core.messages import AIMessage

import src.settings as settings
from src.agents.mcp.artifacts import McpArtifactStore
from src.agents.mcp.cache import get_tool_cache
from src.agents.mcp.client import reset_mcp_runtime
from src.agents.mcp.executor import MCPToolExecutor
from src.agents.mcp.policy import load_tools, resolve_visible_servers
from src.agents.mcp.service import McpPluginService
from src.agents.mcp.store import SqliteMcpServerStore
from src.agents.mcp.write_store import SqliteMcpWriteProposalStore
from src.agents.tools.runtime import ToolRuntime
from src.pipelines.document_rag.schemas import RequestContext
from tests._mcp_stub import McpStubServer, reset_write_calls, write_call_count

WRITE_TOOLS = [
    "kicad_create_schematic",
    "artifact_bad_extension",
    "artifact_bad_hash",
    "artifact_empty",
    "artifact_huge",
    "artifact_missing",
]
READ_CAP = {"external_tool.invoke"}
FULL_CAPS = {"external_tool.invoke", "external_tool.write"}


class _WriteChannelBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.auth_db = os.path.join(self._tmp.name, "auth.db")
        self.artifact_dir = os.path.join(self._tmp.name, "artifacts")
        self.store = SqliteMcpServerStore(self.auth_db)
        self.write_store = SqliteMcpWriteProposalStore(self.auth_db)
        self.artifact_store = McpArtifactStore(self.artifact_dir)
        self.key = Fernet.generate_key().decode("ascii")
        self.events: list[dict] = []
        reset_write_calls()
        self._patches = [
            patch.object(settings, "AUTH_DB_PATH", self.auth_db),
            patch.object(settings, "MCP_CREDENTIALS_KEY", self.key),
            patch.object(settings, "MCP_ENABLED", True),
            patch.object(settings, "MCP_WRITE_ENABLED", True),
            patch.object(settings, "CONVERSATION_UNIFIED_TOOLS_ENABLED", True),
            patch.object(settings, "RESOURCE_GOVERNOR_ENABLED", True),
            patch.object(settings, "MCP_ALLOW_INSECURE_HTTP", True),
            patch.object(settings, "MCP_ALLOW_PRIVATE_HOSTS", True),
            patch.object(settings, "MCP_CONNECT_TIMEOUT_SECONDS", 10),
            patch.object(settings, "MCP_CALL_TIMEOUT_SECONDS", 5),
            patch.object(settings, "MCP_CALLS_PER_TURN", 8),
            patch.object(settings, "MCP_WRITE_CONFIRM_TTL_SECONDS", 1800),
            patch.object(settings, "MCP_WRITE_MAX_ATTEMPTS", 3),
            patch.object(settings, "MCP_MAX_ARTIFACT_BYTES", 65536),
            patch.object(
                settings,
                "MCP_ARTIFACT_ALLOWED_EXTENSIONS",
                "kicad_sch,kicad_pcb,kicad_pro,zip,json,csv",
            ),
            patch.object(settings, "MCP_ARTIFACT_STORAGE_DIR", self.artifact_dir),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self._patches):
            item.stop()
        reset_mcp_runtime()
        get_tool_cache().clear_all()
        reset_write_calls()
        self._tmp.cleanup()

    def _ctx(self, department: int = 7) -> RequestContext:
        return RequestContext(
            user_id="alice", roles=["employee"], metadata={"department_id": department}
        )

    def _seed(
        self,
        stub_url: str,
        *,
        allow_write: bool = True,
        write_tools: list[str] | None = None,
        department: int = 7,
    ):
        self.store.create(
            slug="stub",
            name="桩写入服务",
            transport="streamable_http",
            url=stub_url,
            department_scope="departments",
            department_ids=[department],
            write_tools=list(write_tools if write_tools is not None else WRITE_TOOLS),
            created_by="admin",
        )
        self.store.set_enabled("stub", True, "admin")
        self.store.upsert_subscription(42, "stub", allow_write=allow_write)

    def _binding(self, tool_name: str, *, capabilities=FULL_CAPS, department: int = 7):
        snapshot = resolve_visible_servers(
            self._ctx(department), user_id=42, store=self.store
        )
        plan = load_tools(snapshot, effective_capabilities=capabilities)
        return plan, {binding.tool_name: binding for binding in plan.bindings}

    def _executor(self, *, department: int = 7) -> MCPToolExecutor:
        rt = ToolRuntime(
            kb_name="kb_hw",
            ctx=self._ctx(department),
            question="q",
            evidence_budget=10,
            on_event=self.events.append,
        )
        return MCPToolExecutor(
            rt,
            user_id=42,
            department_id=department,
            store=self.store,
            write_store=self.write_store,
            artifact_store=self.artifact_store,
        )

    def _confirm(self, proposal_id: str) -> None:
        service = McpPluginService(
            store=self.store,
            write_store=self.write_store,
            artifact_store=self.artifact_store,
        )
        service.confirm_write_proposal(42, proposal_id)

    def _run_worker_once(self) -> dict:
        claimed = self.write_store.claim("worker-1", limit=1)
        self.assertTrue(claimed)
        executor = MCPToolExecutor(
            write_store=self.write_store,
            store=self.store,
            artifact_store=self.artifact_store,
        )
        return executor.execute_proposal(claimed[0], worker_id="worker-1")


class McpWriteGateTests(_WriteChannelBase):
    def test_four_gates_control_write_visibility(self):
        with McpStubServer(include_write_tools=True) as stub:
            self._seed(stub.url)
            # 1) Global switch off.
            with patch.object(settings, "MCP_WRITE_ENABLED", False):
                _plan, bindings = self._binding("kicad_create_schematic")
            self.assertNotIn("kicad_create_schematic", bindings)
            # 2) Tool outside the admin whitelist.
            self.store.update("stub", write_tools=["artifact_huge"])
            _plan, bindings = self._binding("kicad_create_schematic")
            self.assertNotIn("kicad_create_schematic", bindings)
            # 3) User consent missing.
            self.store.update("stub", write_tools=WRITE_TOOLS)
            self.store.upsert_subscription(42, "stub", allow_write=False)
            _plan, bindings = self._binding("kicad_create_schematic")
            self.assertNotIn("kicad_create_schematic", bindings)
            # 4) Capability missing.
            self.store.upsert_subscription(42, "stub", allow_write=True)
            _plan, bindings = self._binding("kicad_create_schematic", capabilities=READ_CAP)
            self.assertNotIn("kicad_create_schematic", bindings)
            # All four gates open -> write binding present.
            _plan, bindings = self._binding("kicad_create_schematic")
            self.assertIn("kicad_create_schematic", bindings)
            self.assertTrue(bindings["kicad_create_schematic"].write)

    def test_write_switch_off_means_zero_write_calls(self):
        with McpStubServer(include_write_tools=True) as stub:
            self._seed(stub.url)
            with patch.object(settings, "MCP_WRITE_ENABLED", False):
                plan, bindings = self._binding("kicad_create_schematic")
            self.assertFalse(any(binding.write for binding in plan.bindings))
            self.assertNotIn("kicad_create_schematic", bindings)
            service = McpPluginService(
                store=self.store,
                write_store=self.write_store,
                artifact_store=self.artifact_store,
            )
            proposal = self.write_store.create(
                user_id=42,
                slug="stub",
                tool_name="kicad_create_schematic",
                arguments_digest="digest",
                arguments={"title": "demo"},
            )
            with patch.object(settings, "MCP_WRITE_ENABLED", False):
                with self.assertRaises(Exception):
                    service.confirm_write_proposal(42, proposal.proposal_id)
        self.assertEqual(write_call_count(), 0)


class McpWriteProposalTests(_WriteChannelBase):
    def test_proposal_creation_never_calls_the_server(self):
        with McpStubServer(include_write_tools=True) as stub:
            self._seed(stub.url)
            _plan, bindings = self._binding("kicad_create_schematic")
            executor = self._executor()
            result = executor.propose(
                bindings["kicad_create_schematic"], {"title": "demo", "size": 32}
            )

            self.assertIn("写入提案已创建", result)
            self.assertEqual(write_call_count(), 0)
            proposals = self.write_store.list_for_user(42)
            self.assertEqual(len(proposals), 1)
            proposal = proposals[0]
            self.assertEqual(proposal.status, "proposed")
            self.assertEqual(proposal.slug, "stub")
            self.assertEqual(proposal.artifact_hint, "size,title")
            # The body is never in the serialized view or the ciphertext.
            self.assertNotIn("demo", str(proposal.to_dict()))
            self.assertNotIn("demo", proposal.arguments_ciphertext)
            # The worker can replay the encrypted arguments.
            self.assertEqual(
                self.write_store.decrypt_arguments(proposal),
                {"title": "demo", "size": 32},
            )

            card_events = [event for event in self.events if event["type"] == "mcp_write_proposal"]
            self.assertEqual(len(card_events), 1)
            card = card_events[0]["payload"]["card"]
            self.assertEqual(card["proposal_id"], proposal.proposal_id)
            self.assertEqual(card["status"], "proposed")
            diagnostics = executor.rt.mcp_diagnostics
            self.assertEqual(diagnostics[-1]["status"], "proposed")

    def test_confirm_execute_and_download_with_original_untouched(self):
        original = os.path.join(self._tmp.name, "original.kicad_sch")
        with open(original, "wb") as handle:
            handle.write(b"(kicad_sch (original design))")
        original_digest = hashlib.sha256(open(original, "rb").read()).hexdigest()

        with McpStubServer(include_write_tools=True) as stub:
            self._seed(stub.url)
            _plan, bindings = self._binding("kicad_create_schematic")
            executor = self._executor()
            executor.propose(bindings["kicad_create_schematic"], {"title": "demo", "size": 16})
            proposal = self.write_store.list_for_user(42)[0]
            self._confirm(proposal.proposal_id)
            outcome = self._run_worker_once()

        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(write_call_count(), 1)
        completed = self.write_store.get(42, proposal.proposal_id)
        self.assertEqual(completed.status, "completed")
        self.assertTrue(completed.artifact_sha256)
        content = self.artifact_store.read(completed.artifact_ref)
        self.assertEqual(hashlib.sha256(content).hexdigest(), completed.artifact_sha256)
        self.assertTrue(completed.artifact_ref.endswith(".kicad_sch"))
        self.assertIn(b"kicad_sch", content)
        # Original file bytes/hash are untouched.
        self.assertEqual(
            hashlib.sha256(open(original, "rb").read()).hexdigest(), original_digest
        )
        # Duplicate confirmation and duplicate claims are no-ops.
        service = McpPluginService(
            store=self.store,
            write_store=self.write_store,
            artifact_store=self.artifact_store,
        )
        with self.assertRaises(Exception):
            service.confirm_write_proposal(42, proposal.proposal_id)
        self.assertEqual(self.write_store.claim("worker-2"), [])
        self.assertEqual(write_call_count(), 1)

    def test_expired_proposal_cannot_be_confirmed_or_claimed(self):
        with McpStubServer(include_write_tools=True) as stub:
            self._seed(stub.url)
            _plan, bindings = self._binding("kicad_create_schematic")
            executor = self._executor()
            with patch.object(settings, "MCP_WRITE_CONFIRM_TTL_SECONDS", 1):
                executor.propose(bindings["kicad_create_schematic"], {"title": "demo"})
            proposal = self.write_store.list_for_user(42)[0]
            time.sleep(1.1)
            service = McpPluginService(
                store=self.store,
                write_store=self.write_store,
                artifact_store=self.artifact_store,
            )
            with self.assertRaises(Exception):
                service.confirm_write_proposal(42, proposal.proposal_id)
            self.assertEqual(self.write_store.get(42, proposal.proposal_id).status, "expired")
            self.assertEqual(self.write_store.claim("worker-1"), [])
        self.assertEqual(write_call_count(), 0)

    def test_reject_keeps_the_row_for_audit(self):
        with McpStubServer(include_write_tools=True) as stub:
            self._seed(stub.url)
            _plan, bindings = self._binding("kicad_create_schematic")
            executor = self._executor()
            executor.propose(bindings["kicad_create_schematic"], {"title": "demo"})
            proposal = self.write_store.list_for_user(42)[0]
            service = McpPluginService(
                store=self.store,
                write_store=self.write_store,
                artifact_store=self.artifact_store,
            )
            rejected = service.reject_write_proposal(42, proposal.proposal_id)
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(self.write_store.get(42, proposal.proposal_id).status, "rejected")
        self.assertEqual(write_call_count(), 0)

    def test_artifact_validation_rejections(self):
        cases = {
            "artifact_bad_extension": "artifact_extension",
            "artifact_bad_hash": "artifact_hash",
            "artifact_empty": "artifact_empty",
            "artifact_missing": "artifact_missing",
        }
        rejected_reasons: list[str] = []
        import src.agents.mcp.executor as executor_mod

        with McpStubServer(include_write_tools=True) as stub:
            self._seed(stub.url)
            for tool_name, expected in cases.items():
                _plan, bindings = self._binding(tool_name)
                executor = self._executor()
                executor.propose(bindings[tool_name], {"title": "demo"})
                proposal = self.write_store.list_for_user(42)[0]
                self._confirm(proposal.proposal_id)
                with patch.object(
                    executor_mod,
                    "record_mcp_artifact_rejected",
                    lambda **kw: rejected_reasons.append(kw["reason"]),
                ):
                    outcome = self._run_worker_once()
                self.assertEqual(outcome["status"], "failed", tool_name)
                self.assertEqual(outcome["error_code"], expected, tool_name)

            # Oversize uses a patched byte cap.
            _plan, bindings = self._binding("artifact_huge")
            executor = self._executor()
            with patch.object(settings, "MCP_MAX_ARTIFACT_BYTES", 1024):
                executor.propose(bindings["artifact_huge"], {"size": 4096})
                proposal = self.write_store.list_for_user(42)[0]
                self._confirm(proposal.proposal_id)
                with patch.object(
                    executor_mod,
                    "record_mcp_artifact_rejected",
                    lambda **kw: rejected_reasons.append(kw["reason"]),
                ):
                    outcome = self._run_worker_once()
            self.assertEqual(outcome["error_code"], "artifact_size")

        self.assertEqual(
            rejected_reasons, ["extension", "hash", "empty", "size"]
        )
        self.assertEqual(write_call_count(), 5)

    def test_transient_failure_retries_then_fails(self):
        with McpStubServer(include_write_tools=True) as stub:
            self._seed(stub.url)
            _plan, bindings = self._binding("kicad_create_schematic")
            executor = self._executor()
            executor.propose(bindings["kicad_create_schematic"], {"title": "demo"})
            proposal = self.write_store.list_for_user(42)[0]
            self._confirm(proposal.proposal_id)
        # Server is now down: the worker sees a retryable connect failure.
        with patch.object(settings, "MCP_WRITE_MAX_ATTEMPTS", 2):
            first = self._run_worker_once()
            self.assertEqual(first["status"], "confirmed")
            self.assertEqual(first["error_code"], "connect")
            second = self._run_worker_once()
            self.assertEqual(second["status"], "failed")
        self.assertEqual(write_call_count(), 0)


class McpWriteE2ETests(_WriteChannelBase):
    def test_runner_turn_proposes_then_worker_delivers_candidate(self):
        import src.agents.runner as runner_mod

        class _FakeRAGBackend:
            name = "fake"

            def retrieve(self, *args, **kwargs):
                return []

        captured: dict = {}
        events: list[dict] = []

        def stream(*args, **kwargs):
            tool = next(
                item
                for item in captured["tools"]
                if getattr(item, "name", "") == "mcp__stub__kicad_create_schematic"
            )
            captured["tool_result"] = tool.invoke({"title": "board", "size": 24})
            yield AIMessage(content="已生成候选产物提案"), {"langgraph_node": "model"}

        fake_agent = type("F", (), {})()
        fake_agent.stream = stream

        def fake_create_deep_agent(**kwargs):
            captured.update(kwargs)
            return fake_agent

        with McpStubServer(include_write_tools=True) as stub:
            self._seed(stub.url)
            with (
                patch.object(runner_mod, "create_chat_model", lambda: object()),
                patch.object(runner_mod, "create_deep_agent", fake_create_deep_agent),
                patch.object(runner_mod, "record_agent", lambda **kwargs: None),
            ):
                runner = runner_mod.MultiSourceAgentRunner(
                    rag_backend=_FakeRAGBackend(), circuit_service=None
                )
                deltas = list(
                    runner.stream(
                        query="画一张原理图",
                        kb_name="kb_hw",
                        history=[],
                        ctx=self._ctx(),
                        thread_id="t-write",
                        event_callback=events.append,
                        attachment_user_id=42,
                    )
                )
            self.assertEqual(deltas, ["已生成候选产物提案"])
            self.assertIn("写入提案已创建", str(captured["tool_result"]))
            self.assertIn("外部插件写入规则", captured["system_prompt"])
            self.assertEqual(write_call_count(), 0)

            proposal = self.write_store.list_for_user(42)[0]
            self._confirm(proposal.proposal_id)
            outcome = self._run_worker_once()
            self.assertEqual(outcome["status"], "completed")
            content = self.artifact_store.read(
                self.write_store.get(42, proposal.proposal_id).artifact_ref
            )
            self.assertIn(b"(kicad_sch", content)

        card_events = [event for event in events if event["type"] == "mcp_write_proposal"]
        self.assertEqual(len(card_events), 1)
        summary = runner.get_last_retrieval_summary()
        self.assertEqual(summary["evidence"], [])


if __name__ == "__main__":
    unittest.main()
