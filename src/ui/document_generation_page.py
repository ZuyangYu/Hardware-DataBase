"""Governed Streamlit workflow for durable document-authoring work orders."""

from __future__ import annotations

import uuid
from typing import Any


_TIMELINE_STAGES = ("排队", "检索", "撰写", "校验", "渲染", "人工审批")
_NODE_STAGES = {
    "initialize": "排队", "authoring_graph": "检索",
    "create_information_requirements": "检索", "retrieve_requirement_evidence": "检索",
    "draft_ready_unit": "撰写", "validate_unit_draft": "校验",
    "detect_template_contamination": "校验", "validate_cross_unit": "校验",
    "complete": "人工审批",
}
_ORDER_STAGES = {
    "planned": "排队", "retrieving": "检索", "ready_to_draft": "撰写", "drafting": "撰写",
    "validating": "校验", "waiting_human_input": "人工审批", "waiting_human_approval": "人工审批",
    "ready_to_render": "渲染", "rendering": "渲染", "blocked": "校验",
}


def _run_timeline(status: dict) -> list[tuple[str, str]]:
    """Map persisted WorkOrder/HarnessRun state to a compact UI timeline."""
    harness_run = status.get("harness_run") or {}
    node = harness_run.get("current_node")
    if status.get("status") == "complete":
        result = [(stage, "done") for stage in _TIMELINE_STAGES]
    else:
        active_stage = _NODE_STAGES.get(node) or _ORDER_STAGES.get(status.get("status"), "排队")
        active_index = _TIMELINE_STAGES.index(active_stage)
        result = [
            (stage, "done" if index < active_index else "active" if index == active_index else "pending")
            for index, stage in enumerate(_TIMELINE_STAGES)
        ]
    if harness_run.get("status") == "failed" or node == "failed":
        error = harness_run.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        result.append((f"失败：{message or '未知错误'}", "error"))
    elif status.get("status") == "cancelled":
        result.append(("已取消", "error"))
    elif status.get("status") == "blocked":
        result.append(("已阻塞", "error"))
    return result


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _safe_analysis_summary(analysis: Any) -> list[dict[str, Any]]:
    """Expose labels and writeability, never OOXML locators or uploaded bytes."""
    return [
        {
            "单元": _value(unit, "label") or _value(unit, "unit_id"),
            "可写": _value(unit, "writable", False),
            "限制": _value(unit, "blocked_reason") or "",
        }
        for unit in _value(analysis, "units", [])
    ]


def _matching_schemas(template: Any, schemas: list[Any]) -> dict[str, Any]:
    """Return schemas bound to both immutable template schema id and version."""
    schema_id = _value(template, "template_schema_id")
    schema_version = _value(template, "template_schema_version")
    return {
        f"{_value(schema, 'document_schema_id')}@{_value(schema, 'version')}": schema
        for schema in schemas
        if _value(schema, "document_schema_id") == schema_id and _value(schema, "version") == schema_version
    }


def render_document_generation_page(st, pipeline, ctx) -> None:
    st.header("📝 文档生成")
    st.caption("所有任务固定到已授权知识库、模板版本和来源快照；页面不从会话状态启动运行。")
    upload_tab, create_tab, runs_tab = st.tabs(["上传模板", "新建生成任务", "任务与下载"])
    with upload_tab:
        _render_template_upload(st, pipeline, ctx)
    with create_tab:
        _render_work_order_creation(st, pipeline, ctx)
    with runs_tab:
        _render_durable_runs(st, pipeline, ctx)


def _render_template_upload(st, pipeline, ctx) -> None:
    st.subheader("上传并分析受控模板")
    upload = st.file_uploader("上传模板", type=["xlsx", "xlsm", "docx"], key="document-template-upload")
    display_name = st.text_input("模板名称", key="document-template-name")
    if st.button("分析模板", type="primary", key="analyze-document-template", disabled=upload is None):
        try:
            analysis = pipeline.analyze_document_template(
                ctx, filename=upload.name, content=upload.getvalue(), template_name=display_name or upload.name,
            )
        except (PermissionError, ValueError, KeyError) as exc:
            st.error(f"模板分析失败：{exc}")
        else:
            # Keep only a persisted-analysis reference for confirmation; no run is started from session state.
            st.session_state["document-template-analysis"] = analysis

    analysis = st.session_state.get("document-template-analysis")
    if analysis is None:
        return
    analysis_status = _value(analysis, "status")
    st.caption(f"分析 ID：{_value(analysis, 'analysis_id')}；格式：{_value(analysis, 'format')}")
    try:
        sanitization_summary = pipeline.get_document_template_sanitization_summary(
            ctx, _value(analysis, "template_version_id"),
        )
    except (PermissionError, ValueError, KeyError) as exc:
        st.error(f"无法读取模板净化结果：{exc}")
        return
    if sanitization_summary is not None:
        st.success("已生成无活动内容的安全模板；后续分析与生成仅使用该副本。")
        st.json(sanitization_summary, expanded=False)
    st.dataframe(_safe_analysis_summary(analysis), width="stretch", hide_index=True)
    suggestions = [
        {"语义单元": _value(item, "label") or _value(item, "semantic_unit_id"), "置信度": _value(item, "confidence")}
        for item in _value(analysis, "suggestions", [])
    ]
    if suggestions:
        st.caption("建议绑定（仅展示安全摘要）")
        st.dataframe(suggestions, width="stretch", hide_index=True)
    if analysis_status == "requires_human":
        st.warning("分析发现受限或需要人工核对的区域。请修正模板后重新上传并分析。")
        st.text_area("修正说明", key="document-template-correction-note")
        if st.button("重新分析修正模板", key="reanalyze-document-template", disabled=upload is None):
            try:
                st.session_state["document-template-analysis"] = pipeline.analyze_document_template(
                    ctx, filename=upload.name, content=upload.getvalue(), template_name=display_name or upload.name,
                )
            except (PermissionError, ValueError, KeyError) as exc:
                st.error(f"模板分析失败：{exc}")
        return
    if analysis_status != "ready_for_confirmation":
        st.error("模板分析未完成，不能启用。")
        return
    if st.button("确认并启用模板", type="primary", key="confirm-document-template"):
        try:
            template = pipeline.confirm_document_template(
                ctx, analysis_id=_value(analysis, "analysis_id"),
                display_name=display_name or _value(analysis, "format", "模板"),
            )
        except (PermissionError, ValueError, KeyError) as exc:
            st.error(f"启用模板失败：{exc}")
        else:
            st.success(f"已启用受控模板：{_value(template, 'template_version_id')}")
            st.session_state.pop("document-template-analysis", None)


