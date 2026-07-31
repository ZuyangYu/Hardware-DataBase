import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config.settings
from src.pipelines.document_rag.schemas import RequestContext, kb_scope_key


PBKDF2_ITERATIONS = 260_000
ROLE_SYSTEM_ADMIN = "system_admin"
ROLE_DEPT_ADMIN = "dept_admin"
ROLE_USER = "user"
VALID_ROLES = {ROLE_SYSTEM_ADMIN, ROLE_DEPT_ADMIN, ROLE_USER}


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
class KnowledgeBaseAccess:
    kb_name: str
    permission: str
    department_id: int | None = None


@dataclass
class KnowledgeBaseSummary:
    name: str
    kb_id: int | None = None
    department_id: int | None = None
    department_name: str | None = None
    owner_user_id: int | None = None
    owner_username: str | None = None
    permission_count: int = 0
    dept_admin_count: int = 0
    registered: bool = False
    physical_exists: bool = False
    created_at: str = ""


@dataclass
class KnowledgeBasePermission:
    username: str
    role: str
    permission: str
    department_name: str | None = None


@dataclass
class AuthSession:
    token: str
    user: AuthUser
    expires_at: str


class AuthService:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.settings.AUTH_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()
        self._ensure_default_admin()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

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
                    role TEXT NOT NULL DEFAULT 'user',
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_revoked "
                "ON auth_sessions(user_id, revoked_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_department ON users(department_id)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    department_id INTEGER,
                    owner_user_id INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(department_id, name),
                    FOREIGN KEY(department_id) REFERENCES departments(id),
                    FOREIGN KEY(owner_user_id) REFERENCES users(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kb_permissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kb_id INTEGER NOT NULL,
                    kb_name TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    permission TEXT NOT NULL DEFAULT 'read',
                    created_at TEXT NOT NULL,
                    UNIQUE(kb_id, user_id),
                    FOREIGN KEY(kb_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            """)
            self._migrate_kb_scope_schema(conn)
            # One-shot migration: remove the synthetic "system" department from
            # older installations (schema version < 2).  Once the flag is set the
            # block is skipped on every subsequent start.
            if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
                self._remove_legacy_system_department(conn)
                conn.execute("PRAGMA user_version = 2")

    def _ensure_column(self, conn, table: str, column: str, ddl: str):
        columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _remove_legacy_system_department(self, conn):
        """Remove the synthetic department used by older installations."""
        conn.execute(
            "UPDATE users SET department_id = NULL, updated_at = ? WHERE role = ? AND department_id IS NOT NULL",
            (utc_now(), ROLE_SYSTEM_ADMIN),
        )
        rows = conn.execute("SELECT id FROM departments WHERE name = 'system'").fetchall()
        for row in rows:
            department_id = int(row["id"])
            conn.execute(
                "UPDATE users SET department_id = NULL, updated_at = ? WHERE department_id = ?",
                (utc_now(), department_id),
            )
            conn.execute(
                "UPDATE knowledge_bases SET department_id = NULL WHERE department_id = ?",
                (department_id,),
            )
            conn.execute("DELETE FROM departments WHERE id = ?", (department_id,))

    def _migrate_kb_scope_schema(self, conn):
        kb_columns = {row["name"] for row in conn.execute("PRAGMA table_info(knowledge_bases)").fetchall()}
        kb_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_bases'"
        ).fetchone()
        kb_sql = kb_sql_row["sql"] if kb_sql_row else ""
        needs_kb_rebuild = "UNIQUE(department_id, name)" not in kb_sql

        permission_columns = {row["name"] for row in conn.execute("PRAGMA table_info(kb_permissions)").fetchall()}
        permission_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'kb_permissions'"
        ).fetchone()
        permission_sql = permission_sql_row["sql"] if permission_sql_row else ""
        needs_permission_rebuild = (
            "kb_id" not in permission_columns
            or "UNIQUE(kb_id, user_id)" not in permission_sql
            or "kb_id INTEGER NOT NULL" not in permission_sql
        )

        if not needs_kb_rebuild and not needs_permission_rebuild:
            return

        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            if needs_kb_rebuild:
                conn.execute("ALTER TABLE knowledge_bases RENAME TO knowledge_bases_old")
                conn.execute("""
                    CREATE TABLE knowledge_bases (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        department_id INTEGER,
                        owner_user_id INTEGER,
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
                    "created_at" if "created_at" in kb_columns else "'' AS created_at",
                ]
                conn.execute(f"""
                    INSERT OR IGNORE INTO knowledge_bases (id, name, department_id, owner_user_id, created_at)
                    SELECT {', '.join(select_columns)}
                    FROM knowledge_bases_old
                """)
                conn.execute("DROP TABLE knowledge_bases_old")

            if needs_permission_rebuild:
                conn.execute("ALTER TABLE kb_permissions RENAME TO kb_permissions_old")
                conn.execute("""
                    CREATE TABLE kb_permissions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kb_id INTEGER NOT NULL,
                        kb_name TEXT NOT NULL,
                        user_id INTEGER NOT NULL,
                        permission TEXT NOT NULL DEFAULT 'read',
                        created_at TEXT NOT NULL,
                        UNIQUE(kb_id, user_id),
                        FOREIGN KEY(kb_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                        FOREIGN KEY(user_id) REFERENCES users(id)
                    )
                """)
                created_expr = "p.created_at" if "created_at" in permission_columns else "''"
                permission_expr = "p.permission" if "permission" in permission_columns else "'read'"
                kb_id_expr = "p.kb_id" if "kb_id" in permission_columns else "NULL"
                conn.execute(f"""
                    INSERT OR IGNORE INTO kb_permissions (kb_id, kb_name, user_id, permission, created_at)
                    SELECT kb.id, kb.name, p.user_id, {permission_expr}, {created_expr}
                    FROM kb_permissions_old p
                    JOIN users u ON u.id = p.user_id
                    JOIN knowledge_bases kb
                      ON kb.name = p.kb_name
                     AND (
                         ({kb_id_expr} IS NOT NULL AND kb.id = {kb_id_expr})
                         OR ({kb_id_expr} IS NULL AND kb.department_id = u.department_id)
                     )
                """)
                conn.execute("DROP TABLE kb_permissions_old")
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    def _ensure_default_admin(self):
        username = config.settings.AUTH_DEFAULT_ADMIN_USERNAME
        password = config.settings.AUTH_DEFAULT_ADMIN_PASSWORD
        if not password or password == "admin123":
            raise RuntimeError("AUTH_DEFAULT_ADMIN_PASSWORD must be set to a non-default strong password.")
        with closing(self._connect()) as conn:
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
                return
            now = utc_now()
            conn.execute(
                """
                INSERT INTO users (username, password_hash, role, department_id, is_active, managed_by_env, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, 1, ?, ?)
                """,
                (username, hash_password(password), ROLE_SYSTEM_ADMIN, None, now, now),
            )

    def create_user(self, username: str, password: str, role: str = "user", department_id: int | None = None) -> AuthUser:
        raise PermissionError("Use create_user_as(actor, ...) so user creation is checked against the actor role.")

    def create_user_as(
        self,
        actor: AuthUser,
        username: str,
        password: str,
        role: str = ROLE_USER,
        department_id: int | None = None,
    ) -> AuthUser:
        scope = "system" if actor.role == ROLE_SYSTEM_ADMIN else "department"
        try:
            if actor.role == ROLE_SYSTEM_ADMIN:
                if role == ROLE_USER:
                    raise PermissionError("系统管理员不能创建普通用户，请由部门管理员创建。")
                if role == ROLE_SYSTEM_ADMIN:
                    department_id = None
                elif role == ROLE_DEPT_ADMIN and department_id is None:
                    raise ValueError("部门管理员必须归属到业务部门")
            elif actor.role == ROLE_DEPT_ADMIN:
                if role != ROLE_USER:
                    raise PermissionError("部门管理员只能创建普通用户。")
                if actor.department_id is None:
                    raise PermissionError("部门管理员必须归属到部门后才能创建用户。")
                if department_id is not None and department_id != actor.department_id:
                    raise PermissionError("部门管理员只能创建本部门用户。")
                department_id = actor.department_id
            else:
                raise PermissionError("无权创建用户。")

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

    def _create_user_record(self, username: str, password: str, role: str = "user", department_id: int | None = None) -> AuthUser:
        username = username.strip()
        if not username:
            raise ValueError("用户名不能为空")
        if not password:
            raise ValueError("密码不能为空")
        if role not in VALID_ROLES:
            raise ValueError("角色必须为 system_admin、dept_admin 或 user")

        now = utc_now()
        with closing(self._connect()) as conn:
            if role == ROLE_SYSTEM_ADMIN:
                department_id = None
            elif department_id is None:
                raise ValueError("普通用户和部门管理员必须归属到部门")
            elif conn.execute("SELECT 1 FROM departments WHERE id = ?", (department_id,)).fetchone() is None:
                raise ValueError("部门不存在")
            conn.execute(
                """
                INSERT INTO users (username, password_hash, role, department_id, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (username, hash_password(password), role, department_id, now, now),
            )
            row = self._get_user_row(conn, username)
        return row_to_user(row)

    def authenticate(self, username: str, password: str) -> AuthSession | None:
        clean_username = username.strip()
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT u.*, d.name AS department_name
                FROM users u
                LEFT JOIN departments d ON d.id = u.department_id
                WHERE u.username = ? AND u.is_active = 1
                """,
                (clean_username,),
            ).fetchone()
            if row is None or not verify_password(password, row["password_hash"]):
                self._audit(
                    "login_failed",
                    target_type="user",
                    target_id=clean_username,
                    success=False,
                    error_message="用户名或密码错误",
                )
                return None

            token = secrets.token_urlsafe(32)
            now_dt = datetime.now(timezone.utc)
            expires_dt = now_dt + timedelta(hours=config.settings.AUTH_SESSION_TTL_HOURS)
            conn.execute(
                """
                INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (hash_token(token), row["id"], now_dt.isoformat(), expires_dt.isoformat()),
            )
        user = row_to_user(row)
        self._audit(
            "login_success",
            actor=user,
            target_type="user",
            target_id=user.username,
            success=True,
        )
        return AuthSession(token=token, user=user, expires_at=expires_dt.isoformat())

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

    def list_users_for_manager(self, manager: AuthUser) -> list[AuthUser]:
        if manager.role == ROLE_SYSTEM_ADMIN:
            return [user for user in self.list_users() if user.role != ROLE_USER]
        if manager.role == ROLE_DEPT_ADMIN:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    """
                    SELECT u.*, d.name AS department_name
                    FROM users u
                    LEFT JOIN departments d ON d.id = u.department_id
                    WHERE u.department_id = ?
                    ORDER BY u.id
                    """,
                    (manager.department_id,),
                ).fetchall()
            return [row_to_user(row) for row in rows]
        return []

    def list_users_as(self, actor: AuthUser) -> list[AuthUser]:
        if actor.role == ROLE_SYSTEM_ADMIN:
            return self.list_users_for_manager(actor)
        if actor.role == ROLE_DEPT_ADMIN:
            return self.list_users_for_manager(actor)
        raise PermissionError("User listing requires an administrator role.")

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

    def set_user_active_as(self, actor: AuthUser, user_id: int, is_active: bool):
        scope = "system" if actor.role == ROLE_SYSTEM_ADMIN else "department"
        try:
            with closing(self._connect()) as conn:
                target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                if target is None:
                    raise ValueError("User does not exist")
                if actor.id == user_id:
                    raise PermissionError("Cannot change the active state of the current account here.")
                if actor.role == ROLE_SYSTEM_ADMIN:
                    if target["role"] == ROLE_USER:
                        raise PermissionError("系统管理员不能管理普通用户，请由所属部门管理员操作。")
                elif actor.role == ROLE_DEPT_ADMIN:
                    if target["role"] != ROLE_USER or target["department_id"] != actor.department_id:
                        raise PermissionError("Department administrators can only manage users in their department.")
                else:
                    raise PermissionError("User activation requires an administrator role.")
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

            with closing(self._connect()) as conn:
                target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                if target is None:
                    raise ValueError("目标用户不存在")
                if actor.id == user_id:
                    raise PermissionError("不能在这里重置当前登录账号密码")

                if actor.role == ROLE_SYSTEM_ADMIN:
                    if target["role"] == ROLE_USER:
                        raise PermissionError("系统管理员不能重置普通用户密码，请由所属部门管理员操作。")
                elif actor.role == ROLE_DEPT_ADMIN:
                    if target["role"] != ROLE_USER or target["department_id"] != actor.department_id:
                        raise PermissionError("部门管理员只能重置本部门普通用户密码")
                else:
                    raise PermissionError("无权重置用户密码")

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

    def create_department(self, name: str) -> Department:
        name = name.strip()
        if not name:
            raise ValueError("部门名称不能为空")
        if name.casefold() == "system":
            raise ValueError("system 是保留名称，请使用业务部门名称")
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO departments (name, created_at) VALUES (?, ?)",
                (name, utc_now()),
            )
            row = conn.execute("SELECT * FROM departments WHERE name = ?", (name,)).fetchone()
        return Department(id=int(row["id"]), name=row["name"])

    def create_department_as(self, actor: AuthUser, name: str) -> Department:
        try:
            if actor.role != ROLE_SYSTEM_ADMIN:
                raise PermissionError("Department creation requires a system administrator role.")
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

    def delete_department_as(self, actor: AuthUser, department_id: int):
        try:
            if actor.role != ROLE_SYSTEM_ADMIN:
                raise PermissionError("Department deletion requires a system administrator role.")
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

    def register_knowledge_base(self, kb_name: str, owner: AuthUser | None = None):
        kb_name = kb_name.strip()
        department_id = owner.department_id if owner else None
        owner_id = owner.id if owner else None
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO knowledge_bases (name, department_id, owner_user_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (kb_name, department_id, owner_id, utc_now()),
            )
            kb = self._get_kb_row(conn, kb_name, department_id)
            if owner_id:
                self.grant_kb_permission(kb_name, owner_id, "admin", conn=conn, department_id=department_id, kb_id=kb["id"])

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
            conn.execute("DELETE FROM knowledge_bases WHERE id = ?", (kb["id"],))

    def list_registered_knowledge_bases(self) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT name FROM knowledge_bases ORDER BY name").fetchall()
        return [row["name"] for row in rows]

    def list_knowledge_base_summaries(self, existing_kbs: list[str]) -> list[KnowledgeBaseSummary]:
        existing = set(existing_kbs)
        summaries: dict[str, KnowledgeBaseSummary] = {}
        with closing(self._connect()) as conn:
            admin_counts = {
                row["department_id"]: int(row["count"])
                for row in conn.execute(
                    """
                    SELECT department_id, COUNT(*) AS count
                    FROM users
                    WHERE role = ? AND is_active = 1 AND department_id IS NOT NULL
                    GROUP BY department_id
                    """,
                    (ROLE_DEPT_ADMIN,),
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
                    kb.created_at,
                    COUNT(DISTINCT p.user_id) AS permission_count
                FROM knowledge_bases kb
                LEFT JOIN departments d ON d.id = kb.department_id
                LEFT JOIN users owner ON owner.id = kb.owner_user_id
                LEFT JOIN kb_permissions p ON p.kb_id = kb.id
                GROUP BY
                    kb.id, kb.name, kb.department_id, d.name,
                    kb.owner_user_id, owner.username, kb.created_at
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
                permission_count=int(row["permission_count"] or 0),
                dept_admin_count=admin_counts.get(department_id, 0),
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

    def list_knowledge_base_permissions(
        self,
        kb_name: str,
        department_id: int | str | None = None,
        kb_id: int | None = None,
    ) -> list[KnowledgeBasePermission]:
        with closing(self._connect()) as conn:
            kb = self._get_kb_row(conn, kb_name, department_id, kb_id)
            if kb is None:
                return []
            rows = conn.execute(
                """
                SELECT u.username, u.role, d.name AS department_name, p.permission
                FROM kb_permissions p
                JOIN users u ON u.id = p.user_id
                LEFT JOIN departments d ON d.id = u.department_id
                WHERE p.kb_id = ?
                ORDER BY d.name, u.username
                """,
                (kb["id"],),
            ).fetchall()
        return [
            KnowledgeBasePermission(
                username=row["username"],
                role=row["role"],
                department_name=row["department_name"],
                permission=row["permission"],
            )
            for row in rows
        ]

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
            with closing(self._connect()) as conn:
                department = conn.execute("SELECT id, name FROM departments WHERE id = ?", (department_id,)).fetchone()
                if department is None:
                    raise ValueError("部门不存在")

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
                    if owner["role"] != ROLE_DEPT_ADMIN:
                        raise ValueError("知识库负责人必须是部门管理员")
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
                    kb = self._get_kb_row(conn, kb_name, department_id)
                else:
                    conn.execute(
                        """
                        UPDATE knowledge_bases
                        SET department_id = ?, owner_user_id = ?
                        WHERE id = ?
                        """,
                        (department_id, owner_user_id, source_kb["id"]),
                    )
                    kb = self._get_kb_row(conn, kb_name, kb_id=source_kb["id"])
                # Re-assigning to a new department strips cross-department grants;
                # flag it in audit metadata so the side effect is traceable.
                conn.execute(
                    """
                    DELETE FROM kb_permissions
                    WHERE kb_id = ?
                      AND user_id IN (
                          SELECT id
                          FROM users
                          WHERE department_id IS NULL OR department_id != ?
                      )
                    """,
                    (kb["id"], department_id),
                )
                if owner_user_id is not None:
                    self.grant_kb_permission(
                        kb_name,
                        owner_user_id,
                        "admin",
                        conn=conn,
                        department_id=department_id,
                        kb_id=kb["id"],
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
                "removed_cross_dept_perms": True,
            },
        )

    def grant_kb_permission(
        self,
        kb_name: str,
        user_id: int,
        permission: str = "read",
        conn=None,
        department_id: int | str | None = None,
        kb_id: int | None = None,
    ):
        if permission not in {"read", "write", "admin"}:
            raise ValueError("权限必须为 read、write 或 admin")
        target_conn = conn or self._connect()
        should_close = conn is None
        try:
            kb = self._get_kb_row(target_conn, kb_name, department_id, kb_id)
            if kb is None:
                raise ValueError("Knowledge base does not exist")
            target_conn.execute(
                """
                INSERT INTO kb_permissions (kb_id, kb_name, user_id, permission, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(kb_id, user_id) DO UPDATE SET
                    kb_name = excluded.kb_name,
                    permission = excluded.permission
                """,
                (kb["id"], kb["name"], user_id, permission, utc_now()),
            )
        finally:
            if should_close:
                target_conn.close()

    def grant_kb_permission_as(
        self,
        actor: AuthUser,
        kb_name: str,
        user_id: int,
        permission: str = "read",
        department_id: int | str | None = None,
        kb_id: int | None = None,
    ):
        if permission not in {"read", "write", "admin"}:
            raise ValueError("权限必须为 read、write 或 admin")
        try:
            with closing(self._connect()) as conn:
                target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                if target is None:
                    raise ValueError("目标用户不存在")
                if target["role"] == ROLE_SYSTEM_ADMIN:
                    raise ValueError("不能授予系统管理员知识库内容权限")

                if actor.role == ROLE_DEPT_ADMIN and department_id in (None, "") and kb_id is None:
                    department_id = actor.department_id
                kb = self._get_kb_row(conn, kb_name, department_id, kb_id)
                if kb is None:
                    raise ValueError("知识库不存在")

                if actor.role == ROLE_SYSTEM_ADMIN:
                    raise PermissionError("System administrators cannot grant knowledge-base content permissions.")
                elif actor.role == ROLE_DEPT_ADMIN:
                    if kb["department_id"] != actor.department_id:
                        raise PermissionError("只能授权本部门知识库")
                    if target["department_id"] != actor.department_id or target["role"] != ROLE_USER:
                        raise PermissionError("部门管理员只能授权本部门普通用户")
                else:
                    raise PermissionError("无权授权知识库")

                self.grant_kb_permission(
                    kb_name,
                    user_id,
                    permission,
                    conn=conn,
                    department_id=kb["department_id"],
                    kb_id=kb["id"],
                )
        except Exception as exc:
            self._audit(
                "grant_kb_permission",
                actor=actor,
                target_type="kb_permission",
                target_id=str(user_id),
                kb_name=kb_name,
                success=False,
                error_message=str(exc),
                metadata={"permission": permission, "target_user_id": user_id},
            )
            raise
        self._audit(
            "grant_kb_permission",
            actor=actor,
            target_type="kb_permission",
            target_id=target["username"],
            kb_name=kb_name,
            metadata={"permission": permission, "target_user_id": user_id},
        )

    def delete_kb_permission(
        self,
        kb_name: str,
        user_id: int,
        conn=None,
        department_id: int | str | None = None,
        kb_id: int | None = None,
    ):
        """Remove a user's permission row on a KB. Internal helper; does
        not validate the caller's role."""
        target_conn = conn or self._connect()
        should_close = conn is None
        try:
            kb = self._get_kb_row(target_conn, kb_name, department_id, kb_id)
            if kb is None:
                raise ValueError("知识库不存在")
            target_conn.execute(
                "DELETE FROM kb_permissions WHERE kb_id = ? AND user_id = ?",
                (kb["id"], user_id),
            )
        finally:
            if should_close:
                target_conn.close()

    def revoke_kb_permission_as(
        self,
        actor: AuthUser,
        kb_name: str,
        user_id: int,
        department_id: int | str | None = None,
        kb_id: int | None = None,
    ):
        try:
            with closing(self._connect()) as conn:
                target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                if target is None:
                    raise ValueError("目标用户不存在")
                if target["role"] == ROLE_SYSTEM_ADMIN:
                    raise ValueError("系统管理员不拥有知识库内容权限，无需撤销")

                if actor.role == ROLE_DEPT_ADMIN and department_id in (None, "") and kb_id is None:
                    department_id = actor.department_id
                kb = self._get_kb_row(conn, kb_name, department_id, kb_id)
                if kb is None:
                    raise ValueError("知识库不存在")

                if actor.role == ROLE_SYSTEM_ADMIN:
                    raise PermissionError("系统管理员不能管理知识库内容权限")
                elif actor.role == ROLE_DEPT_ADMIN:
                    if kb["department_id"] != actor.department_id:
                        raise PermissionError("只能撤销本部门知识库的权限")
                    if target["department_id"] != actor.department_id or target["role"] != ROLE_USER:
                        raise PermissionError("部门管理员只能撤销本部门普通用户的权限")
                else:
                    raise PermissionError("无权撤销知识库权限")

                self.delete_kb_permission(
                    kb_name,
                    user_id,
                    conn=conn,
                    department_id=kb["department_id"],
                    kb_id=kb["id"],
                )
        except Exception as exc:
            self._audit(
                "revoke_kb_permission",
                actor=actor,
                target_type="kb_permission",
                target_id=str(user_id),
                kb_name=kb_name,
                success=False,
                error_message=str(exc),
                metadata={"target_user_id": user_id},
            )
            raise
        self._audit(
            "revoke_kb_permission",
            actor=actor,
            target_type="kb_permission",
            target_id=target["username"],
            kb_name=kb_name,
            metadata={"target_user_id": user_id},
        )

    def list_accessible_kbs(self, user: AuthUser, existing_kbs: list[str]) -> list[str]:
        if user.role == ROLE_SYSTEM_ADMIN:
            return []

        with closing(self._connect()) as conn:
            if user.role == ROLE_DEPT_ADMIN:
                rows = conn.execute(
                    """
                    SELECT kb.name
                    FROM knowledge_bases kb
                    WHERE kb.department_id = ?
                    UNION
                    SELECT kb.name
                    FROM kb_permissions p
                    JOIN knowledge_bases kb ON kb.id = p.kb_id
                    WHERE p.user_id = ? AND kb.department_id = ?
                    """,
                    (user.department_id, user.id, user.department_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT kb.name
                    FROM kb_permissions p
                    JOIN knowledge_bases kb ON kb.id = p.kb_id
                    WHERE p.user_id = ? AND kb.department_id = ?
                    """,
                    (user.id, user.department_id),
                ).fetchall()
        allowed = {row[0] for row in rows}
        return [kb for kb in existing_kbs if kb in allowed]

    def get_kb_permissions_for_user(self, user: AuthUser, existing_kbs: list[str] | None = None) -> dict[str, str]:
        if existing_kbs is None:
            existing_filter = None
        else:
            existing_filter = set(existing_kbs)

        if user.role == ROLE_SYSTEM_ADMIN:
            return {}

        permissions: dict[str, str] = {}
        levels = {"read": 1, "write": 2, "admin": 3}

        def add_permission(kb_name: str, permission: str, department_id: int | str | None = None):
            if existing_filter is not None and kb_name not in existing_filter:
                return
            effective_department_id = department_id if department_id not in (None, "") else user.department_id
            key = kb_scope_key(kb_name, effective_department_id)
            current = permissions.get(key)
            if current is None or levels.get(permission, 0) > levels.get(current, 0):
                permissions[key] = permission

        with closing(self._connect()) as conn:
            if user.role == ROLE_DEPT_ADMIN:
                rows = conn.execute(
                    "SELECT name, department_id FROM knowledge_bases WHERE department_id = ?",
                    (user.department_id,),
                ).fetchall()
                for row in rows:
                    add_permission(row["name"], "admin", row["department_id"])

            rows = conn.execute(
                """
                SELECT kb.name AS kb_name, kb.department_id, p.permission
                FROM kb_permissions p
                JOIN knowledge_bases kb ON kb.id = p.kb_id
                WHERE p.user_id = ? AND kb.department_id = ?
                """,
                (user.id, user.department_id),
            ).fetchall()
            for row in rows:
                add_permission(row["kb_name"], row["permission"], row["department_id"])

        return permissions

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


def build_request_context(session_state) -> RequestContext:
    session_id = ensure_session_id(session_state)
    username = session_state.get("username")
    department_id = session_state.get("department_id")
    kb_id = session_state.get("current_kb_id")
    resource_department_id = session_state.get("current_kb_department_id")
    if resource_department_id in (None, ""):
        resource_department_id = department_id
    if username:
        auth_service = AuthService()
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
