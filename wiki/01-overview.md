# 01 · 系统总览

## 1. 一句话定位

Hardware DataBase 是一个**基于 Agent 的硬件设计知识问答与受控文档生产平台**：
把部门内散落的规范手册、Excel 台账、EDF 网表、评审对话等异构资料统一摄取、索引，
用「检索 → 证据 → 有据可依的回答 / 可放行的工程文档」取代「翻文件 + 人工汇总」。

系统的三条产品主线：

1. **知识问答**（Chat）：自然语言提问，Agent 自主多跳检索文档/表格/电路/外部对话，给出带引用的回答，可一键导出 Markdown/Excel/Word/PDF/PPT。
2. **资产治理**（Assets / Wiki / Memory）：把解析结果沉淀为文档资产、知识 Wiki 页、长期记忆，并受部门与知识库权限约束。
3. **文档生成**（Document Authoring）：从模板或系统配方出发，结合冻结的证据快照，生产 ICD/FPT/需求规格等受控文档，人工审核后放行。

三条主线的共同底座：**多源摄取管道 + 统一证据模型 + 可观测的 Agent 运行时 + 部门级权限治理**。

---

## 2. 能力矩阵（按子系统）

| 子系统              | 代码位置                                                                  | 核心类/入口                                                        | 规模           | 默认开关                                    |
| ------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------ | -------------- | ------------------------------------------- |
| 文档问答（RAGFlow） | `src/pipelines/document_rag/`                                           | `RAGFlowBackend` / `RAGFlowClient`                             | ~3.5k          | 常开（需 RAGFlow 实例）                     |
| Agent 查询运行时    | `src/agents/`                                                           | `MultiSourceAgentRunner`                                         | ~14.7k         | 常开                                        |
| 表格结构化          | `src/pipelines/spreadsheet/`                                            | `XlsxParser` / `TableIndexStore` / `SpreadsheetSemanticTool` | ~2.5k          | 常开                                        |
| 电路结构化          | `src/circuit/`                                                          | `CircuitIndexService` / `CircuitQueryEngine`                   | ~8.6k          | 常开（语义向量默认停用）                    |
| 长期记忆            | `src/memory/`                                                           | `MemoryService` / `MemoryWorker`                               | ~6.7k          | `MEMORY_ENABLED=true`；个人记忆需 opt-in  |
| 对话附件            | `src/attachments/`                                                      | `AttachmentService` / `AttachmentWorker`                       | ~9.0k          | `CHAT_ATTACHMENTS_ENABLED=true`           |
| 文档生成            | `src/document_authoring/`                                               | `DocumentGenerationService` / `InternalDocumentHarnessRuntime` | ~49.4k         | 灰度：多个`DOCUMENT_*` 开关默认 `false` |
| 结果导出            | `src/result_exports/`                                                   | `ResultExportWorker` / `render_result`                         | ~4.5k          | `RESULT_EXPORT_ENABLED=true`              |
| 外部对话            | `src/external_conversations/`                                           | `ExternalConversationQueryEngine`                                | ~1.2k          | 随上传路由启用                              |
| Wiki / 资产 / 项目  | `src/core/wiki.py`、`doc_assets.py`、`assets.py`、`src/projects/` | `WikiService` 等                                                 | ~4.4k          | 常开                                        |
| RAGAS 评估          | `src/evaluation/`                                                       | `EvaluationService` + `hardware-database-eval`                 | ~9.4k          | 管理端可用，需`eval` 依赖组               |
| API 层              | `src/api/`                                                              | `create_app` / `AppPipeline`                                   | ~10.7k         | 唯一后端入口                                |
| 前端                | `frontend/`                                                             | React 18 + Vite 6 + Tailwind 4                                     | ~104 个 ts/tsx | 独立构建                                    |

---

## 3. 技术栈与关键依赖

**运行时**：Python ≥ 3.12，包管理用 `uv`（`pyproject.toml` + `uv.lock`），ASGI 服务用 Uvicorn/FastAPI。

