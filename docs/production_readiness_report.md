# 硬件数据库平台 · 多部门多用户上线全面优化报告

> 审计日期：2026-09-08 · 审计范围：全仓库（src/、frontend/、deploy/、tests/、docs/、git 历史）
> 方法：四路并行代码审计（安全与权限 / 数据与后台任务 / 查询与 LLM 链路 / 运维可观测与工程化），所有结论均带 `file:line` 依据，逐条对照源码验证。
> 严重级：**P0 = 上线阻塞** · **P1 = 上线前必须处理** · **P2 = 上线后迭代**

---

## 一、总体评估

**代码内部质量高，工程外围缺失。** 权限模型、审计哈希链、Durable turns 事件日志、失败回滚、隐私脱敏遥测等核心设计已达生产水准；但"围绕代码的一切"——并发能力、部署形态、备份、告警路由、CI——目前为零或接近零。

| 维度 | 评级 | 一句话结论 |
|---|---|---|
| 认证与权限 | ★★★★☆ | 路由覆盖完整、无未授权端点；密码策略与代理 IP 识别有缺口 |
| 数据层 | ★★★☆☆ | WAL 齐全、turn 模型设计好；单 worker 串行 + 无备份 + 无界增长 |
| 查询/LLM 链路 | ★★★☆☆ | fail-open、取消、重试健全；**无任何并发/成本闸门** |
| 部署形态 | ★★☆☆☆ | 唯一"部署脚本"是 nohup + Vite dev server，无 Docker/systemd/nginx |
| 可观测 | ★★★☆☆ | OTel 三信号 + Grafana 栈齐备；告警不路由、日志不轮转 |
| 工程化 | ★★☆☆☆ | ~1261 个测试零 CI 执行；eval 门禁无消费方；docs/ 被 gitignore |

**多部门多用户场景下的三大致命约束：**
1. **全网同时只能跑 1 个查询**（worker 串行执行 turns）；
2. **无并发/限速/配额**（legacy `/query` 每请求裸开线程跑完整 agent loop）；
3. **无备份**（`storage/` 是部门、用户、权限、审计的唯一副本，盘坏即全失）。

**已验证的良好基础**（无需改动）：全部 100+ 路由鉴权覆盖无遗漏；`reject_system_admin_kb_access` 每个内容路由都在；会话/turn 归属 SQL 级隔离无 IDOR；`GET /config` 密钥打码；遥测隐私脱敏层；启动时 `.env`/DB chmod 600；sqlglot 只读校验 + 表白名单的表格 SQL；全路径遍历防护（kb_paths / CircuitStore / archive）；默认 admin 拒绝 `admin123`（fail-closed）；停用/改密即吊销全部会话；durable turn 事件日志 + seq 重放；摄取内容哈希去重 + 失败回滚；电路发布事务化 + 快照回滚；memory 子系统的 lease/generation 围栏是全仓库最佳队列范式。

---

## 二、P0 — 上线阻塞项（不解决不能开服务）

### P0-1 查询吞吐 = 全部署 1 并发
- **现状**：`src/workers/main.py:75-88` 把 `list_pending_turn_work(limit=8)` 拿到的 turns **串行逐个**执行，`_run_turn`（`src/api/routes/query.py:222-513`）阻塞至 agent loop + LLM 流结束；parse 任务还要等本批 chat turn 全部跑完才处理（`workers/main.py:93-107`）。`POST /turns/{id}/start` 生产路径是 no-op（`query.py:562-565`）。
- **风险**：一个 30–120s 的慢查询让所有部门排队，有效吞吐 ≈ 0.5–2 turn/min；Excel 解析队列同被阻塞。
- **修复**：
  1. worker 内加并发池——`claim_turn` 已是 `BEGIN IMMEDIATE` 原子认领 + worker_id 校验 + retry<3（`conversation.py:803-867`），天然支持 N 线程并发，改造成本低；
  2. chat turn 与 parse claim 拆成两个循环/线程；
  3. 新增 `WORKER_TURN_CONCURRENCY` 配置（建议初始 4–8）；
  4. parse claim 目前**无 lease token 围栏**（对照 memory 子系统 `lease_owner/lease_token/lease_expires_at` 范式，`memory/catalog.py:316-331`），扩到 N worker 前必须补围栏，否则 30 分钟僵死解析会被双跑。

