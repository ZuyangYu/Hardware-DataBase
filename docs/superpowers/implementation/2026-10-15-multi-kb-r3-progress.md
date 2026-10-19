# R3 实施记录

方案：`docs/superpowers/plans/2026-10-15-multi-knowledge-base-retrieval-authoring-human-review-plan-R3.md`。
基础提交：`3c00bd97d9d6de9d8b470b271bd50d0bdccf7d45`。

## 2026-10-16 会话接续

从会话 `01a13e99-03ab-7493-9d19-0236aac3d3c6` 恢复到 `feature/multi-kb-retrieval-r3`，接续 `f2de38e` 之后未提交的冲突服务/API 和前端测试。本节更新后续进度，下方 T0–T5 记录保留为原阶段的验收结果。

本轮工作：

- [x] 审查并接续原有未提交改动，保持原 feature worktree。
- [x] 共享 authoring SQLite/PostgreSQL 冲突存储、事项去重、版本锁、幂等正文检查与租户复查。
- [x] PostgreSQL 在读取幂等记录前锁定事项行；四个并发重试返回同一决策，无重复写入。
- [x] 设计候选进入 `approval_pending`；延期继续阻断；人工批准和自动发布都检查未决冲突。
- [x] 事项列表/详情/决策和产物预览/下载重新核验冻结来源；声明不能覆盖服务器来源的 KB、角色、审批状态和证据身份。
- [x] 冲突发现后 Harness 进入 `waiting_human` 并释放租约；该状态重试不消耗故障重试预算；有效决策可以幂等入队恢复原 HarnessRun。
- [x] Worker 从持久快照恢复整个选库集合，按每库本地记录 ID 和版本戳构建 `frozen` 检索契约；常规和放宽查询共享调用预算。冻结检索失败不当成零命中。
- [x] 来源复核使用实时授权，检查内容、源版本、修订、审批、索引代次和处理产物；证据按实际 KB/记录绑定，允许不同库内同名文件。
- [x] 保留旧单库快照哈希读取兼容；保存完整多库选库快照，恢复时以当前执行者重新授权。
- [x] 聊天状态卡接入来源冲突审阅：证据矩阵、候选/延期、处理后果、理由、历史、版本冲突和撤权清屏、网络失败使用原幂等键重试。
- [x] `knowledge_scope_changed` 的 409 恢复输入草稿；再次发送创建新会话，清除旧模板和附件绑定。
- [x] 新增经授权的产物审计 JSON 旁车下载，绑定产物哈希、来源快照、事项、决策、发布阻断项和执行审计 ID。
- [x] 旧 SQLite 冲突表先补列/回填 JSON 中的快照哈希，再建去重索引；重复初始化不删除历史。新增双后端 `--check/--apply/--verify` 脚本，默认只读，旧重复事项要求先人工核对。
- [x] 全量回归与最后补强的定向回归通过；本轮代码随本文提交。

验证结果：

- Python 全量：**3352 passed，200 skipped，60 warnings，52 subtests passed**，耗时 13 分 11 秒。未配置的外部服务套件保留跳过。
- 最后补强的权限/冲突/冻结执行/迁移/旧发布门禁：**62 passed，2 subtests passed**，包含实际目标库写权限用例和新增迁移脚本用例。
- 前端全量：**239 passed**；生产构建通过。
- 浏览器：**5 passed**，使用 API fixtures，覆盖完整 ChatPage 的审阅、待审批、409、撤权及重试交互。
- 真实临时 PostgreSQL：**31 passed，4 subtests passed**，覆盖 authoring、冲突并发重放和多库范围存储；迁移脚本真实 PG apply/verify/重复 apply 通过。
- Ruff、`compileall`、`git diff --check` 通过。临时 PostgreSQL 容器已清理。

复验入口：

```bash
uv run pytest -q
uv run pytest -q tests/test_authoring_conflicts.py tests/test_multi_kb_frozen_execution.py tests/test_multi_kb_conflict_migration.py
HDB_TEST_POSTGRES_DSN=<disposable-PG-DSN> uv run pytest -q tests/test_document_authoring_pg.py tests/test_multi_kb_store_contract.py
uv run python scripts/production_hardening/migrate_multi_kb_conflict_review.py --db <authoring.db> --check
uv run python scripts/production_hardening/migrate_multi_kb_conflict_review.py --backend postgres --dsn <authoring-DSN> --verify
cd frontend
npm test
npm run build
npm run test:e2e -- e2e/multi-kb-conflict-review.spec.ts
```

