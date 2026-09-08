from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import Mock

import src.settings
import httpx

from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline
from tests._api_stub import Server, StubPipeline, make_auth


class DocumentGenerationSessionApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.server = Server(cls.app)
        cls.server.start()
        cls.url = cls.server.url

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self._old_pw = src.settings.AUTH_DEFAULT_ADMIN_PASSWORD
        src.settings.AUTH_DEFAULT_ADMIN_PASSWORD = "StrongTestPassword123!"
        self.addCleanup(setattr, src.settings, "AUTH_DEFAULT_ADMIN_PASSWORD", self._old_pw)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        auth_db = os.path.join(self.tmp.name, "auth.db")
        old_auth_db = src.settings.AUTH_DB_PATH
        src.settings.AUTH_DB_PATH = auth_db
        self.addCleanup(setattr, src.settings, "AUTH_DB_PATH", old_auth_db)
        self.auth, _, _, _ = make_auth(auth_db)
        self.stub = StubPipeline()
        self.app.dependency_overrides[get_pipeline] = lambda: self.stub
        self.app.dependency_overrides[get_auth_service] = lambda: self.auth
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = httpx.Client(base_url=self.url, timeout=30)
        self.addCleanup(self.client.close)

    def _headers(self, username="user1"):
        response = self.client.post(
            "/api/v1/login",
            json={"username": username, "password": "pw123456"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": f"Bearer {response.json()['token']}"}

    def test_create_answer_and_confirm_clarification_session(self):
        calls: list[tuple[str, object]] = []
        self.stub.create_document_generation_session = lambda ctx, **kwargs: (
            calls.append(("create", kwargs))
            or {
                "session_id": "generation-session-1",
                "status": "needs_clarification",
                "brief": {"confirmed": False},
                "messages": [{
                    "message_id": "m1",
                    "role": "assistant",
                    "content": "请确认项目版本",
                    "question_id": "scope.revision",
                    "options": ["当前发布版本"],
                }],
            }
        )
        self.stub.answer_document_generation_session = lambda ctx, session_id, **kwargs: (
            calls.append(("answer", (session_id, kwargs)))
            or {
                "session_id": session_id,
                "status": "needs_clarification",
                "brief": {"confirmed": False, "scope": {"revision": kwargs["answer"]}},
                "messages": [],
            }
        )
        self.stub.confirm_document_generation_session = lambda ctx, session_id: (
            calls.append(("confirm", session_id))
            or {
                "session_id": session_id,
                "status": "ready_to_generate",
                "brief": {"confirmed": True},
                "messages": [],
            }
        )
        headers = self._headers("admin1")

        created = self.client.post(
            "/api/v1/document-generation/sessions?kb=shared",
            headers=headers,
            json={
                "template_version_id": "tv1",
                "purpose": "生成评审表",
                "document_schema_id": "schema-1",
                "document_schema_version": "2",
            },
        )
        answered = self.client.post(
            "/api/v1/document-generation/sessions/generation-session-1/messages?kb=shared",
            headers=headers,
            json={
                "question_id": "scope.revision",
                "answer": "当前发布版本",
                "client_request_id": "clarification-request-1",
            },
        )
        confirmed = self.client.post(
            "/api/v1/document-generation/sessions/generation-session-1/confirm?kb=shared",
            headers=headers,
            json={},
        )

        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(answered.status_code, 200, answered.text)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["status"], "ready_to_generate")
        self.assertEqual([call[0] for call in calls], ["create", "answer", "confirm"])
        self.assertEqual(calls[0][1]["document_schema_id"], "schema-1")
        self.assertEqual(calls[0][1]["document_schema_version"], "2")
        self.assertEqual(
            calls[1][1][1]["client_request_id"],
            "clarification-request-1",
        )

    def test_session_creation_requires_write_permission(self):
        headers = self._headers()
        response = self.client.post(
            "/api/v1/document-generation/sessions?kb=shared",
            headers=headers,
            json={"template_version_id": "tv1"},
        )

        self.assertEqual(response.status_code, 403, response.text)

    def test_v2_session_request_allows_template_free_output_spec_input(self):
        captured = {}
        self.stub.create_document_generation_session = lambda ctx, **kwargs: (
            captured.update(kwargs)
            or {
                "session_id": "generation-session-v2",
                "contract_version": "output_spec_v1",
                "template_version_id": None,
                "status": "awaiting_plan",
                "messages": [],
            }
        )
        response = self.client.post(
            "/api/v1/document-generation/sessions?kb=shared",
            headers=self._headers("admin1"),
            json={
                "contract_version": "output_spec_v1",
                "purpose": "生成通用报告",
                "output_spec": {
                    "document_type": "report",
                    "layout_source": {
                        "mode": "generated_structure",
                        "constraints_profile_id": "generic-report",
                        "constraints_profile_version": "1",
                    },
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(captured["contract_version"], "output_spec_v1")
        self.assertIsNone(captured["template_version_id"])
        self.assertEqual(captured["output_spec"]["document_type"], "report")

    def test_plan_proposal_endpoints_forward_hash_bound_request_and_safe_projection(self):
        captured = {}
        self.stub.create_document_plan_proposal = lambda ctx, session_id, **kwargs: (
            captured.update({"session_id": session_id, **kwargs})
            or {
                "session_id": session_id,
                "task_id": "task-1",
                "document_plan_id": "plan-1",
                "document_plan_version": 1,
                "plan_hash": "sha256:plan",
                "output_spec_id": "spec-1",
                "output_spec_version": 1,
                "output_spec_hash": "sha256:spec",
                "status": "awaiting_plan_confirmation",
                "executable": True,
                "deliverables": [{"format": "xlsx", "role": "primary"}],
                "layout_summary": {"mode": "provided_template"},
                "outline_count": 2,
                "table_count": 1,
                "source_summary": {"knowledge_base_count": 1, "attachment_count": 0},
                "policies": {"missing_data": "mark_tbd", "inference": "forbid"},
                "warnings": [],
                "blockers": [],
                "next_actions": ["confirm_document_plan"],
            }
        )
        self.stub.get_document_plan = lambda ctx, plan_id, version: {
            "document_plan_id": plan_id,
            "document_plan_version": version,
            "status": "proposed",
            "source_summary": {"knowledge_base_count": 1, "attachment_count": 0},
            "warnings": [],
            "blockers": [],
            "next_actions": ["confirm_document_plan"],
        }
        headers = self._headers("admin1")
        created = self.client.post(
            "/api/v1/document-generation/sessions/session-1/plan-proposals?kb=shared",
            headers=headers,
            json={"client_request_id": "proposal-request-1", "expected_output_spec_version": 3},
        )
        fetched = self.client.get(
            "/api/v1/document-generation/plans/plan-1/versions/1?kb=shared",
            headers=headers,
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(captured, {
            "session_id": "session-1",
            "client_request_id": "proposal-request-1",
            "expected_output_spec_version": 3,
        })
        self.assertNotIn("source_names", created.json())
        self.assertNotIn("evidence", created.json())

    def test_confirm_plan_endpoint_forwards_hashes_and_reports_submission(self):
        captured = {}
        self.stub.confirm_document_plan = lambda ctx, session_id, **kwargs: (
            captured.update({"session_id": session_id, **kwargs})
            or {
                "submission_id": "document-plan-submission-1",
                "status": "pending",
                "session_id": session_id,
                "task_id": "task-1",
                "document_plan_id": "plan-1",
                "document_plan_version": 1,
                "plan_hash": "sha256:plan",
                "work_order_id": None,
                "job_id": None,
                "next_actions": ["await_generation"],
            }
        )
        headers = self._headers("admin1")
        response = self.client.post(
            "/api/v1/document-generation/sessions/session-1/confirm-plan?kb=shared",
            headers=headers,
            json={
                "expected_output_spec_hash": "sha256:spec",
                "expected_plan_hash": "sha256:plan",
                "client_request_id": "confirm-request-1",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(captured, {
            "session_id": "session-1",
            "expected_output_spec_hash": "sha256:spec",
            "expected_plan_hash": "sha256:plan",
            "client_request_id": "confirm-request-1",
        })
        body = response.json()
        self.assertEqual(body["submission_id"], "document-plan-submission-1")
        self.assertEqual(body["status"], "pending")
        # No job can exist yet: the outbox worker owns that transition.
        self.assertIsNone(body.get("work_order_id"))
        self.assertIsNone(body.get("job_id"))

    def test_confirm_plan_maps_errors_to_http_statuses(self):
        headers = self._headers("admin1")
        payload = {
            "expected_output_spec_hash": "sha256:spec",
            "expected_plan_hash": "sha256:plan",
            "client_request_id": "confirm-request-1",
        }

        self.stub.confirm_document_plan = Mock(side_effect=ValueError("stale plan"))
        stale = self.client.post(
            "/api/v1/document-generation/sessions/session-1/confirm-plan?kb=shared",
            headers=headers, json=payload,
        )
        self.assertEqual(stale.status_code, 409, stale.text)

        self.stub.confirm_document_plan = Mock(side_effect=PermissionError("no access"))
        forbidden = self.client.post(
            "/api/v1/document-generation/sessions/session-1/confirm-plan?kb=shared",
            headers=headers, json=payload,
        )
        self.assertEqual(forbidden.status_code, 403, forbidden.text)

        self.stub.confirm_document_plan = Mock(side_effect=KeyError("generation session not found"))
        missing = self.client.post(
            "/api/v1/document-generation/sessions/session-1/confirm-plan?kb=shared",
            headers=headers, json=payload,
        )
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_work_order_creation_requires_write_permission(self):
        response = self.client.post(
            "/api/v1/document-generation/work-orders?kb=shared",
            headers=self._headers(),
            json={
                "template_version_id": "tv1",
                "document_schema_id": "schema-1",
                "document_schema_version": "1",
            },
        )

        self.assertEqual(response.status_code, 403, response.text)

    def test_task_projection_endpoints_use_task_identity(self):
        self.stub.get_document_task_projection = lambda ctx, task_id: {
            "task_id": task_id,
            "status": "needs_clarification",
            "conversation_refs": {"conversation_id": "17"},
            "next_actions": ["answer_clarification"],
        }
        self.stub.list_document_task_projections = lambda ctx, **kwargs: [{
            "task_id": "task-a",
            "knowledge_base_name": kwargs.get("knowledge_base_name"),
            "conversation_id": kwargs.get("conversation_id"),
        }]
        headers = self._headers("admin1")

        single = self.client.get(
            "/api/v1/document-generation/tasks/task-a/projection?kb=shared",
            headers=headers,
        )
        listed = self.client.get(
            "/api/v1/document-generation/tasks?kb=shared&conversation_id=17",
            headers=headers,
        )

        self.assertEqual(single.status_code, 200, single.text)
        self.assertEqual(single.json()["task_id"], "task-a")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()[0], {
            "task_id": "task-a",
            "knowledge_base_name": "shared",
            "conversation_id": "17",
        })

    def test_task_review_endpoints_are_task_bound_and_idempotent(self):
        self.stub.list_document_reviews = lambda ctx, task_id: [{
            "review_id": "review-a",
            "task_id": task_id,
            "status": "pending",
        }]
        self.stub.get_document_review = lambda ctx, review_id: {
            "review_id": review_id,
            "task_id": "task-a",
            "status": "pending",
        }
        self.stub.submit_document_review_decision = lambda ctx, review_id, **kwargs: {
            "review_id": review_id,
            "task_id": "task-a",
            "status": kwargs["status"],
            "decision": kwargs["decision"],
        }
        headers = self._headers("admin1")

        listed = self.client.get(
            "/api/v1/document-generation/tasks/task-a/reviews?kb=shared",
            headers=headers,
        )
        detail = self.client.get(
            "/api/v1/document-generation/reviews/review-a?kb=shared",
            headers=headers,
        )
        decided = self.client.post(
            "/api/v1/document-generation/reviews/review-a/decision?kb=shared",
            headers=headers,
            json={
                "subject_hash": "subject-a",
                "decision": {"outcome": "approved"},
                "status": "approved",
                "client_request_id": "decision-a",
            },
        )

        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()[0]["task_id"], "task-a")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["review_id"], "review-a")
        self.assertEqual(decided.status_code, 200, decided.text)
        self.assertEqual(decided.json()["status"], "approved")

    def test_task_revision_endpoints_preserve_parent_and_snapshot_contract(self):
        self.stub.list_document_revisions = lambda ctx, task_id: [{
            "revision_id": "revision-a",
            "task_id": task_id,
            "parent_artifact_id": "artifact-a",
            "status": "planned",
        }]
        self.stub.get_document_revision = lambda ctx, revision_id: {
            "revision_id": revision_id,
            "task_id": "task-a",
            "parent_artifact_id": "artifact-a",
            "input_snapshot_hash": "snapshot-a",
        }
        self.stub.create_document_revision = lambda ctx, task_id, **kwargs: {
            "revision_id": "revision-a",
            "task_id": task_id,
            "parent_artifact_id": kwargs["parent_artifact_id"],
            "status": "planned",
        }
        headers = self._headers("admin1")

        listed = self.client.get(
            "/api/v1/document-generation/tasks/task-a/revisions?kb=shared",
            headers=headers,
        )
        detail = self.client.get(
            "/api/v1/document-generation/revisions/revision-a?kb=shared",
            headers=headers,
        )
        created = self.client.post(
            "/api/v1/document-generation/tasks/task-a/revisions?kb=shared",
            headers=headers,
            json={
                "parent_artifact_id": "artifact-a",
                "request_type": "field_update",
                "request": "更新版本字段",
                "changed_fields": ["pcb_revision"],
                "client_request_id": "revision-request-1",
            },
        )

        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["input_snapshot_hash"], "snapshot-a")
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["parent_artifact_id"], "artifact-a")

    def test_revision_completion_binds_child_and_revalidation_result(self):
        self.stub.complete_document_revision = lambda ctx, revision_id, **kwargs: {
            "revision_id": revision_id,
            "child_artifact_id": kwargs["child_artifact_id"],
            "revalidation_status": kwargs["revalidation_status"],
            "revalidation_result": kwargs["revalidation_result"],
            "status": "revalidated",
        }
        response = self.client.post(
            "/api/v1/document-generation/revisions/revision-a/complete?kb=shared",
            headers=self._headers("admin1"),
            json={
                "child_artifact_id": "artifact-child",
                "revalidation_status": "passed",
                "revalidation_result": {"report_id": "report-child"},
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "revision_id": "revision-a",
            "child_artifact_id": "artifact-child",
            "revalidation_status": "passed",
            "revalidation_result": {"report_id": "report-child"},
            "status": "revalidated",
        })

    def test_task_resume_endpoint_uses_task_identity(self):
        headers = self._headers("admin1")
        response = self.client.post(
            "/api/v1/document-generation/tasks/task-a/resume?kb=shared",
            headers=headers,
            json={},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "task_id": "task-a",
            "work_order_id": "wo-1",
            "run_id": "bg-task-resume",
            "status": "queued",
        })

    def test_generation_start_requires_write_permission(self):
        response = self.client.post(
            "/api/v1/document-generation/work-orders/wo-1/generate?kb=shared",
            headers=self._headers(),
            json={},
        )

        self.assertEqual(response.status_code, 403, response.text)

    def test_other_document_mutations_require_write_permission(self):
        headers = self._headers()
        requests = [
            (
                "/api/v1/document-generation/work-orders/wo-1/icd-scope-resolution?kb=shared",
                {"resolutions": [], "comment": ""},
            ),
            ("/api/v1/document-generation/harness-runs/run-1/pause?kb=shared", {}),
            ("/api/v1/document-generation/harness-runs/run-1/cancel?kb=shared", {}),
            ("/api/v1/document-generation/artifacts/artifact-1/feedback?kb=shared", {"comment": "反馈"}),
            ("/api/v1/document-generation/artifacts/artifact-1/approve?kb=shared", {"comment": "批准"}),
        ]

        for url, payload in requests:
            response = self.client.post(url, headers=headers, json=payload)
            self.assertEqual(response.status_code, 403, response.text)

    def test_template_sanitization_summary_allows_read_access(self):
        response = self.client.get(
            "/api/v1/document-generation/templates/tv1/sanitization?kb=shared",
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 200, response.text)

    def test_confirmed_session_id_is_forwarded_when_creating_work_order(self):
        captured = {}

        def prepare(ctx, *, knowledge_base_name, **kwargs):
            captured.update(kwargs)
            return {"stage": "ready", "work_order_id": "wo-brief"}

        self.stub.prepare_knowledge_base_document_generation = prepare
        response = self.client.post(
            "/api/v1/document-generation/work-orders?kb=shared",
            headers=self._headers("admin1"),
            json={
                "template_version_id": "tv1",
                "document_schema_id": "schema-1",
                "document_schema_version": "1",
                "generation_session_id": "generation-session-1",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(captured["generation_session_id"], "generation-session-1")
