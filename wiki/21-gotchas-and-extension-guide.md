# 21 · 已知坑、技术债与扩展指引

本页是「改代码之前先看」的清单：失效配置、死代码、容易误判的行为、以及常见扩展任务的步骤。

---

## 1. 失效/惰性配置（设置项存在但无效果）

| 配置 | 现状 | 说明 |
|---|---|---|
| `AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS` / `_MAX_DELAY_SECONDS` | 声明未使用 | 实际重试退避由 langchain 适配器与 provider 决定 |
| `MEMORY_ALLOW_GENERAL_CHAT_EXTRACTION` | 未读取 | 通用对话本来就没有 dept/KB，不会入队项目反思 |
| `MEMORY_USER_MEMORY_OPT_IN` | 未读取 | 个人记忆的 opt-in 是 `user_memory_settings` 行 |
| `MEMORY_USER_MEMORY_REQUIRE_PER_EVENT_CONSENT` | 未读取 | 逐事件同意是**无条件强制**的 |
| `RESOURCE_GOVERNOR_ENABLED` | 默认 false 且调用点 `getattr(..., True)` | 键总存在 → 实际取 False；所有资源许可是**惰性的**，只有显式打开才生效 |
| `RESOURCE_GOVERNOR_REQUIRE_PRECONFIGURED` | 仅在 governor 启用时有意义 | — |
| `CONVERSATION_SERIALIZE_SAME_THREAD` | 默认 false | 关闭时 `fencing_token/session_generation` 恒 0，`StaleTurnOwner` 不会触发；多轮可并发 |
| `.env.example` 的 dataset 名（`ADAS_new_develop`） | 与 `settings.py` 默认（`department_governance`/`project_design_assets`）不同 | 以实际 `.env` 与 RAGFlow 配置为准 |
| `RAGFLOW_FILE_ROOT` | 与 `PIPELINE_ARCHIVE_ROOT` 同默认 | 仅清理逻辑使用；不要让它指向不同目录 |
| `MEMORY_*_REQUIRE_*` 类开关 | 见上 | — |

**惰性/半实现功能**

| 功能 | 现状 |
|---|---|
| `RESEARCH_AGENT_ENABLED` / `REVIEWER_AGENT_ENABLED` | 两个子代理**未接入 runner/AppPipeline**，只被测试与 `scripts/production_hardening/reviewer_eval.py` 使用 |
| `PipelineRuntime.ensure_worker_running()` | 有意 no-op（上传绝不内联解析） |
| `clear_finished_parse_tasks` | RAGFlow 后端未覆写 → no-op（已完成行本来就被隐藏） |
| `_wait_archived_document_result` / `_wait_parse_progress` | 死代码（无调用者） |
| `classify_source_group` / `expand_source_group_for_file` / `display_source_group` | `src/` 内无调用者（前端直接传具体分组） |
| `src/test_data/` | 只有模型/store/词法查询骨架；**没有解析器**、没有 `manifest.py`；`PARSER_REGISTRY` 运行期为空 |
| `templateMappingCorrection.ts`（前端） | 仅单测引用，无页面消费 |
| `documentAuthoring.ts` 的大部分函数 | 已定义并有单测，但无页面调用（文档生成没有独立工作台） |
| `CircuitStatus.PARTIAL_EDF/PARTIAL_PDF` | 摄取路径不用 |
| `CircuitStructureCoverage.title_block/coordinates/visual_layout` | 恒 `UNAVAILABLE`（为未来解析器预留三态） |
| `power_tree.ambiguous_edges` / `control_edges` | 恒空列表（预留输出） |
| `SourceGroupRoute.weights` | 计算但无消费者（只用 `source_groups` + `confidence`） |
| 导出 `result_export_outbox` | 只写不读（队列表是 `result_export_jobs`）；保留为持久派发/审计行 |
| `AttachmentStore.sync_attachment_parse_state` | 已废弃的 no-op（为 API 对称保留） |
| `chromadb` / 语义向量 | **未声明依赖**；`CircuitVectorIndex` 与外部对话向量默认显式 no-op |
| `llama_index.core.Settings._embed_model` | `src/` 内无绑定 → 语义索引默认停用 |

