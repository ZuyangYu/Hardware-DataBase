"""Minimal P2a/P2b Streamlit surface for project-bound document work orders."""

from __future__ import annotations


def render_document_generation_page(st, pipeline, ctx) -> None:
    st.header("📝 文档生成")
    st.caption("任务固定到项目、已批准配置基线、模板版本和来源快照；聊天上下文不会参与生成。")
    try:
        projects = pipeline.list_accessible_projects(ctx)
    except PermissionError as exc:
        st.error(str(exc))
        return
    if not projects:
        st.info("当前账号没有可访问的项目。请由项目管理员添加 ProjectPrincipalBinding 后重试。")
        return

    project_by_id = {project.project_id: project for project in projects}
    project_id = st.selectbox(
        "项目",
        options=list(project_by_id),
        format_func=lambda value: f"{project_by_id[value].name} ({value})",
        key="document-generation-project",
    )
    try:
        sources = pipeline.get_project_source_catalog(project_id, ctx)
    except PermissionError:
        sources = []
    with st.expander("冻结前的项目来源目录", expanded=False):
        if not sources:
            st.warning("项目尚无可用来源。任务创建会拒绝未批准的基线、版本或区域策略。")
        else:
            st.dataframe([source.model_dump(mode="json") for source in sources], width="stretch", hide_index=True)

    baselines = pipeline.projects.store.list_baselines(project_id, ctx.tenant_id or "default", approved_only=True)
    templates = pipeline.document_generation.store.list_templates(approved_only=True)
    schemas = pipeline.document_generation.store.list_document_schemas(approved_only=True)
    st.subheader("新建文档工作单")
    if not baselines or not templates or not schemas:
        st.warning("需要先由管理员完成项目基线、模板和 Document Schema 的注册与批准。")
    else:
        baseline_by_id = {baseline.baseline_id: baseline for baseline in baselines}
        template_by_id = {template.template_version_id: template for template in templates}
        schema_by_key = {f"{schema.document_schema_id}@{schema.version}": schema for schema in schemas}
        col1, col2, col3 = st.columns(3)
        with col1:
            baseline_id = st.selectbox("配置基线", list(baseline_by_id), format_func=lambda value: baseline_by_id[value].name)
        with col2:
            template_id = st.selectbox("受控模板", list(template_by_id), format_func=lambda value: template_by_id[value].template_id)
        with col3:
            schema_key = st.selectbox("Document Schema", list(schema_by_key))
        schema = schema_by_key[schema_key]
        harness_policy_id = None
        if schema.execution_mode == "internal_harness":
            policies = pipeline.document_generation.store.list_harness_policies(approved_only=True)
            if not policies:
                st.warning("此 Schema 需要已批准的 Harness Policy，当前无法创建工作单。")
            else:
                policy_by_key = {
                    f"{policy.harness_policy_id}@{policy.version}": policy for policy in policies
                }
                policy_key = st.selectbox("Harness Policy", list(policy_by_key))
                harness_policy_id = policy_by_key[policy_key].harness_policy_id
                st.caption("语义草稿仅可使用冻结来源中的已验证 Evidence；模型不会获得模板原件或任意工具权限。")
        else:
            st.caption("此 Schema 使用确定性工作流，不调用 Writer。")
        idempotency_key = st.text_input("幂等键（可选）", key="document-generation-idempotency")
        if st.button(
            "创建工作单",
            type="primary",
            key="create-document-work-order",
            disabled=schema.execution_mode == "internal_harness" and harness_policy_id is None,
        ):
            try:
                order = pipeline.create_document_work_order(
                    ctx,
                    project_id=project_id,
                    baseline_id=baseline_id,
                    template_version_id=template_id,
                    document_schema_id=schema.document_schema_id,
                    document_schema_version=schema.version,
                    idempotency_key=idempotency_key or None,
                    harness_policy_id=harness_policy_id,
                )
            except (PermissionError, ValueError, KeyError) as exc:
                st.error(f"无法创建工作单：{exc}")
            else:
                st.success(f"已创建工作单：{order.work_order_id}")

    st.subheader("任务状态与产物")
    work_order_id = st.text_input("工作单 ID", key="document-generation-work-order")
    if work_order_id:
        status = pipeline.get_document_run_status(work_order_id, ctx)
        if status is None:
            st.warning("未找到该工作单。")
        else:
            st.json(status)
            harness_run = status.get("harness_run")
            if harness_run and harness_run["status"] in {"queued", "running", "retrying"}:
                control_left, control_right = st.columns(2)
                with control_left:
                    if st.button("暂停 Harness", key=f"pause-harness-{harness_run['run_id']}"):
                        try:
                            pipeline.pause_harness_run(ctx, harness_run["run_id"])
                        except (KeyError, PermissionError, ValueError) as exc:
                            st.error(f"暂停失败：{exc}")
                        else:
                            st.success("已请求暂停；正在运行的节点会在下一次受控状态提交前停止。")
                with control_right:
                    if st.button("取消 Harness", key=f"cancel-harness-{harness_run['run_id']}"):
                        try:
                            pipeline.cancel_harness_run(ctx, harness_run["run_id"])
                        except (KeyError, PermissionError, ValueError) as exc:
                            st.error(f"取消失败：{exc}")
                        else:
                            st.success("已取消 Harness。")
            artifacts = pipeline.document_generation.store.list_artifacts(work_order_id)
            for artifact in artifacts:
                st.write(f"{artifact.stage}: {artifact.artifact_id}")
                try:
                    data = pipeline.download_document_artifact(ctx, artifact.artifact_id)
                except PermissionError:
                    st.caption("当前权限不能下载此产物。")
                    continue
                st.download_button(
                    "下载",
                    data=data,
                    file_name=f"{artifact.artifact_id}.{pipeline.document_generation.store.get_work_order(work_order_id).target_format}",
                    key=f"download-{artifact.artifact_id}",
                )
