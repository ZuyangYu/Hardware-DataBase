# src/streamlit_app.py
import os
import tempfile
import streamlit as st
import time
from src.core.rag_pipeline import RAGPipeline
from src.core.resource_manager import resource_manager
from config.settings import DEFAULT_KB_NAME

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
        pipeline.create_kb(DEFAULT_KB_NAME)
        return pipeline, None
    except Exception as e:
        return None, str(e)


def init_session_state():
    """初始化会话状态"""
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "current_kb" not in st.session_state:
        st.session_state.current_kb = DEFAULT_KB_NAME
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
    if st.session_state.current_kb == kb_name:
        st.session_state.current_kb = DEFAULT_KB_NAME
        st.session_state.kb_selector = DEFAULT_KB_NAME
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


# ==================== 主界面 ====================
def main():
    init_session_state()
    pipeline, error = init_pipeline()

    if error:
        st.error(f"❌ 系统初始化失败: {error}")
        st.stop()

    if st.session_state.toast_msg:
        st.toast(st.session_state.toast_msg)
        st.session_state.toast_msg = None
        time.sleep(0.5)

    if st.session_state.error_msg:
        st.error(st.session_state.error_msg)
        st.session_state.error_msg = None

    if not st.session_state.kb_list:
        refresh_kb_list(pipeline)

    # ------------------ 顶部栏 (应用更稳健的 CSS Sticky 效果) ------------------
    # 使用 st.container 包裹顶部内容，并插入隐藏的 div 用于 CSS 定位
    with st.container():
        st.markdown("""
            <div class="fixed-header-marker"></div>
            <style>
                /* 使用 :has 选择器精确定位头部容器 */
                div[data-testid="stVerticalBlock"] > div:has(div.fixed-header-marker) {
                    position: sticky;
                    top: 0.2rem; /* 预留出 Streamlit 顶部工具栏的高度 */
                    background-color: white;
                    z-index: 999;
                    padding-top: 1rem; /* 在容器内部补偿视觉间距 */
                    padding-bottom: 10px;
                    border-bottom: 1px solid #f0f2f6;
                    margin-top: -2rem; /* 抵消可能的外部间距 */
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

        selected_tab = st.radio("**🚩 功能切换:**", ["💬 智能对话", "📚 知识库管理"], label_visibility="collapsed")
        st.divider()
        st.markdown(f"**📍 当前对话挂载知识库:**")
        if st.session_state.current_kb not in st.session_state.kb_list:
            st.session_state.current_kb = DEFAULT_KB_NAME
            if DEFAULT_KB_NAME not in st.session_state.kb_list:
                st.session_state.kb_list.append(DEFAULT_KB_NAME)

        selected_kb = st.selectbox("切换知识库", options=st.session_state.kb_list, key="kb_selector")
        if selected_kb != st.session_state.current_kb:
            st.session_state.current_kb = selected_kb
            st.session_state.messages = []
            st.session_state.confirm_delete_file = None
            st.rerun()

        kb_files = pipeline.list_files(st.session_state.current_kb)
        st.info(f"当前库包含 {len(kb_files)} 个文件")
        if kb_files:
            with st.expander("📚 查看库内文档"):
                for f in kb_files:
                    st.markdown(f"- 📄 {f}")

        # "清空"按钮的位置
        if selected_tab == "💬 智能对话":
            if st.button("🗑️ 清空对话", use_container_width=True, type="secondary"):
                st.session_state.messages = []
                st.rerun()

        st.divider()
        st.markdown("<h3>🐱‍👓️ 说明与注意事项</h3>", unsafe_allow_html=True)

        st.warning("""
        **1. 文件支持:** 
        - 支持 PDF, TXT, MD, DOCX, CSV, HTML 格式文档。

        **2. 知识库管理:** 
        - **新建**: 点击"知识库管理"页面的"➕ 新建"。 
        - **切换**: 切换知识库会**清空当前对话**。

        **3. 数据安全:** 
        - 删除文件或知识库的操作是**不可恢复**的。 
        - 默认库 `source_documents` 不可被删除。
        """)
        st.divider()
        st.caption("© 2025 HardWare RAG Assistant")

    # ------------------ 页面内容分发 ------------------
    if selected_tab == "💬 智能对话":
        render_chat_tab(pipeline)
    elif selected_tab == "📚 知识库管理":
        render_kb_management_tab(pipeline)


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
                safe_content = content.replace("\n", "<br>")
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

                        with st.expander("📋 复制完整回复内容", expanded=False):
                            st.code(main_text.strip(), language=None)

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

        with st.chat_message("assistant", avatar="😻"):
            first_chunk = None
            error_occured = None

            # --- 关键修改：带有错误处理的思考过程 ---
            with st.spinner("正在思考中..."):
                try:
                    # 获取生成器
                    gen = pipeline.query(user_input_to_process, st.session_state.current_kb, chat_history[-5:])
                    # 尝试获取第一个字符，这会触发实际的检索和推理
                    first_chunk = next(gen)
                except StopIteration:
                    # 生成器正常结束但为空
                    first_chunk = None
                except Exception as e:
                    # 捕获所有其他错误（如连接超时、API错误）
                    error_occured = str(e)

            # --- 根据结果进行输出 ---
            if error_occured:
                st.error(f"❌ 处理请求时发生错误: {error_occured}")
                full_response = f"Error: {error_occured}"
            elif first_chunk is None:
                st.warning("⚠️ AI 未生成任何内容。")
                full_response = "Empty response."
            else:
                # 定义一个帮助函数来重新组合流
                def stream_helper():
                    yield first_chunk  # 先输出刚才拿到的第一个块
                    yield from gen  # 再输出剩下的

                # 使用 write_stream 渲染
                full_response = st.write_stream(stream_helper())

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
                    except:
                        pass
                status.update(label="✅ 完成", state="complete", expanded=False)
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
        files = pipeline.list_files(kb)
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
                if kb != DEFAULT_KB_NAME:
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
