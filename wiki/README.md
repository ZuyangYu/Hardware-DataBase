# Hardware DataBase 系统技术 Wiki

> 面向技术团队的深度系统说明。目标：让新加入的工程师能在 1~2 天内建立完整心智模型，
> 并在需要改动任意子系统时能精确定位代码、契约与开关。
>
> **基准快照**：2026-09-20 的仓库状态（仓库根目录）。
> 代码是唯一事实来源；本 Wiki 中所有路径、类名、表名、环境变量均来自对源码的逐文件核对。
> 若发现文档与代码不一致，以代码为准，并顺手修正本 Wiki。

---

## 这个系统是什么

Hardware DataBase（包名 `hardware-database`）是面向**硬件设计资产**的多源问答与文档生产平台。
它把四类异构数据统一到一个「检索 → 证据 → 回答/文档」的流水线上：

| 数据形态 | 处理管道 | 检索方式 |
|---|---|---|
| 文档（`.doc/.docx/.pdf`） | RAGFlow 远端解析 | RAGFlow `/api/v1/retrieval`（混合检索） |
| 表格（`.xlsx`，附件含 `.xlsm`） | 自研纯标准库 OOXML 解析 + 每 KB SQLite 结构化索引 | 语义行 LIKE / 单元格精确查 / LLM 生成只读 SQL |
| 电路（`.edf/.edif`，配套原理图 PDF） | 内置 SpyDrNet + 自研查询引擎 | 确定性结构化查询 + 可选语义向量补充 |
| 外部对话（`.txt/.md`） | 本地解析 + LLM 结构化/摘要 | 关键词索引 + 可选向量 |

在此之上还有三块「重资产」子系统：

1. **Agent 查询运行时**：基于 `deepagents` 的有界 ReAct 循环，模型自主决定调用哪些检索工具、多跳顺序与次数；前端实时展示工具轨迹。
2. **文档生成（Document Authoring）**：模板驱动的受控工程文档生产（ICD / FPT / 需求规格 / 通用报告），具备哈希绑定的不可变输入、证据接地、确定性渲染、人工放行门。约 4.9 万行，是仓库最大的子系统。
3. **RAGAS 评估**：管理员可用的回答质量评估子系统，支持在线采集 + 离线复评，带阈值门禁与历史基线对比。

---

## 阅读路线建议

**D0：想先跑起来** → [02-architecture.md](02-architecture.md) 的「部署拓扑与运行手册」+ [20-config-and-storage.md](20-config-and-storage.md) 的配置清单。

**D1：理解主查询链路** → [01-overview.md](01-overview.md) → [02-architecture.md](02-architecture.md) → [04-conversation-turns.md](04-conversation-turns.md) → [05-agent-runtime.md](05-agent-runtime.md) → [06-tools-and-evidence.md](06-tools-and-evidence.md)。

**D2：按领域深入**

| 你要做的事 | 先读 |
|---|---|
| 改文档上传/解析/检索 | [08-ingestion-and-ragflow.md](08-ingestion-and-ragflow.md) |
| 改 Excel 索引或 SQL 工具 | [09-spreadsheet.md](09-spreadsheet.md) |
| 改电路解析/查询 | [10-circuit.md](10-circuit.md) |
| 改长期记忆 | [11-memory.md](11-memory.md) |
| 改对话附件/文档修订 | [12-attachments.md](12-attachments.md) |
| 改文档生成/模板/ICD | [13-document-authoring.md](13-document-authoring.md) + [14-authoring-render-and-api.md](14-authoring-render-and-api.md) |
| 改 Wiki / 资产 / 项目治理 | [15-wiki-assets-projects.md](15-wiki-assets-projects.md) |
| 改导出 / 外部对话 / 评估 | [16-exports-and-external-conversations.md](16-exports-and-external-conversations.md) / [17-evaluation.md](17-evaluation.md) |
| 改前端 | [19-frontend.md](19-frontend.md) |
| 改模型/并发/限流 | [07-model-and-resources.md](07-model-and-resources.md) |
| 接入/维护 MCP、Skill 或 Sandbox 扩展 | [22-external-extensions.md](22-external-extensions.md) + [操作指南](../docs/mcp-plugins.md) |
| 上生产/扩容/排障 | [18-observability-and-ops.md](18-observability-and-ops.md) + [20-config-and-storage.md](20-config-and-storage.md) |
| 踩坑预警 | [21-gotchas-and-extension-guide.md](21-gotchas-and-extension-guide.md) |

---

## 页面索引

