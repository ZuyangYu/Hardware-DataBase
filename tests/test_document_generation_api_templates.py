import os
import tempfile
import unittest

import src.settings
import httpx

from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline
from src.document_authoring.template_analysis import (
    TemplateAnalysis,
    TemplateAnalysisSuggestion,
    TemplateAnalysisUnit,
)

from tests._api_stub import Server, StubPipeline, make_auth


class _Analysis:
    analysis_id = "a1"
    template_version_id = "tv1"
    format = "xlsx"
    status = "ready_for_confirmation"
    units = [type("U", (), {"unit_id": "u1", "label": "型号", "writable": True, "blocked_reason": None})()]
    suggestions = [type("S", (), {"semantic_unit_id": "s1", "label": "型号", "confidence": 0.9})()]


class _RejectedDecisionAnalysis(_Analysis):
    activation_decision = type("Decision", (), {"status": "requires_human", "reason_codes": ["mapping_conflict"]})()


def _review_analysis() -> TemplateAnalysis:
    return TemplateAnalysis(
        analysis_id="a1",
        template_version_id="tv1",
        content_hash="a" * 64,
        format="xlsx",
        status="requires_human",
        units=[
            TemplateAnalysisUnit(
                unit_id="sheet:Review!A1",
                locator={"sheet_name": "Review", "cell": "A1"},
                label="项目名称",
                writable=True,
                value_preview="项目名称",
                value_hash="secret-value-hash",
                structural_role_hint="fixed_label",
            ),
            TemplateAnalysisUnit(
                unit_id="sheet:Review!B1",
                locator={"sheet_name": "Review", "cell": "B1"},
                label="项目名称",
                writable=True,
                structural_role_hint="placeholder",
            ),
        ],
        suggestions=[
            TemplateAnalysisSuggestion(
                semantic_unit_id="project_name",
                label="项目名称",
                target_unit_ids=["sheet:Review!B1"],
                retrieval_terms=["project name"],
                confidence=0.98,
                overwrite_basis="placeholder",
            ),
        ],
    )


