"""Shared test isolation.

Admission control is a process-wide singleton keyed by user/tenant id.  Tests
reuse small user ids across suites, so quotas would accumulate across unrelated
tests and produce order-dependent 429s.  Admission stays disabled by default in
tests; the admission suites enable it explicitly (and reset the controller).
"""

from __future__ import annotations

import os
import tempfile

import pytest

@pytest.fixture(autouse=True)
def _isolate_worker_readiness(monkeypatch):
    # Unit API clients do not start real consumers. Deployment tests opt in.
    monkeypatch.setenv("HDB_REQUIRED_WORKER_ROLES", "")
    monkeypatch.setenv("HDB_API_SPAWN_QUALITY_WORKER", "0")
    monkeypatch.setattr(src.settings, "COST_LEDGER_ENABLED", False)


@pytest.fixture(autouse=True)
def _disable_admission_by_default():
    original = src.settings.ADMISSION_CONTROL_ENABLED
    src.settings.ADMISSION_CONTROL_ENABLED = False
    reset_admission_controller()
    try:
        yield
    finally:
        src.settings.ADMISSION_CONTROL_ENABLED = original
        reset_admission_controller()


# 生产 .env 切到 PostgreSQL 后，凡是构造 ConversationService / 记忆存储的测试都会
# 通过工厂直接写 DSN 指向的库（`__new__` 里拦截），测试就会污染真实数据。测试默认
# 强制回落 sqlite；需要 PG 的套件显式传 DSN（`HDB_TEST_POSTGRES_DSN`）并自行设置。
_BACKEND_SETTINGS = (
    "REDIS_REQUIRED",
    "REDIS_URL",
    "CONTROL_PLANE_BACKEND",
    "CONTROL_PLANE_DATABASE_URL",
    "EVALUATION_STORE_BACKEND",
    "EVALUATION_STORE_DATABASE_URL",
    "MEMORY_STORE_BACKEND",
    "MEMORY_DATABASE_URL",
    "AGENT_CHECKPOINT_BACKEND",
    "AGENT_CHECKPOINT_DATABASE_URL",
    "DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND",
    "DOCUMENT_AUTHORING_CHECKPOINTER_DATABASE_URL",
    "RESOURCE_GOVERNOR_BACKEND",
    "RESOURCE_GOVERNOR_DATABASE_URL",
    # 存储收敛的本地台账域（第 1-3 期）：同样强制回落 sqlite，
    # 避免测试顺着生产 .env 写进真实 PG。
    "PROJECT_STORE_BACKEND",
    "PROJECT_STORE_DATABASE_URL",
    "PIPELINE_DOCUMENT_STORE_BACKEND",
    "PIPELINE_DOCUMENT_DATABASE_URL",
    "AUTHORING_STORE_BACKEND",
    "AUTHORING_STORE_DATABASE_URL",
    "AUTH_STORE_BACKEND",
    "AUTH_STORE_DATABASE_URL",
    "COMPATIBILITY_STORE_BACKEND",
    "COMPATIBILITY_STORE_DATABASE_URL",
)

_SQLITE_BACKENDS = {
    "REDIS_REQUIRED": "false",
    "REDIS_URL": "",
    "CONTROL_PLANE_BACKEND": "sqlite",
    "CONTROL_PLANE_DATABASE_URL": "",
    "EVALUATION_STORE_BACKEND": "sqlite",
    "EVALUATION_STORE_DATABASE_URL": "",
    "MEMORY_STORE_BACKEND": "sqlite",
    "MEMORY_DATABASE_URL": "",
    "AGENT_CHECKPOINT_BACKEND": "sqlite",
    "AGENT_CHECKPOINT_DATABASE_URL": "",
    "DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND": "sqlite",
    "DOCUMENT_AUTHORING_CHECKPOINTER_DATABASE_URL": "",
    "RESOURCE_GOVERNOR_BACKEND": "sqlite",
    "RESOURCE_GOVERNOR_DATABASE_URL": "",
    "PROJECT_STORE_BACKEND": "sqlite",
    "PROJECT_STORE_DATABASE_URL": "",
    "PIPELINE_DOCUMENT_STORE_BACKEND": "sqlite",
    "PIPELINE_DOCUMENT_DATABASE_URL": "",
    "AUTHORING_STORE_BACKEND": "sqlite",
    "AUTHORING_STORE_DATABASE_URL": "",
    "AUTH_STORE_BACKEND": "sqlite",
    "AUTH_STORE_DATABASE_URL": "",
    "COMPATIBILITY_STORE_BACKEND": "sqlite",
    "COMPATIBILITY_STORE_DATABASE_URL": "",
}

