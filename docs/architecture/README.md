# Hardware DataBase 架构图

本目录包含两张架构图，均为原生 `.drawio` 文件，可直接用 draw.io Desktop 或 IDE 的 Draw.io 插件打开编辑。

## 1. 服务架构框图（黑白）

- `service-architecture-bw.drawio`：黑白框线图，面向服务/进程视角。
- `service-architecture-bw.preview.png`：布局预览（PIL 近似渲染，**不是** draw.io 导出图；最终以 `.drawio` 为准）。
- `service-architecture-bw.py`：重新生成脚本。

读法：

- 顶部是用户 → React SPA → nginx/Vite → FastAPI API 服务的同步入口。
- 中间 6 列：查询与 Agent、摄取与索引、治理与资产、交付与质量、扩展与沙箱、异步 Worker。
- 每列下方是该服务栈的直接依赖：LLM、RAGFlow、PostgreSQL、Redis、文件存储、MCP Gateway、Sandbox Runner/Broker。
- 实线表示同步调用，虚线表示异步任务 / 横切关注点；最右侧虚线为生产进程边界（API 进程 vs 独立 Worker 进程）。
- 底部是可观测性横切链路：OTel SDK → Collector → Prometheus/Tempo/Loki → Grafana/Phoenix。
- 共享依赖在多个服务栈下重复出现，表示该服务对其有直接依赖；生产部署中 PostgreSQL / Redis / 文件存储为同一实例。

## 2. 会话链路优化前后对比

- `conversation-optimization-before-after.drawio`：左右双泳道对比图，左边为优化前、右边为当前链路，中间为关键变化。
- `conversation-optimization-before-after.preview.png`：布局预览（PIL 近似渲染，**不是** draw.io 导出图）。
- `conversation-optimization-before-after.py`：重新生成脚本。

覆盖的 10 个环节：turn 创建、计划入口、检索准入、证据提升、会话记忆、工具事件、上下文压缩、循环预算、前端呈现、完成落账。重点变化：

- 检索命中先进入不可引用的 `candidate_evidence`，由 `evidence_read` 按需提升为正式证据；
- 预取最近 3 轮已读证据与上一轮 `answer_gaps`，支持跨轮续接；
- 事件使用 `schema_version / item_id / call_id` 类型化契约，前端按 `call_id` 配对；
- 按模型真实窗口解析上下文，发真实 `context_compacted` 并写 `context_manifest`；
- 循环预算提高并按“是否有新增证据 / 候选 / 来源”判断是否继续；
- 前端移除计划 / 预算 / 校验卡片，只保留「处理过程」和最终回答。

## 3. 系统架构总览（分层 × 业务域）

- `current-system-architecture.drawio`：横向 5 层、纵向 8 条业务链路的详细视图。
- `preview.png`：布局预览（PIL 近似渲染，**不是** draw.io 导出图）。
- `generate.py`：重新生成脚本。

读法：

- 横向 5 层：`L1 交互入口` → `L2 API` → `L3 编排/领域服务` → `L4 运行时/引擎` → `L5 数据/外部依赖`。
- 纵向 8 条业务链路：知识问答、摄取与索引、权限与治理、资产沉淀与附件、结果导出、评估/质量/成本、扩展与沙箱、可观测与运维。
- 每一行是一条独立链路；箭头只连接同一行相邻层的卡片，**不跨行、不交叉**。
- 同一后端可能被多条链路使用，因此在各行的 L5 数据列重复标注，便于按业务域追踪。
- 底部附 Docker Compose 部署与进程拓扑（web / api / migrate / postgres / redis / workers / 外部依赖）。

## 4. 通用 Agent 架构升级（Workspace-first）

- `general-agent-upgrade.drawio`：左右双泳道对比图，左边为升级前（Task-centric）、
  右边为升级后（Workspace-first），中间为 6 个关键变化；下方为升级后的通用 Agent
  分层与当前边界。
- `general-agent-upgrade.preview.png`：布局预览（PIL 近似渲染，**不是** draw.io 导出图；
  最终以 `.drawio` 为准）。
- `general-agent-upgrade.py`：重新生成脚本（`.venv/bin/python docs/architecture/general-agent-upgrade.py`）。

覆盖的 6 个升级维度：项目容器、执行模型、工具与能力、产物与交付、隔离与存储、前端呈现。重点变化：

- Workspace 成为项目容器（`kind=general|knowledge`），1 个 workspace 下挂 1..N 个 session；
  知识库只是可选绑定，未绑定即通用工作区。
- 聊天 turn 用 `trace_context.workspace_id` 关联工作区，不再自动创建 task / task_pending；
  复用 turn 队列 / fencing / SSE，显式 `/tasks` 的 task 模型保持不变。
- 通用工具面：`workspace_list/read/write/edit/glob/grep` +
  `workspace_publish_artifact`；kb_search、表格 SQL、网表、Skill、MCP 都是工具。
- 产物写入隐藏目录 `workspace/<id>/out/`，登记为版本化 artifact（同名自动递增），
  按 `session_id + turn_id` 挂到 assistant 消息，只显示文件名。
- 隔离简化为「用户即租户」：`user_id` 是数据归属和权限边界，部门只是共享分组；
  `WorkspaceStore` 跟随控制面 PostgreSQL，启动迁移清理旧 task/tenant 表与死列。
- 前端「处理过程」只投影真实工具动作，编排事件留在 turn 事件；流式统一「正在处理」。

## 依据

- `README.md`
- `wiki/01-overview.md`
- `wiki/02-architecture.md`
- `wiki/20-general-agent.md`
- `src/` 当前分支代码（路由、worker、存储与外部扩展）
