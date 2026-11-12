# 通用 Agent：Workspace、Session 与产物

底层 agent 是 **Workspace-first 的通用 agent**：

- **通用**：文件读写、知识库检索、表格 SQL、网表查询都是工具；不预设固定流程。
- **持久**：Workspace 是项目容器，Session 是会话，Turn/Run 是一次执行。
- **知识库是工具**：Workspace 可以绑定 KB，绑定后 `kb_search` 可用；未绑定就是通用工作区。
- **外部扩展**：MCP / Skill 是可选的工具与流程扩展。

## 数据模型

```
workspace
 ├─ kind: general | knowledge
 ├─ kb binding（knowledge workspace 可选）
 ├─ hidden workspace/<id>/
 │   └─ out/              # 交付物；发布后登记为 artifact
 ├─ session 1..N
 │   └─ turn/run 1..N     # 复用现有 turn 队列 / fencing / SSE
 └─ artifacts             # 版本化产物，可下载
```

聊天 turn 通过 `trace_context={"workspace_id": ...}` 关联 session 的 workspace。
普通聊天不创建 task；长期后台任务不在当前产品范围内。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/workspaces` | 创建通用/知识库工作区 |
| GET | `/api/v1/workspaces` | 当前用户的工作区 |
| GET | `/api/v1/workspaces/{id}` | 工作区详情 |
| GET | `/api/v1/workspaces/{id}/sessions` | 工作区下的会话 |
| POST | `/api/v1/conversations` | 新建会话（可带 `workspace_id`） |
| GET | `/api/v1/conversations?workspace_id=...` | 按工作区过滤会话 |
| GET | `/api/v1/conversations/{id}/artifacts` | 会话产物（前端对话页使用） |
| GET | `/api/v1/artifacts/{id}/download` | 按产物 id 下载（不暴露磁盘路径） |
| — | `artifact.turn_id` | 产物归属哪一轮，前端据此挂到 assistant 消息下 |

## 工作区与产物

- 工具：`workspace_list` / `workspace_read` / `workspace_write` /
  `workspace_edit`（唯一匹配保护）/ `workspace_glob` / `workspace_grep`；
- 路径必须是工作区内的相对路径，拒绝 `..`、绝对路径和越界符号链接；
- 普通聊天用 `workspace_publish_artifact(name, content, kind)` 发布交付物；
- 内部仍写入工作区，但回答和前端不暴露 `out/` 或磁盘路径；
- `workspace_artifacts` 按 `session_id + turn_id` 归属，同名文件自动递增版本；
- 下载接口会再次校验产物路径必须在 workspace 目录内。

## 前端

- 产物按 `turn_id` 归属到对应的 assistant 消息，文件名直接显示在消息下方
  （无独立区域/背景），点击下载；
- 聊天「处理过程」只投影实际工具动作；`skill_loaded` / `permission` / `generate`
  属于运行时编排事件，保留在 turn 事件，不进入聊天轨迹；流式中的身份文案统一为
  「正在处理」，不把检索当成独立模式；
- 会话侧栏仍按当前挂载的知识库筛选；workspace UI 是下一阶段。

## 测试

```bash
uv run pytest \
  tests/test_workspace_store.py tests/test_workspace.py \
  tests/test_artifacts_api.py tests/test_agent_runner_stream.py -q
```

实机功能矩阵（需要服务运行）：

```bash
HDB_E2E_USERNAME=aaa_admin HDB_E2E_PASSWORD=... \
uv run python scripts/functional_aaa.py
HDB_E2E_USERNAME=aaa_admin HDB_E2E_PASSWORD=... \
uv run python scripts/functional_skills.py
```

覆盖：精确位号、表格 SQL、资料概览、workspace 产物、产物版本、
产物挂到 assistant 消息、反编造、Skill 加载。结果写入
`HDB_AAA_RESULTS` / `HDB_SKILL_RESULTS`。

前端：

```bash
cd frontend && npm run build
```

## MCP 桥

外部 agent（dcode/OpenCode/Claude Code）可以通过 MCP 使用平台：

```bash
HDB_API_BASE=http://127.0.0.1:8002/api/v1 \
HDB_API_TOKEN=<token> \
uv run python -m src.mcp_bridge.server

