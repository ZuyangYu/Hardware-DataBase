# config/settings.py
import os
from enum import Enum
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 根目录
DATA_ROOT = os.path.join(BASE_DIR, "data")
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
CHROMA_PATH = os.path.join(STORAGE_DIR, "chroma_db")
LOG_DIR = os.path.join(STORAGE_DIR, "logs")
RERANKER_CACHE = os.path.join(STORAGE_DIR, "reranker_cache")
DEFAULT_KB_NAME = "source_documents"
RAG_BACKEND = os.getenv("RAG_BACKEND", "local").lower()
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"
AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", os.path.join(STORAGE_DIR, "auth.db"))
AUTH_DEFAULT_ADMIN_USERNAME = os.getenv("AUTH_DEFAULT_ADMIN_USERNAME", "admin")
AUTH_DEFAULT_ADMIN_PASSWORD = os.getenv("AUTH_DEFAULT_ADMIN_PASSWORD", "admin123")
AUTH_SESSION_TTL_HOURS = int(os.getenv("AUTH_SESSION_TTL_HOURS", "24"))


class Provider(Enum):
    """
    AI 服务提供商
    只保留两种：
    - ollama: 本地 Ollama 服务
    - custom: 所有第三方 API（OpenAI、OpenRouter、DeepSeek、Grok 等）
    """
    OLLAMA = "ollama"
    CUSTOM = "custom"


try:
    PROVIDER = Provider(os.getenv("PROVIDER", "ollama").lower())
except ValueError:
    valid = ", ".join(p.value for p in Provider)
    raise ValueError(f"PROVIDER 值无效，可选: {valid}")

# ==================== Ollama 配置 ====================
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_LLM_MODEL = os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:32b")
OLLAMA_EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text:latest")

# ==================== 自定义 API 配置 ====================
# 适用于所有第三方服务：OpenAI、OpenRouter、DeepSeek、Grok、Moonshot 等
CUSTOM_API_KEY = os.getenv("CUSTOM_API_KEY", "")
CUSTOM_BASE_URL = os.getenv("CUSTOM_BASE_URL", "")
CUSTOM_LLM_MODEL = os.getenv("CUSTOM_LLM_MODEL", "")
CUSTOM_EMBEDDING_MODEL = os.getenv("CUSTOM_EMBEDDING_MODEL", "")
CUSTOM_EMBEDDING_API_KEY = os.getenv("CUSTOM_EMBEDDING_API_KEY", "")
CUSTOM_EMBEDDING_BASE_URL = os.getenv("CUSTOM_EMBEDDING_BASE_URL", "")
CUSTOM_CONTEXT_WINDOW = int(os.getenv("CUSTOM_CONTEXT_WINDOW", "128000"))
CUSTOM_MAX_TOKENS = int(os.getenv("CUSTOM_MAX_TOKENS", "4096"))

# 是否使用本地 Ollama 提供 Embedding（很多第三方 API 不支持 Embedding）
USE_OLLAMA_EMBEDDING = os.getenv("USE_OLLAMA_EMBEDDING", "false").lower() == "true"

# ==================== RAG 参数 ====================
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "512"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "50"))
VECTOR_TOP_K = int(os.getenv("VECTOR_TOP_K", "20"))
BM25_TOP_K = int(os.getenv("BM25_TOP_K", "20"))
FINAL_TOP_K = int(os.getenv("FINAL_TOP_K", "5"))
RRF_K = int(os.getenv("RRF_K", "60"))


# ==================== Reranker 配置 ====================
class RerankerType(Enum):
    NONE = "none"  # 不使用 Reranker
    LOCAL = "local"  # 本地 Sentence Transformer
    API = "api"  # API Reranker


try:
    RERANKER_TYPE = RerankerType(os.getenv("RERANKER_TYPE", "none").lower())
except ValueError:
    valid = ", ".join(r.value for r in RerankerType)
    raise ValueError(f"RERANKER_TYPE 值无效，可选: {valid}")
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-gemma")
RERANKER_API_KEY = os.getenv("RERANKER_API_KEY", "")
RERANKER_API_BASE = os.getenv("RERANKER_API_BASE", "https://api.openai.com/v1")

