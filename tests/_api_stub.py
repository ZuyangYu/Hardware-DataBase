"""Shared stubs for API/CLI tests -- avoids re-declaring the fake pipeline."""
import socket
import threading
import time

import config.settings
import httpx
import uvicorn
from src.core.auth import AuthService, ROLE_DEPT_ADMIN, ROLE_USER
from src.pipelines.document_rag.schemas import DocumentInfo, IngestResult, ParsedChunk, ParseResult


class StubParseTask:
    """Duck-typed stand-in for ``ingestion.parse_tasks.ParseTask``."""

    def __init__(self, task_id: str, kb_name: str, status: str = "running", progress: int = 42):
        self.id = task_id
        self.kb_name = kb_name
        self.source_path = ""
        self.original_name = "sample.pdf"
        self.source_group = "文档资料"
        self.created_by = "admin1"
        self.status = status
        self.progress = progress
        self.stage = "解析中"
        self.message = ""
        self.result = ""
        self.document_id = "d1"
        self.created_at = 0.0
        self.updated_at = 0.0
        self.started_at = None
        self.finished_at = None


class StubPipeline:
    """Minimal stand-in for AppPipeline covering every method the API calls."""

    def __init__(self):
        self.uploaded: list = []
        self.created: list = []
        self.deleted: list = []
        self.deleted_kbs: list = []
        self.deleted_tasks: list = []
        self.paused_tasks: list = []
        self.resumed_tasks: list = []
        self.cleared_kbs: list = []
        self.applied_settings: list = []
        self.query_chunks = ["第一段", "第二段"]

    # KB / files ---------------------------------------------------------
    def list_knowledge_bases(self, ctx=None):
        return ["shared"]

    def list_all_knowledge_bases_for_admin(self, ctx=None):
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

    def delete_knowledge_base(self, kb_name, ctx=None):
        self.deleted_kbs.append(kb_name)
        return True, f"知识库 '{kb_name}' 已删除"

    def delete_document(self, filename, kb_name, ctx=None):
        self.deleted.append((kb_name, filename))
        return "已删除"

    # Parse tasks --------------------------------------------------------
    def list_parse_tasks(self, kb_name=None, ctx=None):
        return [StubParseTask("t1", kb_name or "shared")]

    def delete_parse_task(self, task_id, ctx=None):
        self.deleted_tasks.append(task_id)
        return "任务已删除"

    def pause_parse_task(self, task_id, ctx=None):
        self.paused_tasks.append(task_id)
        return "当前后端不支持暂停解析任务"

    def resume_parse_task(self, task_id, ctx=None):
        self.resumed_tasks.append(task_id)
        return "当前后端不支持启动解析任务"

    def clear_finished_parse_tasks(self, kb_name=None, ctx=None):
        self.cleared_kbs.append(kb_name)

    def get_parse_result(self, kb_name, document_id, ctx=None):
        if document_id == "missing":
            return None
        return ParseResult(
            document_id=document_id,
            file_name="a.pdf",
            chunk_count=2,
            chunks=[
                ParsedChunk(index=0, content="chunk 0", metadata={"page": 1}),
                ParsedChunk(index=1, content="chunk 1", metadata={"page": 2}),
            ],
        )

    # Query --------------------------------------------------------------
    def query(self, msg, kb_name, history, ctx, agent_thread_id=""):
        # ``agent_thread_id`` is the LangGraph thread id; the stub just records
        # it via a side attribute so tests can assert on it if they care.
        self.last_thread_id = agent_thread_id
        for chunk in self.query_chunks:
            yield chunk

    def get_last_retrieval_summary(self):
        return {"status": "success", "evidence": [], "retrieval_rounds": 1}

    def get_last_agent_footer(self):
        return "footer"

    def get_last_token_usage_summary(self):
        return {"total_tokens": 10}

    # Governance / config ------------------------------------------------
    @staticmethod
    def governance_stats(ctx=None):
        return {"shared": {"files": 1, "failed": 0, "parsing": 0}}

    @staticmethod
    def check_ragflow_connection(base_url, api_key, dataset_names, timeout=120):
        return True, "ok", []

    def apply_settings(self, new_settings):
        self.applied_settings.append(dict(new_settings))


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
