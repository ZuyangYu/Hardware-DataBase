# src/streamlit_app.py
import html
import json
import streamlit as st
import streamlit.components.v1 as components
import time
from src.core.app_pipeline import AppPipeline
from src.core.auth import AuthService, ROLE_DEPT_ADMIN, ROLE_SYSTEM_ADMIN, ROLE_USER, build_request_context, ensure_session_id
from src.core.app_logs import AppLogService, format_local_time, query_trace_status
from src.core.conversation import ConversationService
from src.ingestion.kb_paths import InvalidKnowledgeBaseName, validate_kb_name
from src.ingestion.source_groups import SOURCE_GROUP_DESCRIPTIONS, USER_SELECTABLE_SOURCE_GROUPS, display_source_group
from src.pipelines.document_rag.schemas import (
    TASK_STATUS_CANCELLED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PAUSED,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    parse_status_view,
)
import config.settings
from src.ui.evaluation_page import render_evaluation_page
from src.ui.document_generation_page import render_document_generation_page

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
    /* 消除顶部默认内边距，避免滚动时出现额外空白。 */
    .block-container {
        padding-top: 0rem !important;
        padding-bottom: 5rem !important; /* 底部留白给输入框 */
    }
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}

    /* ========== 2. 侧边栏样式 ========== */
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

    /* --- 增大选项字体并对齐圆点 --- */
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


    /* ========== 4. 聊天界面样式 ========== */
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
    """初始化应用编排 Pipeline。"""
    try:
        pipeline = AppPipeline()
        return pipeline, None
    except Exception as e:
        return None, str(e)


@st.cache_resource
def init_auth_service():
    return AuthService()


@st.cache_resource
def init_conversation_service():
    return ConversationService()


@st.cache_resource
@st.cache_resource
def init_log_service():
    return AppLogService()


def init_session_state():
    """Initialize session state."""
    ensure_session_id(st.session_state)
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
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
        st.session_state.current_kb = None
    if "current_kb_id" not in st.session_state:
        st.session_state.current_kb_id = None
    if "current_kb_department_id" not in st.session_state:
        st.session_state.current_kb_department_id = None
    if "current_kb_identity" not in st.session_state:
        st.session_state.current_kb_identity = None
    if "kb_identity_map" not in st.session_state:
        st.session_state.kb_identity_map = {}
    if "kb_identity_by_name" not in st.session_state:
        st.session_state.kb_identity_by_name = {}
    if "kb_list" not in st.session_state:
        st.session_state.kb_list = []
    if "show_create_kb" not in st.session_state:
        st.session_state.show_create_kb = False
    if "create_kb_error" not in st.session_state:
        st.session_state.create_kb_error = None
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
    time.sleep(0.2)


def clear_persisted_auth_token():
    st.session_state.auth_token = None
    if AUTH_QUERY_PARAM in st.query_params:
        del st.query_params[AUTH_QUERY_PARAM]


def render_auth_restore_script():
    return


def render_auth_store_script(token: str):
    return


def render_auth_clear_script():
    components.html(f"""
        <script>
        window.parent.localStorage.removeItem("{AUTH_QUERY_PARAM}");
        </script>
    """, height=0)


def refresh_auth_state():
    token = st.session_state.get("auth_token") or read_auth_token_from_url()
    if not token:
        st.session_state.authenticated = False
        return
    st.session_state.auth_token = token
    if AUTH_QUERY_PARAM in st.query_params:
        del st.query_params[AUTH_QUERY_PARAM]

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
    # Drop the session-selector widget state so it does not carry a stale
    # session id (possibly from another KB) into the next render.
    st.session_state.pop("chat_session_selector", None)


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
    if not user_id or not kb_name:
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
    # Clear the session-selector widget state so the selectbox picks up the new
    # session id via its index on rerun. Without this, Streamlit reuses the
    # widget's previous value (an older session), which triggers load_chat_session
    # on that older session and makes the new chat appear to "jump away".
    st.session_state.pop("chat_session_selector", None)


def persist_chat_message(role: str, content: str):
    user_id = st.session_state.get("user_id")
    if not user_id:
        return None
    session = ensure_current_chat_session()
    if session:
        return init_conversation_service().add_message(user_id, session.id, role, content)
    return None


TOKEN_USAGE_HEADING = "**Token 使用量**"


def _is_agent_observation_footer(text: str) -> bool:
    return text.startswith(("**概览**", "**执行时间线**", "**路由说明**")) or "Agent 观测" in text


def _is_token_usage_footer(text: str) -> bool:
    return text.startswith(TOKEN_USAGE_HEADING)


def split_assistant_diagnostics(content: str) -> tuple[str, str, str]:
    text = str(content or "")
    marker = "\n---\n"
    if marker not in text:
        return text, "", ""
    parts = text.split(marker)
    answer_parts = [parts[0].strip()]
    observation_parts = []
    token_parts = []
    for raw_part in parts[1:]:
        part = raw_part.strip()
        if not part:
            continue
        if _is_token_usage_footer(part):
            token_parts.append(part)
        elif _is_agent_observation_footer(part):
            observation_parts.append(part)
        else:
            answer_parts.append(part)
    return (
        marker.join(part for part in answer_parts if part).strip(),
        marker.join(observation_parts).strip(),
        marker.join(token_parts).strip(),
    )


def strip_agent_observation(content: str) -> str:
    answer, _, _ = split_assistant_diagnostics(content)
    return answer


def split_agent_observation(content: str) -> tuple[str, str]:
    answer, observation, _ = split_assistant_diagnostics(content)
    return answer, observation


