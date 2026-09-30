<div align="center">

## 项目介绍

硬件研发知识分散在规格书、接口表、网表、原理图和历史讨论中。一次工程查询往往需要同时查阅多种资料，再把结论整理进设计说明或评审文档。

Hardware DataBase 为这些资料提供统一入口：文档由 RAGFlow 处理，表格和电路采用结构化索引，Agent 按问题调用检索工具，再将来源与结果展示给工程师。

```text
工程资料 → 多源解析与检索 → 带来源的回答 / 文档初稿 → 人工审核 → 导出与归档
```

适合希望自托管工程知识库、复用历史设计资料、探索技术文档自动化的硬件团队与开发者。

**当前状态：持续开发中的预览版本。** 默认部署面向单机与小规模内部使用；多主机高可用和完整灾备验收仍需进一步完善。

## 核心能力

| 能力                   | 说明                                                                                    |
| ---------------------- | --------------------------------------------------------------------------------------- |
| **多源检索**     | PDF/Word 等文档接入 RAGFlow；Excel、EDF 网表与历史对话使用各自的索引和检索工具          |
| **Agent 查询**   | 基于 deepagents / LangGraph，在有界工具循环中选择检索来源、组织回答，并展示执行轨迹     |
| **工程文档生成** | 分析模板、制定文档计划、检索填充依据，支持 ICD 等文档的审核、修订与生成                 |
| **权限与审计**   | 部门、员工、知识库授权及操作审计；系统管理员负责治理，与业务内容访问分离                |
| **持久化任务**   | 对话、解析、文档生成和导出通过后台任务执行，具备状态记录、租约与恢复机制                |
| **多格式导出**   | 将结果固化为快照，由后台 Worker 生成 Markdown、Excel、Word、PDF、PowerPoint             |
| **可控扩展**     | 远程 MCP、文本 Skill 与执行型 Skill；按审核、授权和订阅生效，MCP 写入与沙箱执行默认关闭 |
| **记忆与评估**   | 经用户授权的记忆提炼、项目记忆检索，以及 RAGAS 在线评估、回答快照重评和历史对比         |
| **资源与观测**   | 模型和工具并发配额；支持 OpenTelemetry、Prometheus、Grafana、Phoenix 等观测组件         |

## 界面预览

| 知识库问答 | 知识资产治理 |
| ---------- | ------------ |
|            |              |

## 快速开始

以下命令在下载或克隆仓库后的**仓库根目录**执行。

### 依赖说明

| 组件                                 | 用途与准备方式                                                          |
| ------------------------------------ | ----------------------------------------------------------------------- |
| **Docker Engine + Compose v2** | 推荐部署方式；需要能够访问 Docker daemon                                |
| **RAGFlow 实例与 API Key**     | 文档上传、解析与检索，需单独部署或接入已有实例                          |
| **模型服务**                   | Ollama 或 OpenAI-compatible API，需单独准备；Agent 需要模型支持工具调用 |
| **PostgreSQL / pgvector**      | Docker 编排自带，用于生产控制面、记忆、检查点等状态                     |
| **Redis**                      | Docker 编排自带，用于取消与跨进程事件；默认是硬依赖                     |
| **Python ≥ 3.12、uv**         | 仅源码开发需要；Docker 镜像内已安装                                     |
| **Node.js ≥ 22.12、npm**      | 源码前端开发推荐环境；Docker 构建无需宿主机安装                         |

**Docker 启动项目自身的业务与观测栈，不包含 RAGFlow 和模型服务。** 两者可在首次登录后配置，但资料解析与问答需要它们可用。

### Docker 部署（推荐）

```bash
bash deploy/docker/quickstart.sh
```

脚本生成数据库、观测栈和管理员口令，构建 API/Web 镜像，并启动 PostgreSQL、Redis、迁移服务、API、独立 Worker、Web 和观测栈。

- Web：`http://<主机IP>:8080`；首次生成的管理员口令会打印到终端。
- 只准备配置：`bash deploy/docker/quickstart.sh --no-start`。
- 资源受限时跳过观测：`bash deploy/docker/quickstart.sh --without-observability`。
- 应用配置在 `deploy/docker/config/.env`；Compose 密码、镜像和端口在 `deploy/docker/.env`，不依赖根目录 `.env`。
- 默认 API 和观测端口绑定宿主回环地址；远程访问观测页面可使用 SSH 端口转发。
- 默认端口已被占用时，配置 `HDB_HTTP_PORT`、`HDB_API_PORT` 和对应 `OBS_*_PORT`。

镜像加速、离线分发、目录权限、升级与备份恢复见 [Docker 部署指南](deploy/docker/README.md)。

### 源码开发

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后准备配置与依赖：

```bash
uv sync --all-groups
cp .env.example .env
```

编辑 `.env`，至少设置：