### P0-2 无并发闸门 / 限速 / 成本上限
- **现状**：全仓库唯一信号量在 eval 路径（`src/evaluation/ragas_adapter.py:1398`）。turns 模型无每用户并发限制（`conversation.py:638-717`）；live `.env` 的 `AGENT_CUSTOM_MAX_TOKENS=60000` × recursion_limit 36（`runner.py:673-678`）。（历史遗留的直连 `POST /query`——每请求裸开线程跑完整 agent loop——已于 2026-09 删除，该无上限路径消失。）
- **风险**：一个泄露 token = 无限并行 60k-token agent loop，直接烧穿 LLM 配额。
- **修复**：API 层加全局信号量 + 每用户并发上限（如全局 8 / 每用户 1–2）+ 令牌桶限速；`_run_turn` 加整体 wall-clock deadline（当前最坏 36 轮 × 120s 无上限）。

### P0-3 无可部署制品与进程管理
- **现状**：无 Dockerfile、无应用侧 compose、无 systemd、无 nginx 配置、无 Makefile（全仓库验证）。唯一脚本 `scripts/hdb.sh` 用 nohup + `fuser -k` 杀端口占用者（`hdb.sh:103,132`）。
- **风险**：进程崩溃无人拉起、开机不自启、无灰度回滚。
- **修复**（二选一落地）：systemd 三单元（api / worker / memory-worker）+ nginx；或 docker-compose 全栈。nginx 必须承担：TLS 终结、SSE 透传（`proxy_buffering off` + read timeout ≥ 300s）、前端静态服务（P0-4）、`X-Forwarded-For` 透传（配合 P1-2）。

### P0-4 前端"生产部署"实际是 Vite dev server
- **现状**：`hdb.sh` `start_front` 直接跑 `./node_modules/.bin/vite --host 0.0.0.0`（`hdb.sh:85-87`），且 `vite.config.ts` `allowedHosts: ['.trycloudflare.com']`；`frontend/dist/` 已构建但没有任何东西服务它。
- **修复**：`npm run build` → nginx 静态托管 `dist/`，`/api` 反代后端（`frontend/src/api/client.ts:3-11` 已按同源设计，前端零改动）。

### P0-5 零 CI：~1261 个测试从不自动运行
- **现状**：141 个测试文件 / ~1261 个用例，但无 `.github/`、无任何 CI 配置；`--fail-on-threshold` eval 门禁（`src/evaluation/gates.py:84-89`）明确为 CI 设计却无 CI 可跑；ruff 亦无人执行。
- **修复**：CI 三 job：`uv run ruff check .` → `uv run python -m pytest` → eval gate（选配）。这是后续一切改动不回退的唯一保障。

### P0-6 无备份机制
- **现状**：全 `src/` 无任何 backup/VACUUM 代码；`storage/`（auth.db：用户/会话/审计链、pipeline_documents.db、各 KB table_indexes）是唯一副本；WAL 模式下裸拷 `.db`（不带 `-wal`）不安全。
- **修复**：每日 `sqlite3 .backup` 热备脚本 + 异机同步 + **一次完整恢复演练**；RAGFlow 侧 MySQL/ES/MinIO 同样纳入备份面。

### P0-7 git 历史中的真实 API Key
- **现状**：`.env` 现已 gitignore，但初始提交 `ae4c979` 与 `ba930e1` 曾提交含真实 key 的 `.env`（含 `sk-or-v1-...` OpenRouter key），可从历史对象库恢复。
- **修复**：**上线前轮换全部相关 key**（RAGFlow / OpenRouter / SiliconFlow）；有条件则 `git filter-repo` 清洗历史。这是凭据卫生事故，不是可选项。

---

