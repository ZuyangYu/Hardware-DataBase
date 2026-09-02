import os
import stat
import threading
from enum import Enum
from dotenv import load_dotenv


ENV_FILE_ENCODING = "utf-8-sig"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE_PATH = os.path.join(BASE_DIR, ".env")

# Serialises .env read-modify-write cycles (save_settings_to_env). Sync API
# routes run in Starlette's threadpool, so two concurrent PUT /config requests
# would otherwise interleave read/modify/write and lose updates. RLock because
# AppPipeline.apply_settings holds it across validate -> save -> reload while
# save_settings_to_env acquires it again.
_ENV_WRITE_LOCK = threading.RLock()

load_dotenv(dotenv_path=ENV_FILE_PATH, encoding=ENV_FILE_ENCODING)
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
PIPELINE_ARCHIVE_ROOT = os.getenv("PIPELINE_ARCHIVE_ROOT", os.path.join(STORAGE_DIR, "pipeline_archives"))
RAGFLOW_FILE_ROOT = os.getenv("RAGFLOW_FILE_ROOT", PIPELINE_ARCHIVE_ROOT)
LOG_DIR = os.path.join(STORAGE_DIR, "logs")


def _resolve_storage_path(raw_path: str | None, default_name: str) -> str:
    """Resolve memory data paths independently of the process CWD."""
    path = str(raw_path or default_name).strip()
    if not os.path.isabs(path):
        path = os.path.join(STORAGE_DIR, path)
    return os.path.abspath(path)

RAGFLOW_BASE_URL = os.getenv("RAGFLOW_BASE_URL", "http://localhost:9380")
RAGFLOW_API_KEY = os.getenv("RAGFLOW_API_KEY", "")
RAGFLOW_GOVERNANCE_DATASET_NAME = os.getenv("RAGFLOW_GOVERNANCE_DATASET_NAME", "department_governance")
RAGFLOW_DESIGN_DATASET_NAME = os.getenv("RAGFLOW_DESIGN_DATASET_NAME", "project_design_assets")
RAGFLOW_TIMEOUT_SECONDS = int(os.getenv("RAGFLOW_TIMEOUT_SECONDS", "120"))
RAGFLOW_SIMILARITY_THRESHOLD = float(os.getenv("RAGFLOW_SIMILARITY_THRESHOLD", "0.25"))
RAGFLOW_VECTOR_WEIGHT = float(os.getenv("RAGFLOW_VECTOR_WEIGHT", "0.4"))