- `AUTH_DEFAULT_ADMIN_PASSWORD`：自行设置至少 8 位的非默认口令；源码方式不会自动生成口令。
- `REDIS_URL`：可用的 Redis 地址；默认 `REDIS_REQUIRED=true`，未配置或不可达会阻止 API 启动。
- `RAGFLOW_BASE_URL` / `RAGFLOW_API_KEY`：文档后端。
- `AGENT_LLM_PROVIDER` 与对应的 `AGENT_OLLAMA_*` 或 `AGENT_CUSTOM_*`：模型配置，也可登录后填写。

没有 Redis 实例时，可在 Linux 上使用仓库脚本启动，它会将连接配置写入根目录 `.env`：

```bash
bash scripts/redis.sh up
uv run hardware-database-server
```

已有 Redis 时，填好 `REDIS_URL` 后直接运行 `uv run hardware-database-server`。API 默认监听 `127.0.0.1:8001`。

另开终端启动前端：

```bash
cd frontend
npm ci
npm run dev
```

访问 `http://127.0.0.1:5176`，前端默认代理到本机 API。源码单机模式支持 SQLite；需要 PostgreSQL 时按 [数据库迁移指南](deploy/postgres/README.md) 配置。仓库的 Bash 部署脚本面向 Linux，其他环境需自行准备基础服务。

## 首次使用

### 配置与账号

1. 使用 `admin` 登录：Docker 使用首次生成的口令，源码方式使用 `.env` 中设置的口令。
2. 在「系统配置」填写 RAGFlow 地址/API Key 和模型端点。
3. 由系统管理员创建业务部门、注册员工账号。
4. **切换为员工账号**，创建知识库、上传资料并进行问答。系统管理员的治理权限不包含知识库内容访问权限。

### 验证第一个业务闭环

准备一份可以用于演示的规格书或接口 Excel 表，然后：

1. 创建知识库并上传文件，等待解析完成。
2. 提问资料中有明确依据的问题，例如：“接口 J1 的电压范围和终端匹配要求是什么？”
3. 对照原文件检查答案的引用与检索轨迹；资料没有该信息时，检查回答是否明确说明证据不足。
4. 从已完成的回答提交 Word/PDF/Excel 等导出任务，在任务中心查看状态并下载。
5. 需要生成工程文档时，进入文档生成流程，上传模板、确认计划并审核结果。

更多数据源和工作流见 [技术 Wiki](wiki/README.md)。

## 架构概览

```text
React + TypeScript
        │ HTTP / SSE
        ▼
FastAPI ── 鉴权、配置、任务 API、审计
        │
        ├── AppPipeline / Agent / 文档生成服务
        │       ├── RAGFlow：文档解析与检索
        │       ├── 表格、电路、历史对话检索工具
        │       ├── Ollama / OpenAI-compatible 模型
        │       └── 经授权的 MCP / Skill / Sandbox
        │
        └── 持久化任务 → Chat / Authoring / Parse / Export / Memory Workers

状态与数据：PostgreSQL · Redis · 本地 storage / SQLite
遥测：OpenTelemetry → Collector → Prometheus / Grafana / Tempo / Loki / Phoenix
```

| 数据面                | 职责                                                       |
| --------------------- | ---------------------------------------------------------- |
| PostgreSQL            | 按域配置的控制面、会话与任务、记忆、检查点、配额等生产状态 |
| Redis                 | 取消信号、跨进程 SSE 和短期实时状态                        |
| RAGFlow               | 文档数据集、解析与检索，属于外部依赖                       |
| `storage/` / SQLite | 附件、原件归档、导出文件、索引与仍采用本地后端的业务数据   |

不同业务域支持独立的后端选择，详见 [配置与存储](wiki/20-config-and-storage.md)。备份需覆盖 PostgreSQL 与本地文件，RAGFlow 数据需要另行备份或重建。

**Worker 运行模式：** Docker 使用独立的 Chat、Authoring、Parse、Export、Memory 容器；直接运行源码 API 时默认自启统一 Worker 与 Export Worker，Memory Worker 需另行启动。`scripts/hdb.sh` 会管理各角色并关闭 API 自启，避免重复启动。完整职责见 [对话与任务](wiki/04-conversation-turns.md) 和 [部署指南](deploy/docker/README.md)。

## 扩展能力

<details>
<summary><strong>结果导出与长期记忆</strong></summary>

- 导出先创建不可变结果快照，再由 Export Worker 渲染各格式。任务具有权限校验、状态记录、保留期和下载审计。
- User Memory 默认不启用提炼，需要用户开启并针对消息授权；Project Memory 在工程范围内检索。
- 记忆作为有界的不可信历史线索注入，正式文档证据优先。源码模式可运行 `uv run hardware-database-memory-worker`。

详见 [结果导出](wiki/16-exports-and-external-conversations.md) 与 [长期记忆](wiki/11-memory.md)。

</details>

<details>
<summary><strong>MCP、Skill 与沙箱</strong></summary>