## 三、P1 — 上线前必须处理

### 安全类

| # | 问题 | 现状与依据 | 修复建议 |
|---|---|---|---|
| P1-1 | 创建用户无最短密码长度 | `_create_user_record` 只查非空（`auth.py:420-421`），API schema 无 `min_length`（`schemas.py:559-563`）；重置路径却强制 ≥8（`auth.py:705-706`） | 创建/重置统一 ≥8 位（建议 10 位 + 弱口令黑名单） |
| P1-2 | 登录锁定基于裸 socket IP，无 X-Forwarded-For 处理 | `login_attempts` 按 `username\|client_ip`（`auth.py:441-442`），`client_ip = request.client.host`（`routes/auth.py:19`）；全仓库无 ProxyHeaders 处理 | 上了 nginx 后全员共享一个 IP：5 次失败把合法用户一并锁死（自我 DoS），攻击者换用户名即绕过（密码喷洒无防护）。uvicorn 加 `--proxy-headers --forwarded-allow-ips`，锁定改「每账号全局阈值 + 每(用户,IP)阈值」双轨 |
| P1-3 | 无 TLS、无安全响应头 | `uvicorn.run` 无 ssl 参数（`app.py:243-249`）；无 CSP/HSTS/X-Frame-Options 中间件；token 存 localStorage（`frontend/src/auth.ts:3,13,28`）无 CSP 兜底 | TLS 由 nginx 终结（P0-3）；补一个安全头中间件；未来迁 cookie 会话需重审 CSRF |
| P1-4 | 端口漂移 | `.env` 8002 / `app.py:248` 默认 8003 / `hdb.sh:10` 8002 / vite 8003；前端 5175 vs `hdb.sh:11` 5174 | 统一为一处配置源 |
| P1-5 | agent_checkpoints.db 是最脆弱的库 | `runner.py:94-106` 单连接 `sqlite3.connect(check_same_thread=False)`，无 WAL、无 busy_timeout，worker 写 / API 删并发 | 连接处加 `journal_mode=WAL; busy_timeout=5000`（两行修复），否则多用户下 `database is locked` 且会话清理静默失败 |

### 数据与可靠性类

| # | 问题 | 现状与依据 | 修复建议 |
|---|---|---|---|
| P1-6 | 无界增长：chat_turn_events / audit_events / checkpoints / worker_instances / 日志 | 仅 query_traces 30 天启动时清理（`app.py:86-95`）；`worker.log` 已 30MB 无轮转（`app.py:120` 以 `"ab"` 追加）；`logger.py:79` 按日文件名 import 时冻结，进程不重启永不换日；答案在 events 里冗余一份（`conversation.py:294,945`） | ① 日志全换 `TimedRotatingFileHandler`；② 定时清理任务：已完成 turn 的 events 过期删除、stale worker_instances、过期 auth_sessions；③ audit_events 保留但定期归档导出 |
| P1-7 | RAGFlow 卡死 `parsing` 仅靠 UI 拉取时才发现 | 超时判定（3600s）只在 `list_parse_tasks` 的 GET 路径执行（`ragflow_backend.py:1521-1578`）；独立 worker 只认领 spreadsheet（`runtime.py:90-93`） | worker 循环加一步：周期刷新超时 parsing → failed 并通知 uploader |
| P1-8 | 无部门/KB 存储配额 | 唯一限制是单请求 512MB（`upload.py:21`）；内容哈希去重挡不住近似重复 | 按 (department, kb) 聚合体积配额 + 软告警；`storage/` 共盘是所有部门的共同故障域 |
| P1-9 | token 用量算完即丢，无成本核算 | `TokenUsageSummary` 只进 OTel span（`tracing.py:147-195`）和 legacy `done` 事件（`query.py:822`）；turns 的 `metrics_json` 无 token 字段（`query.py:332-353`）；`query_traces` 无 token 列（`app_logs.py:207-231`） | `chat_turns` 加 `prompt_tokens/completion_tokens/total_tokens` 列落库；出「部门 × 月」用量报表——多部门预算/计费的前提 |
| P1-10 | 取消不中止上游 LLM 流 + 无 turn 整体 deadline | cancel 只在 chunk/tool 边界生效（`runner.py:705`、`runtime.py:281`），LLM 流继续计费至多 120s；`_run_turn` 无 wall-clock deadline | 给 langchain 模型流加 cancel 钩子；`_run_turn` 包 deadline（如 deep=10min / fast=4min） |
| P1-11 | legacy `/query` SSE 无 keepalive | 消费端阻塞在 `q.get`（`query.py:911`），120s 首 token 内代理空闲超时即断流（对比 turns SSE 有 0.4s keepalive，`query.py:702-712`） | 随 P0-2 下线/限流，主推 turns 模型 |
| P1-12 | 告警链路断裂 | 6 条 Prometheus 规则中 `HdbRetrievalEmptyRateHigh` 引用的 `hdb_retrieval_*` 指标从未被 emit（只是 span 属性，`query.py:494-500`）；**无 Alertmanager**，全部告警路由到空 | 修正/删除死规则；部署 Alertmanager 接邮件/企业微信；观察栈上生产需改默认口令（Grafana `change-me`，compose:58） |