| # | 文档 | 内容要点 |
|---|---|---|
| 01 | [overview](01-overview.md) | 系统定位、能力矩阵、技术栈与依赖、术语表、代码地图、端到端数据流 |
| 02 | [architecture](02-architecture.md) | 分层架构、请求生命周期、进程模型与部署拓扑、存储全景、横切设计原则 |
| 03 | [api-and-auth](03-api-and-auth.md) | FastAPI 构造、路由清单、错误映射、鉴权/两级角色/KB 共享、审计链、上传 |
| 04 | [conversation-turns](04-conversation-turns.md) | 会话权威库、turns 状态机、租约/栅栏、SSE 事件目录、取消、控制面 PG |
| 05 | [agent-runtime](05-agent-runtime.md) | MultiSourceAgentRunner 全流程、提示词装配、工具可见性、action plan、checkpoint、claim/evidence、子代理 |
| 06 | [tools-and-evidence](06-tools-and-evidence.md) | ToolRuntime、7 类工具契约、检索执行器、证据渲染/预算、fail-open 语义 |
| 07 | [model-and-resources](07-model-and-resources.md) | model_factory profile、chat_model_runtime、LLM governor、model gateway、多模态、准入与资源治理 |
| 08 | [ingestion-and-ragflow](08-ingestion-and-ragflow.md) | 文件路由、IngestionOrchestrator、四类 handler、文档台账 DDL、归档、嵌入对象、RAGFlow 后端与 retrieve 算法、source group 路由 |
| 09 | [spreadsheet](09-spreadsheet.md) | 纯标准库 xlsx 解析、每 KB 表索引、语义行与置信度、SQL 物化、LLM SQL 生成与只读校验 |
| 10 | [circuit](10-circuit.md) | 领域模型、EDF 解析与 SpyDrNet 补丁、索引与存储、查询引擎、电源/偏置/防护推导、图与向量、datasheet 链接 |
| 11 | [memory](11-memory.md) | 记忆 Catalog、candidate/verified 生命周期、consent 不可变、outbox、worker、检索与 `<untrusted_memory>` 注入 |
| 12 | [attachments](12-attachments.md) | 会话附件模型、上传配额、本地解析器、FTS/trigram/dense 检索、文档修订与放行、job/lease/协调器 |
| 13 | [document-authoring](13-document-authoring.md) | 核心概念与 ID/状态机、两条入口路径、模板分析、planning v2、harness 图与 agent、receipt/lease/边界 |
| 14 | [authoring-render-and-api](14-authoring-render-and-api.md) | Writer 与渲染器、模板消毒、持久化表结构、迁移、ICD 生成/对比、API 与灰度开关 |
| 15 | [wiki-assets-projects](15-wiki-assets-projects.md) | Wiki 蒸馏与检索、文档资产、硬件资产候选、项目治理与 source set snapshot |
| 16 | [exports-and-external-conversations](16-exports-and-external-conversations.md) | ResultSnapshot 不可变导出、5 种渲染器、导出 worker、外部对话解析/索引/LLM 结构化 |
| 17 | [evaluation](17-evaluation.md) | RAGAS 评估：CLI/服务/运行控制、采集 QC、评分、门禁、报告、历史与导入、API |
| 18 | [observability-and-ops](18-observability-and-ops.md) | OTel 引导、指标目录、结构化日志与脱敏、健康/就绪、worker 注册、部署与脚本、基准 |
| 19 | [frontend](19-frontend.md) | 技术栈、路由表、页面清单与端点、API 客户端与 SSE、状态模式、权限重复 |
| 20 | [config-and-storage](20-config-and-storage.md) | 全量配置项分类表、SQLite/PG 表清单、磁盘布局、迁移、备份与恢复 |
| 21 | [gotchas-and-extension-guide](21-gotchas-and-extension-guide.md) | 已知失效配置/死代码、易踩的坑、扩展新数据源/新工具/新渲染器的步骤 |
| 22 | [external-extensions](22-external-extensions.md) | MCP、文本/执行型 Skill、Sandbox Gateway/Runner 的权限边界、生命周期、产物与可观测性 |

---

## 本 Wiki 的写法约定

- **路径**：一律相对仓库根；行号锚点形如 `src/agents/runner.py:2340` 表示该行附近的实现。
- **契约优先**：先给数据契约（dataclass/表结构/枚举），再讲流程。
- **标注状态**：凡是「默认关闭」「已废弃」「仅测试使用」都会显式标注，避免误用。
- **变量命名**：`SCREAMING_SNAKE` 为环境变量或常量；`CamelCase` 为类；`snake_case` 为函数/字段。
- **中文与英文混排**：系统面向中文用户，提示词、错误文案、UI 均为中文；本 Wiki 用中文叙述，代码标识符保持原样。

## 维护须知

本 Wiki 位于仓库根目录 `wiki/`，**已纳入 git 版本管理**。
`.gitignore` 只忽略 `/docs/`——那里仍是本地设计/评估笔记（未跟踪），不要把新页面写回 `docs/wiki/`。

约定：

- 每页顶部标注「对应代码」；改动相关子系统时同步更新该页；
- 新增页面后在 `README.md` 的索引表登记编号与链接；
- 行号锚点会随重构漂移，失效时顺手修正；
- 页面内链接使用相对路径（同目录），重命名 `wiki/` 目录本身不会破坏链接。
