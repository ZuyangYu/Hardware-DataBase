# R3 隔离 PostgreSQL 迁移、审计恢复与关闭保护

## 实际执行范围

使用新建且随机命名的一次性 `postgres:16-alpine`，所有数据为本轮合成数据；容器和数据库与既有部署分离。演练结束后该容器已删除。没有迁移业务数据库、切换应用版本或改变部署配置。

本地可复现脚本和原始报告：`storage/evaluations/multi-kb-r3-migration-20261019/`。

- `drill.py`：本轮演练脚本；报告绑定脚本哈希。
- `report.json`：15 项检查、真实 PG 测试摘要、数据行数与备份/数据哈希。
- `postgres-tests.log`：真实 PostgreSQL 回归日志。
- `post-migration-with-audit.dump`：迁移并写入候选决策后的 `pg_dump -Fc`，只含合成资料。

服务端迁移工具仍使用仓库原有 `migrate_multi_kb_scope.py` 和 `migrate_multi_kb_conflict_review.py`。本轮新增的是执行证据，没有变更迁移实现。

## 真实 PG 回归

`HDB_TEST_POSTGRES_DSN` 仅指向本轮临时 PG。以下集合 **48 passed，6 subtests passed**，0 skipped：

```bash
uv run pytest -q \
  tests/test_document_authoring_pg.py \
  tests/test_multi_kb_store_contract.py \
  tests/test_terminal_domain_postgres_multi_kb.py \
  tests/test_control_plane_schema.py
```

测试使用各自的随机临时数据库；覆盖工单/冲突持久化、并发幂等决定、范围存储和多库长期记忆隔离。

## 迁移与恢复过程

1. 在临时 PG 建立 schema_version=1 的最小旧会话/轮次表，插入一个会话和一个已完成轮次；建立缺少 R3 新列的旧冲突表，插入一个带冻结来源戳的合成事项。
2. 对两种迁移执行 `--check`，均正确返回未就绪；前后完整行快照相同。
3. 执行 `--apply` 与 `--verify`。旧会话/轮次新增范围字段为空，未伪造历史选库；冲突来源/证据戳从原始 JSON 回填，原始事项 payload 保留。
4. 再次应用两种迁移，完整行快照与 schema 迁移时间戳均不变。
5. 通过真实 `ConflictDecisionStore` 提交 `choose_design_candidate`，事项保持 `approval_pending`；相同请求返回同一决策，无重复日志。
6. 在本演练进程关闭 `MULTI_KB_HUMAN_REVIEW_ENABLED`，真实服务保护拒绝新的多库操作，返回 `multi_kb_human_review_disabled`。事项、决定和新 schema 保持不变；随后恢复本进程变量。
7. 对**包含迁移后决策历史**的数据库执行 `pg_dump -Fc`，使用 `pg_restore --exit-on-error` 恢复到另一新数据库；逐表完整行快照一致。
8. 对恢复库执行两种迁移的 verify/重复 apply，全部通过且数据不变；重放原决定仍返回原结果，决策日志仍只有 1 条。

本轮 15 项检查全部通过，耗时 41.443 秒（含真实 PG 回归及容器启动/清理）。

恢复后核对表行数：`control_plane_schema=2`、`chat_sessions=1`、`chat_turns=1`、`authoring_conflict_cases=1`、`authoring_conflict_decisions=1`。

- 含新审计历史的备份 SHA-256：`ab44e3cd620c19f0e2550351560fd40c01ad73653107fb21de6453179277bd41`。
- 迁移后/恢复后规范化完整行 SHA-256：`611b1878e6dab7cb6dc48cb9aa19165c83860f5429881fb30d6c5600ae8b6996`。

## 发布边界

这证明了 R3 增量迁移和带审计历史的数据库恢复在合成隔离样本上的一致性，以及服务层关闭保护。没有运行旧版本 Worker 二进制、切换线上 API/Worker、模拟业务持续写入中的灾难切换，或证明生产 RPO/RTO。

生产回滚应停止新准入、排空/隔离在途任务、保留新表/列/决策审计，再按兼容版本和关闭开关处理。不能恢复到缺少新决定的旧快照后称为无损回滚，也不能直接让旧 Worker 接手等待人工确认的任务。实际版本切换和负责人签署仍需独立演练。