| 依赖                                                        | 版本约束                   | 用途                                                              |
| ----------------------------------------------------------- | -------------------------- | ----------------------------------------------------------------- |
| `deepagents`                                              | ≥0.7.8                    | `create_deep_agent`：Agent 循环骨架（底层是编译后的 LangGraph） |
| `langgraph`                                               | `==1.2.11`               | 图执行、checkpointer（SQLite/Postgres）                           |
| `langgraph-checkpoint-sqlite` / `-postgres`             | `3.1.1` / `3.1.2`      | 会话与文档生成的持久化 checkpoint                                 |
| `langmem`                                                 | `==0.0.30`               | 长期记忆抽取适配层（隔离在`src/memory/manager.py` 后）          |
| `langchain` / `langchain-openai` / `langchain-ollama` | 1.3.x                      | 模型适配                                                          |
| `llama-index-core`                                        | ≥0.12.52                  | 向量索引（电路/外部对话语义补充，默认停用）                       |
| `fastapi` / `uvicorn` / `httpx`                       | —                         | API 与 HTTP 客户端                                                |
| `markdown-it-py`                                          | ≥4                        | 回答 → 块模型（导出的中间表示）                                  |
| `python-docx` / `python-pptx` / `reportlab`           | —                         | Word/PPT/PDF 导出与文档渲染                                       |
| `sqlglot`                                                 | ≥27,<29                   | LLM 生成 SQL 的只读校验                                           |
| `pypdf`                                                   | ≥4                        | PDF 解析与标注                                                    |
| `psycopg[binary]` / `redis`                             | —                         | Postgres 控制面 / Redis 实时平面                                  |
| `opentelemetry-*` / `openinference-*`                   | —                         | 可观测性（traces/metrics/logs）                                   |
| `pandas`                                                  | ≥2.3.3                    | 兼容第三方（非表格索引主链路）                                    |
| `networkx`                                                | ≥3.5                      | 电路连通图                                                        |
| `ragas`                                                   | `==0.4.3`（`eval` 组） | 评估指标；刻意锁版本防止指标语义漂移                              |

**前端**：React 18.3、react-router-dom 7、Tailwind 4、Radix UI（shadcn 风格本地组件）、
lucide-react、sonner（toast）、vitest（测试）。**刻意不引入**：Redux/Zustand、react-query、axios、
markdown 渲染库、图表库、i18n 库——全部手写实现（见 [19-frontend.md](19-frontend.md)）。

**外部系统**：

| 外部依赖                     | 角色                                            | 缺失时的行为                                               |
| ---------------------------- | ----------------------------------------------- | ---------------------------------------------------------- |
| RAGFlow                      | 文档解析与检索的唯一后端                        | 文档问答不可用；`RAGFLOW_API_KEY` 为空时客户端构造即抛错 |
| LLM（Ollama 或 OpenAI 兼容） | Agent、写作、记忆、评估裁判                     | 查询失败但 turn 会被正常收敛为失败态                       |
| PostgreSQL                   | 控制面/记忆投影/checkpoint/配额（可选）         | 默认全 SQLite 单机                                         |
| Redis                        | 取消快路径、跨进程 SSE、分布式准入（默认必需） | `REDIS_REQUIRED=true` 时缺失拒绝启动且 not_ready；测试可显式关闭 |
| ChromaDB                     | 电路/外部对话语义向量（**未声明为依赖**） | 语义索引显式 no-op                                         |

---

## 4. 端到端数据流（鸟瞰）

### 4.1 上传链路

```
前端 / CLI
  └─ POST /api/v1/kbs/{kb}/files  (multipart, ≤512MiB/请求)
      └─ AppPipeline.upload_files → DocumentManager → RAGFlowBackend.upload_files
          └─ IngestionOrchestrator.upload_files
              ├─ route_file(扩展名, source_group) → PipelineSpec
              ├─ 内容 SHA-256 → 去重（pipeline_documents 台账）
              ├─ DocumentArchiveManager.archive_source_file → storage/pipeline_archives/...
              └─ handler.submit()
                  ├─ RAGFlowDocumentHandler   → 远端上传 + markdown 解析任务（异步）
                  ├─ SpreadsheetPipelineHandler → 台账 status=archived，等 worker claim
                  ├─ CircuitPipelineHandler     → 同步解析 + 索引（indexed/degraded）
                  └─ ExternalConversationHandler → 本地解析 + index.db + LLM 后处理（后台线程）
```

