# 23 · 可靠性与持续验证实施计划

日期：2026-10-08。基线：`develop` / `62a85a7`。

## 目标与实施顺序

把代码审查中确认的问题变成可重复验证的修复，并把这些验证加入发布流程。

| 阶段 | 工作 | 验收标准 | 状态 |
|---|---|---|---|
| P0-1 | 统一日志、Span 属性与异常事件的凭据脱敏 | 内存 OTel exporter 中没有测试凭据；Bearer、JSON、URL 和异常堆栈用例通过 | 已完成并验证 |
| P0-2 | 备份恢复失败回滚 | 解包、复制 dump、PG 恢复失败后文件回滚且服务不带不一致数据启动；成功路径可恢复原服务 | 已完成并验证 |
| P0-3 | PostgreSQL attempt/pointer 与跨进程提交互斥 | PG 与 SQLite 同契约；旧 attempt 不能改写 canonical；隔离初始化失败不会执行可见 thread | 已完成并验证 |
| P0-4 | 部署配置与热重载优先级 | Docker 注入的部署项在普通配置保存后保持有效；需要重启的运行时保持启动值 | 已完成并验证 |
| P1-1 | 健康检查与告警口径 | 401/403/404/429 不报正常；LLM 探测携带鉴权；低流量告警、无流量与用户取消用例通过 | 已完成并验证 |
| P1-2 | 秒级直方图和通知接入说明 | SDK 导出的桶适配亚秒到分钟延迟；通知配置可验证且凭据来自部署环境 | 已完成并验证 |
| P1-3 | 测试隔离与自动 CI | 无本地 `.env` 可以运行回归；CI 覆盖锁文件、lint、后端、PG/Redis、前端和告警规则 | 已落地，本地检查通过 |
| P1-4 | 回答 grounding 指标语义 | 不把“有证据/有引用编号”标为语义验证通过；无效引用与未引用段落有回归用例 | 已完成并验证 |
| P2-1 | 前端按路由拆包、同步运维文档 | 前端测试与构建通过；旧的 Alertmanager、Redis、备份和 checkpoint 描述与实现一致 | 已完成并验证 |
| P2-2 | 真实业务与容量验收入口 | 可执行的测试说明覆盖浏览器恢复、真实文件、混合负载、故障接管与质量回归 | 独立部署、文件/浏览器/双副本/故障验证已执行；长时容量与业务质量门槛待执行 |

## 验证分层

1. **每次提交**：脱敏内存导出、恢复故障注入、配置子进程回归、SQLite checkpoint、探针响应、引用诊断、前端单测和构建。
2. **集成环境**：使用一次性 PostgreSQL 与 Redis，执行 checkpoint/控制面/配额/实时事件契约与并发测试。测试实例与业务数据隔离。
3. **目标部署**：真实 API/Worker/RAGFlow/模型与文件夹具；验证 SSE 断线重连、刷新、重复提交、取消、权限变化、文档修订和产物下载。
4. **容量验收**：预热 5 分钟，混合负载 30 分钟 × 3，2 倍过载 5 分钟后恢复，24 小时持续运行。冻结模型、配置、到达率与数据集指纹。

## 发布判断

- 关键修复以回归用例和实际执行结果为准。
- 多实例必须验证单 owner、旧 attempt 拒绝、重复终态/产物为零，以及跨副本 checkpoint 恢复。
- 容量报告同时记录 e2e、TTFT、排队 P95/P99、接受后失败率、主动拒绝和过载恢复时间；单机短时聊天基线不作为混合业务容量结论。
- 回答质量参考事实由业务确认。生产失败案例先保存可追溯上下文，再补人工参考答案并加入固定评测集。

## 执行记录

### 实现位置

- `src/observability/{privacy,logging,tracing,bootstrap}.py`：OTLP/local 日志、Span/exception/status/link 脱敏；采集开关仍有效；秒级直方图与父级采样连续性。
- `scripts/backup.sh`：故障时文件回滚、保持停止；成功时恢复原运行服务。
- `src/agents/checkpoints/{attempts,postgres_provider}.py`、`src/api/routes/query.py`：PG 共享指针、跨进程锁与初始化失败终止；`src/storage/control_plane/budget.py` 纳入指针短连接峰值。
- `src/settings.py`、`deploy/docker/hdb.env.example`：重启项保持运行时值、Docker 配置对齐；MCP 凭据轮换按原声明重启生效。
- `src/observability/health.py`、`deploy/observability/prometheus/`：2xx 与鉴权探针、真实请求量分母、取消语义及告警回归。
- `deploy/observability/{alertmanager,docker-compose.alerting.yml}`：环境文件提供 URL/token 的通知模板与覆盖配置。
- `tests/conftest.py`、`.github/workflows/ci.yml`：收集前隔离、一次性 PG/Redis 的后端回归、前端和规则检查。
- `src/agents/runner.py`、`src/evaluation/ragas_adapter.py`：引用诊断与语义校验分开，RAGAS 继续接收有效引用上下文。
- `frontend/src/App.tsx`：路由懒加载和加载占位；相关 Wiki 已同步。

