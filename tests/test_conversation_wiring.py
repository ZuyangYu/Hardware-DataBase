import os
import unittest


class ConversationWiringTests(unittest.TestCase):
    def test_app_pipeline_wires_conversation_service_into_runner(self):
        """Real construction chain: AppPipeline -> backend bundle ->
        MultiSourceAgentRunner.conversation_service with a usable engine."""
        from src.core.app_pipeline import AppPipeline

        pipeline = AppPipeline()
        runner = pipeline.agent
        self.assertIsNotNone(runner.conversation_service)
        # the engine shares the same root the store uses, so indexed files are visible
        store = getattr(pipeline.backend, "conversations", None)
        self.assertIsNotNone(store)
        self.assertEqual(
            os.path.abspath(runner.conversation_service.root),
            os.path.abspath(store.root),
        )
        # search against an empty index must not raise (fail-open contract)
        from src.services.kb_scope import kb_scope_from_context

        ctx = type("C", (), {"metadata": {"department_id": "dept_x"}})
        scope = kb_scope_from_context("kb_any", ctx())
        rows = runner.conversation_service.search_by_scope(scope.department_id, "kb_any", "任意")
        self.assertEqual(rows, [])

    def test_runtime_bundle_registers_conversation_handler(self):
        from src.pipelines.document_rag.ragflow_backend import RAGFlowBackend

        backend = RAGFlowBackend()
        handler_kinds = set(backend.ingestion.handlers.keys())
        self.assertIn("external_conversation", handler_kinds)


if __name__ == "__main__":
    unittest.main()