---

## 2. 容易踩的行为坑

### 2.1 摄取与检索

1. `.txt/.md/.markdown` 在**非**「外部数据」分组时走 RAGFlow——这是**有意绕过扩展名表**的；
   把它们加进 `document_rag` 的 supported 会因扩展名冲突导致注册表抛错。
2. `.xlsm` 只有附件通道接受；KB 上传会报「无配置管道」并跳过。
3. `route_file` 的 `.xls` 拒绝是**不归档**的（连文件都不会存）。
4. `_INGEST_LOCKS` 是进程内锁：跨进程并发上传同一内容理论上可能双双到达 RAGFlow 提交
   （SQLite 唯一约束最终兜底）。
5. RAGFlow 解析超时判定依赖 **UTC 解析** `parse_started_at`；改动时间格式会导致 +8 小时误判。
6. `code == 102`（非 owner）在提交阶段是 warning，在删除阶段是本地清理成功，在存在性判定是「保留映射」。

### 2.2 表格

7. 公式**不求值**（只读缓存 `<v>`）。
8. 语义行的前向填充是启发式：跨节继承可能过度；消费方必须看 `inherited_json` 与置信度。
9. 列类型只按 raw_values 推断 → 日期列在 SQL 里是 `TEXT` 比较。
10. 物理表名 `t_<record>_<seq>` 随重解析变化；用 `sql_table_registry` 取名字。

### 2.3 电路

11. 按页分区**全有或全无**：一个实例缺 `Page Name` 就整体退化为位号前缀分组。
12. `query()` 只校验 `query_operation` 不分派；类型化读模型走 `typed_query()`。
13. 证据封顶不同（直接 0.80–0.99 / 图 0.88 / 语义 0.69）——**不要跨阶段比分数**。
14. 零欧电阻判定里的 `"0R,5%%"`（双百分号）是数据怪癖，别当笔误删掉。
15. 图文件存在但发布元数据 `failed` 时，图证据读取会被拒绝。
16. 器件身份解析**只做精确匹配**（refdes/identifier/alias/role），没有模糊/子串。
17. datasheet 链接读取时严格再校验（generation/部门/哈希/源版本），任一不符静默丢弃。

### 2.4 记忆

18. `ensure_memory_schema` 对 psycopg 连接**直接返回**：PG 下 DDL 必须由控制面 schema 预置，
    仓库里没有 `.sql`。
19. consent 授权字段有 SQL 触发器保护，**只有 `revoked_at` 可改**；不要尝试 UPDATE manifest 行。
20. 记忆去抖水位用 `(int(message_id), turn_id)` 复合排序；改排序会破坏「新消息覆盖旧任务」语义。
21. 反思模型调用超时**不能强杀**：`_reflection_in_flight` 会保持到回调触发，别以为线程池是干净的。

### 2.5 附件

22. 没有「隔离区」目录：上传即落地到会话资产目录，可读性由行状态控制。
23. `resolve_refs_for_turn` 会拒绝 `parse_status='failed'` 的附件；用户必须先 retry。
24. `attachment_edit` 的目标文本必须**唯一匹配**；歧义会拒绝而不是猜（这是设计目标）。
25. XLSX 修订会拒绝跨表引用/`INDIRECT`/`OFFSET` 公式；PDF 修订是**加注记**而非改字形。
26. `CHAT_ATTACHMENTS_ENABLED=false` 时所有附件端点返回 **404**（不是 403）。

### 2.6 文档生成

27. 代码库 docstring 里有一处「confrontational」拼写（`planning/` 注释），不影响功能。
28. `WorkOrder` 指纹 v1/v2/v3 有兼容窗口：**绝不能**把 `task_id`/`revision_id` 纳入冻结输入。
29. 计划路由要求三个字段齐全，部分绑定会抛 `PlanExecutionRouteError`——**不会**回退 legacy。
30. 已提交收据但缺持久化草稿 → `PlanExecutionRouteError`（崩溃恢复的硬约束）。
31. `idempotency_key` 冲突但载荷不同 → 明确报错，不是覆盖。
32. 渲染器只认注册区域与 `deterministic_only|validated_draft` 写策略；运行时无法获得新写入位置。
33. 受控文档类型（icd/fpt/requirements/specification）与修订**永不自动发布**。
34. `legacy_template_claims` / `harness_checkpoints` 旧表**永不删除或重写**（迁移只读它们）。