### 4.2 查询链路（核心）

```
用户提问（前端 ChatPage）
  └─ POST /conversations/{id}/turns          # 落库 user 消息、冻结附件快照、生成 turn
  └─ POST /turns/{turn_id}/start             # 幂等（生产可为 no-op）
  └─ GET  /turns/{turn_id}/events            # SSE：delta / stage / tool_* / done / error
        │
        └─ worker 线程执行 _run_turn
            ├─ ConversationService.claim_turn_result  # 租约 + fencing + 顺序
            ├─ AgentRunner.stream(...)
            │   ├─ 作用域解析 + ConversationOrchestrator.plan() → route/tools
            │   ├─ 工具装配（capability 过滤）
            │   ├─ 系统提示词装配（固定规则 → 记忆 → 附件预检 → 导出协议 → 收据）
            │   ├─ deepagents 循环：模型自主调用 document_search / circuit_search /
            │   │   spreadsheet_* / conversation_search / wiki_search / memory_search ...
            │   └─ ToolRuntime 累积 Evidence、诊断、预算
            ├─ 回答归一化（markdown 导出意图处理）
            └─ complete_turn_result（同事务写回答 + turn 终态事件 + 导出任务 + 记忆 outbox）
```

### 4.3 文档生成链路

```
模板上传 → 消毒（宏/外链/嵌入物剥离）→ 确定性盘点 → LLM 建议 → 安全策略判定
  → （人工确认）激活为 DocumentSchema + 区域/绑定
  → 创建 GenerationSession（legacy brief 或 v2 OutputSpec intake）
  → 编译 DocumentPlan（语义单元 / 依赖边 / 覆盖契约 / 单元任务）
  → 人工哈希绑定确认 → DocumentPlanSubmission（outbox）→ worker 派发
  → 物化 DocumentWorkOrder（v3 指纹：绑定 output_spec/plan 的 id+version+hash）
  → InternalDocumentHarnessRuntime.create_run 冻结 manifest
  → AuthoringGraph / PlanDAGExecutor：逐单元检索 → 写作 → 校验 → 落盘（receipt 幂等）
  → 覆盖评估 → 单元评审 → 聚合 DocumentModel → 渲染 → 包校验 → 放行门
  → DocumentArtifact(review_candidate) → 人工 approve → approved_release
```

---

## 5. 关键设计原则（贯穿全系统的「铁律」）

1. **后端权威（backend authority）**
   前端只提供交互提示，路由/执行器/权限/工具可见性全部由服务端决定
   （`ConversationPlan.authority == "backend"`；`ConversationOrchestrator.plan`）。
2. **两级角色 + 内容隔离**
   `system_admin` 是**治理角色**，永远不能访问任何知识库内容（检索/上传/文件/解析任务），
   `employee` 是部门员工。目的是防止平台管理员静默窥视部门私有数据。
   实现点：`RequestContext.has_kb_permission` 对 sysadmin 恒 False；API 层 `reject_system_admin_kb_access`。
3. **知识库默认私有 + 所有者共享**
   逐用户授权表已删除，改为「所有者 + 部门共享开关」`knowledge_bases.is_shared`；
   共享后本部门员工获得 admin。删库仅所有者可删。
4. **Fail-open（可用性优先）与 Fail-closed（安全优先）分层**

   - 工具/检索/记忆/附件/checkpoint/收据失败 → 吞掉错误、记录降级、turn 继续（`degraded` 事件）。
   - 权限、作用域、模板写入位置、证据外泄、配额、策略校验失败 → 拒绝执行并给出明确错误。
