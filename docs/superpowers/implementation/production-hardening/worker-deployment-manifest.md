# Worker 部署清单（P0-005）

> 基线：`commit 01c807e`；依据实施方案 §5.4。
> 生产初始规模：2 个 chat 进程、1 个 authoring 进程、1 个 parse 进程；先验证 SQLite 承载能力再扩容。
> 所有角色共用同一 `AUTH_DB_PATH` / `PIPELINE_DOCUMENTS_DB` / `CHAT_ATTACHMENT_INDEX_DB_PATH`（绝对路径、本机共享文件系统，WAL 生效）。

## 1. 进程与队列归属

| 进程 | 启动命令 | 消费队列/职责 | 不消费 |
|---|---|---|---|
| Chat Worker ×2 | `uv run python -m src.workers.main --role chat` | `chat_turns` claim/lease、`requeue_stale_turns`、`expire_queued_turns`、队列指标 | 文档计划/生成、解析、附件协调 |
| Authoring Worker ×1 | `uv run python -m src.workers.main --role authoring` | document plan submissions、authoring jobs（generate/resume/convert） | chat turns、解析 |
| Parse Worker ×1 | `uv run python -m src.workers.main --role parse` | RAGFlow/大文件解析 `runtime.run_once`、AttachmentWorker 解析、**专用协调控制线程**（等待 turn 扫描 + 附件协调选主） | chat turns、authoring jobs |
| Memory Worker | `uv run python -m src.memory.worker`（独立入口，不在角色路由内） | Memory 投影/抽取 | chat/authoring/parse |
| Export Worker | `uv run python -m src.result_exports.worker` | 导出任务 | 与 chat/authoring 独立 |
| （兼容/本地开发） | `uv run python -m src.workers.main --role all` | 旧组合循环 | `all` 禁止与专用角色同时运行 |

## 2. 启动顺序与就绪检查

1. 确认数据库可写且 WAL 生效：`sqlite3 storage/auth.db 'PRAGMA journal_mode;'` 返回 `wal`；
2. 启动 parse worker（持有 `coordinator_leases`，等待 turn 超时扫描每 5 秒执行）；
3. 启动 authoring worker；
4. 启动 chat workers；
5. 启动 export/memory worker（若未由 API 进程托管）；
6. 验证队列状态：`GET /api/v1/system/status` 或 `sqlite3 storage/auth.db "select status,count(*) from chat_turns group by status;"`。

## 3. 健康与接管

- 每个进程注册 `worker_registry` 心跳；Chat 任务另由 `worker_heartbeat_at` + 会话 lease（`CHAT_TURN_LEASE_SECONDS=120`）双重复核；
- 附件协调：lease 15 秒、每 5 秒续约、接管递增 `fencing_token`；多个 parse 进程可候选运行，同一时刻只有持有有效 lease 的循环扫描；
- `python -m src.attachments.worker` 独立入口与 parse 角色复用同一选主协议，不会出现两个扫描者。

## 4. 回滚

按 `rollback-runbook.md` P0-004/P0-005：先停新角色进程，等待 lease 过期/撤销，再启用 `--role all` 兼容单进程。