R3 尚未整体验收：

1. 冲突检测已接入受控硬件字段抽取器，并保留原文跨度、对象、条件和来源身份；固定合成集回归已补齐，独立人工标注的真实资料质量验收仍未完成，不能把字段规则抽取称为任意文档语义冲突识别。
2. 章节生成输入、计划回执和依赖失效/重算已接入并完成本地端到端验证；取消、迟到 Worker、多章节恢复以及正式审批系统仍需发布前演练。
3. 严格多源结构化适配已接入冻结文档生成路径，并完成同库混合处理器隔离和四库容量回归；真实结构化服务数据和生产容量仍需发布前验收。
4. 来源引用点击定位及真实 RAGFlow 双/四库已完成本地/隔离冒烟（见后续修复和真实资料记录）；独立人工标注相关性/冲突质量门槛、长稳负载及真实关闭/回滚演练仍待完成。
5. 审计附录仍保留独立 JSON/ZIP 旁车；内嵌审计章节下载已接入，但正式审批系统保持外部待审批边界。

本轮没有执行生产数据库迁移、修改运行环境配置、启用功能开关或部署。临时 PG 仅用于隔离测试。

## 2026-10-16 后续修复

- [x] 历史权限复核递归展开 `source_dependencies.inherited_turn_ids`；API 在读取、SSE、取消和重启入口加载父 turn，缺失或不完整依赖 fail closed，撤权继承来源后不再泄露派生正文。
- [x] 多知识库 turn 在 SQLite/PG 的完成与编辑路径跳过 `project_reflection` 长期记忆 outbox；单库自动提取行为保持不变。
- [x] 证据去重和字段预算按知识库/本地记录保留独立出处；相同正文跨库不会覆盖来源，字段上限先保证每个已命中 KB 的代表席位。
- [x] 生成产物审计 lineage 兼容旧测试替身/旧 artifact 行，仍对真实父产物递归执行来源授权和版本复核。

本轮新增/修复回归：多库、冲突、冻结、结构化、引用、文档生成相关 **465 passed，20 skipped，2 subtests passed**；Ruff 与 `git diff --check` 通过。此前隔离真实 RAGFlow 试跑的 7/7 固定锚点、四库融合及撤权边界结果见 [`2026-10-16-multi-kb-r3-live-acceptance.md`](./2026-10-16-multi-kb-r3-live-acceptance.md)。

R3 仍未宣称完成独立人工相关性/冲突标注、长稳负载、正式审批系统接入和真实迁移回滚演练；这些仍是 T5/T10/T11 的发布前证据。Office/PDF 内嵌审计章节已完成本地实现与回归，生产格式兼容性仍需发布前验收。

## 任务状态

- [x] T0：共享检索池生命周期与旧单库兼容。
- [x] T1：范围模型、稳定 KB ID 授权与独立上下文。
- [x] T2：会话/轮次范围、SQLite/PG 持久化与增量迁移。
- [ ] T3：API/Worker/Agent、历史依赖、记忆与撤权隔离。
- [ ] T4：scoped outcome、检索调度、预算、真实截止与错误语义（核心已完成，工具接线随 T3/T5）。
- [ ] T5：证据复合身份、融合、覆盖与质量门槛（融合/runtime 已完成，待 T3 工具接线与固定集质量验收）。
- [ ] T6：前端选库、范围变化恢复、引用与端到端验收。
- [ ] T7：结构化来源严格范围适配。
- [ ] T8：文档来源冻结与索引代次。
- [ ] T9：规划、预览、工单、Worker、恢复贯通。
- [ ] T9.5 A–E：关键冲突检测、策略、存储/API、审阅、等待/恢复及导出门禁。
- [ ] T10：章节事实/决策/审批校验与产物授权。
- [ ] T11：真实服务、固定评测集、负载、迁移/回滚与发布验收（真实检索冒烟、22 例固定冲突回归与本地门禁已完成；人工标注和其余发布证据待验收）。

## 2026-10-16 T10 决策重算补强