### 2026-10-08 验证结果

| 验证 | 命令 / 条件 | 结果 |
|---|---|---|
| 全量后端 | `HDB_TEST_POSTGRES_DSN` 与 `HDB_TEST_REDIS_URL` 指向一次性 `pgvector/pgvector:pg17`、`redis:8-alpine`；`uv run --no-sync python -m pytest -q --disable-warnings -ra` | **3093 passed, 7 skipped, 43 subtests passed**；676.51 秒 |
| 前端 | `npm test` / `npm run build` | **27 文件 / 206 测试通过**，构建通过 |
| JS 拆包 | 相同项目构建 | 主包 **880.81 → 406.42 KB**；gzip **257.71 → 130.35 KB** |
| 静态 / 锁文件 | `uv run --no-sync ruff check .`、`uv lock --check`、`bash -n scripts/backup.sh`、`git diff --check` | 全部通过 |
| 告警规则 | `promtool check rules` / `promtool test rules`（Prometheus v3.5.0） | **15 条规则有效**；低流量、无流量、取消和取消不稀释错误率 4 场景通过 |
| 通知 | Alertmanager v0.28.1 + 本地临时 webhook；URL/token 文件，缩短分组等待 | **Bearer 鉴权、firing、resolved 均验证送达**；两套 compose 合并配置通过 |
| 数据集 | QA CLI validate；附件 E2E loader | `hardware_qa_v1.jsonl` 合法；附件集 50 条可加载 |

7 个跳过项：2 个真实模型 smoke、1 个未入库电路评测集、3 个外部模板夹具用例、1 个真实 ICD
夹具回归。首轮全量有 1 个旧 MCP 热重载预期失败，调整为已有重启契约后，最终全量通过。
以上为首轮结果。本轮真实业务结果记录在下节；实际模型质量评分和长时容量仍按目标部署验收。

### 真实部署补充修复（2026-10-08）

- `src/storage/bootstrap.py` 补齐全新控制面的记忆、导出表/索引/外键，旧 core-only 库可幂等补齐；
  `domain_pg.py` 的 SQLite 数据保全检查允许辅助文件中尚无主表的全新部署。
- `src/attachments/coordinator.py` 改用两种后端共有的 waiting-turn 接口；
  `factory.py` 让进程内 ConversationService 共用连接池，关闭后可重建，避免逐请求创建 pool。
- `src/agents/checkpoints/fork.py` 复制 DeltaChannel 重建所需的祖先和 writes；
  `postgres_provider.py` 串行化 pool/schema 初始化，运行期仍保留每个 graph 的独立 saver lock。
- `query.py` → `AppPipeline` → runner 显式传递会话 ID；附件 ACL、文档恢复/收据不使用 attempt thread。
- `src/result_exports/store.py` 按 JSON 内容比较不可变快照，避免 PostgreSQL JSONB 的空白规范化导致重复导出失败；
  `session_cleanup.py` 先删除修订外键边和编辑提案，再删除附件，清理事务可重试。
- PostgreSQL stale-turn 恢复采用 `CHAT_TURN_HEARTBEAT_TTL_SECONDS`，与 SQLite 保持一致（最小 30 秒），
  修复设置较短 TTL 后仍等待硬编码 90 秒的问题。
- 前端刷新恢复当前标签页已选会话，按用户名/KB 隔离，并验证保存的 ID 仍有服务端授权；
  登录“新对话”与首条发送的并发建会话合并为同一请求，晚到的建会话不再抢走选择或清空乐观消息。
- 新生成的部署 manifest 记录 `git_commit` 与 `source_sha256`（含未提交文件），验收报告可追溯到源码字节。

### 2026-10-08 真实业务验收结果

