# src/streamlit_app.py
import os
import tempfile
import html
import streamlit as st
import time
from src.core.rag_pipeline import RAGPipeline
from src.core.resource_manager import resource_manager
import config.settings

# ==================== 页面配置 ========================
st.set_page_config(
    page_title="HardWare RAG",
    page_icon="😺",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ==================== CSS 样式配置 =====================
st.markdown("""
<style>
    /* ========== 1. 全局与容器调整 ========== */
    /* 核心修复：消除顶部默认内边距，防止滚动时的回弹计算误差 */
    .block-container {
        padding-top: 0rem !important;
        padding-bottom: 5rem !important; /* 底部留白给输入框 */
    }
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}

    /* ========== 2. 侧边栏样式========== */
    .sidebar-main-title {
        font-size: 24px !important;
        font-weight: 700 !important;
        padding-top: 5px !important;
        padding-bottom: 15px !important; /* 调整与下方分割线的距离 */
    }

    section[data-testid="stSidebar"] p {
        font-size: 16px !important;
        line-height: 1.8 !important;
    }

    /* --- 增大选项字体 & 对齐圆点 --- */
    [data-testid="stRadio"] label {
        display: flex !important;
        align-items: center !important; /* 垂直对齐圆点和文字 */
        margin-bottom: 20px !important; /* 增加选项间距 */
    }
    [data-testid="stRadio"] span {
        font-size: 18px !important; /* 增大选项字体 */
        font-weight: 700 !important;
    }

    section[data-testid="stSidebar"] h3:not(.sidebar-main-title) {
        font-size: 20px !important;
        padding-top: 5px !important;
        padding-bottom: 30px !important;
    }
    section[data-testid="stSidebar"] hr {
        margin-top: 1rem !important;
        margin-bottom: 1rem !important;
    }

    /* ========== 3. 状态指示灯 ========== */
    .status-indicator {
        display: inline-block;
        width: 10px;
        height: 10px;
        border-radius: 50%;
        margin-right: 5px;
    }
    .status-error { background-color: #f44336; }
    .status-ok { background-color: #4caf50; }


    /* ========== 4. 聊天界面样式  ========== */
    [data-testid="stChatMessageContent"] {
        background-color: #f0f2f6;
        border-radius: 10px;
        padding: 10px 15px;
        border-top-left-radius: 0;
        margin-right: 40%;
        font-size: 20px !important;
        margin-top: 20px !important;
    }

    .user-chat-container {
        display: flex;
        justify-content: flex-end;
        align-items: flex-start;
        margin-bottom: 20px;
    }

    .user-avatar {
        width: 30px;
        height: 30px;
        font-size: 32px;
        margin-left: 3px;
        margin-right: 15px;
        display: flex;
        align-items: flex-start;
        padding-top: 0px;
    }

    .user-bubble {
        background-color: transparent;
        border: 1px solid #e0e0e0;
        color: inherit;
        padding: 8px 12px;
        border-radius: 12px;
        border-top-right-radius: 0;
        max-width: 80%;
        text-align: left;
        word-wrap: break-word;
        box-shadow: 0 1px 1px rgba(0,0,0,0.03);
        font-size: 20px !important;
        margin-top: 30px;
    }

    [data-testid="stChatMessage"] [data-testid="stChatMessageAvatar"] {
        width: 60px !important;
        height: 60px !important;
        min-width: 60px !important;
        margin-right: 15px !important;
    }

    [data-testid="stChatMessage"] [data-testid="stChatMessageAvatar"] > div {
        width: 60px !important;
        height: 60px !important;
        line-height: 60px !important;
        font-size: 40px !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        border-radius: 50% !important;
    }
</style>
""", unsafe_allow_html=True)


# ==================== 初始化逻辑 ========================
@st.cache_resource
def init_pipeline():
    """初始化 RAG Pipeline"""
    try:
        pipeline = RAGPipeline()
        pipeline.create_kb(config.settings.DEFAULT_KB_NAME)
        return pipeline, None
    except Exception as e:
        return None, str(e)


def init_session_state():
    """初始化会话状态"""
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "current_kb" not in st.session_state:
        st.session_state.current_kb = config.settings.DEFAULT_KB_NAME
    if "kb_list" not in st.session_state:
        st.session_state.kb_list = []
    if "show_create_kb" not in st.session_state:
        st.session_state.show_create_kb = False
    if "confirm_delete_file" not in st.session_state:
        st.session_state.confirm_delete_file = None
    if "confirm_delete_kb" not in st.session_state:
        st.session_state.confirm_delete_kb = None
    if "toast_msg" not in st.session_state:
        st.session_state.toast_msg = None
    if "error_msg" not in st.session_state:
        st.session_state.error_msg = None
    if "file_cache" not in st.session_state:
        st.session_state.file_cache = {}


# ==================== 逻辑处理回调函数 ===================
def create_kb_callback(pipeline):
    """创建知识库回调"""
    name = st.session_state.get("new_kb_name_input", "").strip()
    if not name:
        st.session_state.error_msg = "❌ 名称不能为空"
        return
    ok, msg = pipeline.create_kb(name)
    if ok:
        st.session_state.kb_list = pipeline.list_knowledge_bases()
        st.session_state.current_kb = name
        st.session_state.kb_selector = name
        st.session_state.show_create_kb = False
        st.session_state.toast_msg = msg
    else:
        st.session_state.error_msg = msg


def delete_kb_confirmed(pipeline, kb_name):
    """执行已确认的知识库删除"""
    pipeline.delete_knowledge_base(kb_name)
    invalidate_file_cache(kb_name)
    if st.session_state.current_kb == kb_name:
        st.session_state.current_kb = config.settings.DEFAULT_KB_NAME
        st.session_state.kb_selector = config.settings.DEFAULT_KB_NAME
        st.session_state.messages = []
    st.session_state.kb_list = pipeline.list_knowledge_bases()
    st.session_state.confirm_delete_kb = None
    st.session_state.toast_msg = f"已删除知识库: {kb_name}"


def switch_kb_callback(kb_name):
    """切换知识库回调"""
    st.session_state.current_kb = kb_name
    st.session_state.kb_selector = kb_name
    st.session_state.messages = []
    st.session_state.confirm_delete_file = None
    st.session_state.confirm_delete_kb = None


def refresh_kb_list(pipeline):
    st.session_state.kb_list = pipeline.list_knowledge_bases()


def get_cached_files(pipeline, kb_name: str) -> list[str]:
    if kb_name not in st.session_state.file_cache:
        st.session_state.file_cache[kb_name] = pipeline.list_files(kb_name)
    return st.session_state.file_cache[kb_name]


def invalidate_file_cache(kb_name: str):
    st.session_state.file_cache.pop(kb_name, None)


# ==================== Tab 3: 系统配置界面 ====================
def render_settings_tab():
    """系统配置页面 —— 允许用户在 UI 上修改所有配置"""
    st.markdown('<div style="height: 30px;"></div>', unsafe_allow_html=True)
    st.subheader("⚙️ 系统配置")
    st.caption("修改配置后点击「🔄 应用配置」生效。配置会保存到 .env 文件。")

    # ---- 辅助：从 config.settings 读取当前值，用于预填充 ----
    def _val(key, default=""):
        """读取当前配置值，不存在则返回默认值"""
        return getattr(config.settings, key, config.settings.DEFAULT_VALUES.get(key, default))

    # ==================== 🤖 模型配置 ====================
    with st.expander("🧠 模型配置", expanded=True):
        provider_options = ["ollama", "custom"]
        current_provider = _val("PROVIDER", "ollama")
        if isinstance(current_provider, config.settings.Provider):
            current_provider = current_provider.value

        provider = st.radio(
            "Provider（模型提供商）",
            options=provider_options,
            index=provider_options.index(current_provider),
            horizontal=True,
            key="cfg_provider",
            help="ollama = 本地模型 | custom = 第三方 API (OpenAI/OpenRouter/DeepSeek/...)"
        )

        if provider == "ollama":
            st.text_input("Ollama Base URL", value=_val("OLLAMA_BASE_URL"), key="cfg_ollama_base_url")
            st.text_input("Ollama LLM 模型", value=_val("OLLAMA_LLM_MODEL"), key="cfg_ollama_llm_model",
                          help="例: qwen2.5:32b")
            st.text_input("Ollama Embedding 模型", value=_val("OLLAMA_EMBEDDING_MODEL"), key="cfg_ollama_emb_model",
                          help="例: nomic-embed-text:latest")
        else:
            st.text_input("API Key", value=_val("CUSTOM_API_KEY"), key="cfg_custom_api_key", type="password")
            st.text_input("Base URL", value=_val("CUSTOM_BASE_URL"), key="cfg_custom_base_url",
                          help="例: https://api.openai.com/v1")
            st.text_input("LLM 模型", value=_val("CUSTOM_LLM_MODEL"), key="cfg_custom_llm_model",
                          help="例: gpt-4o, deepseek-chat")
            c1, c2 = st.columns(2)
            with c1:
                st.number_input("Context Window", min_value=1024, max_value=512000,
                                value=int(_val("CUSTOM_CONTEXT_WINDOW", "128000")),
                                step=1000, key="cfg_custom_ctx_window")
            with c2:
                st.number_input("Max Tokens", min_value=256, max_value=65536,
                                value=int(_val("CUSTOM_MAX_TOKENS", "4096")),
                                step=256, key="cfg_custom_max_tokens")

            use_ollama_emb = st.checkbox(
                "使用 Ollama 提供 Embedding（推荐，免费）",
                value=_val("USE_OLLAMA_EMBEDDING", False) == True or _val("USE_OLLAMA_EMBEDDING", "false") == True,
                key="cfg_use_ollama_emb",
                help="很多第三方 API 不支持 Embedding，建议开启"
            )
            if use_ollama_emb:
                st.text_input("Ollama Base URL", value=_val("OLLAMA_BASE_URL"), key="cfg_ollama_base_url")
                st.text_input("Ollama Embedding 模型", value=_val("OLLAMA_EMBEDDING_MODEL"), key="cfg_ollama_emb_model")
            else:
                st.text_input("Embedding API Key", value=_val("CUSTOM_EMBEDDING_API_KEY"),
                              key="cfg_custom_emb_api_key", type="password")
                st.text_input("Embedding Base URL", value=_val("CUSTOM_EMBEDDING_BASE_URL"),
                              key="cfg_custom_emb_base_url", help="例: https://api.siliconflow.cn/v1")
                st.text_input("Embedding 模型", value=_val("CUSTOM_EMBEDDING_MODEL"), key="cfg_custom_emb_model",
                              help="例: text-embedding-3-small")

    # ==================== 🔍 RAG 参数 ====================
    with st.expander("🔎 RAG 检索参数"):
        c1, c2 = st.columns(2)
        with c1:
            st.number_input("Chunk Size（分块大小）", min_value=64, max_value=4096,
                            value=int(_val("CHUNK_SIZE", "512")), step=64, key="cfg_chunk_size",
                            help="文档分块的大小（token 数）")
        with c2:
            st.number_input("Chunk Overlap（分块重叠）", min_value=0, max_value=2048,
                            value=int(_val("CHUNK_OVERLAP", "50")), step=10, key="cfg_chunk_overlap",
                            help="相邻分块的重叠量")

        st.divider()
        c3, c4, c5 = st.columns(3)
        with c3:
            st.number_input("Vector Top-K", min_value=1, max_value=100,
                            value=int(_val("VECTOR_TOP_K", "20")), key="cfg_vector_top_k",
                            help="向量检索返回数量")
        with c4:
            st.number_input("BM25 Top-K", min_value=1, max_value=100,
                            value=int(_val("BM25_TOP_K", "20")), key="cfg_bm25_top_k",
                            help="BM25 检索返回数量")
        with c5:
            st.number_input("Final Top-K", min_value=1, max_value=50,
                            value=int(_val("FINAL_TOP_K", "5")), key="cfg_final_top_k",
                            help="最终返回给 LLM 的文档数")
        st.number_input("RRF K（倒数排名融合参数）", min_value=1, max_value=200,
                        value=int(_val("RRF_K", "60")), key="cfg_rrf_k",
                        help="控制排名融合的平滑度，通常 60 效果好")

    # ==================== 🔄 Reranker 配置 ====================
    with st.expander("🔄 Reranker 配置"):
        reranker_options = ["none", "local", "api"]
        current_reranker = _val("RERANKER_TYPE", "none")
        if isinstance(current_reranker, config.settings.RerankerType):
            current_reranker = current_reranker.value

        reranker_type = st.selectbox(
            "Reranker 类型",
            options=reranker_options,
            index=reranker_options.index(current_reranker),
            key="cfg_reranker_type",
            help="none = 不使用 | local = 本地模型（需下载）| api = API 服务"
        )
        if reranker_type != "none":
            st.text_input("Reranker 模型", value=_val("RERANKER_MODEL"), key="cfg_reranker_model",
                          help="例: BAAI/bge-reranker-v2-m3")
        if reranker_type == "api":
            st.text_input("Reranker API Key", value=_val("RERANKER_API_KEY"), key="cfg_reranker_api_key", type="password")
            st.text_input("Reranker API Base", value=_val("RERANKER_API_BASE"), key="cfg_reranker_api_base")

    # ==================== 💬 系统提示词 ====================
    with st.expander("💬 系统提示词"):
        default_system_prompt = config.settings.DEFAULT_VALUES.get("SYSTEM_PROMPT", "")
        current_system_prompt = _val("SYSTEM_PROMPT", default_system_prompt)
        if isinstance(current_system_prompt, bool):
            current_system_prompt = default_system_prompt
        st.text_area(
            "System Prompt（系统提示词）",
            value=str(current_system_prompt),
            height=150,
            key="cfg_system_prompt",
            help="定义 AI 助手的人设和行为规则"
        )
        default_no_ctx = config.settings.DEFAULT_VALUES.get("NO_CONTEXT_PROMPT", "")
        current_no_ctx = _val("NO_CONTEXT_PROMPT", default_no_ctx)
        if isinstance(current_no_ctx, bool):
            current_no_ctx = default_no_ctx
        st.text_area(
            "无上下文提示词",
            value=str(current_no_ctx),
            height=80,
            key="cfg_no_context_prompt",
            help="知识库未检索到内容时使用的提示词"
        )

    # ==================== 操作按钮 ====================
    st.divider()
    col_apply, col_reset = st.columns(2)
    with col_apply:
        apply_clicked = st.button("🔄 应用配置", type="primary", use_container_width=True)
    with col_reset:
        reset_clicked = st.button("↩️ 恢复默认", type="secondary", use_container_width=True)

    if reset_clicked:
        _reset_to_defaults()

    if apply_clicked:
        _apply_settings()


def _validate_settings(new_settings: dict) -> list[str]:
    """验证配置值"""
    errors = []
    provider = new_settings.get("PROVIDER", "ollama")

    if provider == "ollama":
        if not new_settings.get("OLLAMA_BASE_URL"):
            errors.append("Ollama Base URL 不能为空")
        if not new_settings.get("OLLAMA_LLM_MODEL"):
            errors.append("Ollama LLM 模型名不能为空")
        if not new_settings.get("OLLAMA_EMBEDDING_MODEL"):
            errors.append("Ollama Embedding 模型名不能为空")
    elif provider == "custom":
        if not new_settings.get("CUSTOM_API_KEY"):
            errors.append("API Key 不能为空")
        if not new_settings.get("CUSTOM_BASE_URL"):
            errors.append("Base URL 不能为空")
        if not new_settings.get("CUSTOM_LLM_MODEL"):
            errors.append("LLM 模型名不能为空")
        use_ollama = new_settings.get("USE_OLLAMA_EMBEDDING", "false") == "true"
        if not use_ollama and not new_settings.get("CUSTOM_EMBEDDING_MODEL"):
            errors.append("未使用 Ollama Embedding 时，必须填写 Custom Embedding 模型名")

    for key in ["CHUNK_SIZE", "CHUNK_OVERLAP", "VECTOR_TOP_K", "BM25_TOP_K", "FINAL_TOP_K", "RRF_K",
                "CUSTOM_CONTEXT_WINDOW", "CUSTOM_MAX_TOKENS"]:
        val = new_settings.get(key, "")
        if val:
            try:
                v = int(val)
                if v <= 0:
                    errors.append(f"{key} 必须为正整数")
            except ValueError:
                errors.append(f"{key} 不是有效的整数: {val}")

    return errors


def _apply_settings():
    """应用配置：收集 → 验证 → 保存 .env → 刷新 → 重新初始化"""
    from src.ingestion.index_builder import clear_all_index_cache
    from src.core.custom_rag_chat import _context_cache

    new_settings = {}

    # ---- Provider & Model ----
    provider = st.session_state.get("cfg_provider", "ollama")
    new_settings["PROVIDER"] = provider

    if provider == "ollama":
        new_settings["OLLAMA_BASE_URL"] = st.session_state.get("cfg_ollama_base_url", "")
        new_settings["OLLAMA_LLM_MODEL"] = st.session_state.get("cfg_ollama_llm_model", "")
        new_settings["OLLAMA_EMBEDDING_MODEL"] = st.session_state.get("cfg_ollama_emb_model", "")
    else:
        new_settings["CUSTOM_API_KEY"] = st.session_state.get("cfg_custom_api_key", "")
        new_settings["CUSTOM_BASE_URL"] = st.session_state.get("cfg_custom_base_url", "")
        new_settings["CUSTOM_LLM_MODEL"] = st.session_state.get("cfg_custom_llm_model", "")
        new_settings["CUSTOM_CONTEXT_WINDOW"] = str(st.session_state.get("cfg_custom_ctx_window", 128000))
        new_settings["CUSTOM_MAX_TOKENS"] = str(st.session_state.get("cfg_custom_max_tokens", 4096))
        use_ollama_emb = st.session_state.get("cfg_use_ollama_emb", False)
        new_settings["USE_OLLAMA_EMBEDDING"] = "true" if use_ollama_emb else "false"
        if use_ollama_emb:
            new_settings["OLLAMA_BASE_URL"] = st.session_state.get("cfg_ollama_base_url", "")
            new_settings["OLLAMA_EMBEDDING_MODEL"] = st.session_state.get("cfg_ollama_emb_model", "")
        else:
            new_settings["CUSTOM_EMBEDDING_API_KEY"] = st.session_state.get("cfg_custom_emb_api_key", "")
            new_settings["CUSTOM_EMBEDDING_BASE_URL"] = st.session_state.get("cfg_custom_emb_base_url", "")
            new_settings["CUSTOM_EMBEDDING_MODEL"] = st.session_state.get("cfg_custom_emb_model", "")

    # ---- RAG 参数 ----
    new_settings["CHUNK_SIZE"] = str(st.session_state.get("cfg_chunk_size", 512))
    new_settings["CHUNK_OVERLAP"] = str(st.session_state.get("cfg_chunk_overlap", 50))
    new_settings["VECTOR_TOP_K"] = str(st.session_state.get("cfg_vector_top_k", 20))
    new_settings["BM25_TOP_K"] = str(st.session_state.get("cfg_bm25_top_k", 20))
    new_settings["FINAL_TOP_K"] = str(st.session_state.get("cfg_final_top_k", 5))
    new_settings["RRF_K"] = str(st.session_state.get("cfg_rrf_k", 60))

    # ---- Reranker ----
    reranker_type = st.session_state.get("cfg_reranker_type", "none")
    new_settings["RERANKER_TYPE"] = reranker_type
    if reranker_type != "none":
        new_settings["RERANKER_MODEL"] = st.session_state.get("cfg_reranker_model", "")
    if reranker_type == "api":
        new_settings["RERANKER_API_KEY"] = st.session_state.get("cfg_reranker_api_key", "")
        new_settings["RERANKER_API_BASE"] = st.session_state.get("cfg_reranker_api_base", "")

    # ---- 系统提示词 ----
    system_prompt = st.session_state.get("cfg_system_prompt", "")
    no_context_prompt = st.session_state.get("cfg_no_context_prompt", "")
    if system_prompt:
        new_settings["SYSTEM_PROMPT"] = system_prompt
    if no_context_prompt:
        new_settings["NO_CONTEXT_PROMPT"] = no_context_prompt

    # ---- 验证 ----
    errors = _validate_settings(new_settings)
    if errors:
        for e in errors:
            st.error(e)
        return

    # ---- 保存并重新加载 ----
    try:
        # 1. 写入 .env
        config.settings.save_settings_to_env(new_settings)

        # 2. 刷新模块变量
        config.settings.reload_settings()

        # 3. 强制重新初始化模型
        resource_manager.initialize(force=True)

        # 4. 清除索引缓存（embedding 可能变了）
        clear_all_index_cache()

        # 5. 清除上下文缓存
        _context_cache.clear()

        # 6. 清除 Streamlit 缓存的 pipeline
        init_pipeline.clear()

        # 7. 清空对话历史（模型已变，旧上下文无效）
        st.session_state.messages = []

        st.session_state.toast_msg = "✅ 配置已更新并生效"
        st.rerun()

    except Exception as e:
        st.error(f"❌ 应用配置失败: {e}")
        st.warning("配置已保存到 .env，但模型初始化失败。请检查配置后点击「应用配置」重试。")


def _reset_to_defaults():
    """恢复默认配置"""
    from src.ingestion.index_builder import clear_all_index_cache
    from src.core.custom_rag_chat import _context_cache

    try:
        config.settings.save_settings_to_env(config.settings.DEFAULT_VALUES)
        config.settings.reload_settings()
        resource_manager.initialize(force=True)
        clear_all_index_cache()
        _context_cache.clear()
        init_pipeline.clear()
        st.session_state.messages = []
        st.session_state.toast_msg = "✅ 已恢复默认配置"
        st.rerun()
    except Exception as e:
        st.error(f"❌ 恢复默认配置失败: {e}")


# ==================== 主界面 ====================
def main():
    init_session_state()
    pipeline, error = init_pipeline()

    if st.session_state.toast_msg:
        st.toast(st.session_state.toast_msg)
        st.session_state.toast_msg = None
        time.sleep(0.5)

    if st.session_state.error_msg:
        st.error(st.session_state.error_msg)
        st.session_state.error_msg = None

    if error:
        st.error(f"❌ 系统初始化失败: {error}")

    # ------------------ 顶部栏 (应用更稳健的 CSS Sticky 效果) ------------------
    with st.container():
        st.markdown("""
            <div class="fixed-header-marker"></div>
            <style>
                /* 使用 :has 选择器精确定位头部容器 */
                div[data-testid="stVerticalBlock"] > div:has(div.fixed-header-marker) {
                    position: sticky;
                    top: 0.2rem;
                    background-color: white;
                    z-index: 999;
                    padding-top: 1rem;
                    padding-bottom: 10px;
                    border-bottom: 1px solid #f0f2f6;
                    margin-top: -2rem;
                }
            </style>
        """, unsafe_allow_html=True)

        col_header, col_status = st.columns([4, 1])
        with col_header:
            st.markdown('<h1 style="font-size: 35px; margin-top: 10px; margin-bottom: 0px;">😺 HardWare RAG</h1>', unsafe_allow_html=True)
            st.markdown(f"**正在使用知识库:** `{st.session_state.current_kb}`")
        with col_status:
            status = resource_manager.get_status()
            st.markdown(f"""
                <div style="text-align:right; padding-top:40px;">
                    <span class="status-indicator {'status-ok' if status.get('models_initialized') else 'status-error'}"></span> AI模型<br>
                    <span class="status-indicator {'status-ok' if status.get('chroma_connected') else 'status-error'}"></span> 向量库</div>
            """, unsafe_allow_html=True)

    # ------------------ 侧边栏 ------------------
    with st.sidebar:
        st.markdown('<h2 class="sidebar-main-title">😼 Hardware RAG导航</h2>', unsafe_allow_html=True)
        st.divider()

        selected_tab = st.radio("**🚩 功能切换:**", ["💬 智能对话", "📚 知识库管理", "⚙️ 系统配置"], label_visibility="collapsed")
        st.divider()

        # 设置页面：侧边栏显示当前配置概览
        if selected_tab == "⚙️ 系统配置":
            _provider = config.settings.PROVIDER.value if isinstance(config.settings.PROVIDER, config.settings.Provider) else str(config.settings.PROVIDER)
            st.markdown("**📍 当前模型配置:**")
            st.markdown(f"- **Provider:** `{_provider}`")

            if _provider == "ollama":
                st.markdown(f"- **LLM:** `{config.settings.OLLAMA_LLM_MODEL}`")
                st.markdown(f"- **Embedding:** `{config.settings.OLLAMA_EMBEDDING_MODEL}`")
            else:
                st.markdown(f"- **LLM:** `{config.settings.CUSTOM_LLM_MODEL}`")
                st.markdown(f"- **Base URL:** `{config.settings.CUSTOM_BASE_URL}`")
                if config.settings.USE_OLLAMA_EMBEDDING:
                    st.markdown(f"- **Embedding:** Ollama (`{config.settings.OLLAMA_EMBEDDING_MODEL}`)")
                else:
                    st.markdown(f"- **Embedding:** `{config.settings.CUSTOM_EMBEDDING_MODEL}`")

            _reranker = config.settings.RERANKER_TYPE.value if isinstance(config.settings.RERANKER_TYPE, config.settings.RerankerType) else str(config.settings.RERANKER_TYPE)
            st.markdown(f"- **Reranker:** `{_reranker}`")

            st.divider()
            st.markdown("**📍 当前 RAG 参数:**")
            st.markdown(f"- **Chunk Size:** {config.settings.CHUNK_SIZE}")
            st.markdown(f"- **Chunk Overlap:** {config.settings.CHUNK_OVERLAP}")
            st.markdown(f"- **Vector Top-K:** {config.settings.VECTOR_TOP_K}")
            st.markdown(f"- **BM25 Top-K:** {config.settings.BM25_TOP_K}")
            st.markdown(f"- **Final Top-K:** {config.settings.FINAL_TOP_K}")
            st.markdown(f"- **RRF K:** {config.settings.RRF_K}")
        else:
            # 其他页面需要 pipeline
            if not pipeline:
                st.warning("⚠️ 系统未初始化，请先在 ⚙️ 系统配置 中检查并修复配置")
                st.stop()

            st.markdown(f"**📍 当前对话挂载知识库:**")
            if st.session_state.current_kb not in st.session_state.kb_list:
                st.session_state.current_kb = config.settings.DEFAULT_KB_NAME
                if config.settings.DEFAULT_KB_NAME not in st.session_state.kb_list:
                    st.session_state.kb_list.append(config.settings.DEFAULT_KB_NAME)

            selected_kb = st.selectbox("切换知识库", options=st.session_state.kb_list, key="kb_selector")
            if selected_kb != st.session_state.current_kb:
                st.session_state.current_kb = selected_kb
                st.session_state.messages = []
                st.session_state.confirm_delete_file = None
                st.rerun()

            kb_files = get_cached_files(pipeline, st.session_state.current_kb)
            st.info(f"当前库包含 {len(kb_files)} 个文件")
            if kb_files:
                with st.expander("📚 查看库内文档"):
                    for f in kb_files:
                        st.markdown(f"- 📄 {f}")

            if selected_tab == "💬 智能对话":
                if st.button("🗑️ 清空对话", use_container_width=True, type="secondary"):
                    st.session_state.messages = []
                    st.rerun()

        st.divider()
        st.markdown("<h3>🐱‍👓️ 说明与注意事项</h3>", unsafe_allow_html=True)

        if selected_tab == "💬 智能对话":
            st.warning("""
            **1. 对话说明:**
            - 回答基于当前知识库中的文档内容。
            - 可点击「📚 参考来源」查看引用的原始文档。

            **2. 上下文记忆:**
            - 保留最近 5 轮对话历史作为上下文。
            - 切换知识库会**清空当前对话**。

            **3. 回答质量:**
            - 如果知识库中没有相关内容，会明确告知。
            - 可在「⚙️ 系统配置」中调整检索参数提高质量。
            """)
        elif selected_tab == "📚 知识库管理":
            st.warning("""
            **1. 文件支持:**
            - 支持 PDF, TXT, MD, DOCX, CSV, HTML 格式文档。

            **2. 知识库操作:**
            - **新建**: 点击"➕ 新建"按钮。
            - **切换**: 在上方下拉框选择。
            - **删除**: 删除操作**不可恢复**。

            **3. 数据安全:**
            - 默认库 `source_documents` 不可被删除。
            - 删除文件会同时移除索引和物理文件。
            """)
        elif selected_tab == "⚙️ 系统配置":
            st.warning("""
            **1. 配置生效:**
            - 修改配置后点击「🔄 应用配置」立即生效。
            - 配置会持久化保存到 .env 文件。

            **2. 模型切换:**
            - 切换 Provider 或模型会**清空当前对话**。
            - API Key 使用密码输入，安全存储。

            **3. 恢复默认:**
            - 点击「↩️ 恢复默认」可还原所有配置。
            - 如初始化失败，仍可进入此页面修复配置。
            """)
        st.divider()
        st.caption("© 2025 HardWare RAG Assistant")

    # ------------------ 页面内容分发 ------------------
    if selected_tab == "💬 智能对话":
        render_chat_tab(pipeline)
    elif selected_tab == "📚 知识库管理":
        render_kb_management_tab(pipeline)
    elif selected_tab == "⚙️ 系统配置":
        render_settings_tab()


# ==================== Tab 1: 对话界面 ====================
def render_chat_tab(pipeline):
    st.markdown('<div style="height: 30px;"></div>', unsafe_allow_html=True)

    # 1. 渲染历史消息
    if not st.session_state.messages:
        st.markdown("""
            <div style='text-align:center; color:#888; padding-top:180px;'>
                <h3 style="margin-top:100px;">🙌 硬件文档检索助手</h3>
                <p>请问有什么可以帮您？</p>
            </div>
        """, unsafe_allow_html=True)
    else:
        for msg in st.session_state.messages:
            role = msg["role"]
            content = msg["content"]
            if role == "user":
                safe_content = html.escape(content).replace("\n", "<br>")
                st.markdown(f"""
                    <div class="user-chat-container">
                        <div class="user-bubble">{safe_content}</div>
                        <div class="user-avatar">🧑</div>
                    </div>
                """, unsafe_allow_html=True)
            else:
                with st.chat_message("assistant", avatar="😽"):
                    # 检查是否是错误消息
                    if content.startswith("Error:") or content == "Empty response.":
                        st.error(content)
                    else:
                        # ==================== 核心修复开始 ====================
                        separator = "**🔍 检索到的上下文:**"

                        main_text = content

                        if separator in content:
                            try:
                                parts = content.split(separator, 1)
                                if len(parts) == 2:
                                    main_text = parts[0]
                                    source_text = parts[1]

                                    st.markdown(main_text.strip())
                                    with st.expander("📚 参考来源"):
                                        st.markdown(source_text.strip())
                                else:
                                    st.markdown(content)
                            except ValueError:
                                st.markdown(content)
                        else:
                            st.markdown(content)

    # 2. 检查并处理新的流式响应
    if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
        user_input_to_process = st.session_state.messages[-1]["content"]

        chat_history = []
        messages_for_history = st.session_state.messages[:-1]
        user_msg = None
        for msg in messages_for_history:
            if msg["role"] == "user":
                user_msg = msg["content"]
            elif msg["role"] == "assistant" and user_msg is not None:
                chat_history.append((user_msg, msg["content"]))
                user_msg = None

        with st.chat_message("assistant", avatar="😽"):
            error_occured = None
            try:
                gen = pipeline.query(user_input_to_process, st.session_state.current_kb, chat_history[-5:])
            except Exception as e:
                error_occured = str(e)

            if error_occured:
                st.error(f"❌ 处理请求时发生错误: {error_occured}")
                full_response = f"Error: {error_occured}"
            else:
                with st.status("正在检索相关文档...", expanded=False) as status:
                    full_response = st.write_stream(gen)
                    status.update(label="回答生成完毕", state="complete", expanded=False)
                if not full_response or not full_response.strip():
                    st.warning("⚠️ AI 未生成任何内容。")
                    full_response = "Empty response."

        # 将最终结果存入历史记录并刷新
        st.session_state.messages.append({"role": "assistant", "content": full_response})
        st.rerun()

    # --- 输入框 ---
    if prompt := st.chat_input("请输入问题..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.rerun()


# ==================== Tab 2: 管理界面 ====================
def render_kb_management_tab(pipeline):
    st.markdown('<div style="height: 30px;"></div>', unsafe_allow_html=True)
    st.subheader("📚 知识库管理")
    with st.container(border=True):
        st.markdown("##### 📤 当前知识库上传文档")
        files = st.file_uploader("拖拽文件到此处", accept_multiple_files=True,
                                 type=["pdf", "txt", "md", "docx", "html", "csv", "xlsx"])
        if files and st.button("开始上传", type="primary"):
            with st.status("处理中...", expanded=True) as status:
                st.write("保存临时文件...")
                temp_paths = []
                temp_dir = tempfile.gettempdir()
                for f in files:
                    path = os.path.join(temp_dir, f.name)
                    with open(path, "wb") as wb:
                        wb.write(f.getbuffer())
                    temp_paths.append(path)
                st.write("正在建立索引...")
                res = pipeline.upload_files(temp_paths, st.session_state.current_kb)
                for p in temp_paths:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                status.update(label="✅ 完成", state="complete", expanded=False)
            invalidate_file_cache(st.session_state.current_kb)
            st.success(res.split('\n')[0])
            time.sleep(1)
            st.rerun()
    st.divider()

    st.markdown("##### 📁 知识库列表")
    col_kbs, col_new = st.columns([9, 1])
    with col_kbs:
        st.caption(f"共有 {len(st.session_state.kb_list)} 个知识库")
    with col_new:
        if st.button("➕ 新建"):
            st.session_state.show_create_kb = True

    if st.session_state.show_create_kb:
        with st.container(border=True):
            st.markdown("###### 新建知识库")
            with st.form("new_kb_form"):
                st.text_input("输入新知识库名称", placeholder="例如: project_alpha", key="new_kb_name_input")
                st.form_submit_button("确认创建", on_click=create_kb_callback, args=(pipeline,))
            if st.button("取消", key="cancel_create_kb"):
                st.session_state.show_create_kb = False
                st.rerun()

    for kb in st.session_state.kb_list:
        files = get_cached_files(pipeline, kb)
        is_current = (kb == st.session_state.current_kb)
        with st.expander(f"{'🟢' if is_current else '⚪'} {kb} ({len(files)} 文件)", expanded=is_current):
            if files:
                st.markdown("**📄 文件列表:**")
                container_kwargs = {"border": True}
                if len(files) > 5:
                    container_kwargs["height"] = 300
                with st.container(**container_kwargs):
                    for f in files:
                        c1, c2 = st.columns([0.80, 0.20])
                        with c1:
                            st.text(f)
                        with c2:
                            current_confirm = st.session_state.confirm_delete_file
                            is_confirming = (current_confirm == (kb, f))
                            if is_confirming:
                                sub_c1, sub_c2 = st.columns([1, 1])
                                with sub_c1:
                                    if st.button("✓", key=f"yes_f_{kb}_{f}", help="确认删除"):
                                        with st.spinner("删除中..."):
                                            res = pipeline.delete_document(f, kb)
                                            invalidate_file_cache(kb)
                                            st.session_state.confirm_delete_file = None
                                            if "✅" in res:
                                                st.session_state.toast_msg = f"已删除: {f}"
                                            else:
                                                st.session_state.error_msg = res
                                            st.rerun()
                                with sub_c2:
                                    if st.button("✗", key=f"no_f_{kb}_{f}", help="取消"):
                                        st.session_state.confirm_delete_file = None
                                        st.rerun()
                            else:
                                if st.button("🗑️", key=f"del_f_{kb}_{f}", help="删除文件"):
                                    st.session_state.confirm_delete_file = (kb, f)
                                    st.rerun()
            else:
                st.caption("暂无文件")

            st.divider()
            col_switch, col_del = st.columns([1, 1])
            with col_switch:
                if not is_current:
                    st.button("🔄 切换到此知识库", key=f"btn_switch_{kb}", on_click=switch_kb_callback, args=(kb,))
                else:
                    st.button("✅ 当前使用中", disabled=True, key=f"btn_cur_{kb}")
            with col_del:
                if kb != config.settings.DEFAULT_KB_NAME:
                    if st.session_state.confirm_delete_kb == kb:
                        st.markdown("**确认删除?**")
                        sub_c1, sub_c2 = st.columns([1, 1])
                        with sub_c1:
                            st.button("✅ 是", key=f"yes_kb_{kb}", on_click=delete_kb_confirmed, args=(pipeline, kb))
                        with sub_c2:
                            if st.button("❌ 否", key=f"no_kb_{kb}"):
                                st.session_state.confirm_delete_kb = None
                                st.rerun()
                    else:
                        if st.button("🗑️ 删除整个库", key=f"del_kb_{kb}"):
                            st.session_state.confirm_delete_kb = kb
                            st.rerun()


if __name__ == "__main__":
    main()
