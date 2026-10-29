import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import src.settings
import src.external_settings as external_settings
from src.pipelines.document_rag.schemas import RequestContext, kb_scope_key


PBKDF2_ITERATIONS = 260_000
ROLE_SYSTEM_ADMIN = "system_admin"
ROLE_EMPLOYEE = "employee"
VALID_ROLES = {ROLE_SYSTEM_ADMIN, ROLE_EMPLOYEE}
API_KEY_SCOPES = frozenset({"read", "write", "invoke"})
API_KEY_ACCESS_MODES = frozenset({"selected_kbs", "all_current_accessible_kbs"})

# M3: schema bootstrap + default-admin provisioning are process-level one-shot.
# All AuthService instances share this guard so later instances skip both steps.
_AUTH_INIT_LOCK = threading.Lock()
_AUTH_INITIALIZED_DBS: set[str] = set()

# H3: login brute-force protection per (username, client_ip).
# Exponential back-off: 5 failures -> 30s lock, doubling per further failure,
# capped at 15 minutes. Cleared on successful login.
LOGIN_MAX_FAILURES = 5
LOGIN_BASE_LOCK_SECONDS = 30
LOGIN_MAX_LOCK_SECONDS = 15 * 60
_LOGIN_TRACKER_LOCK = threading.Lock()


@dataclass
class AuthUser:
    id: int
    username: str
    role: str
    is_active: bool
    department_id: int | None = None
    department_name: str | None = None


@dataclass
class Department:
    id: int
    name: str


@dataclass
class KnowledgeBaseSummary:
    name: str
    kb_id: int | None = None
    department_id: int | None = None
    department_name: str | None = None
    owner_user_id: int | None = None
    owner_username: str | None = None
    is_shared: bool = False
    employee_count: int = 0
    registered: bool = False
    physical_exists: bool = False
    created_at: str = ""


@dataclass(frozen=True)
class KnowledgeBaseIdentity:
    kb_id: int
    kb_name: str
    department_id: int | None


@dataclass(frozen=True)
class ExternalKnowledgeBaseReference:
    kb_ref: str
    kb_id: int
    kb_name: str
    department_id: int | None
    department_name: str | None = None


@dataclass(frozen=True)
class ExternalFileReference:
    file_ref: str
    kb_id: int
    record_id: int


@dataclass(frozen=True)
class ExternalIngestionJob:
    job_ref: str
    kb_ref: str
    kb_id: int
    record_id: int
    key_id: str


@dataclass(frozen=True)
class ExternalAssetReference:
    asset_ref: str
    kb_id: int
    asset_id: int


@dataclass(frozen=True)
class ExternalSourceScope:
    scope_ref: str
    key_id: str
    kb_ref: str
    kb_id: int
    department_id: int
    sources: list[dict]
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class ExternalEvidenceRecord:
    evidence_id: str
    key_id: str
    scope_ref: str
    kb_ref: str
    kb_id: int
    department_id: int
    record_id: int
    file_ref: str
    file_name: str
    content: str
    content_hash: str
    source_content_hash: str
    locator: dict
    score: float
    backend: str
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class EmployeeApiKeyPrincipal:
    """The authenticated identity for an External request.

    ``kb_ids`` is only the Key allowlist.  The caller must still resolve the
    employee and current KB permission for every request; this object is never
    a replacement for live authorization.
    """

    key_id: str
    user_id: int
    scopes: frozenset[str]
    kb_access_mode: str
    kb_ids: frozenset[int]
    kb_refs: frozenset[str]
    expires_at: str


@dataclass
class AuthSession:
    token: str
    user: AuthUser
    expires_at: str