# Task 7 gray rollout: role/structure read models can be disabled per
# deployment; exact refdes/net queries always stay available.
CIRCUIT_SEMANTIC_QUERY_ENABLED = os.getenv("CIRCUIT_SEMANTIC_QUERY_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}

AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", os.path.join(STORAGE_DIR, "auth.db"))
# LangGraph checkpointer：agent 会话状态（thread 级完整消息历史）的持久化库。
AGENT_CHECKPOINT_DB_PATH = os.getenv(
    "AGENT_CHECKPOINT_DB_PATH", os.path.join(STORAGE_DIR, "agent_checkpoints.db")
)
AUTH_DEFAULT_ADMIN_USERNAME = os.getenv("AUTH_DEFAULT_ADMIN_USERNAME", "admin")
AUTH_DEFAULT_ADMIN_PASSWORD = os.getenv("AUTH_DEFAULT_ADMIN_PASSWORD", "")
AUTH_SESSION_TTL_HOURS = int(os.getenv("AUTH_SESSION_TTL_HOURS", "24"))


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
MEMORY_SQLITE_PATH = _resolve_storage_path(os.getenv("MEMORY_SQLITE_PATH"), "memory.db")
MEMORY_SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("MEMORY_SQLITE_BUSY_TIMEOUT_MS", "30000"))
MEMORY_SINGLE_WRITER = os.getenv("MEMORY_SINGLE_WRITER", "true").lower() in {"1", "true", "yes", "on"}
MEMORY_EXTRACTION_ENABLED = os.getenv("MEMORY_EXTRACTION_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
MEMORY_DEBOUNCE_SECONDS = int(os.getenv("MEMORY_DEBOUNCE_SECONDS", "300"))
MEMORY_JOB_LEASE_SECONDS = int(os.getenv("MEMORY_JOB_LEASE_SECONDS", "180"))
MEMORY_JOB_MAX_RETRIES = int(os.getenv("MEMORY_JOB_MAX_RETRIES", "5"))
MEMORY_JOB_MAX_CONCURRENCY = int(os.getenv("MEMORY_JOB_MAX_CONCURRENCY", "1"))
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
MEMORY_INDEX_FIELDS = os.getenv("MEMORY_INDEX_FIELDS", "content.content,content.title,content.subject")
MEMORY_ALLOW_GENERAL_CHAT_EXTRACTION = os.getenv("MEMORY_ALLOW_GENERAL_CHAT_EXTRACTION", "false").lower() in {"1", "true", "yes", "on"}
MEMORY_USER_MEMORY_OPT_IN = os.getenv("MEMORY_USER_MEMORY_OPT_IN", "false").lower() in {"1", "true", "yes", "on"}
MEMORY_USER_MEMORY_REQUIRE_PER_EVENT_CONSENT = os.getenv("MEMORY_USER_MEMORY_REQUIRE_PER_EVENT_CONSENT", "true").lower() in {"1", "true", "yes", "on"}
MEMORY_RETENTION_DAYS = os.getenv("MEMORY_RETENTION_DAYS", "")

FINAL_TOP_K = int(os.getenv("FINAL_TOP_K", "5"))
AGENT_MAX_RETRIEVAL_ROUNDS = int(os.getenv("AGENT_MAX_RETRIEVAL_ROUNDS", "3"))
WORKER_POLL_INTERVAL_SECONDS = float(os.getenv("WORKER_POLL_INTERVAL_SECONDS", "0.5"))
WORKER_PARSE_BATCH_SIZE = int(os.getenv("WORKER_PARSE_BATCH_SIZE", "1"))
CHAT_TURN_HEARTBEAT_TTL_SECONDS = int(os.getenv("CHAT_TURN_HEARTBEAT_TTL_SECONDS", "90"))
CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS", "10"))
DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES = os.getenv("DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES", "false").lower() == "true"
DOCUMENT_AUTO_ACCEPT_AI_TEMPLATE_RECOMMENDATIONS = os.getenv("DOCUMENT_AUTO_ACCEPT_AI_TEMPLATE_RECOMMENDATIONS", "false").lower() == "true"
DOCUMENT_AUTO_PUBLISH_VERIFIED = os.getenv("DOCUMENT_AUTO_PUBLISH_VERIFIED", "false").lower() == "true"

# Cross-cutting observability configuration.  These settings deliberately live
# here, alongside the rest of the application's single source of truth, so the
# API, worker, Streamlit process and evaluation threads cannot drift apart.
OBS_ENABLED = os.getenv("OBS_ENABLED", "true").lower() == "true"
OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "hardware-database-api")
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
OBS_ENVIRONMENT = os.getenv("OBS_ENVIRONMENT", "development")
OBS_SERVICE_VERSION = os.getenv("OBS_SERVICE_VERSION", "0.1.0")
OBS_TRACE_SAMPLE_RATIO = float(os.getenv("OBS_TRACE_SAMPLE_RATIO", "1.0"))
OBS_CAPTURE_CONTENT = os.getenv("OBS_CAPTURE_CONTENT", "false").lower() == "true"
OBS_CAPTURE_QUERY = os.getenv("OBS_CAPTURE_QUERY", "false").lower() == "true"
OBS_CAPTURE_EVIDENCE = os.getenv("OBS_CAPTURE_EVIDENCE", "false").lower() == "true"
OBS_CAPTURE_LLM_CONTENT = os.getenv("OBS_CAPTURE_LLM_CONTENT", "false").lower() == "true"
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

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", (
    "你是一个专业的硬件技术助手。请严格基于检索到的【参考资料】回答用户问题。\n"
    "规则：\n"
    "1. 如果【参考资料】包含答案，请详细回答，并在关键结论后标注证据来源编号，如 [1][2]（编号见证据片段前的 [n] 标记）。\n"
    "2. 回答前自查：证据是否已覆盖问题的全部要点？若仍有缺口，先继续检索补证，不要凭当前片段草率收敛。\n"
    "3. 如果【参考资料】内容不足或无关，请明确说明知识库中未找到相关信息，不要编造。\n"
    "4. 多次检索时不要用完全相同的查询原样重复调用；重复前先确认已有证据是否覆盖问题，需要新信息时换关键词或换检索工具。\n"
    "5. 回答必须使用中文。"
))

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


