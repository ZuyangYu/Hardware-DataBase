# 通用 Agent：任务、工作区与产物

这套实现把 agent 从"检索问答"升级为"可挂载知识库的通用 agent"：

- **通用**：文件读写、知识库检索、表格 SQL、网表查询都是工具；任务不预设固定流程。
- **持久**：一个任务（task）拥有工作区、产物和多个 turn，可以继续、修改、续跑。
- **知识库一等公民**：任务挂载 KB 时，项目事实必须以 KB 证据为准；工作区文件和计算只用于加工与交付。
- **外部扩展**：任务定义（可选）放在 `storage/task_definitions/`，平台不内置业务模板。

## 数据模型

```
task (durable session)
 ├─ plan           # 可选；默认空，由 agent 自行规划
 ├─ workspace/     # 每任务一个隔离目录
 │   └─ out/       # 交付物；任务结束时自动登记为 artifact
 ├─ turn 1..N      # 复用现有 turn 队列 / fencing / SSE
 └─ artifacts      # 版本化产物，可下载
```

`chat_turns.trace_context` 携带 `{"task_id": ...}`；worker 每次执行时由 task store 重建：
工作区路径、可选外部定义、已有产物列表。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/tasks` | 创建任务（自动建会话 + 首个 turn） |
| GET | `/api/v1/tasks` | 当前用户的任务列表 |
| GET | `/api/v1/tasks/{id}` | 任务详情（当前 turn、产物、turns） |
| POST | `/api/v1/tasks/{id}/messages` | 继续/修改：同一任务新建 turn |
| POST | `/api/v1/tasks/{id}/stop` | 请求取消当前 turn |
| GET | `/api/v1/tasks/{id}/events` | 任务执行记录（聚合 turn 事件） |
| GET | `/api/v1/tasks/{id}/artifacts` | 产物列表 |
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
- 计划：`task_plan_update` 由 agent 维护（pending/running/done/blocked）；
- 路径必须是工作区内的相对路径，拒绝 `..`、绝对路径和越界符号链接；
- 交付物写入 `out/`；turn 结束时扫描并登记 artifact（同名文件自动递增版本）；
- 下载接口会再次校验产物路径必须在任务工作区内。

## 验收与执行记录

- 每次任务尝试结束生成确定性验收：产物、回答、引用、证据、部分交付；
- `GET /tasks/{id}/events` 返回跨 turn 的执行记录，工作台展示最近事件；
- 验收是"可用性下限"，不是质量评分；语义质量仍需人工/评测集。

## 前端

- 路由：`/tasks`（导航"工作区 → 任务"）；
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
- **任务存储**：默认 SQLite（`TASK_STORE_BACKEND=sqlite`）；多实例部署设置
  `TASK_STORE_BACKEND=postgres`，使用 `TASK_DB_URL`（空则回退
  `CONTROL_PLANE_DATABASE_URL`）。Postgres 表为 `agent_tasks` /
  `agent_task_turns` / `agent_task_artifacts`。
- **MCP**：已有任务/KB 问答/结构化查询桥；原始检索工具尚未直接暴露。