def format_token_usage_summary(summary) -> str:
    if not summary or _usage_attr(summary, "call_count") <= 0:
        return ""
    usage_returned = _usage_attr(summary, "usage_returned_count")
    call_count = _usage_attr(summary, "call_count")
    has_total_usage = usage_returned > 0
    lines = [
        TOKEN_USAGE_HEADING,
        "",
        (
            f"- 总计：输入 {_format_token_value(_usage_attr(summary, 'prompt_tokens'), has_total_usage)} / "
            f"输出 {_format_token_value(_usage_attr(summary, 'completion_tokens'), has_total_usage)} / "
            f"合计 {_format_token_value(_usage_attr(summary, 'total_tokens'), has_total_usage)} tokens"
        ),
        f"- 模型：{_usage_attr(summary, 'provider', '-')} / {_usage_attr(summary, 'model', '-')}",
        f"- 调用：{call_count} 次；返回 usage：{usage_returned} 次",
        "",
        "| 阶段 | 输入 | 输出 | 合计 | 调用 | usage |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    by_stage = _usage_attr(summary, "by_stage", {}) or {}
    for stage, stage_summary in sorted(by_stage.items()):
        stage_usage_returned = _usage_attr(stage_summary, "usage_returned_count")
        stage_has_usage = stage_usage_returned > 0
        lines.append(
            "| "
            f"{_token_stage_label(stage)} | "
            f"{_format_token_value(_usage_attr(stage_summary, 'prompt_tokens'), stage_has_usage)} | "
            f"{_format_token_value(_usage_attr(stage_summary, 'completion_tokens'), stage_has_usage)} | "
            f"{_format_token_value(_usage_attr(stage_summary, 'total_tokens'), stage_has_usage)} | "
            f"{_usage_attr(stage_summary, 'call_count')} | "
            f"{stage_usage_returned} |"
        )
    if usage_returned < call_count:
        lines.extend(["", "> 部分模型调用未返回 usage，未对缺失部分做估算。"])
    return "\n".join(lines)


def _usage_attr(value, name: str, default=0):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _format_token_value(value, has_usage: bool) -> str:
    return str(int(value or 0)) if has_usage else "未返回"


def _token_stage_label(stage: str) -> str:
    labels = {
        "query_router": "路由判断",
        "direct_answer": "直接回答",
        "question_analysis": "问题分析",
        "source_planning": "检索规划",
        "intermediate_draft": "中间草稿",
        "sufficiency_judge": "充分性判断",
        "next_retrieval_planning": "补检索规划",
        "final_answer": "最终生成",
        "unknown": "未标记",
    }
    return labels.get(str(stage or "unknown"), str(stage or "unknown"))


def clear_current_chat_session():
    user_id = st.session_state.get("user_id")
    session_id = st.session_state.get("chat_session_id")
    if user_id and session_id:
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


def build_query_log_metadata() -> dict:
    return {
        "ragflow_similarity_threshold": config.settings.RAGFLOW_SIMILARITY_THRESHOLD,
        "ragflow_vector_weight": config.settings.RAGFLOW_VECTOR_WEIGHT,
        "ragflow_top_k": config.settings.FINAL_TOP_K,
        "ragflow_governance_dataset": config.settings.RAGFLOW_GOVERNANCE_DATASET_NAME,
        "ragflow_design_dataset": config.settings.RAGFLOW_DESIGN_DATASET_NAME,
    }


def format_query_trace_params(trace) -> str:
    try:
        metadata = json.loads(trace.metadata_json or "{}")
    except json.JSONDecodeError:
        metadata = {}

    parts = [
        f"similarity={metadata.get('ragflow_similarity_threshold', '-')}",
        f"vector_weight={metadata.get('ragflow_vector_weight', '-')}",
        f"top_k={metadata.get('ragflow_top_k', '-')}",
        f"final_top_k={trace.final_top_k if trace.final_top_k is not None else '-'}",
    ]
    governance_dataset = metadata.get("ragflow_governance_dataset")
    design_dataset = metadata.get("ragflow_design_dataset")
    if governance_dataset or design_dataset:
        parts.append(f"datasets={governance_dataset or '-'} / {design_dataset or '-'}")
    return ", ".join(parts)


def _parse_log_metadata(raw: str) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def render_audit_event_detail(event) -> None:
    """审计日志行级详情：展示 dataframe 里放不下的完整 metadata / 目标 / 错误信息。"""
    st.markdown(
        f"**操作者**：{event.actor_username or '-'}"
        f"（角色 {event.actor_role or '-'}，ID {event.actor_user_id or '-'}）"
    )
    st.markdown(f"**部门 ID**：{event.department_id if event.department_id is not None else '-'}")
    st.markdown(f"**时间**：{format_local_time(event.created_at)}")
    st.markdown(f"**目标**：{event.target_type or '-'} / {event.target_id or '-'}")
    st.markdown(f"**知识库**：{event.kb_name or '-'}")
    st.markdown(f"**结果**：{'✅ 成功' if event.success else '❌ 失败'}")
    if event.error_message:
        st.markdown("**错误信息**：")
        st.code(event.error_message)
    metadata = _parse_log_metadata(event.metadata_json)
    st.markdown("**元数据**：")
    if metadata:
        st.json(metadata)
    else:
        st.caption("（无）")


def render_query_trace_detail(trace, viewer, log_service) -> None:
    """查询日志行级详情：展示改写问题、会话定位、检索参数、完整错误、命中证据等。

    脱敏在数据层完成（list_query_traces / list_evidence 对非 owner 返回 redacted），
    这里直接渲染字段即可，无需再判断角色。
    """
    st.markdown("**原问题**：")
    st.code(trace.original_query or "")
    if trace.rewritten_query:
        st.markdown("**改写后问题**：")
        st.code(trace.rewritten_query)
    st.markdown(
        f"**会话定位**：session={trace.chat_session_id or '-'}，"
        f"user_msg={trace.user_message_id or '-'}，"
        f"assistant_msg={trace.assistant_message_id or '-'}"
    )
    st.markdown(f"**后端 / 检索器**：{trace.backend or '-'} / {trace.retriever_type or '-'}")
    st.markdown(f"**耗时**：{trace.latency_ms if trace.latency_ms is not None else '-'} ms")
    st.markdown(f"**状态**：{trace.status or '-'}")
    if trace.error_message:
        st.markdown("**错误信息**：")
        st.code(trace.error_message)
    st.markdown(f"**时间**：{format_local_time(trace.created_at)}")
    st.markdown(f"**检索参数**：{format_query_trace_params(trace)}")
    metadata = _parse_log_metadata(trace.metadata_json)
    st.markdown("**元数据**：")
    if metadata:
        st.json(metadata)
    else:
        st.caption("（无）")

    try:
        evidence = log_service.list_evidence(viewer, trace.id)
    except Exception as exc:
        st.caption(f"证据读取失败：{exc}")
        evidence = []
    if evidence:
        with st.expander(f"命中证据（{len(evidence)} 条）", expanded=False):
            for item in evidence:
                score = item.rerank_score if item.rerank_score is not None else "-"
                st.markdown(f"**#{item.rank} · {item.file_name or '-'}**（score={score}）")
                if item.text_preview:
                    st.code(item.text_preview)


def format_container_inspection_warning(metadata: dict | None) -> str:
    if not metadata:
        return ""
    inspection = metadata.get("container_inspection") or {}
    if not isinstance(inspection, dict):
        return ""
    embedded_count = int(inspection.get("embedded_object_count") or 0)
    media_count = int(inspection.get("media_object_count") or 0)
    if embedded_count <= 0 and media_count <= 0:
        return ""
    parts = []
    if embedded_count:
        parts.append(f"{embedded_count} 个内嵌对象")
    if media_count:
        parts.append(f"{media_count} 个媒体对象")
    return "检测到 " + ", ".join(parts) + "，当前仅提示，尚未展开到子管道处理。"


def format_ragflow_document_status(status: str) -> tuple[str, str]:
    view = parse_status_view(status)
    if view.is_success:
        return view.label, "可检索"
    if view.is_failed:
        return view.label, "不可检索"
    if view.normalized in {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING, TASK_STATUS_PAUSED}:
        return view.label, "解析中"
    if view.normalized == TASK_STATUS_CANCELLED:
        return view.label, "已停止"
    return view.label, view.searchability


# ==================== 逻辑处理回调函数 ===================
def create_kb_callback(pipeline):
    """Create knowledge base callback."""
    raw_name = st.session_state.get("new_kb_name_input", "").strip()
    st.session_state.create_kb_error = None
    if not raw_name:
        st.session_state.create_kb_error = "名称不能为空"
        return
    try:
        name = validate_kb_name(raw_name.replace(" ", "_"))
    except InvalidKnowledgeBaseName as exc:
        st.session_state.create_kb_error = format_create_kb_error(str(exc))
        return

    ctx = build_request_context(st.session_state)
    ok, msg = pipeline.create_kb(name, ctx=ctx)
    if ok:
        st.session_state.kb_list = pipeline.list_knowledge_bases(ctx=ctx)
        refresh_kb_identity_map(st.session_state.kb_list)
        set_current_kb(name)
        st.session_state.show_create_kb = False
        st.session_state.toast_msg = msg
    else:
        st.session_state.create_kb_error = format_create_kb_error(msg)


def format_create_kb_error(message: str) -> str:
    if "may only contain letters" in message:
        return "知识库名称只能使用英文字母、数字、下划线、短横线和点号，并且必须以字母或数字开头。"
    if "cannot be empty" in message:
        return "名称不能为空。"
    if "consecutive dots" in message:
        return "知识库名称不能包含连续的点号。"
    return message


def delete_kb_confirmed(pipeline, kb_name):
    """Delete a confirmed knowledge base."""
    ctx = build_request_context(st.session_state)
    ok, msg = pipeline.delete_knowledge_base(kb_name, ctx=ctx)
    if ok:
        invalidate_file_cache(kb_name)
        ctx = build_request_context(st.session_state)
        st.session_state.kb_list = pipeline.list_knowledge_bases(ctx=ctx)
        refresh_kb_identity_map(st.session_state.kb_list)
        if st.session_state.current_kb == kb_name:
            set_current_kb(st.session_state.kb_list[0] if st.session_state.kb_list else None)
            reset_chat_state()
        st.session_state.toast_msg = msg
    else:
        st.session_state.error_msg = msg
    st.session_state.confirm_delete_kb = None


def switch_kb_callback(kb_name):
    """Switch the active knowledge base."""
    set_current_kb(kb_name)
    reset_chat_state()
    st.session_state.confirm_delete_file = None
    st.session_state.confirm_delete_kb = None


def refresh_kb_list(pipeline):
    ctx = build_request_context(st.session_state)
    st.session_state.kb_list = pipeline.list_knowledge_bases(ctx=ctx)
    refresh_kb_identity_map(st.session_state.kb_list)


def _kb_identity_stats_key(kb_id: int | str | None, department_id: int | str | None, kb_name: str) -> str:
    kb_id_value = int(kb_id or 0) if str(kb_id or "").isdigit() else 0
    if kb_id_value:
        return f"kb_id:{kb_id_value}"
    return f"department:{department_id or ''}:kb:{kb_name or ''}"


def _pipeline_governance_stats(ctx) -> dict:
    return AppPipeline.governance_stats(ctx)


def refresh_kb_identity_map(kb_names: list[str] | None = None):
    try:
        auth_service = init_auth_service()
        existing = kb_names or []
        summaries = auth_service.list_knowledge_base_summaries(existing)
    except Exception:
        st.session_state.kb_identity_map = {}
        return

    identity_map = {}
    identity_by_name = {}
    actor_department_id = st.session_state.get("department_id")
    for item in summaries:
        if kb_names and item.name not in kb_names:
            continue
        identity_key = _kb_identity_stats_key(item.kb_id, item.department_id, item.name)
        identity_map[identity_key] = {
            "name": item.name,
            "kb_id": item.kb_id,
            "department_id": item.department_id,
            "department_name": item.department_name,
            "label": f"{item.name} ({item.department_name})" if item.department_name else item.name,
        }
        existing_key = identity_by_name.get(item.name)
        existing = identity_map.get(existing_key) if existing_key else None
        prefer_item = (
            existing is None
            or item.department_id == actor_department_id
            or (existing.get("department_id") in (None, "") and item.department_id not in (None, ""))
        )
        if prefer_item:
            identity_by_name[item.name] = identity_key
    st.session_state.kb_identity_map = identity_map
    st.session_state.kb_identity_by_name = identity_by_name


def set_current_kb(kb_name: str | None = None, identity_key: str | None = None):
    if identity_key is None and kb_name:
        identity_key = st.session_state.get("kb_identity_by_name", {}).get(kb_name)
    identity = st.session_state.get("kb_identity_map", {}).get(identity_key or "", {})
    selected_name = identity.get("name") or kb_name
    st.session_state.current_kb = selected_name
    if selected_name:
        st.session_state.current_kb_identity = identity_key
        st.session_state.current_kb_id = identity.get("kb_id")
        st.session_state.current_kb_department_id = identity.get("department_id")
        st.session_state.kb_selector = identity_key or selected_name
    else:
        st.session_state.current_kb_identity = None
        st.session_state.current_kb_id = None
        st.session_state.current_kb_department_id = None
        if "kb_selector" in st.session_state:
            del st.session_state["kb_selector"]


def kb_selector_options() -> list[str]:
    options = [
        identity
        for name in st.session_state.get("kb_list", [])
        if (identity := st.session_state.get("kb_identity_by_name", {}).get(name))
    ]
    if options:
        return options
    return list(st.session_state.get("kb_list", []))


def format_kb_selector(identity_key: str) -> str:
    info = st.session_state.get("kb_identity_map", {}).get(identity_key)
    if info:
        return info.get("label") or info.get("name") or identity_key
    return identity_key


def get_manageable_kbs(pipeline) -> list[str]:
    ctx = build_request_context(st.session_state)
    if ctx.is_system_admin() and hasattr(pipeline, "list_all_knowledge_bases_for_admin"):
        return pipeline.list_all_knowledge_bases_for_admin(ctx=ctx)
    return st.session_state.kb_list


def has_current_kb_permission(required: str = "read") -> bool:
    kb_name = st.session_state.get("current_kb")
    return bool(kb_name and build_request_context(st.session_state).has_kb_permission(kb_name, required))


def get_cached_files(pipeline, kb_name: str) -> list[str]:
    ctx = build_request_context(st.session_state)
    kb_identity = st.session_state.get("current_kb_identity") or kb_name
    cache_key = f"{ctx.user_id}:{kb_identity}:{kb_name}"
    cached = st.session_state.file_cache.get(cache_key)
    now = time.time()
    # Cache with TTL so RAGFlow parse status/progress refreshes instead of
    # freezing at the upload-time state ("parsing") for the whole session.
    if not cached or now - cached.get("timestamp", 0) >= BACKEND_TASK_CACHE_TTL_SECONDS:
        cached = {"timestamp": now, "data": pipeline.list_files(kb_name, ctx=ctx)}
        st.session_state.file_cache[cache_key] = cached
    return cached["data"]


def get_cached_file_infos(pipeline, kb_name: str, ctx=None):
    ctx = ctx or build_request_context(st.session_state)
    kb_identity = st.session_state.get("current_kb_identity") or kb_name
    cache_key = f"{ctx.user_id}:{kb_identity}:{kb_name}:infos"
    cached = st.session_state.file_cache.get(cache_key)
    now = time.time()
    # Same TTL as parse-task cache: parsed/failed states surface within seconds.
    if not cached or now - cached.get("timestamp", 0) >= BACKEND_TASK_CACHE_TTL_SECONDS:
        if hasattr(pipeline, "list_file_infos"):
            cached = {"timestamp": now, "data": pipeline.list_file_infos(kb_name, ctx=ctx)}
        else:
            cached = {"timestamp": now, "data": []}
        st.session_state.file_cache[cache_key] = cached
    return cached["data"]


def invalidate_file_cache(kb_name: str):
    ctx = build_request_context(st.session_state)
    kb_identity = st.session_state.get("current_kb_identity") or kb_name
    st.session_state.file_cache.pop(f"{ctx.user_id}:{kb_identity}:{kb_name}", None)
    st.session_state.file_cache.pop(f"{ctx.user_id}:{kb_identity}:{kb_name}:infos", None)
    st.session_state.file_cache.pop(f"{ctx.user_id}:{kb_name}", None)
    st.session_state.file_cache.pop(f"{ctx.user_id}:{kb_name}:infos", None)


def _selected_parse_result_key(key_prefix: str) -> str:
    return f"{key_prefix}_selected_parse_result_file"


def toggle_parse_result_file(key_prefix: str, document_id: str):
    state_key = _selected_parse_result_key(key_prefix)
    st.session_state[state_key] = None if st.session_state.get(state_key) == document_id else document_id


def render_parse_result_detail(pipeline, kb_name: str, selected_document_id: str | None, display_name: str | None = None):
    if not selected_document_id:
        return
    ctx = build_request_context(st.session_state)
    result = pipeline.get_parse_result(kb_name, selected_document_id, ctx=ctx)
    title_name = display_name or selected_document_id
    with st.container(border=True):
        st.markdown(f"###### 解析分块 · {title_name}")
        if not result:
            st.caption("该文件尚未生成解析结果，或索引中没有找到对应分块。")
            return
        st.caption(f"共 {result.chunk_count} 个解析分块")
        for chunk in result.chunks:
            title = f"分块 {chunk.index + 1}"
            source_group = display_source_group(chunk.metadata.get("source_group"))
            page_label = chunk.metadata.get("page_label")
            details = [value for value in [source_group, f"第 {page_label} 页" if page_label else None] if value]
            if details:
                title = f"{title} · {' · '.join(details)}"
            with st.expander(title):
                st.write(chunk.content)


def render_compact_document_list(files: list[str]):
    for file_name in files:
        st.markdown(f"📄 {file_name}")


def format_task_time(timestamp: float | None) -> str:
    if not timestamp:
        return "-"
    return time.strftime("%H:%M:%S", time.localtime(timestamp))


BACKEND_TASK_CACHE_TTL_SECONDS = 5


def _parse_task_context_key(key_prefix: str, kb_name: str, ctx, suffix: str) -> str:
    resource_department_id = str(ctx.metadata.get("resource_department_id") or ctx.metadata.get("department_id") or "")
    kb_identity = st.session_state.get("current_kb_identity") or kb_name
    return f"{key_prefix}:{ctx.user_id}:{resource_department_id}:{kb_identity}:{kb_name}:{suffix}"


def _backend_task_cache_key(key_prefix: str, kb_name: str, ctx) -> str:
    return _parse_task_context_key(key_prefix, kb_name, ctx, "backend_parse_tasks")


def _clear_backend_task_cache(key_prefix: str, kb_name: str, ctx):
    st.session_state.pop(_backend_task_cache_key(key_prefix, kb_name, ctx), None)


def _get_parse_tasks_for_panel(pipeline, kb_name: str, ctx, key_prefix: str, uses_backend_tasks: bool):
    if not uses_backend_tasks:
        return pipeline.list_parse_tasks(kb_name, ctx=ctx)

    cache_key = _backend_task_cache_key(key_prefix, kb_name, ctx)
    cached = st.session_state.get(cache_key)
    now = time.time()
    if cached and now - cached.get("timestamp", 0) < BACKEND_TASK_CACHE_TTL_SECONDS:
        return cached.get("tasks", [])

    tasks = pipeline.list_parse_tasks(kb_name, ctx=ctx)
    st.session_state[cache_key] = {"timestamp": now, "tasks": tasks}
    return tasks


def _should_show_parse_task(task, uses_backend_tasks: bool) -> bool:
    if not uses_backend_tasks:
        return True
    status = parse_status_view(task.status)
    return not status.is_success and status.normalized != TASK_STATUS_CANCELLED


def _safe_task_progress(progress) -> int:
    try:
        value = int(progress or 0)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(100, value))


