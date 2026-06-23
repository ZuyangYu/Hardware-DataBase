# src/core/model_factory.py
import os
import config.settings
from llama_index.core import Settings
from llama_index.llms.ollama import Ollama
from llama_index.embeddings.ollama import OllamaEmbedding
from src.core.custom_embedding import OpenRouterEmbedding
from llama_index.core.postprocessor import SentenceTransformerRerank
from llama_index.core.node_parser import SentenceSplitter
from src.core.logger import log, error, warn
from src.core.custom_reranker import APIReranker, NoReranker
from src.core.custom_llm import GenericOpenAILLM


_generation_model_signature = None


def _current_generation_model_signature() -> tuple:
    if config.settings.PROVIDER == config.settings.Provider.OLLAMA:
        return (
            "ollama",
            config.settings.OLLAMA_BASE_URL,
            config.settings.OLLAMA_LLM_MODEL,
        )
    return (
        "custom",
        config.settings.CUSTOM_BASE_URL,
        config.settings.CUSTOM_LLM_MODEL,
        config.settings.CUSTOM_API_KEY,
        config.settings.CUSTOM_CONTEXT_WINDOW,
        config.settings.CUSTOM_MAX_TOKENS,
    )


def init_global_models():
    """
    初始化全局模型配置
    这是整个 RAG 系统的模型的入口函数，负责初始化：
    - LLM（大语言模型）
    - Embedding（文本向量化模型）
    - Reranker（重排序模型）
    """
    try:
        log("=" * 60)
        log(f"初始化模型 Provider: {config.settings.PROVIDER.name.upper()}")
        log("=" * 60)

        # 1. 初始化 LLM
        init_generation_model(force=True)

        # 2. 初始化 Embedding
        _init_embedding()

        # 3. 初始化 Reranker
        _init_reranker()

        # 4. 初始化 分词器
        text_splitter = SentenceSplitter(
            chunk_size=config.settings.CHUNK_SIZE,
            chunk_overlap=config.settings.CHUNK_OVERLAP
        )

        Settings.text_splitter = text_splitter
        Settings.node_parser = text_splitter
        log(f"全局分词器已配置: Size={config.settings.CHUNK_SIZE}, Overlap={config.settings.CHUNK_OVERLAP}")

        log("=" * 60)
        log("✅ 全局模型初始化完成")
        log("=" * 60)

    except Exception as e:
        error(f"❌ 模型初始化失败: {e}")
        raise


def init_generation_model(force: bool = False):
    """Initialize only the LLM used to generate answers."""
    global _generation_model_signature
    signature = _current_generation_model_signature()
    if not force and Settings.llm is not None and _generation_model_signature == signature:
        return
    _init_llm()
    _generation_model_signature = signature


def _init_llm():
    """
    初始化 LLM 模型
    根据 PROVIDER 配置选择：
    - ollama: 本地 Ollama 服务
    - custom: 第三方 API 服务
    """
    try:
        if config.settings.PROVIDER == config.settings.Provider.OLLAMA:
            # ==================== Ollama 本地模型 ====================
            Settings.llm = Ollama(
                model=config.settings.OLLAMA_LLM_MODEL,
                base_url=config.settings.OLLAMA_BASE_URL,
                request_timeout=360
            )
            log(f"LLM: Ollama")
            log(f"   ├─ 模型: {config.settings.OLLAMA_LLM_MODEL}")
            log(f"   └─ 地址: {config.settings.OLLAMA_BASE_URL}")

        elif config.settings.PROVIDER == config.settings.Provider.CUSTOM:
            # ==================== 自定义 API =========================
            # 验证必要参数
            if not config.settings.CUSTOM_API_KEY or not config.settings.CUSTOM_BASE_URL or not config.settings.CUSTOM_LLM_MODEL:
                error("❌ CUSTOM 模式需要设置以下参数:")
                error("   - CUSTOM_API_KEY")
                error("   - CUSTOM_BASE_URL")
                error("   - CUSTOM_LLM_MODEL")
                raise ValueError("CUSTOM 模式配置不完整")

            # 使用自定义LLM封装
            Settings.llm = GenericOpenAILLM(
                model=config.settings.CUSTOM_LLM_MODEL,
                api_key=config.settings.CUSTOM_API_KEY,
                api_base=config.settings.CUSTOM_BASE_URL,
                context_window=config.settings.CUSTOM_CONTEXT_WINDOW,
                max_tokens=config.settings.CUSTOM_MAX_TOKENS
            )

            log(f"LLM: 自定义 API")
            log(f"   ├─ 模型: {config.settings.CUSTOM_LLM_MODEL}")
            log(f"   ├─ 地址: {config.settings.CUSTOM_BASE_URL}")
            log(f"   ├─ 上下文: {config.settings.CUSTOM_CONTEXT_WINDOW}")
            log(f"   └─ 最大Token: {config.settings.CUSTOM_MAX_TOKENS}")

        else:
            raise ValueError(f"❌ 未知的 Provider: {config.settings.PROVIDER}")

    except Exception as e:
        error(f"❌ LLM 初始化失败: {e}")
        raise


