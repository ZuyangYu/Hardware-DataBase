# R3 实施记录

方案：`docs/superpowers/plans/2026-10-15-multi-knowledge-base-retrieval-authoring-human-review-plan-R3.md`。
基础提交：`3c00bd97d9d6de9d8b470b271bd50d0bdccf7d45`。

## 任务状态

- [x] T0：共享检索池生命周期与旧单库兼容。
- [ ] T1：范围模型、稳定 KB ID 授权与独立上下文（实施中）。
- [ ] T2：会话/轮次范围、SQLite/PG 持久化与增量迁移。
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

## 阶段边界

T0 不等于多库问答可用。新入口保持关闭；当前未执行生产迁移、未修改运行环境 `.env`、未重启线上 API/Worker。M1/M2/M3/M4/M5 均未验收。
