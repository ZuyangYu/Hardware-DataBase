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
from unittest.mock import patch

from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthUser, ROLE_DEPT_ADMIN
from src.pipelines.document_rag.schemas import RequestContext


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


if __name__ == "__main__":
    unittest.main()