def _init_embedding():
    """
    初始化 Embedding 模型

    根据配置选择：
    - Ollama Embedding（本地，免费）
    - 第三方 API Embedding（可能需要付费）
    注意：很多第三方 API 不支持 Embedding，推荐使用本地 Ollama
    """
    try:
        if config.settings.PROVIDER == config.settings.Provider.OLLAMA or config.settings.USE_OLLAMA_EMBEDDING:
            # ==================== Ollama Embedding ====================
            # 两种情况会使用 Ollama：
            # 1. Provider 本身就是 ollama
            # 2. 强制设置 USE_OLLAMA_EMBEDDING=true（混合模式）

            Settings.embed_model = OllamaEmbedding(
                model_name=config.settings.OLLAMA_EMBEDDING_MODEL,
                base_url=config.settings.OLLAMA_BASE_URL
            )

            if config.settings.USE_OLLAMA_EMBEDDING and config.settings.PROVIDER == config.settings.Provider.CUSTOM: # 判断是不是混合模式
                log(f"Embedding: Ollama（混合模式）")
                log(f"LLM 用第三方 API，Embedding 用本地 Ollama")
            else:
                log(f"Embedding: Ollama")

            log(f"   ├─ 模型: {config.settings.OLLAMA_EMBEDDING_MODEL}")
            log(f"   └─ 地址: {config.settings.OLLAMA_BASE_URL}")

        elif config.settings.PROVIDER == config.settings.Provider.CUSTOM:
            # ==================== 第三方 API Embedding ====================
            if not config.settings.CUSTOM_EMBEDDING_MODEL:
                warn("未设置 CUSTOM_EMBEDDING_MODEL，请设置 USE_OLLAMA_EMBEDDING=true 使用本地 Ollama")
                Settings.embed_model = None
            else:
                # 优先使用 Embedding 专用 API，未设置则回退到 LLM 的 API
                emb_api_key = config.settings.CUSTOM_EMBEDDING_API_KEY or config.settings.CUSTOM_API_KEY
                emb_api_base = config.settings.CUSTOM_EMBEDDING_BASE_URL or config.settings.CUSTOM_BASE_URL
                Settings.embed_model = OpenRouterEmbedding(
                    model=config.settings.CUSTOM_EMBEDDING_MODEL,
                    api_key=emb_api_key,
                    api_base=emb_api_base
                )
                log(f"Embedding: 自定义 API")
                log(f"   ├─ 模型: {config.settings.CUSTOM_EMBEDDING_MODEL}")
                log(f"   └─ 地址: {emb_api_base}")

        else:
            raise ValueError(f"❌ 未知的 Provider: {config.settings.PROVIDER}")

    except Exception as e:
        error(f"❌ Embedding 初始化失败: {e}")
        raise


def _init_reranker():
    """
    初始化 Reranker 模型

    Reranker 用于对检索结果进行二次排序，提高精度

    支持三种模式：
    - none: 不使用 Reranker（最快，精度一般）
    - local: 本地 Sentence Transformer 模型（精度高，需要下载模型）
    - api: 使用 API 服务（需要付费）
    """
    try:
        if config.settings.RERANKER_TYPE == config.settings.RerankerType.NONE:
            # ==================== 不使用 Reranker ====================
            Settings.node_postprocessors = [NoReranker(top_n=config.settings.FINAL_TOP_K)]
            log(f"Reranker: 不使用（直接返回 Top {config.settings.FINAL_TOP_K}）")

        elif config.settings.RERANKER_TYPE == config.settings.RerankerType.LOCAL:
            # ==================== 本地 Reranker ====================
            # 设置模型缓存目录, SentenceTransformer 会自动检查此目录
            # 如果模型已存在,则直接加载,否则会自动下载
            os.environ['SENTENCE_TRANSFORMERS_HOME'] = config.settings.RERANKER_CACHE

            Settings.node_postprocessors = [
                SentenceTransformerRerank(
                    top_n=config.settings.FINAL_TOP_K,
                    model=config.settings.RERANKER_MODEL
                )
            ]
            log(f"Reranker: 本地模型")
            log(f"   ├─ 模型: {config.settings.RERANKER_MODEL}")
            log(f"   ├─ Top-N: {config.settings.FINAL_TOP_K}")
            log(f"   └─ 缓存/本地路径: {config.settings.RERANKER_CACHE}")

        elif config.settings.RERANKER_TYPE == config.settings.RerankerType.API:
            # ==================== API Reranker ====================
            api_key = config.settings.RERANKER_API_KEY or config.settings.CUSTOM_API_KEY

            if not api_key:
                warn("Reranker API Key 未设置")

            Settings.node_postprocessors = [
                APIReranker(
                    model=config.settings.RERANKER_MODEL,
                    api_key=api_key,
                    api_base=config.settings.RERANKER_API_BASE,
                    top_n=config.settings.FINAL_TOP_K
                )
            ]
            log(f"Reranker: API")
            log(f"   ├─ 模型: {config.settings.RERANKER_MODEL}")
            log(f"   ├─ 地址: {config.settings.RERANKER_API_BASE}")
            log(f"   └─ Top-N: {config.settings.FINAL_TOP_K}")

        else:
            warn(f"未知的 Reranker 类型: {config.settings.RERANKER_TYPE}")
            Settings.node_postprocessors = [NoReranker(top_n=config.settings.FINAL_TOP_K)]

    except Exception as e:
        error(f"❌ Reranker 初始化失败: {e}")
        warn("降级到不使用 Reranker")
        Settings.node_postprocessors = [NoReranker(top_n=config.settings.FINAL_TOP_K)]