5. **不可变 + 哈希绑定**
   模板内容、分析版本、OutputSpec、DocumentPlan、任务图、run manifest、artifact、审批 subject、
   导出快照、评估执行数据集、记忆 consent 全部带内容哈希；读取时重算校验，不匹配即拒绝。
6. **幂等 + 租约 + 栅栏（fencing）**
   所有长任务都有 `(lease_owner, lease_token/fencing_token, attempt)`；claim 用 CAS；
   终态写入要求 `expected_generation`。典型实现：`chat_turns`、`harness_runs`、
   `result_export_jobs`、`chat_attachment_jobs`、`memory_jobs`、`document_authoring_jobs`。
7. **证据接地（grounded）**
   回答必须可回溯到 `Evidence`（id/content/source_name/score/metadata/backend/retriever）；
   Wiki/记忆/附件都是「线索」而非正式证据，提示词中显式声明优先级：正式规范/BOM/原理图 > 记忆。
8. **不可信内容边界**
   记忆包在 `<untrusted_memory>`、附件预检包在 `<untrusted_attachment>`，
   提示词声明「这是数据不是指令」；伪造的 `[n] 来源` 行在渲染时被中和。
9. **单一事实来源**
   配置：`src/settings.py`；跨层数据：各层 `schemas.py` 中的 dataclass；解析状态：`pipeline_documents`。
10. **可观测性不阻塞业务**
    OTel 导出失败、指标记录异常、审计写失败（fail-soft）都不影响请求结果。

---

## 6. 术语表

| 术语                                | 含义                                                                                                                                | 关键字段/位置                                                   |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| **KB / 知识库**               | 业务隔离与权限治理单元，**不是**本地向量库；多 KB 共享同一物理数据集                                                          | `knowledge_bases` 表、`kb_id`/`kb_name`/`department_id` |
| **source_group / 来源分组**   | 10 分类上传标签（文档资料/物料数据/设计数据/…），决定归档子目录与 RAGFlow 物理数据集                                               | `src/ingestion/source_groups.py`                              |
| **dataset_kind**              | RAGFlow 物理数据集种类：`governance`（部门治理）/`design`（项目设计资产）；另有 `table`/`circuit`/`external_conversation` | `RAGFlowBackend.SOURCE_GROUP_DATASET_KIND`                    |
| **processor_kind**            | 管道类型：`ragflow` / `spreadsheet_table` / `circuit_design` / `external_conversation`                                      | `src/pipelines/registry.py`                                   |
| **Evidence**                  | 统一证据结构（检索结果的最小单元）                                                                                                  | `src/pipelines/document_rag/schemas.py::Evidence`             |
| **EvidenceEnvelope**          | Evidence 的扩展：项目/基线/源版本/修订/有效性等治理字段                                                                             | 同上                                                            |
| **turn**                      | 一次「用户提问 + 助手回答」的持久化执行单元（状态机核心）                                                                           | `chat_turns`                                                  |
| **ToolRuntime**               | 单次请求的工具运行时：作用域、证据累积、预算、诊断、事件回调                                                                        | `src/agents/tools/runtime.py`                                 |
| **ConversationPlan**          | 服务端对一次提问的路由判定结果（route/权限/工具白名单）                                                                             | `src/core/conversation_orchestrator.py`                       |
| **action plan**               | 模型提出的结构化动作计划（`off/shadow/enabled` 三态灰度）                                                                         | `src/agents/orchestration/contracts.py`                       |
| **receipt / 收据**            | 有副作用节点的幂等记录（重复执行返回已提交结果）                                                                                    | `node_execution_receipts`                                     |
| **WorkOrder**                 | 文档生成的冻结执行定义（输入指纹 v1/v2/v3）                                                                                         | `document_work_orders`                                        |
| **HarnessRun**                | WorkOrder 的一次执行尝试（租约/栅栏/降级原因）                                                                                      | `harness_runs`                                                |
| **OutputSpec / DocumentPlan** | v2 规划产物：「生成什么」与「怎么执行」，均不可变且哈希绑定                                                                         | `document_output_specs` / `document_plans`                  |
| **Artifact**                  | 不可变的生成产物（`draft_preview/review_candidate/approved_release`）                                                             | `document_artifacts`                                          |
| **Revision**                  | 受治理的产物修订请求（永不自动放行）                                                                                                | `artifact_revisions`                                          |
| **candidate / verified**      | 记忆的两种投影态：候选（模型抽取）与已验证（人工/证据确认）                                                                         | `memory_projections.projection_kind`                          |
| **consent event**             | 个人记忆的逐事件授权（不可变，只能 revoke）                                                                                         | `memory_consent_events`                                       |
| **ResultSnapshot**            | 回答结果的不可变快照（导出的源）                                                                                                    | `result_snapshots`                                            |
| **ResultEnvelope**            | 快照载荷：标题/回答/表格/引用/块模型                                                                                                | `src/result_exports/models.py`                                |
| **collection QC**             | 评估在线采集后的质检门（fail/pass_with_warnings/pass）                                                                              | `collection_qc.json`                                          |
| **cohort_fingerprint**        | 评估样本集合指纹，用于基线可比性校验                                                                                                | `src/evaluation/history.py`                                   |
| **ICP / ICD**                 | 互联文档（Interconnect Document）：连接器/引脚定义表格是核心产物                                                                    | `icd_*.py`                                                    |
| **shadow planning**           | 只编译并记录计划、不执行的灰度模式                                                                                                  | `DOCUMENT_PLANNING_SHADOW_ENABLED`                            |