# ==================== 系统提示词 ====================
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", (
    "你是一个专业的硬件技术助手,你的名字叫小智。请严格基于下方的【参考资料】回答用户问题。\n"
    "规则：\n"
    "1. 如果【参考资料】包含答案，请详细回答。\n"
    "2. 如果【参考资料】内容不足或无关，请明确说明'知识库中未找到相关信息'，不要编造。\n"
    "3. 回答必须使用中文。"
))

NO_CONTEXT_PROMPT = os.getenv("NO_CONTEXT_PROMPT", (
    "（知识库中没有找到相关上下文，请基于你自己的知识回答，并告知用户知识库中无相关信息）"
))

# 确保目录存在
os.makedirs(DATA_ROOT, exist_ok=True)
os.makedirs(STORAGE_DIR, exist_ok=True)
os.makedirs(CHROMA_PATH, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(RERANKER_CACHE, exist_ok=True)

# ==================== 默认值字典 ====================
DEFAULT_VALUES = {
    "RAG_BACKEND": "local",
    "AUTH_ENABLED": "true",
    "AUTH_DB_PATH": os.path.join(STORAGE_DIR, "auth.db"),
    "AUTH_DEFAULT_ADMIN_USERNAME": "admin",
    "AUTH_DEFAULT_ADMIN_PASSWORD": "admin123",
    "AUTH_SESSION_TTL_HOURS": "24",
    "PROVIDER": "ollama",
    "OLLAMA_BASE_URL": "http://localhost:11434",
    "OLLAMA_LLM_MODEL": "qwen2.5:32b",
    "OLLAMA_EMBEDDING_MODEL": "nomic-embed-text:latest",
    "CUSTOM_API_KEY": "",
    "CUSTOM_BASE_URL": "",
    "CUSTOM_LLM_MODEL": "",
    "CUSTOM_EMBEDDING_MODEL": "",
    "CUSTOM_EMBEDDING_API_KEY": "",
    "CUSTOM_EMBEDDING_BASE_URL": "",
    "CUSTOM_CONTEXT_WINDOW": "128000",
    "CUSTOM_MAX_TOKENS": "4096",
    "USE_OLLAMA_EMBEDDING": "false",
    "CHUNK_SIZE": "512",
    "CHUNK_OVERLAP": "50",
    "VECTOR_TOP_K": "20",
    "BM25_TOP_K": "20",
    "FINAL_TOP_K": "5",
    "RRF_K": "60",
    "RERANKER_TYPE": "none",
    "RERANKER_MODEL": "BAAI/bge-reranker-v2-gemma",
    "RERANKER_API_KEY": "",
    "RERANKER_API_BASE": "https://api.openai.com/v1",
    "SYSTEM_PROMPT": (
        "你是一个专业的硬件技术助手,你的名字叫小智。请严格基于下方的【参考资料】回答用户问题。\n"
        "规则：\n"
        "1. 如果【参考资料】包含答案，请详细回答。\n"
        "2. 如果【参考资料】内容不足或无关，请明确说明'知识库中未找到相关信息'，不要编造。\n"
        "3. 回答必须使用中文。"
    ),
    "NO_CONTEXT_PROMPT": "（知识库中没有找到相关上下文，请基于你自己的知识回答，并告知用户知识库中无相关信息）",
}


def get_kb_storage_path(kb_name: str) -> str:
    """获取知识库索引元数据(docstore.json等)的持久化路径"""
    # 将元数据存放在 storage/index_stores/知识库名/ 下
    path = os.path.join(STORAGE_DIR, "index_stores", kb_name)
    os.makedirs(path, exist_ok=True)
    return path


def reload_settings():
    """
    重新读取 .env 文件并更新所有模块级全局变量。
    用于 Streamlit 设置页面 "应用配置" 后动态刷新。
    """
    global RAG_BACKEND, AUTH_ENABLED, AUTH_DB_PATH
    global AUTH_DEFAULT_ADMIN_USERNAME, AUTH_DEFAULT_ADMIN_PASSWORD, AUTH_SESSION_TTL_HOURS
    global PROVIDER, OLLAMA_BASE_URL, OLLAMA_LLM_MODEL, OLLAMA_EMBEDDING_MODEL
    global CUSTOM_API_KEY, CUSTOM_BASE_URL, CUSTOM_LLM_MODEL, CUSTOM_EMBEDDING_MODEL
    global CUSTOM_EMBEDDING_API_KEY, CUSTOM_EMBEDDING_BASE_URL
    global CUSTOM_CONTEXT_WINDOW, CUSTOM_MAX_TOKENS, USE_OLLAMA_EMBEDDING
    global CHUNK_SIZE, CHUNK_OVERLAP, VECTOR_TOP_K, BM25_TOP_K, FINAL_TOP_K, RRF_K
    global RERANKER_TYPE, RERANKER_MODEL, RERANKER_API_KEY, RERANKER_API_BASE
    global SYSTEM_PROMPT, NO_CONTEXT_PROMPT

    load_dotenv(override=True)

    RAG_BACKEND = os.getenv("RAG_BACKEND", "local").lower()
    AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"
    AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", os.path.join(STORAGE_DIR, "auth.db"))
    AUTH_DEFAULT_ADMIN_USERNAME = os.getenv("AUTH_DEFAULT_ADMIN_USERNAME", "admin")
    AUTH_DEFAULT_ADMIN_PASSWORD = os.getenv("AUTH_DEFAULT_ADMIN_PASSWORD", "admin123")
    AUTH_SESSION_TTL_HOURS = int(os.getenv("AUTH_SESSION_TTL_HOURS", "24"))
    PROVIDER = Provider(os.getenv("PROVIDER", "ollama").lower())
    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_LLM_MODEL = os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:32b")
    OLLAMA_EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text:latest")
    CUSTOM_API_KEY = os.getenv("CUSTOM_API_KEY", "")
    CUSTOM_BASE_URL = os.getenv("CUSTOM_BASE_URL", "")
    CUSTOM_LLM_MODEL = os.getenv("CUSTOM_LLM_MODEL", "")
    CUSTOM_EMBEDDING_MODEL = os.getenv("CUSTOM_EMBEDDING_MODEL", "")
    CUSTOM_EMBEDDING_API_KEY = os.getenv("CUSTOM_EMBEDDING_API_KEY", "")
    CUSTOM_EMBEDDING_BASE_URL = os.getenv("CUSTOM_EMBEDDING_BASE_URL", "")
    CUSTOM_CONTEXT_WINDOW = int(os.getenv("CUSTOM_CONTEXT_WINDOW", "128000"))
    CUSTOM_MAX_TOKENS = int(os.getenv("CUSTOM_MAX_TOKENS", "4096"))
    USE_OLLAMA_EMBEDDING = os.getenv("USE_OLLAMA_EMBEDDING", "false").lower() == "true"
    CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "512"))
    CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "50"))
    VECTOR_TOP_K = int(os.getenv("VECTOR_TOP_K", "20"))
    BM25_TOP_K = int(os.getenv("BM25_TOP_K", "20"))
    FINAL_TOP_K = int(os.getenv("FINAL_TOP_K", "5"))
    RRF_K = int(os.getenv("RRF_K", "60"))
    RERANKER_TYPE = RerankerType(os.getenv("RERANKER_TYPE", "none").lower())
    RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-gemma")
    RERANKER_API_KEY = os.getenv("RERANKER_API_KEY", "")
    RERANKER_API_BASE = os.getenv("RERANKER_API_BASE", "https://api.openai.com/v1")
    SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", DEFAULT_VALUES["SYSTEM_PROMPT"])
    NO_CONTEXT_PROMPT = os.getenv("NO_CONTEXT_PROMPT", DEFAULT_VALUES["NO_CONTEXT_PROMPT"])


def _format_env_value(value: str) -> str:
    """格式化 .env 值，包含 # 或空格时用引号包裹"""
    if '#' in value or '"' in value or ' ' in value or '\n' in value:
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    return value


def save_settings_to_env(settings_dict: dict, env_path: str = None):
    """
    智能更新 .env 文件：保留注释和格式，只更新 KEY=VALUE 行。
    对于文件中没有的新 key，追加到末尾。
    """
    if env_path is None:
        env_path = os.path.join(BASE_DIR, ".env")

    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    updated_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        # 跳过注释和空行
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        # 解析 KEY=VALUE（忽略行内注释）
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in settings_dict:
                new_lines.append(f"{key}={_format_env_value(settings_dict[key])}\n")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    # 追加文件中没有的新 key
    for key, value in settings_dict.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={_format_env_value(value)}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