| 验证 | 条件 | 结果 |
|---|---|---|
| 全量后端（补充修复后） | 一次性 `pgvector/pgvector:pg17`、`redis:8-alpine`；`uv run --no-sync python -m pytest -q` | **3107 passed, 7 skipped, 43 subtests passed**；715.98 秒 |
| 附件修订 E2E | 独立 PG/Redis/API/Worker + 真实模型与 RAGFlow；隔离 KB `acceptance` | **6/6 通过**（MD/TXT/DOCX 替换、子串替换、待审、审核发布） |
| 混合负载冒烟 | 聊天/解析/导出，0.2 req/s，预热 10s + 稳态 120s + 2 倍过载 30s + 回落 30s | **44/44 成功，0 接受后失败、0 限流、0 服务端拒绝、0 清理失败**；e2e P95 6.05s，排队 P95 1.68s，TTFT P95 10.07s |
| 双副本接管 | 独立部署 + 第二 API 副本；确认输出后 SIGKILL 主进程组并由新 Worker 接管 | **9/9 检查通过**；接管 39.8s，历史上下文与唯一终态保留，旧 attempt 写入被拒 |
| 故障隔离 | 依次终止 authoring/parse/chat Worker；不可达 RAGFlow 的第二副本 | **全部场景完成**；通用聊天与 API 不中断；未触碰共享 RAGFlow |
| 浏览器回归 | Chromium 真实登录；断网/刷新/重复提交/SSE 游标/取消/跨 tab 退出 | **连续两轮 3/3 通过**；失败时保留 trace 与截图 |
| 静态 / 锁文件 | `ruff check .`、`uv lock --check`、`bash -n scripts/backup.sh`、`git diff --check` | 全部通过 |

未执行：24 小时长稳态与 2 倍过载的正式容量报告、业务确认的语义质量评分、真实收件系统的
firing/resolved 送达。混合负载中的文档生成类需要部署专属模板/工作单载荷，短时冒烟未包含该类。

真实验收报告根目录：`storage/evaluations/reliability-live-20261008-6/`；
双副本报告在 `storage/evaluations/reliability-replicas-20261008-3/`，
故障隔离报告在 `storage/evaluations/reliability-faults-20261008-1/`。
报告包含真实调用结果；业务状态来自一次性 PostgreSQL/Redis，文件与账户随部署退出清理。

### 部署生效

更新应用后重启 API/Worker，以加载新的 provider、秒级桶及重启配置策略。
PG 检查点会建立共享 attempt/pointer 表；扩容前用连接预算脚本重新核算短连接峰值。
通知模板可直接接入部署环境的 URL/token 文件；真实收件系统验收需同时确认 firing/resolved。
线上最近一次配置保存的后端/endpoint 等重启项，以进程注入值及启动时生效配置为准。

## 真实部署验收入口

### A. 实际文件修订与产物

使用隔离测试部署，配置真实 API/Worker/RAGFlow/模型与有写权限的测试 KB。
`acceptance_deployment.py` 仅复制 `.env` 的模型/RAGFlow 连接设置，管理独立容器、配置、用户与文件：

```bash
uv run --no-sync python scripts/production_hardening/acceptance_deployment.py \
  --limit 6 --output storage/evaluations/acceptance-files
# 后续浏览器/混合负载复用同一个独立部署；退出时终止进程并删除容器及 active.env。
uv run --no-sync python scripts/production_hardening/acceptance_deployment.py \
  --serve --port 8214 --output storage/evaluations/acceptance-live
```

附件 E2E runner 通过公开 HTTP API 建立身份、读取带游标的 SSE 审计事件和执行清理，支持 PG/SQLite。
复用已有部署时设置 `HDB_E2E_DEPARTMENT_ID`、`HDB_E2E_KB` 与管理员环境凭据；
输出错误包含真实回答、调用工具和解析状态，清理失败也会让最终退出码非零。

```bash
HDB_E2E=1 uv run hardware-database-e2e \
  --base-url http://127.0.0.1:8001 \
  --dataset evaluation/datasets/attachment_editing_v1.jsonl \
  --output storage/evaluations/reliability-e2e --min-pass-rate 0.9
uv run hardware-database-eval validate --dataset evaluation/datasets/hardware_qa_v1.jsonl
uv run hardware-database-eval --help
```

固定数据集覆盖真实文件的修改、父/子版本、放行与审计链。硬性条件：越权发布、修改错
父版本、重复终态/产物为零。质量 gate 使用已人工确认的参考答案；不以编号存在代替事实支持。
每次保留模型名/版本、数据集 SHA-256、配置 revision、git commit 和完整报告目录。

### B. 浏览器业务回归

