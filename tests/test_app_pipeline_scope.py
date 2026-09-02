# ruff: noqa: E402
import sys
import types

if "chromadb" not in sys.modules:
    chromadb_stub = types.ModuleType("chromadb")
    chromadb_stub.PersistentClient = object
    chromadb_stub.Settings = lambda **kwargs: kwargs
    sys.modules["chromadb"] = chromadb_stub


resource_manager_module = types.ModuleType("src.core.resource_manager")
resource_manager_module.resource_manager = types.SimpleNamespace(
    chroma_client=None,
    get_kb_lock=lambda kb_name: __import__("threading").RLock(),
    get_status=lambda: {},
    initialize=lambda force=False: True,
)
sys.modules["src.core.resource_manager"] = resource_manager_module

index_builder_module = types.ModuleType("src.ingestion.index_builder")
index_builder_module.get_or_build_index = lambda *args, **kwargs: None
sys.modules["src.ingestion.index_builder"] = index_builder_module

document_manager_module = types.ModuleType("src.services.document_manager")
document_manager_module.DocumentManager = object
sys.modules["src.services.document_manager"] = document_manager_module

import unittest
from unittest.mock import MagicMock, patch

from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthUser, ROLE_DEPT_ADMIN
from src.pipelines.document_rag.schemas import BackendResult, DocumentInfo, RequestContext


class _Backend:
    name = "fake"

    def __init__(self):
        self.created = []

    def create_kb_storage(self, kb_name, ctx=None):
        self.created.append(kb_name)


class _Auth:
    def __init__(self):
        self.registered = []
        self.exists_calls = []

    def knowledge_base_exists(self, kb_name, department_id=None, kb_id=None):
        self.exists_calls.append((kb_name, department_id, kb_id))
        return str(department_id) == "dept_existing" and kb_name == "shared"

    def get_user_by_username(self, username):
        return AuthUser(
            id=1,
            username=username,
            role=ROLE_DEPT_ADMIN,
            is_active=True,
            department_id=7,
        )

    def register_knowledge_base(self, kb_name, owner=None):
        self.registered.append((kb_name, owner.department_id if owner else None))


class _AgentWithTokenSummary:
    def __init__(self):
        self.summary = object()
        self.clear_calls = 0

    def clear_last_token_usage_summary(self):
        self.clear_calls += 1
        self.summary = None

    def get_last_token_usage_summary(self):
        return self.summary


class AppPipelineScopeTests(unittest.TestCase):
    def _pipeline(self):
        pipeline = object.__new__(AppPipeline)
        pipeline.backend = _Backend()
        pipeline.documents = None
        return pipeline

    def test_create_kb_checks_existence_by_department_scope(self):
        pipeline = self._pipeline()
        auth = _Auth()
        ctx = RequestContext(user_id="admin_a", roles=[ROLE_DEPT_ADMIN], metadata={"department_id": "dept_a"})

        with patch("src.core.app_pipeline.AuthService", return_value=auth):
            ok, message = pipeline.create_kb("shared", ctx=ctx)

        self.assertTrue(ok)
        self.assertEqual(pipeline.backend.created, ["shared"])
        self.assertEqual(auth.registered, [("shared", 7)])

    def test_create_kb_rejects_existing_in_same_department(self):
        pipeline = self._pipeline()
        ctx = RequestContext(user_id="admin_a", roles=[ROLE_DEPT_ADMIN], metadata={"department_id": "dept_existing"})

        with patch("src.core.app_pipeline.AuthService", return_value=_Auth()):
            ok, message = pipeline.create_kb("shared", ctx=ctx)

        self.assertFalse(ok)
        self.assertEqual(pipeline.backend.created, [])

    def test_create_kb_ignores_current_selected_kb_id(self):
        pipeline = self._pipeline()
        auth = _Auth()
        ctx = RequestContext(
            user_id="admin_a",
            roles=[ROLE_DEPT_ADMIN],
            metadata={"department_id": "dept_a", "kb_id": 42},
        )

        with patch("src.core.app_pipeline.AuthService", return_value=auth):
            ok, message = pipeline.create_kb("new_kb", ctx=ctx)

        self.assertTrue(ok)
        self.assertEqual(auth.exists_calls, [("new_kb", "dept_a", None)])
        self.assertEqual(pipeline.backend.created, ["new_kb"])

    def test_query_early_return_clears_previous_token_usage_summary(self):
        pipeline = self._pipeline()
        pipeline.agent = _AgentWithTokenSummary()

        response = "".join(pipeline.query("", "kb", []))

        self.assertTrue(response)
        self.assertIsNone(pipeline.get_last_token_usage_summary())
        self.assertEqual(pipeline.agent.clear_calls, 1)

    def test_scan_kb_sources_exposes_application_layer_catalog_contract(self):
        class Store:
            def list_documents(self, kb_name, department_id=None):
                return []

        class CatalogBackend(_Backend):
            def __init__(self):
                super().__init__()
                self.store = Store()
                self.list_calls = []

            def list_documents(self, kb_name, ctx=None):
                self.list_calls.append((kb_name, ctx))
                return [
                    DocumentInfo(
                        id="ragflow:1",
                        name="adas-spec.pdf",
                        metadata={"content_kind": "document_text"},
                    )
                ]

        pipeline = object.__new__(AppPipeline)
        pipeline.backend = CatalogBackend()
        pipeline.spreadsheet_service = None
        pipeline.circuit_service = None
        ctx = RequestContext(
            user_id="evaluation",
            allowed_kbs=["47:ADAS"],
            kb_permissions={"47:ADAS": "read"},
            metadata={"department_id": 47},
        )

        catalog = pipeline.scan_kb_sources("ADAS", ctx)

        self.assertEqual([item["document_name"] for item in catalog["sources"]], ["adas-spec.pdf"])
        self.assertEqual(pipeline.backend.list_calls, [("ADAS", ctx)])

    def test_delete_document_audit_success_from_backend_ok(self):
        """AppPipeline.delete_document must use BackendResult.ok to determine
        the audit success flag instead of sniffing error prefixes from the
        message string (the old approach missed "Document mapping was not found"
        and "Document does not belong to the current department")."""
        pipeline = self._pipeline()
        # Mock documents to return a BackendResult with ok=False so we can
        # verify the audit flag is False too.
        mock_docs = MagicMock()
        mock_docs.delete_document.return_value = BackendResult(
            ok=False, message="Document mapping was not found.", backend="ragflow"
        )
        pipeline.documents = mock_docs
        pipeline._audit = MagicMock()

        ctx = RequestContext(user_id="admin_a", roles=[ROLE_DEPT_ADMIN])
        # Returns the message string; does NOT raise (BackendResult carries
        # the failure signal in .ok, not by raising).
        msg = pipeline.delete_document("d1", "kb", ctx=ctx)
        self.assertEqual(msg, "Document mapping was not found.")

        # The audit call must have success=False because BackendResult.ok is False.
        pipeline._audit.assert_called_once()
        call_kwargs = pipeline._audit.call_args[1]
        self.assertFalse(call_kwargs.get("success", True))
        self.assertIn("not found", call_kwargs.get("error_message", ""))


if __name__ == "__main__":
    unittest.main()