def _track_parse_task_file_cache(kb_name: str, key_prefix: str, ctx, all_tasks: list, visible_tasks: list, uses_backend_tasks: bool):
    if uses_backend_tasks:
        visible_key = _parse_task_context_key(key_prefix, kb_name, ctx, "backend_visible_task_ids")
        current_visible_ids = {task.id for task in visible_tasks}
        previous_visible_ids = set(st.session_state.get(visible_key, []))
        if previous_visible_ids and previous_visible_ids - current_visible_ids:
            invalidate_file_cache(kb_name)
        st.session_state[visible_key] = list(current_visible_ids)
        return

    completed_key = _parse_task_context_key(key_prefix, kb_name, ctx, "local_completed_task_ids")
    completed_ids = {task.id for task in all_tasks if parse_status_view(task.status).is_success}
    previous_completed_ids = set(st.session_state.get(completed_key, []))
    if completed_ids - previous_completed_ids:
        invalidate_file_cache(kb_name)
    st.session_state[completed_key] = list(completed_ids)


def _render_parse_task_summary(tasks: list, uses_backend_tasks: bool):
    counts = {
        TASK_STATUS_QUEUED: 0,
        TASK_STATUS_RUNNING: 0,
        TASK_STATUS_PAUSED: 0,
        TASK_STATUS_FAILED: 0,
    }
    for task in tasks:
        normalized = parse_status_view(task.status).normalized
        if normalized in counts:
            counts[normalized] += 1

    parts = [
        f"当前显示 {len(tasks)} 个任务",
        f"解析中 {counts[TASK_STATUS_RUNNING]}",
        f"排队 {counts[TASK_STATUS_QUEUED]}",
        f"失败 {counts[TASK_STATUS_FAILED]}",
    ]
    if uses_backend_tasks:
        parts.append("已完成文档会进入文件列表")
    st.caption(" - ".join(parts))


@st.fragment
def render_parse_task_panel(pipeline, kb_name: str, key_prefix: str):
    ctx = build_request_context(st.session_state)
    uses_backend_tasks = True

    with st.container(border=True):
        c_title, c_refresh, c_clear = st.columns([0.68, 0.16, 0.16])
        with c_title:
            st.markdown("##### 解析 / 索引任务")
        with c_refresh:
            if st.button("刷新", key=f"{key_prefix}_refresh_tasks", use_container_width=True):
                _clear_backend_task_cache(key_prefix, kb_name, ctx)
                invalidate_file_cache(kb_name)
                st.rerun()
        with c_clear:
            st.button(
                "清理完成",
                key=f"{key_prefix}_clear_tasks",
                use_container_width=True,
                disabled=True,
                help="RAGFlow 完成文档会进入文件列表，此处不清理远端记录。",
            )

        st.caption("显示 RAGFlow 文档解析与结构化索引任务；完成后会进入文件列表。")

        try:
            all_tasks = _get_parse_tasks_for_panel(pipeline, kb_name, ctx, key_prefix, uses_backend_tasks)
        except Exception as exc:
            st.warning(f"解析任务读取失败: {exc}")
            return

        tasks = [task for task in all_tasks if _should_show_parse_task(task, uses_backend_tasks)]
        _track_parse_task_file_cache(kb_name, key_prefix, ctx, all_tasks, tasks, uses_backend_tasks)

        if not tasks:
            st.caption("当前知识库暂无解析任务")
            return

        _render_parse_task_summary(tasks, uses_backend_tasks)

        for task in tasks:
            task_status = parse_status_view(task.status)
            progress = _safe_task_progress(task.progress)
            stage = task.stage or task_status.label
            source_group = display_source_group(task.source_group) if task.source_group else ""
            details = [task_status.label, task_status.searchability, source_group]
            detail_text = " - ".join(value for value in details if value)
            st.divider()
            c_info, c_actions = st.columns([0.72, 0.28])
            with c_info:
                st.markdown(f"**{task.original_name}** - {detail_text}")
                st.progress(progress, text=f"{progress}% - {stage}")
                st.caption(f"最后更新 {format_task_time(task.updated_at)}")
                if task.message:
                    if task_status.is_failed:
                        st.error(task.message)
                    else:
                        st.caption(task.message)
            with c_actions:
                action_label = "移除任务" if task_status.is_failed else "停止任务"
                action_help = (
                    "移除该失败任务、本地归档与映射；远端清理为尽力执行。"
                    if task_status.is_failed
                    else "停止并移除该未完成解析/索引任务。"
                )
                if task_status.can_cancel:
                    if st.button(action_label, key=f"{key_prefix}_stop_{task.id}", use_container_width=True, help=action_help):
                        st.session_state.toast_msg = pipeline.delete_parse_task(task.id, ctx=ctx)
                        _clear_backend_task_cache(key_prefix, kb_name, ctx)
                        invalidate_file_cache(kb_name)
                        st.rerun()
                else:
                    st.button(action_label, key=f"{key_prefix}_stop_noop_{task.id}", disabled=True, use_container_width=True, help=action_help)


def spreadsheet_profile_summary(metadata: dict | None) -> dict:
    profile = (metadata or {}).get("spreadsheet_profile") or {}
    status = str((metadata or {}).get("status") or "").lower()
    status_view = parse_status_view(status, "spreadsheet_table")

    sheets = profile.get("sheets") or []
    row_count = sum(int(sheet.get("non_empty_row_count") or 0) for sheet in sheets)
    cell_count = sum(int(sheet.get("non_empty_cell_count") or 0) for sheet in sheets)
    block_count = int(profile.get("text_block_count") or 0)
    semantic_row_count = int(profile.get("semantic_row_count") or 0)
    object_count = sum(
        int(profile.get(key) or 0)
        for key in ("embedded_object_count", "media_object_count", "drawing_object_count")
    )
    return {
        "profile": profile,
        "status_view": status_view,
        "sheet_count": len(sheets),
        "row_count": row_count,
        "cell_count": cell_count,
        "block_count": block_count,
        "semantic_row_count": semantic_row_count,
        "object_count": object_count,
        "warnings": profile.get("warnings") or [],
    }


def format_spreadsheet_profile(metadata: dict | None) -> tuple[str, str, str]:
    summary = spreadsheet_profile_summary(metadata)
    status_view = summary["status_view"]
    if not status_view.is_success:
        return "待处理", status_view.searchability, "Excel 文件已保存，但尚未完成解析。"
    detail = (
        f"{summary['sheet_count']} 个工作表，"
        f"{summary['row_count']} 行有效数据，"
        f"{summary['block_count']} 个行块，"
        f"{summary['semantic_row_count']} 个语义行"
    )
    if summary["object_count"]:
        detail += f"，检测到 {summary['object_count']} 个嵌入/媒体/绘图对象"
    return "原貌结构", "结构化解析", detail


