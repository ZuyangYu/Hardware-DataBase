import os
import json
import stat
import threading
import hashlib
from enum import Enum
from dotenv import load_dotenv


ENV_FILE_ENCODING = "utf-8-sig"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 默认仍是仓库根 .env；容器部署可用 HDB_ENV_PATH 把可写配置指向挂载目录。
# 原因：管理页保存走 save_settings_to_env 的 mkstemp + os.replace 原子替换，
# 而镜像层内的 /app 与单文件 bind mount 都不允许 rename（EBUSY），
# 因此 Docker 部署把 .env 放在挂载的目录里再指过来。
ENV_FILE_PATH = os.getenv("HDB_ENV_PATH") or os.path.join(BASE_DIR, ".env")
CONFIG_REVISION = ""


def _config_revision(path: str = ENV_FILE_PATH) -> str:
    """Stable content revision shared by API and workers at task boundaries."""
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:16]
    except OSError:
        return "missing"


# Keys that used to be deployment flags but are now built-in behavior or an
# internal constant.  They are intentionally ignored; startup warns once so
# operators can delete them instead of keeping stale knobs forever.
OBSOLETE_ENV_KEYS: dict[str, str] = {
    "AGENT_COMPLEXITY_BUDGET_ENABLED": "built-in agent behavior",
    "AGENT_PARTIAL_SYNTHESIS_ENABLED": "built-in agent behavior",
    "AGENT_RESEARCH_SUBAGENT_ENABLED": "built-in agent behavior",
    "AGENT_VERIFIER_ENABLED": "built-in agent behavior",
    "CANCEL_FAST_PATH_ENABLED": "enabled automatically when REDIS_URL is set",
    "CONVERSATION_UNIFIED_TOOLS_ENABLED": "built-in platform path",
    "MULTI_KB_EXPLICIT_FIELDS_ENABLED": "internal field-contract constant",
    "MULTI_KB_REVIEW_REQUIRE_APPROVAL_FOR_CRITICAL": "security invariant (always on)",
    "RESEARCH_AGENT_ENABLED": "internal subagent constant",
    "REVIEWER_AGENT_ENABLED": "internal subagent constant",
    "SANDBOX_DIAGNOSTIC_TEST_ENABLED": "test-only diagnostic hook",
    "SHARED_RETRIEVAL_EXECUTOR_ENABLED": "built-in shared retrieval pool",
}


def obsolete_env_keys(path: str = ENV_FILE_PATH) -> tuple[str, ...]:
    """Return obsolete keys present in the process env or the .env file."""
    keys = set(os.environ)
    try:
        with open(path, encoding=ENV_FILE_ENCODING) as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                keys.add(stripped.split("=", 1)[0].strip())
    except OSError:
        pass
    return tuple(sorted(key for key in keys if key in OBSOLETE_ENV_KEYS))


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment setting using the application's flag syntax."""
    return os.getenv(name, str(default).lower()).strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str) -> frozenset[str]:
    """Parse a comma-separated rollout allowlist into normalized values."""
    return frozenset(
        item.strip().casefold()
        for item in os.getenv(name, "").split(",")
        if item.strip()
    )

# Serialises .env read-modify-write cycles (save_settings_to_env). Sync API
# routes run in Starlette's threadpool, so two concurrent PUT /config requests
# would otherwise interleave read/modify/write and lose updates. RLock because
# AppPipeline.apply_settings holds it across validate -> save -> reload while
# save_settings_to_env acquires it again.
_ENV_WRITE_LOCK = threading.RLock()

load_dotenv(dotenv_path=ENV_FILE_PATH, encoding=ENV_FILE_ENCODING)
CONFIG_REVISION = _config_revision()
STORAGE_DIR = os.getenv("STORAGE_DIR") or os.path.join(BASE_DIR, "storage")
PIPELINE_ARCHIVE_ROOT = os.getenv("PIPELINE_ARCHIVE_ROOT", os.path.join(STORAGE_DIR, "pipeline_archives"))
RAGFLOW_FILE_ROOT = os.getenv("RAGFLOW_FILE_ROOT", PIPELINE_ARCHIVE_ROOT)
LOG_DIR = os.path.join(STORAGE_DIR, "logs")
# Workspace-first agent storage.  A workspace owns the hidden file tree and
# artifacts for a session/user; there is no task product behind it.
WORKSPACE_DB_PATH = os.getenv(
    "WORKSPACE_DB_PATH", os.path.join(STORAGE_DIR, "workspaces.db")
)
WORKSPACE_ROOT = os.getenv(
    "WORKSPACE_ROOT", os.path.join(STORAGE_DIR, "workspaces")
)
# Workspace store backend: "" follows CONTROL_PLANE_BACKEND; or sqlite/postgres.
WORKSPACE_STORE_BACKEND = os.getenv("WORKSPACE_STORE_BACKEND", "").strip().lower()
WORKSPACE_DB_URL = os.getenv("WORKSPACE_DB_URL", "").strip()


def _resolve_storage_path(raw_path: str | None, default_name: str) -> str:
    """Resolve memory data paths independently of the process CWD."""
    path = str(raw_path or default_name).strip()
    if not os.path.isabs(path):
        path = os.path.join(STORAGE_DIR, path)
    return os.path.abspath(path)


RESULT_EXPORT_STORAGE_DIR = _resolve_storage_path(os.getenv("RESULT_EXPORT_STORAGE_DIR"), "exports")
RESULT_EXPORT_ENABLED = _env_bool("RESULT_EXPORT_ENABLED", True)
RESULT_EXPORT_MD_ENABLED = _env_bool("RESULT_EXPORT_MD_ENABLED", True)
RESULT_EXPORT_XLSX_ENABLED = _env_bool("RESULT_EXPORT_XLSX_ENABLED", True)
RESULT_EXPORT_DOCX_ENABLED = _env_bool("RESULT_EXPORT_DOCX_ENABLED", True)
RESULT_EXPORT_PDF_ENABLED = _env_bool("RESULT_EXPORT_PDF_ENABLED", True)
RESULT_EXPORT_PPTX_ENABLED = _env_bool("RESULT_EXPORT_PPTX_ENABLED", True)
# Arrange-before-export: one model pass turns the chat answer into
# deliverable content following the user's original request.
RESULT_EXPORT_ORGANIZE_ENABLED = _env_bool("RESULT_EXPORT_ORGANIZE_ENABLED", True)

RAGFLOW_BASE_URL = os.getenv("RAGFLOW_BASE_URL", "http://localhost:9380")
RAGFLOW_API_KEY = os.getenv("RAGFLOW_API_KEY", "")
RAGFLOW_GOVERNANCE_DATASET_NAME = os.getenv("RAGFLOW_GOVERNANCE_DATASET_NAME", "department_governance")
RAGFLOW_DESIGN_DATASET_NAME = os.getenv("RAGFLOW_DESIGN_DATASET_NAME", "project_design_assets")
RAGFLOW_TIMEOUT_SECONDS = int(os.getenv("RAGFLOW_TIMEOUT_SECONDS", "120"))
# Citation readback pages chunk listings explicitly. RAGFlow defaults to 30
# chunks per page; a single request silently truncates long documents.
RAGFLOW_CHUNK_LIST_PAGE_SIZE = int(os.getenv("RAGFLOW_CHUNK_LIST_PAGE_SIZE", "30"))
RAGFLOW_CHUNK_LIST_MAX_PAGES = int(os.getenv("RAGFLOW_CHUNK_LIST_MAX_PAGES", "100"))
RAGFLOW_CHUNK_LIST_MAX_BYTES = int(os.getenv("RAGFLOW_CHUNK_LIST_MAX_BYTES", str(8 * 1024 * 1024)))
# 解析状态同步最小间隔: 后台 worker 按该间隔把仍在解析中的文档状态与
# RAGFlow 远端对齐, 避免本地台账永远停留在 parsing。
RAGFLOW_STATUS_REFRESH_MIN_SECONDS = int(os.getenv("RAGFLOW_STATUS_REFRESH_MIN_SECONDS", "60"))
RAGFLOW_SIMILARITY_THRESHOLD = float(os.getenv("RAGFLOW_SIMILARITY_THRESHOLD", "0.25"))
RAGFLOW_VECTOR_WEIGHT = float(os.getenv("RAGFLOW_VECTOR_WEIGHT", "0.4"))
# RAGFlow <0.21 会静默忽略检索的 metadata_condition（定制老版本可能直接报错）。
# 开启后，检索把本地台账翻译成 document_ids 预过滤下发（0.19 起支持），
# 隔离与召回不再依赖远端元数据能力；关闭则退回旧行为。
RAGFLOW_RETRIEVAL_DOC_SCOPE_ENABLED = os.getenv("RAGFLOW_RETRIEVAL_DOC_SCOPE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
# document_ids 预过滤的规模上限：超过则回退旧行为（共享数据集上的 top_k 召回可能变差）。
RAGFLOW_RETRIEVAL_DOC_IDS_MAX = int(os.getenv("RAGFLOW_RETRIEVAL_DOC_IDS_MAX", "2000") or 2000)
# 文档元数据（meta_fields）写入失败时是否降级继续：老版本/定制构建可能拒绝
# meta_fields；检索隔离由 document_ids 保证，元数据不是上传的硬前提。
RAGFLOW_METADATA_WRITE_BEST_EFFORT = os.getenv("RAGFLOW_METADATA_WRITE_BEST_EFFORT", "true").strip().lower() in {"1", "true", "yes", "on"}

# Task 7 gray rollout: role/structure read models can be disabled per
# deployment; exact refdes/net queries always stay available.
CIRCUIT_SEMANTIC_QUERY_ENABLED = os.getenv("CIRCUIT_SEMANTIC_QUERY_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}

# 表格管线:模板使用说明/模板变更历史等样板 sheet 不进行行索引,
# 避免其作为证据污染检索堆(源文件仍保留,可随时关闭开关回溯)。
SPREADSHEET_SKIP_BOILERPLATE_SHEETS = os.getenv("SPREADSHEET_SKIP_BOILERPLATE_SHEETS", "true").strip().lower() in {"1", "true", "yes", "on"}

# Agent 单题证据堆上限:超出后按问题词法相关性准入新证据(已入堆不淘汰)。
AGENT_EVIDENCE_BUDGET = int(os.getenv("AGENT_EVIDENCE_BUDGET", "30") or 30)
# 对话附件(长文档分片)精读需要比普通问答更多的证据槽位; 附件在场时取两者较大值。
AGENT_ATTACHMENT_EVIDENCE_BUDGET = int(
    os.getenv("AGENT_ATTACHMENT_EVIDENCE_BUDGET", "80") or 80
)

# 站内通知的可选外发 webhook（企业微信/钉钉/邮件网关等）。留空则仅站内通知。
NOTIFY_WEBHOOK_URL = os.getenv("NOTIFY_WEBHOOK_URL", "").strip()
NOTIFY_WEBHOOK_TOKEN = os.getenv("NOTIFY_WEBHOOK_TOKEN", "").strip()
NOTIFY_WEBHOOK_TIMEOUT_SECONDS = float(
    os.getenv("NOTIFY_WEBHOOK_TIMEOUT_SECONDS", "2") or 2
)

AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", os.path.join(STORAGE_DIR, "auth.db"))
# LangGraph checkpointer：agent 会话状态（thread 级完整消息历史）的持久化库。
AGENT_CHECKPOINT_DB_PATH = os.getenv(
    "AGENT_CHECKPOINT_DB_PATH", os.path.join(STORAGE_DIR, "agent_checkpoints.db")
)
# P1-001: checkpointer backend selection; postgres is the multi-instance target.
AGENT_CHECKPOINT_BACKEND = os.getenv("AGENT_CHECKPOINT_BACKEND", "sqlite").strip().lower()
if AGENT_CHECKPOINT_BACKEND not in {"sqlite", "postgres"}:
    AGENT_CHECKPOINT_BACKEND = "sqlite"
AGENT_CHECKPOINT_DATABASE_URL = os.getenv("AGENT_CHECKPOINT_DATABASE_URL", "")
# P1: bounded connection pool for the PostgreSQL checkpointer (thread-safe
# multi-instance use; 1 connection per bounded API/Worker thread).
AGENT_CHECKPOINT_POOL_SIZE = int(os.getenv("AGENT_CHECKPOINT_POOL_SIZE", "4"))
AUTH_DEFAULT_ADMIN_USERNAME = os.getenv("AUTH_DEFAULT_ADMIN_USERNAME", "admin")
AUTH_DEFAULT_ADMIN_PASSWORD = os.getenv("AUTH_DEFAULT_ADMIN_PASSWORD", "")
AUTH_SESSION_TTL_HOURS = int(os.getenv("AUTH_SESSION_TTL_HOURS", "24"))
# Long-lived personal MCP token, exchanged for a short session token by the bridge.
MCP_TOKEN_TTL_DAYS = int(os.getenv("MCP_TOKEN_TTL_DAYS", "365"))
MCP_SESSION_TTL_HOURS = int(os.getenv("MCP_SESSION_TTL_HOURS", "24"))


class Provider(Enum):
    OLLAMA = "ollama"
    CUSTOM = "custom"


try:
    AGENT_LLM_PROVIDER = Provider(os.getenv("AGENT_LLM_PROVIDER", "ollama").lower())
except ValueError as exc:
    valid = ", ".join(p.value for p in Provider)
    raise ValueError(f"Invalid AGENT_LLM_PROVIDER, expected one of: {valid}") from exc

AGENT_OLLAMA_BASE_URL = os.getenv("AGENT_OLLAMA_BASE_URL", "http://localhost:11434")
AGENT_OLLAMA_MODEL = os.getenv("AGENT_OLLAMA_MODEL", "qwen2.5:32b")
AGENT_CUSTOM_API_KEY = os.getenv("AGENT_CUSTOM_API_KEY", "")
AGENT_CUSTOM_BASE_URL = os.getenv("AGENT_CUSTOM_BASE_URL", "")
AGENT_CUSTOM_MODEL = os.getenv("AGENT_CUSTOM_MODEL", "")
AGENT_CUSTOM_MAX_TOKENS = int(os.getenv("AGENT_CUSTOM_MAX_TOKENS", "4096"))
# Optional OpenAI-compatible reasoning effort (low/high/max). Reasoning models
# count hidden reasoning tokens against max_tokens, so a low effort avoids
# spending the whole output budget before any visible answer is emitted.
AGENT_REASONING_EFFORT = os.getenv("AGENT_REASONING_EFFORT", "").strip().lower()
# 声明模型上下文窗口（输入侧），激活 deepagents SummarizationMiddleware 的
# 主动压缩（85% 触发 / 保留 10%）。OpenAI 兼容中转（OpenRouter/DeepSeek/
# SiliconFlow）不暴露模型 profile，需要部署方显式给出；模型自带已知 profile
# 时以已知值为准。0 = 不声明。
AGENT_MODEL_MAX_INPUT_TOKENS = int(os.getenv("AGENT_MODEL_MAX_INPUT_TOKENS", "65536"))
AGENT_TEMPERATURE = float(os.getenv("AGENT_TEMPERATURE", "0.2"))
AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "120"))
AGENT_RATE_LIMIT_MAX_RETRIES = int(os.getenv("AGENT_RATE_LIMIT_MAX_RETRIES", "4"))
AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS = float(os.getenv("AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS", "1"))
AGENT_RATE_LIMIT_MAX_DELAY_SECONDS = float(os.getenv("AGENT_RATE_LIMIT_MAX_DELAY_SECONDS", "16"))

# Process-wide LLM admission control (src/core/llm_governor.py):
# interactive traffic keeps reserved capacity and batch jobs (wiki/memory)
# are capped separately. 0 = unlimited queue wait.
LLM_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "4"))
LLM_BATCH_MAX_CONCURRENCY = int(os.getenv("LLM_BATCH_MAX_CONCURRENCY", "1"))
LLM_QUEUE_TIMEOUT_SECONDS = int(os.getenv("LLM_QUEUE_TIMEOUT_SECONDS", "180"))
LLM_QUEUE_WARN_SECONDS = int(os.getenv("LLM_QUEUE_WARN_SECONDS", "10"))
LLM_STREAM_IDLE_TIMEOUT_SECONDS = int(os.getenv("LLM_STREAM_IDLE_TIMEOUT_SECONDS", "0"))
# Platform plumbing, not a product switch: the governed unified tool surface
# is the default path. Legacy assembly only remains for internal callers that
# have no RequestContext.
CONVERSATION_UNIFIED_TOOLS_ENABLED = True

# External conversation (外部对话) domain switches.
EXTERNAL_CONVERSATION_LLM_STRUCTURE = os.getenv("EXTERNAL_CONVERSATION_LLM_STRUCTURE", "true").lower() in {"1", "true", "yes", "on"}
EXTERNAL_CONVERSATION_LLM_MAX_CHARS = int(os.getenv("EXTERNAL_CONVERSATION_LLM_MAX_CHARS", "12000"))
EXTERNAL_CONVERSATION_LLM_SUMMARY = os.getenv("EXTERNAL_CONVERSATION_LLM_SUMMARY", "true").lower() in {"1", "true", "yes", "on"}
EXTERNAL_CONVERSATION_LLM_TIMEOUT_SECONDS = int(os.getenv("EXTERNAL_CONVERSATION_LLM_TIMEOUT_SECONDS", "60"))

