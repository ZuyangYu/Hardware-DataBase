# src/streamlit_app.py
import os
import tempfile
import html
import streamlit as st
import streamlit.components.v1 as components
import time
from src.core.rag_pipeline import RAGPipeline
from src.core.resource_manager import resource_manager
from src.core.auth import AuthService, ROLE_DEPT_ADMIN, ROLE_SYSTEM_ADMIN, ROLE_USER, build_request_context, ensure_session_id
from src.core.app_logs import AppLogService
from src.core.conversation import ConversationService
import config.settings

AUTH_QUERY_PARAM = "hd_session"

# ==================== 页面配置 ========================
st.set_page_config(
    page_title="Hardware DataBase",
    page_icon="assets/favicon.svg",
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


def init_auth_service():
    return AuthService()


@st.cache_resource
def init_conversation_service():
    return ConversationService()


@st.cache_resource
def init_log_service():
    return AppLogService()


def init_session_state():
    """初始化会话状态"""
    ensure_session_id(st.session_state)
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = not config.settings.AUTH_ENABLED
    if "username" not in st.session_state:
        st.session_state.username = None
    if "role" not in st.session_state:
        st.session_state.role = None
    if "department_id" not in st.session_state:
        st.session_state.department_id = None
    if "user_id" not in st.session_state:
        st.session_state.user_id = None
    if "auth_token" not in st.session_state:
        st.session_state.auth_token = None
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "chat_session_id" not in st.session_state:
        st.session_state.chat_session_id = None
    if "loaded_chat_session_id" not in st.session_state:
        st.session_state.loaded_chat_session_id = None
    if "chat_session_kb" not in st.session_state:
        st.session_state.chat_session_kb = None
    if "pending_user_message_id" not in st.session_state:
        st.session_state.pending_user_message_id = None
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


def read_auth_token_from_url() -> str | None:
    token = st.query_params.get(AUTH_QUERY_PARAM)
    if isinstance(token, list):
        token = token[0] if token else None
    return token or None


def persist_auth_token(token: str):
    st.session_state.auth_token = token
    st.query_params.update({AUTH_QUERY_PARAM: token})
    time.sleep(0.2)


def clear_persisted_auth_token():
    st.session_state.auth_token = None
    if AUTH_QUERY_PARAM in st.query_params:
        del st.query_params[AUTH_QUERY_PARAM]


def render_auth_restore_script():
    if read_auth_token_from_url():
        return
    components.html(f"""
        <script>
        const token = window.parent.localStorage.getItem("{AUTH_QUERY_PARAM}");
        const params = new URLSearchParams(window.parent.location.search);
        if (token && !params.has("{AUTH_QUERY_PARAM}")) {{
            params.set("{AUTH_QUERY_PARAM}", token);
            window.parent.location.search = params.toString();
        }}
        </script>
    """, height=0)


def render_auth_store_script(token: str):
    components.html(f"""
        <script>
        window.parent.localStorage.setItem("{AUTH_QUERY_PARAM}", "{token}");
        </script>
    """, height=0)


def render_auth_clear_script():
    components.html(f"""
        <script>
        window.parent.localStorage.removeItem("{AUTH_QUERY_PARAM}");
        </script>
    """, height=0)


def refresh_auth_state():
    if not config.settings.AUTH_ENABLED:
        st.session_state.authenticated = True
        return

    token = st.session_state.get("auth_token") or read_auth_token_from_url()
    if not token:
        st.session_state.authenticated = False
        return
    st.session_state.auth_token = token

    user = init_auth_service().get_user_by_token(token)
    if user is None:
        st.session_state.authenticated = False
        st.session_state.username = None
        st.session_state.role = None
        st.session_state.department_id = None
        st.session_state.user_id = None
        clear_persisted_auth_token()
        reset_chat_state()
        return

    st.session_state.authenticated = True
    st.session_state.username = user.username
    st.session_state.role = user.role
    st.session_state.department_id = user.department_id
    st.session_state.user_id = user.id


def reset_chat_state():
    st.session_state.messages = []
    st.session_state.chat_session_id = None
    st.session_state.loaded_chat_session_id = None
    st.session_state.chat_session_kb = None


def load_chat_session(session_id: int):
    user_id = st.session_state.get("user_id")
    if not user_id:
        return

    conversation_service = init_conversation_service()
    session = conversation_service.get_session(user_id, session_id)
    if not session:
        reset_chat_state()
        return
    messages = conversation_service.list_messages(user_id, session_id)
    st.session_state.chat_session_id = session_id
    st.session_state.loaded_chat_session_id = session_id
    st.session_state.chat_session_kb = session.kb_name
    st.session_state.messages = [
        {"role": msg.role, "content": msg.content}
        for msg in messages
        if msg.role in {"user", "assistant"}
    ]


def ensure_current_chat_session():
    user_id = st.session_state.get("user_id")
    kb_name = st.session_state.get("current_kb")
    if not config.settings.AUTH_ENABLED or not user_id or not kb_name:
        return None

    conversation_service = init_conversation_service()
    requested_id = st.session_state.get("chat_session_id")
    if st.session_state.get("chat_session_kb") != kb_name:
        requested_id = None

    session = conversation_service.get_or_create_session(user_id, kb_name, requested_id)
    st.session_state.chat_session_id = session.id
    st.session_state.chat_session_kb = kb_name

    if st.session_state.get("loaded_chat_session_id") != session.id:
        load_chat_session(session.id)
    return session


def start_new_chat_session():
    user_id = st.session_state.get("user_id")
    kb_name = st.session_state.get("current_kb")
    if not user_id or not kb_name:
        reset_chat_state()
        return
    session = init_conversation_service().create_session(user_id, kb_name)
    st.session_state.chat_session_id = session.id
    st.session_state.chat_session_kb = kb_name
    st.session_state.loaded_chat_session_id = session.id
    st.session_state.messages = []


def persist_chat_message(role: str, content: str):
    user_id = st.session_state.get("user_id")
    if not config.settings.AUTH_ENABLED or not user_id:
        return None
    session = ensure_current_chat_session()
    if session:
        return init_conversation_service().add_message(user_id, session.id, role, content)
    return None


def clear_current_chat_session():
    user_id = st.session_state.get("user_id")
    session_id = st.session_state.get("chat_session_id")
    if config.settings.AUTH_ENABLED and user_id and session_id:
        init_conversation_service().clear_session(user_id, session_id)
    st.session_state.messages = []


def current_auth_user():
    return init_auth_service().get_user_by_username(st.session_state.get("username"))


def record_audit(action: str, **kwargs):
    try:
        init_log_service().record_audit(action=action, actor=current_auth_user(), **kwargs)
    except Exception as exc:
        # 日志失败不能阻断业务流程。
        print(f"audit log failed: {exc}")


# ==================== 逻辑处理回调函数 ===================
def create_kb_callback(pipeline):
    """创建知识库回调"""
    name = st.session_state.get("new_kb_name_input", "").strip()
    if not name:
        st.session_state.error_msg = "❌ 名称不能为空"
        return
    ctx = build_request_context(st.session_state)
    ok, msg = pipeline.create_kb(name, ctx=ctx)
    record_audit(
        "create_kb",
        target_type="knowledge_base",
        target_id=name,
        kb_name=name,
        success=ok,
        error_message="" if ok else msg,
    )
    if ok:
        st.session_state.kb_list = pipeline.list_knowledge_bases(ctx=ctx)
        st.session_state.current_kb = name
        st.session_state.kb_selector = name
        st.session_state.show_create_kb = False
        st.session_state.toast_msg = msg
    else:
        st.session_state.error_msg = msg


def delete_kb_confirmed(pipeline, kb_name):
    """执行已确认的知识库删除"""
    ok, msg = pipeline.delete_knowledge_base(kb_name)
    record_audit(
        "delete_kb",
        target_type="knowledge_base",
        target_id=kb_name,
        kb_name=kb_name,
        success=ok,
        error_message="" if ok else msg,
    )
    if ok:
        invalidate_file_cache(kb_name)
        if st.session_state.current_kb == kb_name:
            st.session_state.current_kb = config.settings.DEFAULT_KB_NAME
            st.session_state.kb_selector = config.settings.DEFAULT_KB_NAME
            reset_chat_state()
        ctx = build_request_context(st.session_state)
        st.session_state.kb_list = pipeline.list_knowledge_bases(ctx=ctx)
        st.session_state.toast_msg = msg
    else:
        st.session_state.error_msg = msg
    st.session_state.confirm_delete_kb = None


def switch_kb_callback(kb_name):
    """切换知识库回调"""
    st.session_state.current_kb = kb_name
    st.session_state.kb_selector = kb_name
    reset_chat_state()
    st.session_state.confirm_delete_file = None
    st.session_state.confirm_delete_kb = None


def refresh_kb_list(pipeline):
    ctx = build_request_context(st.session_state)
    st.session_state.kb_list = pipeline.list_knowledge_bases(ctx=ctx)


def get_cached_files(pipeline, kb_name: str) -> list[str]:
    ctx = build_request_context(st.session_state)
    cache_key = f"{ctx.user_id}:{kb_name}"
    if cache_key not in st.session_state.file_cache:
        st.session_state.file_cache[cache_key] = pipeline.list_files(kb_name, ctx=ctx)
    return st.session_state.file_cache[cache_key]


def invalidate_file_cache(kb_name: str):
    ctx = build_request_context(st.session_state)
    st.session_state.file_cache.pop(f"{ctx.user_id}:{kb_name}", None)


def render_parse_result_viewer(pipeline, kb_name: str, files: list[str], key_prefix: str):
    if not files:
        return
    selected_file = st.selectbox("查看解析结果", files, key=f"{key_prefix}_parse_result_file")
    if not selected_file:
        return
    ctx = build_request_context(st.session_state)
    result = pipeline.get_parse_result(kb_name, selected_file, ctx=ctx)
    if not result:
        st.caption("该文件尚未生成解析结果，或索引中没有找到对应分块。")
        return
    st.caption(f"共 {result.chunk_count} 个解析分块")
    for chunk in result.chunks:
        title = f"分块 {chunk.index + 1}"
        source_group = chunk.metadata.get("source_group")
        if source_group:
            title = f"{title} · {source_group}"
        with st.expander(title):
            st.write(chunk.content)


def render_parse_task_panel(pipeline, kb_name: str, key_prefix: str):
    ctx = build_request_context(st.session_state)
    tasks = pipeline.list_parse_tasks(kb_name, ctx=ctx)
    if any(task.status == "completed" for task in tasks):
        invalidate_file_cache(kb_name)

    with st.container(border=True):
        c_title, c_refresh, c_clear = st.columns([0.68, 0.16, 0.16])
        with c_title:
            st.markdown("##### 🧩 解析任务")
        with c_refresh:
            if st.button("刷新", key=f"{key_prefix}_refresh_tasks", use_container_width=True):
                st.rerun()
        with c_clear:
            if st.button("清理完成", key=f"{key_prefix}_clear_tasks", use_container_width=True):
                pipeline.clear_finished_parse_tasks(kb_name, ctx=ctx)
                st.rerun()

        if not tasks:
            st.caption("当前知识库暂无解析任务")
            return

        status_labels = {
            "queued": "排队中",
            "running": "解析中",
            "paused": "已暂停",
            "completed": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
        }
        for task in tasks:
            st.divider()
            c_info, c_actions = st.columns([0.72, 0.28])
            with c_info:
                status_label = status_labels.get(task.status, task.status)
                st.markdown(f"**{task.original_name}** · {status_label}")
                st.progress(task.progress, text=f"{task.progress}% · {task.stage}")
                if task.message:
                    st.caption(task.message)
            with c_actions:
                if task.status in {"queued", "running"}:
                    if st.button("暂停", key=f"{key_prefix}_pause_{task.id}", use_container_width=True):
                        st.session_state.toast_msg = pipeline.pause_parse_task(task.id, ctx=ctx)
                        st.rerun()
                elif task.status == "paused":
                    if st.button("启动", key=f"{key_prefix}_resume_{task.id}", use_container_width=True):
                        st.session_state.toast_msg = pipeline.resume_parse_task(task.id, ctx=ctx)
                        st.rerun()
                else:
                    st.button("暂停", key=f"{key_prefix}_noop_{task.id}", disabled=True, use_container_width=True)
                if st.button("删除任务", key=f"{key_prefix}_delete_{task.id}", use_container_width=True):
                    st.session_state.toast_msg = pipeline.delete_parse_task(task.id, ctx=ctx)
                    st.rerun()


def render_login_page():
    """渲染登录页。"""
    auth_service = init_auth_service()
    render_auth_restore_script()
    st.markdown("""
        <style>
            .login-panel {
                width: min(560px, 100%);
                text-align: center;
                margin: 175px auto 18px auto;
            }
            .login-logo {
                font-size: 70px;
                line-height: 1;
                margin-bottom: 10px;
            }
            .login-title {
                font-size: 36px;
                font-weight: 800;
                color: #0f172a;
                margin: 0 0 6px 0;
            }
            .login-subtitle {
                color: #64748b;
                font-size: 16px;
                margin-bottom: 8px;
            }
            .login-tagline {
                color: #334155;
                font-size: 14px;
                margin-bottom: 24px;
            }
            div[data-testid="stForm"] {
                border: 1px solid #dbe3ef;
                border-radius: 8px;
                padding: 82px 30px 78px 30px;
                background: #ffffff;
                box-shadow: 0 18px 50px rgba(15, 23, 42, 0.10);
            }
            .login-footnote {
                color: #64748b;
                font-size: 13px;
                line-height: 1.6;
                margin-top: 14px;
                text-align: center;
            }
        </style>
    """, unsafe_allow_html=True)

    st.markdown("""
        <div class="login-panel">
            <div class="login-logo">😺</div>
            <div class="login-title">Hardware DataBase</div>
            <div class="login-subtitle">硬件智能数据基座</div>
            <div class="login-tagline">统一检索设计资料、项目文档与知识库内容</div>
        </div>
    """, unsafe_allow_html=True)
    _, center, _ = st.columns([0.95, 1.1, 0.95])
    with center:
        with st.form("login_form"):
            username = st.text_input("用户名", placeholder="admin")
            password = st.text_input("密码", type="password", placeholder="请输入密码")
            submitted = st.form_submit_button("登录", width="stretch", type="primary")
        if submitted:
            session = auth_service.authenticate(username.strip(), password)
            if session:
                init_log_service().record_audit(
                    action="login_success",
                    actor=session.user,
                    target_type="user",
                    target_id=session.user.username,
                    success=True,
                )
                st.session_state.authenticated = True
                st.session_state.username = session.user.username
                st.session_state.role = session.user.role
                st.session_state.department_id = session.user.department_id
                st.session_state.user_id = session.user.id
                persist_auth_token(session.token)
                render_auth_store_script(session.token)
                reset_chat_state()
                st.session_state.toast_msg = f"✅ 已登录: {session.user.username}"
                time.sleep(0.2)
                st.rerun()
            else:
                init_log_service().record_audit(
                    action="login_failed",
                    target_type="user",
                    target_id=username.strip(),
                    success=False,
                    error_message="用户名或密码错误",
                    metadata={"attempted_username": username.strip()},
                )
                st.error("用户名或密码错误")
        st.markdown("""
            <div class="login-footnote">
                使用管理员分配的账号登录
            </div>
        """, unsafe_allow_html=True)


def logout():
    record_audit("logout", target_type="user", target_id=st.session_state.get("username") or "")
    init_auth_service().revoke_session(st.session_state.get("auth_token"))
    render_auth_clear_script()
    st.session_state.authenticated = False
    st.session_state.username = None
    st.session_state.role = None
    st.session_state.department_id = None
    st.session_state.user_id = None
    clear_persisted_auth_token()
    reset_chat_state()
    st.session_state.file_cache = {}
    st.session_state.toast_msg = "已退出登录"
    st.rerun()


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

    with st.expander("🔐 登录与会话"):
        st.checkbox(
            "启用登录",
            value=bool(_val("AUTH_ENABLED", False)),
            key="cfg_auth_enabled",
            help="开启后进入应用必须先登录。"
        )
        st.text_input(
            "认证数据库路径",
            value=str(_val("AUTH_DB_PATH", "storage/auth.db")),
            key="cfg_auth_db_path",
            help="SQLite 用户与会话数据库路径。"
        )
        st.text_input(
            "默认管理员用户名",
            value=str(_val("AUTH_DEFAULT_ADMIN_USERNAME", "admin")),
            key="cfg_auth_admin_username",
            help="仅在认证数据库中不存在该用户时自动创建。"
        )
        st.text_input(
            "默认管理员密码",
            value=str(_val("AUTH_DEFAULT_ADMIN_PASSWORD", "admin123")),
            key="cfg_auth_admin_password",
            type="password",
            help="仅首次创建默认管理员时使用。创建后请通过数据库管理或后续用户管理功能修改。"
        )
        st.number_input(
            "会话有效期（小时）",
            min_value=1,
            max_value=24 * 30,
            value=int(_val("AUTH_SESSION_TTL_HOURS", "24")),
            key="cfg_auth_session_ttl",
        )
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
        rag_backend_options = ["local", "ragflow"]
        current_backend = str(_val("RAG_BACKEND", "local")).lower()
        if current_backend not in rag_backend_options:
            current_backend = "local"
        st.selectbox(
            "RAG 后端",
            options=rag_backend_options,
            index=rag_backend_options.index(current_backend),
            key="cfg_rag_backend",
            help="local = 当前内置检索；ragflow = 预留接口，后续接入 RAGFlow API"
        )
        st.divider()
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
    st.caption("修改后点击应用配置生效。")
    _, col_apply, _ = st.columns([2, 1, 2])
    with col_apply:
        if st.button("应用配置", type="primary", width="stretch"):
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
    new_settings["RAG_BACKEND"] = st.session_state.get("cfg_rag_backend", "local")
    new_settings["AUTH_ENABLED"] = "true" if st.session_state.get("cfg_auth_enabled", False) else "false"
    new_settings["AUTH_DB_PATH"] = st.session_state.get("cfg_auth_db_path", "")
    new_settings["AUTH_DEFAULT_ADMIN_USERNAME"] = st.session_state.get("cfg_auth_admin_username", "")
    new_settings["AUTH_DEFAULT_ADMIN_PASSWORD"] = st.session_state.get("cfg_auth_admin_password", "")
    new_settings["AUTH_SESSION_TTL_HOURS"] = str(st.session_state.get("cfg_auth_session_ttl", 24))

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

        # 7. 清空当前 UI 对话状态（模型已变，旧上下文无效）
        reset_chat_state()

        record_audit(
            "change_settings",
            target_type="system_settings",
            target_id="env",
            metadata={
                "provider": new_settings.get("PROVIDER"),
                "rag_backend": new_settings.get("RAG_BACKEND"),
                "auth_enabled": new_settings.get("AUTH_ENABLED"),
                "reranker_type": new_settings.get("RERANKER_TYPE"),
            },
        )
        st.session_state.toast_msg = "✅ 配置已更新并生效"
        st.rerun()

    except Exception as e:
        st.error(f"❌ 应用配置失败: {e}")
        st.warning("配置已保存到 .env，但模型初始化失败。请检查配置后点击「应用配置」重试。")


def render_log_center_tab():
    st.markdown('<div style="height: 30px;"></div>', unsafe_allow_html=True)
    st.subheader("📊 日志中心")

    viewer = current_auth_user()
    if viewer is None or viewer.role not in {ROLE_SYSTEM_ADMIN, ROLE_DEPT_ADMIN}:
        st.error("当前账号无权访问日志中心")
        st.stop()

    if viewer.role == ROLE_SYSTEM_ADMIN:
        st.caption("系统管理员视图：可查看全局审计日志和查询日志。")
    else:
        st.caption("部门管理员视图：仅显示本部门相关日志。")

    log_service = init_log_service()
    tab_audit, tab_query = st.tabs(["审计日志", "查询日志"])

    with tab_audit:
        c1, c2, c3, c4 = st.columns([1.1, 1.1, 1.1, 0.8])
        with c1:
            action_options = [
                "全部",
                "login_success",
                "login_failed",
                "logout",
                "create_user",
                "set_user_active",
                "create_department",
                "delete_department",
                "create_kb",
                "delete_kb",
                "upload_document",
                "delete_document",
                "grant_kb_permission",
                "change_settings",
            ]
            action = st.selectbox("操作类型", action_options, key="audit_action_filter")
        with c2:
            audit_kb = st.selectbox("知识库", ["全部"] + st.session_state.get("kb_list", []), key="audit_kb_filter")
        with c3:
            success_label = st.selectbox("结果", ["全部", "成功", "失败"], key="audit_success_filter")
        with c4:
            audit_limit = st.number_input("条数", min_value=20, max_value=1000, value=200, step=20, key="audit_limit")
        audit_keyword = st.text_input("关键词", placeholder="用户名、对象、错误信息", key="audit_keyword")

        success_filter = None
        if success_label == "成功":
            success_filter = True
        elif success_label == "失败":
            success_filter = False

        audit_events = log_service.list_audit_events(
            viewer=viewer,
            action=None if action == "全部" else action,
            kb_name=None if audit_kb == "全部" else audit_kb,
            success=success_filter,
            keyword=audit_keyword.strip() or None,
            limit=int(audit_limit),
        )
        if not audit_events:
            st.info("暂无审计日志")
        else:
            st.dataframe(
                [
                    {
                        "时间": event.created_at,
                        "用户": event.actor_username,
                        "角色": event.actor_role,
                        "部门ID": event.department_id,
                        "操作": event.action,
                        "对象类型": event.target_type,
                        "对象": event.target_id,
                        "知识库": event.kb_name,
                        "结果": "成功" if event.success else "失败",
                        "错误": event.error_message,
                    }
                    for event in audit_events
                ],
                width="stretch",
                hide_index=True,
            )

    with tab_query:
        c1, c2, c3 = st.columns([1.2, 1.1, 0.8])
        with c1:
            query_kb = st.selectbox("知识库", ["全部"] + st.session_state.get("kb_list", []), key="query_kb_filter")
        with c2:
            status = st.selectbox("状态", ["全部", "success", "failed"], key="query_status_filter")
        with c3:
            query_limit = st.number_input("条数", min_value=20, max_value=1000, value=200, step=20, key="query_limit")
        query_keyword = st.text_input("关键词", placeholder="用户名、问题、错误信息", key="query_keyword")

        traces = log_service.list_query_traces(
            viewer=viewer,
            kb_name=None if query_kb == "全部" else query_kb,
            status=None if status == "全部" else status,
            keyword=query_keyword.strip() or None,
            limit=int(query_limit),
        )
        if not traces:
            st.info("暂无查询日志")
        else:
            st.dataframe(
                [
                    {
                        "时间": trace.created_at,
                        "用户": trace.username,
                        "部门ID": trace.department_id,
                        "知识库": trace.kb_name,
                        "问题": trace.original_query[:120],
                        "后端": trace.backend,
                        "检索": trace.retriever_type,
                        "耗时ms": trace.latency_ms,
                        "状态": trace.status,
                        "错误": trace.error_message,
                    }
                    for trace in traces
                ],
                width="stretch",
                hide_index=True,
            )

            selected_trace_id = st.selectbox(
                "查看查询详情",
                [trace.id for trace in traces],
                format_func=lambda trace_id: next(
                    (
                        f"#{trace.id} {trace.created_at} {trace.username}: {trace.original_query[:40]}"
                        for trace in traces
                        if trace.id == trace_id
                    ),
                    str(trace_id),
                ),
                key="trace_detail_select",
            )
            selected_trace = next((trace for trace in traces if trace.id == selected_trace_id), None)
            if selected_trace:
                with st.expander("查询详情", expanded=True):
                    st.markdown(f"**原始问题:** {selected_trace.original_query}")
                    st.markdown(f"**知识库:** `{selected_trace.kb_name}`")
                    st.markdown(
                        f"**参数:** vector_top_k={selected_trace.vector_top_k}, "
                        f"bm25_top_k={selected_trace.bm25_top_k}, final_top_k={selected_trace.final_top_k}"
                    )
                    st.markdown(f"**耗时:** {selected_trace.latency_ms} ms")
                    if selected_trace.error_message:
                        st.error(selected_trace.error_message)

                evidence = log_service.list_evidence(viewer, selected_trace.id)
                if evidence:
                    st.markdown("##### 检索证据")
                    st.dataframe(
                        [
                            {
                                "排名": item.rank,
                                "文件": item.file_name,
                                "chunk": item.chunk_id,
                                "预览": item.text_preview,
                                "rerank": item.rerank_score,
                                "rrf": item.rrf_score,
                            }
                            for item in evidence
                        ],
                        width="stretch",
                        hide_index=True,
                    )
                else:
                    st.caption("当前版本已预留检索证据表；完整证据落库需要继续改造检索链路。")


# ==================== 主界面 ====================
def main():
    init_session_state()
    refresh_auth_state()

    if config.settings.AUTH_ENABLED and not st.session_state.authenticated:
        render_login_page()
        return

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
    elif pipeline:
        ctx = build_request_context(st.session_state)
        st.session_state.kb_list = pipeline.list_knowledge_bases(ctx=ctx)
        if not st.session_state.kb_list and st.session_state.role == ROLE_SYSTEM_ADMIN:
            st.session_state.kb_list = [config.settings.DEFAULT_KB_NAME]
        if st.session_state.kb_list and st.session_state.current_kb not in st.session_state.kb_list:
            st.session_state.current_kb = st.session_state.kb_list[0]

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
            st.markdown('<h1 style="font-size: 35px; margin-top: 10px; margin-bottom: 0px;">😺 Hardware DataBase</h1>', unsafe_allow_html=True)
        with col_status:
            status = resource_manager.get_status()
            st.markdown(f"""
                <div style="text-align:right; padding-top:22px;">
                    <span class="status-indicator {'status-ok' if status.get('models_initialized') else 'status-error'}"></span> AI模型<br>
                    <span class="status-indicator {'status-ok' if status.get('chroma_connected') else 'status-error'}"></span> 向量库</div>
            """, unsafe_allow_html=True)

    # ------------------ 侧边栏 ------------------
    with st.sidebar:
        st.markdown('<h2 class="sidebar-main-title">😼 Hardware DataBase导航</h2>', unsafe_allow_html=True)
        user_label = st.session_state.username or "anonymous"
        role_label = st.session_state.role or "anonymous"
        st.caption(f"当前用户: {user_label} / {role_label}")
        if config.settings.AUTH_ENABLED and st.button("退出登录", width="stretch"):
            logout()
        st.divider()

        role = st.session_state.get("role")
        tab_options = ["💬 智能对话"]
        if role in {ROLE_SYSTEM_ADMIN, ROLE_DEPT_ADMIN}:
            tab_options.append("👥 部门管理")
            tab_options.append("📚 知识库管理")
            tab_options.append("📊 日志中心")
        if role == ROLE_SYSTEM_ADMIN:
            tab_options.append("⚙️ 系统配置")

        selected_tab = st.radio("**🚩 功能切换:**", tab_options, label_visibility="collapsed")
        st.divider()

        # 设置页面：侧边栏显示当前配置概览
        if selected_tab == "⚙️ 系统配置":
            if role != ROLE_SYSTEM_ADMIN:
                st.error("当前账号无权访问系统配置")
                st.stop()
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
        elif selected_tab in {"💬 智能对话", "📚 知识库管理"}:
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
                reset_chat_state()
                st.session_state.confirm_delete_file = None
                st.rerun()

            kb_files = get_cached_files(pipeline, st.session_state.current_kb)
            st.info(f"当前库包含 {len(kb_files)} 个文件")
            if kb_files:
                with st.expander("📚 查看库内文档"):
                    for f in kb_files:
                        st.markdown(f"- 📄 {f}")
                    st.divider()
                    render_parse_result_viewer(
                        pipeline,
                        st.session_state.current_kb,
                        kb_files,
                        key_prefix="sidebar",
                    )

            if selected_tab == "💬 智能对话":
                ensure_current_chat_session()
                if st.button("➕ 新建对话", width="stretch", type="secondary"):
                    start_new_chat_session()
                    st.rerun()

                if config.settings.AUTH_ENABLED and st.session_state.get("user_id"):
                    sessions = init_conversation_service().list_sessions(
                        st.session_state.user_id,
                        st.session_state.current_kb,
                    )
                    if sessions:
                        session_ids = [session.id for session in sessions]
                        current_id = st.session_state.get("chat_session_id")
                        current_index = session_ids.index(current_id) if current_id in session_ids else 0
                        selected_session_id = st.selectbox(
                            "我的会话",
                            options=session_ids,
                            index=current_index,
                            format_func=lambda sid: next(
                                (session.title for session in sessions if session.id == sid),
                                f"会话 {sid}",
                            ),
                            key="chat_session_selector",
                        )
                        if selected_session_id != st.session_state.get("chat_session_id"):
                            load_chat_session(selected_session_id)
                            st.rerun()

                if st.button("🗑️ 清空当前对话", width="stretch", type="secondary"):
                    clear_current_chat_session()
                    st.rerun()
        elif selected_tab in {"👥 部门管理", "📊 日志中心"}:
            if not pipeline:
                st.warning("⚠️ 系统未初始化，请先在 ⚙️ 系统配置 中检查并修复配置")
                st.stop()

        st.divider()
        st.markdown("<h3>🐱‍👓️ 说明与注意事项</h3>", unsafe_allow_html=True)

        if selected_tab == "💬 智能对话":
            st.warning("""
            **1. 对话说明:**
            - 回答基于当前知识库中的文档内容。
            - 可点击「📚 参考来源」查看引用的原始文档。

            **2. 上下文记忆:**
            - 保留最近 5 轮对话历史作为上下文。
            - 切换知识库会加载该知识库下当前用户自己的会话。

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
        elif selected_tab == "👥 部门管理":
            st.warning("""
            **部门管理:**
            - 系统管理员可维护全部部门、管理员和用户。
            - 部门管理员可维护本部门普通用户。
            - 部门管理员不能创建管理员账号。
            """)
        elif selected_tab == "📊 日志中心":
            st.warning("""
            **日志范围:**
            - 系统管理员可查看全局日志。
            - 部门管理员仅可查看本部门日志。
            - 日志用于审计、排错和检索质量追踪。
            """)
        elif selected_tab == "⚙️ 系统配置":
            st.warning("""
            **1. 配置生效:**
            - 修改配置后点击「🔄 应用配置」立即生效。
            - 配置会持久化保存到 .env 文件。

            **2. 模型切换:**
            - 切换 Provider 或模型会**清空当前对话**。
            - API Key 使用密码输入，安全存储。
            """)
        st.divider()
        st.caption("© 2025 Hardware DataBase Assistant")

    # ------------------ 页面内容分发 ------------------
    if selected_tab == "💬 智能对话":
        render_chat_tab(pipeline)
    elif selected_tab == "📚 知识库管理":
        if st.session_state.get("role") not in {ROLE_SYSTEM_ADMIN, ROLE_DEPT_ADMIN}:
            st.error("当前账号无权访问知识库管理")
            st.stop()
        render_kb_management_tab(pipeline)
    elif selected_tab == "👥 部门管理":
        if st.session_state.get("role") not in {ROLE_SYSTEM_ADMIN, ROLE_DEPT_ADMIN}:
            st.error("当前账号无权访问部门管理")
            st.stop()
        render_department_management_tab()
    elif selected_tab == "📊 日志中心":
        if st.session_state.get("role") not in {ROLE_SYSTEM_ADMIN, ROLE_DEPT_ADMIN}:
            st.error("当前账号无权访问日志中心")
            st.stop()
        render_log_center_tab()
    elif selected_tab == "⚙️ 系统配置":
        if st.session_state.get("role") != ROLE_SYSTEM_ADMIN:
            st.error("当前账号无权访问系统配置")
            st.stop()
        render_settings_tab()


# ==================== Tab 1: 对话界面 ====================
def render_chat_tab(pipeline):
    st.markdown('<div style="height: 30px;"></div>', unsafe_allow_html=True)
    ensure_current_chat_session()

    # 1. 渲染历史消息
    if not st.session_state.messages:
        st.markdown("""
            <div style='text-align:center; color:#888; padding-top:180px;'>
                <h3 style="margin-top:100px;">🙌 硬件数据检索助手</h3>
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
        query_started_at = time.perf_counter()

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
                ctx = build_request_context(st.session_state)
                gen = pipeline.query(user_input_to_process, st.session_state.current_kb, chat_history[-5:], ctx=ctx)
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
        assistant_message = persist_chat_message("assistant", full_response)
        latency_ms = int((time.perf_counter() - query_started_at) * 1000)
        init_log_service().record_query_trace(
            user=current_auth_user(),
            kb_name=st.session_state.current_kb,
            original_query=user_input_to_process,
            chat_session_id=st.session_state.get("chat_session_id"),
            user_message_id=st.session_state.get("pending_user_message_id"),
            assistant_message_id=assistant_message.id if assistant_message else None,
            backend=config.settings.RAG_BACKEND,
            latency_ms=latency_ms,
            status="failed" if full_response.startswith("Error:") else "success",
            error_message=full_response if full_response.startswith("Error:") else "",
        )
        st.session_state.pending_user_message_id = None
        st.rerun()

    # --- 原生聊天输入框 ---
    if prompt := st.chat_input("请输入问题..."):
        ensure_current_chat_session()
        st.session_state.messages.append({"role": "user", "content": prompt})
        user_message = persist_chat_message("user", prompt)
        st.session_state.pending_user_message_id = user_message.id if user_message else None
        st.rerun()


def render_department_management_tab():
    st.markdown('<div style="height: 30px;"></div>', unsafe_allow_html=True)
    st.subheader("👥 部门管理")

    auth_service = init_auth_service()
    current_user = current_auth_user()
    if current_user is None:
        st.error("无法获取当前用户")
        return

    if current_user.role == ROLE_DEPT_ADMIN:
        users = [
            user for user in auth_service.list_users_for_manager(current_user)
            if user.role == ROLE_USER
        ]
        st.caption(f"当前部门: {current_user.department_name or '-'}")
        st.markdown("##### 本部门用户")
        if not users:
            st.info("本部门暂无普通用户")
        else:
            for user in users:
                c1, c2, c3 = st.columns([2, 1, 1])
                with c1:
                    st.markdown(f"`{user.username}`")
                with c2:
                    st.caption("启用" if user.is_active else "停用")
                with c3:
                    next_active = not user.is_active
                    button_label = "启用" if next_active else "停用"
                    if st.button(button_label, key=f"dept_user_toggle_{user.id}", width="stretch"):
                        try:
                            auth_service.set_user_active(user.id, next_active)
                            record_audit(
                                "set_user_active",
                                target_type="user",
                                target_id=user.username,
                                metadata={"is_active": next_active, "scope": "department"},
                            )
                            st.success(f"已{button_label}: {user.username}")
                            st.rerun()
                        except Exception as e:
                            record_audit(
                                "set_user_active",
                                target_type="user",
                                target_id=user.username,
                                success=False,
                                error_message=str(e),
                                metadata={"is_active": next_active, "scope": "department"},
                            )
                            st.error(f"操作失败: {e}")

        st.divider()
        with st.form("dept_create_user_form"):
            st.markdown("##### 创建普通用户")
            new_username = st.text_input("用户名", key="dept_auth_new_username")
            new_password = st.text_input("密码", type="password", key="dept_auth_new_password")
            if st.form_submit_button("创建用户", width="stretch"):
                try:
                    auth_service.create_user(
                        new_username,
                        new_password,
                        ROLE_USER,
                        department_id=current_user.department_id,
                    )
                    record_audit(
                        "create_user",
                        target_type="user",
                        target_id=new_username,
                        metadata={"role": ROLE_USER, "scope": "department"},
                    )
                    st.success(f"已创建普通用户: {new_username}")
                    st.rerun()
                except Exception as e:
                    record_audit(
                        "create_user",
                        target_type="user",
                        target_id=new_username,
                        success=False,
                        error_message=str(e),
                        metadata={"role": ROLE_USER, "scope": "department"},
                    )
                    st.error(f"创建用户失败: {e}")
        return

    if current_user.role == ROLE_SYSTEM_ADMIN:
        render_system_department_management()


def render_system_department_management():
    auth_service = init_auth_service()
    users = auth_service.list_users()
    departments = auth_service.list_departments()

    user_tab, dept_tab, create_tab = st.tabs(["用户列表", "部门列表", "创建"])
    with user_tab:
        st.caption(f"当前用户数: {len(users)}")
        grouped_users = {}
        for user in users:
            dept_name = user.department_name or "未分配"
            grouped_users.setdefault(dept_name, []).append(user)

        for dept_name, dept_users in grouped_users.items():
            active_count = sum(1 for user in dept_users if user.is_active)
            with st.expander(f"{dept_name} · {len(dept_users)} 人 · 启用 {active_count}", expanded=False):
                for user in dept_users:
                    c1, c2, c3, c4 = st.columns([1.6, 1.2, 0.8, 0.9])
                    with c1:
                        st.markdown(f"`{user.username}`")
                    with c2:
                        st.caption(user.role)
                    with c3:
                        st.caption("启用" if user.is_active else "停用")
                    with c4:
                        if user.id == st.session_state.get("user_id"):
                            st.button("当前账号", key=f"user_self_{user.id}", disabled=True, width="stretch")
                        else:
                            next_active = not user.is_active
                            button_label = "启用" if next_active else "停用"
                            if st.button(button_label, key=f"user_toggle_{user.id}", width="stretch"):
                                try:
                                    auth_service.set_user_active(user.id, next_active)
                                    record_audit(
                                        "set_user_active",
                                        target_type="user",
                                        target_id=user.username,
                                        metadata={"is_active": next_active},
                                    )
                                    st.success(f"已{button_label}: {user.username}")
                                    st.rerun()
                                except Exception as e:
                                    record_audit(
                                        "set_user_active",
                                        target_type="user",
                                        target_id=user.username,
                                        success=False,
                                        error_message=str(e),
                                        metadata={"is_active": next_active},
                                    )
                                    st.error(f"操作失败: {e}")

    with dept_tab:
        st.caption(f"当前部门数: {len(departments)}")
        for dept in departments:
            c1, c2, c3 = st.columns([0.8, 2.4, 0.9])
            with c1:
                st.caption(str(dept.id))
            with c2:
                st.markdown(f"`{dept.name}`")
            with c3:
                if dept.name == "system":
                    st.button("受保护", key=f"dept_system_{dept.id}", disabled=True, width="stretch")
                else:
                    if st.button("删除", key=f"dept_delete_{dept.id}", width="stretch"):
                        try:
                            auth_service.delete_department(dept.id)
                            record_audit(
                                "delete_department",
                                target_type="department",
                                target_id=dept.name,
                            )
                            st.success(f"已删除部门: {dept.name}")
                            st.rerun()
                        except Exception as e:
                            record_audit(
                                "delete_department",
                                target_type="department",
                                target_id=dept.name,
                                success=False,
                                error_message=str(e),
                            )
                            st.error(f"删除部门失败: {e}")

    with create_tab:
        col_dept, col_user = st.columns(2)
        with col_dept:
            with st.form("create_department_form"):
                st.markdown("###### 创建部门")
                new_department_name = st.text_input("新部门名称", key="auth_new_department_name")
                if st.form_submit_button("创建部门", width="stretch"):
                    try:
                        auth_service.create_department(new_department_name)
                        record_audit(
                            "create_department",
                            target_type="department",
                            target_id=new_department_name,
                        )
                        st.success(f"已创建部门: {new_department_name}")
                        st.rerun()
                    except Exception as e:
                        record_audit(
                            "create_department",
                            target_type="department",
                            target_id=new_department_name,
                            success=False,
                            error_message=str(e),
                        )
                        st.error(f"创建部门失败: {e}")

        with col_user:
            with st.form("create_user_form"):
                st.markdown("###### 创建用户")
                new_username = st.text_input("新用户名", key="auth_new_username")
                new_password = st.text_input("新用户密码", type="password", key="auth_new_password")
                new_role = st.selectbox("角色", [ROLE_USER, ROLE_DEPT_ADMIN, ROLE_SYSTEM_ADMIN], key="auth_new_role")
                department_names = [dept.name for dept in departments]
                selected_department = st.selectbox("部门", department_names, key="auth_new_department")
                if st.form_submit_button("创建用户", width="stretch"):
                    try:
                        department_id = next((dept.id for dept in departments if dept.name == selected_department), None)
                        auth_service.create_user(new_username, new_password, new_role, department_id=department_id)
                        record_audit(
                            "create_user",
                            target_type="user",
                            target_id=new_username,
                            metadata={"role": new_role, "department": selected_department},
                        )
                        st.success(f"已创建用户: {new_username}")
                        st.rerun()
                    except Exception as e:
                        record_audit(
                            "create_user",
                            target_type="user",
                            target_id=new_username,
                            success=False,
                            error_message=str(e),
                            metadata={"role": new_role, "department": selected_department},
                        )
                        st.error(f"创建用户失败: {e}")


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
                st.write("创建后台解析任务...")
                ctx = build_request_context(st.session_state)
                res = pipeline.upload_files(temp_paths, st.session_state.current_kb, ctx=ctx)
                upload_ok = res.startswith("✅")
                record_audit(
                    "upload_document",
                    target_type="document",
                    target_id=", ".join(f.name for f in files),
                    kb_name=st.session_state.current_kb,
                    success=upload_ok,
                    error_message="" if upload_ok else res,
                    metadata={"file_count": len(files), "result": res.split("\n")[0]},
                )
                for p in temp_paths:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                status.update(label="✅ 已提交解析任务", state="complete", expanded=False)
            st.success(res.split('\n')[0])
            time.sleep(1)
            st.rerun()
    st.divider()

    render_parse_task_panel(pipeline, st.session_state.current_kb, key_prefix="kb_mgmt")
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

    if st.session_state.get("role") in {ROLE_SYSTEM_ADMIN, ROLE_DEPT_ADMIN} and st.session_state.kb_list:
        with st.expander("🔑 知识库访问授权"):
            auth_service = init_auth_service()
            manager = auth_service.get_user_by_username(st.session_state.get("username"))
            users = auth_service.list_users_for_manager(manager) if manager else []
            if st.session_state.get("role") == ROLE_DEPT_ADMIN:
                users = [user for user in users if user.role == ROLE_USER]
            if not users:
                st.info("暂无可授权用户")
            else:
                with st.form("grant_kb_permission_form"):
                    grant_kb = st.selectbox("知识库", st.session_state.kb_list, key="grant_kb_name")
                    user_labels = [f"{user.username} ({user.role})" for user in users]
                    selected_user_label = st.selectbox("用户", user_labels, key="grant_user_label")
                    permission = st.selectbox("权限", ["read", "write", "admin"], key="grant_permission")
                    if st.form_submit_button("授权"):
                        selected_index = user_labels.index(selected_user_label)
                        target_user = users[selected_index]
                        try:
                            auth_service.grant_kb_permission(grant_kb, target_user.id, permission)
                            record_audit(
                                "grant_kb_permission",
                                target_type="kb_permission",
                                target_id=target_user.username,
                                kb_name=grant_kb,
                                metadata={"permission": permission, "target_user_id": target_user.id},
                            )
                            st.success(f"已授权 {target_user.username} 访问 {grant_kb}: {permission}")
                            st.rerun()
                        except Exception as e:
                            record_audit(
                                "grant_kb_permission",
                                target_type="kb_permission",
                                target_id=target_user.username,
                                kb_name=grant_kb,
                                success=False,
                                error_message=str(e),
                                metadata={"permission": permission, "target_user_id": target_user.id},
                            )
                            st.error(f"授权失败: {e}")

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
                                            ctx = build_request_context(st.session_state)
                                            res = pipeline.delete_document(f, kb, ctx=ctx)
                                            delete_ok = "✅" in res
                                            record_audit(
                                                "delete_document",
                                                target_type="document",
                                                target_id=f,
                                                kb_name=kb,
                                                success=delete_ok,
                                                error_message="" if delete_ok else res,
                                            )
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
                st.divider()
                render_parse_result_viewer(pipeline, kb, files, key_prefix=f"mgmt_{kb}")
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