def render_excel_ledger_panel(file_infos: list, key_prefix: str):
    spreadsheet_infos = [
        info for info in file_infos
        if getattr(info, "processor_kind", "") == "spreadsheet_table"
    ]
    if not spreadsheet_infos:
        return

    rows = []
    for info in spreadsheet_infos:
        metadata = info.metadata or {}
        summary = spreadsheet_profile_summary(metadata)
        profile = summary["profile"]
        status_view = summary["status_view"]
        rows.append(
            {
                "文件": info.name,
                "状态": status_view.label,
                "结构": "已解析" if status_view.is_success else status_view.searchability,
                "工作表": summary["sheet_count"],
                "有效行": summary["row_count"],
                "单元格": summary["cell_count"],
                "语义行": summary["semantic_row_count"],
                "块": summary["block_count"],
                "对象": summary["object_count"],
                "record_id": metadata.get("store_id", ""),
                "kb_id": profile.get("kb_id", ""),
                "归档路径": info.local_path or metadata.get("local_path", ""),
            }
        )

    with st.expander("Excel 结构化台账", expanded=False):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Excel 文件", len(spreadsheet_infos))
        c2.metric("工作表", sum(row["工作表"] for row in rows))
        c3.metric("语义行", sum(row["语义行"] for row in rows))
        c4.metric("待处理", sum(1 for row in rows if row["结构"] != "已解析"))

        st.dataframe(
            rows,
            width="stretch",
            hide_index=True,
            column_config={
                "文件": st.column_config.TextColumn(width="large"),
                "状态": st.column_config.TextColumn(width="small"),
                "结构": st.column_config.TextColumn(width="small"),
                "工作表": st.column_config.NumberColumn(width="small"),
                "有效行": st.column_config.NumberColumn(width="small"),
                "单元格": st.column_config.NumberColumn(width="small"),
                "语义行": st.column_config.NumberColumn(width="small"),
                "块": st.column_config.NumberColumn(width="small"),
                "对象": st.column_config.NumberColumn(width="small"),
                "record_id": st.column_config.TextColumn(width="small"),
                "kb_id": st.column_config.TextColumn(width="small"),
                "归档路径": st.column_config.TextColumn(width="large"),
            },
        )

        selected = st.selectbox(
            "查看工作表结果",
            spreadsheet_infos,
            format_func=lambda item: item.name,
            key=f"{key_prefix}_excel_ledger_select",
        )
        profile = ((selected.metadata or {}).get("spreadsheet_profile") or {})
        sheets = profile.get("sheets") or []
        if sheets:
            st.dataframe(
                [
                    {
                        "sheet": sheet.get("sheet_name", ""),
                        "行数": sheet.get("row_count", 0),
                        "列数": sheet.get("column_count", 0),
                        "有效行": sheet.get("non_empty_row_count", 0),
                        "单元格": sheet.get("non_empty_cell_count", 0),
                        "表头行": str(sheet.get("header_row_index") or "-"),
                        "语义行": sheet.get("semantic_row_count", 0),
                        "文本块": sheet.get("text_block_count", 0),
                    }
                    for sheet in sheets
                ],
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("该 Excel 尚未生成 sheet 结构明细。")


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
                st.error("用户名或密码错误")
        st.markdown("""
            <div class="login-footnote">
                使用管理员分配的账号登录
            </div>
        """, unsafe_allow_html=True)


def logout():
    # Pass the current user into revoke_session so it doesn't re-query
    # get_user_by_token(token) just to resolve the actor for the audit row.
    init_auth_service().revoke_session(
        st.session_state.get("auth_token"),
        actor=current_auth_user(),
    )
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

    def _val(key, default=""):
        """Read current config value with DEFAULT_VALUES fallback."""
        return getattr(config.settings, key, config.settings.DEFAULT_VALUES.get(key, default))

    def _check_ragflow_connection():
        base_url = str(st.session_state.get("cfg_ragflow_base_url", "")).rstrip("/")
        api_key = str(st.session_state.get("cfg_ragflow_api_key", ""))
        dataset_names = [
            str(st.session_state.get("cfg_ragflow_governance_dataset", "")),
            str(st.session_state.get("cfg_ragflow_design_dataset", "")),
        ]
        timeout = int(st.session_state.get("cfg_ragflow_timeout", 120))
        ok, message, missing = AppPipeline.check_ragflow_connection(base_url, api_key, dataset_names, timeout)
        if not ok:
            st.error(message)
        elif missing:
            st.warning(message)
        else:
            st.success(message)

    with st.expander("登录与会话"):
        st.text_input(
            "认证数据库路径",
            value=str(_val("AUTH_DB_PATH", "storage/auth.db")),
            key="cfg_auth_db_path",
            help="SQLite 用户与会话数据库路径。",
        )
        st.text_input(
            "默认管理员用户名",
            value=str(_val("AUTH_DEFAULT_ADMIN_USERNAME", "admin")),
            key="cfg_auth_admin_username",
            help="仅在认证数据库中不存在该用户时自动创建。",
        )
        st.text_input(
            "默认管理员密码",
            value=str(_val("AUTH_DEFAULT_ADMIN_PASSWORD", "")),
            key="cfg_auth_admin_password",
            type="password",
            help="仅首次创建默认管理员时使用。",
        )
        st.number_input(
            "会话有效期（小时）",
            min_value=1,
            max_value=24 * 30,
            value=int(_val("AUTH_SESSION_TTL_HOURS", "24")),
            key="cfg_auth_session_ttl",
        )

    with st.expander("模型配置", expanded=True):
        provider_options = ["ollama", "custom"]
        current_provider = _val("AGENT_LLM_PROVIDER", "ollama")
        if isinstance(current_provider, config.settings.Provider):
            current_provider = current_provider.value

        provider = st.radio(
            "Agent LLM Provider",
            options=provider_options,
            index=provider_options.index(current_provider),
            horizontal=True,
            key="cfg_provider",
            help="只影响 Agent 最终答案生成。",
        )

        if provider == "ollama":
            st.text_input("Agent Ollama Base URL", value=_val("AGENT_OLLAMA_BASE_URL", "http://localhost:11434"), key="cfg_ollama_base_url")
            st.text_input(
                "Agent Ollama 模型",
                value=_val("AGENT_OLLAMA_MODEL", "qwen2.5:32b"),
                key="cfg_agent_ollama_model",
                help="例如 qwen2.5:32b",
            )
        else:
            st.text_input("Agent API Key", value=_val("AGENT_CUSTOM_API_KEY", ""), key="cfg_custom_api_key", type="password")
            st.text_input(
                "Agent Base URL",
                value=_val("AGENT_CUSTOM_BASE_URL", ""),
                key="cfg_custom_base_url",
                help="例如 https://api.openai.com/v1",
            )
            st.text_input(
                "Agent LLM 模型",
                value=_val("AGENT_CUSTOM_MODEL", ""),
                key="cfg_agent_custom_model",
                help="例如 gpt-4o, deepseek-chat",
            )
            c1, c2 = st.columns(2)
            with c1:
                st.number_input("Temperature", min_value=0.0, max_value=2.0, value=float(_val("AGENT_TEMPERATURE", "0.2")), step=0.1, key="cfg_agent_temperature")
            with c2:
                st.number_input("Max Tokens", min_value=256, max_value=65536, value=int(_val("AGENT_CUSTOM_MAX_TOKENS", "4096")), step=256, key="cfg_custom_max_tokens")
            st.number_input("Timeout Seconds", min_value=10, max_value=600, value=int(_val("AGENT_TIMEOUT_SECONDS", "120")), step=10, key="cfg_agent_timeout")

    # ==================== RAG config ====================
    with st.expander("RAG 配置", expanded=True):
        st.caption("RAG 后端固定为 RAGFlow，本地向量知识库链路已移除。")
        st.text_input(
            "RAGFlow Base URL",
            value=str(_val("RAGFLOW_BASE_URL", "http://localhost:9380")),
            key="cfg_ragflow_base_url",
            help="例如 http://localhost:9380 或你的 RAGFlow 网关地址",
        )
        st.text_input(
            "RAGFlow API Key",
            value=str(_val("RAGFLOW_API_KEY", "")),
            key="cfg_ragflow_api_key",
            type="password",
        )
        d1, d2 = st.columns(2)
        with d1:
            st.text_input(
                "部门治理 Dataset",
                value=str(_val("RAGFLOW_GOVERNANCE_DATASET_NAME", "department_governance")),
                key="cfg_ragflow_governance_dataset",
            )
        with d2:
            st.text_input(
                "设计资料 Dataset",
                value=str(_val("RAGFLOW_DESIGN_DATASET_NAME", "project_design_assets")),
                key="cfg_ragflow_design_dataset",
            )
        if st.button("检查 RAGFlow 连接", key="cfg_check_ragflow_connection"):
            _check_ragflow_connection()

        st.divider()
        st.markdown("###### RAGFlow 检索")
        p1, p2, p3, p4 = st.columns(4)
        with p1:
            st.number_input(
                "超时秒数",
                min_value=10,
                max_value=600,
                value=int(_val("RAGFLOW_TIMEOUT_SECONDS", "120")),
                key="cfg_ragflow_timeout",
            )
        with p2:
            st.number_input(
                "相似度阈值",
                min_value=0.0,
                max_value=1.0,
                value=float(_val("RAGFLOW_SIMILARITY_THRESHOLD", "0.25")),
                step=0.05,
                key="cfg_ragflow_similarity",
            )
        with p3:
            st.number_input(
                "向量权重",
                min_value=0.0,
                max_value=1.0,
                value=float(_val("RAGFLOW_VECTOR_WEIGHT", "0.4")),
                step=0.05,
                key="cfg_ragflow_vector_weight",
            )
        with p4:
            st.number_input(
                "Final Top-K",
                min_value=1,
                max_value=50,
                value=int(_val("FINAL_TOP_K", "5")),
                key="cfg_final_top_k",
            )
    # ==================== System prompts ====================
    with st.expander("系统提示词"):
        default_system_prompt = config.settings.DEFAULT_VALUES.get("SYSTEM_PROMPT", "")
        current_system_prompt = _val("SYSTEM_PROMPT", default_system_prompt)
        if isinstance(current_system_prompt, bool):
            current_system_prompt = default_system_prompt
        st.text_area(
            "System Prompt",
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

    # ==================== Actions ====================
    st.divider()
    st.caption("修改后点击应用配置生效。")
    _, col_apply, _ = st.columns([2, 1, 2])
    with col_apply:
        if st.button("应用配置", type="primary", width="stretch"):
            _apply_settings()


def _validate_settings(new_settings: dict) -> list[str]:
    """Validate settings collected from the Streamlit settings page."""
    errors = []
    provider = new_settings.get("AGENT_LLM_PROVIDER", "ollama")

    if provider == "ollama":
        if not new_settings.get("AGENT_OLLAMA_BASE_URL"):
            errors.append("Agent Ollama Base URL 不能为空")
        if not new_settings.get("AGENT_OLLAMA_MODEL"):
            errors.append("Agent Ollama 模型名不能为空")
    elif provider == "custom":
        if not new_settings.get("AGENT_CUSTOM_API_KEY"):
            errors.append("Agent API Key 不能为空")
        if not new_settings.get("AGENT_CUSTOM_BASE_URL"):
            errors.append("Agent Base URL 不能为空")
        if not new_settings.get("AGENT_CUSTOM_MODEL"):
            errors.append("Agent LLM 模型名不能为空")

    if not new_settings.get("RAGFLOW_BASE_URL"):
        errors.append("RAGFlow Base URL 不能为空")
    if not new_settings.get("RAGFLOW_API_KEY"):
        errors.append("RAGFlow API Key 不能为空")

    for key in ["AGENT_CUSTOM_MAX_TOKENS", "AGENT_TIMEOUT_SECONDS", "RAGFLOW_TIMEOUT_SECONDS", "FINAL_TOP_K"]:
        val = new_settings.get(key, "")
        if val:
            try:
                if int(val) <= 0:
                    errors.append(f"{key} 必须为正整数")
            except ValueError:
                errors.append(f"{key} 不是有效整数: {val}")

    return errors


def _apply_settings():
    """Collect, validate, persist and reload settings."""
    new_settings = {
        "AUTH_DB_PATH": st.session_state.get("cfg_auth_db_path", ""),
        "AUTH_DEFAULT_ADMIN_USERNAME": st.session_state.get("cfg_auth_admin_username", ""),
        "AUTH_DEFAULT_ADMIN_PASSWORD": st.session_state.get("cfg_auth_admin_password", ""),
        "AUTH_SESSION_TTL_HOURS": str(st.session_state.get("cfg_auth_session_ttl", 24)),
    }

    provider = st.session_state.get("cfg_provider", "ollama")
    new_settings["AGENT_LLM_PROVIDER"] = provider

    if provider == "ollama":
        new_settings["AGENT_OLLAMA_BASE_URL"] = st.session_state.get("cfg_ollama_base_url", "")
        new_settings["AGENT_OLLAMA_MODEL"] = st.session_state.get("cfg_agent_ollama_model", "")
    else:
        new_settings["AGENT_CUSTOM_API_KEY"] = st.session_state.get("cfg_custom_api_key", "")
        new_settings["AGENT_CUSTOM_BASE_URL"] = st.session_state.get("cfg_custom_base_url", "")
        new_settings["AGENT_CUSTOM_MODEL"] = st.session_state.get("cfg_agent_custom_model", "")
        new_settings["AGENT_CUSTOM_MAX_TOKENS"] = str(st.session_state.get("cfg_custom_max_tokens", 4096))
        new_settings["AGENT_TEMPERATURE"] = str(st.session_state.get("cfg_agent_temperature", 0.2))
        new_settings["AGENT_TIMEOUT_SECONDS"] = str(st.session_state.get("cfg_agent_timeout", 120))

    new_settings["RAGFLOW_BASE_URL"] = st.session_state.get("cfg_ragflow_base_url", "")
    new_settings["RAGFLOW_API_KEY"] = st.session_state.get("cfg_ragflow_api_key", "")
    new_settings["RAGFLOW_GOVERNANCE_DATASET_NAME"] = st.session_state.get(
        "cfg_ragflow_governance_dataset",
        "department_governance",
    )
    new_settings["RAGFLOW_DESIGN_DATASET_NAME"] = st.session_state.get(
        "cfg_ragflow_design_dataset",
        "project_design_assets",
    )
    new_settings["RAGFLOW_TIMEOUT_SECONDS"] = str(st.session_state.get("cfg_ragflow_timeout", 120))
    new_settings["RAGFLOW_SIMILARITY_THRESHOLD"] = str(st.session_state.get("cfg_ragflow_similarity", 0.25))
    new_settings["RAGFLOW_VECTOR_WEIGHT"] = str(st.session_state.get("cfg_ragflow_vector_weight", 0.4))
    new_settings["FINAL_TOP_K"] = str(st.session_state.get("cfg_final_top_k", 5))

    system_prompt = st.session_state.get("cfg_system_prompt", "")
    no_context_prompt = st.session_state.get("cfg_no_context_prompt", "")
    if system_prompt:
        new_settings["SYSTEM_PROMPT"] = system_prompt
    if no_context_prompt:
        new_settings["NO_CONTEXT_PROMPT"] = no_context_prompt

    errors = _validate_settings(new_settings)
    if errors:
        for e in errors:
            st.error(e)
        return

    try:
        AppPipeline.apply_settings(new_settings)
        init_pipeline.clear()
        init_auth_service.clear()
        reset_chat_state()
        record_audit(
            "change_settings",
            target_type="system_settings",
            target_id="env",
            metadata={
                "agent_llm_provider": new_settings.get("AGENT_LLM_PROVIDER"),
                "rag_backend": "ragflow",
            },
        )
        st.session_state.toast_msg = "配置已更新并生效"
        st.rerun()
    except Exception as e:
        st.error(f"应用配置失败: {e}")
        st.warning("配置已保存到 .env，但重新初始化失败。请检查配置后重试。")


AUDIT_ACTION_LABELS = {
    "login_success": "登录成功",
    "login_failed": "登录失败",
    "logout": "登出",
    "create_kb": "创建知识库",
    "delete_kb": "删除知识库",
    "upload_document": "上传文档",
    "delete_document": "删除文档",
    "grant_kb_permission": "授权知识库",
    "revoke_kb_permission": "撤销知识库权限",
    "change_settings": "修改配置",
    "create_user": "创建用户",
    "set_user_active": "启停用户",
    "reset_user_password": "重置密码",
    "create_department": "创建部门",
    "delete_department": "删除部门",
    "assign_kb": "重挂知识库",
    "delete_parse_task": "取消解析任务",
    "clear_parse_tasks": "清理解析任务",
}


def _audit_action_label(action: str) -> str:
    return AUDIT_ACTION_LABELS.get(action, action)


def build_audit_rows(events) -> list[dict]:
    """审计日志表格行：列与 events 同序，便于按 dataframe 选中行索引回查事件。"""
    return [
        {
            "ID": e.id,
            "时间": format_local_time(e.created_at),
            "操作者": e.actor_username or "-",
            "角色": e.actor_role or "-",
            "动作": _audit_action_label(e.action),
            "目标": f"{e.target_type or '-'} / {e.target_id or '-'}",
            "知识库": e.kb_name or "-",
            "结果": "✅" if e.success else "❌",
        }
        for e in events
    ]


def build_query_rows(traces, show_query_content: bool) -> list[dict]:
    """查询日志表格行。系统管理员看他人查询时原文脱敏为「已隐藏」。"""
    rows = []
    for t in traces:
        if not show_query_content:
            summary = "已隐藏"
        else:
            summary = (t.original_query or "")[:60] or "(空)"
        rows.append(
            {
                "ID": t.id,
                "时间": format_local_time(t.created_at),
                "用户": t.username or "-",
                "知识库": t.kb_name or "-",
                "问题摘要": summary,
                "耗时(ms)": t.latency_ms if t.latency_ms is not None else "-",
                "状态": "✅" if t.status == "success" else "❌",
            }
        )
    return rows


def _selected_row_index(key: str) -> int | None:
    """读取 st.dataframe 单行选中的首条行索引，未选中返回 None。"""
    sel = st.session_state.get(key)
    if not sel:
        return None
    rows = getattr(sel, "selection", None)
    rows = getattr(rows, "rows", None) if rows is not None else None
    return rows[0] if rows else None


def render_log_center_tab():
    st.markdown('<div style="height: 30px;"></div>', unsafe_allow_html=True)
    st.subheader("日志中心")

    viewer = current_auth_user()
    if viewer is None or viewer.role not in {ROLE_SYSTEM_ADMIN, ROLE_DEPT_ADMIN}:
        st.error("当前账号无权访问日志中心")
        st.stop()

    if viewer.role == ROLE_SYSTEM_ADMIN:
        st.caption("全局视图：可查看全部部门审计与查询日志，他人查询原文已脱敏。")
    else:
        st.caption("本部门视图：仅显示本部门日志，他人查询原文已脱敏。")

    log_service = init_log_service()
    tab_audit, tab_query = st.tabs(["审计日志", "查询日志"])

    # -------------------- 审计日志 --------------------
    with tab_audit:
        c1, c2, c3, c4 = st.columns([0.22, 0.22, 0.22, 0.34])
        with c1:
            audit_action_options = [""] + log_service.list_audit_actions(viewer)
            audit_action = st.selectbox(
                "动作",
                audit_action_options,
                format_func=lambda value: "全部" if not value else _audit_action_label(value),
                key="audit_action_filter",
            )
        with c2:
            audit_success = st.selectbox(
                "结果",
                [None, True, False],
                format_func=lambda value: "全部" if value is None else ("成功" if value else "失败"),
                key="audit_success_filter",
            )
        with c3:
            audit_kb = st.text_input("知识库", key="audit_kb_filter")
        with c4:
            audit_keyword = st.text_input("关键词", placeholder="用户名、对象、错误信息", key="audit_keyword")

        audit_kb_v = audit_kb.strip() or None
        audit_kw_v = audit_keyword.strip() or None
        audit_total = log_service.count_audit_events(
            viewer, action=audit_action or None, kb_name=audit_kb_v, success=audit_success, keyword=audit_kw_v
        )
        audit_bk = log_service.audit_breakdown(
            viewer, action=audit_action or None, kb_name=audit_kb_v, success=audit_success, keyword=audit_kw_v
        )

        m1, m2, m3 = st.columns(3)
        m1.metric("审计事件总数", audit_total)
        m2.metric("成功", audit_bk["success"])
        m3.metric("失败", audit_bk["failed"])

        with st.expander("近 7 日趋势 / 动作分布", expanded=False):
            daily = log_service.audit_recent_daily(viewer, days=7)
            if daily:
                st.dataframe(
                    [{"日期": d, "审计事件数": n} for d, n in daily],
                    width="stretch",
                    hide_index=True,
                )
            else:
                st.caption("近 7 日无审计事件")
            action_bd = log_service.audit_action_breakdown(
                viewer, kb_name=audit_kb_v, success=audit_success, keyword=audit_kw_v
            )
            if action_bd:
                top3 = "、".join(f"{_audit_action_label(a)} {n}" for a, n in action_bd[:3])
                st.caption(f"动作分布 Top3：{top3}")
            else:
                st.caption("暂无动作分布数据")

        audit_events = log_service.list_audit_events(
            viewer,
            action=audit_action or None,
            kb_name=audit_kb_v,
            success=audit_success,
            keyword=audit_kw_v,
            limit=300,
        )
        if not audit_events:
            st.info("暂无审计日志")
        else:
            st.caption(f"共 {audit_total} 条，展示 {len(audit_events)} 条，点击表格行查看详情")
            audit_rows = build_audit_rows(audit_events)
            st.dataframe(
                audit_rows,
                width="stretch",
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key="audit_table_select",
            )
            sel_idx = _selected_row_index("audit_table_select")
            if sel_idx is not None and 0 <= sel_idx < len(audit_events):
                event = audit_events[sel_idx]
                st.markdown(f"#### 详情 #{event.id}")
                render_audit_event_detail(event)
            else:
                st.info("点击表格任意一行查看详情")

    # -------------------- 查询日志 --------------------
    with tab_query:
        c1, c2, c3 = st.columns([0.25, 0.25, 0.5])
        with c1:
            query_status = st.selectbox(
                "状态",
                ["", "success", "failed", "partial", "no_evidence"],
                format_func=lambda value: "全部" if not value else value,
                key="query_status_filter",
            )
        with c2:
            query_kb = st.text_input("知识库", key="query_kb_filter")
        with c3:
            show_query_content = viewer.role != ROLE_SYSTEM_ADMIN
            query_keyword = st.text_input(
                "关键词",
                placeholder="用户名、问题、错误信息" if show_query_content else "用户名、错误信息",
                key="query_keyword",
            )

        query_kb_v = query_kb.strip() or None
        query_kw_v = query_keyword.strip() or None
        query_total = log_service.count_query_traces(
            viewer, kb_name=query_kb_v, status=query_status or None, keyword=query_kw_v
        )
        q_bk = log_service.query_status_breakdown(
            viewer, kb_name=query_kb_v, status=query_status or None, keyword=query_kw_v
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("查询日志总数", query_total)
        m2.metric("成功", q_bk["success"])
        m3.metric("失败", q_bk["failed"])
        m4.metric("部分/无证据", q_bk["partial"] + q_bk["no_evidence"])

        with st.expander("失败原因 Top 5", expanded=False):
            failures = log_service.query_failure_top(viewer, kb_name=query_kb_v, keyword=query_kw_v, limit=5)
            if failures:
                st.dataframe(
                    [{"失败原因": r, "次数": n} for r, n in failures],
                    width="stretch",
                    hide_index=True,
                )
            else:
                st.caption("暂无失败查询")

        traces = log_service.list_query_traces(
            viewer,
            kb_name=query_kb_v,
            status=query_status or None,
            keyword=query_kw_v,
            limit=300,
        )
        if not traces:
            st.info("暂无查询日志")
        else:
            st.caption(f"共 {query_total} 条，展示 {len(traces)} 条，点击表格行查看详情")
            query_rows = build_query_rows(traces, show_query_content)
            st.dataframe(
                query_rows,
                width="stretch",
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key="query_table_select",
            )
            sel_idx = _selected_row_index("query_table_select")
            if sel_idx is not None and 0 <= sel_idx < len(traces):
                trace = traces[sel_idx]
                st.markdown(f"#### 详情 #{trace.id}")
                render_query_trace_detail(trace, viewer, log_service)
            else:
                st.info("点击表格任意一行查看详情")


def _agent_thread_id_for_current_kb() -> str:
    return f"{st.session_state.get('chat_session_id') or st.session_state.get('session_id')}:{st.session_state.current_kb}"


def render_chat_tab(pipeline):
    st.markdown('<div style="height: 30px;"></div>', unsafe_allow_html=True)
    if not st.session_state.current_kb:
        st.info("暂无可用知识库，请先创建知识库或联系管理员授权。")
        return
    ensure_current_chat_session()

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
                    if content.startswith("Error:") or content == "Empty response.":
                        st.error(content)
                    else:
                        display_content, observation, token_footer = split_assistant_diagnostics(content)
                        separator = "**🔍 检索到的上下文:**"
                        fallback_separator = "**检索到的上下文:**"
                        active_separator = separator if separator in display_content else fallback_separator
                        if active_separator in display_content:
                            parts = display_content.split(active_separator, 1)
                            st.markdown(parts[0].strip())
                            with st.expander("📚 参考来源"):
                                st.markdown(parts[1].strip())
                        else:
                            st.markdown(display_content)
                        if observation:
                            with st.expander("Agent 观测", expanded=False):
                                st.markdown(observation)
                        if token_footer:
                            with st.expander("Token 使用量", expanded=False):
                                st.markdown(token_footer)

    should_process_user_message = st.session_state.messages and st.session_state.messages[-1]["role"] == "user"
    if should_process_user_message:
        user_input_to_process = st.session_state.messages[-1]["content"]
        query_started_at = time.perf_counter()
        agent_thread_id = _agent_thread_id_for_current_kb()
        agent_query = user_input_to_process

        chat_history = []
        messages_for_history = st.session_state.messages[:-1]
        user_msg = None
        for msg in messages_for_history:
            if msg["role"] == "user":
                user_msg = msg["content"]
            elif msg["role"] == "assistant" and user_msg is not None:
                chat_history.append((user_msg, strip_agent_observation(msg["content"])))
                user_msg = None

        with st.chat_message("assistant", avatar="😽"):
            error_occured = None
            agent_footer = ""
            token_usage_footer = ""
            try:
                ctx = build_request_context(st.session_state)
                gen = pipeline.query(
                    agent_query,
                    st.session_state.current_kb,
                    chat_history[-5:],
                    ctx=ctx,
                    agent_thread_id=agent_thread_id,
                )
            except Exception as e:
                error_occured = str(e)

            if error_occured:
                st.error(f"❌ 处理请求时发生错误: {error_occured}")
                full_response = f"Error: {error_occured}"
            else:
                status = st.status("正在处理您的请求...", expanded=False)
                has_started_answer = False

                def stream_with_status():
                    nonlocal has_started_answer
                    for chunk in gen:
                        if not has_started_answer:
                            status.update(label="正在生成回答...", state="running", expanded=False)
                            has_started_answer = True
                        yield chunk

                full_response = st.write_stream(stream_with_status())
                status.update(label="回答生成完毕", state="complete", expanded=False)
                if isinstance(full_response, list):
                    full_response = "".join(str(item) for item in full_response)
                if not full_response or not full_response.strip():
                    st.warning("⚠️ AI 未生成任何内容。")
                    full_response = "Empty response."
                # Agent observability footer (trace / route note / retrieval
                # diagnostics) is rendered collapsed, separate from the answer.
                agent_footer = pipeline.get_last_agent_footer() if hasattr(pipeline, "get_last_agent_footer") else ""
                if agent_footer:
                    with st.expander("Agent 观测", expanded=False):
                        st.markdown(agent_footer)
                token_usage_summary = (
                    pipeline.get_last_token_usage_summary()
                    if hasattr(pipeline, "get_last_token_usage_summary")
                    else None
                )
                token_usage_footer = format_token_usage_summary(token_usage_summary)
                if token_usage_footer:
                    with st.expander("Token 使用量", expanded=False):
                        st.markdown(token_usage_footer)

        persisted_response = full_response
        footer_sections = [section.strip() for section in (agent_footer, token_usage_footer) if section and section.strip()]
        if footer_sections:
            persisted_response = "\n---\n".join([full_response.rstrip(), *footer_sections])

        st.session_state.messages.append({"role": "assistant", "content": persisted_response})
        assistant_message = persist_chat_message("assistant", persisted_response)
        latency_ms = int((time.perf_counter() - query_started_at) * 1000)
        query_status, query_error_message = query_trace_status(full_response)
        retrieval_summary = (
            pipeline.get_last_retrieval_summary()
            if hasattr(pipeline, "get_last_retrieval_summary")
            else {}
        )
        rewritten_queries = retrieval_summary.get("rewritten_queries") or []
        rewritten_query = " | ".join(rewritten_queries)[:500] if rewritten_queries else ""
        log_service = init_log_service()
        trace_id = log_service.record_query_trace(
            user=current_auth_user(),
            kb_name=st.session_state.current_kb,
            original_query=agent_query,
            chat_session_id=st.session_state.get("chat_session_id"),
            user_message_id=st.session_state.get("pending_user_message_id"),
            assistant_message_id=assistant_message.id if assistant_message else None,
            rewritten_query=rewritten_query,
            backend="ragflow",
            retriever_type=retrieval_summary.get("retriever_type") or "",
            final_top_k=retrieval_summary.get("final_top_k"),
            latency_ms=latency_ms,
            status=query_status,
            error_message=query_error_message,
            metadata=build_query_log_metadata(),
        )
        try:
            log_service.record_retrieved_evidence(trace_id, retrieval_summary.get("evidence") or [])
        except Exception as evidence_error:
            # 证据落库失败不应影响主流程与已写入的 trace。
            from src.core.logger import log as _log
            _log(f"retrieved_evidence logging failed for trace {trace_id}: {evidence_error}")
        st.session_state.pending_user_message_id = None
        st.rerun()

    prompt = st.chat_input("请输入问题...")
    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt})
        user_message = persist_chat_message("user", prompt)
        st.session_state.pending_user_message_id = user_message.id if user_message else None
        st.rerun()
def render_department_management_tab():
    st.markdown('<div style="height: 30px;"></div>', unsafe_allow_html=True)
    st.subheader("👥 部门管理")
    current_user = current_auth_user()
    if current_user is None:
        st.error("无法获取当前用户")
        return

    auth_service = init_auth_service()
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
                c1, c2, c3, c4 = st.columns([1.6, 0.8, 0.8, 1.2])
                with c1:
                    st.markdown(f"`{user.username}`")
                with c2:
                    st.caption("启用" if user.is_active else "停用")
                with c3:
                    next_active = not user.is_active
                    button_label = "启用" if next_active else "停用"
                    if st.button(button_label, key=f"dept_user_toggle_{user.id}", width="stretch"):
                        try:
                            auth_service.set_user_active_as(current_user, user.id, next_active)
                            st.success(f"已{button_label}: {user.username}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"操作失败: {e}")
                with c4:
                    with st.popover("重置密码", use_container_width=True):
                        new_password = st.text_input(
                            "新密码",
                            type="password",
                            key=f"dept_reset_pwd_{user.id}",
                        )
                        if st.button("确认重置", key=f"dept_reset_pwd_btn_{user.id}", use_container_width=True):
                            try:
                                auth_service.reset_user_password_as(current_user, user.id, new_password)
                                st.success(f"已重置密码: {user.username}")
                                st.rerun()
                            except Exception as e:
                                st.error(f"重置失败: {e}")

        st.divider()
        with st.form("dept_create_user_form"):
            st.markdown("##### 创建普通用户")
            new_username = st.text_input("用户名", key="dept_auth_new_username")
            new_password = st.text_input("密码", type="password", key="dept_auth_new_password")
            if st.form_submit_button("创建用户", width="stretch"):
                try:
                    auth_service.create_user_as(
                        current_user,
                        new_username,
                        new_password,
                        ROLE_USER,
                        department_id=current_user.department_id,
                    )
                    st.success(f"已创建普通用户: {new_username}")
                    st.rerun()
                except Exception as e:
                    st.error(f"创建用户失败: {e}")
        return

    if current_user.role == ROLE_SYSTEM_ADMIN:
        render_system_department_management(current_user)
    else:
        st.error("当前账号无权访问部门管理")


def render_system_department_management(current_user):
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
                    c1, c2, c3, c4, c5 = st.columns([1.4, 1.0, 0.7, 0.8, 1.1])
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
                                    auth_service.set_user_active_as(current_user, user.id, next_active)
                                    st.success(f"已{button_label}: {user.username}")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"操作失败: {e}")
                    with c5:
                        if user.id == st.session_state.get("user_id"):
                            st.button("重置密码", key=f"reset_self_{user.id}", disabled=True, width="stretch")
                        else:
                            with st.popover("重置密码", use_container_width=True):
                                new_password = st.text_input(
                                    "新密码",
                                    type="password",
                                    key=f"sys_reset_pwd_{user.id}",
                                )
                                if st.button("确认重置", key=f"sys_reset_pwd_btn_{user.id}", use_container_width=True):
                                    try:
                                        auth_service.reset_user_password_as(current_user, user.id, new_password)
                                        st.success(f"已重置密码: {user.username}")
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"重置失败: {e}")

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
                            auth_service.delete_department_as(current_user, dept.id)
                            st.success(f"已删除部门: {dept.name}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"删除部门失败: {e}")

    with create_tab:
        col_dept, col_user = st.columns(2)
        with col_dept:
            with st.form("create_department_form"):
                st.markdown("###### 创建部门")
                new_department_name = st.text_input("新部门名称", key="auth_new_department_name")
                if st.form_submit_button("创建部门", width="stretch"):
                    try:
                        auth_service.create_department_as(current_user, new_department_name)
                        st.success(f"已创建部门: {new_department_name}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"创建部门失败: {e}")

        with col_user:
            with st.form("create_user_form"):
                st.markdown("###### 创建用户")
                new_username = st.text_input("新用户名", key="auth_new_username")
                new_password = st.text_input("新用户密码", type="password", key="auth_new_password")
                new_role = st.selectbox("角色", [ROLE_DEPT_ADMIN, ROLE_SYSTEM_ADMIN], key="auth_new_role")
                business_departments = [dept for dept in departments if dept.name != "system"]
                selected_department = "system"
                if new_role == ROLE_SYSTEM_ADMIN:
                    st.caption("系统管理员固定归属 system 部门，不挂载业务部门。")
                    department_id = next((dept.id for dept in departments if dept.name == "system"), None)
                else:
                    department_names = [dept.name for dept in business_departments]
                    selected_department = st.selectbox("部门", department_names, key="auth_new_department")
                    department_id = next((dept.id for dept in business_departments if dept.name == selected_department), None)
                if st.form_submit_button("创建用户", width="stretch"):
                    try:
                        auth_service.create_user_as(
                            current_user,
                            new_username,
                            new_password,
                            new_role,
                            department_id=department_id,
                        )
                        st.success(f"已创建用户: {new_username}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"创建用户失败: {e}")
def render_kb_governance_tab(pipeline):
    st.markdown('<div style="height: 30px;"></div>', unsafe_allow_html=True)
    st.subheader("知识库治理")
    st.caption("系统管理员用于查看全局资产、归属、授权和异常状态；不展示文档正文。")

    auth_service = init_auth_service()
    existing_kbs = pipeline.list_all_knowledge_bases_for_admin(ctx=build_request_context(st.session_state)) if pipeline else []
    if not pipeline:
        st.warning("RAGFlow 后端未初始化，当前无法读取知识库治理信息。")
    summaries = auth_service.list_knowledge_base_summaries(existing_kbs)
    ctx = build_request_context(st.session_state)
    pipeline_stats = _pipeline_governance_stats(ctx)

    rows = []
    issue_count = 0
    for item in summaries:
        backend_stats = pipeline_stats.get(_kb_identity_stats_key(item.kb_id, item.department_id, item.name))
        if backend_stats is None:
            backend_stats = pipeline_stats.get(item.name, {})
        file_count = backend_stats.get("files", 0)
        failed_count = backend_stats.get("failed", 0)
        parsing_count = backend_stats.get("parsing", 0)
        issues = []
        if not item.registered:
            issues.append("未登记")
        if not item.department_id:
            issues.append("未分配部门")
        if item.department_id and item.dept_admin_count == 0:
            issues.append("无部门管理员")
        if failed_count:
            issues.append(f"解析失败 {failed_count}")
        if item.permission_count == 0:
            issues.append("未授权")
        if issues:
            issue_count += 1
        rows.append(
            {
                "知识库": item.name,
                "部门": item.department_name or "未分配",
                "负责人": item.owner_username or "-",
                "文件数": file_count,
                "解析中": parsing_count,
                "失败": failed_count,
                "授权数": item.permission_count,
                "登记": "是" if item.registered else "否",
                "问题": "；".join(issues) if issues else "-",
            }
        )

    c1, c2, c3 = st.columns(3)
    c1.metric("知识库", len(summaries))
    c2.metric("异常", issue_count)
    c3.metric("未分配部门", sum(1 for item in summaries if not item.department_id))

    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    else:
        st.info("暂无知识库治理数据")
def render_kb_management_tab(pipeline):
    st.markdown('<div style="height: 30px;"></div>', unsafe_allow_html=True)
    st.subheader("📚 知识库管理")
    manageable_kbs = get_manageable_kbs(pipeline)
    can_create_kb = st.session_state.get("role") == ROLE_DEPT_ADMIN
    upload_types = ["pdf", "doc", "docx", "xls", "xlsx", "edf", "edif"]

    if not st.session_state.current_kb and not manageable_kbs:
        st.info("暂无可用知识库，请联系本部门管理员创建或授权。")
        st.markdown("##### 📁 知识库列表")
        if can_create_kb and st.button("➕ 新建"):
            st.session_state.show_create_kb = True
        if st.session_state.show_create_kb:
            with st.container(border=True):
                st.markdown("###### 新建知识库")
                with st.form("new_kb_form_empty"):
                    st.text_input("输入新知识库名称", placeholder="例如: project_alpha", key="new_kb_name_input")
                    st.form_submit_button("确认创建", on_click=create_kb_callback, args=(pipeline,))
                if st.session_state.get("create_kb_error"):
                    st.error(st.session_state.create_kb_error)
                if st.button("取消", key="cancel_create_kb_empty"):
                    st.session_state.show_create_kb = False
                    st.session_state.create_kb_error = None
                    st.rerun()
        return

    if has_current_kb_permission("write"):
        with st.container(border=True):
            st.markdown("##### 📤 当前知识库上传文档")
            source_group = st.selectbox(
                "文件类型",
                USER_SELECTABLE_SOURCE_GROUPS,
                format_func=lambda group: f"{display_source_group(group)}（{SOURCE_GROUP_DESCRIPTIONS.get(group, '')}）",
                key="upload_source_group",
            )
            files = st.file_uploader("拖拽文件到此处", accept_multiple_files=True, type=upload_types)
            if files and st.button("开始上传", type="primary"):
                with st.status("处理中...", expanded=True) as status:
                    st.write("保存临时文件...")
                    progress_bar = st.progress(0, text="等待文件处理进度")

                    def update_upload_progress(progress: int, stage: str):
                        safe_progress = max(0, min(100, int(progress)))
                        progress_bar.progress(safe_progress, text=stage or f"文件处理进度 {safe_progress}%")
                        status.update(label=f"文件处理中 {safe_progress}%", state="running", expanded=True)

                    st.write("按文件类型分发到对应处理管道...")
                    ctx = build_request_context(st.session_state)
                    result = pipeline.upload_files(
                        files,
                        st.session_state.current_kb,
                        ctx=ctx,
                        source_group=source_group,
                        progress_callback=update_upload_progress,
                    )
                    result_message = result.to_message()
                    result_summary = result_message.split("\n")[0] if result_message else ""
                    upload_ok = result.ok
                    upload_partial = result.partial
                    invalidate_file_cache(st.session_state.current_kb)
                    _clear_backend_task_cache("kb_mgmt", st.session_state.current_kb, ctx)
                    progress_bar.progress(100, text="文件处理流程完成")
                    if upload_ok:
                        status.update(label="✅ 文件处理流程完成", state="complete", expanded=False)
                    elif upload_partial:
                        status.update(label="⚠️ 文件部分处理完成", state="complete", expanded=True)
                    else:
                        status.update(label="❌ 文件未处理成功", state="error", expanded=True)
                if upload_ok:
                    st.success(result_summary)
                elif upload_partial:
                    st.warning(result_summary)
                    st.text(result_message)
                else:
                    st.error(result_summary or "文件处理失败")
                    if result_message:
                        st.text(result_message)
                time.sleep(1)
                st.rerun()
        st.divider()

        render_parse_task_panel(pipeline, st.session_state.current_kb, key_prefix="kb_mgmt")
        st.divider()
    elif st.session_state.current_kb:
        st.info("当前账号没有该知识库的内容上传权限。")
        st.divider()

    st.markdown("##### 📁 知识库列表")
    col_kbs, col_new = st.columns([9, 1])
    with col_kbs:
        st.caption(f"共有 {len(manageable_kbs)} 个知识库")
    with col_new:
        if can_create_kb and st.button("➕ 新建"):
            st.session_state.show_create_kb = True

    if st.session_state.show_create_kb:
        with st.container(border=True):
            st.markdown("###### 新建知识库")
            with st.form("new_kb_form"):
                st.text_input("输入新知识库名称", placeholder="例如: project_alpha", key="new_kb_name_input")
                st.form_submit_button("确认创建", on_click=create_kb_callback, args=(pipeline,))
            if st.session_state.get("create_kb_error"):
                st.error(st.session_state.create_kb_error)
            if st.button("取消", key="cancel_create_kb"):
                st.session_state.show_create_kb = False
                st.session_state.create_kb_error = None
                st.rerun()

    if st.session_state.get("role") == ROLE_DEPT_ADMIN and manageable_kbs:
        with st.expander("🔑 知识库访问授权"):
            auth_service = init_auth_service()
            manager = auth_service.get_user_by_username(st.session_state.get("username"))
            users = auth_service.list_users_for_manager(manager) if manager else []
            users = [user for user in users if user.role != ROLE_SYSTEM_ADMIN]
            if st.session_state.get("role") == ROLE_DEPT_ADMIN:
                users = [user for user in users if user.role == ROLE_USER]
            if not users:
                st.info("暂无可授权用户")
            else:
                with st.form("grant_kb_permission_form"):
                    grant_kb = st.selectbox("知识库", manageable_kbs, key="grant_kb_name")
                    user_labels = [f"{user.username} ({user.role})" for user in users]
                    selected_user_labels = st.multiselect("用户", user_labels, key="grant_user_labels")
                    permission = st.selectbox("权限", ["read", "write", "admin"], key="grant_permission")
                    if st.form_submit_button("授权"):
                        if not selected_user_labels:
                            st.warning("请至少选择一个用户。")
                        else:
                            selected_users = [
                                users[user_labels.index(label)]
                                for label in selected_user_labels
                            ]
                            success_users = []
                            failed_messages = []
                            for target_user in selected_users:
                                try:
                                    auth_service.grant_kb_permission_as(manager, grant_kb, target_user.id, permission)
                                    success_users.append(target_user.username)
                                except Exception as e:
                                    failed_messages.append(f"{target_user.username}: {e}")
                            if success_users:
                                st.success(f"已授权 {len(success_users)} 个用户访问 {grant_kb}: {permission}")
                            if failed_messages:
                                st.error("授权失败: " + "；".join(failed_messages))
                            if success_users and not failed_messages:
                                st.rerun()

    for kb in manageable_kbs:
        ctx = build_request_context(st.session_state)
        can_read_kb = ctx.has_kb_permission(kb, "read")
        can_write_kb = ctx.has_kb_permission(kb, "write")
        can_admin_kb = ctx.has_kb_permission(kb, "admin")
        file_infos = get_cached_file_infos(pipeline, kb, ctx) if can_read_kb else []
        files = ([info.name for info in file_infos] or get_cached_files(pipeline, kb)) if can_read_kb else []
        file_info_by_id = {info.id: info for info in file_infos}
        is_current = (kb == st.session_state.current_kb)
        with st.expander(f"{'🟢' if is_current else '⚪'} {kb} ({len(files)} 文件)", expanded=is_current):
            if not can_read_kb:
                st.caption("仅系统管理可见；当前账号没有内容读取权限。")
            elif files:
                render_excel_ledger_panel(file_infos, f"mgmt_{kb}")
                st.markdown("**📄 文件列表:**")
                container_kwargs = {"border": True}
                if len(files) > 5:
                    container_kwargs["height"] = 300
                selected_doc_id = st.session_state.get(_selected_parse_result_key(f"mgmt_{kb}"))
                if selected_doc_id not in file_info_by_id and file_info_by_id:
                    selected_doc_id = None
                with st.container(**container_kwargs):
                    rows = file_infos if file_infos else files
                    for file_index, item in enumerate(rows):
                        if hasattr(item, "id"):
                            doc_id = item.id
                            f = item.name
                            info = item
                        else:
                            doc_id = item
                            f = item
                            info = None
                        c1, c2, c3 = st.columns([0.68, 0.16, 0.16])
                        with c1:
                            processor_kind = info.metadata.get("processor_kind", "") if info else ""
                            if processor_kind == "spreadsheet_table":
                                local_path = info.metadata.get("local_path", "")
                                st.markdown(f"📊 {f}  \n`Excel 管道: 已归档` `待结构化解析`")
                                if local_path:
                                    st.caption(f"本地归档: {local_path}")
                                container_warning = format_container_inspection_warning(info.metadata)
                                if container_warning:
                                    st.caption(container_warning)
                            elif info and info.metadata.get("ragflow_document_id"):
                                status = str(info.metadata.get("status", "unknown")).lower()
                                status_label, searchability_label = format_ragflow_document_status(status)
                                dataset_kind = info.metadata.get("dataset_kind", "")
                                local_path = info.metadata.get("local_path", "")
                                ragflow_error = info.metadata.get("ragflow_error", "")
                                ragflow_status_note = info.metadata.get("ragflow_status_note", "")
                                st.markdown(f"📄 {f}  \n`RAGFlow: {status_label}` `{searchability_label}` `{dataset_kind}`")
                                if local_path:
                                    st.caption(f"本地归档: {local_path}")
                                if status == "cancelled":
                                    st.caption("解析已停止，当前不会进入检索结果。可删除后重新上传解析。")
                                if ragflow_error:
                                    st.caption(f"RAGFlow 错误: {ragflow_error}")
                                elif ragflow_status_note:
                                    st.caption("RAGFlow 状态暂不可读，解析任务已提交。")
                                container_warning = format_container_inspection_warning(info.metadata)
                                if container_warning:
                                    st.caption(container_warning)
                            else:
                                st.markdown(f"📄 {f}")
                        with c2:
                            chunk_label = "收起" if selected_doc_id == doc_id else "分块"
                            info_status = str(info.metadata.get("status", "")).lower() if info else ""
                            info_processor = info.metadata.get("processor_kind", "") if info else ""
                            chunk_disabled = bool(info_processor == "spreadsheet_table" or info_status in {"cancelled", "failed", "uploaded", "parsing"})
                            chunk_help = "Excel 文件已进入独立表格管道，当前不展示 RAG 分块" if info_processor == "spreadsheet_table" else ("当前文档尚不可检索，没有可展示分块" if chunk_disabled else None)
                            if st.button(chunk_label, key=f"chunks_f_{kb}_{file_index}", width="stretch", disabled=chunk_disabled, help=chunk_help):
                                toggle_parse_result_file(f"mgmt_{kb}", doc_id)
                                st.rerun()
                        with c3:
                            current_confirm = st.session_state.confirm_delete_file
                            is_confirming = (current_confirm == (kb, doc_id))
                            if not can_write_kb:
                                st.button("🗑️", key=f"del_f_{kb}_{file_index}", help="无删除权限", disabled=True)
                            elif is_confirming:
                                sub_c1, sub_c2 = st.columns([1, 1])
                                with sub_c1:
                                    if st.button("✓", key=f"yes_f_{kb}_{file_index}", help="确认删除"):
                                        with st.spinner("删除中..."):
                                            ctx = build_request_context(st.session_state)
                                            res = pipeline.delete_document(doc_id, kb, ctx=ctx)
                                            delete_ok = not str(res).startswith(("Error:", "系统错误:", "删除失败"))
                                            invalidate_file_cache(kb)
                                            st.session_state.confirm_delete_file = None
                                            if st.session_state.get(_selected_parse_result_key(f"mgmt_{kb}")) == doc_id:
                                                st.session_state[_selected_parse_result_key(f"mgmt_{kb}")] = None
                                            if delete_ok:
                                                st.session_state.toast_msg = f"已删除: {f}"
                                            else:
                                                st.session_state.error_msg = str(res)
                                            st.rerun()
                                with sub_c2:
                                    if st.button("✗", key=f"no_f_{kb}_{file_index}", help="取消"):
                                        st.session_state.confirm_delete_file = None
                                        st.rerun()
                            else:
                                if st.button("🗑️", key=f"del_f_{kb}_{file_index}", help="删除文件"):
                                    st.session_state.confirm_delete_file = (kb, doc_id)
                                    st.rerun()
                if selected_doc_id:
                    st.divider()
                    selected_info = file_info_by_id.get(selected_doc_id)
                    render_parse_result_detail(
                        pipeline,
                        kb,
                        selected_doc_id,
                        selected_info.name if selected_info else selected_doc_id,
                    )
            else:
                st.caption("暂无文件")

            st.divider()
            col_switch, col_del = st.columns([1, 1])
            with col_switch:
                if not can_read_kb:
                    st.button("🔄 切换到此知识库", disabled=True, key=f"btn_no_read_{kb}", help="无内容检索权限")
                elif not is_current:
                    st.button("🔄 切换到此知识库", key=f"btn_switch_{kb}", on_click=switch_kb_callback, args=(kb,))
                else:
                    st.button("✅ 当前使用中", disabled=True, key=f"btn_cur_{kb}")
            with col_del:
                if not can_admin_kb:
                    st.button("🗑️ 删除整个库", disabled=True, key=f"del_kb_disabled_{kb}", help="无知识库 admin 权限")
                elif st.session_state.confirm_delete_kb == kb:
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


def main():
    init_session_state()
    refresh_auth_state()

    if not st.session_state.authenticated:
        render_login_page()
        return

    pipeline, pipeline_error = init_pipeline()

    if st.session_state.toast_msg:
        st.toast(st.session_state.toast_msg)
        st.session_state.toast_msg = None
        time.sleep(0.5)

    if st.session_state.error_msg:
        st.error(st.session_state.error_msg)
        st.session_state.error_msg = None

    if pipeline_error:
        st.error(f"❌ 系统初始化失败: {pipeline_error}")
    elif pipeline:
        build_request_context(st.session_state)
        refresh_kb_list(pipeline)
        if st.session_state.kb_list and st.session_state.current_kb not in st.session_state.kb_list:
            set_current_kb(st.session_state.kb_list[0])
        elif not st.session_state.kb_list:
            set_current_kb(None)

    # ------------------ 顶部栏 (应用更稳健的 CSS Sticky 效果) ------------------
    with st.container():
        model_ok = True
        backend_ok = pipeline is not None
        backend_label = "RAGFlow"
        st.markdown(f"""
            <style>
                div[data-testid="stVerticalBlock"] > div:has(.app-header-shell) {{
                    position: sticky;
                    top: 2.75rem;
                    z-index: 50;
                    background-color: white;
                    border-bottom: 1px solid #f0f2f6;
                }}
                .app-header-shell {{
                    background-color: white;
                    padding: 0.75rem 0 0.65rem;
                    overflow: visible;
                }}
                .app-header-row {{
                    display: flex;
                    align-items: flex-start;
                    justify-content: space-between;
                    gap: 24px;
                    width: 100%;
                }}
                .app-header-title {{
                    font-size: 35px;
                    margin-top: 0;
                    margin-bottom: 0;
                    line-height: 1.2;
                }}
                .app-header-status {{
                    flex: 0 0 auto;
                    min-width: 96px;
                    padding-top: 4px;
                    text-align: right;
                    white-space: nowrap;
                    line-height: 1.7;
                }}
                @media (max-width: 640px) {{
                    .app-header-row {{
                        align-items: flex-start;
                        flex-direction: column;
                        gap: 4px;
                    }}
                    .app-header-status {{
                        padding-top: 0;
                        text-align: left;
                    }}
                }}
            </style>
            <div class="app-header-shell">
                <div class="app-header-row">
                    <h1 class="app-header-title">😺 Hardware DataBase</h1>
                    <div class="app-header-status">
                        <span class="status-indicator {'status-ok' if model_ok else 'status-error'}"></span> AI模型<br>
                        <span class="status-indicator {'status-ok' if backend_ok else 'status-error'}"></span> {backend_label}
                    </div>
                </div>
            </div>
        """, unsafe_allow_html=True)

    # ------------------ 侧边栏 ------------------
    with st.sidebar:
        st.markdown('<h2 class="sidebar-main-title">😼 Hardware DataBase导航</h2>', unsafe_allow_html=True)
        user_label = st.session_state.username or "anonymous"
        role_label = st.session_state.role or "anonymous"
        st.caption(f"当前用户: {user_label} / {role_label}")
        if st.button("退出登录", width="stretch"):
            logout()
        st.divider()

        role = st.session_state.get("role")
        if role == ROLE_SYSTEM_ADMIN:
            tab_options = ["🧭 知识库治理", "👥 部门管理", "📊 日志中心", "🧪 RAGAS 评估", "⚙️ 系统配置"]
        elif role == ROLE_DEPT_ADMIN:
            tab_options = ["💬 智能对话", "📝 文档生成", "👥 部门管理", "📚 知识库管理", "📊 日志中心"]
        else:
            tab_options = ["💬 智能对话", "📝 文档生成"]

        selected_tab = st.radio("**🚩 功能切换:**", tab_options, label_visibility="collapsed")
        st.divider()

        # 设置页面：侧边栏显示当前配置概览
        if selected_tab == "⚙️ 系统配置":
            if role != ROLE_SYSTEM_ADMIN:
                st.error("当前账号无权访问系统配置")
                st.stop()
            provider_value = (
                config.settings.AGENT_LLM_PROVIDER.value
                if isinstance(config.settings.AGENT_LLM_PROVIDER, config.settings.Provider)
                else str(config.settings.AGENT_LLM_PROVIDER)
            )
            st.markdown("**📍 当前模型配置:**")
            st.markdown(f"- **Provider:** `{provider_value}`")
            if provider_value == "ollama":
                st.markdown(f"- **LLM:** `{config.settings.AGENT_OLLAMA_MODEL}`")
            else:
                st.markdown(f"- **LLM:** `{config.settings.AGENT_CUSTOM_MODEL}`")
                st.markdown(f"- **Base URL:** `{config.settings.AGENT_CUSTOM_BASE_URL}`")

            st.divider()
            st.markdown("**📍 当前 RAG 后端:**")
            st.markdown("- **Backend:** `ragflow`")
            st.caption("RAGFlow 是正式解析、索引、检索主线。")
            st.markdown(f"- **Base URL:** `{config.settings.RAGFLOW_BASE_URL}`")
            st.markdown(f"- **Governance Dataset:** `{config.settings.RAGFLOW_GOVERNANCE_DATASET_NAME}`")
            st.markdown(f"- **Design Dataset:** `{config.settings.RAGFLOW_DESIGN_DATASET_NAME}`")
            st.markdown(f"- **Similarity:** {config.settings.RAGFLOW_SIMILARITY_THRESHOLD}")
            st.markdown(f"- **Vector Weight:** {config.settings.RAGFLOW_VECTOR_WEIGHT}")
        elif selected_tab in {"💬 智能对话", "📚 知识库管理"}:
            if pipeline is None:
                st.warning("⚠️ 系统未初始化，请先在 ⚙️ 系统配置 中检查并修复配置")
                st.stop()

            st.markdown("**📍 当前对话挂载知识库:**")
            if not st.session_state.kb_list:
                set_current_kb(None)
                if "kb_selector" in st.session_state:
                    del st.session_state["kb_selector"]
                st.info("暂无可用知识库，请先创建知识库或联系管理员授权。")
            else:
                if st.session_state.current_kb not in st.session_state.kb_list:
                    set_current_kb(st.session_state.kb_list[0])
                if st.session_state.get("kb_selector") not in st.session_state.kb_list:
                    st.session_state.kb_selector = st.session_state.current_kb
                selected_kb = st.selectbox("切换知识库", options=st.session_state.kb_list, key="kb_selector")
                if selected_kb != st.session_state.current_kb:
                    set_current_kb(selected_kb)
                    reset_chat_state()
                    st.session_state.confirm_delete_file = None
                    st.rerun()

                kb_files = get_cached_files(pipeline, st.session_state.current_kb)
                st.info(f"当前库包含 {len(kb_files)} 个文件")
                if kb_files:
                    with st.expander("📚 查看库内文档"):
                        render_compact_document_list(kb_files)

            if selected_tab == "💬 智能对话":
                if not has_current_kb_permission("read"):
                    st.info("当前账号没有该知识库的内容检索权限。")
                else:
                    ensure_current_chat_session()
                    if st.button("➕ 新建对话", width="stretch", type="secondary"):
                        start_new_chat_session()
                        st.rerun()

                if has_current_kb_permission("read") and st.session_state.get("user_id"):
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
        elif selected_tab == "📊 日志中心":
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
            - 删除文件会同时移除索引和物理文件。
            """)
        elif selected_tab == "🧭 知识库治理":
            st.warning("""
            **知识库治理:**
            - 查看全局知识库资产、归属、授权和解析状态。
            - 不进入知识库对话，不展示文档正文或分块正文。
            - 需要测试内容效果时，请创建测试部门和测试部门管理员。
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
        elif selected_tab == "📝 文档生成":
            st.warning("""
            **文档生成：**
            - 仅使用已授权项目、已批准基线和冻结来源集。
            - 候选产物不等于正式发布；人工审批将绑定候选内容、验证报告和来源快照。
            - 不会使用当前聊天会话或随意的本地文件路径作为输入。
            """)
        st.divider()
        st.caption("© 2025 Hardware DataBase Assistant")

    # ------------------ 页面内容分发 ------------------
    if pipeline is None:
        st.error(f"应用后端未初始化: {pipeline_error}")
        st.info("请先进入系统配置检查 RAGFlow 与 Agent 模型配置。")
        return

    if selected_tab == "💬 智能对话":
        if st.session_state.get("role") == ROLE_SYSTEM_ADMIN:
            st.error("系统管理员不使用知识库对话，请使用测试部门管理员账号进行功能测试。")
            st.stop()
        render_chat_tab(pipeline)
    elif selected_tab == "📝 文档生成":
        if st.session_state.get("role") == ROLE_SYSTEM_ADMIN:
            st.error("系统管理员没有项目正文的默认访问权限；请使用项目成员账号。")
            st.stop()
        render_document_generation_page(st, pipeline, build_request_context(st.session_state))
    elif selected_tab == "🧭 知识库治理":
        if st.session_state.get("role") != ROLE_SYSTEM_ADMIN:
            st.error("当前账号无权访问知识库治理")
            st.stop()
        render_kb_governance_tab(pipeline)
    elif selected_tab == "📚 知识库管理":
        if st.session_state.get("role") != ROLE_DEPT_ADMIN:
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
    elif selected_tab == "🧪 RAGAS 评估":
        if st.session_state.get("role") != ROLE_SYSTEM_ADMIN:
            st.error("当前账号无权访问 RAGAS 评估")
            st.stop()
        render_evaluation_page(st.session_state.get("role"))
    elif selected_tab == "⚙️ 系统配置":
        if st.session_state.get("role") != ROLE_SYSTEM_ADMIN:
            st.error("当前账号无权访问系统配置")
            st.stop()
        render_settings_tab()


if __name__ == "__main__":
    main()