class AuthService:
    def __init__(self, db_path: str | None = None):
        from src.storage import domain_pg

        self._backend, self._dsn, resolved = domain_pg.resolve_store_backend(
            explicit_db_path=db_path,
            prefix="AUTH_STORE",
            default_db_path=src.settings.AUTH_DB_PATH,
        )
        if self._backend == "postgres":
            from src.storage.auth_domain import domain_classes as auth_domain_classes

            domain_pg.ensure_domain_group_ready(
                dsn=self._dsn,
                store_classes=auth_domain_classes(),
                sqlite_db_path=os.path.join(src.settings.STORAGE_DIR, "auth.db"),
                primary_table="users",
                migration_command=(
                    "uv run python scripts/production_hardening/rebuild_auth_domain.py"
                    " --dsn $CONTROL_PLANE_DATABASE_URL"
                ),
            )
            domain_pg.assert_mechanical_shape(
                self._dsn,
                "users",
                {"is_active": "bigint", "created_at": "text", "updated_at": "text"},
                hint=(
                    "认证域的表需要先重建为机械形状（早期按控制面语义建过）：\n"
                    "  uv run python scripts/production_hardening/rebuild_auth_domain.py"
                    " --dsn $CONTROL_PLANE_DATABASE_URL"
                ),
            )
            self.db_path = None
            # 后端无关的一次性引导：默认部门 + 默认管理员（空库/迁移后都幂等）。
            init_key = f"postgres:{self._dsn}"
            if init_key in _AUTH_INITIALIZED_DBS:
                return
            with _AUTH_INIT_LOCK:
                if init_key in _AUTH_INITIALIZED_DBS:
                    return
                with closing(self._connect()) as conn:
                    self._ensure_default_department(conn)
                self._ensure_default_admin()
                _AUTH_INITIALIZED_DBS.add(init_key)
            return
        self.db_path = resolved or src.settings.AUTH_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        init_key = os.path.abspath(self.db_path)
        if init_key in _AUTH_INITIALIZED_DBS:
            return
        # Double-checked locking: sync routes run in Starlette's threadpool, so
        # two concurrent constructions must not both run the migration and the
        # full-price default-admin PBKDF2 checks.
        with _AUTH_INIT_LOCK:
            if init_key in _AUTH_INITIALIZED_DBS:
                return
            self._init_db()
            self._ensure_default_admin()
            _AUTH_INITIALIZED_DBS.add(init_key)

    def _connect(self):
        if getattr(self, "_backend", "sqlite") == "postgres":
            from src.storage.domain_pg import open_pg_connection

            return open_pg_connection(self._dsn)
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _mirror_control_plane_row(self, table: str, key_value, *, delete: bool = False) -> None:
        """Best-effort: keep the control plane's copy of an auth row current.

        With ``CONTROL_PLANE_BACKEND=postgres`` the control plane reads
        ``users.is_active``/``users.department_id`` and enforces
        ``fk_chat_sessions_user`` against its own ``users`` copy, while the
        auth domain stays authoritative in SQLite.  Without this mirror a
        post-cutover user cannot create sessions (FK violation) and their
        turns are never claimed (``JOIN users ... AND u.is_active``).

        Fail-soft: a mirror failure must never break the auth mutation; see
        ``src/storage/auth_mirror.py`` for the failure semantics.

        When the auth domain itself runs on PostgreSQL (``AUTH_STORE_BACKEND=
        postgres``) the rows already live there and the mirror is a no-op —
        the bridge is only for the hybrid (SQLite auth + PostgreSQL control
        plane) shape.
        """
        if getattr(self, "_backend", "sqlite") == "postgres":
            return
        # 只镜像权威库：bootstrap 生成表结构时在临时模板库上实例化的 AuthService
        # 不得把模板里的行写进控制面。
        if os.path.abspath(str(self.db_path)) != os.path.abspath(str(src.settings.AUTH_DB_PATH)):
            return
        try:
            from src.storage.auth_mirror import mirror_auth_row

            mirror_auth_row(self.db_path, table, key_value, delete=delete)
        except Exception as exc:  # noqa: BLE001 - fail-soft by contract
            try:
                from src.core.logger import error

                error(f"[auth-mirror] {table}#{key_value} 镜像异常: {type(exc).__name__}: {exc}")
            except Exception:  # pragma: no cover - logging must not raise
                pass

    def _audit(
        self,
        action: str,
        actor: AuthUser | None = None,
        target_type: str = "",
        target_id: str = "",
        kb_name: str = "",
        success: bool = True,
        error_message: str = "",
        metadata: dict | None = None,
    ) -> None:
        """Record an audit event, fail-soft: never let audit failure break the
        caller. Centralizing this here means both Streamlit and the API layer
        get audit coverage for free, with no client-side record_audit calls."""
        try:
            # Deferred import: src.core.app_logs imports auth for role constants,
            # so a top-level import here would cycle.
            from src.core.app_logs import AppLogService

            AppLogService().record_audit(
                action=action,
                actor=actor,
                target_type=target_type,
                target_id=target_id,
                kb_name=kb_name,
                success=success,
                error_message=error_message,
                metadata=metadata,
            )
        except Exception as audit_error:
            from src.core.logger import warn

            warn(f"AuthService audit failed: {audit_error}")

    def _audit_requested(
        self,
        action: str,
        *,
        actor: AuthUser | None = None,
        target_type: str = "",
        target_id: str = "",
        kb_name: str = "",
        metadata: dict | None = None,
    ) -> None:
        """Write the pre-mutation intent row for a governance write.

        The final row keeps the original action.  If the process dies after
        the mutation but before that row, the ``*.requested`` event remains
        visible instead of the change disappearing from the trail.
        """

        self._audit(
            f"{action}.requested",
            actor=actor,
            target_type=target_type,
            target_id=target_id,
            kb_name=kb_name,
            success=True,
            metadata={**(metadata or {}), "phase": "requested"},
        )

    def _init_db(self):
        with closing(self._connect()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS departments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'employee',
                    department_id INTEGER,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    managed_by_env INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(department_id) REFERENCES departments(id)
                )
            """)
            self._ensure_column(conn, "users", "department_id", "INTEGER")
            self._ensure_column(conn, "users", "managed_by_env", "INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "UPDATE users SET role = ? WHERE role = 'admin'",
                (ROLE_SYSTEM_ADMIN,),
            )
            # 两级角色迁移: 旧的部门管理员与普通用户统一并入"员工"。
            # 幂等: 已迁移的行不再匹配。
            conn.execute(
                "UPDATE users SET role = ? WHERE role IN ('dept_admin', 'user')",
                (ROLE_EMPLOYEE,),
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS login_attempts (
                    key TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    client_ip TEXT NOT NULL DEFAULT '',
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    last_failure_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    department_id INTEGER,
                    owner_user_id INTEGER,
                    is_shared INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(department_id, name),
                    FOREIGN KEY(department_id) REFERENCES departments(id),
                    FOREIGN KEY(owner_user_id) REFERENCES users(id)
                )
            """)
            self._ensure_default_department(conn)
            self._ensure_column(conn, "knowledge_bases", "is_shared", "INTEGER NOT NULL DEFAULT 0")
            self._migrate_kb_scope_schema(conn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS employee_api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_key_id TEXT NOT NULL UNIQUE,
                    key_digest TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    kb_access_mode TEXT NOT NULL DEFAULT 'selected_kbs',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    last_used_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS employee_api_key_kbs (
                    api_key_id INTEGER NOT NULL,
                    kb_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(api_key_id, kb_id),
                    FOREIGN KEY(api_key_id) REFERENCES employee_api_keys(id),
                    FOREIGN KEY(kb_id) REFERENCES knowledge_bases(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS external_kb_refs (
                    kb_ref TEXT PRIMARY KEY,
                    kb_id INTEGER NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    retired_at TEXT,
                    FOREIGN KEY(kb_id) REFERENCES knowledge_bases(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS external_file_refs (
                    file_ref TEXT PRIMARY KEY,
                    kb_id INTEGER NOT NULL,
                    record_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    retired_at TEXT,
                    UNIQUE(kb_id, record_id),
                    FOREIGN KEY(kb_id) REFERENCES knowledge_bases(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS external_asset_refs (
                    asset_ref TEXT PRIMARY KEY,
                    kb_id INTEGER NOT NULL,
                    asset_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    retired_at TEXT,
                    UNIQUE(kb_id, asset_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS external_source_scopes (
                    scope_ref TEXT PRIMARY KEY,
                    key_id TEXT NOT NULL,
                    kb_ref TEXT NOT NULL,
                    kb_id INTEGER NOT NULL,
                    department_id INTEGER NOT NULL,
                    sources_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS external_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    key_id TEXT NOT NULL,
                    scope_ref TEXT NOT NULL DEFAULT '',
                    kb_ref TEXT NOT NULL,
                    kb_id INTEGER NOT NULL,
                    department_id INTEGER NOT NULL,
                    record_id INTEGER NOT NULL,
                    file_ref TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_content_hash TEXT NOT NULL,
                    locator_json TEXT NOT NULL DEFAULT '{}',
                    score REAL NOT NULL DEFAULT 0,
                    backend TEXT NOT NULL DEFAULT 'ragflow',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS external_ingestion_jobs (
                    job_ref TEXT PRIMARY KEY,
                    kb_ref TEXT NOT NULL,
                    kb_id INTEGER NOT NULL,
                    record_id INTEGER NOT NULL,
                    key_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(kb_id, record_id, key_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS external_upload_idempotency (
                    key_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    kb_id INTEGER NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(key_id, idempotency_key)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_employee_api_keys_user_id "
                "ON employee_api_keys(user_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_employee_api_key_kbs_kb_id "
                "ON employee_api_key_kbs(kb_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_external_file_refs_record "
                "ON external_file_refs(kb_id, record_id)"
            )

    def _ensure_column(self, conn, table: str, column: str, ddl: str):
        columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _ensure_default_department(self, conn):
        now = utc_now()
        conn.execute(
            "INSERT OR IGNORE INTO departments (name, created_at) VALUES ('system', ?)",
            (now,),
        )

    def _migrate_kb_scope_schema(self, conn):
        # 逐用户授权（kb_permissions）已废弃：绑定改为所有者维度的共享开关
        # （knowledge_bases.is_shared），不再有逐用户授予对象。启动时清理历史表。
        conn.execute("DROP TABLE IF EXISTS kb_permissions")

        kb_columns = {row["name"] for row in conn.execute("PRAGMA table_info(knowledge_bases)").fetchall()}
        kb_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_bases'"
        ).fetchone()
        kb_sql = kb_sql_row["sql"] if kb_sql_row else ""
        needs_kb_rebuild = "UNIQUE(department_id, name)" not in kb_sql

        if not needs_kb_rebuild:
            return

        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("ALTER TABLE knowledge_bases RENAME TO knowledge_bases_old")
            conn.execute("""
                CREATE TABLE knowledge_bases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    department_id INTEGER,
                    owner_user_id INTEGER,
                    is_shared INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(department_id, name),
                    FOREIGN KEY(department_id) REFERENCES departments(id),
                    FOREIGN KEY(owner_user_id) REFERENCES users(id)
                )
            """)
            select_columns = [
                "id",
                "name",
                "department_id" if "department_id" in kb_columns else "NULL AS department_id",
                "owner_user_id" if "owner_user_id" in kb_columns else "NULL AS owner_user_id",
                "is_shared" if "is_shared" in kb_columns else "0 AS is_shared",
                "created_at" if "created_at" in kb_columns else "'' AS created_at",
            ]
            conn.execute(f"""
                INSERT OR IGNORE INTO knowledge_bases (id, name, department_id, owner_user_id, is_shared, created_at)
                SELECT {', '.join(select_columns)}
                FROM knowledge_bases_old
            """)
            conn.execute("DROP TABLE knowledge_bases_old")
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    def _ensure_default_admin(self):
        username = src.settings.AUTH_DEFAULT_ADMIN_USERNAME
        password = src.settings.AUTH_DEFAULT_ADMIN_PASSWORD
        if not password or password == "admin123" or len(password) < 8:
            raise RuntimeError("AUTH_DEFAULT_ADMIN_PASSWORD must be set to a non-default strong password.")
        with closing(self._connect()) as conn:
            dept = conn.execute("SELECT id FROM departments WHERE name = 'system'").fetchone()
            department_id = dept["id"] if dept else None
            row = conn.execute(
                "SELECT id, password_hash, role, managed_by_env FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if row is not None:
                should_sync_password = (
                    row["role"] == ROLE_SYSTEM_ADMIN
                    and (row["managed_by_env"] or verify_password("admin123", row["password_hash"]))
                    and not verify_password(password, row["password_hash"])
                )
                if should_sync_password:
                    conn.execute(
                        "UPDATE users SET password_hash = ?, managed_by_env = 1, updated_at = ? WHERE id = ?",
                        (hash_password(password), utc_now(), row["id"]),
                    )
                self._mirror_control_plane_row("users", row["id"])
                return
            now = utc_now()
            try:
                conn.execute(
                    """
                    INSERT INTO users (username, password_hash, role, department_id, is_active, managed_by_env, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, 1, ?, ?)
                    """,
                    (username, hash_password(password), ROLE_SYSTEM_ADMIN, department_id, now, now),
                )
            except Exception as exc:  # noqa: BLE001 - backend-specific integrity errors
                # API 与各 worker 同时首次启动时，另一进程/容器可能刚插入了
                # 同名管理员。SQLite 与 PostgreSQL 的异常类型不同，但两者都
                # 使用唯一约束作为并发仲裁；其它完整性错误照常抛出。
                is_unique_violation = isinstance(exc, sqlite3.IntegrityError)
                if self._backend == "postgres":
                    try:
                        from psycopg.errors import UniqueViolation

                        is_unique_violation = isinstance(exc, UniqueViolation)
                    except ImportError:  # pragma: no cover - postgres backend has psycopg
                        is_unique_violation = False
                if not is_unique_violation:
                    raise
                if conn.execute(
                    "SELECT 1 FROM users WHERE username = ?", (username,)
                ).fetchone() is None:
                    raise
            admin_row = conn.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            ).fetchone()
        if admin_row is not None:
            self._mirror_control_plane_row("users", int(admin_row["id"]))

    def create_user(self, username: str, password: str, role: str = ROLE_EMPLOYEE, department_id: int | None = None) -> AuthUser:
        raise PermissionError("Use create_user_as(actor, ...) so user creation is checked against the actor role.")

    def create_user_as(
        self,
        actor: AuthUser,
        username: str,
        password: str,
        role: str = ROLE_EMPLOYEE,
        department_id: int | None = None,
    ) -> AuthUser:
        """Active administrators can create staff or additional administrators.

        Employees belong to business departments; administrators belong to
        the system department. The deployment account keeps its existing
        environment-managed initialization.
        """
        scope = "system" if actor.role == ROLE_SYSTEM_ADMIN else "department"
        try:
            if not actor.is_active or actor.role != ROLE_SYSTEM_ADMIN:
                raise PermissionError("只有启用的系统管理员可以创建账号。")
            if role not in VALID_ROLES:
                raise ValueError("角色必须为 system_admin 或 employee")
            system_department_id = self._system_department_id()
            if role == ROLE_SYSTEM_ADMIN:
                if department_id is not None and department_id != system_department_id:
                    raise ValueError("系统管理员不能归属业务部门")
                department_id = system_department_id
            elif department_id is None or department_id == system_department_id:
                raise ValueError("员工必须归属到业务部门，不能归属 system 部门")

            self._audit_requested(
                "create_user",
                actor=actor,
                target_type="user",
                target_id=username.strip(),
                metadata={"role": role, "scope": scope},
            )
            created = self._create_user_record(username, password, role, department_id)
        except Exception as exc:
            self._audit(
                "create_user",
                actor=actor,
                target_type="user",
                target_id=username.strip(),
                success=False,
                error_message=str(exc),
                metadata={"role": role, "scope": scope},
            )
            raise
        self._audit(
            "create_user",
            actor=actor,
            target_type="user",
            target_id=created.username,
            metadata={"role": role, "scope": scope},
        )
        return created

    def _system_department_id(self) -> int | None:
        with closing(self._connect()) as conn:
            dept = conn.execute("SELECT id FROM departments WHERE name = 'system'").fetchone()
        return dept["id"] if dept else None

    def _create_user_record(self, username: str, password: str, role: str = ROLE_EMPLOYEE, department_id: int | None = None) -> AuthUser:
        username = username.strip()
        if not username:
            raise ValueError("用户名不能为空")
        if not password:
            raise ValueError("密码不能为空")
        if len(password) < 8:
            raise ValueError("密码长度不能少于 8 位")
        if role not in VALID_ROLES:
            raise ValueError("角色必须为 system_admin 或 employee")

        now = utc_now()
        with closing(self._connect()) as conn:
            if department_id is None:
                dept = conn.execute("SELECT id FROM departments WHERE name = 'system'").fetchone()
                department_id = dept["id"] if dept else None
            inserted = conn.execute(
                """
                INSERT INTO users (username, password_hash, role, department_id, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(username) DO NOTHING RETURNING id
                """,
                (username, hash_password(password), role, department_id, now, now),
            ).fetchone()
            if inserted is None:
                raise ValueError("用户名已存在")
            row = self._get_user_row(conn, username)
        self._mirror_control_plane_row("users", row["id"])
        return row_to_user(row)

    @staticmethod
    def _login_attempt_key(username: str, client_ip: str | None) -> str:
        return f"{username.strip().lower()}|{client_ip or ''}"

    def _login_lock_remaining(self, conn, key: str) -> int | None:
        """Seconds left in the current lock window, or None when unlocked."""
        row = conn.execute(
            "SELECT locked_until FROM login_attempts WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None or not row["locked_until"]:
            return None
        try:
            locked_until = datetime.fromisoformat(row["locked_until"])
        except ValueError:
            return None
        remaining = (locked_until - datetime.now(timezone.utc)).total_seconds()
        return int(remaining) if remaining > 0 else None

    def _record_login_failure(self, key: str, username: str, client_ip: str | None) -> None:
        with closing(self._connect()) as conn:
            with _LOGIN_TRACKER_LOCK:
                conn.execute(
                    """
                    INSERT INTO login_attempts (key, username, client_ip, failed_count, locked_until, last_failure_at)
                    VALUES (?, ?, ?, 1, NULL, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        failed_count = login_attempts.failed_count + 1,
                        last_failure_at = excluded.last_failure_at
                    """,
                    (key, username, client_ip or "", utc_now()),
                )
                count = int(
                    conn.execute(
                        "SELECT failed_count FROM login_attempts WHERE key = ?",
                        (key,),
                    ).fetchone()["failed_count"]
                )
                if count >= LOGIN_MAX_FAILURES:
                    delay = min(
                        LOGIN_BASE_LOCK_SECONDS * (2 ** (count - LOGIN_MAX_FAILURES)),
                        LOGIN_MAX_LOCK_SECONDS,
                    )
                    conn.execute(
                        "UPDATE login_attempts SET locked_until = ? WHERE key = ?",
                        (
                            (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(),
                            key,
                        ),
                    )

    def _clear_login_failures(self, key: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM login_attempts WHERE key = ?", (key,))

    def authenticate(
        self, username: str, password: str, ip: str | None = None
    ) -> tuple[AuthSession | None, str]:
        """Authenticate and return ``(session, reason)``.

        ``reason`` is a user-facing Chinese message on failure (empty on
        success). Nonexistent user and wrong password share one message to
        avoid account enumeration; disabled accounts and lock windows get
        distinct messages so users know re-trying is pointless vs. wait.
        """
        clean_username = username.strip()
        attempt_key = self._login_attempt_key(clean_username, ip)

        # H3 brute-force guard: short-circuit before the full-price PBKDF2
        # verify while this (username, ip) pair sits in a back-off lock window.
        # Degrade to no counting on tracker errors -- never block valid logins.
        lock_remaining: int | None = None
        try:
            with closing(self._connect()) as conn:
                with _LOGIN_TRACKER_LOCK:
                    lock_remaining = self._login_lock_remaining(conn, attempt_key)
        except Exception:
            lock_remaining = None
        if lock_remaining is not None:
            reason = f"登录失败次数过多，账号已临时锁定，请约 {lock_remaining} 秒后重试"
            self._audit(
                "login_failed",
                target_type="user",
                target_id=clean_username,
                success=False,
                error_message=reason,
                metadata={"client_ip": ip or "", "locked_remaining_seconds": lock_remaining},
            )
            return None, reason

        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT u.*, d.name AS department_name
                FROM users u
                LEFT JOIN departments d ON d.id = u.department_id
                WHERE u.username = ?
                """,
                (clean_username,),
            ).fetchone()
            if row is None:
                try:
                    self._record_login_failure(attempt_key, clean_username, ip)
                except Exception:
                    pass
                reason = "用户名或密码错误"
                self._audit(
                    "login_failed",
                    target_type="user",
                    target_id=clean_username,
                    success=False,
                    error_message=reason,
                    metadata={"client_ip": ip or ""},
                )
                return None, reason
            if not bool(row["is_active"]):
                reason = "账号已被停用，请联系系统管理员"
                self._audit(
                    "login_failed",
                    target_type="user",
                    target_id=clean_username,
                    success=False,
                    error_message=reason,
                    metadata={"client_ip": ip or ""},
                )
                return None, reason
            if not verify_password(password, row["password_hash"]):
                try:
                    self._record_login_failure(attempt_key, clean_username, ip)
                except Exception:
                    pass
                reason = "用户名或密码错误"
                self._audit(
                    "login_failed",
                    target_type="user",
                    target_id=clean_username,
                    success=False,
                    error_message=reason,
                    metadata={"client_ip": ip or ""},
                )
                return None, reason

            token = secrets.token_urlsafe(32)
            now_dt = datetime.now(timezone.utc)
            expires_dt = now_dt + timedelta(hours=src.settings.AUTH_SESSION_TTL_HOURS)
            conn.execute(
                """
                INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (hash_token(token), row["id"], now_dt.isoformat(), expires_dt.isoformat()),
            )

        try:
            self._clear_login_failures(attempt_key)
        except Exception:
            pass
        user = row_to_user(row)
        self._audit(
            "login_success",
            actor=user,
            target_type="user",
            target_id=user.username,
            success=True,
        )
        return AuthSession(token=token, user=user, expires_at=expires_dt.isoformat()), ""

    def get_user_by_token(self, token: str | None) -> AuthUser | None:
        if not token:
            return None
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT u.*, d.name AS department_name
                FROM auth_sessions s
                JOIN users u ON u.id = s.user_id
                LEFT JOIN departments d ON d.id = u.department_id
                WHERE s.token_hash = ?
                  AND s.revoked_at IS NULL
                  AND s.expires_at > ?
                  AND u.is_active = 1
                """,
                (hash_token(token), now),
            ).fetchone()
        return row_to_user(row) if row else None

    def revoke_session(self, token: str | None, actor: AuthUser | None = None):
        if not token:
            return
        # Caller may pass the already-resolved actor (e.g. the API logout route,
        # which has current_user) to avoid a second get_user_by_token query.
        if actor is None:
            actor = self.get_user_by_token(token)
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (utc_now(), hash_token(token)),
            )
        self._audit(
            "logout",
            actor=actor,
            target_type="user",
            target_id=actor.username if actor else "",
        )

    def list_users(self) -> list[AuthUser]:
        with closing(self._connect()) as conn:
            rows = conn.execute("""
                SELECT u.*, d.name AS department_name
                FROM users u
                LEFT JOIN departments d ON d.id = u.department_id
                ORDER BY u.id
            """).fetchall()
        return [row_to_user(row) for row in rows]

    def list_users_as(self, actor: AuthUser) -> list[AuthUser]:
        if actor.role == ROLE_SYSTEM_ADMIN:
            return self.list_users()
        raise PermissionError("User listing requires the system administrator role.")

    def list_departments(self) -> list[Department]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM departments ORDER BY id").fetchall()
        return [Department(id=int(row["id"]), name=row["name"]) for row in rows]

    def set_user_active(self, user_id: int, is_active: bool):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise ValueError("用户不存在")
            conn.execute(
                "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
                (1 if is_active else 0, utc_now(), user_id),
            )
            if not is_active:
                conn.execute(
                    "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                    (utc_now(), user_id),
                )
        self._mirror_control_plane_row("users", user_id)

    def set_user_active_as(self, actor: AuthUser, user_id: int, is_active: bool):
        scope = "system" if actor.role == ROLE_SYSTEM_ADMIN else "department"
        try:
            with closing(self._connect()) as conn:
                target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                if target is None:
                    raise ValueError("User does not exist")
                if actor.id == user_id:
                    raise PermissionError("Cannot change the active state of the current account here.")
                if actor.role != ROLE_SYSTEM_ADMIN:
                    raise PermissionError("Account activation requires the system administrator role.")
            self._audit_requested(
                "set_user_active",
                actor=actor,
                target_type="user",
                target_id=str(user_id),
                metadata={"is_active": is_active, "scope": scope},
            )
            self.set_user_active(user_id, is_active)
        except Exception as exc:
            self._audit(
                "set_user_active",
                actor=actor,
                target_type="user",
                target_id=str(user_id),
                success=False,
                error_message=str(exc),
                metadata={"is_active": is_active, "scope": scope},
            )
            raise
        self._audit(
            "set_user_active",
            actor=actor,
            target_type="user",
            target_id=target["username"],
            metadata={"is_active": is_active, "scope": scope},
        )

    def reset_user_password_as(self, actor: AuthUser, user_id: int, new_password: str):
        scope = "system" if actor.role == ROLE_SYSTEM_ADMIN else "department"
        try:
            if not new_password:
                raise ValueError("密码不能为空")
            if len(new_password) < 8:
                raise ValueError("密码长度不能少于 8 位")

            self._audit_requested(
                "reset_user_password",
                actor=actor,
                target_type="user",
                target_id=str(user_id),
                metadata={"scope": scope},
            )
            with closing(self._connect()) as conn:
                target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                if target is None:
                    raise ValueError("目标用户不存在")
                if actor.id == user_id:
                    raise PermissionError("不能在这里重置当前登录账号密码")

                if actor.role != ROLE_SYSTEM_ADMIN:
                    raise PermissionError("账号管理需要系统管理员权限")

                conn.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, managed_by_env = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (hash_password(new_password), utc_now(), user_id),
                )
                conn.execute(
                    "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                    (utc_now(), user_id),
                )
                self._mirror_control_plane_row("users", user_id)
        except Exception as exc:
            self._audit(
                "reset_user_password",
                actor=actor,
                target_type="user",
                target_id=str(user_id),
                success=False,
                error_message=str(exc),
                metadata={"scope": scope},
            )
            raise
        self._audit(
            "reset_user_password",
            actor=actor,
            target_type="user",
            target_id=target["username"],
            metadata={"scope": scope},
        )

    def change_own_password(
        self,
        user: AuthUser,
        current_password: str,
        new_password: str,
    ):
        """Change the signed-in account's own password (current password required).

        Revokes every session for the account, so the caller must sign in again
        with the new password.
        """
        try:
            if not current_password:
                raise ValueError("请输入当前密码")
            if not new_password:
                raise ValueError("密码不能为空")
            if len(new_password) < 8:
                raise ValueError("密码长度不能少于 8 位")
            if new_password == current_password:
                raise ValueError("新密码不能与当前密码相同")

            self._audit_requested(
                "change_own_password",
                actor=user,
                target_type="user",
                target_id=user.username,
            )
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT * FROM users WHERE id = ?", (user.id,)).fetchone()
                if row is None:
                    raise ValueError("账号不存在")
                if not row["is_active"]:
                    raise PermissionError("账号已停用")
                if not verify_password(current_password, row["password_hash"]):
                    raise PermissionError("当前密码不正确")

                conn.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, managed_by_env = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (hash_password(new_password), utc_now(), user.id),
                )
                conn.execute(
                    "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                    (utc_now(), user.id),
                )
                self._mirror_control_plane_row("users", user.id)
        except Exception as exc:
            self._audit(
                "change_own_password",
                actor=user,
                target_type="user",
                target_id=str(user.id),
                success=False,
                error_message=str(exc),
            )
            raise
        self._audit(
            "change_own_password",
            actor=user,
            target_type="user",
            target_id=user.username,
        )

    def create_department(self, name: str) -> Department:
        name = name.strip()
        if not name:
            raise ValueError("部门名称不能为空")
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO departments (name, created_at) VALUES (?, ?)",
                (name, utc_now()),
            )
            row = conn.execute("SELECT * FROM departments WHERE name = ?", (name,)).fetchone()
        self._mirror_control_plane_row("departments", int(row["id"]))
        return Department(id=int(row["id"]), name=row["name"])

    def create_department_as(self, actor: AuthUser, name: str) -> Department:
        try:
            if actor.role != ROLE_SYSTEM_ADMIN:
                raise PermissionError("Department creation requires a system administrator role.")
            self._audit_requested(
                "create_department",
                actor=actor,
                target_type="department",
                target_id=name.strip(),
            )
            dept = self.create_department(name)
        except Exception as exc:
            self._audit(
                "create_department",
                actor=actor,
                target_type="department",
                target_id=name.strip(),
                success=False,
                error_message=str(exc),
            )
            raise
        self._audit(
            "create_department",
            actor=actor,
            target_type="department",
            target_id=dept.name,
        )
        return dept

    def delete_department(self, department_id: int):
        with closing(self._connect()) as conn:
            dept = conn.execute("SELECT * FROM departments WHERE id = ?", (department_id,)).fetchone()
            if dept is None:
                raise ValueError("部门不存在")
            if dept["name"] == "system":
                raise ValueError("system 部门不可删除")
            user_count = conn.execute(
                "SELECT COUNT(*) AS count FROM users WHERE department_id = ?",
                (department_id,),
            ).fetchone()["count"]
            if user_count:
                raise ValueError("该部门下仍有用户，不能删除")
            kb_count = conn.execute(
                "SELECT COUNT(*) AS count FROM knowledge_bases WHERE department_id = ?",
                (department_id,),
            ).fetchone()["count"]
            if kb_count:
                raise ValueError("该部门下仍有关联知识库，不能删除")
            conn.execute("DELETE FROM departments WHERE id = ?", (department_id,))
        self._mirror_control_plane_row("departments", department_id, delete=True)

    def delete_department_as(self, actor: AuthUser, department_id: int):
        try:
            if actor.role != ROLE_SYSTEM_ADMIN:
                raise PermissionError("Department deletion requires a system administrator role.")
            self._audit_requested(
                "delete_department",
                actor=actor,
                target_type="department",
                target_id=str(department_id),
            )
            self.delete_department(department_id)
        except Exception as exc:
            self._audit(
                "delete_department",
                actor=actor,
                target_type="department",
                target_id=str(department_id),
                success=False,
                error_message=str(exc),
            )
            raise
        self._audit(
            "delete_department",
            actor=actor,
            target_type="department",
            target_id=str(department_id),
        )

    def _get_kb_row(
        self,
        conn,
        kb_name: str | None = None,
        department_id: int | str | None = None,
        kb_id: int | None = None,
    ):
        if kb_id is not None:
            return conn.execute("SELECT * FROM knowledge_bases WHERE id = ?", (kb_id,)).fetchone()
        if kb_name is None:
            return None
        kb_name = kb_name.strip()
        if department_id not in (None, ""):
            return conn.execute(
                "SELECT * FROM knowledge_bases WHERE name = ? AND department_id = ?",
                (kb_name, int(department_id)),
            ).fetchone()
        rows = conn.execute("SELECT * FROM knowledge_bases WHERE name = ?", (kb_name,)).fetchall()
        if len(rows) > 1:
            raise ValueError(f"Ambiguous knowledge base name without department scope: {kb_name}")
        return rows[0] if rows else None

    def register_knowledge_base(self, kb_name: str, owner: AuthUser | None = None, is_shared: bool = False):
        """登记知识库归属。新库默认私有（仅所有者可访问），由所有者决定是否部门共享。"""
        kb_name = kb_name.strip()
        department_id = owner.department_id if owner else None
        owner_id = owner.id if owner else None
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO knowledge_bases (name, department_id, owner_user_id, is_shared, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (kb_name, department_id, owner_id, 1 if is_shared else 0, utc_now()),
            )

    def knowledge_base_exists(
        self,
        kb_name: str,
        department_id: int | str | None = None,
        kb_id: int | None = None,
    ) -> bool:
        with closing(self._connect()) as conn:
            return self._get_kb_row(conn, kb_name, department_id, kb_id) is not None

    def get_knowledge_base_id(
        self,
        kb_name: str,
        department_id: int | str | None = None,
        kb_id: int | None = None,
    ) -> int | None:
        with closing(self._connect()) as conn:
            row = self._get_kb_row(conn, kb_name, department_id, kb_id)
        return int(row["id"]) if row else None

    def get_knowledge_base_identity(self, kb_id: int) -> KnowledgeBaseIdentity | None:
        """Read stable resource identity; this does not authorize content access."""
        with closing(self._connect()) as conn:
            row = self._get_kb_row(conn, kb_id=kb_id)
        if row is None:
            return None
        return KnowledgeBaseIdentity(
            kb_id=int(row["id"]),
            kb_name=str(row["name"]),
            department_id=int(row["department_id"]) if row["department_id"] is not None else None,
        )

    def get_knowledge_base_owner(
        self,
        kb_name: str,
        department_id: int | str | None = None,
        kb_id: int | None = None,
    ) -> int | None:
        """返回知识库创建者 user id；历史库无 owner 时返回 None。"""
        with closing(self._connect()) as conn:
            kb = self._get_kb_row(conn, kb_name, department_id, kb_id)
            if kb is None:
                return None
            owner = kb["owner_user_id"]
            return int(owner) if owner is not None else None

    def get_knowledge_base_sharing(
        self,
        kb_name: str,
        department_id: int | str | None = None,
        kb_id: int | None = None,
    ) -> tuple[int | None, bool] | None:
        """返回 ``(owner_user_id, is_shared)``；知识库不存在时返回 None。"""
        with closing(self._connect()) as conn:
            kb = self._get_kb_row(conn, kb_name, department_id, kb_id)
            if kb is None:
                return None
            owner = kb["owner_user_id"]
            return (int(owner) if owner is not None else None, bool(kb["is_shared"]))

    def set_knowledge_base_sharing(
        self,
        kb_name: str,
        actor: AuthUser,
        is_shared: bool,
        department_id: int | str | None = None,
        kb_id: int | None = None,
    ) -> bool:
        """切换知识库的部门共享状态；仅所有者可操作。

        这是治理敏感动作(共享后全部门获得 admin)，因此在这里统一审计。
        """
        target_id = kb_name
        try:
            if actor is None or actor.role != ROLE_EMPLOYEE:
                raise PermissionError("只有部门员工可以调整知识库共享状态。")
            self._audit_requested(
                "update_kb_sharing",
                actor=actor,
                target_type="knowledge_base",
                target_id=target_id,
                kb_name=target_id,
                metadata={"is_shared": bool(is_shared)},
            )
            with closing(self._connect()) as conn:
                kb = self._get_kb_row(conn, kb_name, department_id, kb_id)
                if kb is None:
                    raise ValueError("知识库不存在")
                target_id = kb["name"]
                owner_id = kb["owner_user_id"]
                if owner_id is None:
                    raise PermissionError("该知识库未指定所有者，无法切换共享状态。")
                if int(owner_id) != int(actor.id):
                    raise PermissionError("只有知识库所有者可以调整共享状态。")
                conn.execute(
                    "UPDATE knowledge_bases SET is_shared = ? WHERE id = ?",
                    (1 if is_shared else 0, kb["id"]),
                )
        except Exception as exc:
            self._audit(
                "update_kb_sharing",
                actor=actor,
                target_type="knowledge_base",
                target_id=target_id,
                kb_name=target_id,
                success=False,
                error_message=str(exc),
                metadata={"is_shared": bool(is_shared)},
            )
            raise
        self._audit(
            "update_kb_sharing",
            actor=actor,
            target_type="knowledge_base",
            target_id=target_id,
            kb_name=target_id,
            metadata={"is_shared": bool(is_shared), "owner_user_id": int(actor.id)},
        )
        return bool(is_shared)

    def delete_knowledge_base_record(
        self,
        kb_name: str,
        department_id: int | str | None = None,
        kb_id: int | None = None,
    ):
        with closing(self._connect()) as conn:
            kb = self._get_kb_row(conn, kb_name, department_id, kb_id)
            if kb is None:
                return
            internal_kb_id = int(kb["id"])
            # External mappings are deliberately removed with the KB.  Keeping
            # them (even retired) would either retain dangling FK rows or let a
            # later KB reuse an old opaque reference.  The delete order honors
            # the foreign keys on both SQLite and PostgreSQL.
            conn.execute(
                "DELETE FROM employee_api_key_kbs WHERE kb_id = ?", (internal_kb_id,)
            )
            conn.execute(
                "DELETE FROM external_upload_idempotency WHERE kb_id = ?",
                (internal_kb_id,),
            )
            conn.execute(
                "DELETE FROM external_evidence WHERE kb_id = ?", (internal_kb_id,)
            )
            conn.execute(
                "DELETE FROM external_source_scopes WHERE kb_id = ?",
                (internal_kb_id,),
            )
            conn.execute(
                "DELETE FROM external_ingestion_jobs WHERE kb_id = ?", (internal_kb_id,)
            )
            conn.execute(
                "DELETE FROM external_file_refs WHERE kb_id = ?", (internal_kb_id,)
            )
            conn.execute(
                "DELETE FROM external_asset_refs WHERE kb_id = ?", (internal_kb_id,)
            )
            conn.execute(
                "DELETE FROM external_kb_refs WHERE kb_id = ?", (internal_kb_id,)
            )
            conn.execute("DELETE FROM knowledge_bases WHERE id = ?", (internal_kb_id,))

    def list_registered_knowledge_bases(self) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT name FROM knowledge_bases ORDER BY name").fetchall()
        return [row["name"] for row in rows]

    def list_knowledge_base_summaries(self, existing_kbs: list[str]) -> list[KnowledgeBaseSummary]:
        existing = set(existing_kbs)
        summaries: dict[str, KnowledgeBaseSummary] = {}
        with closing(self._connect()) as conn:
            employee_counts = {
                row["department_id"]: int(row["count"])
                for row in conn.execute(
                    """
                    SELECT department_id, COUNT(*) AS count
                    FROM users
                    WHERE role = ? AND is_active = 1 AND department_id IS NOT NULL
                    GROUP BY department_id
                    """,
                    (ROLE_EMPLOYEE,),
                ).fetchall()
            }
            rows = conn.execute(
                """
                SELECT
                    kb.id,
                    kb.name,
                    kb.department_id,
                    d.name AS department_name,
                    kb.owner_user_id,
                    owner.username AS owner_username,
                    kb.is_shared,
                    kb.created_at
                FROM knowledge_bases kb
                LEFT JOIN departments d ON d.id = kb.department_id
                LEFT JOIN users owner ON owner.id = kb.owner_user_id
                ORDER BY d.name, kb.name
                """
            ).fetchall()

        for row in rows:
            department_id = row["department_id"]
            summary_key = f"{department_id or 'none'}:{row['name']}"
            summaries[summary_key] = KnowledgeBaseSummary(
                name=row["name"],
                kb_id=int(row["id"]),
                department_id=department_id,
                department_name=row["department_name"],
                owner_user_id=row["owner_user_id"],
                owner_username=row["owner_username"],
                is_shared=bool(row["is_shared"]),
                employee_count=employee_counts.get(department_id, 0),
                registered=True,
                physical_exists=row["name"] in existing,
                created_at=row["created_at"] or "",
            )

        registered_names = {row["name"] for row in rows}
        for kb_name in sorted(existing - registered_names):
            summaries[kb_name] = KnowledgeBaseSummary(
                name=kb_name,
                registered=False,
                physical_exists=True,
            )

        return sorted(summaries.values(), key=lambda item: (item.department_name or "", item.name))

    def assign_knowledge_base_as(
        self,
        actor: AuthUser,
        kb_name: str,
        department_id: int,
        owner_user_id: int | None = None,
        source_kb_id: int | None = None,
    ):
        try:
            if actor is None or actor.role != ROLE_SYSTEM_ADMIN:
                raise PermissionError("只有系统管理员可以调整知识库归属。")
            if not kb_name.strip():
                raise ValueError("知识库名称不能为空")

            kb_name = kb_name.strip()
            self._audit_requested(
                "assign_kb",
                actor=actor,
                target_type="knowledge_base",
                target_id=kb_name,
                kb_name=kb_name,
                metadata={
                    "department_id": department_id,
                    "owner_user_id": owner_user_id,
                    "source_kb_id": source_kb_id,
                },
            )
            with closing(self._connect()) as conn:
                department = conn.execute("SELECT id, name FROM departments WHERE id = ?", (department_id,)).fetchone()
                if department is None:
                    raise ValueError("部门不存在")
                if department["name"] == "system":
                    raise ValueError("业务知识库不能挂载到 system 部门")

                source_kb = self._get_kb_row(conn, kb_name, kb_id=source_kb_id)
                target_kb = self._get_kb_row(conn, kb_name, department_id)
                if source_kb is not None and target_kb is not None and target_kb["id"] != source_kb["id"]:
                    raise ValueError("目标部门已存在同名知识库")
                if source_kb is not None and source_kb["department_id"] not in (None, department_id):
                    # RAGFlow chunks and local structured indexes are scoped by
                    # department metadata. Moving only this SQL row would make
                    # documents disappear or cross tenant boundaries.
                    raise ValueError("暂不支持跨部门迁移已有知识库；请在目标部门新建知识库并执行受控导入。")

                if owner_user_id is not None:
                    owner = conn.execute("SELECT * FROM users WHERE id = ?", (owner_user_id,)).fetchone()
                    if owner is None:
                        raise ValueError("负责人不存在")
                    if owner["role"] != ROLE_EMPLOYEE:
                        raise ValueError("知识库负责人必须是部门员工")
                    if owner["department_id"] != department_id:
                        raise ValueError("负责人必须属于所选部门")

                if source_kb is None:
                    conn.execute(
                        """
                        INSERT INTO knowledge_bases (name, department_id, owner_user_id, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (kb_name, department_id, owner_user_id, utc_now()),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE knowledge_bases
                        SET department_id = ?, owner_user_id = ?
                        WHERE id = ?
                        """,
                        (department_id, owner_user_id, source_kb["id"]),
                    )
        except Exception as exc:
            self._audit(
                "assign_kb",
                actor=actor,
                target_type="knowledge_base",
                target_id=kb_name.strip() if kb_name else "",
                kb_name=kb_name.strip() if kb_name else "",
                success=False,
                error_message=str(exc),
                metadata={"department_id": department_id, "owner_user_id": owner_user_id, "source_kb_id": source_kb_id},
            )
            raise
        self._audit(
            "assign_kb",
            actor=actor,
            target_type="knowledge_base",
            target_id=kb_name,
            kb_name=kb_name,
            metadata={
                "department_id": department_id,
                "owner_user_id": owner_user_id,
                "source_kb_id": source_kb_id,
            },
        )

    def list_accessible_kbs(self, user: AuthUser, existing_kbs: list[str]) -> list[str]:
        """员工可见的知识库: 自己是所有者, 或所有者已开启部门共享。

        无主知识库(历史登记或治理挂载未指定负责人)保持部门可见, 避免数据失联。
        系统管理员不参与内容访问。
        """
        if user.role == ROLE_SYSTEM_ADMIN:
            return []

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT name FROM knowledge_bases
                WHERE department_id = ?
                  AND (is_shared = 1 OR owner_user_id IS NULL OR owner_user_id = ?)
                """,
                (user.department_id, user.id),
            ).fetchall()
        allowed = {row["name"] for row in rows}
        return [kb for kb in existing_kbs if kb in allowed]

    def get_kb_permissions_for_user(self, user: AuthUser, existing_kbs: list[str] | None = None) -> dict[str, str]:
        """员工对自己创建的知识库、以及所有者已共享的知识库拥有 admin 权限。

        私有库对其他同部门员工完全不可见(不落授权表)。无主历史库保持部门可见。
        """
        if existing_kbs is None:
            existing_filter = None
        else:
            existing_filter = set(existing_kbs)

        if user.role == ROLE_SYSTEM_ADMIN:
            return {}

        permissions: dict[str, str] = {}
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT name, department_id FROM knowledge_bases
                WHERE department_id = ?
                  AND (is_shared = 1 OR owner_user_id IS NULL OR owner_user_id = ?)
                """,
                (user.department_id, user.id),
            ).fetchall()
        for row in rows:
            if existing_filter is not None and row["name"] not in existing_filter:
                continue
            permissions[kb_scope_key(row["name"], row["department_id"])] = "admin"
        return permissions

    def list_accessible_knowledge_base_identities(
        self, user: AuthUser
    ) -> list[KnowledgeBaseIdentity]:
        """Return live employee KB identities without exposing them externally."""
        if user.role != ROLE_EMPLOYEE or not user.is_active or user.department_id is None:
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, name, department_id
                FROM knowledge_bases
                WHERE department_id = ?
                  AND (is_shared = 1 OR owner_user_id IS NULL OR owner_user_id = ?)
                ORDER BY name, id
                """,
                (user.department_id, user.id),
            ).fetchall()
        return [
            KnowledgeBaseIdentity(
                kb_id=int(row["id"]),
                kb_name=str(row["name"]),
                department_id=int(row["department_id"]) if row["department_id"] is not None else None,
            )
            for row in rows
        ]

    def list_all_knowledge_base_identities(self) -> list[KnowledgeBaseIdentity]:
        """Return every registered KB for migration/backfill tooling."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, name, department_id FROM knowledge_bases ORDER BY department_id, name, id"
            ).fetchall()
        return [
            KnowledgeBaseIdentity(
                kb_id=int(row["id"]),
                kb_name=str(row["name"]),
                department_id=int(row["department_id"]) if row["department_id"] is not None else None,
            )
            for row in rows
        ]

    def ensure_external_kb_ref(self, kb_id: int) -> ExternalKnowledgeBaseReference:
        """Create an opaque, never-reused KB reference on first observation."""
        with closing(self._connect()) as conn:
            kb = conn.execute(
                """
                SELECT kb.id, kb.name, kb.department_id, d.name AS department_name
                FROM knowledge_bases kb
                LEFT JOIN departments d ON d.id = kb.department_id
                WHERE kb.id = ?
                """,
                (int(kb_id),),
            ).fetchone()
            if kb is None:
                raise ValueError("知识库不存在")
            existing = conn.execute(
                """
                SELECT kb_ref FROM external_kb_refs
                WHERE kb_id = ?
                """,
                (int(kb_id),),
            ).fetchone()
            if existing is None:
                # A URL-safe random token is opaque and non-enumerable; it
                # never contains or encodes the internal integer KB id.
                kb_ref = f"kbref_{secrets.token_urlsafe(18)}"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO external_kb_refs
                        (kb_ref, kb_id, created_at, retired_at)
                    VALUES (?, ?, ?, NULL)
                    """,
                    (kb_ref, int(kb_id), utc_now()),
                )
                existing = conn.execute(
                    "SELECT kb_ref FROM external_kb_refs WHERE kb_id = ?",
                    (int(kb_id),),
                ).fetchone()
            return ExternalKnowledgeBaseReference(
                kb_ref=str(existing["kb_ref"]),
                kb_id=int(kb["id"]),
                kb_name=str(kb["name"]),
                department_id=int(kb["department_id"]) if kb["department_id"] is not None else None,
                department_name=kb["department_name"],
            )

    def get_external_kb_ref(self, kb_ref: str) -> ExternalKnowledgeBaseReference | None:
        if not kb_ref or len(str(kb_ref)) > 200:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT e.kb_ref, kb.id AS kb_id, kb.name AS kb_name,
                       kb.department_id, d.name AS department_name
                FROM external_kb_refs e
                JOIN knowledge_bases kb ON kb.id = e.kb_id
                LEFT JOIN departments d ON d.id = kb.department_id
                WHERE e.kb_ref = ? AND e.retired_at IS NULL
                """,
                (str(kb_ref),),
            ).fetchone()
        if row is None:
            return None
        return ExternalKnowledgeBaseReference(
            kb_ref=str(row["kb_ref"]),
            kb_id=int(row["kb_id"]),
            kb_name=str(row["kb_name"]),
            department_id=int(row["department_id"]) if row["department_id"] is not None else None,
            department_name=row["department_name"],
        )

    def ensure_external_file_ref(self, kb_id: int, record_id: int) -> ExternalFileReference:
        """Create an opaque file reference for a scoped pipeline record."""
        with closing(self._connect()) as conn:
            kb = conn.execute("SELECT id FROM knowledge_bases WHERE id = ?", (int(kb_id),)).fetchone()
            if kb is None:
                raise ValueError("知识库不存在")
            row = conn.execute(
                """
                SELECT file_ref FROM external_file_refs
                WHERE kb_id = ? AND record_id = ? AND retired_at IS NULL
                """,
                (int(kb_id), int(record_id)),
            ).fetchone()
            if row is None:
                file_ref = f"fileref_{secrets.token_urlsafe(18)}"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO external_file_refs
                        (file_ref, kb_id, record_id, created_at, retired_at)
                    VALUES (?, ?, ?, ?, NULL)
                    """,
                    (file_ref, int(kb_id), int(record_id), utc_now()),
                )
                row = conn.execute(
                    """
                    SELECT file_ref FROM external_file_refs
                    WHERE kb_id = ? AND record_id = ? AND retired_at IS NULL
                    """,
                    (int(kb_id), int(record_id)),
                ).fetchone()
            if row is None:
                raise RuntimeError("无法建立外部文件引用")
            return ExternalFileReference(
                file_ref=str(row["file_ref"]), kb_id=int(kb_id), record_id=int(record_id)
            )

    def get_external_file_ref(self, file_ref: str) -> ExternalFileReference | None:
        if not file_ref or len(str(file_ref)) > 200:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT file_ref, kb_id, record_id
                FROM external_file_refs
                WHERE file_ref = ? AND retired_at IS NULL
                """,
                (str(file_ref),),
            ).fetchone()
        if row is None:
            return None
        return ExternalFileReference(
            file_ref=str(row["file_ref"]),
            kb_id=int(row["kb_id"]),
            record_id=int(row["record_id"]),
        )

    def ensure_external_asset_ref(
        self, kb_id: int, asset_id: int
    ) -> ExternalAssetReference:
        with closing(self._connect()) as conn:
            kb = conn.execute(
                "SELECT id FROM knowledge_bases WHERE id = ?", (int(kb_id),)
            ).fetchone()
            if kb is None:
                raise ValueError("知识库不存在")
            row = conn.execute(
                """
                SELECT asset_ref FROM external_asset_refs
                WHERE kb_id = ? AND asset_id = ? AND retired_at IS NULL
                """,
                (int(kb_id), int(asset_id)),
            ).fetchone()
            if row is None:
                asset_ref = f"assetref_{secrets.token_urlsafe(18)}"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO external_asset_refs
                        (asset_ref, kb_id, asset_id, created_at, retired_at)
                    VALUES (?, ?, ?, ?, NULL)
                    """,
                    (asset_ref, int(kb_id), int(asset_id), utc_now()),
                )
                row = conn.execute(
                    """
                    SELECT asset_ref FROM external_asset_refs
                    WHERE kb_id = ? AND asset_id = ? AND retired_at IS NULL
                    """,
                    (int(kb_id), int(asset_id)),
                ).fetchone()
            if row is None:
                raise RuntimeError("无法建立外部资产引用")
            return ExternalAssetReference(
                asset_ref=str(row["asset_ref"]),
                kb_id=int(kb_id),
                asset_id=int(asset_id),
            )

    def get_external_asset_ref(self, asset_ref: str) -> ExternalAssetReference | None:
        if not asset_ref or len(str(asset_ref)) > 200:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT asset_ref, kb_id, asset_id
                FROM external_asset_refs
                WHERE asset_ref = ? AND retired_at IS NULL
                """,
                (str(asset_ref),),
            ).fetchone()
        if row is None:
            return None
        return ExternalAssetReference(
            asset_ref=str(row["asset_ref"]),
            kb_id=int(row["kb_id"]),
            asset_id=int(row["asset_id"]),
        )

    def ensure_external_job_ref(
        self, *, key_id: str, kb_ref: str, kb_id: int, record_id: int
    ) -> ExternalIngestionJob:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT job_ref FROM external_ingestion_jobs
                WHERE kb_id = ? AND record_id = ? AND key_id = ?
                """,
                (int(kb_id), int(record_id), str(key_id)),
            ).fetchone()
            if row is None:
                job_ref = f"jobref_{secrets.token_urlsafe(18)}"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO external_ingestion_jobs
                        (job_ref, kb_ref, kb_id, record_id, key_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_ref,
                        str(kb_ref),
                        int(kb_id),
                        int(record_id),
                        str(key_id),
                        utc_now(),
                    ),
                )
                row = conn.execute(
                    """
                    SELECT job_ref FROM external_ingestion_jobs
                    WHERE kb_id = ? AND record_id = ? AND key_id = ?
                    """,
                    (int(kb_id), int(record_id), str(key_id)),
                ).fetchone()
            if row is None:
                raise RuntimeError("无法建立外部入库任务引用")
            return ExternalIngestionJob(
                job_ref=str(row["job_ref"]),
                kb_ref=str(kb_ref),
                kb_id=int(kb_id),
                record_id=int(record_id),
                key_id=str(key_id),
            )

    def get_external_job(self, job_ref: str, key_id: str) -> ExternalIngestionJob | None:
        if not job_ref or len(str(job_ref)) > 200:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT job_ref, kb_ref, kb_id, record_id, key_id
                FROM external_ingestion_jobs
                WHERE job_ref = ? AND key_id = ?
                """,
                (str(job_ref), str(key_id)),
            ).fetchone()
        if row is None:
            return None
        return ExternalIngestionJob(
            job_ref=str(row["job_ref"]),
            kb_ref=str(row["kb_ref"]),
            kb_id=int(row["kb_id"]),
            record_id=int(row["record_id"]),
            key_id=str(row["key_id"]),
        )

    def claim_external_upload(
        self,
        *,
        key_id: str,
        idempotency_key: str,
        request_hash: str,
        kb_id: int,
    ) -> tuple[str, str]:
        """Claim an upload idempotency slot.

        Returns ``("new", "")``, ``("replay", response_json)``,
        ``("in_progress", "")`` or ``("conflict", "")``.
        """
        ttl_seconds = max(
            1,
            int(external_settings.HDB_EXTERNAL_UPLOAD_IDEMPOTENCY_TTL_SECONDS or 3600),
        )
        with closing(self._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """
                    SELECT request_hash, response_json, created_at
                    FROM external_upload_idempotency
                    WHERE key_id = ? AND idempotency_key = ?
                    """,
                    (str(key_id), str(idempotency_key)),
                ).fetchone()
                if row is not None:
                    if str(row["request_hash"] or "") != str(request_hash):
                        conn.rollback()
                        return "conflict", ""
                    response_json = str(row["response_json"] or "")
                    if response_json:
                        conn.rollback()
                        return "replay", response_json
                    try:
                        age = (
                            datetime.now(timezone.utc)
                            - datetime.fromisoformat(str(row["created_at"]))
                        ).total_seconds()
                    except (ValueError, TypeError):
                        age = ttl_seconds + 1
                    if age <= ttl_seconds:
                        conn.rollback()
                        return "in_progress", ""
                    conn.execute(
                        """
                        DELETE FROM external_upload_idempotency
                        WHERE key_id = ? AND idempotency_key = ?
                        """,
                        (str(key_id), str(idempotency_key)),
                    )
                conn.execute(
                    """
                    INSERT INTO external_upload_idempotency
                        (key_id, idempotency_key, kb_id, request_hash,
                         response_json, created_at)
                    VALUES (?, ?, ?, ?, '', ?)
                    """,
                    (
                        str(key_id),
                        str(idempotency_key),
                        int(kb_id),
                        str(request_hash),
                        utc_now(),
                    ),
                )
                conn.commit()
                return "new", ""
            except Exception:
                conn.rollback()
                # A concurrent claimant may have won the unique insert race.
                row = conn.execute(
                    """
                    SELECT request_hash, response_json
                    FROM external_upload_idempotency
                    WHERE key_id = ? AND idempotency_key = ?
                    """,
                    (str(key_id), str(idempotency_key)),
                ).fetchone()
                if row is not None:
                    if str(row["request_hash"] or "") != str(request_hash):
                        return "conflict", ""
                    response_json = str(row["response_json"] or "")
                    return ("replay", response_json) if response_json else ("in_progress", "")
                raise

    def finish_external_upload(
        self,
        *,
        key_id: str,
        idempotency_key: str,
        response_json: str,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE external_upload_idempotency
                SET response_json = ?
                WHERE key_id = ? AND idempotency_key = ? AND response_json = ''
                """,
                (str(response_json), str(key_id), str(idempotency_key)),
            )

    def abort_external_upload(self, *, key_id: str, idempotency_key: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                DELETE FROM external_upload_idempotency
                WHERE key_id = ? AND idempotency_key = ? AND response_json = ''
                """,
                (str(key_id), str(idempotency_key)),
            )

    def create_external_source_scope(
        self,
        *,
        key_id: str,
        kb_ref: str,
        kb_id: int,
        department_id: int,
        sources: list[dict],
        ttl_seconds: int | None = None,
    ) -> ExternalSourceScope:
        ttl = max(
            60,
            int(
                ttl_seconds
                if ttl_seconds is not None
                else external_settings.HDB_EXTERNAL_SOURCE_SCOPE_TTL_SECONDS
            ),
        )
        now = datetime.now(timezone.utc)
        created_at = now.isoformat()
        expires_at = (now + timedelta(seconds=ttl)).isoformat()
        scope_ref = f"scoperef_{secrets.token_urlsafe(18)}"
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO external_source_scopes
                    (scope_ref, key_id, kb_ref, kb_id, department_id,
                     sources_json, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope_ref,
                    str(key_id),
                    str(kb_ref),
                    int(kb_id),
                    int(department_id),
                    json.dumps(sources, ensure_ascii=False),
                    created_at,
                    expires_at,
                ),
            )
        return ExternalSourceScope(
            scope_ref=scope_ref,
            key_id=str(key_id),
            kb_ref=str(kb_ref),
            kb_id=int(kb_id),
            department_id=int(department_id),
            sources=sources,
            created_at=created_at,
            expires_at=expires_at,
        )

    def get_external_source_scope(
        self, scope_ref: str, key_id: str
    ) -> ExternalSourceScope | None:
        if not scope_ref or len(str(scope_ref)) > 200:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT scope_ref, key_id, kb_ref, kb_id, department_id,
                       sources_json, created_at, expires_at
                FROM external_source_scopes
                WHERE scope_ref = ? AND key_id = ?
                """,
                (str(scope_ref), str(key_id)),
            ).fetchone()
        if row is None:
            return None
        try:
            if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now(timezone.utc):
                return None
        except ValueError:
            return None
        try:
            sources = json.loads(row["sources_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(sources, list) or not sources:
            return None
        return ExternalSourceScope(
            scope_ref=str(row["scope_ref"]),
            key_id=str(row["key_id"]),
            kb_ref=str(row["kb_ref"]),
            kb_id=int(row["kb_id"]),
            department_id=int(row["department_id"]),
            sources=sources,
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
        )

    def save_external_evidence(
        self,
        *,
        key_id: str,
        scope_ref: str,
        kb_ref: str,
        kb_id: int,
        department_id: int,
        record_id: int,
        file_ref: str,
        file_name: str,
        content: str,
        content_hash: str,
        source_content_hash: str,
        locator: dict,
        score: float,
        backend: str,
        ttl_seconds: int | None = None,
    ) -> str:
        ttl = max(
            60,
            int(
                ttl_seconds
                if ttl_seconds is not None
                else external_settings.HDB_EXTERNAL_EVIDENCE_TTL_SECONDS
            ),
        )
        now = datetime.now(timezone.utc)
        evidence_id = f"evidence_{secrets.token_urlsafe(18)}"
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO external_evidence
                    (evidence_id, key_id, scope_ref, kb_ref, kb_id, department_id,
                     record_id, file_ref, file_name, content, content_hash,
                     source_content_hash, locator_json, score, backend,
                     created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    str(key_id),
                    str(scope_ref),
                    str(kb_ref),
                    int(kb_id),
                    int(department_id),
                    int(record_id),
                    str(file_ref),
                    str(file_name),
                    str(content),
                    str(content_hash),
                    str(source_content_hash),
                    json.dumps(locator, ensure_ascii=False),
                    float(score),
                    str(backend),
                    now.isoformat(),
                    (now + timedelta(seconds=ttl)).isoformat(),
                ),
            )
        return evidence_id

    def get_external_evidence(
        self, evidence_id: str, key_id: str
    ) -> ExternalEvidenceRecord | None:
        if not evidence_id or len(str(evidence_id)) > 200:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT evidence_id, key_id, scope_ref, kb_ref, kb_id,
                       department_id, record_id, file_ref, file_name, content,
                       content_hash, source_content_hash, locator_json, score,
                       backend, created_at, expires_at
                FROM external_evidence
                WHERE evidence_id = ? AND key_id = ?
                """,
                (str(evidence_id), str(key_id)),
            ).fetchone()
        if row is None:
            return None
        try:
            if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now(timezone.utc):
                return None
        except ValueError:
            return None
        try:
            locator = json.loads(row["locator_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            locator = {}
        return ExternalEvidenceRecord(
            evidence_id=str(row["evidence_id"]),
            key_id=str(row["key_id"]),
            scope_ref=str(row["scope_ref"] or ""),
            kb_ref=str(row["kb_ref"]),
            kb_id=int(row["kb_id"]),
            department_id=int(row["department_id"]),
            record_id=int(row["record_id"]),
            file_ref=str(row["file_ref"]),
            file_name=str(row["file_name"]),
            content=str(row["content"]),
            content_hash=str(row["content_hash"]),
            source_content_hash=str(row["source_content_hash"]),
            locator=locator if isinstance(locator, dict) else {},
            score=float(row["score"] or 0.0),
            backend=str(row["backend"] or "ragflow"),
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
        )

    @staticmethod
    def _api_key_digest(raw_key: str) -> str:
        pepper = str(external_settings.HDB_API_KEY_PEPPER or "").strip()
        if pepper:
            return hmac.new(pepper.encode("utf-8"), raw_key.encode("utf-8"), hashlib.sha256).hexdigest()
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @staticmethod
    def _api_key_public_id(raw_key: str) -> str | None:
        parts = str(raw_key or "").split("_", 2)
        if len(parts) != 3 or parts[0] != "hdbk" or not parts[1] or not parts[2]:
            return None
        if len(parts[1]) != 24 or any(char not in "0123456789abcdef" for char in parts[1].lower()):
            return None
        return parts[1]

    @staticmethod
    def _normalize_api_key_scopes(scopes) -> frozenset[str]:
        if not isinstance(scopes, (list, tuple, set, frozenset)):
            raise ValueError("scopes 必须是非空数组")
        normalized = frozenset(str(scope).strip().lower() for scope in scopes if str(scope).strip())
        if not normalized or not normalized.issubset(API_KEY_SCOPES):
            raise ValueError("scopes 仅支持 read、write、invoke")
        return normalized

    def create_employee_api_key(
        self,
        actor: AuthUser,
        *,
        name: str,
        scopes,
        kb_access_mode: str,
        kb_refs: list[str] | None,
        expires_in_days: int | None = None,
        replacing_public_key_id: str | None = None,
    ) -> dict:
        if actor is None or actor.role != ROLE_EMPLOYEE or not actor.is_active:
            raise PermissionError("只有启用的员工可以创建 API Key")
        clean_name = str(name or "").strip()
        if not clean_name or len(clean_name) > 120:
            raise ValueError("Key 名称不能为空且不能超过 120 个字符")
        normalized_scopes = self._normalize_api_key_scopes(scopes)
        mode = str(kb_access_mode or "selected_kbs").strip()
        if mode not in API_KEY_ACCESS_MODES:
            raise ValueError("kb_access_mode 不受支持")
        if mode == "all_current_accessible_kbs":
            if kb_refs:
                raise ValueError("all_current_accessible_kbs 不能提交 kb_refs")
            if not bool(external_settings.HDB_EXTERNAL_ALL_KBS_MODE_ENABLED):
                raise ValueError("all_current_accessible_kbs 未启用")
            if str(getattr(src.settings, "OBS_ENVIRONMENT", "development")).strip().lower() in {
                "prod",
                "production",
            }:
                raise ValueError("生产环境不允许 all_current_accessible_kbs")
            selected_refs: list[str] = []
            selected_ids: list[int] = []
        else:
            selected_refs = list(dict.fromkeys(str(ref).strip() for ref in (kb_refs or []) if str(ref).strip()))
            if not selected_refs:
                raise ValueError("selected_kbs 必须至少选择一个 kb_ref")
            with closing(self._connect()) as conn:
                placeholders = ",".join("?" for _ in selected_refs)
                rows = conn.execute(
                    f"""
                    SELECT e.kb_ref, e.kb_id, kb.name, kb.department_id
                    FROM external_kb_refs e
                    JOIN knowledge_bases kb ON kb.id = e.kb_id
                    WHERE e.retired_at IS NULL AND e.kb_ref IN ({placeholders})
                    """,
                    selected_refs,
                ).fetchall()
            by_ref = {str(row["kb_ref"]): row for row in rows}
            if len(by_ref) != len(selected_refs):
                raise ValueError("存在未知或已停用的 kb_ref")
            permissions = self.get_kb_permissions_for_user(actor)
            selected_ids = []
            for ref in selected_refs:
                row = by_ref[ref]
                if row["department_id"] != actor.department_id:
                    raise PermissionError("Key 只能绑定员工当前部门的知识库")
                permission = permissions.get(kb_scope_key(row["name"], row["department_id"]))
                if permission not in {"read", "write", "admin"}:
                    raise PermissionError("员工当前无权访问所选知识库")
                selected_ids.append(int(row["kb_id"]))

        min_ttl = int(external_settings.HDB_API_KEY_MIN_TTL_DAYS or 1)
        max_ttl = min(int(external_settings.HDB_API_KEY_MAX_TTL_DAYS or 365), 365)
        ttl = int(expires_in_days if expires_in_days is not None else external_settings.HDB_API_KEY_DEFAULT_TTL_DAYS)
        if ttl < min_ttl or ttl > max_ttl:
            raise ValueError(f"expires_in_days 必须在 {min_ttl} 到 {max_ttl} 天之间")

        now = datetime.now(timezone.utc)
        created_at = now.isoformat()
        expires_at = (now + timedelta(days=ttl)).isoformat()
        public_id = secrets.token_hex(12)
        secret = secrets.token_urlsafe(32)
        raw_key = f"hdbk_{public_id}_{secret}"
        key_digest = self._api_key_digest(raw_key)
        max_active = int(external_settings.HDB_API_KEY_MAX_ACTIVE_PER_USER or 10)

        with closing(self._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                active_query = """
                    SELECT COUNT(*) AS count FROM employee_api_keys
                    WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?
                """
                active_params: list[object] = [actor.id, created_at]
                if replacing_public_key_id:
                    active_query += " AND public_key_id != ?"
                    active_params.append(str(replacing_public_key_id))
                active = conn.execute(active_query, active_params).fetchone()
                if int(active["count"] or 0) >= max_active:
                    raise ValueError("KEY_LIMIT_REACHED")
                inserted = conn.execute(
                    """
                    INSERT INTO employee_api_keys
                        (public_key_id, key_digest, user_id, name, scopes_json,
                         kb_access_mode, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    RETURNING id
                    """,
                    (
                        public_id,
                        key_digest,
                        actor.id,
                        clean_name,
                        json.dumps(sorted(normalized_scopes)),
                        mode,
                        created_at,
                        expires_at,
                    ),
                ).fetchone()
                if inserted is None:
                    raise RuntimeError("无法创建 API Key")
                key_row_id = int(inserted["id"])
                for kb_id in selected_ids:
                    conn.execute(
                        """
                        INSERT INTO employee_api_key_kbs (api_key_id, kb_id, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (key_row_id, kb_id, created_at),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        result = {
            "key_id": public_id,
            "name": clean_name,
            "scopes": sorted(normalized_scopes),
            "kb_access_mode": mode,
            "kb_refs": selected_refs,
            "created_at": created_at,
            "expires_at": expires_at,
            "status": "active",
            "masked_key": f"hdbk_{public_id}_••••",
            "api_key": raw_key,
        }
        self._audit(
            "create_employee_api_key",
            actor=actor,
            target_type="employee_api_key",
            target_id=public_id,
            metadata={
                "public_key_id": public_id,
                "scopes": sorted(normalized_scopes),
                "kb_access_mode": mode,
                "kb_refs": selected_refs,
            },
        )
        return result

    def _api_key_info(self, row, kb_refs: list[str]) -> dict:
        revoked_at = row["revoked_at"]
        expires_at = str(row["expires_at"] or "")
        expired = False
        try:
            expired = datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc)
        except ValueError:
            expired = True
        status = "revoked" if revoked_at else ("expired" if expired else "active")
        try:
            scopes = sorted(set(json.loads(row["scopes_json"] or "[]")))
        except (TypeError, json.JSONDecodeError):
            scopes = []
        return {
            "key_id": str(row["public_key_id"]),
            "name": str(row["name"]),
            "scopes": scopes,
            "kb_access_mode": str(row["kb_access_mode"]),
            "kb_refs": kb_refs,
            "created_at": str(row["created_at"]),
            "expires_at": expires_at,
            "revoked_at": revoked_at,
            "last_used_at": row["last_used_at"],
            "status": status,
            "masked_key": f"hdbk_{row['public_key_id']}_••••",
        }

    def _api_key_kb_refs(self, conn, key_id: int) -> list[str]:
        rows = conn.execute(
            """
            SELECT e.kb_ref
            FROM employee_api_key_kbs k
            JOIN external_kb_refs e ON e.kb_id = k.kb_id
            WHERE k.api_key_id = ? AND e.retired_at IS NULL
            ORDER BY e.kb_ref
            """,
            (key_id,),
        ).fetchall()
        return [str(row["kb_ref"]) for row in rows]

    def list_employee_api_keys(self, actor: AuthUser) -> list[dict]:
        if actor is None or actor.role != ROLE_EMPLOYEE or not actor.is_active:
            raise PermissionError("只有启用的员工可以管理 API Key")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM employee_api_keys WHERE user_id = ? ORDER BY id DESC",
                (actor.id,),
            ).fetchall()
            return [self._api_key_info(row, self._api_key_kb_refs(conn, int(row["id"]))) for row in rows]

    def get_employee_api_key(self, actor: AuthUser, public_key_id: str) -> dict | None:
        if actor is None or actor.role != ROLE_EMPLOYEE or not actor.is_active:
            raise PermissionError("只有启用的员工可以管理 API Key")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM employee_api_keys WHERE user_id = ? AND public_key_id = ?",
                (actor.id, str(public_key_id)),
            ).fetchone()
            if row is None:
                return None
            return self._api_key_info(row, self._api_key_kb_refs(conn, int(row["id"])))

    def revoke_employee_api_key(self, actor: AuthUser, public_key_id: str) -> bool:
        if actor is None or actor.role != ROLE_EMPLOYEE or not actor.is_active:
            raise PermissionError("只有启用的员工可以管理 API Key")
        now = utc_now()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id FROM employee_api_keys WHERE user_id = ? AND public_key_id = ?",
                (actor.id, str(public_key_id)),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE employee_api_keys SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
                (now, int(row["id"])),
            )
        self._audit(
            "revoke_employee_api_key",
            actor=actor,
            target_type="employee_api_key",
            target_id=str(public_key_id),
            metadata={"public_key_id": str(public_key_id)},
        )
        return True

    def rotate_employee_api_key(
        self,
        actor: AuthUser,
        public_key_id: str,
        *,
        expires_in_days: int | None = None,
    ) -> dict | None:
        existing = self.get_employee_api_key(actor, public_key_id)
        if existing is None:
            return None
        if existing["status"] == "revoked":
            raise ValueError("API Key 已撤销，不能轮换")
        created = self.create_employee_api_key(
            actor,
            name=f"{existing['name']} (rotated)",
            scopes=existing["scopes"],
            kb_access_mode=existing["kb_access_mode"],
            kb_refs=existing["kb_refs"],
            expires_in_days=expires_in_days,
            replacing_public_key_id=public_key_id,
        )
        if existing["status"] == "active":
            self.revoke_employee_api_key(actor, public_key_id)
        created["rotated_from"] = public_key_id
        self._audit(
            "rotate_employee_api_key",
            actor=actor,
            target_type="employee_api_key",
            target_id=public_key_id,
            metadata={
                "rotated_from": public_key_id,
                "new_key_id": created["key_id"],
            },
        )
        return created

    def narrow_employee_api_key_kbs(
        self,
        actor: AuthUser,
        public_key_id: str,
        kb_refs: list[str],
    ) -> dict | None:
        if actor is None or actor.role != ROLE_EMPLOYEE or not actor.is_active:
            raise PermissionError("只有启用的员工可以管理 API Key")
        existing = self.get_employee_api_key(actor, public_key_id)
        if existing is None:
            return None
        if existing["status"] == "revoked":
            raise ValueError("API Key 已撤销")
        if existing["kb_access_mode"] != "selected_kbs":
            raise ValueError("只有 selected_kbs 模式可以修改 allowlist")
        selected_refs = list(
            dict.fromkeys(str(ref).strip() for ref in kb_refs if str(ref).strip())
        )
        if not selected_refs:
            raise ValueError("allowlist 不能为空")
        current_refs = set(existing["kb_refs"])
        if not set(selected_refs).issubset(current_refs):
            raise ValueError("只能收窄 allowlist，不能扩大")
        with closing(self._connect()) as conn:
            placeholders = ",".join("?" for _ in selected_refs)
            rows = conn.execute(
                f"""
                SELECT e.kb_ref, e.kb_id, kb.name, kb.department_id
                FROM external_kb_refs e
                JOIN knowledge_bases kb ON kb.id = e.kb_id
                WHERE e.retired_at IS NULL AND e.kb_ref IN ({placeholders})
                """,
                selected_refs,
            ).fetchall()
            by_ref = {str(row["kb_ref"]): row for row in rows}
            if len(by_ref) != len(selected_refs):
                raise ValueError("存在未知或已停用的 kb_ref")
            permissions = self.get_kb_permissions_for_user(actor)
            selected_ids: list[int] = []
            for ref in selected_refs:
                row = by_ref[ref]
                if row["department_id"] != actor.department_id:
                    raise PermissionError("Key 只能绑定员工当前部门的知识库")
                permission = permissions.get(kb_scope_key(row["name"], row["department_id"]))
                if permission not in {"read", "write", "admin"}:
                    raise PermissionError("员工当前无权访问所选知识库")
                selected_ids.append(int(row["kb_id"]))
            key_row = conn.execute(
                "SELECT id FROM employee_api_keys WHERE user_id = ? AND public_key_id = ?",
                (actor.id, str(public_key_id)),
            ).fetchone()
            if key_row is None:
                return None
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "DELETE FROM employee_api_key_kbs WHERE api_key_id = ?",
                    (int(key_row["id"]),),
                )
                for kb_id in selected_ids:
                    conn.execute(
                        """
                        INSERT INTO employee_api_key_kbs (api_key_id, kb_id, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (int(key_row["id"]), kb_id, utc_now()),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self._audit(
            "narrow_employee_api_key_kbs",
            actor=actor,
            target_type="employee_api_key",
            target_id=str(public_key_id),
            metadata={"public_key_id": str(public_key_id), "kb_refs": selected_refs},
        )
        return self.get_employee_api_key(actor, public_key_id)

    def resolve_employee_api_key(
        self, raw_key: str | None
    ) -> tuple[EmployeeApiKeyPrincipal | None, str]:
        """Authenticate a key and return a stable failure code.

        The caller decides how much of ``KEY_EXPIRED``/``KEY_REVOKED`` to
        expose; ``authenticate_employee_api_key`` remains the boolean-ish
        convenience wrapper used by older callers.
        """
        public_id = self._api_key_public_id(raw_key or "")
        if public_id is None:
            return None, "AUTH_INVALID"
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT k.*, u.role AS user_role, u.is_active AS user_is_active
                FROM employee_api_keys k
                JOIN users u ON u.id = k.user_id
                WHERE k.public_key_id = ?
                """,
                (public_id,),
            ).fetchone()
            if row is None:
                return None, "AUTH_INVALID"
            expected = str(row["key_digest"] or "")
            actual = self._api_key_digest(str(raw_key))
            if not hmac.compare_digest(expected, actual):
                return None, "AUTH_INVALID"
            if row["user_role"] != ROLE_EMPLOYEE or not bool(row["user_is_active"]):
                return None, "AUTH_INVALID"
            if row["revoked_at"]:
                return None, "KEY_REVOKED"
            try:
                if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now(timezone.utc):
                    return None, "KEY_EXPIRED"
            except ValueError:
                return None, "AUTH_INVALID"
            mode = str(row["kb_access_mode"] or "")
            if mode not in API_KEY_ACCESS_MODES:
                return None, "AUTH_INVALID"
            if mode == "all_current_accessible_kbs" and (
                not bool(external_settings.HDB_EXTERNAL_ALL_KBS_MODE_ENABLED)
                or str(getattr(src.settings, "OBS_ENVIRONMENT", "development"))
                .strip()
                .lower()
                in {"prod", "production"}
            ):
                return None, "KEY_SCOPE_DENIED"
            try:
                scopes = frozenset(json.loads(row["scopes_json"] or "[]"))
            except (TypeError, json.JSONDecodeError):
                return None, "AUTH_INVALID"
            if not scopes or not scopes.issubset(API_KEY_SCOPES):
                return None, "AUTH_INVALID"
            kb_rows = conn.execute(
                """
                SELECT k.kb_id, e.kb_ref
                FROM employee_api_key_kbs k
                JOIN external_kb_refs e ON e.kb_id = k.kb_id
                WHERE k.api_key_id = ? AND e.retired_at IS NULL
                """,
                (int(row["id"]),),
            ).fetchall()
            last_used = row["last_used_at"]
            should_update = True
            if last_used:
                try:
                    should_update = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(str(last_used))
                    ).total_seconds() >= 60
                except ValueError:
                    should_update = True
            if should_update:
                conn.execute(
                    "UPDATE employee_api_keys SET last_used_at = ? WHERE id = ?",
                    (utc_now(), int(row["id"])),
                )
            return (
                EmployeeApiKeyPrincipal(
                    key_id=public_id,
                    user_id=int(row["user_id"]),
                    scopes=frozenset(str(scope) for scope in scopes),
                    kb_access_mode=mode,
                    kb_ids=frozenset(int(item["kb_id"]) for item in kb_rows),
                    kb_refs=frozenset(str(item["kb_ref"]) for item in kb_rows),
                    expires_at=str(row["expires_at"]),
                ),
                "",
            )

    def authenticate_employee_api_key(self, raw_key: str | None) -> EmployeeApiKeyPrincipal | None:
        principal, _failure_code = self.resolve_employee_api_key(raw_key)
        return principal

    def get_user_by_username(self, username: str | None) -> AuthUser | None:
        if not username:
            return None
        with closing(self._connect()) as conn:
            row = self._get_user_row(conn, username)
        return row_to_user(row) if row else None

    def get_user_by_id(self, user_id: int | None) -> AuthUser | None:
        if user_id is None:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT u.*, d.name AS department_name
                FROM users u
                LEFT JOIN departments d ON d.id = u.department_id
                WHERE u.id = ?
                """,
                (int(user_id),),
            ).fetchone()
        return row_to_user(row) if row else None

    def _get_user_row(self, conn, username: str):
        return conn.execute(
            """
            SELECT u.*, d.name AS department_name
            FROM users u
            LEFT JOIN departments d ON d.id = u.department_id
            WHERE u.username = ?
            """,
            (username,),
        ).fetchone()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def row_to_user(row) -> AuthUser:
    return AuthUser(
        id=int(row["id"]),
        username=row["username"],
        role=row["role"],
        is_active=bool(row["is_active"]),
        department_id=row["department_id"] if "department_id" in row.keys() else None,
        department_name=row["department_name"] if "department_name" in row.keys() else None,
    )


def ensure_session_id(session_state) -> str:
    if "session_id" not in session_state:
        session_state.session_id = uuid.uuid4().hex
    return session_state.session_id


def anonymous_user_id(session_id: str) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return f"anonymous-{digest}"


def build_request_context(session_state, auth_service: AuthService | None = None) -> RequestContext:
    session_id = ensure_session_id(session_state)
    username = session_state.get("username")
    department_id = session_state.get("department_id")
    kb_id = session_state.get("current_kb_id")
    resource_department_id = session_state.get("current_kb_department_id")
    if resource_department_id in (None, ""):
        resource_department_id = department_id
    if username:
        auth_service = auth_service or AuthService()
        user = auth_service.get_user_by_username(username)
        # 用 DB 里的实时角色/状态,而非 session_state 快照:降级或停用的账号
        # 不能凭旧会话继续以原角色操作。
        if user is None or not user.is_active:
            return RequestContext(
                user_id=anonymous_user_id(session_id),
                session_id=session_id,
                roles=["anonymous"],
            )
        kb_permissions = auth_service.get_kb_permissions_for_user(user)
        return RequestContext(
            user_id=username,
            session_id=session_id,
            roles=[user.role],
            allowed_kbs=sorted(key for key in kb_permissions if ":" not in key),
            kb_permissions=kb_permissions,
            metadata={
                "actor_department_id": department_id,
                "resource_department_id": resource_department_id,
                "department_id": resource_department_id,
                "kb_id": kb_id,
            },
        )
    return RequestContext(
        user_id=anonymous_user_id(session_id),
        session_id=session_id,
        roles=["anonymous"],
    )