### 工程与文档类

| # | 问题 | 现状与依据 | 修复建议 |
|---|---|---|---|
| P1-13 | 文档基准 eval 数据集不存在 | README 与 `evaluation/README.md` 引用 `evaluation/datasets/hardware_qa_v1.jsonl`（25 条），磁盘实际是 `aaa_hard_eval_v1.jsonl`（47 条）——所有文档化 eval 命令按写会失败 | 修正文档或补齐数据集；上线前至少跑一轮全量 eval 存基线 |
| P1-14 | 无部署手册/Runbook；docs/ 被 gitignore | `.gitignore:102` 忽略 `/docs/`；README 引用的 5 篇架构文档磁盘上不存在；README 有失实陈述（"`.env` 已提交"、默认端口 8000） | 取消 docs/ ignore；补《部署手册》《备份恢复手册》《故障排查手册》 |
| P1-15 | 无外部契约测试 | 全部 141 个测试基于 stub（明示 "no RAGFlow access"，`test_external_conversation_end_to_end.py:5-7`）；无真实 RAGFlow/LLM 冒烟、无浏览器 e2e | 加 nightly 契约冒烟 job（真实 RAGFlow + 1 条查询 + 1 条上传解析），防 RAGFlow 升级静默劣化 |

---

## 四、P2 — 上线后迭代清单

**安全细节**
- `/health/ready` 未认证但回显 storage 绝对路径（`health.py:34`）；structured 接口回显服务器绝对路径（`structured.py:91,203`）→ 改相对路径。
- PBKDF2 260k 迭代低于 OWASP 2023 建议 600k（`auth.py:17`）→ 下次全员改密时平滑升代。
- 过期/吊销 session 行永不清除（auth_sessions 只增不减）→ 并入 P1-6 清理任务。
- 上传无单请求文件数上限、无 MIME 嗅探（仅扩展名，`registry.py:134-170`）→ multipart 加数量上限。
- 审计哈希链无 HMAC 密钥，且 `audit_integrity_status` 全仓库零调用（`app_logs.py:503-540` 定义后无人用）→ 接入 admin 界面定期巡检；有合规要求再加 HMAC。
- `record_audit` 读-写非原子（无 `BEGIN IMMEDIATE`，`app_logs.py:289-349`），API 与 worker 并发审计可致链分叉、完整性校验永久失败 → 包事务即可。
- allowlist SQL 无执行成本/超时限制（笛卡尔积可烧 CPU，`spreadsheet_tools.py:592-650`）→ 加 `progress_handler` 步数上限。