def _render_work_order_creation(st, pipeline, ctx) -> None:
    st.subheader("新建生成任务")
    try:
        options = pipeline.list_knowledge_base_document_generation_options(ctx)
    except (PermissionError, ValueError, KeyError) as exc:
        st.error(f"无法读取知识库生成配置：{exc}")
        return
    knowledge_bases = options["knowledge_bases"]
    if not knowledge_bases:
        st.info("当前账号没有可用于文档生成的知识库，请联系管理员授权知识库。")
        return
    knowledge_base_name = st.selectbox(
        "已授权知识库", knowledge_bases, key="document-generation-kb",
    )
    templates, schemas = options["templates"], options["schemas"]
    if not templates or not schemas:
        st.warning("需要已批准的模板和 Document Schema 才能创建任务。")
        return
    template_by_id = {_value(item, "template_version_id"): item for item in templates}
    left, right = st.columns(2)
    with left:
        template_id = st.selectbox("受控模板", list(template_by_id), format_func=lambda value: _value(template_by_id[value], "template_id"))
    template = template_by_id[template_id]
    compatible_schemas = _matching_schemas(template, schemas)
    with right:
        if not compatible_schemas:
            st.warning("选中的模板没有已批准的匹配 Schema。")
            return
        schema_key = st.selectbox("Document Schema", list(compatible_schemas))
    schema = compatible_schemas[schema_key]
    if st.button("创建生成任务", type="primary", key="create-document-work-order"):
        try:
            order = pipeline.create_knowledge_base_document_work_order(
                ctx, knowledge_base_name=knowledge_base_name, template_version_id=template_id,
                document_schema_id=_value(schema, "document_schema_id"),
                document_schema_version=_value(schema, "version"), idempotency_key=f"streamlit-kb-{uuid.uuid4().hex}",
            )
        except (PermissionError, ValueError, KeyError) as exc:
            st.error(f"无法创建生成任务：{exc}")
        else:
            st.success(f"已创建工作单：{_value(order, 'work_order_id')}")


def _render_durable_runs(st, pipeline, ctx) -> None:
    st.subheader("任务与下载")
    try:
        options = pipeline.list_knowledge_base_document_generation_options(ctx)
    except (PermissionError, ValueError, KeyError) as exc:
        st.error(f"无法读取知识库：{exc}")
        return
    knowledge_bases = options["knowledge_bases"]
    if not knowledge_bases:
        st.info("当前账号没有可用于文档生成的知识库，请联系管理员授权知识库。")
        return
    knowledge_base_name = st.selectbox(
        "已授权知识库", knowledge_bases, key="document-run-kb",
    )
    try:
        work_orders = pipeline.list_knowledge_base_document_work_orders(ctx, knowledge_base_name)
    except (PermissionError, ValueError, KeyError) as exc:
        st.error(f"无法读取工作单：{exc}")
        return
    if not work_orders:
        st.info("该知识库尚无持久化工作单。")
        return
    work_order_id = st.selectbox("工作单", [_value(order, "work_order_id") for order in work_orders], key="document-generation-work-order")
    status = pipeline.get_document_run_status(work_order_id, ctx)
    if status is None:
        st.warning("未找到该工作单。")
        return
    _render_run_status(st, pipeline, ctx, status)


