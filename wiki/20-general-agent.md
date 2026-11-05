# 通用 Agent：Workspace、Session 与产物

这套实现把底层 agent 从"知识库问答"升级为 **Workspace-first 的通用 agent**：

- **通用**：文件读写、知识库检索、表格 SQL、网表查询都是工具；不预设固定流程。
- **持久**：Workspace 是项目容器，Session 是会话，Turn/Run 是一次执行。
- **知识库是工具**：Workspace 可以绑定 KB，绑定后 `kb_search` 可用；未绑定就是通用工作区。
- **外部扩展**：MCP / Skill / 任务定义都是可选扩展。

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

普通聊天不再自动创建 task。聊天 turn 通过 `trace_context={"workspace_id": ...}` 关联
session 的 workspace；显式长任务才使用 `/tasks` 的 `task_id`。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/workspaces` | 创建通用/知识库工作区 |
| GET | `/api/v1/workspaces` | 当前用户的工作区 |
| GET | `/api/v1/workspaces/{id}/sessions` | 工作区下的会话 |
| POST | `/api/v1/tasks` | 创建显式长任务（自动建会话 + 首个 turn） |
| GET | `/api/v1/tasks` | 当前用户的任务列表 |
| GET | `/api/v1/tasks/{id}` | 任务详情（当前 turn、产物、turns） |
| POST | `/api/v1/tasks/{id}/messages` | 继续/修改：同一任务新建 turn |
| POST | `/api/v1/tasks/{id}/stop` | 请求取消当前 turn |
| GET | `/api/v1/tasks/{id}/events` | 任务执行记录（聚合 turn 事件） |
| GET | `/api/v1/conversations/{id}/artifacts` | 会话产物（前端对话页使用） |
| GET | `/api/v1/artifacts/{id}/download` | 按产物 id 下载（不暴露磁盘路径） |
| — | `artifact.turn_id` | 产物归属哪一轮，前端据此挂到 assistant 消息下 |
| GET | `/api/v1/tasks/{id}/artifacts` | 任务维度产物列表 |
| GET | `/api/v1/tasks/{id}/artifacts/{aid}/download` | 下载产物 |
| GET | `/api/v1/task-definitions` | 列出外部任务定义 |

创建示例：

```json
{
  "query": "按 TCN2 网表整理关键器件清单，输出 5 列表格",
  "kb_name": "AAA",
  "definition_id": "component_list",
  "title": "ADAS 器件清单"
}
```

## 外部任务定义（可选）

支持 `*.yaml` / `*.yml` / `*.json` / `*.md`（Markdown front-matter）。
目录：`TASK_DEFINITION_DIR`（默认 `storage/task_definitions/`）。

```yaml
id: component_list
title: 关键器件清单
goal: 产出可追溯的关键器件清单
deliverable: out/component_list.md（固定 5 列）
coverage:
  - 每个型号/用量都能追溯到证据编号
  - 缺失字段写“待确认”
skill: skill-key-device-list   # 可选：提示 agent 参考已物化的 SKILL.md
applies_when: [清单, BOM]
instructions: 不要展开完整 BOM
```

定义只描述**目标与验收**，不写步骤；没有定义时 agent 自由规划。

## 工作区与产物

- 工具：`workspace_list` / `workspace_read` / `workspace_write` /
  `workspace_edit`（唯一匹配保护）/ `workspace_glob` / `workspace_grep`；
- 显式 task 使用 `task_plan_update` 维护计划；普通聊天没有任务计划；
- 路径必须是工作区内的相对路径，拒绝 `..`、绝对路径和越界符号链接；
- 普通聊天用 `workspace_publish_artifact(name, content, kind)` 发布交付物；
  显式 task 仍用 `task_publish_artifact`；
- 内部仍写入工作区，但回答和前端不暴露 `out/` 或磁盘路径；
- `workspace_artifacts` 按 `session_id + turn_id` 归属，同名文件自动递增版本；
- 下载接口会再次校验产物路径必须在 workspace 目录内。

## 验收与执行记录

- 每次任务尝试结束生成确定性验收：产物、回答、引用、证据、部分交付；
- `GET /tasks/{id}/events` 返回跨 turn 的执行记录，工作台展示最近事件；
- 验收是"可用性下限"，不是质量评分；语义质量仍需人工/评测集。

## 前端

- 产物按 `turn_id` 归属到对应的 assistant 消息，文件名直接显示在消息下方
  （无独立区域/背景），点击下载；
- 聊天「处理过程」只投影实际工具动作；`task_context` / `task_mode` / `skill_loaded`
  属于运行时编排事件，保留在 turn 事件和任务工作台，不进入聊天轨迹；流式中的
  身份文案统一为「正在处理」，不把检索当成独立模式；
- `/tasks` 保留为历史/深链接，不再是主导航入口；
- 页面：新建任务（选知识库/外部定义）、任务列表、当前输出、产物下载、继续修改、停止；
- 轮询任务详情；终态后停止轮询。

## 测试

```bash
uv run pytest \
  tests/test_task_store.py tests/test_workspace.py \
  tests/test_task_definitions.py tests/test_tasks_api.py \
  tests/test_query_task_plumbing.py -q
```

实机功能矩阵（需要服务运行）：

```bash
HDB_E2E_USERNAME=aaa_admin HDB_E2E_PASSWORD=... \
uv run python scripts/functional_tasks.py
```

覆盖：写入+编辑、继续修改/产物版本、计划工具、停止、外部定义、
执行记录、挂载知识库+引用验收、MCP 桥。结果写入
`HDB_FUNCTIONAL_RESULTS`（默认 `/tmp/functional_tasks.json`）。

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
```

工具：

- `list_kbs`
- `create_task` / `get_task` / `list_task_artifacts`
- `ask_knowledge_base`（走受治理的 turn，返回带引用的回答）
- `list_spreadsheets` / `list_circuit_designs`

说明：第一版 MCP 桥没有直接暴露 `knowledge_search`/`evidence_read` 原始检索
工具；外部 agent 通过 `ask_knowledge_base` 或任务使用知识库。原始检索的
MCP 暴露需要新增一个受 token/ACL 约束的检索端点。

## 当前边界（下一阶段）

- **执行能力**：尚未接入任意 shell/代码执行；现有 Sandbox Runner 是 bundle 制，
  需要注册通用执行 bundle 后才能提供 `run_script` 类能力。
- **续跑**：`prepare_attempt` 已支持从 `resume_ref`/committed pointer fork
  上一 attempt；任务继续时 worker 会尝试按会话状态恢复。显式的"任务级
  checkpoint 快照/恢复"还需要与 attempt 隔离一起验证。
- **HITL**：尚无审批流；等执行能力接入后再对危险动作加审批。
- **队列隔离**：任务 turn 使用独立的 `task` 资源类（`RESOURCE_TASK_MAX_CONCURRENT`），
  不再占用交互 chat 槽位；隔离的 uncertain permit 在宽限期后自动释放。
- **稳定性巡检**：`uv run python scripts/check_stability.py` 检查
  uncertain permit、非终态 turn、重复 worker 部署。
- **任务存储**：默认 SQLite（`TASK_STORE_BACKEND=sqlite`）；多实例部署设置
  `TASK_STORE_BACKEND=postgres`，使用 `TASK_DB_URL`（空则回退
  `CONTROL_PLANE_DATABASE_URL`）。Postgres 表为 `agent_tasks` /
  `agent_task_turns` / `agent_task_artifacts`。
- **MCP**：已有任务/KB 问答/结构化查询桥；原始检索工具尚未直接暴露。