```bash
cd frontend
npx playwright install chromium
HDB_E2E=1 HDB_BROWSER_API_URL=http://127.0.0.1:8214 npm run test:e2e
```

独立部署预置 `acceptance_browser`；默认密码见部署 manifest。
`frontend/test-results/acceptance.json` 保存结果，失败时保存 trace 和截图；CI 校验用例可以收集。

| 场景 | 操作 | 验收 |
|---|---|---|
| 路由首次加载 | 登录后打开 chat、资产、配置、评估与扩展页 | 页面正常，按路由获取 JS，失败可刷新恢复 |
| SSE 断线 | 回答期间断开网络 10s，再恢复 | 相同 turn 重放，消息不重复、不丢最终状态 |
| 刷新与重登 | 回答期间刷新；另一个 tab 登出 | 能恢复正在运行的 turn；失效 token 回登录 |
| 重复提交 | 同一 client_request_id 重发；修改载荷重发 | 同载荷复用；不同载荷拒绝 |
| 取消与接管 | 取消生成；测试机终止持有任务的 Worker 后恢复 | 取消不计失败率；新 owner 恢复；旧 owner 权威写入拒绝 |
| 附件/权限 | 上传真实 DOCX/XLSX/PDF；运行期间撤销 KB 权限 | 各格式有正确解析状态；撤权后不给旧上下文新能力 |
| 修订与下载 | 多轮修订、放行、导出，检查下载文件 | 文件字节/父版本/审计链相符，产物仅提交一次 |

### C. 容量与故障验收

先冻结到达率和 workload：建议聊天 60%、附件解析 20%、文档生成 10%、导出 10%。
`mixed_load.py` 按固定到达率运行聊天、真实附件解析、导出，以及配置了模板/工作单引用的文档生成。
`evaluation/workloads/reliability-smoke.json` 是聊天/解析/导出三类短时冒烟；正式四类 workload 需补齐
`document_kb` 和 `document_payload`（`template_version_id/document_schema_id/document_schema_version` 等）。
缺少文档引用时拒绝启动该类 workload。报告记录 job/turn ID、文件 SHA-256、数据集指纹、模型选择器、
git commit/config revision，以及接受后失败、客户端限流、服务端拒绝和排队/TTFT/e2e 分位数。
各阶段使用连续的固定时间轴，poll/SSE 不阻塞到达调度；文件内容在启动时冻结。
测试账户凭据使用 `HDB_PROBE_USER/HDB_PROBE_PASSWORD`。

```bash
uv run --no-sync python scripts/production_hardening/mixed_load.py \
  --base-url http://127.0.0.1:8214 --workload evaluation/workloads/reliability-smoke.json \
  --model-reference custom/ark-code-latest --warmup-seconds 10 --duration-seconds 120 \
  --overload-seconds 30 --recovery-seconds 30 --rate 0.2 --max-inflight 4 \
  --output storage/evaluations/reliability-mixed-smoke
uv run --no-sync python scripts/production_hardening/live_replica_drill.py \
  --output storage/evaluations/reliability-replicas
uv run --no-sync python scripts/production_hardening/acceptance_deployment.py \
  --fault-isolation --output storage/evaluations/reliability-faults
uv run python scripts/production_hardening/connection_budget.py --help
```

稳态执行 3 次；再以 2 倍到达率运行 5 分钟并回落；长稳态执行 24 小时。
记录接受后失败率、主动拒绝率、TTFT/排队/e2e P95/P99、数据库连接峰值、Worker 接管时间、
重任务最大等待和过载恢复时间。验收阈值由目标部署容量和实际模型 SLA 固定在测试报告；
数据一致性条件（单 owner、终态与产物唯一、租约 fencing 有效）必须全部满足。
`live_replica_drill.py` 验证跨副本文件字节、并发幂等提交、SSE 唯一终态；在真实输出后 SIGKILL
主副本/Worker，再由另一 Worker 接管，并检查前轮暗号仍存在。故障隔离入口分别终止 authoring/parse/chat
Worker，配置不可达的 RAGFlow endpoint，检查通用聊天和 API 可用性；不操作共享 RAGFlow 服务。

### D. 持续质量回归

把脱敏后的生产失败样例按“无答案、错误引文、证据不足、事实冲突、单位转换、跨文件约束”分类；
保存证据与版本信息，由业务确认参考事实后加入 `evaluation/datasets/`。
每次模型、提示词、检索阈值变更均用同一数据集复评，并与历史基线对比。
当前在线指标只报告词法引用诊断，语义 grounding、充分性与冲突标为未评估。