# 也可以使用 HDB_USERNAME/HDB_PASSWORD。HDB_API_KEY（X-HDB-API-Key）
# 已预留给 external-api-service 分支；当前主线的 API 仍以 Session Token 为准。
```

工具：

- `list_kbs`
- `list_kb_files` / `list_directories` / `read_file_chunks` / `download_kb_file`
- `get_parse_task` / `retry_parse`
- `search_knowledge_base`（无状态词法检索，返回 chunk 定位；不创建会话）
- `list_tables` / `query_table`（表格物化表的 schema 与只读 SQL）
- `list_spreadsheets` / `list_circuit_designs`
- `query_circuit`（按实例/位号或网络名查询电路设计）
- `list_assets` / `get_asset` / `list_doc_assets` / `get_doc_asset`
- `upload_file`（单文件真实写入；需要 KB write 权限，平台会审计并触发解析）
- `create_directory_import` / `upload_directory_import_file` / `get_directory_import`
  （目录批量导入：先登记路径清单，再逐文件上传并查询进度；单文件仍受 MCP 大小限制）
- `delete_file`（默认 dry-run，必须显式 `confirm=true` 才删除）
- `delete_directory`（默认 dry-run；需 `confirm=true`，`recursive=true` 才会删除目录内容）

说明：MCP 桥不提供平台代答/证据链工具；外部 agent 用
`search_knowledge_base` 定位 chunk，再用 `read_file_chunks` 读原文自行推理。
检索结果受 token 所有者的知识库 ACL 约束。

## 隔离边界

- 本产品按 **用户** 隔离：`user_id` 是数据归属和权限边界。
- 部门只是共享/可见性分组，不是租户容器。
- 会话、workspace、artifact、KB 归属用户；部门共享是显式策略。
- `RequestContext.tenant_id` 兼容字段使用 `str(user.id)`，供附件/导出/资源
  governor 复用，不引入独立租户管理。
- `WorkspaceStore` 默认跟随控制面：控制面是 PostgreSQL 时也用 PostgreSQL，
  避免多实例下 SQLite 分裂。
- 启动迁移会删除旧 task 表（`tasks*` / `agent_tasks*`）、独立 `tenants` 表，
  以及 `users` / `departments` / `knowledge_bases` / workspace 表上的
  `tenant_id` 死列；附件、导出等兼容字段保留，其值统一来自 `user.id`。
- 解析文件与 workspace 派生文件是两套数据：解析文件只通过 KB 检索/引用访问，
  workspace 只存 Agent 生成的草稿和交付物。

## 当前边界（下一阶段）

- **执行能力**：尚未接入任意 shell/代码执行；现有 Sandbox Runner 是 bundle 制，
  需要注册通用执行 bundle 后才能提供 `run_script` 类能力。
- **Workspace UI**：后端已有 workspace/session 映射；前端仍是知识库会话列表，
  下一阶段增加 workspace 切换和文件视图。
- **对象存储**：解析文件/workspace 产物仍可能落本地；多副本 SaaS 需要接
  S3/MinIO，并按 `user_id/workspace_id` 前缀隔离。
- **KB 物理命名**：当前数据库按部门隔离登记，但 RAGFlow 物理数据集名
  仍需命名空间，避免同名 KB 冲突。
- **并发配额**：admission 已接收 user id；per-user 并发/存储/token 配额
  仍需配置化。
- **HITL**：尚无审批流；等执行能力接入后再对危险动作加审批。
- **队列隔离**：显式 task 产品已移除；聊天 turn 使用交互 chat 资源类。
