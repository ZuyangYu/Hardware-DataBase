import contextlib
import gc
import io
import json
import os
import tempfile
import unittest

import config.settings

import src.cli.main as climain
from src.api.app import create_app
from src.api.deps import get_auth_service, get_pipeline
from src.cli import config as clicfg
from src.cli import session as sess
from src.cli.client import ApiClient

from tests._api_stub import Server, StubPipeline, make_auth


class CliClientTests(unittest.TestCase):
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
        self._old_pw = config.settings.AUTH_DEFAULT_ADMIN_PASSWORD
        config.settings.AUTH_DEFAULT_ADMIN_PASSWORD = "StrongTestPassword123!"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, config.settings, "AUTH_DEFAULT_ADMIN_PASSWORD", self._old_pw)
        self.db_path = os.path.join(self.tmp.name, "auth.db")
        self._old_db = config.settings.AUTH_DB_PATH
        config.settings.AUTH_DB_PATH = self.db_path
        self.addCleanup(setattr, config.settings, "AUTH_DB_PATH", self._old_db)
        self.auth, self.dept, self.admin, self.user = make_auth(self.db_path)
        self.stub = StubPipeline()
        self.app.dependency_overrides[get_pipeline] = lambda: self.stub
        self.app.dependency_overrides[get_auth_service] = lambda: self.auth
        self.addCleanup(self.app.dependency_overrides.clear)
        self.cfg_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.cfg_tmp.cleanup)
        os.environ["HDB_CONFIG_DIR"] = self.cfg_tmp.name
        self.addCleanup(os.environ.pop, "HDB_CONFIG_DIR", None)
        self.addCleanup(gc.collect)

    def _run(self, argv):
        buf = io.StringIO()
        rc = -1
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = climain.main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        return rc, buf.getvalue()

    def test_session_roundtrip(self):
        sess.save_session("u", "tok", "http://x")
        s = sess.load_session()
        self.assertIsNotNone(s)
        self.assertEqual(s["token"], "tok")
        sess.clear_session()
        self.assertIsNone(sess.load_session())

    def test_resolve_api_url_priority(self):
        self.assertEqual(clicfg.resolve_api_url("http://a"), "http://a")
        self.assertEqual(clicfg.resolve_api_url(None, "http://b"), "http://b")
        self.assertEqual(clicfg.resolve_api_url(), "http://127.0.0.1:8000")

    def test_apiclient_login_and_whoami(self):
        api = ApiClient(self.url)
        res = api.login("admin1", "pw123456")
        self.assertEqual(res["user"]["username"], "admin1")
        api.token = res["token"]
        self.assertEqual(api.whoami()["role"], "dept_admin")

    def test_apiclient_query_sse(self):
        api = ApiClient(self.url)
        api.token = api.login("admin1", "pw123456")["token"]
        events = [(e, d) for e, d in api.query("shared", "问")]
        kinds = [e for e, _ in events]
        self.assertIn("delta", kinds)
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(events[-1][1]["answer"], "第一段第二段")

    def test_apiclient_upload_forbidden_for_user(self):
        api = ApiClient(self.url)
        api.token = api.login("user1", "pw123456")["token"]
        path = os.path.join(self.tmp.name, "f.txt")
        with open(path, "wb") as wb:
            wb.write(b"x")
        with self.assertRaises(Exception) as ctx:
            api.upload("shared", [path])
        self.assertEqual(getattr(ctx.exception, "status_code", None), 403)

    def test_main_login_then_query_json(self):
        rc, _ = self._run(["--api-url", self.url, "login", "--user", "admin1", "--password", "pw123456"])
        self.assertEqual(rc, 0)
        self.assertIsNotNone(sess.load_session())

        rc, out = self._run(["--api-url", self.url, "--json", "query", "--kb", "shared", "问"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["answer"], "第一段第二段")
        self.assertEqual(data["summary"]["status"], "success")

    def test_main_list_kb_json(self):
        self._run(["--api-url", self.url, "login", "--user", "user1", "--password", "pw123456"])
        rc, out = self._run(["--api-url", self.url, "--json", "list-kb"])
        self.assertEqual(rc, 0)
        kbs = json.loads(out)
        self.assertEqual(kbs[0]["name"], "shared")

    def test_main_query_stream_default(self):
        self._run(["--api-url", self.url, "login", "--user", "user1", "--password", "pw123456"])
        rc, out = self._run(["--api-url", self.url, "query", "--kb", "shared", "问"])
        self.assertEqual(rc, 0)
        self.assertIn("第一段第二段", out)

    def test_main_unauthenticated_exits_nonzero(self):
        rc, _ = self._run(["--api-url", self.url, "whoami"])
        self.assertNotEqual(rc, 0)