### 2.7 会话/并发

35. `failed` 是**可复活**的（SSE 掉线标记），`completed`/`cancelled` 才是终态。
36. `POST /turns/{id}/start` 生产可为 no-op；不带 `client_request_id` 的重复提交会产生两个 turn。
37. 附件等待超时（600s）与排队超时（600s）都会把 turn 置 `failed`，事件分别是
    `attachment_wait_timeout` / `chat_queue_timeout`。
38. 无 Redis 时跨进程 SSE 退化为 0.4s DB 轮询（同进程 worker 不受影响）。
39. PG 后端下 attempt/pointer 表（`hdb_checkpoint_attempts/pointers`）**尚未实现**；
    多实例 checkpoint 隔离还不完整（模块 docstring 已注明）。

### 2.8 平台与安全

40. 仓库根的 `.env` **已提交且含真实 API Key**，且有大量旧架构死变量
    （`RAG_BACKEND`、`PROVIDER`、`CUSTOM_LLM_MODEL`、`BM25_TOP_K`、`RERANKER_TYPE`、`CHUNK_SIZE`…，
    `settings.py` 已忽略）。**不要**把它当模板，用 `.env.example`。
41. `AUTH_DEFAULT_ADMIN_PASSWORD` 为空或 `admin123` 时**进程拒绝启动**。
42. `system_admin` 不能访问任何 KB 内容——这是合规铁律，不是 bug。
43. 默认 CORS 放行本地开发 origin；生产必须显式设置 `HDB_API_CORS_ORIGINS`。
44. Vite `allowedHosts` 硬编码 `.trycloudflare.com`（演示用），生产构建前应移除。
45. Grafana/Phoenix DB 的默认密码是 `change-me`；`HDB_TOKEN` 可选。
46. **没有 Alertmanager**：Prometheus 告警不会自动通知。
47. `docs/` 与 `CLAUDE.md` 是本地文件（gitignored）；本 Wiki 已迁至仓库根 `wiki/` 并纳入版本管理——
    新增/修改文档请写 `wiki/`，不要写回 `docs/wiki/`（该路径已不存在）。
48. `requirements.txt` 是 uv 生成的，**改依赖请改 `pyproject.toml`** 后 `uv lock`。
49. 仓库没有 Dockerfile/systemd/k8s/nginx 与 CI workflow；`deploy/` 只有可观测性栈。
50. `hardware-database-eval` CLI 不调用 `init_observability`（离线评估无外部遥测）。

---

## 3. 扩展指引

### 3.1 新增一种文件类型/管道

```
1. src/pipelines/registry.py：定义 PipelineSpec（processor_kind/content_kind/supported/rejected/
   stage/dataset_kind），用 PIPELINE_REGISTRY.register(...) 注册
   —— 注意扩展名不能与现有 spec 冲突（注册表会抛错）
2. 实现 PipelineHandler（existing_record_dataset_kind/submit 必填；process_record 若需后台解析）
3. src/pipelines/runtime_factory.py：构造并放入 handlers 映射
4. 若要后台解析：把 processor_kind 加进 PipelineRuntime.background_processor_kinds()
   （目前只有 spreadsheet_table）
5. 若要参与检索：实现工具（见 3.2）并加进 pipeline_catalog.scan_kb_sources
6. 补测试：路由、去重、归档、状态机、删除顺序
```

### 3.2 新增一个 Agent 工具

```
1. src/agents/tools/<name>_tool.py：make_<name>_tool(rt, service) 闭包；
   用 timed_tool_call 包裹（自动获得取消/计时/事件/governor 许可/fail-open）
2. 产出 Evidence 时保证 id 唯一、metadata 含 content_kind/processor_kind/locator
3. runner.py 注册（注意 wiki_assist / 附件作用域 / 文档开关等条件）
4. tool_assembly.TOOL_CAPABILITIES 登记能力名（否则统一模式被过滤）
5. TOOL_LABELS 加中文名（前端轨迹展示）
6. 若属叙述白名单之外，确认执行/叙述可见性符合预期
7. 测试：契约 + 越权拒绝 + 降级路径
```