def _render_run_status(st, pipeline, ctx, status: dict) -> None:
    st.write(" · ".join(f"{label}（{state}）" for label, state in _run_timeline(status)))
    harness_run = status.get("harness_run") or {}
    if harness_run:
        st.caption(
            f"节点：{harness_run.get('current_node') or '-'}；检查点：{harness_run.get('checkpoint_id') or '-'}；"
            f"重试：{harness_run.get('retry_count', 0)}；检索轮次：{harness_run.get('retrieval_round_count', 0)}"
        )
    units = status.get("unit_statuses") or {}
    if units:
        st.dataframe([{"单元": unit_id, "状态": value} for unit_id, value in units.items()], width="stretch", hide_index=True)
    validation = status.get("validation") or {}
    if validation.get("issues"):
        st.warning(f"验证状态：{validation.get('status')}")
        st.dataframe(validation["issues"], width="stretch", hide_index=True)
    if harness_run.get("status") in {"queued", "running", "retrying"}:
        left, right = st.columns(2)
        with left:
            if st.button("暂停 Harness", key=f"pause-harness-{harness_run['run_id']}"):
                _call_run_control(st, pipeline.pause_harness_run, ctx, harness_run["run_id"], "暂停")
        with right:
            if st.button("取消 Harness", key=f"cancel-harness-{harness_run['run_id']}"):
                _call_run_control(st, pipeline.cancel_harness_run, ctx, harness_run["run_id"], "取消")
        refresh = getattr(st, "autorefresh", None)
        if callable(refresh):
            refresh(interval=2000, key=f"document-run-refresh-{status['work_order_id']}")
        else:
            st.button("刷新状态", key=f"refresh-document-run-{status['work_order_id']}")
    for artifact in status.get("artifacts", []):
        artifact_id = artifact["artifact_id"]
        st.write(f"{artifact.get('stage', '产物')}：{artifact_id}")
        _render_artifact_download(st, pipeline, ctx, status, artifact_id)
        if artifact.get("stage") == "review_candidate":
            _render_artifact_preview(st, pipeline, ctx, artifact_id)
            feedback = st.text_input("反馈说明", key=f"feedback-comment-{artifact_id}")
            if st.button("提交反馈", key=f"submit-feedback-{artifact_id}"):
                try:
                    pipeline.submit_document_feedback(ctx, artifact_id, comment=feedback)
                except (PermissionError, ValueError, KeyError) as exc:
                    st.error(f"提交反馈失败：{exc}")
                else:
                    st.success("反馈已保存；候选文件仍未发布。")
            comment = st.text_input("批准说明", key=f"approval-comment-{artifact_id}")
            if st.button("批准并发布", key=f"approve-artifact-{artifact_id}"):
                try:
                    pipeline.approve_document_artifact(ctx, artifact_id, comment=comment)
                except (PermissionError, ValueError, KeyError) as exc:
                    st.error(f"批准失败：{exc}")
                else:
                    st.success("已批准并发布。")


def _call_run_control(st, action, ctx, run_id: str, label: str) -> None:
    try:
        action(ctx, run_id)
    except (PermissionError, ValueError, KeyError) as exc:
        st.error(f"{label}失败：{exc}")
    else:
        st.success(f"已请求{label} Harness。")


def _render_artifact_download(st, pipeline, ctx, status: dict, artifact_id: str) -> None:
    """Read artifact bytes only after an explicit request, never on polling reruns."""
    data_key = f"document-artifact-download-{artifact_id}"
    content = st.session_state.get(data_key)
    if content is None and st.button("准备下载", key=f"prepare-download-{artifact_id}"):
        try:
            content = pipeline.download_document_artifact(ctx, artifact_id)
        except PermissionError:
            st.caption("当前权限不能下载此产物。")
        else:
            st.session_state[data_key] = content
    if content is not None:
        st.download_button(
            "下载", data=content, file_name=f"{artifact_id}.{status.get('target_format', 'bin')}",
            key=f"download-{artifact_id}",
        )


def _render_artifact_preview(st, pipeline, ctx, artifact_id: str) -> None:
    """Load a bounded preview only after an explicit user action."""
    preview_key = f"document-artifact-preview-{artifact_id}"
    preview = st.session_state.get(preview_key)
    if preview is None and st.button("加载预览", key=f"prepare-preview-{artifact_id}"):
        try:
            preview = pipeline.preview_document_artifact(ctx, artifact_id)
        except PermissionError:
            st.caption("当前权限不能预览此产物。")
            return
        except (ValueError, KeyError) as exc:
            st.error(f"加载预览失败：{exc}")
            return
        st.session_state[preview_key] = preview
    if preview is None:
        return
    if preview.get("warnings"):
        for warning in preview["warnings"]:
            st.warning(warning)
    if preview.get("format") in {"xlsx", "xlsm"}:
        for sheet in preview.get("sheets", []):
            st.caption(f"预览工作表：{sheet.get('name', '未命名')}（最多 50 行、12 列）")
            st.dataframe(sheet.get("rows", []), width="stretch", hide_index=True)
    elif preview.get("format") == "docx":
        for paragraph in preview.get("paragraphs", []):
            st.write(paragraph)
        for table in preview.get("tables", []):
            st.dataframe(table, width="stretch", hide_index=True)
    if preview.get("truncated"):
        st.caption("预览已截断；下载候选文件可查看完整内容。")