# sqlite 后端下的默认路径全部指向真实 `storage/`：测试若不显式传路径，就会把
# 测试用户/审计/检查点写进生产文件（历史问题：auth.db 里出现 locktest_* 之类账号）。
# 这些文件改到临时目录，子进程通过 os.environ 继承。
_PATH_SETTINGS = {
    "AGENT_CHECKPOINT_DB_PATH": "agent_checkpoints.db",
    "MEMORY_SQLITE_PATH": "memory.db",
    "RESOURCE_GOVERNOR_DB_PATH": "resource_quotas.db",
    "DOCUMENT_AUTHORING_CHECKPOINTER_PATH": "document_authoring_checkpoints.sqlite",
    "DOCUMENT_AUTHORING_COMPATIBILITY_DB_PATH": "document_authoring_compatibility.sqlite",
}

# Apply isolation before collecting test modules: unittest.setUpClass and
# module-level app factories run before function-scoped autouse fixtures.
# 共享机器上 /tmp/opencode 可能属于其他用户（存在但不可写），此时必须回落系统临时目录。
_TEST_TMP_BASE = "/tmp/opencode"
if not os.path.isdir(_TEST_TMP_BASE) or not os.access(_TEST_TMP_BASE, os.W_OK):
    _TEST_TMP_BASE = None
_TEST_STORAGE = tempfile.TemporaryDirectory(prefix="hdb-tests-", dir=_TEST_TMP_BASE)
os.environ.setdefault("HDB_TEST_REDIS_URL", "redis://127.0.0.1:1/15")
os.environ.update(_SQLITE_BACKENDS)
os.environ.update({
    "HDB_ENV_PATH": os.devnull,
    "STORAGE_DIR": _TEST_STORAGE.name,
    "AUTH_DB_PATH": os.path.join(_TEST_STORAGE.name, "auth.db"),
    "OBS_ENABLED": "false",
    "HDB_REQUIRED_WORKER_ROLES": "",
    "AUTH_DEFAULT_ADMIN_PASSWORD": "IsolatedTestPassword123!",
})
for _key, _filename in _PATH_SETTINGS.items():
    os.environ[_key] = os.path.join(_TEST_STORAGE.name, _filename)

import src.settings  # noqa: E402
from src.core.concurrency.admission import reset_admission_controller  # noqa: E402


@pytest.fixture(scope="session")
def _shared_auth_db(tmp_path_factory) -> str:
    """整个测试会话共用一个临时 auth 库。

    刻意不做成"每个测试一个库"：不少用例（如 setUpClass 里 create_app/建用户）
    依赖类级或跨用例的共享状态，频繁换库会破坏原有语义。隔离的目标是别写生产
    `storage/auth.db`，不是让每个用例互不可见。
    """
    return os.path.join(_TEST_STORAGE.name, "auth.db")


@pytest.fixture(autouse=True)
def _isolate_production_backends(tmp_path, _shared_auth_db):
    # settings + os.environ 都置为 sqlite + 临时路径：后者让测试 spawn 的子进程
    #（重新 import settings 时会读 .env）也继承隔离，既不写生产 PG，也不写生产文件。
    saved = {key: getattr(src.settings, key, None) for key in _BACKEND_SETTINGS}
    saved_env = {key: os.environ.get(key) for key in _BACKEND_SETTINGS}
    saved_paths = {key: getattr(src.settings, key, None) for key in _PATH_SETTINGS}
    saved_paths_env = {key: os.environ.get(key) for key in _PATH_SETTINGS}
    saved_auth = getattr(src.settings, "AUTH_DB_PATH", None)
    saved_auth_env = os.environ.get("AUTH_DB_PATH")
    for key, value in _SQLITE_BACKENDS.items():
        setattr(src.settings, key, value == "true" if key == "REDIS_REQUIRED" else value)
        os.environ[key] = value
    for key, filename in _PATH_SETTINGS.items():
        path = str(tmp_path / filename)
        setattr(src.settings, key, path)
        os.environ[key] = path
    setattr(src.settings, "AUTH_DB_PATH", _shared_auth_db)
    os.environ["AUTH_DB_PATH"] = _shared_auth_db
    try:
        yield
    finally:
        for key, value in saved.items():
            setattr(src.settings, key, value)
        for key, value in saved_paths.items():
            setattr(src.settings, key, value)
        setattr(src.settings, "AUTH_DB_PATH", saved_auth)
        for key, value in {**saved_env, **saved_paths_env}.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if saved_auth_env is None:
            os.environ.pop("AUTH_DB_PATH", None)
        else:
            os.environ["AUTH_DB_PATH"] = saved_auth_env
