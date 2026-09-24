import unittest

from src.pipelines.document_rag.schemas import RequestContext, kb_scope_key
from src.pipelines.document_rag.ragflow_backend import _ctx_department_id
from src.services.kb_scope import kb_scope_from_context, local_kb_scope_key


class KbScopeTests(unittest.TestCase):
    def test_scope_reads_department_and_kb_id_from_context(self):
        ctx = RequestContext(metadata={"department_id": 7, "kb_id": "42"})
        scope = kb_scope_from_context("kb_alpha", ctx)
        self.assertEqual(scope.kb_name, "kb_alpha")
        self.assertEqual(scope.department_id, "7")
        self.assertEqual(scope.kb_id, 42)
        self.assertTrue(scope.has_department)

    def test_scope_prefers_resource_department_over_actor_department(self):
        ctx = RequestContext(metadata={"actor_department_id": "dept_actor", "resource_department_id": "dept_resource"})
        scope = kb_scope_from_context("kb_alpha", ctx)
        self.assertEqual(scope.department_id, "dept_resource")

    def test_scope_requires_department(self):
        scope = kb_scope_from_context("kb_alpha", RequestContext())
        with self.assertRaises(PermissionError):
            scope.require_department("upload to")

    def test_request_context_prefers_scoped_permission_key(self):
        ctx = RequestContext(
            allowed_kbs=["shared"],
            kb_permissions={"other:shared": "admin", "dept_a:shared": "read"},
            metadata={"department_id": "dept_a"},
        )
        self.assertEqual(kb_scope_key("shared", "dept_a"), "dept_a:shared")
        self.assertTrue(ctx.has_kb_permission("shared", "read"))
        self.assertFalse(ctx.has_kb_permission("shared", "write"))

    def test_request_context_does_not_fallback_to_bare_permission_with_department(self):
        ctx = RequestContext(
            allowed_kbs=["shared"],
            kb_permissions={"shared": "admin", "dept_a:shared": "read"},
            metadata={"department_id": "dept_b"},
        )

        self.assertFalse(ctx.has_kb_permission("shared", "read"))
        self.assertFalse(ctx.has_kb_permission("shared", "admin"))

    def test_request_context_rejects_bare_permission_without_department(self):
        ctx = RequestContext(
            allowed_kbs=["shared"],
            kb_permissions={"shared": "admin"},
        )

        self.assertFalse(ctx.has_kb_permission("shared", "admin"))

    def test_request_context_permissions_use_resource_department(self):
        ctx = RequestContext(
            kb_permissions={"dept_actor:shared": "admin", "dept_resource:shared": "read"},
            metadata={"actor_department_id": "dept_actor", "resource_department_id": "dept_resource"},
        )
        self.assertTrue(ctx.has_kb_permission("shared", "read"))
        self.assertFalse(ctx.has_kb_permission("shared", "write"))

    def test_ragflow_department_helper_does_not_validate_placeholder_kb_name(self):
        ctx = RequestContext(metadata={"department_id": 7})
        self.assertEqual(_ctx_department_id(ctx), "7")

    def test_local_scope_key_includes_department_for_local_backend_isolation(self):
        ctx = RequestContext(metadata={"department_id": "dept_a"})
        self.assertEqual(local_kb_scope_key("shared", ctx), "dept_dept_a__shared")
        self.assertEqual(local_kb_scope_key("shared", None), "shared")


if __name__ == "__main__":
    unittest.main()