DEFAULT_VALUES = {
    "RAGFLOW_BASE_URL": "http://localhost:9380",
    "RAGFLOW_API_KEY": "",
    "RAGFLOW_GOVERNANCE_DATASET_NAME": "department_governance",
    "RAGFLOW_DESIGN_DATASET_NAME": "project_design_assets",
    "RAGFLOW_TIMEOUT_SECONDS": "120",
    "RAGFLOW_SIMILARITY_THRESHOLD": "0.25",
    "RAGFLOW_VECTOR_WEIGHT": "0.4",
    "PIPELINE_ARCHIVE_ROOT": os.path.join(STORAGE_DIR, "pipeline_archives"),
    "RAGFLOW_FILE_ROOT": os.path.join(STORAGE_DIR, "pipeline_archives"),
    "AUTH_DB_PATH": os.path.join(STORAGE_DIR, "auth.db"),
    "AGENT_CHECKPOINT_DB_PATH": os.path.join(STORAGE_DIR, "agent_checkpoints.db"),
    "AUTH_DEFAULT_ADMIN_USERNAME": "admin",
    "AUTH_DEFAULT_ADMIN_PASSWORD": "",
    "AUTH_SESSION_TTL_HOURS": "24",
    "AGENT_LLM_PROVIDER": "ollama",
    "AGENT_OLLAMA_BASE_URL": "http://localhost:11434",
    "AGENT_OLLAMA_MODEL": "qwen2.5:32b",
    "AGENT_CUSTOM_API_KEY": "",
    "AGENT_CUSTOM_BASE_URL": "",
    "AGENT_CUSTOM_MODEL": "",
    "AGENT_CUSTOM_MAX_TOKENS": "4096",
    "AGENT_MODEL_MAX_INPUT_TOKENS": "65536",
    "AGENT_TEMPERATURE": "0.2",
    "AGENT_TIMEOUT_SECONDS": "120",
    "AGENT_RATE_LIMIT_MAX_RETRIES": "4",
    "AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS": "1",
    "AGENT_RATE_LIMIT_MAX_DELAY_SECONDS": "16",
    "MEMORY_ENABLED": "true",
    "MEMORY_STORE_BACKEND": "sqlite",
    "MEMORY_SQLITE_PATH": os.path.join(STORAGE_DIR, "memory.db"),
    "MEMORY_SQLITE_BUSY_TIMEOUT_MS": "30000",
    "MEMORY_SINGLE_WRITER": "true",
    "MEMORY_EXTRACTION_ENABLED": "true",
    "MEMORY_DEBOUNCE_SECONDS": "300",
    "MEMORY_JOB_LEASE_SECONDS": "180",
    "MEMORY_JOB_MAX_RETRIES": "5",
    "MEMORY_JOB_MAX_CONCURRENCY": "1",
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
    "MEMORY_INDEX_FIELDS": "content.content,content.title,content.subject",
    "MEMORY_ALLOW_GENERAL_CHAT_EXTRACTION": "false",
    "MEMORY_USER_MEMORY_OPT_IN": "false",
    "MEMORY_USER_MEMORY_REQUIRE_PER_EVENT_CONSENT": "true",
    "MEMORY_RETENTION_DAYS": "",
    "FINAL_TOP_K": "5",
    "AGENT_MAX_RETRIEVAL_ROUNDS": "3",
    "WORKER_POLL_INTERVAL_SECONDS": "0.5",
    "WORKER_PARSE_BATCH_SIZE": "1",
    "CHAT_TURN_HEARTBEAT_TTL_SECONDS": "90",
    "CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS": "10",
    "OBS_ENABLED": "true",
    "OTEL_SERVICE_NAME": "hardware-database-api",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4317",
    "OBS_ENVIRONMENT": "development",
    "OBS_SERVICE_VERSION": "0.1.0",
    "OBS_TRACE_SAMPLE_RATIO": "1.0",
    "OBS_CAPTURE_CONTENT": "false",
    "OBS_CAPTURE_QUERY": "false",
    "OBS_CAPTURE_EVIDENCE": "false",
    "OBS_CAPTURE_LLM_CONTENT": "false",
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
    "DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES": "false",
    "DOCUMENT_AUTO_ACCEPT_AI_TEMPLATE_RECOMMENDATIONS": "false",
    "DOCUMENT_AUTO_PUBLISH_VERIFIED": "false",
    "SYSTEM_PROMPT": SYSTEM_PROMPT,
    "NO_CONTEXT_PROMPT": NO_CONTEXT_PROMPT,
}