---

## 7. 代码地图

```Python
Hardware-DataBase/
├── src/
│   ├── settings.py                 # 单一配置事实来源（1731 行，全部 .env 读写/校验/热重载）
│   ├── api/                        # FastAPI 唯一后端入口
│   │   ├── app.py                  # create_app / lifespan / 中间件 / 路由注册
│   │   ├── deps.py                 # 鉴权依赖、pipeline/auth 单例、KB 权限拒绝
│   │   ├── context.py              # AuthUser → RequestContext
│   │   ├── errors.py               # PermissionError→403 / ValueError→400 / RuntimeError→500
│   │   ├── admission.py            # 请求级准入控制
│   │   ├── schemas.py              # 全部 HTTP DTO（1373 行）
│   │   └── routes/                 # 24 个路由模块（query/document_generation 最大）
│   ├── core/                       # 应用核心（21.3k）
│   │   ├── app_pipeline.py         # 中央编排器（6136 行）
│   │   ├── conversation.py         # 会话/轮次权威库（2973 行，SQLite ↔ PG 分派）
│   │   ├── conversation_orchestrator.py  # 路由判定
│   │   ├── intent.py               # 规则版意图分类
│   │   ├── auth.py                 # 用户/部门/KB/会话/审计（1246 行）
│   │   ├── wiki.py / doc_assets.py / assets.py  # 知识沉淀
│   │   ├── model_factory.py / chat_model_runtime.py / llm_governor.py / model_gateway.py
│   │   ├── concurrency/            # 资源治理（resource_governor 2125 行）
│   │   ├── redis_*.py / cancellation.py / query_tokens.py
│   │   └── app_logs.py             # 审计链 + 查询 trace
│   ├── agents/                     # Agent 运行时（14.7k）
│   │   ├── runner.py               # 3646 行：提示词/工具装配/主循环/footer
│   │   ├── tools/                  # 11 个工具模块 + ToolRuntime
│   │   ├── orchestration/          # action plan 契约/执行/收据/能力
│   │   ├── checkpoints/            # attempt 语义 + SQLite/PG provider
│   │   ├── subagents/              # research/reviewer（默认关闭，仅测试/脚本使用）
│   │   ├── scopes/                 # 表格/电路作用域解析器
│   │   └── claim_evidence.py       # claim/requirement/coverage 词表与模型
│   ├── pipelines/                  # 摄取管道（7.7k）
│   │   ├── registry.py             # 扩展名 → PipelineSpec
│   │   ├── ingestion.py            # IngestionOrchestrator + 4 handler
│   │   ├── runtime.py / runtime_factory.py  # 持久化解析 worker
│   │   ├── document_store_sqlite.py         # pipeline_documents 台账
│   │   ├── document_rag/           # RAGFlow 后端与 schema
│   │   └── spreadsheet/            # xlsx_parser / table_store / sql_materializer
│   ├── circuit/                    # 电路子系统（8.6k，含 vendor/spydrnet）
│   ├── memory/ / attachments/ / document_authoring/ / result_exports/
│   ├── external_conversations/ / projects/ / evaluation/ / ingestion/ / services/
│   ├── storage/control_plane/      # 控制面 PG schema/loader/adapter
│   ├── observability/              # OTel 引导、指标、健康、worker 注册
│   ├── workers/main.py             # 统一后台 worker（role: all|chat|authoring|parse）
│   ├── maintenance/cli.py          # 维护命令
│   └── test_data/                  # 结构化测试数据（骨架）
├── frontend/                       # React SPA
├── deploy/observability/           # OTel Collector/Prometheus/Tempo/Loki/Grafana/Phoenix
├── deploy/redis/                   # 自管 Redis（实时面：取消快路径 + 跨进程 SSE）
├── deploy/postgres/                # 自管 PostgreSQL（pgvector：控制面/记忆/检查点/配额）
├── scripts/                        # hdb.sh 进程管理 + redis.sh/postgres.sh 自管设施 + production_hardening/ 迁移演练
├── benchmarks/spreadsheet/         # 表格检索基准（与 RAGAS 评估独立）
├── evaluation/                     # RAGAS 数据集
├── tests/                          # ~307 个 Python 测试模块
└── storage/                        # 运行期状态（gitignored）
```

