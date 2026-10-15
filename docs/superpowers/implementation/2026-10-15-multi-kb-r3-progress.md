# R3 实施记录

方案：`docs/superpowers/plans/2026-10-15-multi-knowledge-base-retrieval-authoring-human-review-plan-R3.md`。
基础提交：`3c00bd97d9d6de9d8b470b271bd50d0bdccf7d45`。

## 任务状态

- [x] T0：共享检索池生命周期与旧单库兼容。
- [x] T1：范围模型、稳定 KB ID 授权与独立上下文。
- [x] T2：会话/轮次范围、SQLite/PG 持久化与增量迁移。
- [ ] T3：API/Worker/Agent、历史依赖、记忆与撤权隔离。
- [ ] T4：scoped outcome、检索调度、预算、真实截止与错误语义。
- [ ] T5：证据复合身份、融合、覆盖与质量门槛。
- [ ] T6：前端选库、范围变化恢复、引用与端到端验收。
- [ ] T7：结构化来源严格范围适配。
- [ ] T8：文档来源冻结与索引代次。
- [ ] T9：规划、预览、工单、Worker、恢复贯通。
- [ ] T9.5 A–E：关键冲突检测、策略、存储/API、审阅、等待/恢复及导出门禁。
- [ ] T10：章节事实/决策/审批校验与产物授权。
- [ ] T11：真实服务、固定评测集、负载、迁移/回滚与发布验收。

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
