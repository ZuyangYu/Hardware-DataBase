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

- 工具：`workspace_list` / `workspace_read` / `workspace_write`；
- 路径必须是工作区内的相对路径，拒绝 `..`、绝对路径和越界符号链接；
- 交付物写入 `out/`；turn 结束时扫描并登记 artifact（同名文件自动递增版本）；
- 下载接口会再次校验产物路径必须在任务工作区内。

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

前端：

```bash
cd frontend && npm run build
```

## 当前边界

- 沙箱执行走既有 bundle 制 Skill（`SKILLS_ENABLED` + `make_skill_tools`），
  不提供任意 shell；需要时按现有 Sandbox Runner 的审核流程注册 bundle。
- 任务存储当前是 SQLite（`TASK_DB_PATH`），多实例部署前应迁移到控制面 Postgres。
- 计划（plan）默认空；后续可由 agent 通过工具更新，前端已支持展示。