---

## 8. 角色与权限速览

| 能力                                        | `system_admin`（治理角色） | `employee`（部门员工）    |
| ------------------------------------------- | ---------------------------- | --------------------------- |
| 部门增删                                    | ✅                           | ❌                          |
| 创建账号（employee 绑定业务部门；system_admin 归属 system 部门） | ✅                | ❌                          |
| KB 挂载（`PUT /kbs/{name}/assign`）       | ✅                           | ❌                          |
| 系统配置 / 日志中心 / RAGAS 评估 / 治理面板 | ✅                           | 日志与治理/状态页（本部门） |
| 创建 KB                                     | ❌（且不能访问内容）         | ✅（新库默认私有）          |
| 上传/删除/查看文件、检索                    | ❌**（铁律）**         | ✅（自己的库 + 部门共享库） |
| 切换 KB 共享                                | ❌                           | ✅ 仅所有者                 |
| 删除 KB                                     | ❌                           | ✅ 仅所有者                 |
| 文档生成 / Wiki / 资产 / 记忆               | ❌ 内容路径被拒              | ✅                          |

实现要点：

- API 层 `deps.reject_system_admin_kb_access(ctx)` 在内容路由前置拒绝；
- `AuthService.list_accessible_kbs` / `get_kb_permissions_for_user` 对 sysadmin 返回空；
- 前端 `KbContentRoute` 同样拦截，但**服务端才是权威**。

---

## 9. 运行形态速览

单机默认（开发/小团队）：

```bash
uv sync --all-groups
uv run hardware-database-server        # API + 前端代理目标（默认 127.0.0.1:8001）
cd frontend && npm ci && npm run dev   # 前端 5176，/api 代理到 8001
```

API 进程在 lifespan 中默认自动拉起子进程：统一 worker（`HDB_API_SPAWN_WORKER=1`）、
导出 worker（`HDB_API_SPAWN_EXPORT_WORKER=1`）；记忆 worker 默认**不**自动拉起
（`HDB_API_SPAWN_MEMORY_WORKER=0`，单写者约束）。

生产/多实例：关闭所有 `HDB_API_SPAWN_*`，按角色独立起 worker，切 Postgres + Redis
（详见 [02-architecture.md](02-architecture.md#6-部署拓扑与运行手册) 与 [18-observability-and-ops.md](18-observability-and-ops.md)）。