MCP 与文本 Skill 目录默认可用；远程服务需登记、启用并授权，无服务时不会发起连接。MCP 写入和沙箱执行默认关闭。

执行型 Skill 需要独立 Runner；MCP Gateway 按需部署。它们不是打开一个开关就自动具备的能力，需要完成服务身份、运行环境和审核配置。

详见 [扩展操作指南](docs/mcp-plugins.md)、[扩展架构](wiki/22-external-extensions.md)、[Runner 部署](deploy/sandbox-runner/README.md) 与 [Gateway 部署](deploy/mcp-gateway/README.md)。

</details>

<details>
<summary><strong>RAGAS 评估</strong></summary>

支持在线调用问答管线、保存回答快照、离线换裁判重评，以及阈值门禁和历史对比。测试通过数量与实际问答质量是不同指标。

```bash
uv sync --group eval
uv run hardware-database-eval validate --dataset evaluation/datasets/hardware_qa_v1.jsonl
```

完整评估需准备模型和 Embedding 配置；数据集、运行和评分命令见 [评估指南](evaluation/README.md)。

</details>

## 常见问题与当前边界

- **启动失败：** 先检查管理员口令、Redis 连通性、目录权限和端口；Docker 查看 `docker compose logs`。
- **登录后不能问答：** 管理员先配置外部服务并创建员工账号；使用有知识库权限的员工账号操作。
- **文档搜不到：** 检查 RAGFlow API Key、数据集和解析状态；Excel 与电路采用独立管线，应同时检查对应任务。
- **任务迟迟不完成：** 检查相关 Worker、模型端点和资源配额；Docker 就绪探针未通过时返回 HTTP 503。
- **改配置后未生效：** 部分配置热更新，监听地址、存储后端等需要重启。Docker 重建相关 API/Worker 时沿用业务与观测两个 Compose 文件，详见部署指南。
- **多节点与灾备：** 本地文件、部分 SQLite 状态及外部 RAGFlow 仍需专门规划；当前默认方案不提供多主机自动故障转移。Alertmanager 已接入，通知接收渠道需自行配置。

## 文档导航

| 文档                                                                 | 内容                                         |
| -------------------------------------------------------------------- | -------------------------------------------- |
| [技术 Wiki](wiki/README.md)                                           | 模块职责、API、鉴权、Agent、检索、任务与前端 |
| [架构说明](wiki/02-architecture.md)                                   | 业务链路、数据权威性与存储边界               |
| [Docker 部署](deploy/docker/README.md)                                | 配置、镜像、离线部署、升级与备份恢复         |
| [配置与存储](wiki/20-config-and-storage.md) / [配置模板](.env.example) | 配置项、热更新与后端选择                     |
| [文档生成](wiki/13-document-authoring.md)                             | 模板、计划、审核与生成工作流                 |
| [外部扩展](wiki/22-external-extensions.md)                            | MCP、Skill、Sandbox 授权与生命周期           |
| [评估指南](evaluation/README.md)                                      | 数据集、问答采集、评分与历史比较             |
| [可观测性与运维](wiki/18-observability-and-ops.md)                    | 遥测、状态检查与部署边界                     |
| [开发约定](CLAUDE.md)                                                 | 项目结构、命令与代码约定                     |

## 开发与贡献

欢迎提交 Bug、使用反馈、文档修正与 Pull Request。先阅读 [开发约定](CLAUDE.md) 和相关 Wiki。

```bash
# 仓库根目录：后端测试与静态检查
uv sync --all-groups
uv run pytest
uv run ruff check .
git diff --check

# 前端：测试、类型检查与构建
cd frontend
npm ci
npm test
npm run build
```

部分集成测试需要显式配置外部服务，具体条件见测试文件。请在反馈中说明版本、运行方式、复现步骤和日志；示例文件与日志请脱敏。改变配置或模块行为时，同步更新对应文档。

## 后续方向

- 打磨真实工程资料的检索、引用和文档生成闭环。
- 完善证据不足、冲突处理与版本时效治理。
- 补齐清洁部署、备份恢复和外部依赖故障演练。
- 在共享存储和状态边界明确后，完善多副本部署。

## License 与致谢

[MIT](LICENSE) © 2025 Luke Yu。第三方依赖及 vendored 代码保留各自的许可证。

感谢 [RAGFlow](https://github.com/infiniflow/ragflow)、[deepagents](https://github.com/langchain-ai/deepagents)、[LangGraph](https://github.com/langchain-ai/langgraph)、[LangMem](https://github.com/langchain-ai/langmem)、[pgvector](https://github.com/pgvector/pgvector)、[Redis](https://redis.io/)、[RAGAS](https://github.com/explodinggradients/ragas)、[React](https://react.dev/)、[Vite](https://vite.dev/) 与 [Tesseract OCR](https://github.com/tesseract-ocr/tesseract)。