def reload_settings():
    """Reload .env-backed settings after Streamlit writes configuration changes."""
    global RAGFLOW_BASE_URL, RAGFLOW_API_KEY
    global RAGFLOW_GOVERNANCE_DATASET_NAME, RAGFLOW_DESIGN_DATASET_NAME
    global RAGFLOW_TIMEOUT_SECONDS, RAGFLOW_SIMILARITY_THRESHOLD, RAGFLOW_VECTOR_WEIGHT
    global AUTH_DB_PATH, AUTH_DEFAULT_ADMIN_USERNAME, AUTH_DEFAULT_ADMIN_PASSWORD, AUTH_SESSION_TTL_HOURS
    global AGENT_CHECKPOINT_DB_PATH
    global PIPELINE_ARCHIVE_ROOT, RAGFLOW_FILE_ROOT
    global AGENT_LLM_PROVIDER
    global AGENT_OLLAMA_BASE_URL, AGENT_OLLAMA_MODEL
    global AGENT_CUSTOM_API_KEY, AGENT_CUSTOM_BASE_URL, AGENT_CUSTOM_MODEL
    global AGENT_CUSTOM_MAX_TOKENS, AGENT_TEMPERATURE, AGENT_TIMEOUT_SECONDS
    global AGENT_MODEL_MAX_INPUT_TOKENS
    global AGENT_RATE_LIMIT_MAX_RETRIES, AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS
    global AGENT_RATE_LIMIT_MAX_DELAY_SECONDS
    global FINAL_TOP_K, AGENT_MAX_RETRIEVAL_ROUNDS
    global WORKER_POLL_INTERVAL_SECONDS, WORKER_PARSE_BATCH_SIZE
    global CHAT_TURN_HEARTBEAT_TTL_SECONDS, CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS
    global DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES, DOCUMENT_AUTO_ACCEPT_AI_TEMPLATE_RECOMMENDATIONS, DOCUMENT_AUTO_PUBLISH_VERIFIED
    global OBS_ENABLED, OTEL_SERVICE_NAME, OTEL_EXPORTER_OTLP_ENDPOINT, OBS_ENVIRONMENT, OBS_SERVICE_VERSION
    global OBS_TRACE_SAMPLE_RATIO, OBS_CAPTURE_CONTENT, OBS_CAPTURE_QUERY, OBS_CAPTURE_EVIDENCE, OBS_CAPTURE_LLM_CONTENT
    global OBS_CONTENT_MAX_CHARS
    global OBS_LOG_FORMAT, OBS_METRICS_ENABLED, OBS_TRACES_ENABLED, OBS_LOGS_ENABLED, OBS_PHOENIX_PROJECT
    global OBS_GRAFANA_BASE_URL, OBS_PHOENIX_BASE_URL, OBS_WORKER_HEARTBEAT_INTERVAL_SECONDS
    global OBS_WORKER_STALE_SECONDS, OBS_DEPENDENCY_TIMEOUT_SECONDS
    global SYSTEM_PROMPT, NO_CONTEXT_PROMPT
    global EXTERNAL_CONVERSATION_LLM_STRUCTURE, EXTERNAL_CONVERSATION_LLM_MAX_CHARS, EXTERNAL_CONVERSATION_LLM_SUMMARY
    global EXTERNAL_CONVERSATION_LLM_TIMEOUT_SECONDS
    global MEMORY_ENABLED, MEMORY_STORE_BACKEND, MEMORY_SQLITE_PATH, MEMORY_SQLITE_BUSY_TIMEOUT_MS
    global MEMORY_SINGLE_WRITER, MEMORY_EXTRACTION_ENABLED, MEMORY_DEBOUNCE_SECONDS, MEMORY_JOB_LEASE_SECONDS
    global MEMORY_JOB_MAX_RETRIES, MEMORY_JOB_MAX_CONCURRENCY, MEMORY_REFLECTION_TIMEOUT_SECONDS
    global MEMORY_REFLECTION_MIN_INTERVAL_SECONDS, MEMORY_REFLECTION_CIRCUIT_FAILURES, MEMORY_REFLECTION_CIRCUIT_COOLDOWN_SECONDS
    global MEMORY_PROJECTION_MAX_RETRIES, MEMORY_RECONCILE_INTERVAL_SECONDS
    global MEMORY_USER_TOP_K, MEMORY_PROJECT_TOP_K, MEMORY_STORE_OVERSAMPLE_FACTOR, MEMORY_STORE_MAX_SCAN
    global MEMORY_CONTEXT_MAX_TOKENS, MEMORY_ITEM_MAX_TOKENS, MEMORY_MIN_SCORE_ENABLED, MEMORY_MIN_SCORE
    global MEMORY_MODEL_PROVIDER, MEMORY_MODEL, MEMORY_MODEL_BASE_URL, MEMORY_MODEL_API_KEY
    global MEMORY_MODEL_REQUIRE_STRUCTURED_TOOLS, MEMORY_EMBEDDING_PROVIDER, MEMORY_EMBEDDING_MODEL
    global MEMORY_EMBEDDING_BASE_URL, MEMORY_EMBEDDING_API_KEY, MEMORY_EMBEDDING_DIMS, MEMORY_INDEX_FIELDS
    global MEMORY_ALLOW_GENERAL_CHAT_EXTRACTION, MEMORY_USER_MEMORY_OPT_IN
    global MEMORY_USER_MEMORY_REQUIRE_PER_EVENT_CONSENT, MEMORY_RETENTION_DAYS

    load_dotenv(dotenv_path=ENV_FILE_PATH, override=True, encoding=ENV_FILE_ENCODING)

    RAGFLOW_BASE_URL = os.getenv("RAGFLOW_BASE_URL", "http://localhost:9380")
    RAGFLOW_API_KEY = os.getenv("RAGFLOW_API_KEY", "")
    RAGFLOW_GOVERNANCE_DATASET_NAME = os.getenv("RAGFLOW_GOVERNANCE_DATASET_NAME", "department_governance")
    RAGFLOW_DESIGN_DATASET_NAME = os.getenv("RAGFLOW_DESIGN_DATASET_NAME", "project_design_assets")
    RAGFLOW_TIMEOUT_SECONDS = int(os.getenv("RAGFLOW_TIMEOUT_SECONDS", "120"))
    RAGFLOW_SIMILARITY_THRESHOLD = float(os.getenv("RAGFLOW_SIMILARITY_THRESHOLD", "0.25"))
    RAGFLOW_VECTOR_WEIGHT = float(os.getenv("RAGFLOW_VECTOR_WEIGHT", "0.4"))
    PIPELINE_ARCHIVE_ROOT = os.getenv("PIPELINE_ARCHIVE_ROOT", os.path.join(STORAGE_DIR, "pipeline_archives"))
    RAGFLOW_FILE_ROOT = os.getenv("RAGFLOW_FILE_ROOT", PIPELINE_ARCHIVE_ROOT)

    AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", os.path.join(STORAGE_DIR, "auth.db"))
    AGENT_CHECKPOINT_DB_PATH = os.getenv(
        "AGENT_CHECKPOINT_DB_PATH", os.path.join(STORAGE_DIR, "agent_checkpoints.db")
    )
    AUTH_DEFAULT_ADMIN_USERNAME = os.getenv("AUTH_DEFAULT_ADMIN_USERNAME", "admin")
    AUTH_DEFAULT_ADMIN_PASSWORD = os.getenv("AUTH_DEFAULT_ADMIN_PASSWORD", "")
    AUTH_SESSION_TTL_HOURS = int(os.getenv("AUTH_SESSION_TTL_HOURS", "24"))

    AGENT_LLM_PROVIDER = Provider(os.getenv("AGENT_LLM_PROVIDER", "ollama").lower())
    AGENT_OLLAMA_BASE_URL = os.getenv("AGENT_OLLAMA_BASE_URL", "http://localhost:11434")
    AGENT_OLLAMA_MODEL = os.getenv("AGENT_OLLAMA_MODEL", "qwen2.5:32b")
    AGENT_CUSTOM_API_KEY = os.getenv("AGENT_CUSTOM_API_KEY", "")
    AGENT_CUSTOM_BASE_URL = os.getenv("AGENT_CUSTOM_BASE_URL", "")
    AGENT_CUSTOM_MODEL = os.getenv("AGENT_CUSTOM_MODEL", "")
    AGENT_CUSTOM_MAX_TOKENS = int(os.getenv("AGENT_CUSTOM_MAX_TOKENS", "4096"))
    AGENT_MODEL_MAX_INPUT_TOKENS = int(os.getenv("AGENT_MODEL_MAX_INPUT_TOKENS", "65536"))
    AGENT_TEMPERATURE = float(os.getenv("AGENT_TEMPERATURE", "0.2"))
    AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "120"))
    AGENT_RATE_LIMIT_MAX_RETRIES = int(os.getenv("AGENT_RATE_LIMIT_MAX_RETRIES", "4"))
    AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS = float(os.getenv("AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS", "1"))
    AGENT_RATE_LIMIT_MAX_DELAY_SECONDS = float(os.getenv("AGENT_RATE_LIMIT_MAX_DELAY_SECONDS", "16"))

    FINAL_TOP_K = int(os.getenv("FINAL_TOP_K", "5"))
    AGENT_MAX_RETRIEVAL_ROUNDS = int(os.getenv("AGENT_MAX_RETRIEVAL_ROUNDS", "3"))
    WORKER_POLL_INTERVAL_SECONDS = float(os.getenv("WORKER_POLL_INTERVAL_SECONDS", "0.5"))
    WORKER_PARSE_BATCH_SIZE = int(os.getenv("WORKER_PARSE_BATCH_SIZE", "1"))
    CHAT_TURN_HEARTBEAT_TTL_SECONDS = int(os.getenv("CHAT_TURN_HEARTBEAT_TTL_SECONDS", "90"))
    CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS", "10"))
    DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES = os.getenv("DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES", "false").lower() == "true"
    DOCUMENT_AUTO_ACCEPT_AI_TEMPLATE_RECOMMENDATIONS = os.getenv("DOCUMENT_AUTO_ACCEPT_AI_TEMPLATE_RECOMMENDATIONS", "false").lower() == "true"
    DOCUMENT_AUTO_PUBLISH_VERIFIED = os.getenv("DOCUMENT_AUTO_PUBLISH_VERIFIED", "false").lower() == "true"
    EXTERNAL_CONVERSATION_LLM_STRUCTURE = os.getenv("EXTERNAL_CONVERSATION_LLM_STRUCTURE", "true").lower() in {"1", "true", "yes", "on"}
    EXTERNAL_CONVERSATION_LLM_MAX_CHARS = int(os.getenv("EXTERNAL_CONVERSATION_LLM_MAX_CHARS", "12000"))
    EXTERNAL_CONVERSATION_LLM_SUMMARY = os.getenv("EXTERNAL_CONVERSATION_LLM_SUMMARY", "true").lower() in {"1", "true", "yes", "on"}
    EXTERNAL_CONVERSATION_LLM_TIMEOUT_SECONDS = int(os.getenv("EXTERNAL_CONVERSATION_LLM_TIMEOUT_SECONDS", "60"))
    MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    MEMORY_STORE_BACKEND = os.getenv("MEMORY_STORE_BACKEND", "sqlite").strip().lower()
    MEMORY_SQLITE_PATH = _resolve_storage_path(os.getenv("MEMORY_SQLITE_PATH"), "memory.db")
    MEMORY_SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("MEMORY_SQLITE_BUSY_TIMEOUT_MS", "30000"))
    MEMORY_SINGLE_WRITER = os.getenv("MEMORY_SINGLE_WRITER", "true").lower() in {"1", "true", "yes", "on"}
    MEMORY_EXTRACTION_ENABLED = os.getenv("MEMORY_EXTRACTION_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    MEMORY_DEBOUNCE_SECONDS = int(os.getenv("MEMORY_DEBOUNCE_SECONDS", "300"))
    MEMORY_JOB_LEASE_SECONDS = int(os.getenv("MEMORY_JOB_LEASE_SECONDS", "180"))
    MEMORY_JOB_MAX_RETRIES = int(os.getenv("MEMORY_JOB_MAX_RETRIES", "5"))
    MEMORY_JOB_MAX_CONCURRENCY = int(os.getenv("MEMORY_JOB_MAX_CONCURRENCY", "1"))
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
    MEMORY_INDEX_FIELDS = os.getenv("MEMORY_INDEX_FIELDS", "content.content,content.title,content.subject")
    MEMORY_ALLOW_GENERAL_CHAT_EXTRACTION = os.getenv("MEMORY_ALLOW_GENERAL_CHAT_EXTRACTION", "false").lower() in {"1", "true", "yes", "on"}
    MEMORY_USER_MEMORY_OPT_IN = os.getenv("MEMORY_USER_MEMORY_OPT_IN", "false").lower() in {"1", "true", "yes", "on"}
    MEMORY_USER_MEMORY_REQUIRE_PER_EVENT_CONSENT = os.getenv("MEMORY_USER_MEMORY_REQUIRE_PER_EVENT_CONSENT", "true").lower() in {"1", "true", "yes", "on"}
    MEMORY_RETENTION_DAYS = os.getenv("MEMORY_RETENTION_DAYS", "")
    OBS_ENABLED = os.getenv("OBS_ENABLED", "true").lower() == "true"
    OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "hardware-database-api")
    OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
    OBS_ENVIRONMENT = os.getenv("OBS_ENVIRONMENT", "development")
    OBS_SERVICE_VERSION = os.getenv("OBS_SERVICE_VERSION", "0.1.0")
    OBS_TRACE_SAMPLE_RATIO = float(os.getenv("OBS_TRACE_SAMPLE_RATIO", "1.0"))
    OBS_CAPTURE_CONTENT = os.getenv("OBS_CAPTURE_CONTENT", "false").lower() == "true"
    OBS_CAPTURE_QUERY = os.getenv("OBS_CAPTURE_QUERY", "false").lower() == "true"
    OBS_CAPTURE_EVIDENCE = os.getenv("OBS_CAPTURE_EVIDENCE", "false").lower() == "true"
    OBS_CAPTURE_LLM_CONTENT = os.getenv("OBS_CAPTURE_LLM_CONTENT", "false").lower() == "true"
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
    if env_path is None:
        env_path = os.path.join(BASE_DIR, ".env")

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
    "AUTH_SESSION_TTL_HOURS",
    "AGENT_CUSTOM_MAX_TOKENS",
    "AGENT_TIMEOUT_SECONDS",
    "AGENT_RATE_LIMIT_MAX_RETRIES",
    "FINAL_TOP_K",
    "AGENT_MAX_RETRIEVAL_ROUNDS",
    "WORKER_PARSE_BATCH_SIZE",
    "CHAT_TURN_HEARTBEAT_TTL_SECONDS",
    "CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS",
    "MEMORY_SQLITE_BUSY_TIMEOUT_MS",
    "MEMORY_DEBOUNCE_SECONDS",
    "MEMORY_JOB_LEASE_SECONDS",
    "MEMORY_JOB_MAX_RETRIES",
    "MEMORY_JOB_MAX_CONCURRENCY",
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
})
_FLOAT_SETTING_KEYS = frozenset({
    "RAGFLOW_SIMILARITY_THRESHOLD",
    "RAGFLOW_VECTOR_WEIGHT",
    "AGENT_TEMPERATURE",
    "AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS",
    "AGENT_RATE_LIMIT_MAX_DELAY_SECONDS",
    "WORKER_POLL_INTERVAL_SECONDS",
    "MEMORY_REFLECTION_MIN_INTERVAL_SECONDS",
})


def validate_settings_values(settings_dict: dict) -> None:
    """Validate candidate .env values against their expected types.

    Raises ``ValueError`` naming the offending key when a value cannot be
    parsed the way ``reload_settings`` (and module import) will parse it.
    """
    for key, value in settings_dict.items():
        text = "" if value is None else str(value)
        if key in _INT_SETTING_KEYS:
            try:
                int(text)
            except ValueError as exc:
                raise ValueError(f"{key} 必须是整数，当前值: {text!r}") from exc
        elif key in _FLOAT_SETTING_KEYS:
            try:
                float(text)
            except ValueError as exc:
                raise ValueError(f"{key} 必须是数字，当前值: {text!r}") from exc
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
        elif key == "MEMORY_STORE_BACKEND" and text.strip().lower() not in {"sqlite", "postgres"}:
            raise ValueError(f"MEMORY_STORE_BACKEND 必须是 sqlite 或 postgres，当前值: {text!r}")
        elif key in {"MEMORY_EMBEDDING_DIMS", "MEMORY_RETENTION_DAYS"} and text.strip():
            try:
                int(text)
            except ValueError as exc:
                raise ValueError(f"{key} 必须是整数或空值，当前值: {text!r}") from exc
