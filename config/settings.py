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


class Provider(Enum):
    """
    AI 服务提供商
    只保留两种：
    - ollama: 本地 Ollama 服务
    - custom: 所有第三方 API（OpenAI、OpenRouter、DeepSeek、Grok 等）
    """
    OLLAMA = "ollama"
    CUSTOM = "custom"


PROVIDER = Provider(os.getenv("PROVIDER", "ollama").lower())

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


RERANKER_TYPE = RerankerType(os.getenv("RERANKER_TYPE", "none").lower())
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-gemma")
RERANKER_API_KEY = os.getenv("RERANKER_API_KEY", "")
RERANKER_API_BASE = os.getenv("RERANKER_API_BASE", "https://api.openai.com/v1")

# 确保目录存在
os.makedirs(DATA_ROOT, exist_ok=True)
os.makedirs(STORAGE_DIR, exist_ok=True)
os.makedirs(CHROMA_PATH, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(RERANKER_CACHE, exist_ok=True)


def get_kb_storage_path(kb_name: str) -> str:
    """获取知识库索引元数据(docstore.json等)的持久化路径"""
    # 将元数据存放在 storage/index_stores/知识库名/ 下
    path = os.path.join(STORAGE_DIR, "index_stores", kb_name)
    os.makedirs(path, exist_ok=True)
    return path
