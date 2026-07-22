"""Shared stubs for API/CLI tests -- avoids re-declaring the fake pipeline."""
import socket
import threading
import time

import config.settings
import httpx
import uvicorn
from src.core.auth import AuthService, ROLE_DEPT_ADMIN, ROLE_USER
from src.pipelines.document_rag.schemas import DocumentInfo, IngestResult


class StubPipeline:
    """Minimal stand-in for AppPipeline covering every method the API calls."""

    def __init__(self):
        self.uploaded: list = []
        self.created: list = []
        self.deleted: list = []
        self.query_chunks = ["第一段", "第二段"]

    def list_knowledge_bases(self, ctx=None):
        return ["shared"]

    def list_file_infos(self, kb_name, ctx=None):
        return [
            DocumentInfo(
                id="d1",
                name="a.pdf",
                status="completed",
                processor_kind="document_rag",
                dataset_kind="design",
            )
        ]

    def upload_files(self, files, target_kb, ctx=None, source_group=None, progress_callback=None):
        self.uploaded.append((target_kb, list(files), source_group))
        return IngestResult(success_count=len(files), total_count=len(files), messages=["ok"])

    def create_kb(self, name, ctx=None):
        self.created.append(name)
        return True, f"知识库 '{name}' 创建成功"

    def delete_document(self, filename, kb_name, ctx=None):
        self.deleted.append((kb_name, filename))
        return "已删除"

    def query(self, msg, kb_name, history, ctx):
        for chunk in self.query_chunks:
            yield chunk

    def get_last_retrieval_summary(self):
        return {"status": "success", "evidence": [], "retrieval_rounds": 1}

    def get_last_agent_footer(self):
        return "footer"

    def get_last_token_usage_summary(self):
        return {"total_tokens": 10}


def make_auth(db_path: str):
    """Build a temp auth.db: one dept, dept_admin 'admin1', user 'user1', KB 'shared'.

    Assumes AUTH_DEFAULT_ADMIN_PASSWORD has already been raised to a non-default
    value by the caller's setUp.
    """
    auth = AuthService(db_path=db_path)
    sysadmin = auth.get_user_by_username(config.settings.AUTH_DEFAULT_ADMIN_USERNAME)
    dept = auth.create_department("hw")
    admin = auth.create_user_as(sysadmin, "admin1", "pw123456", ROLE_DEPT_ADMIN, dept.id)
    user = auth.create_user_as(admin, "user1", "pw123456", ROLE_USER, dept.id)
    auth.register_knowledge_base("shared", owner=admin)
    auth.grant_kb_permission_as(admin, "shared", user.id, "read")
    return auth, dept, admin, user


class Server:
    """Runs a FastAPI app on an ephemeral port in a background thread."""

    def __init__(self, app):
        self.app = app
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        # Avoid installing signal handlers from a non-main thread.
        self.server.install_signal_handlers = lambda: None
        self._thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.url}/health", timeout=0.5).status_code == 200:
                    return
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("API server did not start within 10s")

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=5)