- [x] 人工决策的事项版本、决策 ID 和状态进入章节 review hash；受影响单元的旧 plan receipt 不再命中，依赖闭包按新输入重新执行，无关单元仍可复用。
- [x] 计划节点回执键改为由运行、节点、尝试次数、输入指纹和受控动作共同生成；同一 HarnessRun 在人工决策后产生新输入时不再触发固定 `node.action_key` 的唯一键冲突。
- [x] 新增回归覆盖：决策后受影响章节与下游章节重算、旧回执保留但不复用、Worker 恢复后写入新回执，以及独立单元 review hash 保持稳定。

本轮验证：T10 定向集合 **78 passed**；Python 全量 **3482 passed，202 skipped，60 warnings，52 subtests passed**；Ruff 与 `git diff --check` 通过。

这完成了 T10 的本地执行闭环，但不等于 R3 整体验收完成。人工相关性/冲突标注、长稳负载、真实双库/四库服务、正式审批系统和迁移回滚仍保留为发布前证据。

## 2026-10-16 T7 结构化适配补强

- [x] 冻结契约增加处理器维度；同一知识库同时含普通文档、表格、电路或外部对话记录时，结构化适配器只校验并读取自身处理器记录，仍验证完整冻结记录身份和版本戳。
- [x] 文档生成的多知识库冻结路径接入表格语义/单元格、电路关系和外部对话适配器；共享授权、来源快照和检索预算沿用多库服务，不再挂载锚点库单库工具。
- [x] 新增同库混合处理器隔离回归，以及四来源结构化 fan-out 的每源预算和来源审计回归。

本轮结构化及关联 authoring 回归 **46 passed**；全量 Python 回归 **3484 passed，202 skipped，60 warnings，52 subtests passed**；Ruff 与 `git diff --check` 通过。

## 2026-10-16 Office/PDF 内嵌审计章节

- [x] 增加受控内嵌表示层：DOCX 在正文末尾追加来源与决策章节，PDF 追加审计页，XLSX/XLSM 增加审计工作表，PPTX 增加审计页；原始产物字节和批准哈希保持不变。
- [x] 内嵌前后重新校验产物哈希、来源权限、冻结快照和冲突决定；状态变化会拒绝下载，不把旁车审计渲染结果当成正式批准产物。
- [x] 新增服务/API 下载入口 `/document-generation/artifacts/{artifact_id}/audit-document`，文档状态卡提供“含内嵌审计章节”下载；原有审计 JSON/ZIP 入口保持兼容。
- [x] 增加 DOCX、PDF、XLSX 内嵌内容、哈希篡改、服务授权和前端按钮回归。

本轮新增内嵌审计及相关 API/前端回归 **88 passed**；Python 全量 **3490 passed，202 skipped，60 warnings，52 subtests passed**，前端全量 **251 passed**；Ruff、前端构建与 `git diff --check` 通过。内嵌表示层仍属于评审/下载副本，不改变正式产物内容哈希或审批状态。

## 2026-10-19 T11 固定冲突回归与门禁

- [x] 新增 `evaluation/datasets/multi_kb_conflicts_v1.jsonl`：22 例原始合成声明，覆盖单位/范围/限值、条件/对象/修订、主库依据、规范审批边界、peer 对比、四库归组与注入文本。
- [x] 新增离线执行器 `scripts/production_hardening/multi_kb_conflict_eval.py`：真实字段抽取、临时持久化事项、写作输入和审批状态评分，逐例保存证据身份、原文跨度、预期/实测、数据集/代码哈希与实际开关。
- [x] 项目/工单库和产物目录显式隔离到临时目录；新增回归证明不会初始化默认部署存储。没有调用模型、在线检索或部署数据库。
- [x] 空集、重复 ID、来源错绑、丢失出处、漏抽取、错归组、漏掉独立关键字段和无复核人身份的已复核标签均可被拒绝或触发门禁失败。
- [x] 两种自动裁决模式独立 CLI 运行均 **22/22**，关键阻断漏放 **0**，误阻断案例 **0**，执行错误 **0**。
- [x] `--require-human-review` 独立 CLI 返回 **3 / insufficient_evaluation**；22 个代理编写标签均保留待独立人工复核，`release_ready=false`。本地通过没有被当成正式验收通过。

