# config/settings.py
import os
from enum import Enum

from dotenv import load_dotenv


ENV_FILE_ENCODING = "utf-8-sig"

load_dotenv(encoding=ENV_FILE_ENCODING)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
PIPELINE_ARCHIVE_ROOT = os.getenv("PIPELINE_ARCHIVE_ROOT", os.path.join(STORAGE_DIR, "pipeline_archives"))
RAGFLOW_FILE_ROOT = os.getenv("RAGFLOW_FILE_ROOT", PIPELINE_ARCHIVE_ROOT)
LOG_DIR = os.path.join(STORAGE_DIR, "logs")

RAGFLOW_BASE_URL = os.getenv("RAGFLOW_BASE_URL", "http://localhost:9380")
RAGFLOW_API_KEY = os.getenv("RAGFLOW_API_KEY", "")
RAGFLOW_GOVERNANCE_DATASET_NAME = os.getenv("RAGFLOW_GOVERNANCE_DATASET_NAME", "department_governance")
RAGFLOW_DESIGN_DATASET_NAME = os.getenv("RAGFLOW_DESIGN_DATASET_NAME", "project_design_assets")
RAGFLOW_TIMEOUT_SECONDS = int(os.getenv("RAGFLOW_TIMEOUT_SECONDS", "120"))
RAGFLOW_SIMILARITY_THRESHOLD = float(os.getenv("RAGFLOW_SIMILARITY_THRESHOLD", "0.25"))
RAGFLOW_VECTOR_WEIGHT = float(os.getenv("RAGFLOW_VECTOR_WEIGHT", "0.4"))

AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", os.path.join(STORAGE_DIR, "auth.db"))
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
AGENT_TEMPERATURE = float(os.getenv("AGENT_TEMPERATURE", "0.2"))
AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "120"))

FINAL_TOP_K = int(os.getenv("FINAL_TOP_K", "5"))
AGENT_MAX_RETRIEVAL_ROUNDS = int(os.getenv("AGENT_MAX_RETRIEVAL_ROUNDS", "3"))

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", (
    "你是一个专业的硬件技术助手。请严格基于参考资料回答用户问题。\n"
    "规则：\n"
    "1. 如果参考资料包含答案，请详细回答。\n"
    "2. 如果参考资料不足或无关，请明确说明知识库中未找到相关信息，不要编造。\n"
    "3. 回答必须使用中文。"
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
    "AGENT_TEMPERATURE": "0.2",
    "AGENT_TIMEOUT_SECONDS": "120",
    "FINAL_TOP_K": "5",
    "AGENT_MAX_RETRIEVAL_ROUNDS": "3",
    "SYSTEM_PROMPT": SYSTEM_PROMPT,
    "NO_CONTEXT_PROMPT": NO_CONTEXT_PROMPT,
}


def reload_settings():
    """Reload .env-backed settings after Streamlit writes configuration changes."""
    global RAGFLOW_BASE_URL, RAGFLOW_API_KEY
    global RAGFLOW_GOVERNANCE_DATASET_NAME, RAGFLOW_DESIGN_DATASET_NAME
    global RAGFLOW_TIMEOUT_SECONDS, RAGFLOW_SIMILARITY_THRESHOLD, RAGFLOW_VECTOR_WEIGHT
    global AUTH_DB_PATH, AUTH_DEFAULT_ADMIN_USERNAME, AUTH_DEFAULT_ADMIN_PASSWORD, AUTH_SESSION_TTL_HOURS
    global PIPELINE_ARCHIVE_ROOT, RAGFLOW_FILE_ROOT
    global AGENT_LLM_PROVIDER
    global AGENT_OLLAMA_BASE_URL, AGENT_OLLAMA_MODEL
    global AGENT_CUSTOM_API_KEY, AGENT_CUSTOM_BASE_URL, AGENT_CUSTOM_MODEL
    global AGENT_CUSTOM_MAX_TOKENS, AGENT_TEMPERATURE, AGENT_TIMEOUT_SECONDS
    global FINAL_TOP_K, AGENT_MAX_RETRIEVAL_ROUNDS
    global SYSTEM_PROMPT, NO_CONTEXT_PROMPT

    load_dotenv(override=True, encoding=ENV_FILE_ENCODING)

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
    AGENT_TEMPERATURE = float(os.getenv("AGENT_TEMPERATURE", "0.2"))
    AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "120"))

    FINAL_TOP_K = int(os.getenv("FINAL_TOP_K", "5"))
    AGENT_MAX_RETRIEVAL_ROUNDS = int(os.getenv("AGENT_MAX_RETRIEVAL_ROUNDS", "3"))
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
    """Update .env keys while preserving comments and unrelated settings."""
    if env_path is None:
        env_path = os.path.join(BASE_DIR, ".env")

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

    with open(env_path, "w", encoding=ENV_FILE_ENCODING) as f:
        f.writelines(new_lines)