# Long-term memory (LangMem) is deliberately fail-open for chat.  The
# catalog/jobs live in AUTH_DB_PATH; only the rebuildable LangGraph projection
# uses this separate SQLite file.
MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
MEMORY_STORE_BACKEND = os.getenv("MEMORY_STORE_BACKEND", "sqlite").strip().lower()
# P1: PostgreSQL projection target (rebuildable; authority ledger migrates first).
MEMORY_DATABASE_URL = os.getenv("MEMORY_DATABASE_URL", "")
MEMORY_SQLITE_PATH = _resolve_storage_path(os.getenv("MEMORY_SQLITE_PATH"), "memory.db")
MEMORY_SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("MEMORY_SQLITE_BUSY_TIMEOUT_MS", "30000"))
MEMORY_SINGLE_WRITER = os.getenv("MEMORY_SINGLE_WRITER", "true").lower() in {"1", "true", "yes", "on"}
MEMORY_EXTRACTION_ENABLED = os.getenv("MEMORY_EXTRACTION_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
MEMORY_DEBOUNCE_SECONDS = int(os.getenv("MEMORY_DEBOUNCE_SECONDS", "300"))
MEMORY_JOB_LEASE_SECONDS = int(os.getenv("MEMORY_JOB_LEASE_SECONDS", "180"))
MEMORY_JOB_MAX_RETRIES = int(os.getenv("MEMORY_JOB_MAX_RETRIES", "5"))
MEMORY_REFLECTION_TIMEOUT_SECONDS = int(os.getenv("MEMORY_REFLECTION_TIMEOUT_SECONDS", "120"))
MEMORY_REFLECTION_MIN_INTERVAL_SECONDS = float(os.getenv("MEMORY_REFLECTION_MIN_INTERVAL_SECONDS", "0"))
MEMORY_REFLECTION_CIRCUIT_FAILURES = int(os.getenv("MEMORY_REFLECTION_CIRCUIT_FAILURES", "5"))
MEMORY_REFLECTION_CIRCUIT_COOLDOWN_SECONDS = int(os.getenv("MEMORY_REFLECTION_CIRCUIT_COOLDOWN_SECONDS", "60"))
MEMORY_PROJECTION_MAX_RETRIES = int(os.getenv("MEMORY_PROJECTION_MAX_RETRIES", "5"))
MEMORY_RECONCILE_INTERVAL_SECONDS = int(os.getenv("MEMORY_RECONCILE_INTERVAL_SECONDS", "3600"))
MEMORY_USER_TOP_K = int(os.getenv("MEMORY_USER_TOP_K", "3"))
MEMORY_PROJECT_TOP_K = int(os.getenv("MEMORY_PROJECT_TOP_K", "5"))
MEMORY_STORE_OVERSAMPLE_FACTOR = int(os.getenv("MEMORY_STORE_OVERSAMPLE_FACTOR", "4"))
MEMORY_STORE_MAX_SCAN = int(os.getenv("MEMORY_STORE_MAX_SCAN", "100"))
MEMORY_CONTEXT_MAX_TOKENS = int(os.getenv("MEMORY_CONTEXT_MAX_TOKENS", "1800"))
MEMORY_ITEM_MAX_TOKENS = int(os.getenv("MEMORY_ITEM_MAX_TOKENS", "350"))
MEMORY_MIN_SCORE_ENABLED = os.getenv("MEMORY_MIN_SCORE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
MEMORY_MIN_SCORE = os.getenv("MEMORY_MIN_SCORE", "")
MEMORY_MODEL_PROVIDER = os.getenv("MEMORY_MODEL_PROVIDER", "")
MEMORY_MODEL = os.getenv("MEMORY_MODEL", "")
MEMORY_MODEL_BASE_URL = os.getenv("MEMORY_MODEL_BASE_URL", "")
MEMORY_MODEL_API_KEY = os.getenv("MEMORY_MODEL_API_KEY", "")
MEMORY_MODEL_REQUIRE_STRUCTURED_TOOLS = os.getenv("MEMORY_MODEL_REQUIRE_STRUCTURED_TOOLS", "true").lower() in {"1", "true", "yes", "on"}
MEMORY_EMBEDDING_PROVIDER = os.getenv("MEMORY_EMBEDDING_PROVIDER", "")
MEMORY_EMBEDDING_MODEL = os.getenv("MEMORY_EMBEDDING_MODEL", "")
MEMORY_EMBEDDING_BASE_URL = os.getenv("MEMORY_EMBEDDING_BASE_URL", "")
MEMORY_EMBEDDING_API_KEY = os.getenv("MEMORY_EMBEDDING_API_KEY", "")
MEMORY_EMBEDDING_DIMS = os.getenv("MEMORY_EMBEDDING_DIMS", "")
# ANN 索引类型：hnsw | ivfflat | flat；留空时按维度自动选择
# （>2000 维必须用 flat——pgvector 的 hnsw/ivfflat 上限 2000 维）。
MEMORY_EMBEDDING_INDEX_KIND = os.getenv("MEMORY_EMBEDDING_INDEX_KIND", "").strip().lower()
MEMORY_INDEX_FIELDS = os.getenv("MEMORY_INDEX_FIELDS", "content.content,content.title,content.subject")
MEMORY_ALLOW_GENERAL_CHAT_EXTRACTION = os.getenv("MEMORY_ALLOW_GENERAL_CHAT_EXTRACTION", "false").lower() in {"1", "true", "yes", "on"}
MEMORY_USER_MEMORY_OPT_IN = os.getenv("MEMORY_USER_MEMORY_OPT_IN", "false").lower() in {"1", "true", "yes", "on"}
MEMORY_USER_MEMORY_REQUIRE_PER_EVENT_CONSENT = os.getenv("MEMORY_USER_MEMORY_REQUIRE_PER_EVENT_CONSENT", "true").lower() in {"1", "true", "yes", "on"}
MEMORY_RETENTION_DAYS = os.getenv("MEMORY_RETENTION_DAYS", "")

FINAL_TOP_K = int(os.getenv("FINAL_TOP_K", "5"))
AGENT_MAX_RETRIEVAL_ROUNDS = int(os.getenv("AGENT_MAX_RETRIEVAL_ROUNDS", "3"))
# Deep-mode hard budgets: keep exploration bounded and reserve the tail for
# final synthesis / artifact generation instead of burning every step on search.
AGENT_DEEP_TOOL_CALL_CAP = int(os.getenv("AGENT_DEEP_TOOL_CALL_CAP", "32"))
AGENT_DEEP_RETRIEVAL_CALL_CAP = int(os.getenv("AGENT_DEEP_RETRIEVAL_CALL_CAP", "20"))
AGENT_DEEP_FINALIZE_RESERVE = int(os.getenv("AGENT_DEEP_FINALIZE_RESERVE", "4"))
AGENT_DEEP_LOOP_SECONDS = int(os.getenv("AGENT_DEEP_LOOP_SECONDS", "360"))
WORKER_POLL_INTERVAL_SECONDS = float(os.getenv("WORKER_POLL_INTERVAL_SECONDS", "0.5"))
WORKER_PARSE_BATCH_SIZE = int(os.getenv("WORKER_PARSE_BATCH_SIZE", "1"))
WORKER_CLAIM_BATCH_SIZE = int(os.getenv("WORKER_CLAIM_BATCH_SIZE", "8"))
WORKER_TURN_CONCURRENCY = int(os.getenv("WORKER_TURN_CONCURRENCY", "1"))
# P0a worker role: all (legacy dev) | chat | authoring | parse
WORKER_ROLE = os.getenv("WORKER_ROLE", "all").strip().lower()
if WORKER_ROLE not in {"all", "chat", "authoring", "parse"}:
    WORKER_ROLE = "all"

# P0b resource governance.  process = per-process semaphores (single host dev);
# deployment = shared SQLite quota table across same-host API/Worker processes.
# Rollout flag: off until the stage acceptance report opens it (plan §10.1).
RESOURCE_GOVERNOR_ENABLED = _env_bool("RESOURCE_GOVERNOR_ENABLED", True)
RESOURCE_GOVERNOR_SCOPE = os.getenv("RESOURCE_GOVERNOR_SCOPE", "process").strip().lower()
if RESOURCE_GOVERNOR_SCOPE not in {"process", "deployment"}:
    RESOURCE_GOVERNOR_SCOPE = "process"
RESOURCE_GOVERNOR_DB_PATH = _resolve_storage_path(
    os.getenv("RESOURCE_GOVERNOR_DB_PATH"), "resource_quotas.db"
)
# P1/G3: deployment quota storage.  sqlite = same-host shared file (default);
# postgres = cross-host shared quota database (requires SCOPE=deployment).
RESOURCE_GOVERNOR_BACKEND = os.getenv("RESOURCE_GOVERNOR_BACKEND", "sqlite").strip().lower()
if RESOURCE_GOVERNOR_BACKEND not in {"sqlite", "postgres"}:
    RESOURCE_GOVERNOR_BACKEND = "sqlite"
RESOURCE_GOVERNOR_DATABASE_URL = os.getenv("RESOURCE_GOVERNOR_DATABASE_URL", "")
RESOURCE_QUEUE_MAX_WAIT_SECONDS = float(os.getenv("RESOURCE_QUEUE_MAX_WAIT_SECONDS", "30"))
RESOURCE_QUEUE_MAX_DEPTH = int(os.getenv("RESOURCE_QUEUE_MAX_DEPTH", "256"))
RESOURCE_PERMIT_LEASE_SECONDS = int(os.getenv("RESOURCE_PERMIT_LEASE_SECONDS", "300"))
# P0-006: uncertain permits older than this pause new admissions for their
# resource and raise an operations alert (never auto-release).
RESOURCE_UNCERTAIN_ESCALATE_SECONDS = int(os.getenv("RESOURCE_UNCERTAIN_ESCALATE_SECONDS", "600"))
# After this grace period an isolated uncertain permit is released with
# server_deadline_passed evidence instead of blocking admission forever.
RESOURCE_UNCERTAIN_AUTO_RELEASE_SECONDS = int(
    os.getenv("RESOURCE_UNCERTAIN_AUTO_RELEASE_SECONDS", "1800")
)
RESOURCE_GOVERNOR_MAINTENANCE_SECONDS = float(
    os.getenv("RESOURCE_GOVERNOR_MAINTENANCE_SECONDS", "5")
)
RESOURCE_GOVERNOR_DEPLOYMENT_ID = os.getenv("RESOURCE_GOVERNOR_DEPLOYMENT_ID", "local").strip() or "local"
# Control-plane storage: sqlite (default, auth.db) or postgres.  The
# PostgreSQL backend stays disabled until the conversation/memory port and
# the cutover rehearsal land; the provider guard rejects silent enablement.
CONTROL_PLANE_BACKEND = os.getenv("CONTROL_PLANE_BACKEND", "sqlite").strip().lower()
CONTROL_PLANE_DATABASE_URL = os.getenv("CONTROL_PLANE_DATABASE_URL", "").strip()
CONTROL_PLANE_POOL_SIZE = int(os.getenv("CONTROL_PLANE_POOL_SIZE", "8"))
EVALUATION_STORE_BACKEND = os.getenv("EVALUATION_STORE_BACKEND", "").strip().lower()
EVALUATION_STORE_DATABASE_URL = os.getenv("EVALUATION_STORE_DATABASE_URL", "").strip()
EVAL_EXECUTION_MODE = os.getenv("EVAL_EXECUTION_MODE", "inline").strip().lower()
# Quality must use the control-plane database for terminal atomicity.
QUALITY_ENABLED = os.getenv("QUALITY_ENABLED", "true").lower() == "true"
QUALITY_CAPTURE_CONTENT = os.getenv("QUALITY_CAPTURE_CONTENT", "true").lower() == "true"
QUALITY_SAMPLE_RATE = float(os.getenv("QUALITY_SAMPLE_RATE", "0.01"))
QUALITY_SAMPLE_DAILY_LIMIT = int(os.getenv("QUALITY_SAMPLE_DAILY_LIMIT", "100"))
QUALITY_PHOENIX_SYNC_ENABLED = os.getenv("QUALITY_PHOENIX_SYNC_ENABLED", "false").lower() == "true"
QUALITY_PHOENIX_ENDPOINT = os.getenv("QUALITY_PHOENIX_ENDPOINT", "").strip()
QUALITY_PHOENIX_API_KEY = os.getenv("QUALITY_PHOENIX_API_KEY", "").strip()
QUALITY_PHOENIX_MAX_ATTEMPTS = int(os.getenv("QUALITY_PHOENIX_MAX_ATTEMPTS", "20"))
COST_LEDGER_ENABLED = os.getenv("COST_LEDGER_ENABLED", "true").lower() == "true"
COST_MODEL_REGISTRY = json.loads(os.getenv("COST_MODEL_REGISTRY", "{}"))
# 本地台账域（pipeline_documents）的存储后端。空值 = 跟随
# CONTROL_PLANE_BACKEND（存储收敛的终态）；显式 sqlite 可以在迁移完成前
# 让单个域先留在本地文件。DATABASE_URL 空 = CONTROL_PLANE_DATABASE_URL。
PIPELINE_DOCUMENT_STORE_BACKEND = os.getenv(
    "PIPELINE_DOCUMENT_STORE_BACKEND", ""
).strip().lower()
PIPELINE_DOCUMENT_DATABASE_URL = os.getenv(
    "PIPELINE_DOCUMENT_DATABASE_URL", ""
).strip()
# 认证域（账号 + 审计）后端。**默认 sqlite**，不跟随控制面：PG 里这几张表
# 早期按语义类型建过，必须先跑 rebuild_auth_domain.py 再显式切换。
AUTH_STORE_BACKEND = os.getenv("AUTH_STORE_BACKEND", "sqlite").strip().lower()
AUTH_STORE_DATABASE_URL = os.getenv("AUTH_STORE_DATABASE_URL", "").strip()
# Redis realtime plane (cancel fast path first; empty URL disables it).
REDIS_URL = os.getenv("REDIS_URL", "").strip()
# 硬依赖开关：true（默认）时 Redis 必须已配置且可达，否则 API/worker 拒绝启动，
# /health/ready 也会因 Redis 不可用判为 not_ready。仅测试隔离时置 false。
REDIS_REQUIRED = _env_bool("REDIS_REQUIRED", True)
CANCEL_KEY_TTL_SECONDS = int(os.getenv("CANCEL_KEY_TTL_SECONDS", "7200"))
TURN_EVENT_STREAM_TTL_SECONDS = int(os.getenv("TURN_EVENT_STREAM_TTL_SECONDS", "7200"))
TURN_EVENT_STREAM_MAXLEN = int(os.getenv("TURN_EVENT_STREAM_MAXLEN", "5000"))
# L2 distributed admission (Redis Lua); requires REDIS_URL to take effect.
DISTRIBUTED_ADMISSION_ENABLED = _env_bool("DISTRIBUTED_ADMISSION_ENABLED", False)
DISTRIBUTED_ADMISSION_TTL_SECONDS = int(os.getenv("DISTRIBUTED_ADMISSION_TTL_SECONDS", "120"))
# Provider RPM budget counted at the model boundary (0 disables).
PROVIDER_RPM_LIMIT = int(os.getenv("PROVIDER_RPM_LIMIT", "0"))
# Provider token budget per minute, counted from estimated input/output tokens.
PROVIDER_TPM_LIMIT = int(os.getenv("PROVIDER_TPM_LIMIT", "0"))
# Production processes must point at a pre-provisioned quota database instead
# of silently creating a same-named local file.
RESOURCE_GOVERNOR_REQUIRE_PRECONFIGURED = _env_bool(
    "RESOURCE_GOVERNOR_REQUIRE_PRECONFIGURED", False
)
CHAT_QUEUE_DEADLINE_SECONDS = int(os.getenv("CHAT_QUEUE_DEADLINE_SECONDS", "600"))
# Deep（知识库复杂任务）回合的硬看门狗。内部工具/时间预算已封顶，
# 外部看门狗只负责兜底，不再允许 15 分钟级挂起。
CHAT_QUEUE_DEADLINE_DEEP_SECONDS = int(
    os.getenv("CHAT_QUEUE_DEADLINE_DEEP_SECONDS", "480")
)
RESOURCE_LLM_MAX_CONCURRENT = int(os.getenv("RESOURCE_LLM_MAX_CONCURRENT", "4"))
RESOURCE_LLM_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_LLM_RESERVED_INTERACTIVE", "2"))
RESOURCE_RAG_MAX_CONCURRENT = int(os.getenv("RESOURCE_RAG_MAX_CONCURRENT", "8"))
RESOURCE_RAG_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_RAG_RESERVED_INTERACTIVE", "4"))
RESOURCE_TOOL_MAX_CONCURRENT = int(os.getenv("RESOURCE_TOOL_MAX_CONCURRENT", "16"))
RESOURCE_TOOL_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_TOOL_RESERVED_INTERACTIVE", "0"))
RESOURCE_CHAT_MAX_CONCURRENT = int(os.getenv("RESOURCE_CHAT_MAX_CONCURRENT", "4"))
RESOURCE_CHAT_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_CHAT_RESERVED_INTERACTIVE", "0"))
# Durable task turns get their own admission class so long-running research
# never consumes interactive chat slots.
RESOURCE_TASK_MAX_CONCURRENT = int(os.getenv("RESOURCE_TASK_MAX_CONCURRENT", "2"))
RESOURCE_TASK_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_TASK_RESERVED_INTERACTIVE", "0"))
RESOURCE_HEAVY_JOB_MAX_CONCURRENT = int(os.getenv("RESOURCE_HEAVY_JOB_MAX_CONCURRENT", "4"))
RESOURCE_HEAVY_JOB_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_HEAVY_JOB_RESERVED_INTERACTIVE", "1"))
RESOURCE_OCR_MAX_CONCURRENT = int(os.getenv("RESOURCE_OCR_MAX_CONCURRENT", "1"))
RESOURCE_OCR_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_OCR_RESERVED_INTERACTIVE", "0"))
RESOURCE_EXPORT_MAX_CONCURRENT = int(os.getenv("RESOURCE_EXPORT_MAX_CONCURRENT", "4"))
RESOURCE_EXPORT_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_EXPORT_RESERVED_INTERACTIVE", "0"))
RESOURCE_MEMORY_MAX_CONCURRENT = int(os.getenv("RESOURCE_MEMORY_MAX_CONCURRENT", "1"))
RESOURCE_MEMORY_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_MEMORY_RESERVED_INTERACTIVE", "0"))

# MCP external plugins (外部插件): remote-only tool federation.  The switch is
# an emergency kill switch and defaults on: without a registered+enabled
# server nothing connects, and visibility still requires department scope,
# subscription and the external_tool.invoke capability.  Every key here is
# hot-reloadable at worker turn boundaries except the credentials key, which
# is environment-only and never enters the config write whitelist.
MCP_ENABLED = _env_bool("MCP_ENABLED", True)
# These are platform plumbing rather than product switches. An old .env may
# still contain false values from the multi-switch rollout; once MCP is part
# of the built-in production path, repair those values in memory so startup
# and ordinary settings changes do not get trapped behind hidden prerequisites.
if MCP_ENABLED:
    RESOURCE_GOVERNOR_ENABLED = True
MCP_ALLOW_INSECURE_HTTP = _env_bool("MCP_ALLOW_INSECURE_HTTP", False)
MCP_ALLOW_PRIVATE_HOSTS = _env_bool("MCP_ALLOW_PRIVATE_HOSTS", False)
MCP_CONNECT_TIMEOUT_SECONDS = int(os.getenv("MCP_CONNECT_TIMEOUT_SECONDS", "15"))
MCP_CALL_TIMEOUT_SECONDS = int(os.getenv("MCP_CALL_TIMEOUT_SECONDS", "60"))
MCP_SSE_READ_TIMEOUT_SECONDS = int(os.getenv("MCP_SSE_READ_TIMEOUT_SECONDS", "300"))
MCP_MAX_RESULT_CHARS = int(os.getenv("MCP_MAX_RESULT_CHARS", "20000"))
MCP_MAX_RESULT_BYTES = int(os.getenv("MCP_MAX_RESULT_BYTES", "65536"))
MCP_MAX_ARGUMENT_CHARS = int(os.getenv("MCP_MAX_ARGUMENT_CHARS", "12000"))
MCP_MAX_TOOLS_PER_SERVER = int(os.getenv("MCP_MAX_TOOLS_PER_SERVER", "50"))
MCP_MAX_SERVERS_PER_RUN = int(os.getenv("MCP_MAX_SERVERS_PER_RUN", "5"))
MCP_CALLS_PER_TURN = int(os.getenv("MCP_CALLS_PER_TURN", "8"))
MCP_FAILURE_COOLDOWN_SECONDS = int(os.getenv("MCP_FAILURE_COOLDOWN_SECONDS", "60"))
# Phase 2 write channel: candidate artifacts only, user-confirmed; off by default.
MCP_WRITE_ENABLED = _env_bool("MCP_WRITE_ENABLED", False)
MCP_WRITE_CONFIRM_TTL_SECONDS = int(os.getenv("MCP_WRITE_CONFIRM_TTL_SECONDS", "1800"))
MCP_MAX_ARTIFACT_BYTES = int(os.getenv("MCP_MAX_ARTIFACT_BYTES", str(50 * 1024 * 1024)))
MCP_ARTIFACT_ALLOWED_EXTENSIONS = os.getenv(
    "MCP_ARTIFACT_ALLOWED_EXTENSIONS", "kicad_sch,kicad_pcb,kicad_pro,drawio,zip,json,csv"
)
# Phase 2 candidate artifacts: validated (extension/size/hash) and stored here
# before any download link exists; originals are never touched.
MCP_ARTIFACT_STORAGE_DIR = _resolve_storage_path(
    os.getenv("MCP_ARTIFACT_STORAGE_DIR"), "mcp_artifacts"
)
MCP_WRITE_MAX_ATTEMPTS = int(os.getenv("MCP_WRITE_MAX_ATTEMPTS", "3"))
# Fernet/HMAC key for stored MCP credentials.  Deliberately absent from
# DEFAULT_VALUES so it is neither returned by GET /config nor writable through
# PUT /config; rotation is an operator action with a restart.
MCP_CREDENTIALS_KEY = os.getenv("MCP_CREDENTIALS_KEY", "").strip()
RESOURCE_MCP_MAX_CONCURRENT = int(os.getenv("RESOURCE_MCP_MAX_CONCURRENT", "4"))
RESOURCE_MCP_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_MCP_RESERVED_INTERACTIVE", "1"))

# External extension sandbox control plane.  The application never starts a
# third-party process; ``runner_api`` means an independently deployed Runner
# API must be available and authenticated.  ``disabled`` is the only safe
# default until a concrete runtime/backend has passed the P0 probe.
SANDBOX_ENABLED = _env_bool("SANDBOX_ENABLED", False)
SANDBOX_BACKEND = os.getenv("SANDBOX_BACKEND", "disabled").strip().lower() or "disabled"
SANDBOX_GATEWAY_ENABLED = _env_bool("SANDBOX_GATEWAY_ENABLED", False)
SANDBOX_GATEWAY_URL = os.getenv("SANDBOX_GATEWAY_URL", "").strip()
SANDBOX_RUNNER_URL = os.getenv("SANDBOX_RUNNER_URL", "").strip()
SANDBOX_SERVICE_AUTH_REF = os.getenv("SANDBOX_SERVICE_AUTH_REF", "").strip()
# The reference names an external identity provider/secret mount; it is never
# interpreted as a bearer token by HDB.  A deployment may inject a short-lived
# token through this owner-only file or configure a process-level provider.
SANDBOX_SERVICE_TOKEN_FILE = os.getenv("SANDBOX_SERVICE_TOKEN_FILE", "").strip()
# Test-only diagnostic hook; never a production deployment option.
SANDBOX_DIAGNOSTIC_TEST_ENABLED = False
SANDBOX_DEFAULT_NETWORK = os.getenv("SANDBOX_DEFAULT_NETWORK", "none").strip().lower() or "none"
SANDBOX_MAX_CONCURRENT = int(os.getenv("SANDBOX_MAX_CONCURRENT", "2"))
SANDBOX_TASK_TIMEOUT_SECONDS = int(os.getenv("SANDBOX_TASK_TIMEOUT_SECONDS", "300"))
SANDBOX_TASK_LEASE_SECONDS = int(os.getenv("SANDBOX_TASK_LEASE_SECONDS", "60"))
SANDBOX_MAX_OUTPUT_BYTES = int(os.getenv("SANDBOX_MAX_OUTPUT_BYTES", str(50 * 1024 * 1024)))
SANDBOX_MAX_INPUT_BYTES = int(os.getenv("SANDBOX_MAX_INPUT_BYTES", str(50 * 1024 * 1024)))
SANDBOX_ALLOW_DYNAMIC_INSTALL = _env_bool("SANDBOX_ALLOW_DYNAMIC_INSTALL", False)
SANDBOX_AUTHORIZED_CAPABILITIES = _env_csv("SANDBOX_AUTHORIZED_CAPABILITIES")
# Enabling execution without an operator-recorded runtime attestation is never
# allowed.  The attestation and profile digest are deployment evidence, not
# user-editable API settings.
SANDBOX_REQUIRE_RUNTIME_ATTESTATION = _env_bool("SANDBOX_REQUIRE_RUNTIME_ATTESTATION", True)
SANDBOX_RUNTIME_ATTESTATION_REF = os.getenv("SANDBOX_RUNTIME_ATTESTATION_REF", "").strip()
SANDBOX_RUNTIME_PROFILE_DIGEST = os.getenv("SANDBOX_RUNTIME_PROFILE_DIGEST", "").strip()
SANDBOX_ARTIFACT_SCANNER_REF = os.getenv("SANDBOX_ARTIFACT_SCANNER_REF", "").strip()
SANDBOX_DISPATCH_INTERVAL_SECONDS = float(os.getenv("SANDBOX_DISPATCH_INTERVAL_SECONDS", "2"))
SANDBOX_RECONCILE_INTERVAL_SECONDS = float(os.getenv("SANDBOX_RECONCILE_INTERVAL_SECONDS", "30"))
SANDBOX_MAX_CONCURRENT_PER_USER = int(os.getenv("SANDBOX_MAX_CONCURRENT_PER_USER", "1"))
SANDBOX_MAX_CONCURRENT_PER_DEPARTMENT = int(os.getenv("SANDBOX_MAX_CONCURRENT_PER_DEPARTMENT", "2"))
SANDBOX_TASK_DB_PATH = _resolve_storage_path(
    os.getenv("SANDBOX_TASK_DB_PATH"), "sandbox_tasks.db"
)
SANDBOX_ARTIFACT_STORAGE_DIR = _resolve_storage_path(
    os.getenv("SANDBOX_ARTIFACT_STORAGE_DIR"), "sandbox_artifacts"
)
SANDBOX_SKILL_STORAGE_DIR = _resolve_storage_path(
    os.getenv("SANDBOX_SKILL_STORAGE_DIR"), "sandbox_skills"
)
SANDBOX_SKILL_PIP_ALLOWED_HOSTS = os.getenv("SANDBOX_SKILL_PIP_ALLOWED_HOSTS", "pypi.org")
SANDBOX_ARTIFACT_ALLOWED_EXTENSIONS = os.getenv(
    "SANDBOX_ARTIFACT_ALLOWED_EXTENSIONS", "json,csv,txt,zip,kicad_sch,kicad_pcb,kicad_pro"
)
# Text Skills are a separate, prompt-data-only catalog.  They have no runtime
# capability, so the catalog is on by default; nothing reaches a conversation
# until an admin enables reviewed content and an employee subscribes.  Sandbox
# execution still requires SANDBOX_ENABLED plus a validated Runner.
SKILLS_ENABLED = _env_bool("SKILLS_ENABLED", True)

# P0b shared retrieval executor: one bounded pool per process for document
# batch retrieval; per-request fan-out and the RAG governor are separate.
# Built-in optimization: always use the bounded shared pool.  Tests may still
# override the module attribute to exercise the legacy fallback.
SHARED_RETRIEVAL_EXECUTOR_ENABLED = True
# R3: independent rollout switch; scope/API acceptance is still required.
# The raw multi-select UI was removed in 2026-10.  The backend scope/retrieval
# path is intentionally retained as the base for a future project-space mount;
# keep this false until that surface is designed and accepted.
MULTI_KB_CHAT_ENABLED = _env_bool("MULTI_KB_CHAT_ENABLED", False)
# Internal field-contract switch; tests may override the module attribute.
MULTI_KB_EXPLICIT_FIELDS_ENABLED = False
MULTI_KB_HUMAN_REVIEW_ENABLED = _env_bool("MULTI_KB_HUMAN_REVIEW_ENABLED", False)
MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED = _env_bool("MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED", False)
# Security invariant: critical source conflicts always require approval.
MULTI_KB_REVIEW_REQUIRE_APPROVAL_FOR_CRITICAL = True
MULTI_KB_MAX_SOURCES = int(os.getenv("MULTI_KB_MAX_SOURCES", "4"))
MULTI_KB_PER_SOURCE_TOP_K = int(os.getenv("MULTI_KB_PER_SOURCE_TOP_K", "8"))
MULTI_KB_PER_RECORD_CANDIDATES = int(os.getenv("MULTI_KB_PER_RECORD_CANDIDATES", "8"))
MULTI_KB_PER_SOURCE_CANDIDATES = int(os.getenv("MULTI_KB_PER_SOURCE_CANDIDATES", "8"))
MULTI_KB_MAX_CALLS_PER_TURN = int(os.getenv("MULTI_KB_MAX_CALLS_PER_TURN", "24"))
MULTI_KB_RETRIEVAL_DEADLINE_SECONDS = float(os.getenv("MULTI_KB_RETRIEVAL_DEADLINE_SECONDS", "30"))
MULTI_KB_EVIDENCE_MAX_ITEMS = int(os.getenv("MULTI_KB_EVIDENCE_MAX_ITEMS", "24"))
MULTI_KB_CONTEXT_MAX_TOKENS = int(os.getenv("MULTI_KB_CONTEXT_MAX_TOKENS", "8000"))
# Source-aware evidence settings are kept compact to stay inside this file's
# architecture line budget; parsing remains centralized here.
_EVIDENCE_DEFAULTS = dict(zip(
    "EVIDENCE_SOURCE_AWARE_ENABLED "
    "EVIDENCE_GLOBAL_CONTEXT_MAX_BYTES EVIDENCE_SYSTEM_PROMPT_RESERVE_BYTES EVIDENCE_ANSWER_RESERVE_BYTES "
    "EVIDENCE_ATTACHMENT_MAX_BYTES EVIDENCE_KNOWLEDGE_BASE_MAX_BYTES "
    "EVIDENCE_ATTACHMENT_REQUIRED_RESERVE_BYTES EVIDENCE_KNOWLEDGE_BASE_REQUIRED_RESERVE_BYTES".split(),
    (False, 32000, 4000, 6000, 12000, 16000, 4000, 4000),
))
_EVIDENCE_BOOL_KEYS = frozenset("EVIDENCE_SOURCE_AWARE_ENABLED".split())
EVIDENCE_INT_SETTING_KEYS = frozenset(set(_EVIDENCE_DEFAULTS) - _EVIDENCE_BOOL_KEYS)
EVIDENCE_POSITIVE_SETTING_KEYS = EVIDENCE_INT_SETTING_KEYS


def _load_evidence_settings() -> dict[str, object]:
    return {**{k: _env_bool(k, bool(v)) for k, v in _EVIDENCE_DEFAULTS.items() if k in _EVIDENCE_BOOL_KEYS},
            **{k: int(os.getenv(k, str(v)) or v) for k, v in _EVIDENCE_DEFAULTS.items() if k not in _EVIDENCE_BOOL_KEYS}}
globals().update(_load_evidence_settings())
def _multi_kb_byte_limit(name: str) -> int:
    """Resolve a byte limit, inheriting the legacy token-named value when blank."""
    raw = os.getenv(name, "").strip()
    if raw:
        value = int(raw)
        if value <= 0:
            raise ValueError(f"{name} must be positive")
        return value
    return int(os.getenv("MULTI_KB_CONTEXT_MAX_TOKENS", "8000"))


MULTI_KB_FUSION_MAX_BYTES = _multi_kb_byte_limit("MULTI_KB_FUSION_MAX_BYTES")
MULTI_KB_TURN_EVIDENCE_MAX_BYTES = _multi_kb_byte_limit("MULTI_KB_TURN_EVIDENCE_MAX_BYTES")
RETRIEVAL_EXECUTOR_MAX_WORKERS = int(os.getenv("RETRIEVAL_EXECUTOR_MAX_WORKERS", "8"))
RETRIEVAL_QUEUE_MAX_DEPTH = int(os.getenv("RETRIEVAL_QUEUE_MAX_DEPTH", "256"))
RETRIEVAL_PER_REQUEST_FANOUT = int(os.getenv("RETRIEVAL_PER_REQUEST_FANOUT", "4"))
RETRIEVAL_QUEUE_MAX_WAIT_SECONDS = float(os.getenv("RETRIEVAL_QUEUE_MAX_WAIT_SECONDS", "30"))
# P0-007: bounded drain before process exit; unconfirmed calls stay leased
# (uncertain after lease expiry) instead of clearing quotas.
RETRIEVAL_SHUTDOWN_DEADLINE_SECONDS = float(
    os.getenv("RETRIEVAL_SHUTDOWN_DEADLINE_SECONDS", "10")
)

# P2 action DAG bounded parallelism is an internal execution choice.  Keep it
# as a module constant so tests can exercise both paths without an env surface.

# P3 optional specialist subagents are internal evaluation switches.  They are
# not deployment features; tests may override the module attributes.
REVIEWER_AGENT_ENABLED = False
RESEARCH_AGENT_ENABLED = False

# P0b admission control: per-user/per-tenant RPM before accepting a request.
ADMISSION_CONTROL_ENABLED = _env_bool("ADMISSION_CONTROL_ENABLED", True)
ADMISSION_CHAT_RPM_PER_USER = int(os.getenv("ADMISSION_CHAT_RPM_PER_USER", "30"))
ADMISSION_CHAT_RPM_PER_TENANT = int(os.getenv("ADMISSION_CHAT_RPM_PER_TENANT", "120"))
ADMISSION_HEAVY_RPM_PER_USER = int(os.getenv("ADMISSION_HEAVY_RPM_PER_USER", "10"))
ADMISSION_HEAVY_RPM_PER_TENANT = int(os.getenv("ADMISSION_HEAVY_RPM_PER_TENANT", "40"))
ADMISSION_RETRY_AFTER_SECONDS = float(os.getenv("ADMISSION_RETRY_AFTER_SECONDS", "5"))
# Per resource-type RPM (plan §4.5).  Empty means "inherit" the base bucket
# (deep -> chat, document/export/ocr/evaluation -> heavy), so deployments can
# tighten one dimension without redefining all of them.
ADMISSION_DEEP_RPM_PER_USER = os.getenv("ADMISSION_DEEP_RPM_PER_USER", "")
ADMISSION_DEEP_RPM_PER_TENANT = os.getenv("ADMISSION_DEEP_RPM_PER_TENANT", "")
ADMISSION_EXPORT_RPM_PER_USER = os.getenv("ADMISSION_EXPORT_RPM_PER_USER", "")
ADMISSION_EXPORT_RPM_PER_TENANT = os.getenv("ADMISSION_EXPORT_RPM_PER_TENANT", "")
ADMISSION_OCR_RPM_PER_USER = os.getenv("ADMISSION_OCR_RPM_PER_USER", "")
ADMISSION_OCR_RPM_PER_TENANT = os.getenv("ADMISSION_OCR_RPM_PER_TENANT", "")
ADMISSION_EVALUATION_RPM_PER_USER = os.getenv("ADMISSION_EVALUATION_RPM_PER_USER", "")
ADMISSION_EVALUATION_RPM_PER_TENANT = os.getenv("ADMISSION_EVALUATION_RPM_PER_TENANT", "")
# Optional per-model dimension (plan §4.5); empty disables the model bucket.
ADMISSION_MODEL_RPM_PER_USER = os.getenv("ADMISSION_MODEL_RPM_PER_USER", "")
ADMISSION_MODEL_RPM_PER_TENANT = os.getenv("ADMISSION_MODEL_RPM_PER_TENANT", "")
# P0-006: bounded unterminal work per session/user; requests replayed with the
# same client_request_id return the original turn and never count twice.
ADMISSION_MAX_UNTERMINAL_TURNS_PER_SESSION = int(
    os.getenv("ADMISSION_MAX_UNTERMINAL_TURNS_PER_SESSION", "8")
)
ADMISSION_MAX_UNTERMINAL_TURNS_PER_USER = int(
    os.getenv("ADMISSION_MAX_UNTERMINAL_TURNS_PER_USER", "32")
)

RESULT_EXPORT_MAX_BYTES = int(os.getenv("RESULT_EXPORT_MAX_BYTES", str(25 * 1024 * 1024)))
RESULT_EXPORT_JOB_LEASE_SECONDS = int(os.getenv("RESULT_EXPORT_JOB_LEASE_SECONDS", "300"))
RESULT_EXPORT_JOB_BATCH_SIZE = int(os.getenv("RESULT_EXPORT_JOB_BATCH_SIZE", "8"))
RESULT_EXPORT_RETENTION_DAYS = int(os.getenv("RESULT_EXPORT_RETENTION_DAYS", "30"))
RESULT_EXPORT_MAX_RUNNING_JOBS = int(os.getenv("RESULT_EXPORT_MAX_RUNNING_JOBS", "4"))
# Character cap for the draft answer sent to the organizer model.
RESULT_EXPORT_ORGANIZE_MAX_CHARS = int(os.getenv("RESULT_EXPORT_ORGANIZE_MAX_CHARS", "40000"))
CHAT_TURN_HEARTBEAT_TTL_SECONDS = int(os.getenv("CHAT_TURN_HEARTBEAT_TTL_SECONDS", "90"))
CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS", "10"))
# P0a: strict per-session turn ordering with execution ownership.  Rollout
# flag: off by default; enable after P0a acceptance to activate serialization.
CONVERSATION_SERIALIZE_SAME_THREAD = os.getenv(
    "CONVERSATION_SERIALIZE_SAME_THREAD", "false"
).lower() in {"1", "true", "yes", "on"}
CHAT_TURN_LEASE_SECONDS = int(os.getenv("CHAT_TURN_LEASE_SECONDS", "120"))
CHAT_TURN_LEASE_RENEW_SECONDS = int(os.getenv("CHAT_TURN_LEASE_RENEW_SECONDS", "15"))
# P0a: a turn waiting for attachment parsing blocks its session for at most
# this many seconds before it fails and the next turn is released.
WAITING_TURN_MAX_SECONDS = int(os.getenv("WAITING_TURN_MAX_SECONDS", "600"))
WAITING_TURN_TIMEOUT_SCAN_SECONDS = float(os.getenv("WAITING_TURN_TIMEOUT_SCAN_SECONDS", "5"))
# Upload endpoints pass bytes to parsers, so bound both the request read and the
# downstream parser's maximum working set.
EVALUATION_DATASET_MAX_BYTES = int(os.getenv("EVALUATION_DATASET_MAX_BYTES", str(100 * 1024 * 1024)))

# Chat attachments (会话附件): session-private, temporary, locally parsed data
# sources. See docs/superpowers/specs/2026-09-03-local-chat-attachment-routing-design.md.
# Enabled by default: deployments can still opt out with an explicit false.
# The flag gates the attachment API/toolset; existing KB and template behavior
# remains available when it is disabled.
CHAT_ATTACHMENTS_ENABLED = os.getenv("CHAT_ATTACHMENTS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
CHAT_ATTACHMENT_STORAGE_DIR = _resolve_storage_path(
    os.getenv("CHAT_ATTACHMENT_STORAGE_DIR"), "chat_attachments"
)
CHAT_ATTACHMENT_INDEX_DB_PATH = _resolve_storage_path(
    os.getenv("CHAT_ATTACHMENT_INDEX_DB_PATH"), "chat_attachments.db"
)
CHAT_ATTACHMENT_MAX_BYTES = int(os.getenv("CHAT_ATTACHMENT_MAX_BYTES", str(50 * 1024 * 1024)))
CHAT_ATTACHMENT_MAX_FILES_PER_SESSION = int(os.getenv("CHAT_ATTACHMENT_MAX_FILES_PER_SESSION", "20"))
CHAT_ATTACHMENT_MAX_SESSION_BYTES = int(os.getenv("CHAT_ATTACHMENT_MAX_SESSION_BYTES", str(500 * 1024 * 1024)))
CHAT_ATTACHMENT_MAX_TENANT_BYTES = int(os.getenv("CHAT_ATTACHMENT_MAX_TENANT_BYTES", str(5 * 1024 * 1024 * 1024)))
CHAT_ATTACHMENT_MAX_PAGES = int(os.getenv("CHAT_ATTACHMENT_MAX_PAGES", "300"))
CHAT_ATTACHMENT_MAX_ROWS = int(os.getenv("CHAT_ATTACHMENT_MAX_ROWS", "200000"))
CHAT_ATTACHMENT_MAX_CELLS = int(os.getenv("CHAT_ATTACHMENT_MAX_CELLS", "2000000"))
CHAT_ATTACHMENT_MAX_SHEETS = int(os.getenv("CHAT_ATTACHMENT_MAX_SHEETS", "100"))
CHAT_ATTACHMENT_MAX_SHARED_STRINGS = int(os.getenv("CHAT_ATTACHMENT_MAX_SHARED_STRINGS", "1000000"))
CHAT_ATTACHMENT_MAX_CELL_TEXT_LENGTH = int(os.getenv("CHAT_ATTACHMENT_MAX_CELL_TEXT_LENGTH", "100000"))
CHAT_ATTACHMENT_MAX_UNCOMPRESSED_BYTES = int(os.getenv("CHAT_ATTACHMENT_MAX_UNCOMPRESSED_BYTES", str(200 * 1024 * 1024)))
CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS = float(os.getenv("CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS", "120"))
CHAT_ATTACHMENT_RETENTION_SECONDS = int(os.getenv("CHAT_ATTACHMENT_RETENTION_SECONDS", str(30 * 24 * 3600)))
CHAT_ATTACHMENT_CONTEXT_MAX_TOKENS = int(os.getenv("CHAT_ATTACHMENT_CONTEXT_MAX_TOKENS", "8000"))
CHAT_ATTACHMENT_CONTEXT_RATIO = float(os.getenv("CHAT_ATTACHMENT_CONTEXT_RATIO", "0.3"))
CHAT_ATTACHMENT_FTS_ENABLED = os.getenv("CHAT_ATTACHMENT_FTS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
CHAT_ATTACHMENT_TRIGRAM_ENABLED = os.getenv("CHAT_ATTACHMENT_TRIGRAM_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
CHAT_ATTACHMENT_DENSE_ENABLED = os.getenv("CHAT_ATTACHMENT_DENSE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
CHAT_ATTACHMENT_RETRIEVAL_MODE = os.getenv("CHAT_ATTACHMENT_RETRIEVAL_MODE", "lexical").strip().lower()
CHAT_ATTACHMENT_EMBEDDING_PROVIDER = os.getenv("CHAT_ATTACHMENT_EMBEDDING_PROVIDER", "openai_compatible").strip()
CHAT_ATTACHMENT_EMBEDDING_BASE_URL = os.getenv("CHAT_ATTACHMENT_EMBEDDING_BASE_URL", "").strip()
CHAT_ATTACHMENT_EMBEDDING_MODEL = os.getenv("CHAT_ATTACHMENT_EMBEDDING_MODEL", "").strip()
CHAT_ATTACHMENT_EMBEDDING_API_KEY = os.getenv("CHAT_ATTACHMENT_EMBEDDING_API_KEY", "").strip()
CHAT_ATTACHMENT_DENSE_MAX_PARTS = int(os.getenv("CHAT_ATTACHMENT_DENSE_MAX_PARTS", "4000"))
CHAT_ATTACHMENT_OCR_ENABLED = os.getenv("CHAT_ATTACHMENT_OCR_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
CHAT_ATTACHMENT_OCR_MAX_PAGES = int(os.getenv("CHAT_ATTACHMENT_OCR_MAX_PAGES", "10"))
CHAT_ATTACHMENT_VISUAL_ENABLED = os.getenv("CHAT_ATTACHMENT_VISUAL_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED = os.getenv("CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED", "false").lower() in {"1", "true", "yes", "on"}
CHAT_ATTACHMENT_VISUAL_PROVIDER = os.getenv("CHAT_ATTACHMENT_VISUAL_PROVIDER", "volcengine_ark").strip()
CHAT_ATTACHMENT_VISUAL_BASE_URL = os.getenv(
    "CHAT_ATTACHMENT_VISUAL_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
).strip()
CHAT_ATTACHMENT_VISUAL_MODEL = os.getenv("CHAT_ATTACHMENT_VISUAL_MODEL", "").strip()
CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN = int(
    os.getenv("CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN", "3")
)
CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES = int(
    os.getenv("CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES", str(8 * 1024 * 1024))
)
CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS = int(
    os.getenv("CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS", "60")
)
ARK_API_KEY = os.getenv("ARK_API_KEY", "").strip()
CHAT_ATTACHMENT_JOB_LEASE_SECONDS = int(os.getenv("CHAT_ATTACHMENT_JOB_LEASE_SECONDS", "300"))
CHAT_ATTACHMENT_JOB_MAX_ATTEMPTS = int(os.getenv("CHAT_ATTACHMENT_JOB_MAX_ATTEMPTS", "3"))

# Cross-cutting observability configuration.  These settings deliberately live
# here, alongside the rest of the application's single source of truth, so the
# API, worker, Streamlit process and evaluation threads cannot drift apart.
OBS_ENABLED = os.getenv("OBS_ENABLED", "true").lower() == "true"
OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "hardware-database-api")
# Host processes use the published local Collector port.  Docker production
# overrides this with the same-network service name in
# docker-compose.observability.yml.
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
OBS_ENVIRONMENT = os.getenv("OBS_ENVIRONMENT", "development")
OBS_SERVICE_VERSION = os.getenv("OBS_SERVICE_VERSION", "0.1.0")
OBS_TRACE_SAMPLE_RATIO = float(os.getenv("OBS_TRACE_SAMPLE_RATIO", "1.0"))
OBS_CAPTURE_CONTENT = os.getenv("OBS_CAPTURE_CONTENT", "false").lower() == "true"
OBS_CAPTURE_QUERY = os.getenv("OBS_CAPTURE_QUERY", "false").lower() == "true"
OBS_CAPTURE_EVIDENCE = os.getenv("OBS_CAPTURE_EVIDENCE", "false").lower() == "true"
OBS_CAPTURE_LLM_CONTENT = os.getenv("OBS_CAPTURE_LLM_CONTENT", "false").lower() == "true"
# MCP request/response bodies are opt-in on top of the per-kind gates; the
# executor still redacts credentials and truncates to OBS_CONTENT_MAX_CHARS.
OBS_CAPTURE_MCP_CONTENT = os.getenv("OBS_CAPTURE_MCP_CONTENT", "false").lower() == "true"
OBS_CONTENT_MAX_CHARS = max(1000, int(os.getenv("OBS_CONTENT_MAX_CHARS", "50000")))
OBS_LOG_FORMAT = os.getenv("OBS_LOG_FORMAT", "json")
OBS_METRICS_ENABLED = os.getenv("OBS_METRICS_ENABLED", "true").lower() == "true"
OBS_TRACES_ENABLED = os.getenv("OBS_TRACES_ENABLED", "true").lower() == "true"
OBS_LOGS_ENABLED = os.getenv("OBS_LOGS_ENABLED", "true").lower() == "true"
OBS_PHOENIX_PROJECT = os.getenv("OBS_PHOENIX_PROJECT", "hardware-database")
OBS_GRAFANA_BASE_URL = os.getenv("OBS_GRAFANA_BASE_URL", "")
OBS_PHOENIX_BASE_URL = os.getenv("OBS_PHOENIX_BASE_URL", "")
OBS_WORKER_HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("OBS_WORKER_HEARTBEAT_INTERVAL_SECONDS", "10"))
OBS_WORKER_STALE_SECONDS = int(os.getenv("OBS_WORKER_STALE_SECONDS", "30"))
OBS_DEPENDENCY_TIMEOUT_SECONDS = float(os.getenv("OBS_DEPENDENCY_TIMEOUT_SECONDS", "5"))
# /health/ready 的结果缓存秒数（0=关闭）。K8s/负载均衡每隔几秒探一次，
# 无缓存时每次探针都会真查 SQLite + PG + storage，放大依赖负载；
# 1 秒既能把探针风暴收敛成一次真实检查，又能在 ~1 秒内暴露真实故障。
OBS_READINESS_CACHE_SECONDS = float(os.getenv("OBS_READINESS_CACHE_SECONDS", "1"))

# Optional business/domain addendum.  The built-in workspace-agent behavior
# lives in ``src/agents/turn_prompt.py`` and is not editable from the UI.
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "")

NO_CONTEXT_PROMPT = os.getenv(
    "NO_CONTEXT_PROMPT",
    "知识库中没有找到相关上下文，请说明这一点；如需基于通用知识回答，必须明确标注。"
)


def ensure_runtime_dirs():
    """Ensure directories required by RAGFlow and pipeline archives exist."""
    os.makedirs(STORAGE_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(PIPELINE_ARCHIVE_ROOT, exist_ok=True)
    os.makedirs(RAGFLOW_FILE_ROOT, exist_ok=True)


ensure_runtime_dirs()


# v3 §11.2: settings that only take effect after a process restart (storage
# backends, DSNs, role/port and other construction-time wiring).  Everything
# else is reloaded at task boundaries.  Readiness reports both the live config
# revision and this list so operators can tell hot reload from restart.
RESTART_REQUIRED_SETTINGS = frozenset(
    {
        "STORAGE_DIR",
        "AUTH_DB_PATH",
        "CONTROL_PLANE_BACKEND",
        "CONTROL_PLANE_DATABASE_URL",
        "CONTROL_PLANE_POOL_SIZE",
        "EVALUATION_STORE_BACKEND",
        "EVALUATION_STORE_DATABASE_URL",
        "EVAL_EXECUTION_MODE",
        "PIPELINE_DOCUMENT_STORE_BACKEND",
        "PIPELINE_DOCUMENT_DATABASE_URL",
        "AUTH_STORE_BACKEND",
        "AUTH_STORE_DATABASE_URL",
        "REDIS_URL",
        "RESOURCE_GOVERNOR_BACKEND",
        "RESOURCE_GOVERNOR_DATABASE_URL",
        "RESOURCE_GOVERNOR_SCOPE",
        "AGENT_CHECKPOINT_BACKEND",
        "AGENT_CHECKPOINT_DATABASE_URL",
        "AGENT_CHECKPOINT_DB_PATH",
        "AGENT_CHECKPOINT_POOL_SIZE",
        "MEMORY_STORE_BACKEND",
        "MEMORY_DATABASE_URL",
        "MEMORY_SQLITE_PATH",
        "WORKER_ROLE",
        "HDB_API_HOST",
        "HDB_API_PORT",
        "OBS_ENABLED",
        "OTEL_SERVICE_NAME",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OBS_ENVIRONMENT",
        "OBS_SERVICE_VERSION",
        "OBS_TRACE_SAMPLE_RATIO",
        "OBS_METRICS_ENABLED",
        "OBS_TRACES_ENABLED",
        "OBS_LOGS_ENABLED",
        "OBS_PHOENIX_PROJECT",
        # Environment-only MCP credential key: rotation requires a restart and
        # must never be writable through the config API.
        "MCP_CREDENTIALS_KEY",
    }
)


DEFAULT_VALUES = {
    "RAGFLOW_BASE_URL": "http://localhost:9380",
    "RAGFLOW_API_KEY": "",
    "RAGFLOW_GOVERNANCE_DATASET_NAME": "department_governance",
    "RAGFLOW_DESIGN_DATASET_NAME": "project_design_assets",
    "RAGFLOW_TIMEOUT_SECONDS": "120",
    "RAGFLOW_CHUNK_LIST_PAGE_SIZE": "30",
    "RAGFLOW_CHUNK_LIST_MAX_PAGES": "100",
    "RAGFLOW_CHUNK_LIST_MAX_BYTES": str(8 * 1024 * 1024),
    "RAGFLOW_STATUS_REFRESH_MIN_SECONDS": "60",
    "RAGFLOW_SIMILARITY_THRESHOLD": "0.25",
    "RAGFLOW_VECTOR_WEIGHT": "0.4",
    "RAGFLOW_RETRIEVAL_DOC_SCOPE_ENABLED": "true",
    "RAGFLOW_RETRIEVAL_DOC_IDS_MAX": "2000",
    "RAGFLOW_METADATA_WRITE_BEST_EFFORT": "true",
    "PIPELINE_ARCHIVE_ROOT": os.path.join(STORAGE_DIR, "pipeline_archives"),
    "RAGFLOW_FILE_ROOT": os.path.join(STORAGE_DIR, "pipeline_archives"),
    "RESULT_EXPORT_STORAGE_DIR": os.path.join(STORAGE_DIR, "exports"),
    "RESULT_EXPORT_ENABLED": "true",
    "RESULT_EXPORT_MD_ENABLED": "true",
    "RESULT_EXPORT_XLSX_ENABLED": "true",
    "RESULT_EXPORT_DOCX_ENABLED": "true",
    "RESULT_EXPORT_PDF_ENABLED": "true",
    "RESULT_EXPORT_PPTX_ENABLED": "true",
    "RESULT_EXPORT_ORGANIZE_ENABLED": "true",
    "AUTH_DB_PATH": os.path.join(STORAGE_DIR, "auth.db"),
    "AGENT_CHECKPOINT_DB_PATH": os.path.join(STORAGE_DIR, "agent_checkpoints.db"),
    "AGENT_CHECKPOINT_BACKEND": "sqlite",
    "AGENT_CHECKPOINT_DATABASE_URL": "",
    "AGENT_CHECKPOINT_POOL_SIZE": "4",
    "AUTH_DEFAULT_ADMIN_USERNAME": "admin",
    "AUTH_DEFAULT_ADMIN_PASSWORD": "",
    "AUTH_SESSION_TTL_HOURS": "24",
    "MCP_TOKEN_TTL_DAYS": "365",
    "MCP_SESSION_TTL_HOURS": "24",
    "AGENT_LLM_PROVIDER": "ollama",
    "AGENT_OLLAMA_BASE_URL": "http://localhost:11434",
    "AGENT_OLLAMA_MODEL": "qwen2.5:32b",
    "AGENT_CUSTOM_API_KEY": "",
    "AGENT_CUSTOM_BASE_URL": "",
    "AGENT_CUSTOM_MODEL": "",
    "AGENT_CUSTOM_MAX_TOKENS": "4096",
    "AGENT_REASONING_EFFORT": "",
    "AGENT_MODEL_MAX_INPUT_TOKENS": "65536",
    "AGENT_TEMPERATURE": "0.2",
    "AGENT_TIMEOUT_SECONDS": "120",
    "AGENT_RATE_LIMIT_MAX_RETRIES": "4",
    "AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS": "1",
    "AGENT_RATE_LIMIT_MAX_DELAY_SECONDS": "16",
    "LLM_MAX_CONCURRENCY": "4",
    "LLM_BATCH_MAX_CONCURRENCY": "1",
    "LLM_QUEUE_TIMEOUT_SECONDS": "180",
    "LLM_QUEUE_WARN_SECONDS": "10",
    "LLM_STREAM_IDLE_TIMEOUT_SECONDS": "0",
    "MEMORY_ENABLED": "true",
    "MEMORY_STORE_BACKEND": "sqlite",
    "MEMORY_DATABASE_URL": "",
    "MEMORY_SQLITE_PATH": os.path.join(STORAGE_DIR, "memory.db"),
    "MEMORY_SQLITE_BUSY_TIMEOUT_MS": "30000",
    "MEMORY_SINGLE_WRITER": "true",
    "MEMORY_EXTRACTION_ENABLED": "true",
    "MEMORY_DEBOUNCE_SECONDS": "300",
    "MEMORY_JOB_LEASE_SECONDS": "180",
    "MEMORY_JOB_MAX_RETRIES": "5",
    "MEMORY_REFLECTION_TIMEOUT_SECONDS": "120",
    "MEMORY_REFLECTION_MIN_INTERVAL_SECONDS": "0",
    "MEMORY_REFLECTION_CIRCUIT_FAILURES": "5",
    "MEMORY_REFLECTION_CIRCUIT_COOLDOWN_SECONDS": "60",
    "MEMORY_PROJECTION_MAX_RETRIES": "5",
    "MEMORY_RECONCILE_INTERVAL_SECONDS": "3600",
    "MEMORY_USER_TOP_K": "3",
    "MEMORY_PROJECT_TOP_K": "5",
    "MEMORY_STORE_OVERSAMPLE_FACTOR": "4",
    "MEMORY_STORE_MAX_SCAN": "100",
    "MEMORY_CONTEXT_MAX_TOKENS": "1800",
    "MEMORY_ITEM_MAX_TOKENS": "350",
    "MEMORY_MIN_SCORE_ENABLED": "false",
    "MEMORY_MIN_SCORE": "",
    "MEMORY_MODEL_PROVIDER": "",
    "MEMORY_MODEL": "",
    "MEMORY_MODEL_BASE_URL": "",
    "MEMORY_MODEL_API_KEY": "",
    "MEMORY_MODEL_REQUIRE_STRUCTURED_TOOLS": "true",
    "MEMORY_EMBEDDING_PROVIDER": "",
    "MEMORY_EMBEDDING_MODEL": "",
    "MEMORY_EMBEDDING_BASE_URL": "",
    "MEMORY_EMBEDDING_API_KEY": "",
    "MEMORY_EMBEDDING_DIMS": "",
    "MEMORY_EMBEDDING_INDEX_KIND": "",
    "MEMORY_INDEX_FIELDS": "content.content,content.title,content.subject",
    "MEMORY_ALLOW_GENERAL_CHAT_EXTRACTION": "false",
    "MEMORY_USER_MEMORY_OPT_IN": "false",
    "MEMORY_USER_MEMORY_REQUIRE_PER_EVENT_CONSENT": "true",
    "MEMORY_RETENTION_DAYS": "",
    "FINAL_TOP_K": "5",
    "AGENT_MAX_RETRIEVAL_ROUNDS": "3",
    "AGENT_DEEP_TOOL_CALL_CAP": "32",
    "AGENT_DEEP_RETRIEVAL_CALL_CAP": "20",
    "AGENT_DEEP_FINALIZE_RESERVE": "4",
    "AGENT_DEEP_LOOP_SECONDS": "360",
    "WORKER_POLL_INTERVAL_SECONDS": "0.5",
    "WORKER_ROLE": "all",
    "RESOURCE_GOVERNOR_ENABLED": "true",
    "RESOURCE_GOVERNOR_SCOPE": "process",
    "RESOURCE_GOVERNOR_DB_PATH": os.path.join(STORAGE_DIR, "resource_quotas.db"),
    "RESOURCE_GOVERNOR_BACKEND": "sqlite",
    "RESOURCE_GOVERNOR_DATABASE_URL": "",
    "RESOURCE_QUEUE_MAX_WAIT_SECONDS": "30",
    "RESOURCE_QUEUE_MAX_DEPTH": "256",
    "RESOURCE_PERMIT_LEASE_SECONDS": "300",
    "RESOURCE_UNCERTAIN_ESCALATE_SECONDS": "600",
    "RESOURCE_GOVERNOR_MAINTENANCE_SECONDS": "5",
    "RESOURCE_GOVERNOR_DEPLOYMENT_ID": "local",
    "RESOURCE_GOVERNOR_REQUIRE_PRECONFIGURED": "false",
    "CONTROL_PLANE_BACKEND": "sqlite",
    "CONTROL_PLANE_DATABASE_URL": "",
    "EVALUATION_STORE_BACKEND": "",
    "EVALUATION_STORE_DATABASE_URL": "",
    "EVAL_EXECUTION_MODE": "inline",
    "QUALITY_ENABLED": "true",
    "QUALITY_CAPTURE_CONTENT": "true",
    "QUALITY_SAMPLE_RATE": "0.01",
    "QUALITY_SAMPLE_DAILY_LIMIT": "100",
    "QUALITY_PHOENIX_SYNC_ENABLED": "false",
    "QUALITY_PHOENIX_ENDPOINT": "",
    "QUALITY_PHOENIX_API_KEY": "",
    "QUALITY_PHOENIX_MAX_ATTEMPTS": "20",
    "COST_LEDGER_ENABLED": "true",
    "COST_MODEL_REGISTRY": "{}",
    "PIPELINE_DOCUMENT_STORE_BACKEND": "",
    "PIPELINE_DOCUMENT_DATABASE_URL": "",
    "AUTH_STORE_BACKEND": "sqlite",
    "AUTH_STORE_DATABASE_URL": "",
    "CONTROL_PLANE_POOL_SIZE": "8",
    "REDIS_URL": "",
    "CANCEL_KEY_TTL_SECONDS": "7200",
    "TURN_EVENT_STREAM_TTL_SECONDS": "7200",
    "TURN_EVENT_STREAM_MAXLEN": "5000",
    "DISTRIBUTED_ADMISSION_ENABLED": "false",
    "DISTRIBUTED_ADMISSION_TTL_SECONDS": "120",
    "PROVIDER_RPM_LIMIT": "0",
    "PROVIDER_TPM_LIMIT": "0",
    "CHAT_QUEUE_DEADLINE_SECONDS": "600",
    "CHAT_QUEUE_DEADLINE_DEEP_SECONDS": "480",
    "RESOURCE_LLM_MAX_CONCURRENT": "4",
    "RESOURCE_LLM_RESERVED_INTERACTIVE": "2",
    "RESOURCE_RAG_MAX_CONCURRENT": "8",
    "RESOURCE_RAG_RESERVED_INTERACTIVE": "4",
    "RESOURCE_TOOL_MAX_CONCURRENT": "16",
    "RESOURCE_TOOL_RESERVED_INTERACTIVE": "0",
    "RESOURCE_CHAT_MAX_CONCURRENT": "4",
    "RESOURCE_CHAT_RESERVED_INTERACTIVE": "0",
    "RESOURCE_HEAVY_JOB_MAX_CONCURRENT": "4",
    "RESOURCE_HEAVY_JOB_RESERVED_INTERACTIVE": "1",
    "RESOURCE_OCR_MAX_CONCURRENT": "1",
    "RESOURCE_OCR_RESERVED_INTERACTIVE": "0",
    "RESOURCE_EXPORT_MAX_CONCURRENT": "4",
    "RESOURCE_EXPORT_RESERVED_INTERACTIVE": "0",
    "RESOURCE_MEMORY_MAX_CONCURRENT": "1",
    "RESOURCE_MEMORY_RESERVED_INTERACTIVE": "0",
    "MCP_ENABLED": "true",
    "MCP_ALLOW_INSECURE_HTTP": "false",
    "MCP_ALLOW_PRIVATE_HOSTS": "false",
    "MCP_CONNECT_TIMEOUT_SECONDS": "15",
    "MCP_CALL_TIMEOUT_SECONDS": "60",
    "MCP_SSE_READ_TIMEOUT_SECONDS": "300",
    "MCP_MAX_RESULT_CHARS": "20000",
    "MCP_MAX_RESULT_BYTES": "65536",
    "MCP_MAX_ARGUMENT_CHARS": "12000",
    "MCP_MAX_TOOLS_PER_SERVER": "50",
    "MCP_MAX_SERVERS_PER_RUN": "5",
    "MCP_CALLS_PER_TURN": "8",
    "MCP_FAILURE_COOLDOWN_SECONDS": "60",
    "MCP_WRITE_ENABLED": "false",
    "MCP_WRITE_CONFIRM_TTL_SECONDS": "1800",
    "MCP_MAX_ARTIFACT_BYTES": str(50 * 1024 * 1024),
    "MCP_ARTIFACT_ALLOWED_EXTENSIONS": "kicad_sch,kicad_pcb,kicad_pro,drawio,zip,json,csv",
    "MCP_ARTIFACT_STORAGE_DIR": os.path.join(STORAGE_DIR, "mcp_artifacts"),
    "MCP_WRITE_MAX_ATTEMPTS": "3",
    "RESOURCE_MCP_MAX_CONCURRENT": "4",
    "RESOURCE_MCP_RESERVED_INTERACTIVE": "1",
    "SANDBOX_ENABLED": "false",
    "SANDBOX_GATEWAY_ENABLED": "false",
    "SANDBOX_DEFAULT_NETWORK": "none",
    "SANDBOX_MAX_CONCURRENT": "2",
    "SANDBOX_MAX_CONCURRENT_PER_USER": "1",
    "SANDBOX_MAX_CONCURRENT_PER_DEPARTMENT": "2",
    "SANDBOX_TASK_TIMEOUT_SECONDS": "300",
    "SANDBOX_TASK_LEASE_SECONDS": "60",
    "SANDBOX_MAX_OUTPUT_BYTES": str(50 * 1024 * 1024),
    "SANDBOX_MAX_INPUT_BYTES": str(50 * 1024 * 1024),
    "SKILLS_ENABLED": "true",
    # Deployment-only sandbox identity/endpoints are intentionally excluded
    # from the ordinary config API; these defaults document the closed state.
    "MULTI_KB_CHAT_ENABLED": "false",
    "MULTI_KB_HUMAN_REVIEW_ENABLED": "false",
    "MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED": "false",
    "MULTI_KB_MAX_SOURCES": "4",
    "MULTI_KB_PER_SOURCE_TOP_K": "8",
    "MULTI_KB_PER_RECORD_CANDIDATES": "8",
    "MULTI_KB_PER_SOURCE_CANDIDATES": "8",
    "MULTI_KB_MAX_CALLS_PER_TURN": "24",
    "MULTI_KB_RETRIEVAL_DEADLINE_SECONDS": "30",
    "MULTI_KB_EVIDENCE_MAX_ITEMS": "24",
    "MULTI_KB_CONTEXT_MAX_TOKENS": "8000",
    "MULTI_KB_FUSION_MAX_BYTES": "",
    "MULTI_KB_TURN_EVIDENCE_MAX_BYTES": "",
    "RETRIEVAL_EXECUTOR_MAX_WORKERS": "8",
    "RETRIEVAL_QUEUE_MAX_DEPTH": "256",
    "RETRIEVAL_PER_REQUEST_FANOUT": "4",
    "RETRIEVAL_QUEUE_MAX_WAIT_SECONDS": "30",
    "RETRIEVAL_SHUTDOWN_DEADLINE_SECONDS": "10",
    "ADMISSION_CONTROL_ENABLED": "true",
    "ADMISSION_CHAT_RPM_PER_USER": "30",
    "ADMISSION_CHAT_RPM_PER_TENANT": "120",
    "ADMISSION_HEAVY_RPM_PER_USER": "10",
    "ADMISSION_HEAVY_RPM_PER_TENANT": "40",
    "ADMISSION_RETRY_AFTER_SECONDS": "5",
    "ADMISSION_DEEP_RPM_PER_USER": "",
    "ADMISSION_DEEP_RPM_PER_TENANT": "",
    "ADMISSION_EXPORT_RPM_PER_USER": "",
    "ADMISSION_EXPORT_RPM_PER_TENANT": "",
    "ADMISSION_OCR_RPM_PER_USER": "",
    "ADMISSION_OCR_RPM_PER_TENANT": "",
    "ADMISSION_EVALUATION_RPM_PER_USER": "",
    "ADMISSION_EVALUATION_RPM_PER_TENANT": "",
    "ADMISSION_MODEL_RPM_PER_USER": "",
    "ADMISSION_MODEL_RPM_PER_TENANT": "",
    "ADMISSION_MAX_UNTERMINAL_TURNS_PER_SESSION": "8",
    "ADMISSION_MAX_UNTERMINAL_TURNS_PER_USER": "32",
    "WORKER_PARSE_BATCH_SIZE": "1",
    "WORKER_CLAIM_BATCH_SIZE": "8",
    "WORKER_TURN_CONCURRENCY": "1",
    "RESULT_EXPORT_MAX_BYTES": str(25 * 1024 * 1024),
    "RESULT_EXPORT_JOB_LEASE_SECONDS": "300",
    "RESULT_EXPORT_JOB_BATCH_SIZE": "8",
    "RESULT_EXPORT_RETENTION_DAYS": "30",
    "RESULT_EXPORT_MAX_RUNNING_JOBS": "4",
    "RESULT_EXPORT_ORGANIZE_MAX_CHARS": "40000",
    "CHAT_TURN_HEARTBEAT_TTL_SECONDS": "90",
    "CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS": "10",
    "CONVERSATION_SERIALIZE_SAME_THREAD": "false",
    "CHAT_TURN_LEASE_SECONDS": "120",
    "CHAT_TURN_LEASE_RENEW_SECONDS": "15",
    "WAITING_TURN_MAX_SECONDS": "600",
    "WAITING_TURN_TIMEOUT_SCAN_SECONDS": "5",
    "EVALUATION_DATASET_MAX_BYTES": str(100 * 1024 * 1024),
    "CHAT_ATTACHMENTS_ENABLED": "true",
    "CHAT_ATTACHMENT_STORAGE_DIR": os.path.join(STORAGE_DIR, "chat_attachments"),
    "CHAT_ATTACHMENT_INDEX_DB_PATH": os.path.join(STORAGE_DIR, "chat_attachments.db"),
    "CHAT_ATTACHMENT_MAX_BYTES": str(50 * 1024 * 1024),
    "CHAT_ATTACHMENT_MAX_FILES_PER_SESSION": "20",
    "CHAT_ATTACHMENT_MAX_SESSION_BYTES": str(500 * 1024 * 1024),
    "CHAT_ATTACHMENT_MAX_TENANT_BYTES": str(5 * 1024 * 1024 * 1024),
    "CHAT_ATTACHMENT_MAX_PAGES": "300",
    "CHAT_ATTACHMENT_MAX_ROWS": "200000",
    "CHAT_ATTACHMENT_MAX_CELLS": "2000000",
    "CHAT_ATTACHMENT_MAX_SHEETS": "100",
    "CHAT_ATTACHMENT_MAX_SHARED_STRINGS": "1000000",
    "CHAT_ATTACHMENT_MAX_CELL_TEXT_LENGTH": "100000",
    "CHAT_ATTACHMENT_MAX_UNCOMPRESSED_BYTES": str(200 * 1024 * 1024),
    "CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS": "120",
    "CHAT_ATTACHMENT_RETENTION_SECONDS": str(30 * 24 * 3600),
    "CHAT_ATTACHMENT_CONTEXT_MAX_TOKENS": "8000",
    "CHAT_ATTACHMENT_CONTEXT_RATIO": "0.3",
    "CHAT_ATTACHMENT_FTS_ENABLED": "true",
    "CHAT_ATTACHMENT_TRIGRAM_ENABLED": "true",
    "CHAT_ATTACHMENT_DENSE_ENABLED": "false",
    "CHAT_ATTACHMENT_RETRIEVAL_MODE": "lexical",
    "CHAT_ATTACHMENT_EMBEDDING_PROVIDER": "openai_compatible",
    "CHAT_ATTACHMENT_EMBEDDING_BASE_URL": "",
    "CHAT_ATTACHMENT_EMBEDDING_MODEL": "",
    "CHAT_ATTACHMENT_EMBEDDING_API_KEY": "",
    "CHAT_ATTACHMENT_DENSE_MAX_PARTS": "4000",
    "CHAT_ATTACHMENT_OCR_ENABLED": "true",
    "CHAT_ATTACHMENT_OCR_MAX_PAGES": "10",
    "CHAT_ATTACHMENT_VISUAL_ENABLED": "false",
    "CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED": "false",
    "CHAT_ATTACHMENT_VISUAL_PROVIDER": "volcengine_ark",
    "CHAT_ATTACHMENT_VISUAL_BASE_URL": "https://ark.cn-beijing.volces.com/api/v3",
    "CHAT_ATTACHMENT_VISUAL_MODEL": "",
    "CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN": "3",
    "CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES": str(8 * 1024 * 1024),
    "CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS": "60",
    "ARK_API_KEY": "",
    "CHAT_ATTACHMENT_JOB_LEASE_SECONDS": "300",
    "CHAT_ATTACHMENT_JOB_MAX_ATTEMPTS": "3",
    "OBS_ENABLED": "true",
    "OTEL_SERVICE_NAME": "hardware-database-api",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4317",
    "OBS_ENVIRONMENT": "development",
    "OBS_SERVICE_VERSION": "0.1.0",
    "OBS_TRACE_SAMPLE_RATIO": "1.0",
    "OBS_CAPTURE_CONTENT": "false",
    "OBS_CAPTURE_QUERY": "false",
    "OBS_CAPTURE_EVIDENCE": "false",
    "OBS_CAPTURE_LLM_CONTENT": "false",
    "OBS_CAPTURE_MCP_CONTENT": "false",
    "OBS_CONTENT_MAX_CHARS": "50000",
    "OBS_LOG_FORMAT": "json",
    "OBS_METRICS_ENABLED": "true",
    "OBS_TRACES_ENABLED": "true",
    "OBS_LOGS_ENABLED": "true",
    "OBS_PHOENIX_PROJECT": "hardware-database",
    "OBS_GRAFANA_BASE_URL": "",
    "OBS_PHOENIX_BASE_URL": "",
    "OBS_WORKER_HEARTBEAT_INTERVAL_SECONDS": "10",
    "OBS_WORKER_STALE_SECONDS": "30",
    "OBS_DEPENDENCY_TIMEOUT_SECONDS": "5",
    "OBS_READINESS_CACHE_SECONDS": "1",
    "SYSTEM_PROMPT": SYSTEM_PROMPT,
    "NO_CONTEXT_PROMPT": NO_CONTEXT_PROMPT,
}


DEFAULT_VALUES.update({key: (("true" if default else "false") if isinstance(default, bool) else str(default)) for key, default in _EVIDENCE_DEFAULTS.items()})

def reload_settings():
    """Reload hot settings, retaining construction-time settings until restart."""
    with _ENV_WRITE_LOCK:
        runtime = {key: globals()[key] for key in RESTART_REQUIRED_SETTINGS if key in globals()}
        environment = {key: os.environ.get(key) for key in RESTART_REQUIRED_SETTINGS}
        try:
            _reload_settings()
        finally:
            globals().update(runtime)
            for key, value in environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _reload_settings():
    """Parse the reloadable configuration under the settings lock."""
    global RAGFLOW_BASE_URL, RAGFLOW_API_KEY
    global RAGFLOW_GOVERNANCE_DATASET_NAME, RAGFLOW_DESIGN_DATASET_NAME
    global RAGFLOW_TIMEOUT_SECONDS, RAGFLOW_SIMILARITY_THRESHOLD, RAGFLOW_VECTOR_WEIGHT
    global RAGFLOW_CHUNK_LIST_PAGE_SIZE, RAGFLOW_CHUNK_LIST_MAX_PAGES, RAGFLOW_CHUNK_LIST_MAX_BYTES
    global RAGFLOW_STATUS_REFRESH_MIN_SECONDS
    global RAGFLOW_RETRIEVAL_DOC_SCOPE_ENABLED, RAGFLOW_RETRIEVAL_DOC_IDS_MAX
    global RAGFLOW_METADATA_WRITE_BEST_EFFORT
    global AUTH_DB_PATH, AUTH_DEFAULT_ADMIN_USERNAME, AUTH_DEFAULT_ADMIN_PASSWORD, AUTH_SESSION_TTL_HOURS
    global MCP_TOKEN_TTL_DAYS, MCP_SESSION_TTL_HOURS
    global AGENT_CHECKPOINT_DB_PATH, AGENT_CHECKPOINT_BACKEND, AGENT_CHECKPOINT_DATABASE_URL
    global AGENT_CHECKPOINT_POOL_SIZE
    global PIPELINE_ARCHIVE_ROOT, RAGFLOW_FILE_ROOT
    global RESULT_EXPORT_STORAGE_DIR
    global RESULT_EXPORT_ENABLED, RESULT_EXPORT_MD_ENABLED, RESULT_EXPORT_XLSX_ENABLED
    global RESULT_EXPORT_DOCX_ENABLED, RESULT_EXPORT_PDF_ENABLED, RESULT_EXPORT_PPTX_ENABLED
    global RESULT_EXPORT_ORGANIZE_ENABLED
    global AGENT_LLM_PROVIDER
    global AGENT_OLLAMA_BASE_URL, AGENT_OLLAMA_MODEL
    global AGENT_CUSTOM_API_KEY, AGENT_CUSTOM_BASE_URL, AGENT_CUSTOM_MODEL
    global AGENT_CUSTOM_MAX_TOKENS, AGENT_REASONING_EFFORT, AGENT_TEMPERATURE, AGENT_TIMEOUT_SECONDS
    global AGENT_MODEL_MAX_INPUT_TOKENS
    global AGENT_RATE_LIMIT_MAX_RETRIES, AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS
    global AGENT_RATE_LIMIT_MAX_DELAY_SECONDS
    global LLM_MAX_CONCURRENCY, LLM_BATCH_MAX_CONCURRENCY
    global LLM_QUEUE_TIMEOUT_SECONDS, LLM_QUEUE_WARN_SECONDS, LLM_STREAM_IDLE_TIMEOUT_SECONDS
    global FINAL_TOP_K, AGENT_MAX_RETRIEVAL_ROUNDS
    global AGENT_DEEP_TOOL_CALL_CAP, AGENT_DEEP_RETRIEVAL_CALL_CAP
    global AGENT_DEEP_FINALIZE_RESERVE, AGENT_DEEP_LOOP_SECONDS
    global CHAT_QUEUE_DEADLINE_DEEP_SECONDS
    global WORKER_POLL_INTERVAL_SECONDS, WORKER_PARSE_BATCH_SIZE, WORKER_CLAIM_BATCH_SIZE, WORKER_TURN_CONCURRENCY
    global FINAL_TOP_K, AGENT_MAX_RETRIEVAL_ROUNDS
    global WORKER_POLL_INTERVAL_SECONDS, WORKER_PARSE_BATCH_SIZE, WORKER_ROLE
    global RESOURCE_GOVERNOR_ENABLED, RESOURCE_GOVERNOR_SCOPE, RESOURCE_GOVERNOR_DB_PATH
    global RESOURCE_GOVERNOR_BACKEND, RESOURCE_GOVERNOR_DATABASE_URL
    global RESOURCE_QUEUE_MAX_WAIT_SECONDS, RESOURCE_QUEUE_MAX_DEPTH, RESOURCE_PERMIT_LEASE_SECONDS
    global RESOURCE_UNCERTAIN_ESCALATE_SECONDS, RESOURCE_UNCERTAIN_AUTO_RELEASE_SECONDS
    global RESOURCE_GOVERNOR_MAINTENANCE_SECONDS
    global RESOURCE_GOVERNOR_DEPLOYMENT_ID, RESOURCE_GOVERNOR_REQUIRE_PRECONFIGURED
    global CONTROL_PLANE_BACKEND, CONTROL_PLANE_DATABASE_URL, CONTROL_PLANE_POOL_SIZE
    global EVALUATION_STORE_BACKEND, EVALUATION_STORE_DATABASE_URL
    global EVAL_EXECUTION_MODE
    global QUALITY_ENABLED, QUALITY_CAPTURE_CONTENT, QUALITY_SAMPLE_RATE, QUALITY_SAMPLE_DAILY_LIMIT
    global QUALITY_PHOENIX_SYNC_ENABLED, QUALITY_PHOENIX_ENDPOINT, QUALITY_PHOENIX_API_KEY, QUALITY_PHOENIX_MAX_ATTEMPTS
    global COST_LEDGER_ENABLED, COST_MODEL_REGISTRY
    global PIPELINE_DOCUMENT_STORE_BACKEND, PIPELINE_DOCUMENT_DATABASE_URL
    global AUTH_STORE_BACKEND, AUTH_STORE_DATABASE_URL
    global REDIS_URL, REDIS_REQUIRED, CANCEL_KEY_TTL_SECONDS
    global TURN_EVENT_STREAM_TTL_SECONDS, TURN_EVENT_STREAM_MAXLEN
    global DISTRIBUTED_ADMISSION_ENABLED, DISTRIBUTED_ADMISSION_TTL_SECONDS
    global PROVIDER_RPM_LIMIT
    global PROVIDER_TPM_LIMIT
    global CHAT_QUEUE_DEADLINE_SECONDS
    global RESOURCE_LLM_MAX_CONCURRENT, RESOURCE_LLM_RESERVED_INTERACTIVE
    global RESOURCE_RAG_MAX_CONCURRENT, RESOURCE_RAG_RESERVED_INTERACTIVE
    global RESOURCE_TOOL_MAX_CONCURRENT, RESOURCE_TOOL_RESERVED_INTERACTIVE
    global RESOURCE_CHAT_MAX_CONCURRENT, RESOURCE_CHAT_RESERVED_INTERACTIVE
    global RESOURCE_HEAVY_JOB_MAX_CONCURRENT, RESOURCE_HEAVY_JOB_RESERVED_INTERACTIVE
    global RESOURCE_OCR_MAX_CONCURRENT, RESOURCE_OCR_RESERVED_INTERACTIVE
    global RESOURCE_EXPORT_MAX_CONCURRENT, RESOURCE_EXPORT_RESERVED_INTERACTIVE
    global RESOURCE_MEMORY_MAX_CONCURRENT, RESOURCE_MEMORY_RESERVED_INTERACTIVE
    global MCP_ENABLED, MCP_ALLOW_INSECURE_HTTP, MCP_ALLOW_PRIVATE_HOSTS
    global MCP_CONNECT_TIMEOUT_SECONDS, MCP_CALL_TIMEOUT_SECONDS, MCP_SSE_READ_TIMEOUT_SECONDS
    global MCP_MAX_RESULT_CHARS, MCP_MAX_RESULT_BYTES, MCP_MAX_ARGUMENT_CHARS
    global MCP_MAX_TOOLS_PER_SERVER, MCP_MAX_SERVERS_PER_RUN, MCP_CALLS_PER_TURN
    global MCP_FAILURE_COOLDOWN_SECONDS, MCP_WRITE_ENABLED, MCP_WRITE_CONFIRM_TTL_SECONDS
    global MCP_MAX_ARTIFACT_BYTES, MCP_ARTIFACT_ALLOWED_EXTENSIONS, MCP_CREDENTIALS_KEY
    global MCP_ARTIFACT_STORAGE_DIR, MCP_WRITE_MAX_ATTEMPTS
    global RESOURCE_MCP_MAX_CONCURRENT, RESOURCE_MCP_RESERVED_INTERACTIVE
    global SANDBOX_ENABLED, SANDBOX_BACKEND, SANDBOX_GATEWAY_ENABLED
    global SANDBOX_GATEWAY_URL, SANDBOX_RUNNER_URL, SANDBOX_SERVICE_AUTH_REF
    global SANDBOX_SERVICE_TOKEN_FILE
    global SANDBOX_DEFAULT_NETWORK
    global SANDBOX_MAX_CONCURRENT, SANDBOX_TASK_TIMEOUT_SECONDS, SANDBOX_TASK_LEASE_SECONDS
    global SANDBOX_MAX_CONCURRENT_PER_USER, SANDBOX_MAX_CONCURRENT_PER_DEPARTMENT
    global SANDBOX_MAX_OUTPUT_BYTES, SANDBOX_MAX_INPUT_BYTES, SANDBOX_ALLOW_DYNAMIC_INSTALL
    global SANDBOX_AUTHORIZED_CAPABILITIES
    global SANDBOX_REQUIRE_RUNTIME_ATTESTATION, SANDBOX_RUNTIME_ATTESTATION_REF
    global SANDBOX_RUNTIME_PROFILE_DIGEST, SANDBOX_ARTIFACT_SCANNER_REF
    global SANDBOX_DISPATCH_INTERVAL_SECONDS, SANDBOX_RECONCILE_INTERVAL_SECONDS
    global SANDBOX_TASK_DB_PATH, SANDBOX_ARTIFACT_STORAGE_DIR
    global SANDBOX_SKILL_STORAGE_DIR
    global SANDBOX_SKILL_PIP_ALLOWED_HOSTS
    global SANDBOX_ARTIFACT_ALLOWED_EXTENSIONS
    global SKILLS_ENABLED
    global RETRIEVAL_EXECUTOR_MAX_WORKERS
    global MULTI_KB_CHAT_ENABLED
    global MULTI_KB_HUMAN_REVIEW_ENABLED
    global MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED
    global MULTI_KB_MAX_SOURCES
    global MULTI_KB_PER_SOURCE_TOP_K, MULTI_KB_MAX_CALLS_PER_TURN
    global MULTI_KB_PER_RECORD_CANDIDATES, MULTI_KB_PER_SOURCE_CANDIDATES
    global MULTI_KB_RETRIEVAL_DEADLINE_SECONDS, MULTI_KB_EVIDENCE_MAX_ITEMS, MULTI_KB_CONTEXT_MAX_TOKENS
    global MULTI_KB_FUSION_MAX_BYTES, MULTI_KB_TURN_EVIDENCE_MAX_BYTES
    global RETRIEVAL_QUEUE_MAX_DEPTH, RETRIEVAL_PER_REQUEST_FANOUT, RETRIEVAL_QUEUE_MAX_WAIT_SECONDS
    global RETRIEVAL_SHUTDOWN_DEADLINE_SECONDS
    global ADMISSION_CONTROL_ENABLED, ADMISSION_CHAT_RPM_PER_USER, ADMISSION_CHAT_RPM_PER_TENANT
    global ADMISSION_HEAVY_RPM_PER_USER, ADMISSION_HEAVY_RPM_PER_TENANT, ADMISSION_RETRY_AFTER_SECONDS
    global ADMISSION_MAX_UNTERMINAL_TURNS_PER_SESSION, ADMISSION_MAX_UNTERMINAL_TURNS_PER_USER
    global ADMISSION_DEEP_RPM_PER_USER, ADMISSION_DEEP_RPM_PER_TENANT
    global ADMISSION_DOCUMENT_RPM_PER_USER, ADMISSION_DOCUMENT_RPM_PER_TENANT
    global ADMISSION_EXPORT_RPM_PER_USER, ADMISSION_EXPORT_RPM_PER_TENANT
    global ADMISSION_OCR_RPM_PER_USER, ADMISSION_OCR_RPM_PER_TENANT
    global ADMISSION_EVALUATION_RPM_PER_USER, ADMISSION_EVALUATION_RPM_PER_TENANT
    global ADMISSION_MODEL_RPM_PER_USER, ADMISSION_MODEL_RPM_PER_TENANT
    global RESULT_EXPORT_MAX_BYTES, RESULT_EXPORT_JOB_LEASE_SECONDS, RESULT_EXPORT_JOB_BATCH_SIZE
    global RESULT_EXPORT_RETENTION_DAYS, RESULT_EXPORT_MAX_RUNNING_JOBS
    global RESULT_EXPORT_ORGANIZE_MAX_CHARS
    global CHAT_TURN_HEARTBEAT_TTL_SECONDS, CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS
    global CONVERSATION_SERIALIZE_SAME_THREAD, CHAT_TURN_LEASE_SECONDS, CHAT_TURN_LEASE_RENEW_SECONDS
    global WAITING_TURN_MAX_SECONDS, WAITING_TURN_TIMEOUT_SCAN_SECONDS
    global EVALUATION_DATASET_MAX_BYTES
    global CHAT_ATTACHMENTS_ENABLED, CHAT_ATTACHMENT_STORAGE_DIR, CHAT_ATTACHMENT_INDEX_DB_PATH
    global CHAT_ATTACHMENT_MAX_BYTES, CHAT_ATTACHMENT_MAX_FILES_PER_SESSION
    global CHAT_ATTACHMENT_MAX_SESSION_BYTES, CHAT_ATTACHMENT_MAX_TENANT_BYTES, CHAT_ATTACHMENT_MAX_PAGES
    global CHAT_ATTACHMENT_MAX_ROWS, CHAT_ATTACHMENT_MAX_CELLS, CHAT_ATTACHMENT_MAX_SHEETS
    global CHAT_ATTACHMENT_MAX_SHARED_STRINGS, CHAT_ATTACHMENT_MAX_CELL_TEXT_LENGTH
    global CHAT_ATTACHMENT_MAX_UNCOMPRESSED_BYTES
    global CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS, CHAT_ATTACHMENT_RETENTION_SECONDS
    global CHAT_ATTACHMENT_CONTEXT_MAX_TOKENS, CHAT_ATTACHMENT_CONTEXT_RATIO
    global CHAT_ATTACHMENT_FTS_ENABLED, CHAT_ATTACHMENT_TRIGRAM_ENABLED
    global CHAT_ATTACHMENT_DENSE_ENABLED, CHAT_ATTACHMENT_RETRIEVAL_MODE
    global CHAT_ATTACHMENT_EMBEDDING_PROVIDER, CHAT_ATTACHMENT_EMBEDDING_BASE_URL
    global CHAT_ATTACHMENT_EMBEDDING_MODEL, CHAT_ATTACHMENT_EMBEDDING_API_KEY
    global CHAT_ATTACHMENT_DENSE_MAX_PARTS
    global CHAT_ATTACHMENT_OCR_ENABLED, CHAT_ATTACHMENT_OCR_MAX_PAGES
    global CHAT_ATTACHMENT_VISUAL_ENABLED, CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED
    global CHAT_ATTACHMENT_VISUAL_PROVIDER, CHAT_ATTACHMENT_VISUAL_BASE_URL
    global CHAT_ATTACHMENT_VISUAL_MODEL, CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN
    global CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES, CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS
    global ARK_API_KEY
    global CHAT_ATTACHMENT_JOB_LEASE_SECONDS, CHAT_ATTACHMENT_JOB_MAX_ATTEMPTS
    global OBS_ENABLED, OTEL_SERVICE_NAME, OTEL_EXPORTER_OTLP_ENDPOINT, OBS_ENVIRONMENT, OBS_SERVICE_VERSION
    global OBS_TRACE_SAMPLE_RATIO, OBS_CAPTURE_CONTENT, OBS_CAPTURE_QUERY, OBS_CAPTURE_EVIDENCE, OBS_CAPTURE_LLM_CONTENT
    global OBS_CAPTURE_MCP_CONTENT
    global OBS_CONTENT_MAX_CHARS
    global OBS_LOG_FORMAT, OBS_METRICS_ENABLED, OBS_TRACES_ENABLED, OBS_LOGS_ENABLED, OBS_PHOENIX_PROJECT
    global OBS_GRAFANA_BASE_URL, OBS_PHOENIX_BASE_URL, OBS_WORKER_HEARTBEAT_INTERVAL_SECONDS
    global OBS_WORKER_STALE_SECONDS, OBS_DEPENDENCY_TIMEOUT_SECONDS
    global OBS_READINESS_CACHE_SECONDS
    global SYSTEM_PROMPT, NO_CONTEXT_PROMPT
    global EXTERNAL_CONVERSATION_LLM_STRUCTURE, EXTERNAL_CONVERSATION_LLM_MAX_CHARS, EXTERNAL_CONVERSATION_LLM_SUMMARY
    global EXTERNAL_CONVERSATION_LLM_TIMEOUT_SECONDS
    global MEMORY_ENABLED, MEMORY_STORE_BACKEND, MEMORY_DATABASE_URL, MEMORY_SQLITE_PATH, MEMORY_SQLITE_BUSY_TIMEOUT_MS
    global MEMORY_SINGLE_WRITER, MEMORY_EXTRACTION_ENABLED, MEMORY_DEBOUNCE_SECONDS, MEMORY_JOB_LEASE_SECONDS
    global MEMORY_JOB_MAX_RETRIES, MEMORY_REFLECTION_TIMEOUT_SECONDS
    global MEMORY_REFLECTION_MIN_INTERVAL_SECONDS, MEMORY_REFLECTION_CIRCUIT_FAILURES, MEMORY_REFLECTION_CIRCUIT_COOLDOWN_SECONDS
    global MEMORY_PROJECTION_MAX_RETRIES, MEMORY_RECONCILE_INTERVAL_SECONDS
    global MEMORY_USER_TOP_K, MEMORY_PROJECT_TOP_K, MEMORY_STORE_OVERSAMPLE_FACTOR, MEMORY_STORE_MAX_SCAN
    global MEMORY_CONTEXT_MAX_TOKENS, MEMORY_ITEM_MAX_TOKENS, MEMORY_MIN_SCORE_ENABLED, MEMORY_MIN_SCORE
    global MEMORY_MODEL_PROVIDER, MEMORY_MODEL, MEMORY_MODEL_BASE_URL, MEMORY_MODEL_API_KEY
    global MEMORY_MODEL_REQUIRE_STRUCTURED_TOOLS, MEMORY_EMBEDDING_PROVIDER, MEMORY_EMBEDDING_MODEL
    global MEMORY_EMBEDDING_BASE_URL, MEMORY_EMBEDDING_API_KEY, MEMORY_EMBEDDING_DIMS, MEMORY_INDEX_FIELDS
    global MEMORY_EMBEDDING_INDEX_KIND
    global MEMORY_ALLOW_GENERAL_CHAT_EXTRACTION, MEMORY_USER_MEMORY_OPT_IN
    global MEMORY_USER_MEMORY_REQUIRE_PER_EVENT_CONSENT, MEMORY_RETENTION_DAYS
    global CONFIG_REVISION

    load_dotenv(dotenv_path=ENV_FILE_PATH, override=True, encoding=ENV_FILE_ENCODING)
    CONFIG_REVISION = _config_revision(ENV_FILE_PATH)
    # Invalidate cached chat models built from the previous env generation
    # (lazy import avoids a settings -> model_factory import cycle).
    from src.core.model_factory import notify_settings_reloaded
    notify_settings_reloaded()

    RAGFLOW_BASE_URL = os.getenv("RAGFLOW_BASE_URL", "http://localhost:9380")
    RAGFLOW_API_KEY = os.getenv("RAGFLOW_API_KEY", "")
    RAGFLOW_GOVERNANCE_DATASET_NAME = os.getenv("RAGFLOW_GOVERNANCE_DATASET_NAME", "department_governance")
    RAGFLOW_DESIGN_DATASET_NAME = os.getenv("RAGFLOW_DESIGN_DATASET_NAME", "project_design_assets")
    RAGFLOW_TIMEOUT_SECONDS = int(os.getenv("RAGFLOW_TIMEOUT_SECONDS", "120"))
    RAGFLOW_CHUNK_LIST_PAGE_SIZE = int(os.getenv("RAGFLOW_CHUNK_LIST_PAGE_SIZE", "30"))
    RAGFLOW_CHUNK_LIST_MAX_PAGES = int(os.getenv("RAGFLOW_CHUNK_LIST_MAX_PAGES", "100"))
    RAGFLOW_CHUNK_LIST_MAX_BYTES = int(os.getenv("RAGFLOW_CHUNK_LIST_MAX_BYTES", str(8 * 1024 * 1024)))
    RAGFLOW_STATUS_REFRESH_MIN_SECONDS = int(os.getenv("RAGFLOW_STATUS_REFRESH_MIN_SECONDS", "60"))
    RAGFLOW_SIMILARITY_THRESHOLD = float(os.getenv("RAGFLOW_SIMILARITY_THRESHOLD", "0.25"))
    RAGFLOW_VECTOR_WEIGHT = float(os.getenv("RAGFLOW_VECTOR_WEIGHT", "0.4"))
    RAGFLOW_RETRIEVAL_DOC_SCOPE_ENABLED = os.getenv("RAGFLOW_RETRIEVAL_DOC_SCOPE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    RAGFLOW_RETRIEVAL_DOC_IDS_MAX = int(os.getenv("RAGFLOW_RETRIEVAL_DOC_IDS_MAX", "2000") or 2000)
    RAGFLOW_METADATA_WRITE_BEST_EFFORT = os.getenv("RAGFLOW_METADATA_WRITE_BEST_EFFORT", "true").strip().lower() in {"1", "true", "yes", "on"}
    PIPELINE_ARCHIVE_ROOT = os.getenv("PIPELINE_ARCHIVE_ROOT", os.path.join(STORAGE_DIR, "pipeline_archives"))
    RAGFLOW_FILE_ROOT = os.getenv("RAGFLOW_FILE_ROOT", PIPELINE_ARCHIVE_ROOT)

    AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", os.path.join(STORAGE_DIR, "auth.db"))
    AGENT_CHECKPOINT_DB_PATH = os.getenv(
        "AGENT_CHECKPOINT_DB_PATH", os.path.join(STORAGE_DIR, "agent_checkpoints.db")
    )
    AGENT_CHECKPOINT_BACKEND = os.getenv("AGENT_CHECKPOINT_BACKEND", "sqlite").strip().lower()
    if AGENT_CHECKPOINT_BACKEND not in {"sqlite", "postgres"}:
        AGENT_CHECKPOINT_BACKEND = "sqlite"
    AGENT_CHECKPOINT_DATABASE_URL = os.getenv("AGENT_CHECKPOINT_DATABASE_URL", "")
    AGENT_CHECKPOINT_POOL_SIZE = int(os.getenv("AGENT_CHECKPOINT_POOL_SIZE", "4"))
    AUTH_DEFAULT_ADMIN_USERNAME = os.getenv("AUTH_DEFAULT_ADMIN_USERNAME", "admin")
    AUTH_DEFAULT_ADMIN_PASSWORD = os.getenv("AUTH_DEFAULT_ADMIN_PASSWORD", "")
    AUTH_SESSION_TTL_HOURS = int(os.getenv("AUTH_SESSION_TTL_HOURS", "24"))
    MCP_TOKEN_TTL_DAYS = int(os.getenv("MCP_TOKEN_TTL_DAYS", "365"))
    MCP_SESSION_TTL_HOURS = int(os.getenv("MCP_SESSION_TTL_HOURS", "24"))

    AGENT_LLM_PROVIDER = Provider(os.getenv("AGENT_LLM_PROVIDER", "ollama").lower())
    AGENT_OLLAMA_BASE_URL = os.getenv("AGENT_OLLAMA_BASE_URL", "http://localhost:11434")
    AGENT_OLLAMA_MODEL = os.getenv("AGENT_OLLAMA_MODEL", "qwen2.5:32b")
    AGENT_CUSTOM_API_KEY = os.getenv("AGENT_CUSTOM_API_KEY", "")
    AGENT_CUSTOM_BASE_URL = os.getenv("AGENT_CUSTOM_BASE_URL", "")
    AGENT_CUSTOM_MODEL = os.getenv("AGENT_CUSTOM_MODEL", "")
    AGENT_CUSTOM_MAX_TOKENS = int(os.getenv("AGENT_CUSTOM_MAX_TOKENS", "4096"))
    # Optional OpenAI-compatible reasoning effort (low/high/max). Reasoning
    # models count hidden reasoning tokens against max_tokens, so a low effort
    # avoids spending the whole output budget before any visible answer.
    AGENT_REASONING_EFFORT = os.getenv("AGENT_REASONING_EFFORT", "").strip().lower()
    AGENT_MODEL_MAX_INPUT_TOKENS = int(os.getenv("AGENT_MODEL_MAX_INPUT_TOKENS", "65536"))
    AGENT_TEMPERATURE = float(os.getenv("AGENT_TEMPERATURE", "0.2"))
    AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "120"))
    AGENT_RATE_LIMIT_MAX_RETRIES = int(os.getenv("AGENT_RATE_LIMIT_MAX_RETRIES", "4"))
    AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS = float(os.getenv("AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS", "1"))
    AGENT_RATE_LIMIT_MAX_DELAY_SECONDS = float(os.getenv("AGENT_RATE_LIMIT_MAX_DELAY_SECONDS", "16"))
    LLM_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "4"))
    LLM_BATCH_MAX_CONCURRENCY = int(os.getenv("LLM_BATCH_MAX_CONCURRENCY", "1"))
    LLM_QUEUE_TIMEOUT_SECONDS = int(os.getenv("LLM_QUEUE_TIMEOUT_SECONDS", "180"))
    LLM_QUEUE_WARN_SECONDS = int(os.getenv("LLM_QUEUE_WARN_SECONDS", "10"))
    LLM_STREAM_IDLE_TIMEOUT_SECONDS = int(os.getenv("LLM_STREAM_IDLE_TIMEOUT_SECONDS", "0"))

    FINAL_TOP_K = int(os.getenv("FINAL_TOP_K", "5"))
    AGENT_MAX_RETRIEVAL_ROUNDS = int(os.getenv("AGENT_MAX_RETRIEVAL_ROUNDS", "3"))
    AGENT_DEEP_TOOL_CALL_CAP = int(os.getenv("AGENT_DEEP_TOOL_CALL_CAP", "32"))
    AGENT_DEEP_RETRIEVAL_CALL_CAP = int(os.getenv("AGENT_DEEP_RETRIEVAL_CALL_CAP", "20"))
    AGENT_DEEP_FINALIZE_RESERVE = int(os.getenv("AGENT_DEEP_FINALIZE_RESERVE", "4"))
    AGENT_DEEP_LOOP_SECONDS = int(os.getenv("AGENT_DEEP_LOOP_SECONDS", "360"))
    WORKER_POLL_INTERVAL_SECONDS = float(os.getenv("WORKER_POLL_INTERVAL_SECONDS", "0.5"))
    WORKER_ROLE = os.getenv("WORKER_ROLE", "all").strip().lower()
    if WORKER_ROLE not in {"all", "chat", "authoring", "parse"}:
        WORKER_ROLE = "all"
    RESOURCE_GOVERNOR_ENABLED = _env_bool("RESOURCE_GOVERNOR_ENABLED", True)
    RESOURCE_GOVERNOR_SCOPE = os.getenv("RESOURCE_GOVERNOR_SCOPE", "process").strip().lower()
    if RESOURCE_GOVERNOR_SCOPE not in {"process", "deployment"}:
        RESOURCE_GOVERNOR_SCOPE = "process"
    RESOURCE_GOVERNOR_DB_PATH = _resolve_storage_path(
        os.getenv("RESOURCE_GOVERNOR_DB_PATH"), "resource_quotas.db"
    )
    RESOURCE_GOVERNOR_BACKEND = os.getenv("RESOURCE_GOVERNOR_BACKEND", "sqlite").strip().lower()
    if RESOURCE_GOVERNOR_BACKEND not in {"sqlite", "postgres"}:
        RESOURCE_GOVERNOR_BACKEND = "sqlite"
    RESOURCE_GOVERNOR_DATABASE_URL = os.getenv("RESOURCE_GOVERNOR_DATABASE_URL", "")
    RESOURCE_QUEUE_MAX_WAIT_SECONDS = float(os.getenv("RESOURCE_QUEUE_MAX_WAIT_SECONDS", "30"))
    RESOURCE_QUEUE_MAX_DEPTH = int(os.getenv("RESOURCE_QUEUE_MAX_DEPTH", "256"))
    RESOURCE_PERMIT_LEASE_SECONDS = int(os.getenv("RESOURCE_PERMIT_LEASE_SECONDS", "300"))
    RESOURCE_UNCERTAIN_ESCALATE_SECONDS = int(os.getenv("RESOURCE_UNCERTAIN_ESCALATE_SECONDS", "600"))
    # After this grace period an isolated uncertain permit is released with
    # server_deadline_passed evidence instead of blocking admission forever.
    RESOURCE_UNCERTAIN_AUTO_RELEASE_SECONDS = int(
        os.getenv("RESOURCE_UNCERTAIN_AUTO_RELEASE_SECONDS", "1800")
    )
    RESOURCE_GOVERNOR_MAINTENANCE_SECONDS = float(
        os.getenv("RESOURCE_GOVERNOR_MAINTENANCE_SECONDS", "5")
    )
    RESOURCE_GOVERNOR_DEPLOYMENT_ID = os.getenv("RESOURCE_GOVERNOR_DEPLOYMENT_ID", "local").strip() or "local"
    RESOURCE_GOVERNOR_REQUIRE_PRECONFIGURED = _env_bool(
        "RESOURCE_GOVERNOR_REQUIRE_PRECONFIGURED", False
    )
    CONTROL_PLANE_BACKEND = os.getenv("CONTROL_PLANE_BACKEND", "sqlite").strip().lower()
    CONTROL_PLANE_DATABASE_URL = os.getenv("CONTROL_PLANE_DATABASE_URL", "").strip()
    CONTROL_PLANE_POOL_SIZE = int(os.getenv("CONTROL_PLANE_POOL_SIZE", "8"))
    EVALUATION_STORE_BACKEND = os.getenv("EVALUATION_STORE_BACKEND", "").strip().lower()
    EVALUATION_STORE_DATABASE_URL = os.getenv("EVALUATION_STORE_DATABASE_URL", "").strip()
    EVAL_EXECUTION_MODE = os.getenv("EVAL_EXECUTION_MODE", "inline").strip().lower()
    QUALITY_ENABLED = os.getenv("QUALITY_ENABLED", "true").lower() == "true"
    QUALITY_CAPTURE_CONTENT = os.getenv("QUALITY_CAPTURE_CONTENT", "true").lower() == "true"
    QUALITY_SAMPLE_RATE = float(os.getenv("QUALITY_SAMPLE_RATE", "0.01"))
    QUALITY_SAMPLE_DAILY_LIMIT = int(os.getenv("QUALITY_SAMPLE_DAILY_LIMIT", "100"))
    QUALITY_PHOENIX_SYNC_ENABLED = os.getenv("QUALITY_PHOENIX_SYNC_ENABLED", "false").lower() == "true"
    QUALITY_PHOENIX_ENDPOINT = os.getenv("QUALITY_PHOENIX_ENDPOINT", "").strip()
    QUALITY_PHOENIX_API_KEY = os.getenv("QUALITY_PHOENIX_API_KEY", "").strip()
    QUALITY_PHOENIX_MAX_ATTEMPTS = int(os.getenv("QUALITY_PHOENIX_MAX_ATTEMPTS", "20"))
    COST_LEDGER_ENABLED = os.getenv("COST_LEDGER_ENABLED", "true").lower() == "true"
    COST_MODEL_REGISTRY = json.loads(os.getenv("COST_MODEL_REGISTRY", "{}"))
    PIPELINE_DOCUMENT_STORE_BACKEND = os.getenv(
        "PIPELINE_DOCUMENT_STORE_BACKEND", ""
    ).strip().lower()
    PIPELINE_DOCUMENT_DATABASE_URL = os.getenv(
        "PIPELINE_DOCUMENT_DATABASE_URL", ""
    ).strip()
    AUTH_STORE_BACKEND = os.getenv("AUTH_STORE_BACKEND", "sqlite").strip().lower()
    AUTH_STORE_DATABASE_URL = os.getenv("AUTH_STORE_DATABASE_URL", "").strip()
    REDIS_URL = os.getenv("REDIS_URL", "").strip()
    REDIS_REQUIRED = _env_bool("REDIS_REQUIRED", True)
    CANCEL_KEY_TTL_SECONDS = int(os.getenv("CANCEL_KEY_TTL_SECONDS", "7200"))
    TURN_EVENT_STREAM_TTL_SECONDS = int(os.getenv("TURN_EVENT_STREAM_TTL_SECONDS", "7200"))
    TURN_EVENT_STREAM_MAXLEN = int(os.getenv("TURN_EVENT_STREAM_MAXLEN", "5000"))
    DISTRIBUTED_ADMISSION_ENABLED = _env_bool("DISTRIBUTED_ADMISSION_ENABLED", False)
    DISTRIBUTED_ADMISSION_TTL_SECONDS = int(os.getenv("DISTRIBUTED_ADMISSION_TTL_SECONDS", "120"))
    PROVIDER_RPM_LIMIT = int(os.getenv("PROVIDER_RPM_LIMIT", "0"))
    PROVIDER_TPM_LIMIT = int(os.getenv("PROVIDER_TPM_LIMIT", "0"))
    CHAT_QUEUE_DEADLINE_SECONDS = int(os.getenv("CHAT_QUEUE_DEADLINE_SECONDS", "600"))
    CHAT_QUEUE_DEADLINE_DEEP_SECONDS = int(os.getenv("CHAT_QUEUE_DEADLINE_DEEP_SECONDS", "480"))
    RESOURCE_LLM_MAX_CONCURRENT = int(os.getenv("RESOURCE_LLM_MAX_CONCURRENT", "4"))
    RESOURCE_LLM_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_LLM_RESERVED_INTERACTIVE", "2"))
    RESOURCE_RAG_MAX_CONCURRENT = int(os.getenv("RESOURCE_RAG_MAX_CONCURRENT", "8"))
    RESOURCE_RAG_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_RAG_RESERVED_INTERACTIVE", "4"))
    RESOURCE_TOOL_MAX_CONCURRENT = int(os.getenv("RESOURCE_TOOL_MAX_CONCURRENT", "16"))
    RESOURCE_TOOL_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_TOOL_RESERVED_INTERACTIVE", "0"))
    RESOURCE_CHAT_MAX_CONCURRENT = int(os.getenv("RESOURCE_CHAT_MAX_CONCURRENT", "4"))
    RESOURCE_CHAT_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_CHAT_RESERVED_INTERACTIVE", "0"))
    RESOURCE_HEAVY_JOB_MAX_CONCURRENT = int(os.getenv("RESOURCE_HEAVY_JOB_MAX_CONCURRENT", "4"))
    RESOURCE_HEAVY_JOB_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_HEAVY_JOB_RESERVED_INTERACTIVE", "1"))
    RESOURCE_OCR_MAX_CONCURRENT = int(os.getenv("RESOURCE_OCR_MAX_CONCURRENT", "1"))
    RESOURCE_OCR_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_OCR_RESERVED_INTERACTIVE", "0"))
    RESOURCE_EXPORT_MAX_CONCURRENT = int(os.getenv("RESOURCE_EXPORT_MAX_CONCURRENT", "4"))
    RESOURCE_EXPORT_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_EXPORT_RESERVED_INTERACTIVE", "0"))
    RESOURCE_MEMORY_MAX_CONCURRENT = int(os.getenv("RESOURCE_MEMORY_MAX_CONCURRENT", "1"))
    RESOURCE_MEMORY_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_MEMORY_RESERVED_INTERACTIVE", "0"))
    MCP_ENABLED = _env_bool("MCP_ENABLED", True)
    if MCP_ENABLED:
        # Keep the platform prerequisite automatic for both fresh and legacy
        # environments. The public emergency kill switch remains authoritative.
        RESOURCE_GOVERNOR_ENABLED = True
    MCP_ALLOW_INSECURE_HTTP = _env_bool("MCP_ALLOW_INSECURE_HTTP", False)
    MCP_ALLOW_PRIVATE_HOSTS = _env_bool("MCP_ALLOW_PRIVATE_HOSTS", False)
    MCP_CONNECT_TIMEOUT_SECONDS = int(os.getenv("MCP_CONNECT_TIMEOUT_SECONDS", "15"))
    MCP_CALL_TIMEOUT_SECONDS = int(os.getenv("MCP_CALL_TIMEOUT_SECONDS", "60"))
    MCP_SSE_READ_TIMEOUT_SECONDS = int(os.getenv("MCP_SSE_READ_TIMEOUT_SECONDS", "300"))
    MCP_MAX_RESULT_CHARS = int(os.getenv("MCP_MAX_RESULT_CHARS", "20000"))
    MCP_MAX_RESULT_BYTES = int(os.getenv("MCP_MAX_RESULT_BYTES", "65536"))
    MCP_MAX_ARGUMENT_CHARS = int(os.getenv("MCP_MAX_ARGUMENT_CHARS", "12000"))
    MCP_MAX_TOOLS_PER_SERVER = int(os.getenv("MCP_MAX_TOOLS_PER_SERVER", "50"))
    MCP_MAX_SERVERS_PER_RUN = int(os.getenv("MCP_MAX_SERVERS_PER_RUN", "5"))
    MCP_CALLS_PER_TURN = int(os.getenv("MCP_CALLS_PER_TURN", "8"))
    MCP_FAILURE_COOLDOWN_SECONDS = int(os.getenv("MCP_FAILURE_COOLDOWN_SECONDS", "60"))
    MCP_WRITE_ENABLED = _env_bool("MCP_WRITE_ENABLED", False)
    MCP_WRITE_CONFIRM_TTL_SECONDS = int(os.getenv("MCP_WRITE_CONFIRM_TTL_SECONDS", "1800"))
    MCP_MAX_ARTIFACT_BYTES = int(os.getenv("MCP_MAX_ARTIFACT_BYTES", str(50 * 1024 * 1024)))
    MCP_ARTIFACT_ALLOWED_EXTENSIONS = os.getenv(
        "MCP_ARTIFACT_ALLOWED_EXTENSIONS", "kicad_sch,kicad_pcb,kicad_pro,drawio,zip,json,csv"
    )
    MCP_ARTIFACT_STORAGE_DIR = _resolve_storage_path(
        os.getenv("MCP_ARTIFACT_STORAGE_DIR"), "mcp_artifacts"
    )
    MCP_WRITE_MAX_ATTEMPTS = int(os.getenv("MCP_WRITE_MAX_ATTEMPTS", "3"))
    MCP_CREDENTIALS_KEY = os.getenv("MCP_CREDENTIALS_KEY", "").strip()
    RESOURCE_MCP_MAX_CONCURRENT = int(os.getenv("RESOURCE_MCP_MAX_CONCURRENT", "4"))
    RESOURCE_MCP_RESERVED_INTERACTIVE = int(os.getenv("RESOURCE_MCP_RESERVED_INTERACTIVE", "1"))
    SANDBOX_ENABLED = _env_bool("SANDBOX_ENABLED", False)
    SANDBOX_BACKEND = os.getenv("SANDBOX_BACKEND", "disabled").strip().lower() or "disabled"
    SANDBOX_GATEWAY_ENABLED = _env_bool("SANDBOX_GATEWAY_ENABLED", False)
    SANDBOX_GATEWAY_URL = os.getenv("SANDBOX_GATEWAY_URL", "").strip()
    SANDBOX_RUNNER_URL = os.getenv("SANDBOX_RUNNER_URL", "").strip()
    SANDBOX_SERVICE_AUTH_REF = os.getenv("SANDBOX_SERVICE_AUTH_REF", "").strip()
    SANDBOX_SERVICE_TOKEN_FILE = os.getenv("SANDBOX_SERVICE_TOKEN_FILE", "").strip()
    SANDBOX_DEFAULT_NETWORK = os.getenv("SANDBOX_DEFAULT_NETWORK", "none").strip().lower() or "none"
    SANDBOX_MAX_CONCURRENT = int(os.getenv("SANDBOX_MAX_CONCURRENT", "2"))
    SANDBOX_MAX_CONCURRENT_PER_USER = int(os.getenv("SANDBOX_MAX_CONCURRENT_PER_USER", "1"))
    SANDBOX_MAX_CONCURRENT_PER_DEPARTMENT = int(
        os.getenv("SANDBOX_MAX_CONCURRENT_PER_DEPARTMENT", "2")
    )
    SANDBOX_TASK_TIMEOUT_SECONDS = int(os.getenv("SANDBOX_TASK_TIMEOUT_SECONDS", "300"))
    SANDBOX_TASK_LEASE_SECONDS = int(os.getenv("SANDBOX_TASK_LEASE_SECONDS", "60"))
    SANDBOX_MAX_OUTPUT_BYTES = int(os.getenv("SANDBOX_MAX_OUTPUT_BYTES", str(50 * 1024 * 1024)))
    SANDBOX_MAX_INPUT_BYTES = int(os.getenv("SANDBOX_MAX_INPUT_BYTES", str(50 * 1024 * 1024)))
    SANDBOX_ALLOW_DYNAMIC_INSTALL = _env_bool("SANDBOX_ALLOW_DYNAMIC_INSTALL", False)
    SANDBOX_AUTHORIZED_CAPABILITIES = _env_csv("SANDBOX_AUTHORIZED_CAPABILITIES")
    SANDBOX_REQUIRE_RUNTIME_ATTESTATION = _env_bool("SANDBOX_REQUIRE_RUNTIME_ATTESTATION", True)
    SANDBOX_RUNTIME_ATTESTATION_REF = os.getenv("SANDBOX_RUNTIME_ATTESTATION_REF", "").strip()
    SANDBOX_RUNTIME_PROFILE_DIGEST = os.getenv("SANDBOX_RUNTIME_PROFILE_DIGEST", "").strip()
    SANDBOX_ARTIFACT_SCANNER_REF = os.getenv("SANDBOX_ARTIFACT_SCANNER_REF", "").strip()
    SANDBOX_DISPATCH_INTERVAL_SECONDS = float(os.getenv("SANDBOX_DISPATCH_INTERVAL_SECONDS", "2"))
    SANDBOX_RECONCILE_INTERVAL_SECONDS = float(os.getenv("SANDBOX_RECONCILE_INTERVAL_SECONDS", "30"))
    SANDBOX_TASK_DB_PATH = _resolve_storage_path(
        os.getenv("SANDBOX_TASK_DB_PATH"), "sandbox_tasks.db"
    )
    SANDBOX_ARTIFACT_STORAGE_DIR = _resolve_storage_path(
        os.getenv("SANDBOX_ARTIFACT_STORAGE_DIR"), "sandbox_artifacts"
    )
    SANDBOX_SKILL_STORAGE_DIR = _resolve_storage_path(
        os.getenv("SANDBOX_SKILL_STORAGE_DIR"), "sandbox_skills"
    )
    SANDBOX_SKILL_PIP_ALLOWED_HOSTS = os.getenv("SANDBOX_SKILL_PIP_ALLOWED_HOSTS", "pypi.org")
    SANDBOX_ARTIFACT_ALLOWED_EXTENSIONS = os.getenv(
        "SANDBOX_ARTIFACT_ALLOWED_EXTENSIONS",
        "json,csv,txt,zip,kicad_sch,kicad_pcb,kicad_pro",
    )
    SKILLS_ENABLED = _env_bool("SKILLS_ENABLED", True)
    MULTI_KB_CHAT_ENABLED = _env_bool("MULTI_KB_CHAT_ENABLED", False)
    MULTI_KB_HUMAN_REVIEW_ENABLED = _env_bool("MULTI_KB_HUMAN_REVIEW_ENABLED", False)
    MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED = _env_bool("MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED", False)
    MULTI_KB_MAX_SOURCES = int(os.getenv("MULTI_KB_MAX_SOURCES", "4"))
    MULTI_KB_PER_SOURCE_TOP_K = int(os.getenv("MULTI_KB_PER_SOURCE_TOP_K", "8"))
    MULTI_KB_PER_RECORD_CANDIDATES = int(os.getenv("MULTI_KB_PER_RECORD_CANDIDATES", "8"))
    MULTI_KB_PER_SOURCE_CANDIDATES = int(os.getenv("MULTI_KB_PER_SOURCE_CANDIDATES", "8"))
    MULTI_KB_MAX_CALLS_PER_TURN = int(os.getenv("MULTI_KB_MAX_CALLS_PER_TURN", "24"))
    MULTI_KB_RETRIEVAL_DEADLINE_SECONDS = float(os.getenv("MULTI_KB_RETRIEVAL_DEADLINE_SECONDS", "30"))
    MULTI_KB_EVIDENCE_MAX_ITEMS = int(os.getenv("MULTI_KB_EVIDENCE_MAX_ITEMS", "24"))
    MULTI_KB_CONTEXT_MAX_TOKENS = int(os.getenv("MULTI_KB_CONTEXT_MAX_TOKENS", "8000"))
    MULTI_KB_FUSION_MAX_BYTES = _multi_kb_byte_limit("MULTI_KB_FUSION_MAX_BYTES")
    MULTI_KB_TURN_EVIDENCE_MAX_BYTES = _multi_kb_byte_limit("MULTI_KB_TURN_EVIDENCE_MAX_BYTES")
    globals().update(_load_evidence_settings())
    RETRIEVAL_EXECUTOR_MAX_WORKERS = int(os.getenv("RETRIEVAL_EXECUTOR_MAX_WORKERS", "8"))
    RETRIEVAL_QUEUE_MAX_DEPTH = int(os.getenv("RETRIEVAL_QUEUE_MAX_DEPTH", "256"))
    RETRIEVAL_PER_REQUEST_FANOUT = int(os.getenv("RETRIEVAL_PER_REQUEST_FANOUT", "4"))
    RETRIEVAL_QUEUE_MAX_WAIT_SECONDS = float(os.getenv("RETRIEVAL_QUEUE_MAX_WAIT_SECONDS", "30"))
    RETRIEVAL_SHUTDOWN_DEADLINE_SECONDS = float(
        os.getenv("RETRIEVAL_SHUTDOWN_DEADLINE_SECONDS", "10")
    )
    ADMISSION_CONTROL_ENABLED = _env_bool("ADMISSION_CONTROL_ENABLED", True)
    ADMISSION_CHAT_RPM_PER_USER = int(os.getenv("ADMISSION_CHAT_RPM_PER_USER", "30"))
    ADMISSION_CHAT_RPM_PER_TENANT = int(os.getenv("ADMISSION_CHAT_RPM_PER_TENANT", "120"))
    ADMISSION_HEAVY_RPM_PER_USER = int(os.getenv("ADMISSION_HEAVY_RPM_PER_USER", "10"))
    ADMISSION_HEAVY_RPM_PER_TENANT = int(os.getenv("ADMISSION_HEAVY_RPM_PER_TENANT", "40"))
    ADMISSION_RETRY_AFTER_SECONDS = float(os.getenv("ADMISSION_RETRY_AFTER_SECONDS", "5"))
    ADMISSION_DEEP_RPM_PER_USER = os.getenv("ADMISSION_DEEP_RPM_PER_USER", "")
    ADMISSION_DEEP_RPM_PER_TENANT = os.getenv("ADMISSION_DEEP_RPM_PER_TENANT", "")
    ADMISSION_EXPORT_RPM_PER_USER = os.getenv("ADMISSION_EXPORT_RPM_PER_USER", "")
    ADMISSION_EXPORT_RPM_PER_TENANT = os.getenv("ADMISSION_EXPORT_RPM_PER_TENANT", "")
    ADMISSION_OCR_RPM_PER_USER = os.getenv("ADMISSION_OCR_RPM_PER_USER", "")
    ADMISSION_OCR_RPM_PER_TENANT = os.getenv("ADMISSION_OCR_RPM_PER_TENANT", "")
    ADMISSION_EVALUATION_RPM_PER_USER = os.getenv("ADMISSION_EVALUATION_RPM_PER_USER", "")
    ADMISSION_EVALUATION_RPM_PER_TENANT = os.getenv("ADMISSION_EVALUATION_RPM_PER_TENANT", "")
    ADMISSION_MODEL_RPM_PER_USER = os.getenv("ADMISSION_MODEL_RPM_PER_USER", "")
    ADMISSION_MODEL_RPM_PER_TENANT = os.getenv("ADMISSION_MODEL_RPM_PER_TENANT", "")
    ADMISSION_MAX_UNTERMINAL_TURNS_PER_SESSION = int(
        os.getenv("ADMISSION_MAX_UNTERMINAL_TURNS_PER_SESSION", "8")
    )
    ADMISSION_MAX_UNTERMINAL_TURNS_PER_USER = int(
        os.getenv("ADMISSION_MAX_UNTERMINAL_TURNS_PER_USER", "32")
    )
    WORKER_PARSE_BATCH_SIZE = int(os.getenv("WORKER_PARSE_BATCH_SIZE", "1"))
    WORKER_CLAIM_BATCH_SIZE = int(os.getenv("WORKER_CLAIM_BATCH_SIZE", "8"))
    WORKER_TURN_CONCURRENCY = int(os.getenv("WORKER_TURN_CONCURRENCY", "1"))
    RESULT_EXPORT_STORAGE_DIR = _resolve_storage_path(os.getenv("RESULT_EXPORT_STORAGE_DIR"), "exports")
    RESULT_EXPORT_ENABLED = _env_bool("RESULT_EXPORT_ENABLED", True)
    RESULT_EXPORT_MD_ENABLED = _env_bool("RESULT_EXPORT_MD_ENABLED", True)
    RESULT_EXPORT_XLSX_ENABLED = _env_bool("RESULT_EXPORT_XLSX_ENABLED", True)
    RESULT_EXPORT_DOCX_ENABLED = _env_bool("RESULT_EXPORT_DOCX_ENABLED", True)
    RESULT_EXPORT_PDF_ENABLED = _env_bool("RESULT_EXPORT_PDF_ENABLED", True)
    RESULT_EXPORT_PPTX_ENABLED = _env_bool("RESULT_EXPORT_PPTX_ENABLED", True)
    RESULT_EXPORT_ORGANIZE_ENABLED = _env_bool("RESULT_EXPORT_ORGANIZE_ENABLED", True)
    RESULT_EXPORT_MAX_BYTES = int(os.getenv("RESULT_EXPORT_MAX_BYTES", str(25 * 1024 * 1024)))
    RESULT_EXPORT_JOB_LEASE_SECONDS = int(os.getenv("RESULT_EXPORT_JOB_LEASE_SECONDS", "300"))
    RESULT_EXPORT_JOB_BATCH_SIZE = int(os.getenv("RESULT_EXPORT_JOB_BATCH_SIZE", "8"))
    RESULT_EXPORT_RETENTION_DAYS = int(os.getenv("RESULT_EXPORT_RETENTION_DAYS", "30"))
    RESULT_EXPORT_MAX_RUNNING_JOBS = int(os.getenv("RESULT_EXPORT_MAX_RUNNING_JOBS", "4"))
    RESULT_EXPORT_ORGANIZE_MAX_CHARS = int(os.getenv("RESULT_EXPORT_ORGANIZE_MAX_CHARS", "40000"))
    CHAT_TURN_HEARTBEAT_TTL_SECONDS = int(os.getenv("CHAT_TURN_HEARTBEAT_TTL_SECONDS", "90"))
    CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS", "10"))
    CONVERSATION_SERIALIZE_SAME_THREAD = os.getenv(
        "CONVERSATION_SERIALIZE_SAME_THREAD", "false"
    ).lower() in {"1", "true", "yes", "on"}
    CHAT_TURN_LEASE_SECONDS = int(os.getenv("CHAT_TURN_LEASE_SECONDS", "120"))
    CHAT_TURN_LEASE_RENEW_SECONDS = int(os.getenv("CHAT_TURN_LEASE_RENEW_SECONDS", "15"))
    WAITING_TURN_MAX_SECONDS = int(os.getenv("WAITING_TURN_MAX_SECONDS", "600"))
    WAITING_TURN_TIMEOUT_SCAN_SECONDS = float(os.getenv("WAITING_TURN_TIMEOUT_SCAN_SECONDS", "5"))
    EVALUATION_DATASET_MAX_BYTES = int(os.getenv("EVALUATION_DATASET_MAX_BYTES", str(100 * 1024 * 1024)))
    CHAT_ATTACHMENTS_ENABLED = os.getenv("CHAT_ATTACHMENTS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    CHAT_ATTACHMENT_STORAGE_DIR = _resolve_storage_path(
        os.getenv("CHAT_ATTACHMENT_STORAGE_DIR"), "chat_attachments"
    )
    CHAT_ATTACHMENT_INDEX_DB_PATH = _resolve_storage_path(
        os.getenv("CHAT_ATTACHMENT_INDEX_DB_PATH"), "chat_attachments.db"
    )
    CHAT_ATTACHMENT_MAX_BYTES = int(os.getenv("CHAT_ATTACHMENT_MAX_BYTES", str(50 * 1024 * 1024)))
    CHAT_ATTACHMENT_MAX_FILES_PER_SESSION = int(os.getenv("CHAT_ATTACHMENT_MAX_FILES_PER_SESSION", "20"))
    CHAT_ATTACHMENT_MAX_SESSION_BYTES = int(os.getenv("CHAT_ATTACHMENT_MAX_SESSION_BYTES", str(500 * 1024 * 1024)))
    CHAT_ATTACHMENT_MAX_TENANT_BYTES = int(os.getenv("CHAT_ATTACHMENT_MAX_TENANT_BYTES", str(5 * 1024 * 1024 * 1024)))
    CHAT_ATTACHMENT_MAX_PAGES = int(os.getenv("CHAT_ATTACHMENT_MAX_PAGES", "300"))
    CHAT_ATTACHMENT_MAX_ROWS = int(os.getenv("CHAT_ATTACHMENT_MAX_ROWS", "200000"))
    CHAT_ATTACHMENT_MAX_CELLS = int(os.getenv("CHAT_ATTACHMENT_MAX_CELLS", "2000000"))
    CHAT_ATTACHMENT_MAX_SHEETS = int(os.getenv("CHAT_ATTACHMENT_MAX_SHEETS", "100"))
    CHAT_ATTACHMENT_MAX_SHARED_STRINGS = int(os.getenv("CHAT_ATTACHMENT_MAX_SHARED_STRINGS", "1000000"))
    CHAT_ATTACHMENT_MAX_CELL_TEXT_LENGTH = int(os.getenv("CHAT_ATTACHMENT_MAX_CELL_TEXT_LENGTH", "100000"))
    CHAT_ATTACHMENT_MAX_UNCOMPRESSED_BYTES = int(os.getenv("CHAT_ATTACHMENT_MAX_UNCOMPRESSED_BYTES", str(200 * 1024 * 1024)))
    CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS = float(os.getenv("CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS", "120"))
    CHAT_ATTACHMENT_RETENTION_SECONDS = int(os.getenv("CHAT_ATTACHMENT_RETENTION_SECONDS", str(30 * 24 * 3600)))
    CHAT_ATTACHMENT_CONTEXT_MAX_TOKENS = int(os.getenv("CHAT_ATTACHMENT_CONTEXT_MAX_TOKENS", "8000"))
    CHAT_ATTACHMENT_CONTEXT_RATIO = float(os.getenv("CHAT_ATTACHMENT_CONTEXT_RATIO", "0.3"))
    CHAT_ATTACHMENT_FTS_ENABLED = os.getenv("CHAT_ATTACHMENT_FTS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    CHAT_ATTACHMENT_TRIGRAM_ENABLED = os.getenv("CHAT_ATTACHMENT_TRIGRAM_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    CHAT_ATTACHMENT_DENSE_ENABLED = os.getenv("CHAT_ATTACHMENT_DENSE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    CHAT_ATTACHMENT_RETRIEVAL_MODE = os.getenv("CHAT_ATTACHMENT_RETRIEVAL_MODE", "lexical").strip().lower()
    CHAT_ATTACHMENT_EMBEDDING_PROVIDER = os.getenv("CHAT_ATTACHMENT_EMBEDDING_PROVIDER", "openai_compatible").strip()
    CHAT_ATTACHMENT_EMBEDDING_BASE_URL = os.getenv("CHAT_ATTACHMENT_EMBEDDING_BASE_URL", "").strip()
    CHAT_ATTACHMENT_EMBEDDING_MODEL = os.getenv("CHAT_ATTACHMENT_EMBEDDING_MODEL", "").strip()
    CHAT_ATTACHMENT_EMBEDDING_API_KEY = os.getenv("CHAT_ATTACHMENT_EMBEDDING_API_KEY", "").strip()
    CHAT_ATTACHMENT_DENSE_MAX_PARTS = int(os.getenv("CHAT_ATTACHMENT_DENSE_MAX_PARTS", "4000"))
    CHAT_ATTACHMENT_OCR_ENABLED = os.getenv("CHAT_ATTACHMENT_OCR_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    CHAT_ATTACHMENT_OCR_MAX_PAGES = int(os.getenv("CHAT_ATTACHMENT_OCR_MAX_PAGES", "10"))
    CHAT_ATTACHMENT_VISUAL_ENABLED = os.getenv("CHAT_ATTACHMENT_VISUAL_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED = os.getenv("CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED", "false").lower() in {"1", "true", "yes", "on"}
    CHAT_ATTACHMENT_VISUAL_PROVIDER = os.getenv("CHAT_ATTACHMENT_VISUAL_PROVIDER", "volcengine_ark").strip()
    CHAT_ATTACHMENT_VISUAL_BASE_URL = os.getenv(
        "CHAT_ATTACHMENT_VISUAL_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
    ).strip()
    CHAT_ATTACHMENT_VISUAL_MODEL = os.getenv("CHAT_ATTACHMENT_VISUAL_MODEL", "").strip()
    CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN = int(
        os.getenv("CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN", "3")
    )
    CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES = int(
        os.getenv("CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES", str(8 * 1024 * 1024))
    )
    CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS = int(
        os.getenv("CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS", "60")
    )
    ARK_API_KEY = os.getenv("ARK_API_KEY", "").strip()
    CHAT_ATTACHMENT_JOB_LEASE_SECONDS = int(os.getenv("CHAT_ATTACHMENT_JOB_LEASE_SECONDS", "300"))
    CHAT_ATTACHMENT_JOB_MAX_ATTEMPTS = int(os.getenv("CHAT_ATTACHMENT_JOB_MAX_ATTEMPTS", "3"))
    EXTERNAL_CONVERSATION_LLM_STRUCTURE = os.getenv("EXTERNAL_CONVERSATION_LLM_STRUCTURE", "true").lower() in {"1", "true", "yes", "on"}
    EXTERNAL_CONVERSATION_LLM_MAX_CHARS = int(os.getenv("EXTERNAL_CONVERSATION_LLM_MAX_CHARS", "12000"))
    EXTERNAL_CONVERSATION_LLM_SUMMARY = os.getenv("EXTERNAL_CONVERSATION_LLM_SUMMARY", "true").lower() in {"1", "true", "yes", "on"}
    EXTERNAL_CONVERSATION_LLM_TIMEOUT_SECONDS = int(os.getenv("EXTERNAL_CONVERSATION_LLM_TIMEOUT_SECONDS", "60"))
    MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    MEMORY_STORE_BACKEND = os.getenv("MEMORY_STORE_BACKEND", "sqlite").strip().lower()
    MEMORY_DATABASE_URL = os.getenv("MEMORY_DATABASE_URL", "")
    MEMORY_SQLITE_PATH = _resolve_storage_path(os.getenv("MEMORY_SQLITE_PATH"), "memory.db")
    MEMORY_SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("MEMORY_SQLITE_BUSY_TIMEOUT_MS", "30000"))
    MEMORY_SINGLE_WRITER = os.getenv("MEMORY_SINGLE_WRITER", "true").lower() in {"1", "true", "yes", "on"}
    MEMORY_EXTRACTION_ENABLED = os.getenv("MEMORY_EXTRACTION_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    MEMORY_DEBOUNCE_SECONDS = int(os.getenv("MEMORY_DEBOUNCE_SECONDS", "300"))
    MEMORY_JOB_LEASE_SECONDS = int(os.getenv("MEMORY_JOB_LEASE_SECONDS", "180"))
    MEMORY_JOB_MAX_RETRIES = int(os.getenv("MEMORY_JOB_MAX_RETRIES", "5"))
    MEMORY_REFLECTION_TIMEOUT_SECONDS = int(os.getenv("MEMORY_REFLECTION_TIMEOUT_SECONDS", "120"))
    MEMORY_REFLECTION_MIN_INTERVAL_SECONDS = float(os.getenv("MEMORY_REFLECTION_MIN_INTERVAL_SECONDS", "0"))
    MEMORY_REFLECTION_CIRCUIT_FAILURES = int(os.getenv("MEMORY_REFLECTION_CIRCUIT_FAILURES", "5"))
    MEMORY_REFLECTION_CIRCUIT_COOLDOWN_SECONDS = int(os.getenv("MEMORY_REFLECTION_CIRCUIT_COOLDOWN_SECONDS", "60"))
    MEMORY_PROJECTION_MAX_RETRIES = int(os.getenv("MEMORY_PROJECTION_MAX_RETRIES", "5"))
    MEMORY_RECONCILE_INTERVAL_SECONDS = int(os.getenv("MEMORY_RECONCILE_INTERVAL_SECONDS", "3600"))
    MEMORY_USER_TOP_K = int(os.getenv("MEMORY_USER_TOP_K", "3"))
    MEMORY_PROJECT_TOP_K = int(os.getenv("MEMORY_PROJECT_TOP_K", "5"))
    MEMORY_STORE_OVERSAMPLE_FACTOR = int(os.getenv("MEMORY_STORE_OVERSAMPLE_FACTOR", "4"))
    MEMORY_STORE_MAX_SCAN = int(os.getenv("MEMORY_STORE_MAX_SCAN", "100"))
    MEMORY_CONTEXT_MAX_TOKENS = int(os.getenv("MEMORY_CONTEXT_MAX_TOKENS", "1800"))
    MEMORY_ITEM_MAX_TOKENS = int(os.getenv("MEMORY_ITEM_MAX_TOKENS", "350"))
    MEMORY_MIN_SCORE_ENABLED = os.getenv("MEMORY_MIN_SCORE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    MEMORY_MIN_SCORE = os.getenv("MEMORY_MIN_SCORE", "")
    MEMORY_MODEL_PROVIDER = os.getenv("MEMORY_MODEL_PROVIDER", "")
    MEMORY_MODEL = os.getenv("MEMORY_MODEL", "")
    MEMORY_MODEL_BASE_URL = os.getenv("MEMORY_MODEL_BASE_URL", "")
    MEMORY_MODEL_API_KEY = os.getenv("MEMORY_MODEL_API_KEY", "")
    MEMORY_MODEL_REQUIRE_STRUCTURED_TOOLS = os.getenv("MEMORY_MODEL_REQUIRE_STRUCTURED_TOOLS", "true").lower() in {"1", "true", "yes", "on"}
    MEMORY_EMBEDDING_PROVIDER = os.getenv("MEMORY_EMBEDDING_PROVIDER", "")
    MEMORY_EMBEDDING_MODEL = os.getenv("MEMORY_EMBEDDING_MODEL", "")
    MEMORY_EMBEDDING_BASE_URL = os.getenv("MEMORY_EMBEDDING_BASE_URL", "")
    MEMORY_EMBEDDING_API_KEY = os.getenv("MEMORY_EMBEDDING_API_KEY", "")
    MEMORY_EMBEDDING_DIMS = os.getenv("MEMORY_EMBEDDING_DIMS", "")
    MEMORY_EMBEDDING_INDEX_KIND = os.getenv("MEMORY_EMBEDDING_INDEX_KIND", "").strip().lower()
    MEMORY_INDEX_FIELDS = os.getenv("MEMORY_INDEX_FIELDS", "content.content,content.title,content.subject")
    MEMORY_ALLOW_GENERAL_CHAT_EXTRACTION = os.getenv("MEMORY_ALLOW_GENERAL_CHAT_EXTRACTION", "false").lower() in {"1", "true", "yes", "on"}
    MEMORY_USER_MEMORY_OPT_IN = os.getenv("MEMORY_USER_MEMORY_OPT_IN", "false").lower() in {"1", "true", "yes", "on"}
    MEMORY_USER_MEMORY_REQUIRE_PER_EVENT_CONSENT = os.getenv("MEMORY_USER_MEMORY_REQUIRE_PER_EVENT_CONSENT", "true").lower() in {"1", "true", "yes", "on"}
    MEMORY_RETENTION_DAYS = os.getenv("MEMORY_RETENTION_DAYS", "")
    OBS_ENABLED = os.getenv("OBS_ENABLED", "true").lower() == "true"
    OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "hardware-database-api")
    OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    OBS_ENVIRONMENT = os.getenv("OBS_ENVIRONMENT", "development")
    OBS_SERVICE_VERSION = os.getenv("OBS_SERVICE_VERSION", "0.1.0")
    OBS_TRACE_SAMPLE_RATIO = float(os.getenv("OBS_TRACE_SAMPLE_RATIO", "1.0"))
    OBS_CAPTURE_CONTENT = os.getenv("OBS_CAPTURE_CONTENT", "false").lower() == "true"
    OBS_CAPTURE_QUERY = os.getenv("OBS_CAPTURE_QUERY", "false").lower() == "true"
    OBS_CAPTURE_EVIDENCE = os.getenv("OBS_CAPTURE_EVIDENCE", "false").lower() == "true"
    OBS_CAPTURE_LLM_CONTENT = os.getenv("OBS_CAPTURE_LLM_CONTENT", "false").lower() == "true"
    OBS_CAPTURE_MCP_CONTENT = os.getenv("OBS_CAPTURE_MCP_CONTENT", "false").lower() == "true"
    OBS_CONTENT_MAX_CHARS = max(1000, int(os.getenv("OBS_CONTENT_MAX_CHARS", "50000")))
    OBS_LOG_FORMAT = os.getenv("OBS_LOG_FORMAT", "json")
    OBS_METRICS_ENABLED = os.getenv("OBS_METRICS_ENABLED", "true").lower() == "true"
    OBS_TRACES_ENABLED = os.getenv("OBS_TRACES_ENABLED", "true").lower() == "true"
    OBS_LOGS_ENABLED = os.getenv("OBS_LOGS_ENABLED", "true").lower() == "true"
    OBS_PHOENIX_PROJECT = os.getenv("OBS_PHOENIX_PROJECT", "hardware-database")
    OBS_GRAFANA_BASE_URL = os.getenv("OBS_GRAFANA_BASE_URL", "")
    OBS_PHOENIX_BASE_URL = os.getenv("OBS_PHOENIX_BASE_URL", "")
    OBS_WORKER_HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("OBS_WORKER_HEARTBEAT_INTERVAL_SECONDS", "10"))
    OBS_WORKER_STALE_SECONDS = int(os.getenv("OBS_WORKER_STALE_SECONDS", "30"))
    OBS_DEPENDENCY_TIMEOUT_SECONDS = float(os.getenv("OBS_DEPENDENCY_TIMEOUT_SECONDS", "5"))
    OBS_READINESS_CACHE_SECONDS = float(os.getenv("OBS_READINESS_CACHE_SECONDS", "1"))
    SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", DEFAULT_VALUES["SYSTEM_PROMPT"])
    NO_CONTEXT_PROMPT = os.getenv("NO_CONTEXT_PROMPT", DEFAULT_VALUES["NO_CONTEXT_PROMPT"])
    ensure_runtime_dirs()


def _format_env_value(value: str) -> str:
    """Format a .env value while preserving spaces and newlines safely."""
    value = str(value)
    if "#" in value or '"' in value or " " in value or "\n" in value:
        escaped = (
            value
            .replace("\\", "\\\\")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", "\\n")
            .replace('"', '\\"')
        )
        return f'"{escaped}"'
    return value


def _env_value_quote_closed(value: str) -> bool:
    """Return whether a quoted .env value closes on the current physical line."""
    value = value.lstrip()
    if not value or value[0] not in ('"', "'"):
        return True

    quote = value[0]
    escaped = False
    for char in value[1:]:
        if escaped:
            escaped = False
            continue
        if quote == '"' and char == "\\":
            escaped = True
            continue
        if char == quote:
            return True
    return False


def save_settings_to_env(settings_dict: dict, env_path: str = None):
    """Update .env keys while preserving comments and unrelated settings.

    Atomic + lock-guarded: writes to a temp file in the same directory and
    ``os.replace``s it over the target so a crash mid-write can never leave a
    truncated .env (which holds the live API keys). Callers should hold this
    via the module-level lock; concurrent PUT /config requests serialise here.
    """
    global CONFIG_REVISION
    if env_path is None:
        env_path = ENV_FILE_PATH

    with _ENV_WRITE_LOCK:
        lines = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding=ENV_FILE_ENCODING) as f:
                lines = f.readlines()

        updated_keys = set()
        new_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                i += 1
                continue

            if "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in settings_dict:
                    new_lines.append(f"{key}={_format_env_value(settings_dict[key])}\n")
                    updated_keys.add(key)
                    value = stripped.split("=", 1)[1]
                    i += 1
                    while i < len(lines) and not _env_value_quote_closed(value):
                        value += "\n" + lines[i].rstrip("\n")
                        i += 1
                    continue
                new_lines.append(line)
                i += 1
                continue

            i += 1

        for key, value in settings_dict.items():
            if key not in updated_keys:
                new_lines.append(f"{key}={_format_env_value(value)}\n")

        import tempfile

        env_dir = os.path.dirname(os.path.abspath(env_path)) or "."
        fd, tmp_path = tempfile.mkstemp(prefix=".env.tmp-", dir=env_dir)
        try:
            with os.fdopen(fd, "w", encoding=ENV_FILE_ENCODING) as f:
                f.writelines(new_lines)
                f.flush()
                os.fsync(f.fileno())
            # mkstemp creates the temp file 0600; os.replace would silently
            # tighten the .env mode and lock out other-user processes
            # (e.g. workers). Keep the target's current mode here; the API
            # startup hook (api/app.py _harden_storage_permissions) is what
            # converges .env to owner-only 0600 at boot.
            try:
                mode = stat.S_IMODE(os.stat(env_path).st_mode)
            except OSError:
                mode = None
            if mode is not None:
                os.chmod(tmp_path, mode)
            os.replace(tmp_path, env_path)
            if os.path.abspath(env_path) == os.path.abspath(ENV_FILE_PATH):
                CONFIG_REVISION = _config_revision(env_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


# Keys parsed as numbers or an enum at import/reload time. A value that cannot
# be converted would brick the server on next boot (module-level parse raises),
# so callers must validate BEFORE persisting anything to .env.
_INT_SETTING_KEYS = frozenset({
    "RAGFLOW_TIMEOUT_SECONDS",
    "RAGFLOW_CHUNK_LIST_PAGE_SIZE",
    "RAGFLOW_CHUNK_LIST_MAX_PAGES",
    "RAGFLOW_CHUNK_LIST_MAX_BYTES",
    "RAGFLOW_STATUS_REFRESH_MIN_SECONDS",
    "RAGFLOW_RETRIEVAL_DOC_IDS_MAX",
    "AUTH_SESSION_TTL_HOURS",
    "AGENT_CUSTOM_MAX_TOKENS",
    "AGENT_MODEL_MAX_INPUT_TOKENS",
    "AGENT_TIMEOUT_SECONDS",
    "AGENT_RATE_LIMIT_MAX_RETRIES",
    "FINAL_TOP_K",
    "AGENT_MAX_RETRIEVAL_ROUNDS",
    "AGENT_DEEP_TOOL_CALL_CAP",
    "AGENT_DEEP_RETRIEVAL_CALL_CAP",
    "AGENT_DEEP_FINALIZE_RESERVE",
    "AGENT_DEEP_LOOP_SECONDS",
    "CHAT_QUEUE_DEADLINE_DEEP_SECONDS",
    "AGENT_CHECKPOINT_POOL_SIZE",
    "LLM_MAX_CONCURRENCY",
    "LLM_BATCH_MAX_CONCURRENCY",
    "LLM_QUEUE_TIMEOUT_SECONDS",
    "LLM_QUEUE_WARN_SECONDS",
    "LLM_STREAM_IDLE_TIMEOUT_SECONDS",
    "WORKER_PARSE_BATCH_SIZE",
    "WORKER_CLAIM_BATCH_SIZE",
    "WORKER_TURN_CONCURRENCY",
    "RESULT_EXPORT_MAX_BYTES",
    "RESULT_EXPORT_JOB_LEASE_SECONDS",
    "RESULT_EXPORT_JOB_BATCH_SIZE",
    "RESULT_EXPORT_RETENTION_DAYS",
    "RESULT_EXPORT_MAX_RUNNING_JOBS",
    "RESULT_EXPORT_ORGANIZE_MAX_CHARS",
    "CHAT_TURN_HEARTBEAT_TTL_SECONDS",
    "CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS",
    "CHAT_TURN_LEASE_SECONDS",
    "CHAT_TURN_LEASE_RENEW_SECONDS",
    "WAITING_TURN_MAX_SECONDS",
    "EVALUATION_DATASET_MAX_BYTES",
    "RESOURCE_QUEUE_MAX_DEPTH",
    "RESOURCE_PERMIT_LEASE_SECONDS",
    "CHAT_QUEUE_DEADLINE_SECONDS",
    "CONTROL_PLANE_POOL_SIZE",
    "CANCEL_KEY_TTL_SECONDS",
    "TURN_EVENT_STREAM_TTL_SECONDS",
    "TURN_EVENT_STREAM_MAXLEN",
    "DISTRIBUTED_ADMISSION_TTL_SECONDS",
    "PROVIDER_RPM_LIMIT",
    "PROVIDER_TPM_LIMIT",
    "ADMISSION_MAX_UNTERMINAL_TURNS_PER_SESSION",
    "ADMISSION_MAX_UNTERMINAL_TURNS_PER_USER",
    "RESOURCE_LLM_MAX_CONCURRENT",
    "RESOURCE_LLM_RESERVED_INTERACTIVE",
    "RESOURCE_RAG_MAX_CONCURRENT",
    "RESOURCE_RAG_RESERVED_INTERACTIVE",
    "RESOURCE_TOOL_MAX_CONCURRENT",
    "RESOURCE_TOOL_RESERVED_INTERACTIVE",
    "RESOURCE_CHAT_MAX_CONCURRENT",
    "RESOURCE_CHAT_RESERVED_INTERACTIVE",
    "RESOURCE_HEAVY_JOB_MAX_CONCURRENT",
    "RESOURCE_HEAVY_JOB_RESERVED_INTERACTIVE",
    "RESOURCE_OCR_MAX_CONCURRENT",
    "RESOURCE_OCR_RESERVED_INTERACTIVE",
    "RESOURCE_EXPORT_MAX_CONCURRENT",
    "RESOURCE_EXPORT_RESERVED_INTERACTIVE",
    "RESOURCE_MEMORY_MAX_CONCURRENT",
    "RESOURCE_MEMORY_RESERVED_INTERACTIVE",
    "MCP_CONNECT_TIMEOUT_SECONDS",
    "MCP_CALL_TIMEOUT_SECONDS",
    "MCP_SSE_READ_TIMEOUT_SECONDS",
    "MCP_MAX_RESULT_CHARS",
    "MCP_MAX_RESULT_BYTES",
    "MCP_MAX_ARGUMENT_CHARS",
    "MCP_MAX_TOOLS_PER_SERVER",
    "MCP_MAX_SERVERS_PER_RUN",
    "MCP_CALLS_PER_TURN",
    "MCP_FAILURE_COOLDOWN_SECONDS",
    "MCP_WRITE_CONFIRM_TTL_SECONDS",
    "MCP_MAX_ARTIFACT_BYTES",
    "RESOURCE_MCP_MAX_CONCURRENT",
    "RESOURCE_MCP_RESERVED_INTERACTIVE",
    "SANDBOX_MAX_CONCURRENT",
    "SANDBOX_MAX_CONCURRENT_PER_USER",
    "SANDBOX_MAX_CONCURRENT_PER_DEPARTMENT",
    "SANDBOX_TASK_TIMEOUT_SECONDS",
    "SANDBOX_TASK_LEASE_SECONDS",
    "SANDBOX_MAX_OUTPUT_BYTES",
    "SANDBOX_MAX_INPUT_BYTES",
    "RETRIEVAL_EXECUTOR_MAX_WORKERS",
    "MULTI_KB_MAX_SOURCES",
    "MULTI_KB_PER_SOURCE_TOP_K",
    "MULTI_KB_PER_RECORD_CANDIDATES",
    "MULTI_KB_PER_SOURCE_CANDIDATES",
    "MULTI_KB_MAX_CALLS_PER_TURN",
    "MULTI_KB_EVIDENCE_MAX_ITEMS",
    "MULTI_KB_CONTEXT_MAX_TOKENS",
    "MULTI_KB_FUSION_MAX_BYTES",
    "MULTI_KB_TURN_EVIDENCE_MAX_BYTES",
    "RETRIEVAL_QUEUE_MAX_DEPTH",
    "RETRIEVAL_PER_REQUEST_FANOUT",
    "ADMISSION_CHAT_RPM_PER_USER",
    "ADMISSION_CHAT_RPM_PER_TENANT",
    "ADMISSION_HEAVY_RPM_PER_USER",
    "ADMISSION_HEAVY_RPM_PER_TENANT",
    "CHAT_ATTACHMENT_MAX_BYTES",
    "CHAT_ATTACHMENT_MAX_FILES_PER_SESSION",
    "CHAT_ATTACHMENT_MAX_SESSION_BYTES",
    "CHAT_ATTACHMENT_MAX_TENANT_BYTES",
    "CHAT_ATTACHMENT_MAX_PAGES",
    "CHAT_ATTACHMENT_MAX_ROWS",
    "CHAT_ATTACHMENT_MAX_CELLS",
    "CHAT_ATTACHMENT_MAX_SHEETS",
    "CHAT_ATTACHMENT_MAX_SHARED_STRINGS",
    "CHAT_ATTACHMENT_MAX_CELL_TEXT_LENGTH",
    "CHAT_ATTACHMENT_MAX_UNCOMPRESSED_BYTES",
    "CHAT_ATTACHMENT_RETENTION_SECONDS",
    "CHAT_ATTACHMENT_CONTEXT_MAX_TOKENS",
    "CHAT_ATTACHMENT_DENSE_MAX_PARTS",
    "CHAT_ATTACHMENT_OCR_MAX_PAGES",
    "CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN",
    "CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES",
    "CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS",
    "CHAT_ATTACHMENT_JOB_LEASE_SECONDS",
    "CHAT_ATTACHMENT_JOB_MAX_ATTEMPTS",
    "MEMORY_SQLITE_BUSY_TIMEOUT_MS",
    "MEMORY_DEBOUNCE_SECONDS",
    "MEMORY_JOB_LEASE_SECONDS",
    "MEMORY_JOB_MAX_RETRIES",
    "MEMORY_REFLECTION_TIMEOUT_SECONDS",
    "MEMORY_REFLECTION_CIRCUIT_FAILURES",
    "MEMORY_REFLECTION_CIRCUIT_COOLDOWN_SECONDS",
    "MEMORY_PROJECTION_MAX_RETRIES",
    "MEMORY_RECONCILE_INTERVAL_SECONDS",
    "MEMORY_USER_TOP_K",
    "MEMORY_PROJECT_TOP_K",
    "MEMORY_STORE_OVERSAMPLE_FACTOR",
    "MEMORY_STORE_MAX_SCAN",
    "MEMORY_CONTEXT_MAX_TOKENS",
    "MEMORY_ITEM_MAX_TOKENS",
} | EVIDENCE_INT_SETTING_KEYS)
_FLOAT_SETTING_KEYS = frozenset({
    "MULTI_KB_RETRIEVAL_DEADLINE_SECONDS",
    "RAGFLOW_SIMILARITY_THRESHOLD",
    "RAGFLOW_VECTOR_WEIGHT",
    "AGENT_TEMPERATURE",
    "AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS",
    "AGENT_RATE_LIMIT_MAX_DELAY_SECONDS",
    "WORKER_POLL_INTERVAL_SECONDS",
    "MEMORY_REFLECTION_MIN_INTERVAL_SECONDS",
    "CHAT_ATTACHMENT_CONTEXT_RATIO",
    "CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS",
    "RESOURCE_QUEUE_MAX_WAIT_SECONDS",
    "WAITING_TURN_TIMEOUT_SCAN_SECONDS",
    "RETRIEVAL_QUEUE_MAX_WAIT_SECONDS",
    "RETRIEVAL_SHUTDOWN_DEADLINE_SECONDS",
    "ADMISSION_RETRY_AFTER_SECONDS",
    "SANDBOX_DISPATCH_INTERVAL_SECONDS",
    "SANDBOX_RECONCILE_INTERVAL_SECONDS",
})


def validate_settings_values(settings_dict: dict) -> None:
    """Validate candidate .env values against their expected types.

    Raises ``ValueError`` naming the offending key when a value cannot be
    parsed the way ``reload_settings`` (and module import) will parse it.
    """
    for key, value in settings_dict.items():
        text = "" if value is None else str(value)
        if key in _INT_SETTING_KEYS:
            if key in {"MULTI_KB_FUSION_MAX_BYTES", "MULTI_KB_TURN_EVIDENCE_MAX_BYTES"} and not text.strip():
                continue
            try:
                parsed_int = int(text)
            except ValueError as exc:
                raise ValueError(f"{key} 必须是整数，当前值: {text!r}") from exc
            if key in ({
                "SANDBOX_MAX_CONCURRENT",
                "SANDBOX_MAX_CONCURRENT_PER_USER",
                "SANDBOX_MAX_CONCURRENT_PER_DEPARTMENT",
                "SANDBOX_TASK_TIMEOUT_SECONDS",
                "SANDBOX_TASK_LEASE_SECONDS",
                "SANDBOX_MAX_OUTPUT_BYTES",
                "SANDBOX_MAX_INPUT_BYTES",
                "MULTI_KB_MAX_SOURCES",
                "MULTI_KB_PER_SOURCE_TOP_K",
                "MULTI_KB_PER_RECORD_CANDIDATES",
                "MULTI_KB_PER_SOURCE_CANDIDATES",
                "MULTI_KB_MAX_CALLS_PER_TURN",
                "MULTI_KB_EVIDENCE_MAX_ITEMS",
                "MULTI_KB_CONTEXT_MAX_TOKENS",
                "MULTI_KB_FUSION_MAX_BYTES",
                "MULTI_KB_TURN_EVIDENCE_MAX_BYTES",
                "RAGFLOW_CHUNK_LIST_PAGE_SIZE",
                "RAGFLOW_CHUNK_LIST_MAX_PAGES",
                "RAGFLOW_CHUNK_LIST_MAX_BYTES",
            } | EVIDENCE_POSITIVE_SETTING_KEYS) and parsed_int <= 0:
                raise ValueError(f"{key} 必须大于 0，当前值: {text!r}")
        elif key in _FLOAT_SETTING_KEYS:
            try:
                parsed_float = float(text)
            except ValueError as exc:
                raise ValueError(f"{key} 必须是数字，当前值: {text!r}") from exc
            if key == "MULTI_KB_RETRIEVAL_DEADLINE_SECONDS":
                import math

                if not math.isfinite(parsed_float) or parsed_float <= 0:
                    raise ValueError(f"{key} 必须为大于 0 的有限数字")
        elif key == "MEMORY_MIN_SCORE" and text.strip():
            try:
                float(text)
            except ValueError as exc:
                raise ValueError(f"{key} 必须是数字或空值，当前值: {text!r}") from exc
        elif key == "AGENT_LLM_PROVIDER":
            valid = ", ".join(p.value for p in Provider)
            lowered = text.strip().lower()
            if lowered not in {p.value for p in Provider}:
                raise ValueError(f"AGENT_LLM_PROVIDER 必须是以下之一: {valid}，当前值: {text!r}")
        elif key == "SANDBOX_BACKEND" and text.strip().lower() not in {"disabled", "runner_api"}:
            raise ValueError(
                "SANDBOX_BACKEND 必须是 disabled 或 runner_api，"
                f"当前值: {text!r}"
            )
        elif key == "SANDBOX_DEFAULT_NETWORK" and text.strip().lower() not in {
            "none",
            "egress_proxy",
        }:
            raise ValueError(
                "SANDBOX_DEFAULT_NETWORK 必须是 none 或 egress_proxy，"
                f"当前值: {text!r}"
            )
        elif key == "MEMORY_STORE_BACKEND" and text.strip().lower() not in {"sqlite", "postgres"}:
            raise ValueError(f"MEMORY_STORE_BACKEND 必须是 sqlite 或 postgres，当前值: {text!r}")
        elif key == "AGENT_CHECKPOINT_BACKEND" and text.strip().lower() not in {"sqlite", "postgres"}:
            raise ValueError(
                f"AGENT_CHECKPOINT_BACKEND 必须是 sqlite 或 postgres，当前值: {text!r}"
            )
        elif key in {"MEMORY_EMBEDDING_DIMS", "MEMORY_RETENTION_DAYS"} and text.strip():
            try:
                int(text)
            except ValueError as exc:
                raise ValueError(f"{key} 必须是整数或空值，当前值: {text!r}") from exc


def validate_provider_configuration(overrides: dict | None = None) -> None:
    """P0-008: reject provider selections that cannot run in this deployment.

    Per-key validation cannot express cross-field requirements, so this guard
    runs at config-save time *and* at process startup.  Selecting a PostgreSQL
    backend never silently falls back to a local empty database: the explicit
    connection target must be present, or the process refuses to start.
    """
    def _value(key: str) -> str:
        if overrides and key in overrides and overrides[key] is not None:
            return str(overrides[key]).strip()
        current = globals().get(key)
        if current not in (None, ""):
            return str(current).strip()
        return str(DEFAULT_VALUES.get(key, "") or "").strip()

    agent_backend = _value("AGENT_CHECKPOINT_BACKEND").lower() or "sqlite"
    if agent_backend not in {"sqlite", "postgres"}:
        raise ValueError(f"AGENT_CHECKPOINT_BACKEND 必须是 sqlite 或 postgres，当前值: {agent_backend!r}")
    if agent_backend == "postgres" and not _value("AGENT_CHECKPOINT_DATABASE_URL"):
        raise ValueError(
            "AGENT_CHECKPOINT_BACKEND=postgres 需要显式配置 AGENT_CHECKPOINT_DATABASE_URL，"
            "否则会静默创建本地空库"
        )

    # 本地台账域（存储收敛）：显式 postgres 必须给出可达目标；空值跟随
    # CONTROL_PLANE_*，由 resolve_domain_backend 在运行时解析。
    for domain_prefix in ("PIPELINE_DOCUMENT", "AUTH_STORE", "EVALUATION_STORE"):
        domain_backend = _value(f"{domain_prefix}_BACKEND").lower()
        if domain_backend and domain_backend not in {"sqlite", "postgres"}:
            raise ValueError(
                f"{domain_prefix}_BACKEND 必须是 sqlite 或 postgres，当前值: {domain_backend!r}"
            )
        if domain_backend == "postgres" and not (
            _value(f"{domain_prefix}_DATABASE_URL") or _value("CONTROL_PLANE_DATABASE_URL")
        ):
            raise ValueError(
                f"{domain_prefix}_BACKEND=postgres 需要 {domain_prefix}_DATABASE_URL"
                " 或 CONTROL_PLANE_DATABASE_URL"
            )

    memory_backend = _value("MEMORY_STORE_BACKEND").lower() or "sqlite"
    if memory_backend not in {"sqlite", "postgres"}:
        raise ValueError(f"MEMORY_STORE_BACKEND 必须是 sqlite 或 postgres，当前值: {memory_backend!r}")
    if memory_backend == "postgres" and not _value("MEMORY_DATABASE_URL"):
        raise ValueError(
            "MEMORY_STORE_BACKEND=postgres 需要显式配置 MEMORY_DATABASE_URL，"
            "否则会静默创建本地空库"
        )

    governor_backend = _value("RESOURCE_GOVERNOR_BACKEND").lower() or "sqlite"
    if governor_backend not in {"sqlite", "postgres"}:
        raise ValueError(
            f"RESOURCE_GOVERNOR_BACKEND 必须是 sqlite 或 postgres，当前值: {governor_backend!r}"
        )
    if governor_backend == "postgres":
        if not _value("RESOURCE_GOVERNOR_DATABASE_URL"):
            raise ValueError(
                "RESOURCE_GOVERNOR_BACKEND=postgres 需要显式配置 "
                "RESOURCE_GOVERNOR_DATABASE_URL，否则会静默落到本地配额库"
            )
        if (_value("RESOURCE_GOVERNOR_SCOPE").lower() or "process") != "deployment":
            raise ValueError(
                "RESOURCE_GOVERNOR_BACKEND=postgres 需要 RESOURCE_GOVERNOR_SCOPE=deployment"
            )

    control_backend = _value("CONTROL_PLANE_BACKEND").lower() or "sqlite"
    if _value("EVAL_EXECUTION_MODE").lower() not in {"inline", "worker"}:
        raise ValueError("EVAL_EXECUTION_MODE 必须是 inline 或 worker")
    import math

    quality_rate = float(_value("QUALITY_SAMPLE_RATE"))
    if not math.isfinite(quality_rate) or not 0 <= quality_rate <= 1:
        raise ValueError("QUALITY_SAMPLE_RATE 必须是 0 到 1 之间的有限数值")
    if int(_value("QUALITY_SAMPLE_DAILY_LIMIT")) < 0 or int(_value("QUALITY_PHOENIX_MAX_ATTEMPTS")) < 1:
        raise ValueError("质量抽样日上限不能为负，Phoenix 最大投递次数必须至少为 1")
    if control_backend not in {"sqlite", "postgres"}:
        raise ValueError(
            f"CONTROL_PLANE_BACKEND 必须是 sqlite 或 postgres，当前值: {control_backend!r}"
        )
    if control_backend == "postgres":
        if not _value("CONTROL_PLANE_DATABASE_URL"):
            raise ValueError(
                "CONTROL_PLANE_BACKEND=postgres 需要显式配置 CONTROL_PLANE_DATABASE_URL，"
                "否则会静默落到本地 auth.db"
            )
        # The PostgreSQL control plane now owns conversation execution, the
        # memory ledger and the result-export ledger (v3 §5.1).  Auth, audit
        # logs, document authoring and attachment bytes intentionally stay on
        # the local SQLite stores: they are separate authorization/log domains
        # and never join a control-plane transaction.  Readiness
        # (``/health/ready`` -> ``control_plane``) fails closed when the
        # versioned schema is missing or stale; see control-plane-port-inventory
        # §3-§5 for the declared boundary and the cutover rehearsal.

    sandbox_backend = _value("SANDBOX_BACKEND").lower() or "disabled"
    if sandbox_backend not in {"disabled", "runner_api"}:
        raise ValueError(
            "SANDBOX_BACKEND 必须是 disabled 或 runner_api，"
            f"当前值: {sandbox_backend!r}"
        )
    sandbox_network = _value("SANDBOX_DEFAULT_NETWORK").lower() or "none"
    if sandbox_network not in {"none", "egress_proxy"}:
        raise ValueError(
            "SANDBOX_DEFAULT_NETWORK 必须是 none 或 egress_proxy，"
            f"当前值: {sandbox_network!r}"
        )
    sandbox_enabled = _value("SANDBOX_ENABLED").lower() in {"1", "true", "yes", "on"}
    gateway_enabled = _value("SANDBOX_GATEWAY_ENABLED").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if sandbox_enabled or gateway_enabled:
        if sandbox_backend == "disabled":
            raise ValueError(
                "SANDBOX_ENABLED/SANDBOX_GATEWAY_ENABLED=true 需要已验证的 SANDBOX_BACKEND，"
                "不能使用 disabled"
            )
        if not _value("SANDBOX_RUNNER_URL"):
            raise ValueError(
                "启用 Sandbox 需要受运维控制的 SANDBOX_RUNNER_URL"
            )
        if not _value("SANDBOX_SERVICE_AUTH_REF"):
            raise ValueError(
                "启用 Sandbox 需要外部 secret/workload identity 的 SANDBOX_SERVICE_AUTH_REF"
            )
        if _value("SANDBOX_REQUIRE_RUNTIME_ATTESTATION").lower() in {"1", "true", "yes", "on", ""}:
            if not _value("SANDBOX_RUNTIME_ATTESTATION_REF"):
                raise ValueError(
                    "启用 Sandbox 需要已审核的 SANDBOX_RUNTIME_ATTESTATION_REF"
                )
            profile_digest = _value("SANDBOX_RUNTIME_PROFILE_DIGEST").lower().removeprefix("sha256:")
            if len(profile_digest) != 64 or any(char not in "0123456789abcdef" for char in profile_digest):
                raise ValueError(
                    "启用 Sandbox 需要有效的 SANDBOX_RUNTIME_PROFILE_DIGEST"
                )
        if not _value("SANDBOX_ARTIFACT_SCANNER_REF"):
            raise ValueError(
                "启用 Sandbox 需要已部署并登记的 SANDBOX_ARTIFACT_SCANNER_REF"
            )
    if gateway_enabled and not _value("SANDBOX_GATEWAY_URL"):
        raise ValueError(
            "SANDBOX_GATEWAY_ENABLED=true 需要受运维控制的 SANDBOX_GATEWAY_URL"
        )
    if _value("SANDBOX_ALLOW_DYNAMIC_INSTALL").lower() in {"1", "true", "yes", "on"}:
        raise ValueError("SANDBOX_ALLOW_DYNAMIC_INSTALL 当前阶段必须保持 false")

    # MCP's unified-tool and governor dependencies are enforced by the
    # effective settings loader above. They are intentionally not validated
    # as user-facing fields: legacy false values are repaired automatically
    # and MCP_ENABLED remains the public emergency kill switch.