最终关联回归 **119 passed，7 subtests passed**；Ruff、`compileall` 与 `git diff --check` 通过。本轮未重新运行全量 Python/前端，也没有修改生产代码、`.env`、运行中的服务或功能开关。

复现说明：[`multi-kb-conflict-acceptance.md`](./multi-kb-conflict-acceptance.md)。独立命令报告保存在 `storage/evaluations/multi-kb-r3-conflicts-20261019/`，数据集 SHA-256 为 `30f439e9641ffec08f4aefee97288d90645117ff4f89cb7f379bf423654dea6b`。报告含执行代码哈希，未将运行存储目录加入 Git。

仍待：独立人工相关性/冲突标注、基于真实样本锁定质量阈值、长稳负载与部署 SLO、真实迁移恢复/回滚以及正式审批系统接入；R3 整体验收继续保持未完成。

## T0 交付与验证

- `initialize_retrieval_executor` 由 API 与 Worker 启动/运行时启用调用，复用已有单例池。
- `get_multi_kb_retrieval_executor` 对关闭、未初始化、初始化失败及关闭后状态拒绝访问。
- `MULTI_KB_CHAT_ENABLED=false` 独立于旧 `SHARED_RETRIEVAL_EXECUTOR_ENABLED`；四组合测试记录实际线程名，旧单库批量分支保持原开关控制。
- 多库启用但池不可用时就绪探针报告 `multi_kb_retrieval=down`。关闭遵循现有 deadline，运行线程未被强制终止。
- 修复 `close` 读取运行 future 集合时与完成回调的并发访问。
- 回归发现 Redis 就绪用例继承前一用例的缓存；用原提交的 health 模块复现后，仅在该测试类清理缓存。

验证命令：

```bash
uv run python -m pytest -q \
  tests/test_multi_kb_executor_lifecycle.py \
  tests/test_shared_retrieval_executor.py \
  tests/test_worker_roles.py tests/test_worker_slot_refill.py \
  tests/test_readiness_cache.py tests/test_control_plane_health.py \
  tests/test_provider_config_guard.py tests/test_health_redis.py
```

结果：**60 passed, 1 skipped**。跳过项为未配置 `HDB_TEST_POSTGRES_DSN` 的既有 PG 健康检查；不计为真实 PG 验收。

## T1 交付与验证

- 冻结的 Pydantic 来源/用途/选库快照模型：拒绝空列表、重复、非法 ID/角色、越限和客户端伪造身份字段；集合以 tuple 存储，以 JSON 数组输出。
- `selection_hash` 绑定版本、租户、操作者、归属库、用途和排序后的 ID/角色，不包含展示名称、时间和随机快照 ID。
- AuthService 提供按稳定 ID 查询真实库身份的公开接口；resolver 整体授权，拒绝未知、跨部门、私有他人库、停用用户、系统管理员及篡改/跨用户/跨租户快照。
- resolve/revalidate/context 边界重新读取用户与权限。撤回共享或改为系统管理员后旧快照失效。
- 每来源 context 只含该来源的权限，metadata/权限集合彼此独立；API helper 委托同一服务层，来源角色不赋权。
- answer/compare/compose 的 1～4 库模型与授权行为已测试；这只表示底层契约支持，不表示对应业务入口已开放。

```bash
uv run python -m pytest -q tests/test_knowledge_scope.py tests/test_auth_kb_scope.py tests/test_kb_scope.py
```

连同上述 T0 套件的最终合并回归：**121 passed, 1 skipped**。新增 T1 用例 42 个。
修改的 Python 文件通过 `ruff check`，补丁通过 `git diff --check`。

## 阶段边界

T0–T2 不等于多库问答可用。新入口保持关闭；当前未执行生产迁移、未修改运行环境 `.env`、未重启线上 API/Worker。M1/M2/M3/M4/M5 均未验收。

## T2 交付与验证