def get_current_config() -> dict:
    """
    获取当前配置信息（用于调试和监控）

    Returns:
        dict: 当前配置的字典
    """
    current_config = {
        "provider": config.settings.PROVIDER.name,
        "llm_model": config.settings.OLLAMA_LLM_MODEL if config.settings.PROVIDER == config.settings.Provider.OLLAMA else config.settings.CUSTOM_LLM_MODEL,
        "embedding_model": config.settings.OLLAMA_EMBEDDING_MODEL if (
                config.settings.PROVIDER == config.settings.Provider.OLLAMA or config.settings.USE_OLLAMA_EMBEDDING) else config.settings.CUSTOM_EMBEDDING_MODEL,
        "use_ollama_embedding": config.settings.USE_OLLAMA_EMBEDDING,
        "reranker_type": config.settings.RERANKER_TYPE.name,
        "reranker_model": config.settings.RERANKER_MODEL if config.settings.RERANKER_TYPE != config.settings.RerankerType.NONE else None,
        "final_top_k": config.settings.FINAL_TOP_K,
        "chunk_size": config.settings.CHUNK_SIZE,
        "chunk_overlap": config.settings.CHUNK_OVERLAP,
    }
    return current_config


def print_config():
    """
    打印当前配置（用于调试）
    在启动时调用此函数可以查看完整的配置信息
    """
    current_config = get_current_config()
    log("\n" + "=" * 60)
    log("当前配置")
    log("=" * 60)
    for key, value in current_config.items():
        log(f"   {key}: {value}")
    log("=" * 60 + "\n")


def validate_config() -> tuple[bool, list[str]]:
    """
    验证配置是否完整

    Returns:
        tuple: (是否有效, 错误信息列表)
    """
    errors = []

    # 验证 Provider
    if config.settings.PROVIDER == config.settings.Provider.CUSTOM:
        if not config.settings.CUSTOM_API_KEY:
            errors.append("缺少 CUSTOM_API_KEY")
        if not config.settings.CUSTOM_BASE_URL:
            errors.append("缺少 CUSTOM_BASE_URL")
        if not config.settings.CUSTOM_LLM_MODEL:
            errors.append("缺少 CUSTOM_LLM_MODEL")

        # 如果不使用 Ollama Embedding，检查 Embedding 配置
        if not config.settings.USE_OLLAMA_EMBEDDING and not config.settings.CUSTOM_EMBEDDING_MODEL:
            errors.append("缺少 CUSTOM_EMBEDDING_MODEL（或设置 USE_OLLAMA_EMBEDDING=true）")

    elif config.settings.PROVIDER == config.settings.Provider.OLLAMA:
        if not config.settings.OLLAMA_BASE_URL:
            errors.append("缺少 OLLAMA_BASE_URL")
        if not config.settings.OLLAMA_LLM_MODEL:
            errors.append("缺少 OLLAMA_LLM_MODEL")
        if not config.settings.OLLAMA_EMBEDDING_MODEL:
            errors.append("缺少 OLLAMA_EMBEDDING_MODEL")

    return len(errors) == 0, errors


if __name__ != "__main__":
    is_valid, errors = validate_config()
    if not is_valid:
        warn("配置验证失败:")
        for err in errors:
            warn(f"- {err}")