### 3.3 新增导出格式

```
1. result_exports/models.py：EXPORT_FORMATS + 别名 + enabled 开关
2. renderers.py：实现 _render_<fmt>，返回 RenderedResult（content/mime/extension/preview）
3. _validate_artifact_payload 增加 MIME/签名/包校验
4. settings.py：RESULT_EXPORT_<FMT>_ENABLED + 加入 is_export_format_enabled
5. intent.py 的格式正则 + core/intent.py 的 _FORMAT_PATTERNS（若要自然语言触发）
6. 前端 ExportMenu 会自动从 /exports/formats 拿到新格式
7. 测试：渲染、载荷校验、意图识别、preview
```

### 3.4 新增文档生成渲染器/配方

```
1. renderers/ 实现渲染器（包校验 + 完整性清单）
2. planning/registry.py 注册 renderer capability（精确版本）
3. planning/recipes.py 加结构配方/profile 限制
4. models.py 的 output_format 枚举 + conversion 路径（如需要）
5. 模板消毒/校验（若是模板型）
6. 测试：结构渲染、包校验、绑定覆盖、评估指标
```

### 3.5 新增记忆类型

```
1. memory/schemas.py 定义 Pydantic 模型（extra="forbid"）
2. memory/prompts.py 加提取指令
3. namespace 仍用 candidate/verified 结构（CatalogAwareStore 会拒绝其它末段）
4. MemoryService 的 scope/状态机与列表视图适配
5. 测试：抽取、投影、consent、retrieval 与 formatter 边界
```

### 3.6 新增评估指标

```
1. cohorts.py：判断是否 RAGAS 指标 / 归属队列
2. 若是 RAGAS：ragas_adapter 的 STANDARD_METRICS / 字段映射 / 适用性 / 批量分组
3. 若是域指标：hardware_metrics.py + 在 service.score 里对 allowed+success 运行
4. gates.py：默认阈值（可选）+ 文档生成硬零集合（如适用）
5. presentation.py：中文标签 + 排序
6. 测试：schema 严格性、不适用、重试/失败语义、门禁退出码
```

### 3.7 把某个域迁到 PostgreSQL 控制面

```
1. 在 storage/control_plane/schema.py 添加核心表 DDL 与索引（或确认 loader 反射可用）
2. 提供一个 ExecutionStore 风格适配器或 PgSqlAdapterConnection 复用 store 逻辑
3. 用 loader.load_sqlite_snapshot 演练（dry-run → 报告 → apply）
4. 校验权威性断言与 schema_version
5. 补 @skipUnless(HDB_TEST_POSTGRES_DSN) 的契约测试（参照 test_control_plane_store_contract.py）
6. 更新 provider 校验（settings.validate_provider_configuration）与 /health/ready
```

### 3.8 新增前端页面

```
1. frontend/src/pages/<X>Page.tsx + api/types.ts 的 DTO
2. App.tsx 路由 + 守卫（KbContentRoute/AdminRoute）
3. AppSidebar 导航项（按角色）
4. 用现有 DataTable/StatCard/ConfirmDialog/ui/* 保持一致
5. 若是长任务：定义轮询节拍（参照现有表），或用 SSE
6. 测试：纯模型/组件的 renderToStaticMarkup 单测（无 jsdom）
```

---

## 4. 技术债与改进建议（按收益排序）