- 两后端保存会话选库 JSON/hash、轮次授权快照/hash 和请求 fingerprint；ChatSession/ChatTurn、列表、Worker claim 和 PG facade 保留字段，旧行默认无范围。
- 首轮范围绑定与轮次/两条消息在同一事务；并发选择只有一组成功，已有旧单库历史不能原地附加多库范围。
- 新协议幂等键绑定问题、范围、附件范围/ID、文档上下文、模式和 Wiki 开关；授权时间和随机快照 ID 不影响重试。旧单库重放行为兼容。
- PG 控制面 schema 升至 2；JSON 后缀列自动沿用 loader 的 JSONB 适配，不批量伪造历史 KB 身份。
- 迁移脚本 `migrate_multi_kb_scope.py` 提供互斥 `--check/--apply/--verify`，默认只检查。SQLite 迁移显式事务覆盖 DDL；PG 使用事务锁及版本检查，拒绝未来版本，重复应用不修改记录/迁移时间。
- 一次性 PG 容器：`hdb-multikb-r3-test-20261015`，仅用于本次验收。所有数据库为随机命名临时库，测试完成后各自删除。

回归结果：**130 passed，0 skipped**，包含真实 PG scope round-trip、重新连接、并发首轮、旧 schema 迁移、既有控制面 Store/facade/loader、记忆入队、附件范围和权限测试。

```bash
HDB_TEST_POSTGRES_DSN=<一次性PG> uv run python -m pytest -q \
  tests/test_multi_kb_store_contract.py tests/test_control_plane_store_contract.py \
  tests/test_control_plane_schema.py tests/test_control_plane_postgres.py \
  tests/test_control_plane_health.py tests/test_control_plane_conversation_service_postgres.py \
  tests/test_control_plane_memory_enqueue_postgres.py tests/test_chat_attachment_scopes.py \
  tests/test_control_plane_factory.py tests/test_knowledge_scope.py tests/test_conversation_wiring.py
```

## T4 检索核心交付与验证

- 新增 `DocumentRetrievalContract`、真实版本戳、结构化 outcome，沿用旧 retrieve 的台账/远端请求/回读实现，旧 list 返回契约继续兼容。
- 普通及 strict 回读补齐台账记录 ID、真实 KB/部门、源文件哈希、原片段文本哈希、定位、版本及索引代次；远端同名身份字段被服务器覆盖。
- chat 远端过滤拒绝可有限回退，始终受本地白名单约束；frozen 禁止宽回退，范围能力/台账映射/文件变化/权限问题不伪装为零命中。检索结束再次验证来源版本。
- `RetrievalBudget` 原子预留每次实际内容 HTTP 调用；回退、零命中重试共享同一预算，HTTP timeout 不超过剩余总时间。
- 多查询 × 多库只展开一次，由已有进程池调度；相同池内嵌套 map 明确拒绝。队列准入改为原子预留，直接 submit 也受队列容量控制。
- 执行截止与旧排队截止分开；超时返回后不合入迟到结果，真实运行结束前不释放槽位。默认无执行截止的旧调用保持原行为。
- 选库授权在调度、逐库调用和主线程合入边界复查；失权终止整次范围。显式空 frozen 映射不转为全库 chat。

回归：**81 passed**，包括新增 scoped outcome/多库调度/截止、既有 RAGFlow 过滤回退与取消、共享池生命周期和文档证据提供器。工具委托、runtime 失效标志和证据融合继续在 T3/T5 接通；P0 尚未对外开放。

## T5 证据核心交付与验证

- 完整复合身份包含租户、部门、KB、文件记录、原文件哈希、原片段哈希、处理产物、索引代次、远端文档与稳定定位。缺少或篡改身份不猜测补齐。
- 多查询按 RRF(k=60) 融合同一身份，单查询重复行不重复加分；相同文本在不同 KB/文件/代次保留独立出处。
- 对合格候选先预留来源覆盖，再填充剩余条数/上下文预算；主库优先获得必要席位。低质量片段不用于凑覆盖，裁剪后重新计算 coverage/complete。
- 为被预算裁剪的候选保留完整身份引用，不缓存越权原文。默认 token 计数是渲染文本的保守 UTF-8 上界，可注入模型 tokenizer；不宣称是供应商实际用量。
- 多库 ToolRuntime 为尚未覆盖的选库保留席位，尊重融合顺序；finalize 不重排已交给模型的引用编号。来源标题包含实际 KB、文件及版本/指纹。

回归：**71 passed**（融合、引用、共享池、文档证据、现有 reranker/检索预算/日志证据）。固定人工标注集的相关性/覆盖门槛、历史引用与前端点击定位仍待执行；尚未标记 T5 整体验收完成。