**性能细节**
- `pipeline_documents.db` 零二级索引，worker 每 0.5s 全表扫描 3 次（`document_store_sqlite.py:822-870`，轮询间隔 `workers/main.py:117`）→ 给 status/processor_kind/created_at/content_hash 建索引。
- 每次 emit 事件触发 `touch_turn_worker` UPDATE（`query.py:297-298`）→ heartbeat 线程已保活，可降频。
- 每 chunk 查 `is_turn_cancel_requested` 开新 SQLite 连接（`query.py:277-278`、`conversation.py:1102-1105`）→ 500ms 缓存。
- `MemoryService` 每查询全量构建又销毁（`runner.py:441`）→ 进程级复用。
- `LLMClient` 用裸 `requests` 无连接池（agent 热路径走 langchain/httpx 不受影响）→ 换 Session。
- RAGFlow 客户端无传输层重试（瞬时 5xx 直接降级为 degraded 答案）；`list_datasets/list_documents` 无分页（`ragflow_backend.py:581-584,637-641`）。
- SSE push 队列满（2000）后静默丢事件且无缺口补拉（`query.py:58-65,656-658`）→ 记录丢帧游标触发 DB resync。
- 死代码：`ParseTaskManager`（`src/ingestion/parse_tasks.py`，零引用）、`record_agent_stage` 空桩（`observability/metrics.py:193-194`）。

**架构扩展（多实例化时再做）**
- 事件推送是进程内 dict（`query.py:46-51`）：多 API 实例退化为每订阅者 0.4s 轮询 auth.db → 引入 Redis pub/sub 或 PG LISTEN。
- 摄取去重锁是进程内 threading.Lock（`ingestion.py:53-81`）：多 API 实例可对同一上传竞争 → 复用 memory 子系统的 lease 围栏。
- API 被 SIGKILL 时 `subprocess.Popen` 出的 worker 成孤儿（`app.py:121-126`，无 pdeathsig）→ 改 systemd 托管后此问题消失。
- 6/7 个 SQLite 库用 bootstrap-convergence 式迁移（`CREATE TABLE IF NOT EXISTS` + DDL 字符串嗅探重建），仅 projects.db 有真 `schema_migrations` 账本（`projects/store.py:65-81`）→ 长期多部门数据建议补版本化迁移。
- `projects.db`、`document_authoring.db`、`agent_checkpoints.db` 未开 WAL（其余 5 库已开）。

---

## 五、分阶段上线路线图

**阶段 0（1~2 周，上线阻塞）**
1. worker 并发池 + WORKER_TURN_CONCURRENCY + chat/parse 循环拆分（P0-1）
2. 全局 + 每用户并发闸门，下线/限流 legacy `/query`（P0-2）
3. systemd（或 compose）+ nginx：TLS、SSE 透传、静态前端（P0-3/P0-4）
4. CI：ruff + pytest 必跑（P0-5）
5. 每日 SQLite 热备脚本 + 恢复演练（P0-6）
6. **轮换全部 API key**（P0-7）
7. agent_checkpoints.db 补 WAL/busy_timeout（P1-5，两行）

**阶段 1（上线首月）**
- 密码最小长度统一、X-Forwarded-For + 双轨锁定（P1-1/2）
- 日志轮转 + DB 清理任务（P1-6）
- chat_turns 落 token 用量 + 部门用量报表（P1-9）
- turn wall-clock deadline + LLM 流取消（P1-10）
- Alertmanager + 修死规则（P1-12）
- 存储/解析卡死巡检（P1-7）
- 文档三件套 + eval 基线修正（P1-13/14）

**阶段 2（稳定后）**
- P2 全清单按需消化
- 部门配额（P1-8）
- 若并发仍不足：评估 Postgres 迁移 + 多 API 实例 + Redis pub/sub（第四节"架构扩展"）

---

## 六、结论

当前系统**功能与安全内核已可用**，最大风险不在代码质量而在运营形态：串行 worker 决定了它现在只够"单部门试用"，无备份决定了它不敢承载唯一数据源，无 CI 决定了它不敢持续迭代。按阶段 0 七项完成后，即可支撑"多部门、每部门数十用户、日常查询为主"的内网生产场景；阶段 1 完成后具备完整的成本核算与告警闭环。