| 优先级 | 项 | 影响 |
|---|---|---|
| 高 | PG 后端补齐 checkpoint attempt/pointer 表 | 多实例下 attempt 隔离不完整，可能串扰 |
| 高 | 文档生成独立工作台页面（评审/纠错/修订/计划确认 UI） | 当前全部靠聊天文本，评审能力未释放 |
| 高 | `TOOL_CAPABILITIES` 补全（wiki_search / release / revision_history） | 统一工具模式下这些工具会被静默过滤 |
| 中 | `RESOURCE_GOVERNOR_ENABLED` 语义统一（去掉 `getattr(..., True)`） | 避免误判许可生效 |
| 中 | 附件存储加 PG/对象存储后端 | 附件仍是 SQLite-only，多实例靠共享文件系统 |
| 中 | 前端权限判断收敛（`\|\| isEmployee` 过宽） | 会显示无效的写入口 |
| 中 | `templateMappingCorrection` 接入 UI | 模板分析人工纠错能力未开放 |
| 中 | RAGFlow `source_group_router` 的 `weights` 要么使用要么删除 | 死配置容易误导 |
| 中 | 审计日志清理策略（当前永久） | 长期增长；需合规确认后再动 |
| 低 | 测试文件平铺 → 按子系统分组 | 307 个扁平文件检索成本高 |
| 低 | 前端引入 lint（ESLint/Biome） | 目前无静态检查 |
| 低 | 引入 Alertmanager / 告警路由 | 告警目前无人接收 |
| 低 | 移除 `src/circuit/vendor/spydrnet` 中的硬编码凭据脚本（`sch_parse/parse_wcca.py` 含 MSSQL 凭据） | 安全扫描会报；确认未被引用后删除或迁出 |
| 低 | 统一时间处理（后端无时区后缀） | 前端按 UTC 解析，跨时区显示风险 |

---

## 5. 变更守则（提交 PR 前自查）

1. **权限**：新端点是否拒绝 `system_admin` 访问内容？是否做了 `KbScope.require_department()`？
2. **作用域**：工具/服务是否接受调用方传入的 user/department/kb/路径作为授权输入？（**不应该**）
3. **幂等**：写操作是否有 `client_request_id`/`idempotency_key`？载荷冲突是否报错？
4. **租约**：长任务是否有 lease + fencing + 心跳 + 过期回收？
5. **哈希**：不可变对象（模板/计划/产物/快照/consent）是否在读取时重算校验？
6. **fail 语义**：这是「可用性问题」（→ fail-open + degraded）还是「安全问题」（→ fail-closed + 明确错误）？
7. **可观测**：是否加了指标（低基数）与结构化日志（脱敏）？是否避免把 id 放进指标标签？
8. **配置**：新增配置是否进了 `settings.py` 的 `DEFAULT_VALUES` 与 `.env.example`？
   是否区分「热重载」与「重启生效」？
9. **测试**：是否覆盖正常路径 + 越权路径 + 降级路径？是否需要 PG/Redis 门控版本？
10. **文档**：本 Wiki 对应页面是否更新？（`docs/` 目前不在 git 里，需团队决定是否纳入）

---

## 6. 快速定位表

| 想找… | 去哪 |
|---|---|
| 一次提问发生了什么 | `src/api/routes/query.py::_run_turn` → `src/agents/runner.py::stream` |
| 为什么走了文档流 | `src/core/conversation_orchestrator.py::plan` + `src/core/intent.py::classify_intent` |
| 为什么没有检索到 | `document_rag_tool` → `ragflow_backend.retrieve` → `_filter_chunks` |
| 为什么表格答错 | `spreadsheet_tools`（SQL 校验链）→ `table_store._sheet_semantic_rows`（语义行） |
| 为什么记忆没出来 | `memory/service.py::search`（candidate 语义索引不健康会跳过）+ consent/opt-in |
| 为什么附件被拒 | `attachments/service.py::upload`（配额/魔数）+ `resolve_refs_for_turn` |
| 为什么生成不出文档 | `document_authoring/service.py` 预检 + `harness/runtime.py::execute` 的降级原因 |
| 为什么任务卡住 | 对应 worker 的 claim/lease 表 + 心跳列 + `/system/status` |
| 配置默认值 | `src/settings.py`（`DEFAULT_VALUES`） + 本 Wiki [20](20-config-and-storage.md) |
| 表结构 | 各域 `_init_db`；PG 见 `src/storage/control_plane/schema.py` |
| 测试怎么写 | 找同域 `tests/test_<topic>.py`，用 `tests/_api_stub.py` 起真实 uvicorn |
