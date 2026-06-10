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
from src.rag_backends.schemas import RequestContext


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
        conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(department_id) REFERENCES departments(id)
                )
            """)
            self._ensure_column(conn, "users", "department_id", "INTEGER")
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    department_id INTEGER,
                    owner_user_id INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(department_id) REFERENCES departments(id),
                    FOREIGN KEY(owner_user_id) REFERENCES users(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kb_permissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kb_name TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    permission TEXT NOT NULL DEFAULT 'read',
                    created_at TEXT NOT NULL,
                    UNIQUE(kb_name, user_id),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            """)
            self._ensure_default_department(conn)

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

    def _ensure_default_admin(self):
        username = config.settings.AUTH_DEFAULT_ADMIN_USERNAME
        password = config.settings.AUTH_DEFAULT_ADMIN_PASSWORD
        with closing(self._connect()) as conn:
            dept = conn.execute("SELECT id FROM departments WHERE name = 'system'").fetchone()
            department_id = dept["id"] if dept else None
            row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if row is not None:
                return
            now = utc_now()
            conn.execute(
                """
                INSERT INTO users (username, password_hash, role, department_id, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (username, hash_password(password), ROLE_SYSTEM_ADMIN, department_id, now, now),
            )

    def create_user(self, username: str, password: str, role: str = "user", department_id: int | None = None) -> AuthUser:
        username = username.strip()
        if not username:
            raise ValueError("用户名不能为空")
        if not password:
            raise ValueError("密码不能为空")
        if role not in VALID_ROLES:
            raise ValueError("角色必须为 system_admin、dept_admin 或 user")

        now = utc_now()
        with closing(self._connect()) as conn:
            if department_id is None:
                dept = conn.execute("SELECT id FROM departments WHERE name = 'system'").fetchone()
                department_id = dept["id"] if dept else None
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
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT u.*, d.name AS department_name
                FROM users u
                LEFT JOIN departments d ON d.id = u.department_id
                WHERE u.username = ? AND u.is_active = 1
                """,
                (username.strip(),),
            ).fetchone()
            if row is None or not verify_password(password, row["password_hash"]):
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
        return AuthSession(token=token, user=row_to_user(row), expires_at=expires_dt.isoformat())

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

    def revoke_session(self, token: str | None):
        if not token:
            return
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (utc_now(), hash_token(token)),
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
            return self.list_users()
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
        return Department(id=int(row["id"]), name=row["name"])

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

    def register_knowledge_base(self, kb_name: str, owner: AuthUser | None = None):
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
            if owner_id:
                self.grant_kb_permission(kb_name, owner_id, "admin", conn=conn)

    def delete_knowledge_base_record(self, kb_name: str):
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM kb_permissions WHERE kb_name = ?", (kb_name,))
            conn.execute("DELETE FROM knowledge_bases WHERE name = ?", (kb_name,))

    def grant_kb_permission(self, kb_name: str, user_id: int, permission: str = "read", conn=None):
        if permission not in {"read", "write", "admin"}:
            raise ValueError("权限必须为 read、write 或 admin")
        target_conn = conn or self._connect()
        should_close = conn is None
        try:
            target_conn.execute(
                """
                INSERT INTO kb_permissions (kb_name, user_id, permission, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(kb_name, user_id) DO UPDATE SET permission = excluded.permission
                """,
                (kb_name, user_id, permission, utc_now()),
            )
        finally:
            if should_close:
                target_conn.close()

    def list_accessible_kbs(self, user: AuthUser, existing_kbs: list[str]) -> list[str]:
        if user.role == ROLE_SYSTEM_ADMIN:
            return existing_kbs

        with closing(self._connect()) as conn:
            if user.role == ROLE_DEPT_ADMIN:
                rows = conn.execute(
                    """
                    SELECT kb.name
                    FROM knowledge_bases kb
                    WHERE kb.department_id = ?
                    UNION
                    SELECT p.kb_name
                    FROM kb_permissions p
                    WHERE p.user_id = ?
                    """,
                    (user.department_id, user.id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT p.kb_name
                    FROM kb_permissions p
                    WHERE p.user_id = ?
                    """,
                    (user.id,),
                ).fetchall()
        allowed = {row[0] for row in rows}
        return [kb for kb in existing_kbs if kb in allowed]

    def get_user_by_username(self, username: str | None) -> AuthUser | None:
        if not username:
            return None
        with closing(self._connect()) as conn:
            row = self._get_user_row(conn, username)
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
    role = session_state.get("role")
    department_id = session_state.get("department_id")
    if username:
        return RequestContext(
            user_id=username,
            session_id=session_id,
            roles=[role or "user"],
            metadata={"department_id": department_id},
        )
    return RequestContext(
        user_id=anonymous_user_id(session_id),
        session_id=session_id,
        roles=["anonymous"],
    )