class DocGenTemplateApiTests(unittest.TestCase):
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
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        old_db = src.settings.AUTH_DB_PATH
        src.settings.AUTH_DB_PATH = self.db_path
        self.addCleanup(setattr, src.settings, "AUTH_DB_PATH", old_db)
        self.auth, self.dept, self.admin, self.user = make_auth(self.db_path)
        self.stub = StubPipeline()
        self.app.dependency_overrides[get_pipeline] = lambda: self.stub
        self.app.dependency_overrides[get_auth_service] = lambda: self.auth
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = httpx.Client(base_url=self.url, timeout=30)
        self.addCleanup(self.client.close)

    def _token(self, username, password="pw123456"):
        r = self.client.post("/api/v1/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["token"]

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_template_analyze_returns_safe_view(self):
        self.stub.analyze_document_template = lambda ctx, *, filename, content, template_name: _Analysis()
        t = self._token("admin1")
        r = self.client.post(
            "/api/v1/document-generation/templates/analyze?kb=shared",
            headers=self._auth(t),
            files={"file": ("t.xlsx", b"PK", "application/octet-stream")},
            data={"template_name": "T"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["analysis_id"], "a1")
        self.assertEqual(body["units"][0]["writable"], True)
        self.assertNotIn("locator", str(body))  # 绝不暴露 OOXML locator
        self.assertNotIn("PK", str(body))  # 绝不回传上传字节

    def test_template_analyze_system_admin_blocked(self):
        t = self._token(src.settings.AUTH_DEFAULT_ADMIN_USERNAME, "StrongTestPassword123!")
        r = self.client.post(
            "/api/v1/document-generation/templates/analyze?kb=shared",
            headers=self._auth(t),
            files={"file": ("t.xlsx", b"PK", "application/octet-stream")},
            data={"template_name": "T"},
        )
        self.assertEqual(r.status_code, 403)

    def test_template_analyze_permission_denied_403(self):
        def _denied(ctx, *, filename, content, template_name):
            raise PermissionError("denied")

        self.stub.analyze_document_template = _denied
        t = self._token("admin1")
        r = self.client.post(
            "/api/v1/document-generation/templates/analyze?kb=shared",
            headers=self._auth(t),
            files={"file": ("t.xlsx", b"PK", "application/octet-stream")},
            data={"template_name": "T"},
        )
        self.assertEqual(r.status_code, 403)
        self.assertIn("denied", r.text)

    def test_template_analyze_does_not_auto_confirm_a_rejected_decision(self):
        old_enabled = src.settings.DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES
        src.settings.DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES = True
        self.addCleanup(
            setattr,
            src.settings,
            "DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES",
            old_enabled,
        )
        confirmed: list[str] = []
        self.stub.analyze_document_template = lambda ctx, *, filename, content, template_name: _RejectedDecisionAnalysis()
        self.stub.confirm_document_template = lambda ctx, *, analysis_id, display_name: confirmed.append(analysis_id)
        token = self._token("admin1")

        response = self.client.post(
            "/api/v1/document-generation/templates/analyze?kb=shared",
            headers=self._auth(token),
            files={"file": ("t.xlsx", b"PK", "application/octet-stream")},
            data={"template_name": "T"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["auto_activated"])
        self.assertEqual(confirmed, [])

    def test_template_analyze_does_not_auto_confirm_without_a_decision(self):
        old_enabled = src.settings.DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES
        src.settings.DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES = True
        self.addCleanup(
            setattr,
            src.settings,
            "DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES",
            old_enabled,
        )
        confirmed: list[str] = []
        self.stub.analyze_document_template = lambda ctx, *, filename, content, template_name: _Analysis()
        self.stub.confirm_document_template = lambda ctx, *, analysis_id, display_name: confirmed.append(analysis_id)
        token = self._token("admin1")

        response = self.client.post(
            "/api/v1/document-generation/templates/analyze?kb=shared",
            headers=self._auth(token),
            files={"file": ("t.xlsx", b"PK", "application/octet-stream")},
            data={"template_name": "T"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["auto_activated"])
        self.assertEqual(confirmed, [])

    def test_template_review_returns_safe_mapping_detail(self):
        self.stub.get_document_template_analysis_for_review = lambda ctx, *, analysis_id: _review_analysis()
        t = self._token("user1")

        r = self.client.get(
            "/api/v1/document-generation/templates/a1/review?kb=shared",
            headers=self._auth(t),
        )

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["analysis_id"], "a1")
        self.assertEqual(body["content_hash"], "a" * 64)
        self.assertEqual(body["units"][0]["structural_role_hint"], "fixed_label")
        self.assertNotIn("locator", str(body))
        self.assertNotIn("value_preview", str(body))
        self.assertNotIn("secret-value-hash", str(body))

    def test_template_correction_uses_context_actor_and_returns_corrected_analysis(self):
        corrected = _review_analysis().model_copy(update={
            "analysis_id": "a2",
            "status": "ready_for_confirmation",
        })

        def _correct(ctx, *, correction):
            self.stub.correction = correction
            return corrected

        self.stub.get_document_template_analysis_for_review = lambda ctx, *, analysis_id: _review_analysis()
        self.stub.correct_document_template_analysis = _correct
        t = self._token("admin1")
        r = self.client.post(
            "/api/v1/document-generation/templates/a1/corrections?kb=shared",
            headers=self._auth(t),
            json={
                "expected_content_hash": "a" * 64,
                "selected_suggestion_ids": ["project_name"],
                "locked_unit_ids": ["sheet:Review!A1"],
                "comment": "锁定固定标题，仅保留占位符字段。",
                "actor_id": "system_admin",
                "suggestions": [{
                    "semantic_unit_id": "forged",
                    "label": "forged",
                    "target_unit_ids": ["sheet:Review!A1"],
                    "retrieval_terms": ["forged"],
                    "confidence": 1.0,
                }],
            },
        )

        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["analysis_id"], "a2")
        self.assertEqual(self.stub.correction.actor_id, "admin1")
        self.assertEqual(self.stub.correction.locked_unit_ids, ["sheet:Review!A1"])
        self.assertEqual(self.stub.correction.suggestions[0].confidence, 0.98)
        self.assertEqual(self.stub.correction.suggestions[0].target_unit_ids, ["sheet:Review!B1"])

    def test_template_correction_requires_at_least_one_retained_mapping(self):
        t = self._token("admin1")
        r = self.client.post(
            "/api/v1/document-generation/templates/a1/corrections?kb=shared",
            headers=self._auth(t),
            json={
                "expected_content_hash": "a" * 64,
                "selected_suggestion_ids": [],
                "comment": "所有映射都不安全。",
            },
        )

        self.assertEqual(r.status_code, 422, r.text)

    def test_options_require_read_permission(self):
        t = self._token("user1")
        r = self.client.get("/api/v1/document-generation/options?kb=shared", headers=self._auth(t))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["knowledge_bases"], ["shared"])
